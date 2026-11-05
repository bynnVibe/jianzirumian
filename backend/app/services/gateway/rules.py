"""
见字如面 - 模型网关「路由规则」+ 负载均衡 + 业务路由

路由规则 = 模型模式（支持 * 通配）→ 一组上游（有序 id 列表）+ 负载均衡策略 + 优先级 +
默认标记 + 授权用户。选路优先级：
  1. 显式 rule_id：
     - 带 role（业务路由注入）：严格绑定——只校验「规则上游是否支持该模型」，
       不以 model_pattern 作门槛（pattern 是规则名/模型模式，不是 role 绑定语义）；
       规则内无上游支持该模型时返回空候选（报 503），绝不跨规则静默回退；
     - 不带 role（聊天页下拉）：校验启用/模式匹配/用户授权，候选再按模型支持过滤；
  2. role 业务路由（gateway_role_routes.rule_id），同严格绑定语义；
  3. 模式匹配（精确 > 通配 fnmatch > 上游模型支持，各按 priority desc 取首条）；
  4. is_default 默认规则；
所有路径的候选上游均按「上游模型清单支持请求模型」过滤（清单为空视为不限制），
杜绝把模型发给不支持它的上游（400 Invalid model id）。
命中后按 lb_strategy 排序上游，健康优先、熔断兜底；代理按序尝试，失败自动切换下一个并更新上游健康度。

负载均衡：round_robin（计数取模）/ weighted（A-Res 加权随机）/ random（洗牌）/
priority（保持规则内上游列表顺序）；健康上游排在熔断上游之前，保留故障切换。
"""
import fnmatch
import json
import logging
import math
import random
import uuid
from datetime import datetime
from typing import List, Optional, Tuple

from app.core import db

logger = logging.getLogger("jianziruyang.gateway.rules")

LB_STRATEGIES = ("round_robin", "weighted", "random", "priority")
# 业务角色清单（供前端下拉与校验）：凡经 create_for_role 出网的 role 都应在此登记，
# 否则无法在「业务路由」中绑定规则/模型（如记忆抽取曾缺席导致只能走默认模型报 503）
ROLES = (
    "chat_answer", "assistant", "intent_rewrite", "evidence_check",
    "wiki_compile", "embed", "rerank", "ocr",
    "memory_extract", "session_summary", "knowledge_summarize", "text_polish",
)

# role → 上游分类映射：select_upstreams 按此过滤候选上游 category，防跨类误选
# （如 LLM 规则误选 embedding/rerank/ocr 上游，反之亦然）。未登记 role 归 llm。
ROLE_CATEGORY = {
    "chat_answer": "llm", "assistant": "llm", "intent_rewrite": "llm",
    "evidence_check": "llm", "wiki_compile": "llm", "memory_extract": "llm",
    "session_summary": "llm", "knowledge_summarize": "llm", "text_polish": "llm",
    "embed": "embedding", "rerank": "rerank", "ocr": "ocr",
}


def _now() -> str:
    return datetime.now().isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _parse_json(raw, fallback):
    try:
        val = json.loads(raw or "")
        return val if isinstance(val, (list, dict)) else fallback
    except (json.JSONDecodeError, TypeError):
        return fallback


def _normalize_lb_strategy(value) -> str:
    s = (value or "").strip().lower()
    return s if s in LB_STRATEGIES else "weighted"


def _normalize_users(value) -> List[str]:
    """授权用户 id 列表归一化（非列表视为空=全员）。"""
    if not isinstance(value, list):
        return []
    return [str(u).strip() for u in value if str(u).strip()]


def _normalize_upstreams(value) -> List[str]:
    """上游 id 有序列表归一化（去重去空，保序）。"""
    if isinstance(value, str):
        value = value.replace("，", ",").split(",")
    if not isinstance(value, list):
        return []
    seen: List[str] = []
    for u in value:
        s = str(u).strip()
        if s and s not in seen:
            seen.append(s)
    return seen


def _user_allowed(row: dict, user_id: Optional[str]) -> bool:
    """规则是否对该用户开放：allowed_users 为空视为全员；否则命中列表。"""
    allowed = _parse_json(row.get("allowed_users"), [])
    if not allowed:
        return True
    return bool(user_id) and str(user_id) in {str(u) for u in allowed}


def _matches(pattern: str, model: str) -> bool:
    """模型模式匹配：空/`*` 通配任何模型；否则精确或 fnmatch 通配。"""
    p = (pattern or "").strip()
    if not p or p == "*":
        return True
    if p == model:
        return True
    return fnmatch.fnmatch(model, p)


