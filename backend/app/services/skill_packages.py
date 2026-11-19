"""不可变 Agent Skill 包：安全解析、完整性、预审与按属主/技能/版本保存。安装不执行文件。"""
import base64
import hashlib
import io
import json
import re
import stat
import zipfile
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

import yaml
from yaml.tokens import AliasToken, AnchorToken
from yaml.nodes import MappingNode, SequenceNode, ScalarNode

from app.core import db
from app.services import skill_review

MAX_BYTES = 10 * 1024 * 1024
MAX_FILES = 100
MAX_TEXT = 1024 * 1024
SCAN_VERSION = "skill-package-2"
_TEXT_SUFFIXES = {".md", ".txt", ".py", ".json", ".yaml", ".yml", ".csv", ".toml", ".sh", ".js", ".ts", ".html", ".css", ".xml"}
_BINARY_MAGIC = {".png": b"\x89PNG\r\n\x1a\n", ".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff", ".pdf": b"%PDF-"}


class PackageError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(data: bytes):
    return hashlib.sha256(data).hexdigest()


def safe_path(path: str) -> str:
    if not isinstance(path, str) or not path or len(path) > 240:
        raise PackageError("资源路径为空或过长")
    decoded = unquote(path)
    if decoded != path or "\\" in path or ":" in path or any(ord(c) < 32 for c in path):
        raise PackageError("资源路径包含非法编码或字符")
    p = PurePosixPath(path)
    if p.is_absolute() or any(s in ("", ".", "..") for s in path.split("/")):
        raise PackageError("拒绝绝对路径或目录穿越")
    if p.parts[0].lower() in ("hooks", "agents", "mcp"):
        raise PackageError("不随 Skill 安装 hooks、agents 或 MCP 配置")
    if any(s.startswith(".") for s in p.parts):
        raise PackageError("不安装隐藏配置、hooks 或仓库元数据")
    return str(p)


def _check_yaml(node, depth=0):
    if depth > 20:
        raise PackageError("YAML 嵌套层数过多")
    if isinstance(node, MappingNode):
        seen = set()
        for key, value in node.value:
            if not isinstance(key, ScalarNode) or key.tag != "tag:yaml.org,2002:str" or key.value in seen:
                raise PackageError("YAML 键必须是唯一字符串")
            seen.add(key.value)
            _check_yaml(value, depth + 1)
    elif isinstance(node, SequenceNode):
        for value in node.value:
            _check_yaml(value, depth + 1)


