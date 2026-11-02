"""
见字如面 - 模型网关「路由规则」+ 负载均衡 + 业务路由

路由规则 = 模型模式（支持 * 通配）→ 一组上游（有序 id 列表）+ 负载均衡策略 + 优先级 +
默认标记 + 授权用户。选路优先级：
  1. 显式 rule_id（用户下拉）——校验启用/模式匹配/用户授权；
  2. role 业务路由（gateway_role_routes.rule_id）；
  3. 模式匹配（精确 > 通配 fnmatch，各按 priority desc 取首条）；
  4. is_default 默认规则；
命中规则后取其 upstreams 列表解析为上游行，按 lb_strategy 排序（健康优先、熔断兜底），
返回 (rule, 有序上游行[含密钥])；代理按序尝试，失败自动切换下一个并更新上游健康度。

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
# 业务角色清单（供前端下拉与校验）
ROLES = (
    "chat_answer", "assistant", "intent_rewrite", "evidence_check",
    "wiki_compile", "embed", "rerank",
)


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


def _materialize(rule_row: dict) -> Tuple[dict, List[dict]]:
    """把规则的 upstreams id 列表解析为有序上游行（含密钥，供 proxy 出网）。"""
    up_ids = _parse_json(rule_row.get("upstreams"), [])
    rule = _row_to_dict(rule_row)
    if not up_ids:
        return rule, []
    rows = db.query("SELECT * FROM gateway_upstreams WHERE enabled=1")
    by_id = {r["id"]: r for r in rows}
    ups = [by_id[i] for i in up_ids if i in by_id]
    return rule, _order_by_strategy(ups, rule_row.get("lb_strategy"))


def select_upstreams(model: str, role: Optional[str] = None,
                     rule_id: Optional[str] = None,
                     user_id: Optional[str] = None) -> Tuple[Optional[dict], List[dict]]:
    """返回 (命中规则, 有序上游行[含密钥])；无命中返回 (None, [])。

    选路优先级：显式 rule_id > role 业务路由 > 模式匹配（精确 > 通配，各按 priority desc）
    > is_default 默认规则。命中后按 lb_strategy 排序上游，健康优先、熔断兜底。
    """
    rules = db.query("SELECT * FROM gateway_rules WHERE enabled=1")
    # 1. 显式规则优先
    if rule_id:
        row = next((r for r in rules if r["id"] == rule_id), None)
        if row and _matches(row.get("model_pattern"), model) and _user_allowed(row, user_id):
            return _materialize(row)
        logger.warning("显式规则不可用，回退常规选路: rule=%s model=%s user=%s",
                       rule_id, model, user_id)
    # 2. role 业务路由
    if role:
        route = get_role_route(role)
        rid = (route or {}).get("rule_id")
        if rid:
            row = next((r for r in rules if r["id"] == rid), None)
            if row and _matches(row.get("model_pattern"), model) and _user_allowed(row, user_id):
                return _materialize(row)
    allowed = [r for r in rules if _user_allowed(r, user_id)]
    # 3a. 精确模式匹配（pattern 字面等于请求模型名），按 priority desc 取首
    exact = [r for r in allowed if (r.get("model_pattern") or "").strip() == model]
    if exact:
        exact.sort(key=lambda r: -int(r.get("priority") or 0))
        return _materialize(exact[0])
    # 3b. 通配匹配（* 或 fnmatch），按 priority desc 取首
    wild = [r for r in allowed
            if (r.get("model_pattern") or "").strip() not in ("", model)
            and _matches(r.get("model_pattern"), model)]
    if wild:
        wild.sort(key=lambda r: -int(r.get("priority") or 0))
        return _materialize(wild[0])
    # 4. 默认规则
    defaults = [r for r in allowed if int(r.get("is_default") or 0) == 1]
    if defaults:
        defaults.sort(key=lambda r: -int(r.get("priority") or 0))
        return _materialize(defaults[0])
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