def _ensure_single_default(rule_id: str) -> None:
    """保证全局唯一默认规则：将其他规则的 is_default 清零。"""
    db.execute("UPDATE gateway_rules SET is_default=0 WHERE id<>? AND is_default=1", (rule_id,))


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def _resolve_upstream_names(up_ids: List[str]) -> List[dict]:
    """按上游 id 列表返回 {id, name, health} 明细（供前端规则行展示 chips）。"""
    if not up_ids:
        return []
    rows = db.query("SELECT id, name, health FROM gateway_upstreams")
    by_id = {r["id"]: r for r in rows}
    out = []
    for i in up_ids:
        r = by_id.get(i)
        out.append({"id": i, "name": r["name"] if r else i,
                    "health": (r.get("health") if r else "unknown") or "unknown"})
    return out


def _row_to_dict(row: dict) -> dict:
    up_ids = _parse_json(row.get("upstreams"), [])
    return {
        "id": row["id"],
        "model_pattern": row.get("model_pattern") or "",
        "upstreams": up_ids,
        "upstream_detail": _resolve_upstream_names(up_ids),
        "lb_strategy": row.get("lb_strategy") or "weighted",
        "priority": int(row.get("priority") or 0),
        "is_default": bool(row.get("is_default")),
        "allowed_users": _parse_json(row.get("allowed_users"), []),
        "enabled": bool(row.get("enabled")),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def list_rules() -> List[dict]:
    rows = db.query("SELECT * FROM gateway_rules ORDER BY is_default DESC, priority DESC,"
                    " created_at DESC")
    return [_row_to_dict(r) for r in rows]


def get_rule(rule_id: str) -> Optional[dict]:
    row = db.query_one("SELECT * FROM gateway_rules WHERE id=?", (rule_id,))
    return _row_to_dict(row) if row else None


def create_rule(payload: dict) -> dict:
    pattern = (payload.get("model_pattern") or "").strip()
    if not pattern:
        raise ValueError("模型模式不能为空（可用 * 匹配全部模型）")
    ups = _normalize_upstreams(payload.get("upstreams"))
    if not ups:
        raise ValueError("请至少选择一个上游")
    lb_strategy = _normalize_lb_strategy(payload.get("lb_strategy"))
    is_default = 1 if payload.get("is_default") else 0
    allowed_users = _normalize_users(payload.get("allowed_users"))
    rid = _new_id()
    db.execute(
        "INSERT INTO gateway_rules (id, model_pattern, upstreams, lb_strategy, priority,"
        " is_default, allowed_users, enabled, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (rid, pattern, json.dumps(ups, ensure_ascii=False), lb_strategy,
         int(payload.get("priority") or 0), is_default,
         json.dumps(allowed_users, ensure_ascii=False),
         1 if payload.get("enabled", True) else 0, _now(), _now()),
    )
    if is_default:
        _ensure_single_default(rid)
    return get_rule(rid)


def update_rule(rule_id: str, patch: dict) -> Optional[dict]:
    cur = db.query_one("SELECT * FROM gateway_rules WHERE id=?", (rule_id,))
    if not cur:
        return None
    pattern = (patch.get("model_pattern") or cur["model_pattern"]).strip()
    if not pattern:
        raise ValueError("模型模式不能为空")
    ups = _normalize_upstreams(patch["upstreams"]) if "upstreams" in patch \
        else _parse_json(cur["upstreams"], [])
    if not ups:
        raise ValueError("请至少选择一个上游")
    lb_strategy = _normalize_lb_strategy(
        patch.get("lb_strategy") if "lb_strategy" in patch else cur.get("lb_strategy"))
    priority = int(patch.get("priority", cur["priority"]) or 0)
    is_default = 1 if (patch.get("is_default") if "is_default" in patch
                       else bool(cur.get("is_default"))) else 0
    allowed_users = _normalize_users(
        patch.get("allowed_users") if "allowed_users" in patch
        else _parse_json(cur.get("allowed_users"), []))
    enabled = 1 if patch.get("enabled", bool(cur["enabled"])) else 0
    db.execute(
        "UPDATE gateway_rules SET model_pattern=?, upstreams=?, lb_strategy=?, priority=?,"
        " is_default=?, allowed_users=?, enabled=?, updated_at=? WHERE id=?",
        (pattern, json.dumps(ups, ensure_ascii=False), lb_strategy, priority, is_default,
         json.dumps(allowed_users, ensure_ascii=False), enabled, _now(), rule_id),
    )
    if is_default:
        _ensure_single_default(rule_id)
    return get_rule(rule_id)