def parse_document(text: str) -> tuple[dict, str]:
    if len(text.encode()) > MAX_TEXT:
        raise PackageError("SKILL.md 超过单文本 1 MiB 上限")
    match = re.match(r"\A---\s*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
    if not match or len(match[1]) > 16384:
        raise PackageError("SKILL.md 需要大小受限的 YAML frontmatter")
    try:
        # 禁止别名/锚点，避免安全加载器仍能构造指数展开或循环结构。
        if any(isinstance(t, (AliasToken, AnchorToken)) for t in yaml.scan(match[1])):
            raise PackageError("YAML 不允许锚点或别名")
        _check_yaml(yaml.compose(match[1], Loader=yaml.SafeLoader))
        metadata = yaml.safe_load(match[1])
    except (yaml.YAMLError, RecursionError) as exc:
        raise PackageError("SKILL.md YAML 格式不合法") from exc
    if not isinstance(metadata, dict):
        raise PackageError("frontmatter 必须是对象")
    slug = metadata.get("name")
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or len(slug) > 64:
        raise PackageError("标准 name 必须是 1–64 字符的小写英文/数字连字符 slug")
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise PackageError("description 必须是 1–1024 字符的用途及触发说明")
    body = text[match.end():].strip()
    if not body:
        raise PackageError("SKILL.md 正文不能为空")
    for key in ("license", "compatibility"):
        if key in metadata and not isinstance(metadata[key], str):
            raise PackageError(f"{key} 必须是字符串")
    # 严格 JSON 化防止 YAML 日期/集合等类型进入不稳定 hash。
    try:
        canonical(metadata)
    except (TypeError, ValueError, RecursionError) as exc:
        raise PackageError("frontmatter 含不支持的数据类型") from exc
    return metadata, body


def _decode_file(path, content):
    if not isinstance(content, str) or len(content) > MAX_BYTES * 2:
        raise PackageError("资源编码非法或超限")
    try:
        data = base64.b64decode(content, validate=True)
    except (ValueError, TypeError) as exc:
        raise PackageError("资源不是合法 base64") from exc
    suffix = PurePosixPath(path).suffix.lower()
    is_text = suffix in _TEXT_SUFFIXES or PurePosixPath(path).name.upper().startswith(("LICENSE", "NOTICE"))
    if is_text:
        if len(data) > MAX_TEXT:
            raise PackageError(f"文本文件 {path} 超过 1 MiB")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PackageError(f"文本 {path} 不是 UTF-8") from exc
        if "\x00" in text:
            raise PackageError("文本文件含二进制内容")
        return data, text
    magic = _BINARY_MAGIC.get(suffix)
    if not magic or not data.startswith(magic):
        raise PackageError(f"拒绝未知或可执行二进制资源：{path}")
    return data, None


def validate(bundle: dict) -> dict:
    if not isinstance(bundle, dict):
        raise PackageError("Skill 包必须是对象")
    files = bundle.get("files")
    if not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES or "SKILL.md" not in files:
        raise PackageError("包需包含 SKILL.md 且文件数不能超过 100")
    total, texts, manifest, seen = 0, {}, [], set()
    for raw_path, encoded in sorted(files.items()):
        path = safe_path(raw_path)
        if path.casefold() in seen:
            raise PackageError("资源文件名存在大小写冲突")
        seen.add(path.casefold())
        data, text = _decode_file(path, encoded)
        total += len(data)
        if total > MAX_BYTES:
            raise PackageError("Skill 包超过 10 MiB 上限")
        if text is not None:
            texts[path] = text
        manifest.append({"path": path, "size": len(data), "hash": sha(data)})
    metadata, body = parse_document(texts["SKILL.md"])
    missing = set()
    for path, text in texts.items():
        if not path.endswith(".md"):
            continue
        refs = re.findall(r"\[[^\]]*\]\(([^\s)]+)(?:\s+[^)]*)?\)", text)
        refs += re.findall(r"`((?:references|scripts|assets)/[^`\s]+)`", text)
        for target in refs:
            target = target.strip("<>").split("#", 1)[0]
            if not target or urlsplit(target).scheme:
                continue
            if target.startswith("//"):
                raise PackageError("拒绝协议相对资源链接")
            if target.startswith("/") or unquote(target) != target or "\\" in target:
                raise PackageError("引用资源路径不安全")
            parts = list(PurePosixPath(path).parent.parts)
            for part in target.split("/"):
                if part in ("", "."):
                    continue
                if part == "..":
                    if not parts:
                        raise PackageError("引用越过包根目录")
                    parts.pop()
                else:
                    parts.append(part)
            joined = safe_path("/".join(parts))
            if joined not in files:
                missing.add(joined)
    provenance = bundle.get("provenance") or {}
    if not isinstance(provenance, dict):
        raise PackageError("来源元信息不合法")
    license_known = bool(provenance.get("origin") == "model_generated" or metadata.get("license") or provenance.get("license") or
                         any(PurePosixPath(p).name.upper().startswith("LICENSE") for p in files))
    return {"metadata": metadata, "body": body, "texts": texts, "files": manifest,
            "size": total, "missing": sorted(missing), "license_known": license_known,
            "hash": sha(canonical({"files": manifest}).encode())}


def requirements(bundle: dict) -> list[str]:
    required = validate(bundle)["metadata"].get("allowed-tools", [])
    if isinstance(required, str):
        required = required.split()
    if (not isinstance(required, list) or len(required) > 32
            or any(not isinstance(x, str) or not x.strip() or len(x) > 200 for x in required)):
        raise PackageError("allowed-tools 必须是最多 32 项的非空工具名称")
    return sorted(set(required) | {"script:" + p for p in bundle["files"] if p.startswith("scripts/")})


def review(bundle: dict, *, resolved_requirements=()) -> dict:
    info = validate(bundle)
    text = "\n".join(info["texts"].values())
    security = [(cat, label) for cat, label, rx in skill_review._SECURITY_RULES if rx.search(text)]
    if security:
        return {"status": "rejected", "verdict": "reject", "risk": "high", "scan_version": SCAN_VERSION,
                "reasons": [label for _, label in security], "categories": [cat for cat, _ in security]}
    reasons, dependencies = [], []
    if info["missing"]:
        reasons.append("缺少引用资源：" + "、".join(info["missing"]))
    if not info["license_known"]:
        reasons.append("来源未提供可确认的授权，包不完整")
    dependencies.extend(requirements(bundle))
    script_requirements = {r for r in dependencies if r.startswith("script:")}
    isolated_scripts_ready = bool(script_requirements) and script_requirements <= set(resolved_requirements)
    # 仅把明确引用包内 Python 文件的运行步骤适配到同源码的已审核工具；通用 shell/代码指令仍阻断。
    capability_text = text
    if isolated_scripts_ready:
        for path in info["texts"]:
            if path.startswith("scripts/") and path.endswith(".py"):
                escaped = re.escape(path)
                capability_text = re.sub(
                    rf"(?:执行|运行|run|execute)[ \t]*(?:python(?:3)?[ \t]+)?`?{escaped}`?",
                    "调用已绑定的隔离函数", capability_text, flags=re.I)
    capability = [label for category, label, rx in skill_review._CAPABILITY_RULES
                  if rx.search(capability_text if category == "exec_code" else text)]
    if capability:
        reasons.append("待适配能力：" + "、".join(capability))
    if set(dependencies) - set(resolved_requirements):
        reasons.append("blocked_dependencies：所声明工具需独立授权、运行时检查及验收；包本身不授予执行权限")
    if info["metadata"].get("compatibility"):
        reasons.append("运行环境要求尚未通过探测：" + info["metadata"]["compatibility"])
    return {"status": "needs_config" if reasons else "ready", "verdict": "pass", "risk": "",
            "reasons": reasons, "dependencies": sorted(set(dependencies)), "scan_version": SCAN_VERSION,
            "complete": not info["missing"] and info["license_known"], "package_hash": info["hash"]}


def from_files(files: dict, provenance=None):
    bundle = {"files": {safe_path(p): base64.b64encode(b).decode() for p, b in files.items()},
              "provenance": provenance or {}}
    validate(bundle)
    return bundle


def from_zip(data: bytes, provenance=None):
    if len(data) > MAX_BYTES:
        raise PackageError("压缩包超限")
    files, total = {}, 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist()) > MAX_FILES * 2:
                raise PackageError("压缩包目录条目超限")
            for entry in archive.infolist():
                safe_path(entry.filename.rstrip("/"))
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise PackageError("不允许符号链接或特殊文件")
                if entry.is_dir():
                    continue
                total += entry.file_size
                if (total > MAX_BYTES or len(files) >= MAX_FILES or entry.flag_bits & 1
                        or entry.file_size > max(1, entry.compress_size) * 100):
                    raise PackageError("加密、过量文件或压缩炸弹被拒绝")
                if entry.filename in files:
                    raise PackageError("压缩包存在重复路径")
                with archive.open(entry) as stream:
                    blob = stream.read(MAX_BYTES + 1)
                if len(blob) != entry.file_size:
                    raise PackageError("压缩包文件长度不一致")
                files[entry.filename] = blob
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise PackageError("无法读取 Skill 压缩包") from exc
    return from_files(files, provenance)


