"""
见字如面 - 用户 Skill 服务

借鉴成熟 Agent「Skill」理念：把可复用的领域能力沉淀为可管理、可开关的技能条目，
让用户在侧边栏「Skill 配置」中管理自己的技能并决定是否应用。支持两类技能：

- prompt（scope=chat）：注入到对话系统提示的一段指令，用于定制回答风格/领域约束；
- playbook（scope=url_import）：链接解析的站点抓取策略（JSON 规则），
  针对公众号/CSDN 等站点沉淀「移动端 UA、渲染优先」等回退策略，提升抓取成功率。

技能落库到 SQLite user_skills 表；内置技能按 owner 幂等种子化，可开关但不可删除/改内容。
"""
import json
import logging
import re
import uuid
from datetime import datetime
from typing import List, Optional

from app.config import settings
from app.core import db
from app.services import skill_review

logger = logging.getLogger("jianziruyang.skill")

# 技能类别 → 允许的作用域：prompt 仅对话；playbook 可驱动链接抓取或正文清洗
SKILL_KINDS = ("prompt", "playbook")
KIND_SCOPES = {"prompt": ("chat",), "playbook": ("url_import", "polish")}
SCOPE_LABEL = {"chat": "对话", "url_import": "链接解析", "polish": "正文清洗"}

_MAX_NAME = 60
_MAX_DESC = 200
_MAX_PROMPT_CHARS = 4000
# playbook 规则条数上限，避免恶意超大 JSON
_MAX_PLAYBOOK_RULES = 30

# 敏感信息不写入技能内容（与记忆服务一致的底线）
_SENSITIVE_RE = re.compile(r"密码|password|token|api[_\s-]?key|身份证|银行卡|信用卡", re.I)

# ---- 内置技能种子（按 owner 幂等）----
# url playbook：把「链接抓取回退策略」沉淀为可开关的内置技能（Q3 场景）
_BUILTIN_URL_PLAYBOOK_RULES = [
    {"match": "mp.weixin.qq.com", "mobile_ua": True,
     "note": "微信公众号文章：移动端 UA 更易拿到完整正文"},
    {"match": "csdn.net", "prefer_render": True,
     "note": "CSDN：正文常延迟加载/含反爬，渲染服务可用时优先隔离渲染"},
]

# 内置正文清洗策略（「内容润色」的确定性规则，scope=polish）
_BUILTIN_POLISH_POLICY = {
    "remove_footnote_markers": True,
    "remove_noise_lines": True,
    "collapse_spaces": True,
    "collapse_blank_lines": True,
    "extra_line_patterns": [],
}

BUILTIN_SKILLS = [
    {
        "builtin_key": "url_fetch_fallback",
        "doc_slug": "web-content-fetch-fallback",
        "name": "网页链接抓取回退策略",
        "description": "针对公众号/CSDN 等站点的链接抓取策略：移动端 UA、渲染优先，"
                       "配合安全抓取器的 TLS/超时回退，提升知识库链接解析成功率。",
        "kind": "playbook",
        "scope": "url_import",
        "content": json.dumps(_BUILTIN_URL_PLAYBOOK_RULES, ensure_ascii=False),
        "enabled": True,
    },
    {
        "builtin_key": "article_clean_policy",
        "doc_slug": "article-content-clean",
        "name": "链接正文清洗策略",
        "description": "链接解析正文「内容润色」的确定性清洗规则：去角标/页码/版权/关注引导等噪声行与多余空格、"
                       "压缩空行；保留段落顺序与句子完整性，不新增任何内容。",
        "kind": "playbook",
        "scope": "polish",
        "content": json.dumps(_BUILTIN_POLISH_POLICY, ensure_ascii=False),
        "enabled": True,
    },
    {
        "builtin_key": "plain_health_summary",
        "doc_slug": "plain-health-summary",
        "name": "通俗化健康结论",
        "description": "回答健康养生问题时，先用一句通俗的话给出结论，再展开专业细节。默认关闭，可按需开启。",
        "kind": "prompt",
        "scope": "chat",
        "content": "在回答健康养生类问题时，请先用一句通俗易懂的话概括核心结论，再给出专业、有据可查的细节。",
        "enabled": False,
    },
]


def _now() -> str:
    return datetime.now().isoformat()