def delete_rule(rule_id: str) -> bool:
    ok = db.execute("DELETE FROM gateway_rules WHERE id=?", (rule_id,)) > 0
    if ok:
        # 清除业务路由对该规则的绑定，避免悬空引用
        db.execute("UPDATE gateway_role_routes SET rule_id='', updated_at=? WHERE rule_id=?",
                   (_now(), rule_id))
    return ok


def list_rules_for_user(user_id: Optional[str], model: Optional[str] = None) -> List[dict]:
    """当前用户被授权的启用规则（供聊天页规则下拉）；可选按模型过滤。"""
    rows = db.query("SELECT * FROM gateway_rules WHERE enabled=1 ORDER BY is_default DESC,"
                    " priority DESC, created_at DESC")
    out = []
    for r in rows:
        if not _user_allowed(r, user_id):
            continue
        if model and not _matches(r.get("model_pattern"), model):
            continue
        out.append(_row_to_dict(r))
    return out


# ---------------------------------------------------------------------------
# 负载均衡（作用于规则内上游列表的 weight/health）
# ---------------------------------------------------------------------------

def _weighted_order(rows: List[dict]) -> List[dict]:
    """按权重加权随机排序（A-Res：key = -ln(rand)/weight，取升序）。"""
    keyed = []
    for r in rows:
        w = max(1, int(r.get("weight") or 1))
        rnd = random.random() or 1e-9
        keyed.append((-math.log(rnd) / w, r))
    keyed.sort(key=lambda x: x[0])
    return [r for _, r in keyed]


# round_robin 全局计数器（进程内递增，取模轮换；重启归零无副作用）
_RR_COUNTER = 0


def _round_robin_order(rows: List[dict]) -> List[dict]:
    """轮询：计数取模选出起点，其余按原序跟随（保留故障切换）。"""
    global _RR_COUNTER
    if not rows:
        return []
    _RR_COUNTER += 1
    idx = (_RR_COUNTER - 1) % len(rows)
    return rows[idx:] + rows[:idx]


def _order_by_strategy(rows: List[dict], strategy: Optional[str]) -> List[dict]:
    """按规则 lb_strategy 对一组上游排序；健康上游排在熔断上游之前。"""
    if not rows:
        return []
    s = _normalize_lb_strategy(strategy)
    healthy = [r for r in rows if (r.get("health") or "healthy") != "down"]
    down = [r for r in rows if (r.get("health") or "healthy") == "down"]

    def order(group: List[dict]) -> List[dict]:
        if s == "round_robin":
            return _round_robin_order(group)
        if s == "random":
            g = list(group)
            random.shuffle(g)
            return g
        if s == "priority":
            return list(group)  # 保持规则内上游列表顺序
        return _weighted_order(group)  # weighted（A-Res）默认

    return order(healthy) + order(down)  # 熔断上游兜底


def resolve_rule_models(rule_id: str) -> Tuple[bool, List[str]]:
    """返回 (模式是否通配, 规则启用上游的模型并集[按上游顺序去重])。

    供业务路由在未显式指定模型时做「规则感知」解析：候选模型必须命中规则
    model_pattern 且在规则上游模型清单内，否则代理出网必然 404。
    """
    row = db.query_one("SELECT * FROM gateway_rules WHERE id=? AND enabled=1", (rule_id,))
    if not row:
        return False, []
    pattern = (row.get("model_pattern") or "").strip()
    wildcard = (not pattern) or pattern == "*"
    up_ids = _parse_json(row.get("upstreams"), [])
    if not up_ids:
        return wildcard, []
    rows = db.query("SELECT * FROM gateway_upstreams WHERE enabled=1")
    by_id = {r["id"]: r for r in rows}
    models: List[str] = []
    for i in up_ids:
        up = by_id.get(i)
        if not up:
            continue
        for m in _parse_json(up.get("models"), []):
            s = str(m).strip()
            if s and s not in models:
                models.append(s)
    return wildcard, models