def save(conn, owner, skill_id, bundle, assessment, now):
    info = validate(bundle)
    version = info["hash"]
    conn.execute("INSERT INTO skill_package_versions VALUES (?,?,?,?,?,?,?)",
                 (owner, skill_id, version, info["metadata"]["name"], canonical(bundle), canonical(assessment), now))
    for entry in info["files"]:
        conn.execute("INSERT INTO skill_package_files VALUES (?,?,?,?,?,?)",
                     (owner, skill_id, version, entry["path"],
                      base64.b64decode(bundle["files"][entry["path"]]), entry["hash"]))


def load(owner: str, skill_id: str, *, enabled=False):
    row = db.query_one(
        "SELECT v.* FROM skill_package_versions v JOIN user_skills s ON s.id=v.skill_id AND s.owner_id=v.owner_id "
        "WHERE v.owner_id=? AND v.skill_id=?" + (" AND s.enabled=1" if enabled else "") +
        " ORDER BY v.created_at DESC,v.rowid DESC LIMIT 1", (owner, skill_id))
    if not row:
        raise PackageError("技能包不存在、未启用或无权访问")
    bundle = json.loads(row["package_json"])
    if validate(bundle)["hash"] != row["version"]:
        raise PackageError("技能包完整性核验失败")
    return bundle


def load_resource(owner: str, skill_id: str, path="SKILL.md", *, enabled=False):
    path = safe_path(path)
    bundle = load(owner, skill_id, enabled=enabled)
    if path not in bundle["files"]:
        raise PackageError("资源不存在")
    return resource(bundle, path)


def resource(bundle: dict, path: str):
    path = safe_path(path)
    if path not in bundle["files"]:
        raise PackageError("资源不存在")
    data, text = _decode_file(path, bundle["files"][path])
    return {"path": path, "title": PurePosixPath(path).name,
            "content": text, "base64": None if text is not None else base64.b64encode(data).decode(),
            "hash": sha(data), "size": len(data)}


def export_zip(bundle):
    validate(bundle)
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in bundle["files"].items():
            archive.writestr(path, base64.b64decode(content))
    return target.getvalue()