def _builtin_id(owner_id: str, builtin_key: str) -> str:
    """内置技能使用确定性 id，保证按 owner 幂等种子化、不重复插入。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"jzrm-skill:{owner_id}:{builtin_key}"))


def _row_to_dict(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "kind": row["kind"],
        "scope": row["scope"],
        "content": row["content"],
        "enabled": bool(row["enabled"]),
        "builtin": bool(row["builtin"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _validate_playbook(content: str, scope: str = "url_import") -> Optional[str]:
    """校验 playbook 内容；按作用域分流：url_import 为规则数组，polish 为清洗策略对象。"""
    if scope == "polish":
        return _validate_polish_policy(content)
    try:
        rules = json.loads(content)
    except (ValueError, TypeError):
        return "playbook 内容必须是合法 JSON"
    if not isinstance(rules, list) or not rules:
        return "playbook 内容必须是非空规则数组"
    if len(rules) > _MAX_PLAYBOOK_RULES:
        return f"playbook 规则数超过上限 {_MAX_PLAYBOOK_RULES}"
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("match"), str) or not rule["match"].strip():
            return "每条规则需包含非空的 match 主机名"
        for flag in ("mobile_ua", "prefer_render"):
            if flag in rule and not isinstance(rule[flag], bool):
                return f"规则字段 {flag} 必须是布尔值"
    return None


# 正文清洗策略允许的字段
_POLISH_BOOL_KEYS = ("remove_footnote_markers", "remove_noise_lines",
                     "collapse_spaces", "collapse_blank_lines")
_MAX_EXTRA_PATTERNS = 10


def _validate_polish_policy(content: str) -> Optional[str]:
    """校验 polish playbook 为合法清洗策略 JSON 对象；返回错误信息或 None。"""
    try:
        policy = json.loads(content)
    except (ValueError, TypeError):
        return "正文清洗策略内容必须是合法 JSON"
    if not isinstance(policy, dict) or not policy:
        return "正文清洗策略必须是非空 JSON 对象"
    unknown = set(policy) - set(_POLISH_BOOL_KEYS) - {"extra_line_patterns"}
    if unknown:
        return f"包含不支持的字段：{', '.join(sorted(unknown))}"
    for key in _POLISH_BOOL_KEYS:
        if key in policy and not isinstance(policy[key], bool):
            return f"字段 {key} 必须是布尔值"
    patterns = policy.get("extra_line_patterns", [])
    if not isinstance(patterns, list) or len(patterns) > _MAX_EXTRA_PATTERNS:
        return f"extra_line_patterns 必须是不超过 {_MAX_EXTRA_PATTERNS} 条的数组"
    for p in patterns:
        if not isinstance(p, str) or not p.strip() or len(p) > 120:
            return "extra_line_patterns 每条须为不超过 120 字的非空字符串"
        try:
            re.compile(p)
        except re.error:
            return f"extra_line_patterns 中存在非法正则：{p[:40]}"
    return None


class SkillService:
    """用户技能管理：CRUD + 开关 + 内置种子 + 消费侧解析。"""

    # ---- 内置种子 ----
    def ensure_builtin(self, owner_id: str) -> None:
        """为某用户幂等种子化内置技能（已存在则不重复插入，保留用户的开关状态）。"""
        if not owner_id:
            return
        now = _now()
        for seed in BUILTIN_SKILLS:
            sid = _builtin_id(owner_id, seed["builtin_key"])
            exists = db.query_one("SELECT id FROM user_skills WHERE id = ?", (sid,))
            if exists:
                continue
            db.execute(
                "INSERT INTO user_skills (id, owner_id, name, description, kind, scope,"
                " content, enabled, builtin, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,1,?,?)",
                (sid, owner_id, seed["name"], seed["description"], seed["kind"],
                 seed.get("scope") or KIND_SCOPES[seed["kind"]][0], seed["content"],
                 1 if seed["enabled"] else 0, now, now),
            )

    # ---- 查询 ----
    def list_skills(self, owner_id: str) -> List[dict]:
        self.ensure_builtin(owner_id)
        # 内置技能按插入顺序（rowid）稳定排列，避免开关后 updated_at 变化导致卡片跳动；
        # 自定义技能按最近更新倒序，最新编辑的排在前面。
        rows = db.query(
            "SELECT * FROM user_skills WHERE owner_id = ?"
            " ORDER BY builtin DESC, CASE WHEN builtin = 1 THEN rowid END ASC, updated_at DESC",
            (owner_id,),
        )
        # 内置技能附上文档 slug（页面卡片据此懒加载对应 SKILL.md）；自定义技能无落盘文档。
        slug_by_id = {_builtin_id(owner_id, s["builtin_key"]): s.get("doc_slug")
                      for s in BUILTIN_SKILLS if s.get("doc_slug")}
        result = []
        for r in rows:
            item = _row_to_dict(r)
            item["doc_slug"] = slug_by_id.get(item["id"]) if item["builtin"] else None
            result.append(item)
        return result

    def get_skill(self, owner_id: str, skill_id: str) -> Optional[dict]:
        row = db.query_one(
            "SELECT * FROM user_skills WHERE id = ? AND owner_id = ?", (skill_id, owner_id)
        )
        return _row_to_dict(row) if row else None

    # ---- 写入 ----
    def create_skill(self, owner_id: str, name: str, kind: str, content: str,
                     description: str = "", enabled: bool = True,
                     scope: Optional[str] = None, actor_name: str = "") -> dict:
        """创建自定义技能；先做安全/能力预审，通过后落库。返回 {'error': ...} 表示失败。"""
        name = (name or "").strip()
        description = (description or "").strip()
        content = content or ""
        if not name or len(name) > _MAX_NAME:
            return {"error": f"技能名称必填且不超过 {_MAX_NAME} 字"}
        if len(description) > _MAX_DESC:
            return {"error": f"技能描述不超过 {_MAX_DESC} 字"}
        if kind not in SKILL_KINDS:
            return {"error": "技能类别仅支持 prompt 或 playbook"}
        # scope 缺省取该类别的首选作用域；显式传入时必须在允许集内
        scope = scope or KIND_SCOPES[kind][0]
        if scope not in KIND_SCOPES[kind]:
            allowed = "、".join(SCOPE_LABEL.get(s, s) for s in KIND_SCOPES[kind])
            return {"error": f"{kind} 技能的作用域仅支持：{allowed}"}
        if _SENSITIVE_RE.search(content) or _SENSITIVE_RE.search(name):
            return {"error": "技能内容不得包含密码/密钥/证件号等敏感信息"}
        if kind == "prompt":
            if not content.strip():
                return {"error": "prompt 技能内容不能为空"}
            if len(content) > _MAX_PROMPT_CHARS:
                return {"error": f"prompt 技能内容不超过 {_MAX_PROMPT_CHARS} 字"}
        else:
            err = _validate_playbook(content, scope)
            if err:
                return {"error": err}
        # 自定义技能上限（内置不计入）
        custom_count = db.query_one(
            "SELECT COUNT(*) AS n FROM user_skills WHERE owner_id = ? AND builtin = 0", (owner_id,)
        )
        if custom_count and custom_count["n"] >= settings.MAX_SKILLS_PER_USER:
            return {"error": f"自定义技能数量已达上限 {settings.MAX_SKILLS_PER_USER}"}
        # 添加预审：安全风险 / 本项目能力可行性；未通过则拒绝落库并留痕
        result = skill_review.review_skill(name, kind, scope, content)
        skill_review.log_review(owner_id, actor_name, "create", name, kind, scope, result, content)
        if result["verdict"] != "pass":
            return {"error": skill_review.reject_message(result), "review": result}
        now = _now()
        sid = str(uuid.uuid4())
        db.execute(
            "INSERT INTO user_skills (id, owner_id, name, description, kind, scope,"
            " content, enabled, builtin, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,0,?,?)",
            (sid, owner_id, name, description, kind, scope, content,
             1 if enabled else 0, now, now),
        )
        return {"skill": self.get_skill(owner_id, sid)}

    def update_skill(self, owner_id: str, skill_id: str, *, name: Optional[str] = None,
                     description: Optional[str] = None, content: Optional[str] = None,
                     enabled: Optional[bool] = None, actor_name: str = "") -> dict:
        """更新技能；内置技能仅允许切换 enabled；名称/内容变更时重新预审。"""
        existing = self.get_skill(owner_id, skill_id)
        if not existing:
            return {"error": "not_found"}
        now = _now()
        if existing["builtin"]:
            # 内置技能仅允许切换 enabled；显式传入名称/描述/内容一律明确拒绝，
            # 不再静默忽略，避免误导 API 调用方以为内容已修改。
            if enabled is None or name is not None or description is not None or content is not None:
                return {"error": "内置技能仅支持开启/关闭，不可编辑内容"}
            db.execute("UPDATE user_skills SET enabled = ?, updated_at = ? WHERE id = ?",
                       (1 if enabled else 0, now, skill_id))
            return {"skill": self.get_skill(owner_id, skill_id)}

        sets, params = [], []
        if name is not None:
            name = name.strip()
            if not name or len(name) > _MAX_NAME:
                return {"error": f"技能名称必填且不超过 {_MAX_NAME} 字"}
            if _SENSITIVE_RE.search(name):
                return {"error": "技能名称不得包含敏感信息"}
            sets.append("name = ?"); params.append(name)
        if description is not None:
            description = description.strip()
            if len(description) > _MAX_DESC:
                return {"error": f"技能描述不超过 {_MAX_DESC} 字"}
            sets.append("description = ?"); params.append(description)
        if content is not None:
            if _SENSITIVE_RE.search(content):
                return {"error": "技能内容不得包含密码/密钥/证件号等敏感信息"}
            if existing["kind"] == "prompt":
                if not content.strip() or len(content) > _MAX_PROMPT_CHARS:
                    return {"error": f"prompt 技能内容需在 1~{_MAX_PROMPT_CHARS} 字之间"}
            else:
                err = _validate_playbook(content, existing["scope"])
                if err:
                    return {"error": err}
            sets.append("content = ?"); params.append(content)
        if enabled is not None:
            sets.append("enabled = ?"); params.append(1 if enabled else 0)
        if not sets:
            return {"skill": existing}
        # 名称/内容变更时重新预审（未传字段沿用现有值），未通过则拒绝本次修改
        if name is not None or content is not None:
            result = skill_review.review_skill(
                name if name is not None else existing["name"],
                existing["kind"], existing["scope"],
                content if content is not None else existing["content"],
            )
            skill_review.log_review(owner_id, actor_name, "update",
                                    name if name is not None else existing["name"],
                                    existing["kind"], existing["scope"], result,
                                    content if content is not None else existing["content"])
            if result["verdict"] != "pass":
                return {"error": skill_review.reject_message(result), "review": result}
        sets.append("updated_at = ?"); params.append(now)
        params.append(skill_id)
        db.execute(f"UPDATE user_skills SET {', '.join(sets)} WHERE id = ?", tuple(params))
        return {"skill": self.get_skill(owner_id, skill_id)}

    def delete_skill(self, owner_id: str, skill_id: str) -> dict:
        existing = self.get_skill(owner_id, skill_id)
        if not existing:
            return {"error": "not_found"}
        if existing["builtin"]:
            return {"error": "内置技能不可删除，可选择关闭"}
        db.execute("DELETE FROM user_skills WHERE id = ? AND owner_id = ?", (skill_id, owner_id))
        return {"success": True}

    # ---- 消费侧 ----
    def active_prompt_skills(self, owner_id: str) -> List[dict]:
        """已启用的 chat 作用域 prompt 技能（用于对话系统提示注入）。"""
        if not owner_id:
            return []
        self.ensure_builtin(owner_id)
        rows = db.query(
            "SELECT name, content FROM user_skills"
            " WHERE owner_id = ? AND enabled = 1 AND scope = 'chat' AND kind = 'prompt'"
            " ORDER BY builtin DESC, updated_at DESC",
            (owner_id,),
        )
        return [{"name": r["name"], "content": r["content"]} for r in rows]

    def build_prompt_text(self, owner_id: str) -> str:
        """在字符预算内拼接已启用 prompt 技能，供 chat 注入。"""
        budget = int(getattr(settings, "SKILL_PROMPT_CHARS", 1500))
        lines, used = [], 0
        for s in self.active_prompt_skills(owner_id):
            line = f"- {s['name']}：{s['content']}".strip()
            if used + len(line) > budget:
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines)

    def url_strategy(self, owner_id: str, host: str) -> dict:
        """合并已启用 url_import playbook 技能中命中该 host 的策略。

        返回 {'mobile_ua': bool, 'prefer_render': bool, 'notes': [..]}。
        """
        strategy = {"mobile_ua": False, "prefer_render": False, "notes": []}
        if not owner_id or not host:
            return strategy
        self.ensure_builtin(owner_id)
        rows = db.query(
            "SELECT content FROM user_skills"
            " WHERE owner_id = ? AND enabled = 1 AND scope = 'url_import' AND kind = 'playbook'",
            (owner_id,),
        )
        host = host.lower()
        for r in rows:
            try:
                rules = json.loads(r["content"])
            except (ValueError, TypeError):
                continue
            if not isinstance(rules, list):
                continue
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                match = str(rule.get("match", "")).strip().lower()
                if not match or not (host == match or host.endswith("." + match) or match in host):
                    continue
                if rule.get("mobile_ua"):
                    strategy["mobile_ua"] = True
                if rule.get("prefer_render"):
                    strategy["prefer_render"] = True
                if rule.get("note"):
                    strategy["notes"].append(str(rule["note"]))
        return strategy

    def polish_policy(self, owner_id: str) -> dict:
        """合并已启用 polish playbook 技能，得到正文清洗策略。

        布尔开关按 OR 合并；extra_line_patterns 去重追加。未启用任何技能时返回全关。
        """
        policy = {k: False for k in _POLISH_BOOL_KEYS}
        policy["extra_line_patterns"] = []
        if not owner_id:
            return policy
        self.ensure_builtin(owner_id)
        rows = db.query(
            "SELECT content FROM user_skills"
            " WHERE owner_id = ? AND enabled = 1 AND scope = 'polish' AND kind = 'playbook'",
            (owner_id,),
        )
        for r in rows:
            try:
                item = json.loads(r["content"])
            except (ValueError, TypeError):
                continue
            if not isinstance(item, dict):
                continue
            for key in _POLISH_BOOL_KEYS:
                if item.get(key) is True:
                    policy[key] = True
            for p in item.get("extra_line_patterns", []) or []:
                if isinstance(p, str) and p.strip() and p not in policy["extra_line_patterns"]:
                    policy["extra_line_patterns"].append(p)
        return policy


skill_service = SkillService()