def resolve_chat_rule(user_id: Optional[str] = None) -> dict:
    """解析聊天回答（chat_answer）实际匹配的路由规则与可选模型（供聊天页模型下拉）。

    匹配顺序：chat_answer 业务路由绑定规则 → 全局默认规则 → 首个该用户可用的启用规则。
    返回 {rule_id, rule_name, models, pinned_model}；models 为规则上游模型并集，
    聊天页下拉只暴露这些模型（选不了其他规则的模型），pinned_model 为业务路由
    固化的模型名（为空表示不选模型时在规则内均衡选择）。
    """
    route = get_role_route("chat_answer") or {}
    rule_id = (route.get("rule_id") or "").strip()
    row = None
    if rule_id:
        row = db.query_one("SELECT * FROM gateway_rules WHERE id=? AND enabled=1", (rule_id,))
    if not row:
        row = db.query_one(
            "SELECT * FROM gateway_rules WHERE enabled=1 AND is_default=1"
            " ORDER BY priority DESC LIMIT 1")
    if not row:
        for r in db.query("SELECT * FROM gateway_rules WHERE enabled=1"
                          " ORDER BY priority DESC, created_at DESC"):
            if _user_allowed(r, user_id):
                row = r
                break
    if not row:
        return {"rule_id": "", "rule_name": "", "models": [],
                "pinned_model": (route.get("model_name") or "").strip()}
    _, models = resolve_rule_models(row["id"])
    return {
        "rule_id": row["id"],
        "rule_name": row.get("model_pattern") or "",
        "models": models,
        "pinned_model": (route.get("model_name") or "").strip(),
    }


def _upstream_supports(up: dict, model: str) -> bool:
    """上游是否支持该模型：模型清单为空视为不限制；否则需精确命中或 `*`。"""
    models = _parse_json(up.get("models"), [])
    if not models:
        return True
    return any(m == model or m == "*" for m in (str(x).strip() for x in models))


def _materialize(rule_row: dict, model: str = "",
                 category: Optional[str] = None) -> Tuple[dict, List[dict]]:
    """把规则的 upstreams id 列表解析为有序上游行（含密钥，供 proxy 出网）。

    传入 model 时先按「上游模型清单支持」过滤，杜绝模型发往不支持它的上游；
    传入 category 时按上游分类过滤（role→ROLE_CATEGORY），杜绝跨类误选。
    """
    up_ids = _parse_json(rule_row.get("upstreams"), [])
    rule = _row_to_dict(rule_row)
    if not up_ids:
        return rule, []
    rows = db.query("SELECT * FROM gateway_upstreams WHERE enabled=1")
    by_id = {r["id"]: r for r in rows}
    ups = [by_id[i] for i in up_ids if i in by_id]
    if category:
        ups = [u for u in ups if (u.get("category") or "llm") == category]
    if model:
        ups = [u for u in ups if _upstream_supports(u, model)]
    return rule, _order_by_strategy(ups, rule_row.get("lb_strategy"))


