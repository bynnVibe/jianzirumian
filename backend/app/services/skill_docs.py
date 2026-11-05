"""
见字如面 - Skill 文档库（渐进式加载 / 懒加载）

承载 OpenClaw / Anthropic Agent Skills 结构的技能说明书（backend/skills/<slug>/SKILL.md
+ references/*.md），为「Skill 配置」页面提供三层按需加载能力：

- L1 发现：列表只回标题 + 简述（由 skill.list_skills 提供，不读文档正文）；
- L2 激活：load_doc() 解析 SKILL.md 的 frontmatter + 主体，并列出可用 references（不含正文）；
- L3 执行：load_reference() 按 path 单独拉取某一篇扩展文档。

内置技能按 doc_slug 落盘读取；自定义技能无落盘文档，由 synthesize_doc() 依据库内字段即时合成，
同样走 L2 弹窗展示，保持「卡片简述 → 具体文档」的一致体验。所有磁盘读取都做目录逃逸防护。
"""
import logging
import os
import re
from typing import List, Optional

from app.config import settings
from app.services.skill import BUILTIN_SKILLS, _builtin_id

logger = logging.getLogger("jianziruyang.skill_docs")

# builtin_key → doc_slug（落盘目录名）
BUILTIN_DOC_SLUGS = {s["builtin_key"]: s["doc_slug"] for s in BUILTIN_SKILLS if s.get("doc_slug")}

