"""
见字如面 - 模型网关安全审计引擎

内置风险检测规则，自动扫描请求中的：
- 凭证泄露（API Key / Cookie / 私钥 / JWT / 云厂商 Token）
- 敏感路径访问（~/.ssh、id_rsa、.env、/etc/passwd、.aws/credentials）
- 工具命令外联（curl / wget、管道到 shell、rm -rf、base64 -d、反弹 shell）
- Unicode 隐写字符（零宽字符、双向控制符、Tag 隐藏字符）
- 追踪像素 / 埋点上报（1x1 gif、analytics、/collect、beacon）
- 公网 IP 探测（ifconfig.me、ipinfo.io 等）与请求中的公网 IP 地址

支持四种审计模式：audit（只审计）、warn（警告）、mask（脱敏后转发）、block（阻断）。
内置规则可停用（disabled_rules），可自定义黑名单（追加规则）与白名单（忽略命中）。

设计原则：本引擎只做纯函数式的文本扫描，不触碰数据库；模式决策与脱敏结果由调用方
（proxy）消费。任何异常都降级为“无风险”，绝不因审计失败阻断正常调用。
"""
import logging
import re
import unicodedata
from typing import Callable, List, Optional

logger = logging.getLogger("jianziruyang.gateway.security")

# 严重程度权重（同时作为风险分）：critical=100 / high=60 / medium=30 / low=10
SEVERITY_WEIGHT = {"low": 10, "medium": 30, "high": 60, "critical": 100}
SEVERITY_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

# 审计模式
AUDIT_MODES = ("audit", "warn", "mask", "block")

# 脱敏占位符（转发给上游时替换命中内容）
MASK_TOKEN = "[REDACTED]"

# 检测开关门控：某些类别规则受独立的检测开关控制
GATE_UNICODE = "unicode_detection"
GATE_TOOL = "tool_cmd_detection"
GATE_OUTBOUND = "outbound_detection"


# ---------------------------------------------------------------------------
# 特殊检测器（非纯正则）
# ---------------------------------------------------------------------------

# Unicode 隐写：零宽 / 双向控制 / 词连接符 / BOM / 软连字符 / Tag 隐藏字符
_INVISIBLE_RANGES = [
    (0x200B, 0x200F),  # 零宽空格/连接符/方向标记
    (0x202A, 0x202E),  # 双向文本控制符
    (0x2060, 0x2064),  # 词连接符 / 不可见操作符
    (0x2066, 0x206F),  # 方向隔离 / 弃用格式符
    (0xFE00, 0xFE0F),  # 变体选择符
    (0xE0000, 0xE007F),  # Tag 字符（Claude Code 隐写上报常用区）
]
_INVISIBLE_SINGLES = {0x00AD, 0xFEFF, 0x180E, 0x034F}


def _detect_unicode_steg(text: str) -> List[dict]:
    """扫描不可见 / 隐写 Unicode 字符，返回命中片段（含码点说明）。"""
    hits: List[dict] = []
    sample = []
    count = 0
    for ch in text:
        cp = ord(ch)
        invisible = cp in _INVISIBLE_SINGLES or any(lo <= cp <= hi for lo, hi in _INVISIBLE_RANGES)
        if invisible:
            count += 1
            if len(sample) < 12:
                name = unicodedata.name(ch, "UNKNOWN")
                sample.append(f"U+{cp:04X}({name})")
    if count:
        hits.append({
            "start": 0,
            "end": len(text),
            "matched": f"检测到 {count} 个隐写字符: {', '.join(sample)}",
            "char_count": count,
        })
    return hits


# IPv4 正则；命中后进一步判断是否为公网地址
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _is_public_ip(ip: str) -> bool:
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return False
    if any(n < 0 or n > 255 for n in nums):
        return False
    a, b = nums[0], nums[1]
    if a == 10 or a == 127 or a == 0:
        return False
    if a == 172 and 16 <= b <= 31:
        return False
    if a == 192 and b == 168:
        return False
    if a == 169 and b == 254:
        return False
    if a >= 224:  # 组播 / 保留
        return False
    return True


def _detect_public_ip(text: str) -> List[dict]:
    hits = []
    for m in _IPV4_RE.finditer(text):
        ip = m.group(0)
        if _is_public_ip(ip):
            hits.append({"start": m.start(), "end": m.end(), "matched": ip})
    return hits


# 特殊检测器注册表
_SPECIAL_DETECTORS: dict[str, Callable[[str], List[dict]]] = {
    "unicode_steg": _detect_unicode_steg,
    "public_ip": _detect_public_ip,
}