def select_upstreams(model: str, role: Optional[str] = None,
                     rule_id: Optional[str] = None,
                     user_id: Optional[str] = None) -> Tuple[Optional[dict], List[dict]]:
    """返回 (命中规则, 有序上游行[含密钥])；无命中返回 (None, [])。

    选路优先级：显式 rule_id > role 业务路由 > 模式匹配（精确 > 通配，各按 priority desc）
    > is_default 默认规则。命中后按 lb_strategy 排序上游，健康优先、熔断兜底。
    """
    rules = db.query("SELECT * FROM gateway_rules WHERE enabled=1")
    # role → 上游分类：按 ROLE_CATEGORY 过滤候选上游（无 role/未知 role 视为通用 chat=llm），
    # 防止把 embedding/rerank/ocr 请求发往 LLM 上游，或反之
    category = ROLE_CATEGORY.get(role, "llm")
    # 1. 显式规则优先
    if rule_id:
        row = next((r for r in rules if r["id"] == rule_id), None)
        if row and _user_allowed(row, user_id):
            if role:
                # 业务路由注入的 rule_id：严格绑定，只认「规则上游支持该模型」，
                # 不支持时返回空候选由代理报 503，绝不跨规则静默回退混淆路由
                rule, ups = _materialize(row, model, category)
                if ups:
                    return rule, ups
                logger.warning("业务路由严格绑定：规则「%s」内无上游支持模型「%s」，"
                               "拒绝跨规则回退: role=%s rule=%s",
                               row.get("model_pattern"), model, role, rule_id)
                return rule, []
            if _matches(row.get("model_pattern"), model):
                rule, ups = _materialize(row, model, category)
                if ups:
                    return rule, ups
                logger.warning("显式规则上游均不支持模型，回退常规选路: rule=%s model=%s user=%s",
                               rule_id, model, user_id)
            else:
                logger.warning("显式规则模式不匹配，回退常规选路: rule=%s model=%s user=%s",
                               rule_id, model, user_id)
        else:
            logger.warning("显式规则不可用（禁用/删除/未授权），回退常规选路: rule=%s model=%s user=%s",
                           rule_id, model, user_id)
    # 2. role 业务路由（rule_id 未注入时）
    if role:
        route = get_role_route(role)
        rid = (route or {}).get("rule_id")
        if rid:
            row = next((r for r in rules if r["id"] == rid), None)
            if row and _user_allowed(row, user_id):
                rule, ups = _materialize(row, model, category)
                if ups:
                    return rule, ups
                logger.warning("业务路由严格绑定：规则「%s」内无上游支持模型「%s」，"
                               "拒绝跨规则回退: role=%s rule=%s",
                               row.get("model_pattern"), model, role, rid)
                return rule, []
    allowed = [r for r in rules if _user_allowed(r, user_id)]
    # 3a. 精确模式匹配（pattern 字面等于请求模型名），按 priority desc 取首（需有上游支持）
    exact = [r for r in allowed if (r.get("model_pattern") or "").strip() == model]
    exact.sort(key=lambda r: -int(r.get("priority") or 0))
    for r in exact:
        rule, ups = _materialize(r, model, category)
        if ups:
            return rule, ups
    # 3b. 通配匹配（* 或 fnmatch），按 priority desc 取首（需有上游支持）
    wild = [r for r in allowed
            if (r.get("model_pattern") or "").strip() not in ("", model)
            and _matches(r.get("model_pattern"), model)]
    wild.sort(key=lambda r: -int(r.get("priority") or 0))
    for r in wild:
        rule, ups = _materialize(r, model, category)
        if ups:
            return rule, ups
    # 3c. 上游模型支持匹配：规则内任一上游支持该模型（按 priority desc），
    #     避免未绑定角色被默认规则接走后发往不支持的上游
    supp = sorted(allowed, key=lambda r: -int(r.get("priority") or 0))
    for r in supp:
        rule, ups = _materialize(r, model, category)
        if ups:
            return rule, ups
    # 4. 默认规则（同样需有上游支持该模型）
    defaults = [r for r in allowed if int(r.get("is_default") or 0) == 1]
    defaults.sort(key=lambda r: -int(r.get("priority") or 0))
    for r in defaults:
        rule, ups = _materialize(r, model, category)
        if ups:
            return rule, ups
    return None, []


# ---------------------------------------------------------------------------
# 业务路由 CRUD（role → rule + model_name）
# ---------------------------------------------------------------------------

def _route_to_dict(row: dict) -> dict:
    return {
        "id": row["id"],
        "role": row["role"],
        "rule_id": row.get("rule_id") or "",
        "model_name": row.get("model_name") or "",
        "updated_at": row.get("updated_at"),
    }


def list_role_routes() -> List[dict]:
    rows = db.query("SELECT * FROM gateway_role_routes ORDER BY role ASC")
    return [_route_to_dict(r) for r in rows]


def get_role_route(role: str) -> Optional[dict]:
    row = db.query_one("SELECT * FROM gateway_role_routes WHERE role=?", (role,))
    return _route_to_dict(row) if row else None


def upsert_role_route(role: str, rule_id: str = "", model_name: str = "") -> dict:
    """按 role upsert 路由（role 唯一）。rule_id/model_name 为空表示清除该 role 绑定。"""
    role = (role or "").strip()
    if role not in ROLES:
        raise ValueError(f"不支持的业务角色：{role}（可选 {'/'.join(ROLES)}）")
    rule_id = (rule_id or "").strip()
    model_name = (model_name or "").strip()
    cur = db.query_one("SELECT id FROM gateway_role_routes WHERE role=?", (role,))
    if cur:
        db.execute("UPDATE gateway_role_routes SET rule_id=?, model_name=?, updated_at=?"
                   " WHERE role=?", (rule_id, model_name, _now(), role))
    else:
        db.execute("INSERT INTO gateway_role_routes (id, role, rule_id, model_name, updated_at)"
                   " VALUES (?,?,?,?,?)",
                   (_new_id(), role, rule_id, model_name, _now()))
    return get_role_route(role)


def delete_role_route(role: str) -> bool:
    return db.execute("DELETE FROM gateway_role_routes WHERE role=?", (role,)) > 0