_FRONTMATTER_RE = re.compile(r"^\uFEFF?---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)
_REF_DIR = "references"


def _skills_root() -> str:
    return settings.SKILLS_DIR


def _skill_dir(slug: str) -> Optional[str]:
    """安全解析技能目录：slug 只允许普通目录名，且必须真实存在于 skills 根下。"""
    if not slug or "/" in slug or "\\" in slug or slug in (".", "..") or not re.fullmatch(r"[A-Za-z0-9._-]+", slug):
        return None
    path = os.path.join(_skills_root(), slug)
    return path if os.path.isdir(path) else None


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """拆出 YAML frontmatter（简单 key: value）与主体 Markdown。"""
    m = _FRONTMATTER_RE.match(text or "")
    if not m:
        return {}, (text or "").strip()
    meta, body = {}, m.group(2).strip()
    for line in m.group(1).splitlines():
        idx = line.find(":")
        if idx <= 0:
            continue
        key = line[:idx].strip().lower()
        val = line[idx + 1:].strip().strip("\"'")
        if key:
            meta[key] = val
    return meta, body


def _first_heading(text: str, fallback: str) -> str:
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip() or fallback
    return fallback


def _list_references(slug: str) -> List[dict]:
    """列出技能 references/ 下的扩展文档（仅路径 + 标题，不读正文——L3 才按需拉取）。"""
    base = _skill_dir(slug)
    if not base:
        return []
    ref_dir = os.path.join(base, _REF_DIR)
    if not os.path.isdir(ref_dir):
        return []
    items = []
    for fn in sorted(os.listdir(ref_dir)):
        if not fn.endswith(".md") or not os.path.isfile(os.path.join(ref_dir, fn)):
            continue
        rel = f"{_REF_DIR}/{fn}"
        try:
            with open(os.path.join(ref_dir, fn), encoding="utf-8") as f:
                title = _first_heading(f.read(), fn[:-3])
        except OSError:
            title = fn[:-3]
        items.append({"path": rel, "title": title})
    return items


def load_doc(slug: str) -> Optional[dict]:
    """L2：读取 SKILL.md 的 frontmatter + 主体，并附可用 references 列表（不含正文）。"""
    base = _skill_dir(slug)
    if not base:
        return None
    skill_md = os.path.join(base, "SKILL.md")
    if not os.path.isfile(skill_md):
        return None
    try:
        with open(skill_md, encoding="utf-8") as f:
            meta, body = _parse_frontmatter(f.read())
    except OSError as e:
        logger.warning("读取技能文档失败 slug=%s: %s", slug, e)
        return None
    title = meta.get("title") or meta.get("name") or slug
    return {
        "slug": slug,
        "name": meta.get("name") or slug,
        "title": title,
        "description": meta.get("description", ""),
        "kind": meta.get("kind", ""),
        "scope": meta.get("scope", ""),
        "version": meta.get("version", ""),
        "body": body,
        "references": _list_references(slug),
        "source": "file",
    }


def load_reference(slug: str, ref_path: str) -> Optional[dict]:
    """L3：按需拉取 references/ 下单篇扩展文档；做目录逃逸防护，仅限 .md。"""
    base = _skill_dir(slug)
    if not base or not ref_path:
        return None
    # 归一化并校验：必须落在 <skill>/references/ 内、以 .md 结尾、无 .. 逃逸
    norm = ref_path.replace("\\", "/").lstrip("/")
    if norm.startswith("../") or "/../" in norm or not norm.endswith(".md"):
        return None
    full = os.path.normpath(os.path.join(base, norm))
    allowed_root = os.path.normpath(os.path.join(base, _REF_DIR))
    if not full.startswith(allowed_root + os.sep) or not os.path.isfile(full):
        return None
    try:
        with open(full, encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        logger.warning("读取技能参考文档失败 slug=%s path=%s: %s", slug, ref_path, e)
        return None
    return {"path": norm, "title": _first_heading(content, os.path.basename(norm)[:-3]), "content": content}


def resolve_builtin_slug(owner_id: str, skill_id: str) -> Optional[str]:
    """把某用户的内置技能行 id 反查到 doc_slug（内置 id 由 owner+builtin_key 确定性生成）。"""
    if not owner_id or not skill_id:
        return None
    for seed in BUILTIN_SKILLS:
        if not seed.get("doc_slug"):
            continue
        if _builtin_id(owner_id, seed["builtin_key"]) == skill_id:
            return seed["doc_slug"]
    return None


def synthesize_doc(skill: dict) -> dict:
    """为无落盘文档的技能（自定义技能）即时合成 L2 文档，保持弹窗体验一致。"""
    kind_label = "对话提示 (prompt)" if skill.get("kind") == "prompt" else "策略 (playbook)"
    scope = skill.get("scope") or ""
    content = skill.get("content") or ""
    fenced = content if skill.get("kind") == "prompt" else f"```json\n{content}\n```"
    body_lines = [
        f"# {skill.get('name', '技能')}",
        "",
        "## Overview",
        "",
        skill.get("description") or "（该技能未填写简要说明）",
        "",
        "## 元信息",
        "",
        f"- 类别：{kind_label}",
        f"- 作用域：{scope or 'chat'}",
        f"- 来源：用户自定义技能（无落盘 SKILL.md，本文档由库内字段即时合成）",
        "",
        "## 内容",
        "",
        fenced.strip(),
    ]
    return {
        "slug": None,
        "name": skill.get("name", ""),
        "title": skill.get("name", "技能"),
        "description": skill.get("description", ""),
        "kind": skill.get("kind", ""),
        "scope": scope,
        "version": "",
        "body": "\n".join(body_lines),
        "references": [],
        "source": "synthesized",
    }


def doc_for_skill(owner_id: str, skill: dict) -> Optional[dict]:
    """按技能行取 L2 文档：内置技能读落盘 SKILL.md，自定义技能即时合成。"""
    if skill.get("builtin"):
        slug = skill.get("doc_slug") or resolve_builtin_slug(owner_id, skill["id"])
        if slug:
            doc = load_doc(slug)
            if doc:
                return doc
        # 内置技能文档缺失时退回合成，保证页面始终可展开
    return synthesize_doc(skill)


def index_docs() -> List[dict]:
    """L1 发现层：扫描 skills 根目录，返回各技能文档的标题+简述（不读主体）。"""
    root = _skills_root()
    if not os.path.isdir(root):
        return []
    out = []
    for slug in sorted(os.listdir(root)):
        skill_md = os.path.join(root, slug, "SKILL.md")
        if not os.path.isfile(skill_md):
            continue
        try:
            with open(skill_md, encoding="utf-8") as f:
                meta, _ = _parse_frontmatter(f.read())
        except OSError:
            continue
        out.append({
            "slug": slug,
            "title": meta.get("title") or meta.get("name") or slug,
            "description": meta.get("description", ""),
            "kind": meta.get("kind", ""),
            "scope": meta.get("scope", ""),
            "builtin_key": meta.get("builtin_key", ""),
            "version": meta.get("version", ""),
        })
    return out