# ---------------------------------------------------------------------------
# 内置规则库
# ---------------------------------------------------------------------------
# 每条规则：id / name / category / severity / gate(检测开关，None=始终启用)
#          + pattern(正则字符串) 或 special(特殊检测器名)
_BUILTIN_RULES = [
    # ---- 凭证泄露 ----
    {"id": "openai_key", "name": "OpenAI API Key", "category": "credential_leak",
     "severity": "critical", "pattern": r"\bsk-[A-Za-z0-9]{20,}\b"},
    {"id": "anthropic_key", "name": "Anthropic API Key", "category": "credential_leak",
     "severity": "critical", "pattern": r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"},
    {"id": "aws_access_key", "name": "AWS Access Key ID", "category": "credential_leak",
     "severity": "critical", "pattern": r"\bAKIA[0-9A-Z]{16}\b"},
    {"id": "private_key", "name": "私钥文件内容", "category": "credential_leak",
     "severity": "critical", "pattern": r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"},
    {"id": "github_token", "name": "GitHub Token", "category": "credential_leak",
     "severity": "critical", "pattern": r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"},
    {"id": "slack_token", "name": "Slack Token", "category": "credential_leak",
     "severity": "high", "pattern": r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"},
    {"id": "bearer_token", "name": "Bearer 令牌", "category": "credential_leak",
     "severity": "high", "pattern": r"\bBearer\s+[A-Za-z0-9\-_\.]{20,}"},
    {"id": "cookie_header", "name": "Cookie 凭证", "category": "credential_leak",
     "severity": "high", "pattern": r"(?i)\bcookie\s*[:=]\s*[^\n]{12,}"},
    {"id": "generic_secret", "name": "通用密钥/口令赋值", "category": "credential_leak",
     "severity": "high",
     "pattern": r"(?i)\b(api[_\-]?key|apikey|secret|access[_\-]?token|auth[_\-]?token|password|passwd|pwd)\b\s*[:=]\s*['\"]?[A-Za-z0-9\-_\.]{8,}"},
    {"id": "jwt_token", "name": "JWT 令牌", "category": "credential_leak",
     "severity": "medium", "pattern": r"\beyJ[A-Za-z0-9_\-]{6,}\.eyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}"},

    # ---- 敏感路径访问 ----
    {"id": "ssh_dir", "name": "SSH 目录/密钥", "category": "sensitive_path",
     "severity": "critical", "pattern": r"(?:~|\.|\b)/?\.ssh/|\bid_rsa\b|\bid_ed25519\b|\bid_dsa\b"},
    {"id": "aws_credentials", "name": "AWS 凭证文件", "category": "sensitive_path",
     "severity": "critical", "pattern": r"\.aws/credentials"},
    {"id": "dotenv_file", "name": ".env 环境变量文件", "category": "sensitive_path",
     "severity": "high", "pattern": r"(?i)\.env(?:\.[a-z]+)?\b"},
    {"id": "etc_passwd", "name": "/etc/passwd 等系统文件", "category": "sensitive_path",
     "severity": "high", "pattern": r"/etc/(?:passwd|shadow|sudoers)\b"},
    {"id": "git_config", "name": ".git/config", "category": "sensitive_path",
     "severity": "medium", "pattern": r"\.git/config"},

    # ---- 工具命令外联（受 tool_cmd_detection 开关控制） ----
    {"id": "curl_wget", "name": "curl/wget 外联", "category": "tool_command",
     "severity": "high", "gate": GATE_TOOL, "pattern": r"\b(?:curl|wget)\b\s+[^\n]{0,80}"},
    {"id": "pipe_to_shell", "name": "管道执行 shell", "category": "tool_command",
     "severity": "critical", "gate": GATE_TOOL, "pattern": r"\|\s*(?:sudo\s+)?(?:sh|bash|zsh|python3?|perl|ruby|node)\b"},
    {"id": "rm_rf", "name": "危险删除 rm -rf", "category": "tool_command",
     "severity": "critical", "gate": GATE_TOOL, "pattern": r"\brm\s+-[rf]{1,2}\b"},
    {"id": "base64_decode", "name": "base64 解码执行", "category": "tool_command",
     "severity": "high", "gate": GATE_TOOL, "pattern": r"\bbase64\s+(?:-d|--decode)\b"},
    {"id": "reverse_shell", "name": "反弹 shell", "category": "tool_command",
     "severity": "critical", "gate": GATE_TOOL, "pattern": r"\bnc\b[^\n]*-e\b|/dev/tcp/\d"},
    {"id": "chmod_777", "name": "chmod 777 放权", "category": "tool_command",
     "severity": "medium", "gate": GATE_TOOL, "pattern": r"\bchmod\s+777\b"},

    # ---- 外联追踪 / 公网探测（受 outbound_detection 开关控制） ----
    {"id": "ip_probe_service", "name": "公网 IP 探测服务", "category": "outbound",
     "severity": "high", "gate": GATE_OUTBOUND,
     "pattern": r"(?i)\b(?:ifconfig\.me|ipinfo\.io|ip\.sb|icanhazip\.com|api\.ipify\.org|whatismyip|checkip\.dyndns)\b"},
    {"id": "exfil_service", "name": "数据外泄中转服务", "category": "outbound",
     "severity": "high", "gate": GATE_OUTBOUND,
     "pattern": r"(?i)\b(?:webhook\.site|requestbin|ngrok\.io|burpcollaborator|pipedream\.net|paste\.ee)\b"},
    {"id": "tracking_pixel", "name": "追踪像素/埋点上报", "category": "outbound",
     "severity": "medium", "gate": GATE_OUTBOUND,
     "pattern": r"(?i)(?:1x1(?:\.gif|\.png)|/pixel\.gif|google-analytics\.com|/collect\?|beacon\.|/track(?:er|ing)?/|\bmc\.yandex\b)"},
    {"id": "public_ip_addr", "name": "公网 IP 地址", "category": "outbound",
     "severity": "medium", "gate": GATE_OUTBOUND, "special": "public_ip"},

    # ---- Unicode 隐写（受 unicode_detection 开关控制） ----
    {"id": "unicode_steg", "name": "Unicode 隐写字符", "category": "unicode_steg",
     "severity": "critical", "gate": GATE_UNICODE, "special": "unicode_steg"},
]

# 预编译正则
_COMPILED: dict[str, re.Pattern] = {}
for _r in _BUILTIN_RULES:
    if _r.get("pattern"):
        _COMPILED[_r["id"]] = re.compile(_r["pattern"])

CATEGORY_LABELS = {
    "credential_leak": "凭证泄露",
    "sensitive_path": "敏感路径",
    "tool_command": "工具命令",
    "outbound": "外联追踪",
    "unicode_steg": "Unicode 隐写",
    "custom_blacklist": "自定义黑名单",
}


def list_builtin_rules() -> List[dict]:
    """返回内置规则元信息（供前端安全审计中心展示与启停）。"""
    return [
        {
            "id": r["id"], "name": r["name"], "category": r["category"],
            "category_label": CATEGORY_LABELS.get(r["category"], r["category"]),
            "severity": r["severity"], "gate": r.get("gate") or "",
            "builtin": True,
        }
        for r in _BUILTIN_RULES
    ]


# ---------------------------------------------------------------------------
# 扫描核心
# ---------------------------------------------------------------------------

def _whitelisted(matched: str, whitelist: List[str]) -> bool:
    ml = matched.lower()
    return any(w and w.lower() in ml for w in whitelist)


def _scan_blacklist(text: str, blacklist: List[str]) -> List[dict]:
    """自定义黑名单：支持正则（能编译则按正则，否则按关键词子串）。命中记为 high。"""
    findings = []
    for entry in blacklist:
        entry = (entry or "").strip()
        if not entry:
            continue
        try:
            rx = re.compile(entry)
            for m in rx.finditer(text):
                if m.group(0):
                    findings.append(_mk_finding(
                        "custom_blacklist", f"黑名单: {entry}", "custom_blacklist",
                        "high", m.start(), m.end(), m.group(0)))
        except re.error:
            low_text, low_entry = text.lower(), entry.lower()
            idx = low_text.find(low_entry)
            while idx >= 0:
                findings.append(_mk_finding(
                    "custom_blacklist", f"黑名单: {entry}", "custom_blacklist",
                    "high", idx, idx + len(entry), text[idx:idx + len(entry)]))
                idx = low_text.find(low_entry, idx + len(entry))
    return findings


def _mk_finding(rule_id: str, rule_name: str, category: str, severity: str,
                start: int, end: int, matched: str) -> dict:
    return {
        "rule_id": rule_id, "rule_name": rule_name, "category": category,
        "category_label": CATEGORY_LABELS.get(category, category),
        "severity": severity, "score": SEVERITY_WEIGHT.get(severity, 10),
        "start": start, "end": end,
        "matched": (matched or "")[:200],
    }


def scan_text(text: str, settings: dict) -> dict:
    """扫描单段文本，返回 {findings, risk_level, risk_score}。

    settings 需包含：disabled_rules(list)、blacklist(list)、whitelist(list)、
    以及三个检测开关（unicode_detection / tool_cmd_detection / outbound_detection，0/1）。
    """
    result = {"findings": [], "risk_level": "none", "risk_score": 0}
    if not text:
        return result
    try:
        disabled = set(settings.get("disabled_rules") or [])
        whitelist = settings.get("whitelist") or []
        blacklist = settings.get("blacklist") or []
        findings: List[dict] = []

        for rule in _BUILTIN_RULES:
            rid = rule["id"]
            if rid in disabled:
                continue
            gate = rule.get("gate")
            if gate and not int(settings.get(gate, 1) or 0):
                continue
            special = rule.get("special")
            if special:
                detector = _SPECIAL_DETECTORS.get(special)
                raw_hits = detector(text) if detector else []
            else:
                rx = _COMPILED.get(rid)
                raw_hits = [{"start": m.start(), "end": m.end(), "matched": m.group(0)}
                            for m in rx.finditer(text)] if rx else []
            for h in raw_hits:
                matched = h.get("matched", "")
                # Unicode 隐写命中的是整段说明，不做白名单过滤
                if special != "unicode_steg" and _whitelisted(matched, whitelist):
                    continue
                findings.append(_mk_finding(
                    rid, rule["name"], rule["category"], rule["severity"],
                    h.get("start", 0), h.get("end", 0), matched))

        findings.extend(_scan_blacklist(text, blacklist))

        if findings:
            risk_score = max(f["score"] for f in findings)
            risk_level = max((f["severity"] for f in findings),
                             key=lambda s: SEVERITY_ORDER.get(s, 0))
            result.update({"findings": findings, "risk_level": risk_level, "risk_score": risk_score})
    except Exception as e:  # 审计绝不阻断主流程
        logger.warning("安全扫描失败（降级为无风险）: %s", e)
    return result


def mask_text(text: str, settings: dict) -> tuple[str, List[dict]]:
    """在 scan 基础上对命中片段做脱敏，返回 (masked_text, findings)。

    - Unicode 隐写字符：直接剔除（不替换为占位符，避免污染语义）。
    - 其余命中：替换为 MASK_TOKEN。重叠片段按起点排序后合并处理。
    """
    scanned = scan_text(text, settings)
    findings = scanned["findings"]
    if not findings:
        return text, findings
    try:
        # 先剔除隐写字符
        cleaned = "".join(
            ch for ch in text
            if not (ord(ch) in _INVISIBLE_SINGLES
                    or any(lo <= ord(ch) <= hi for lo, hi in _INVISIBLE_RANGES))
        )
        # 其余规则在 cleaned 上重新定位并替换（重新扫描避免偏移错乱）
        non_steg = [f for f in findings if f["category"] != "unicode_steg"]
        if non_steg:
            rescan = scan_text(cleaned, settings)
            spans = sorted(
                [(f["start"], f["end"]) for f in rescan["findings"]
                 if f["category"] != "unicode_steg"],
                key=lambda s: s[0],
            )
            out, last = [], 0
            for start, end in spans:
                if start < last:  # 跳过重叠
                    continue
                out.append(cleaned[last:start])
                out.append(MASK_TOKEN)
                last = end
            out.append(cleaned[last:])
            cleaned = "".join(out)
        return cleaned, findings
    except Exception as e:
        logger.warning("脱敏失败（返回原文）: %s", e)
        return text, findings


def decide_action(findings: List[dict], mode: str) -> str:
    """根据审计模式与命中情况决定动作：'' | 'warn' | 'mask' | 'block'。

    - audit：只记录，不改变请求（动作记为 ''，风险仍入库展示）
    - warn ：记录并在响应头/日志标注警告，仍放行
    - mask ：命中时脱敏后放行
    - block：命中即阻断（返回 403）
    无命中时一律返回 ''。
    """
    if not findings:
        return ""
    if mode == "block":
        return "block"
    if mode == "mask":
        return "mask"
    if mode == "warn":
        return "warn"
    return ""


def scan_payload(obj, settings: dict, do_mask: bool = False):
    """递归扫描 JSON 结构（dict/list/str）中的所有字符串。

    返回 (result_obj, findings, risk_level, risk_score)：
    - do_mask=True 时 result_obj 为脱敏后的副本，否则为原对象。
    - findings 为去重合并后的命中列表（跨字段聚合）。
    """
    all_findings: List[dict] = []
    top_level = {"none": 0}

    def _walk(node):
        if isinstance(node, str):
            if do_mask:
                masked, fnd = mask_text(node, settings)
            else:
                scanned = scan_text(node, settings)
                masked, fnd = node, scanned["findings"]
            all_findings.extend(fnd)
            return masked
        if isinstance(node, list):
            return [_walk(x) for x in node]
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        return node

    try:
        result_obj = _walk(obj)
    except Exception as e:
        logger.warning("payload 扫描失败（返回原对象）: %s", e)
        return obj, [], "none", 0

    if all_findings:
        risk_score = max(f["score"] for f in all_findings)
        risk_level = max((f["severity"] for f in all_findings),
                         key=lambda s: SEVERITY_ORDER.get(s, 0))
    else:
        risk_score, risk_level = 0, "none"
    _ = top_level  # 保留占位，避免误用
    return result_obj, all_findings, risk_level, risk_score
