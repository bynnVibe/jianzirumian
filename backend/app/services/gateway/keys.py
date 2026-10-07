"""
见字如面 - 模型网关本地密钥与配额管理

为每个下游用户/应用生成独立的本地访问密钥（sk-...），可设置 Token / 请求次数配额与
允许访问的模型；真实上游 Key 保存在渠道里，不分发给调用方。

密钥明文只在创建时返回一次，数据库仅存 sha256(key) 与前缀（便于识别）。
"""
import hashlib
import json
import logging
import secrets
from datetime import datetime
from typing import List, Optional

from app.core import db

logger = logging.getLogger("jianziruyang.gateway.keys")

_KEY_PREFIX = "sk-jzrm-"


def _now() -> str:
    return datetime.now().isoformat()


def _new_id() -> str:
    import uuid
    return str(uuid.uuid4())


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _generate_key() -> str:
    return _KEY_PREFIX + secrets.token_urlsafe(32)


def _row_to_dict(row: dict) -> dict:
    try:
        allowed = json.loads(row.get("allowed_models") or "[]")
    except (json.JSONDecodeError, TypeError):
        allowed = []
    token_quota = int(row.get("token_quota") or 0)
    token_used = int(row.get("token_used") or 0)
    request_quota = int(row.get("request_quota") or 0)
    request_count = int(row.get("request_count") or 0)
    return {
        "id": row["id"],
        "name": row["name"],
        "key_prefix": row.get("key_prefix") or "",
        "token_quota": token_quota,
        "token_used": token_used,
        "token_remaining": (token_quota - token_used) if token_quota else None,
        "request_quota": request_quota,
        "request_count": request_count,
        "request_remaining": (request_quota - request_count) if request_quota else None,
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
               expires_at: Optional[str] = None) -> dict:
    """创建密钥，返回 dict（含明文 key，仅此一次）。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("密钥名称不能为空")
    plain = _generate_key()
    key_id = _new_id()
    models = [str(m).strip() for m in (allowed_models or []) if str(m).strip()]
    db.execute(
        "INSERT INTO gateway_keys (id, name, key_prefix, key_hash, token_quota, token_used,"
        " request_quota, request_count, allowed_models, enabled, expires_at, created_by,"
        " created_at, last_used_at)"
        " VALUES (?,?,?,?,?,0,?,0,?,1,?,?,?,NULL)",
        (key_id, name, plain[:len(_KEY_PREFIX) + 6], _hash(plain),
         max(0, int(token_quota or 0)), max(0, int(request_quota or 0)),
         json.dumps(models, ensure_ascii=False), expires_at or None,
         created_by or "", _now()),
    )
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
    enabled = 1 if patch.get("enabled", cur["enabled"]) else 0
    expires_at = patch.get("expires_at", cur["expires_at"]) or None
    db.execute(
        "UPDATE gateway_keys SET name=?, token_quota=?, request_quota=?, allowed_models=?,"
        " enabled=?, expires_at=? WHERE id=?",
        (name, max(0, token_quota), max(0, request_quota),
         json.dumps(allowed, ensure_ascii=False), enabled, expires_at, key_id),
    )
    return get_key(key_id)


def delete_key(key_id: str) -> bool:
    return db.execute("DELETE FROM gateway_keys WHERE id=?", (key_id,)) > 0


def reset_usage(key_id: str) -> Optional[dict]:
    db.execute("UPDATE gateway_keys SET token_used=0, request_count=0 WHERE id=?", (key_id,))
    return get_key(key_id)


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


def consume_request(key_id: str) -> None:
    """请求计数 +1 并刷新最近使用时间（进入上游调用前扣减）。"""
    db.execute(
        "UPDATE gateway_keys SET request_count = request_count + 1, last_used_at=? WHERE id=?",
        (_now(), key_id),
    )


def consume_tokens(key_id: str, total_tokens: int) -> None:
    """按实际 Token 消耗累加（响应结束后调用）。"""
    if total_tokens and total_tokens > 0:
        db.execute(
            "UPDATE gateway_keys SET token_used = token_used + ? WHERE id=?",
            (int(total_tokens), key_id),
        )
