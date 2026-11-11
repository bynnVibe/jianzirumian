"""
见字如面 - 模型网关本地密钥与配额管理

密钥分三类（key_type）：
- internal：系统内部调用密钥（无限配额，后台模块出网兜底）；
- public  ：公共密钥——未分配用户密钥的用户默认共用；生成时可按「每用户」设置
            每日 Token/请求限额（daily_token_limit/daily_request_limit）与每分钟流控
            （rate_per_minute）；每用户额度用尽 → 拒绝并提示「联系管理员」；
- user    ：用户密钥——生成时强制绑定 1..n 个用户（bound_users），沿用 Token/请求
            总配额并可设每分钟流控；配额用尽时系统自动降级切换公共密钥，
            不阻碍正常使用（界面友好提示见 /api/gateway/my-quota）。

真实上游 Key 保存在渠道里，不分发给调用方。密钥明文只在创建时返回一次，
数据库仅存 sha256(key) 与前缀（便于识别）；公共/用户密钥的明文另存于
gateway_settings.outbound_keys（本地信任边界），供系统按用户解析后自动携带出网。
"""
import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from app.core import db

logger = logging.getLogger("jianziruyang.gateway.keys")

_KEY_PREFIX = "sk-jzrm-"

# 密钥类型：internal(系统内部) | public(公共) | user(用户)
KEY_TYPES = ("internal", "public", "user")
KEY_TYPE_LABELS = {"internal": "系统内部调用", "public": "公共密钥", "user": "用户密钥"}


def _now() -> str:
    return datetime.now().isoformat()


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _new_id() -> str:
    import uuid
    return str(uuid.uuid4())


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _generate_key() -> str:
    return _KEY_PREFIX + secrets.token_urlsafe(32)


def _parse_list(raw) -> List[str]:
    try:
        val = json.loads(raw or "[]")
        return [str(x).strip() for x in val if str(x).strip()] if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _row_to_dict(row: dict) -> dict:
    allowed = _parse_list(row.get("allowed_models"))
    bound_users = _parse_list(row.get("bound_users"))
    key_type = (row.get("key_type") or "user").strip()
    if key_type not in KEY_TYPES:
        key_type = "user"
    token_quota = int(row.get("token_quota") or 0)
    token_used = int(row.get("token_used") or 0)
    request_quota = int(row.get("request_quota") or 0)
    request_count = int(row.get("request_count") or 0)
    return {
        "id": row["id"],
        "name": row["name"],
        "key_prefix": row.get("key_prefix") or "",
        "key_type": key_type,
        "key_type_label": KEY_TYPE_LABELS.get(key_type, key_type),
        "bound_users": bound_users,
        "token_quota": token_quota,
        "token_used": token_used,
        "token_remaining": (token_quota - token_used) if token_quota else None,
        "request_quota": request_quota,
        "request_count": request_count,
        "request_remaining": (request_quota - request_count) if request_quota else None,
        "daily_token_limit": int(row.get("daily_token_limit") or 0),
        "daily_request_limit": int(row.get("daily_request_limit") or 0),
        "rate_per_minute": int(row.get("rate_per_minute") or 0),
        "allowed_models": allowed,
        "enabled": bool(row.get("enabled")),
        "expires_at": row.get("expires_at"),
        "created_by": row.get("created_by") or "",
        "created_at": row.get("created_at"),
        "last_used_at": row.get("last_used_at"),
        "expired": bool(row.get("expires_at")) and row["expires_at"] < _now(),
    }


class KeyRejected(Exception):
    """密钥校验失败（不存在/禁用/过期/超配额/模型不允许）。"""

    def __init__(self, message: str, code: str = "invalid_key"):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = 429 if code in ("quota_exceeded", "daily_limit_exceeded",
                                           "rate_limited") else (
            403 if code == "model_not_allowed" else 401)


class QuotaRejected(Exception):
    """按用户维度的配额/流控拒绝（公共密钥每日限额、每分钟流控）。"""

    def __init__(self, message: str, code: str = "daily_limit_exceeded", status_code: int = 429):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def list_keys() -> List[dict]:
    rows = db.query("SELECT * FROM gateway_keys ORDER BY created_at DESC")
    return [_row_to_dict(r) for r in rows]


def get_key(key_id: str) -> Optional[dict]:
    row = db.query_one("SELECT * FROM gateway_keys WHERE id=?", (key_id,))
    return _row_to_dict(row) if row else None


def create_key(name: str, created_by: str = "", token_quota: int = 0,
               request_quota: int = 0, allowed_models: Optional[List[str]] = None,
               expires_at: Optional[str] = None, key_type: str = "public",
               bound_users: Optional[List[str]] = None,
               daily_token_limit: int = 0, daily_request_limit: int = 0,
               rate_per_minute: int = 0) -> dict:
    """创建密钥，返回 dict（含明文 key，仅此一次）。

    - key_type=user：bound_users 必填（强制绑定 1..n 个用户）；
    - key_type=public（默认）：可设每用户每日限额与流控（生成时设置）；
    - 公共/用户密钥的明文登记到 outbound_keys 映射，供系统按用户解析自动携带。
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("密钥名称不能为空")
    key_type = (key_type or "public").strip().lower()
    if key_type not in KEY_TYPES:
        raise ValueError(f"不支持的密钥类型：{key_type}（可选 {'/'.join(KEY_TYPES)}）")
    users = [str(u).strip() for u in (bound_users or []) if str(u).strip()]
    if key_type == "user" and not users:
        raise ValueError("用户密钥必须绑定至少 1 个用户（bound_users）")
    if key_type == "internal":
        users = []
    plain = _generate_key()
    key_id = _new_id()
    models = [str(m).strip() for m in (allowed_models or []) if str(m).strip()]
    db.execute(
        "INSERT INTO gateway_keys (id, name, key_prefix, key_hash, token_quota, token_used,"
        " request_quota, request_count, allowed_models, enabled, expires_at, created_by,"
        " created_at, last_used_at, key_type, bound_users, daily_token_limit,"
        " daily_request_limit, rate_per_minute)"
        " VALUES (?,?,?,?,?,0,?,0,?,1,?,?,?,NULL,?,?,?,?,?)",
        (key_id, name, plain[:len(_KEY_PREFIX) + 6], _hash(plain),
         max(0, int(token_quota or 0)), max(0, int(request_quota or 0)),
         json.dumps(models, ensure_ascii=False), expires_at or None,
         created_by or "", _now(), key_type, json.dumps(users, ensure_ascii=False),
         max(0, int(daily_token_limit or 0)), max(0, int(daily_request_limit or 0)),
         max(0, int(rate_per_minute or 0))),
    )
    if key_type in ("public", "user"):
        from app.services.gateway import settings as settings_svc
        settings_svc.set_outbound_key(key_id, plain)
    info = get_key(key_id)
    info["key"] = plain  # 明文仅创建时返回
    return info


def update_key(key_id: str, patch: dict) -> Optional[dict]:
    cur = get_key(key_id)
    if not cur:
        return None
    name = patch.get("name", cur["name"])
    token_quota = int(patch.get("token_quota", cur["token_quota"]) or 0)
    request_quota = int(patch.get("request_quota", cur["request_quota"]) or 0)
    allowed = patch.get("allowed_models", cur["allowed_models"])
    if not isinstance(allowed, list):
        allowed = cur["allowed_models"]
    allowed = [str(m).strip() for m in allowed if str(m).strip()]
    bound = patch.get("bound_users", cur["bound_users"])
    if not isinstance(bound, list):
        bound = cur["bound_users"]
    bound = [str(u).strip() for u in bound if str(u).strip()]
    if cur["key_type"] == "user" and not bound:
        raise ValueError("用户密钥必须绑定至少 1 个用户（bound_users）")
    enabled = 1 if patch.get("enabled", cur["enabled"]) else 0
    expires_at = patch.get("expires_at", cur["expires_at"]) or None
    db.execute(
        "UPDATE gateway_keys SET name=?, token_quota=?, request_quota=?, allowed_models=?,"
        " enabled=?, expires_at=?, bound_users=?, daily_token_limit=?, daily_request_limit=?,"
        " rate_per_minute=? WHERE id=?",
        (name, max(0, token_quota), max(0, request_quota),
         json.dumps(allowed, ensure_ascii=False), enabled, expires_at,
         json.dumps(bound, ensure_ascii=False),
         max(0, int(patch.get("daily_token_limit", cur["daily_token_limit"]) or 0)),
         max(0, int(patch.get("daily_request_limit", cur["daily_request_limit"]) or 0)),
         max(0, int(patch.get("rate_per_minute", cur["rate_per_minute"]) or 0)),
         key_id),
    )
    return get_key(key_id)


def delete_key(key_id: str) -> bool:
    ok = db.execute("DELETE FROM gateway_keys WHERE id=?", (key_id,)) > 0
    if ok:
        from app.services.gateway import settings as settings_svc
        settings_svc.remove_outbound_keys([key_id])
        db.execute("DELETE FROM gateway_key_usage WHERE key_id=?", (key_id,))
    return ok


def reset_usage(key_id: str) -> Optional[dict]:
    db.execute("UPDATE gateway_keys SET token_used=0, request_count=0 WHERE id=?", (key_id,))
    return get_key(key_id)


# ---------------------------------------------------------------------------
# 系统内部出网密钥（各业务模块经网关 /v1 出网自动携带）
# ---------------------------------------------------------------------------

_INTERNAL_KEY_NAME = "系统内部调用"


def ensure_internal_key() -> str:
    """返回系统内部出网密钥明文（无限配额、不限模型）。

    明文持久化在 gateway_settings.internal_key（本地信任边界，与上游 api_key 同级）；
    对应密钥行被删除时自动重建，保证系统模块始终可经网关出网。
    """
    from app.services.gateway import settings as settings_svc
    cur = (settings_svc.get_settings().get("internal_key") or "").strip()
    if cur:
        row = db.query_one("SELECT id, enabled FROM gateway_keys WHERE key_hash=?", (_hash(cur),))
        if row and int(row["enabled"] or 0) == 1:
            return cur
    info = create_key(_INTERNAL_KEY_NAME, created_by="system",
                      token_quota=0, request_quota=0, allowed_models=[],
                      key_type="internal")
    plain = info["key"]
    settings_svc.save_settings({"internal_key": plain})
    logger.info("已创建系统内部出网密钥（%s…）", plain[:len(_KEY_PREFIX) + 6])
    return plain


def get_internal_key_meta() -> dict:
    """系统内部出网密钥的配置现状（供管理页显性化展示，不含明文）。"""
    from app.services.gateway import settings as settings_svc
    plain = (settings_svc.get_settings().get("internal_key") or "").strip()
    meta = {"configured": bool(plain), "prefix": plain[:len(_KEY_PREFIX) + 6] if plain else "",
            "name": _INTERNAL_KEY_NAME, "key_id": "", "enabled": False,
            "created_at": "", "request_count": 0, "token_used": 0}
    if plain:
        row = db.query_one("SELECT * FROM gateway_keys WHERE key_hash=?", (_hash(plain),))
        if row:
            info = _row_to_dict(row)
            meta.update({"key_id": info["id"], "enabled": info["enabled"],
                         "created_at": info["created_at"] or "",
                         "request_count": info["request_count"],
                         "token_used": info["token_used"]})
    return meta


def rotate_internal_key(created_by: str = "") -> dict:
    """轮换系统内部出网密钥：新建密钥行 → 更新 settings.internal_key → 删除旧行。

    返回含明文的创建结果（仅此一次展示）；轮换后旧密钥立即失效，
    业务模块下次出网自动携带新密钥（create_for_role 每次现取）。
    """
    from app.services.gateway import settings as settings_svc
    old = (settings_svc.get_settings().get("internal_key") or "").strip()
    info = create_key(_INTERNAL_KEY_NAME, created_by=created_by or "system",
                      token_quota=0, request_quota=0, allowed_models=[],
                      key_type="internal")
    settings_svc.save_settings({"internal_key": info["key"]})
    if old:
        db.execute("DELETE FROM gateway_keys WHERE key_hash=?", (_hash(old),))
    logger.info("已轮换系统内部出网密钥（新 %s…）", info["key"][:len(_KEY_PREFIX) + 6])
    return info


# ---------------------------------------------------------------------------
# 运行时校验与配额消费（代理链路调用）
# ---------------------------------------------------------------------------

def authenticate(raw_key: str) -> dict:
    """校验下游请求携带的本地密钥，返回内部行 dict（含 id/name/quota）。

    校验失败抛 KeyRejected（区分 invalid/disabled/expired/quota）。
    """
    raw_key = (raw_key or "").strip()
    if not raw_key:
        raise KeyRejected("缺少访问密钥", "missing_key")
    row = db.query_one("SELECT * FROM gateway_keys WHERE key_hash=?", (_hash(raw_key),))
    return _validate(row)


def _validate(row):
    if not row:
        raise KeyRejected("无效的访问密钥", "invalid_key")
    if not row.get("enabled"):
        raise KeyRejected("密钥已被禁用", "disabled")
    if row.get("expires_at") and row["expires_at"] < _now():
        raise KeyRejected("密钥已过期", "expired")
    # 请求次数配额
    req_quota = int(row.get("request_quota") or 0)
    if req_quota and int(row.get("request_count") or 0) >= req_quota:
        raise KeyRejected("请求次数配额已用尽", "quota_exceeded")
    # Token 配额
    tok_quota = int(row.get("token_quota") or 0)
    if tok_quota and int(row.get("token_used") or 0) >= tok_quota:
        raise KeyRejected("Token 配额已用尽", "quota_exceeded")
    return row


def check_model_allowed(row: dict, model: str) -> bool:
    """密钥是否允许访问该模型（allowed_models 为空表示不限制）。"""
    try:
        allowed = json.loads(row.get("allowed_models") or "[]")
    except (json.JSONDecodeError, TypeError):
        allowed = []
    if not allowed:
        return True
    return model in allowed


def consume_request(key_id: str, model: Optional[str] = None) -> dict:
    """跨进程原子认领；重读权限，进入上游阶段只调用一次，不退款。"""
    with db.transaction() as conn:
        found = conn.execute("SELECT * FROM gateway_keys WHERE id=?", (key_id,)).fetchone()
        row = _validate(dict(found) if found else None)
        if model is not None and not check_model_allowed(row, model):
            raise KeyRejected("该密钥无权访问请求模型", "model_not_allowed")
        result = conn.execute(
            "UPDATE gateway_keys SET request_count=request_count+1,last_used_at=? "
            "WHERE id=? AND enabled=1 AND (request_quota=0 OR request_count<request_quota)",
            (_now(), key_id),
        )
        if result.rowcount != 1:
            raise KeyRejected("请求次数配额已用尽", "quota_exceeded")
        row["request_count"] += 1
        return row


def consume_tokens(key_id: str, total_tokens: int) -> None:
    """按实际 Token 消耗累加（响应结束后调用）。"""
    if total_tokens and total_tokens > 0:
        db.execute(
            "UPDATE gateway_keys SET token_used = token_used + ? WHERE id=?",
            (int(total_tokens), key_id),
        )


# ---------------------------------------------------------------------------
# 出网密钥解析（系统按用户自动携带：用户密钥 > 公共密钥 > 内部密钥）
# ---------------------------------------------------------------------------

def _plain_of(key_id: str) -> str:
    from app.services.gateway import settings as settings_svc
    return str((settings_svc.get_outbound_keys() or {}).get(key_id) or "")


def _key_available(info: dict) -> bool:
    """启用且未过期，且（如设了总配额）未用尽。"""
    if not info or not info["enabled"] or info["expired"]:
        return False
    if info["token_quota"] and info["token_used"] >= info["token_quota"]:
        return False
    if info["request_quota"] and info["request_count"] >= info["request_quota"]:
        return False
    return True


def find_user_key(user_id: str) -> Optional[dict]:
    """该用户绑定的首个可用用户密钥（key_type=user 且 bound_users 命中）。"""
    if not user_id:
        return None
    rows = db.query("SELECT * FROM gateway_keys WHERE key_type='user' AND enabled=1"
                    " ORDER BY created_at ASC")
    for r in rows:
        info = _row_to_dict(r)
        if str(user_id) in set(info["bound_users"]) and _key_available(info):
            return info
    return None


def find_public_key() -> Optional[dict]:
    """首个可用的公共密钥（key_type=public，按创建先后取一）。"""
    rows = db.query("SELECT * FROM gateway_keys WHERE key_type='public' AND enabled=1"
                    " ORDER BY created_at ASC")
    for r in rows:
        info = _row_to_dict(r)
        if _key_available(info):
            return info
    return None


def resolve_outbound_key(user_id: str = "") -> Tuple[str, str]:
    """系统出网时按用户解析应携带的密钥：返回 (明文, key_type)。

    优先级：该用户绑定的用户密钥 → 公共密钥 → 系统内部密钥（兜底，永不为空）。
    用户密钥总配额用尽时此处即降级公共密钥（代理侧另有每用户日限额兜底）。
    """
    if user_id:
        info = find_user_key(str(user_id))
        if info:
            plain = _plain_of(info["id"])
            if plain:
                return plain, "user"
    info = find_public_key()
    if info:
        plain = _plain_of(info["id"])
        if plain:
            return plain, "public"
    return ensure_internal_key(), "internal"


# ---------------------------------------------------------------------------
# 每用户配额与流控（公共密钥每日限额 / 用户密钥流控）
# ---------------------------------------------------------------------------

def get_day_usage(key_id: str, user_id: str, day: str = "") -> dict:
    """某密钥某用户当日用量（tokens/requests）。"""
    row = db.query_one(
        "SELECT tokens, requests FROM gateway_key_usage WHERE key_id=? AND user_id=? AND day=?",
        (key_id, user_id or "", day or _today()))
    return {"tokens": int((row or {}).get("tokens") or 0),
            "requests": int((row or {}).get("requests") or 0)}


def _requests_last_minute(key_id: str) -> int:
    """近 60 秒该密钥的请求数（流控用；以网关日志为准，跨进程一致）。"""
    since = (datetime.now() - timedelta(seconds=60)).isoformat()
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM gateway_logs WHERE key_id=? AND created_at>=?",
        (key_id, since))
    return int((row or {}).get("n") or 0)


def enforce_user_quota(row: dict, user_id: str = "") -> None:
    """按密钥类型执行「每用户」配额与流控；超限抛 QuotaRejected。

    - internal：不限制（系统兜底通道）；
    - public  ：每用户每日 Token/请求限额 + 每分钟流控；额度用尽提示联系管理员；
    - user    ：每分钟流控（总配额由 authenticate/consume_request 认领执行）。
    """
    key_type = (row.get("key_type") or "user").strip()
    if key_type == "internal":
        return
    rate = int(row.get("rate_per_minute") or 0)
    if rate and _requests_last_minute(row["id"]) >= rate:
        raise QuotaRejected(f"请求过于频繁（限每分钟 {rate} 次），请稍后再试",
                            "rate_limited", 429)
    if key_type != "public":
        return
    d_tok = int(row.get("daily_token_limit") or 0)
    d_req = int(row.get("daily_request_limit") or 0)
    if not (d_tok or d_req):
        return
    usage = get_day_usage(row["id"], user_id)
    if d_req and usage["requests"] >= d_req:
        raise QuotaRejected(
            f"您今日的公共密钥请求额度已用完（{usage['requests']}/{d_req} 次），请联系管理员",
            "daily_limit_exceeded", 429)
    if d_tok and usage["tokens"] >= d_tok:
        raise QuotaRejected(
            f"您今日的公共密钥 Token 额度已用完（{usage['tokens']}/{d_tok}），请联系管理员",
            "daily_limit_exceeded", 429)


def record_usage(key_id: str, user_id: str = "", tokens: int = 0,
                 requests: int = 0) -> None:
    """按 (key_id, user_id, day) 累计用量（请求认领 +1，响应结束后累加 Token）。"""
    if not key_id:
        return
    day = _today()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO gateway_key_usage (key_id, user_id, day, tokens, requests, updated_at)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(key_id, user_id, day) DO UPDATE SET"
            " tokens=tokens+excluded.tokens, requests=requests+excluded.requests,"
            " updated_at=excluded.updated_at",
            (key_id, user_id or "", day, max(0, int(tokens or 0)),
             max(0, int(requests or 0)), _now()),
        )


def user_quota_status(user_id: str) -> dict:
    """「我的配额」状态（供聊天界面友好提示，非阻断性）。

    返回当前用户出网将使用的密钥类型、当日用量/限额、是否用尽与提示文案。
    """
    user_id = str(user_id or "")
    out = {"user_id": user_id, "key_type": "internal", "key_name": "",
           "daily_tokens": 0, "daily_requests": 0,
           "daily_token_limit": 0, "daily_request_limit": 0,
           "token_quota": 0, "token_used": 0,
           "request_quota": 0, "request_count": 0,
           "exhausted": False, "degraded": False, "message": ""}
    info = find_user_key(user_id)
    if info:
        usage = get_day_usage(info["id"], user_id)
        out.update({"key_type": "user", "key_name": info["name"],
                    "daily_tokens": usage["tokens"], "daily_requests": usage["requests"],
                    "token_quota": info["token_quota"], "token_used": info["token_used"],
                    "request_quota": info["request_quota"],
                    "request_count": info["request_count"]})
        return out
    # 无可用用户密钥：看是否有绑定的密钥但已用尽（降级提示）
    bound = None
    rows = db.query("SELECT * FROM gateway_keys WHERE key_type='user'")
    for r in rows:
        cand = _row_to_dict(r)
        if user_id and user_id in set(cand["bound_users"]):
            bound = cand
            break
    pub = find_public_key()
    if pub:
        usage = get_day_usage(pub["id"], user_id)
        exhausted = bool(
            (pub["daily_request_limit"] and usage["requests"] >= pub["daily_request_limit"])
            or (pub["daily_token_limit"] and usage["tokens"] >= pub["daily_token_limit"]))
        out.update({"key_type": "public", "key_name": pub["name"],
                    "daily_tokens": usage["tokens"], "daily_requests": usage["requests"],
                    "daily_token_limit": pub["daily_token_limit"],
                    "daily_request_limit": pub["daily_request_limit"],
                    "exhausted": exhausted,
                    "degraded": bool(bound)})
        if exhausted:
            out["message"] = "今日公共密钥额度已用完，请联系管理员"
        elif bound:
            out["message"] = (f"您的专属密钥「{bound['name']}」配额已用尽，"
                              "已自动降级为公共密钥继续提供服务")
        return out
    out["exhausted"] = True
    out["message"] = "今日公共密钥额度已用完，请联系管理员"
    return out
