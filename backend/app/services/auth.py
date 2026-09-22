"""
见字如面 - 认证服务
用户管理 + 登录态（基于 token，滑动续期，有效期由 SESSION_DURATION 配置）
用户数据持久化到 SQLite (backend/data/app.db)
"""
import hashlib
import secrets
import time
import threading
from typing import Optional

from app.config import settings
from app.core import db

# 活跃会话短缓存（权威数据在 SQLite auth_sessions）
# { token: { user_id, expires_at, cached_at } }
_active_sessions: dict[str, dict] = {}
_session_lock = threading.Lock()
SESSION_DURATION = settings.SESSION_DURATION  # 默认 24 小时，滑动续期
_SESSION_CACHE_TTL = 60


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _store_session(token: str, user_id: str, expires_at: float, user_agent: str = "", ip: str = ""):
    now = time.time()
    db.execute(
        "INSERT INTO auth_sessions (token_hash, user_id, expires_at, created_at, last_seen_at, user_agent, ip)"
        " VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(token_hash) DO UPDATE SET"
        " user_id=excluded.user_id, expires_at=excluded.expires_at, last_seen_at=excluded.last_seen_at,"
        " user_agent=excluded.user_agent, ip=excluded.ip",
        (_token_hash(token), user_id, expires_at, now, now, user_agent, ip),
    )
    with _session_lock:
        _active_sessions[token] = {"user_id": user_id, "expires_at": expires_at, "cached_at": now}


# ---- 开发测试用固定 token ----
# 启动时自动为管理员账号注入一个长期有效的 token，便于本地调试
import os  # noqa: E402
if os.environ.get('DEV_TEST_TOKEN'):
    _store_session(
        os.environ['DEV_TEST_TOKEN'],
        "1c067a06417073d58b106e37a320c561",
        time.time() + 86400 * 365,
    )

# PBKDF2 迭代次数（OWASP 推荐 SHA-256 至少 600k）
_PBKDF2_ITERATIONS = 600_000


def _cleanup_expired():
    """清理过期会话（短缓存 + SQLite）"""
    now = time.time()
    with _session_lock:
        expired = [
            t for t, s in _active_sessions.items()
            if s["expires_at"] <= now or now - s.get("cached_at", 0) > _SESSION_CACHE_TTL
        ]
        for t in expired:
            del _active_sessions[t]
    db.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (now,))


def _hash_password(password: str, salt: str = None) -> tuple[str, str]:
    """对密码进行 PBKDF2-SHA256 加盐哈希（慢哈希，抗暴力破解）"""
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS
    ).hex()
    return h, salt


def _hash_password_legacy(password: str, salt: str) -> str:
    """旧版单轮 SHA-256 哈希，仅用于存量用户验证（登录成功后自动升级）"""
    return hashlib.sha256((salt + password).encode()).hexdigest()


def _verify_password(user: dict, password: str) -> bool:
    """验证密码，兼容新旧两种哈希格式"""
    if user.get("hash_algo") == "pbkdf2":
        pw_hash, _ = _hash_password(password, user["salt"])
    else:
        pw_hash = _hash_password_legacy(password, user["salt"])
    return secrets.compare_digest(pw_hash, user["password_hash"])


def _load_users() -> list[dict]:
    """从 SQLite 加载全部用户"""
    return db.query("SELECT * FROM users ORDER BY created_at")


def _get_user(user_id: str) -> Optional[dict]:
    return db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))


# ============================================
# 公开接口
# ============================================

def register(username: str, password: str, contact: str) -> dict:
    """
    注册新用户。
    - 第一个注册用户自动成为管理员
    - 管理员只能有一个
    - 用户名不能重复
    返回 user_info dict（不含密码）
    """
    users = _load_users()

    # 检查用户名和联系方式是否已存在
    for u in users:
        if u["username"] == username:
            raise ValueError("用户名已存在")
        if (u.get("contact") or "").strip() == contact.strip():
            raise ValueError("该联系方式已被其他账号使用")

    # 第一个用户为管理员
    is_admin = len(users) == 0

    # 密码哈希
    pw_hash, salt = _hash_password(password)

    now = time.time()
    user = {
        "id": str(secrets.token_hex(16)),
        "username": username,
        "password_hash": pw_hash,
        "salt": salt,
        "hash_algo": "pbkdf2",
        "contact": contact,
        "role": "admin" if is_admin else "user",
        "is_active": 1,
        "created_at": now,
        "updated_at": now,
        "last_login_at": None,
    }

    db.execute(
        "INSERT INTO users (id, username, password_hash, salt, hash_algo, contact,"
        " role, is_active, created_at, updated_at, last_login_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            user["id"], username, pw_hash, salt, "pbkdf2", contact,
            user["role"], 1, now, now, None,
        ),
    )

    # 返回不包含密码的用户信息
    return _user_info(user)


def login(username: str, password: str) -> tuple[Optional[str], Optional[dict]]:
    """
    用户登录。
    成功返回 (token, user_info)，失败返回 (None, None)
    token 有效期为 SESSION_DURATION 秒（滑动续期）。
    """
    _cleanup_expired()

    u = db.query_one("SELECT * FROM users WHERE username = ?", (username,))
    if not u:
        return None, None
    if not u["is_active"]:
        raise ValueError("该账号已被禁用，请联系管理员")

    # 验证密码（兼容旧格式）
    if not _verify_password(u, password):
        return None, None

    now = time.time()
    updates = {"last_login_at": now}

    # 旧格式哈希登录成功后自动升级为 PBKDF2
    if u.get("hash_algo") != "pbkdf2":
        new_hash, new_salt = _hash_password(password)
        updates.update({
            "password_hash": new_hash,
            "salt": new_salt,
            "hash_algo": "pbkdf2",
        })

    set_parts = ", ".join(f"{k} = ?" for k in updates)
    db.execute(
        f"UPDATE users SET {set_parts}, updated_at = ? WHERE id = ?",
        (*updates.values(), now, u["id"]),
    )
    u.update(updates)

    # 生成 token 并持久化会话
    token = secrets.token_hex(32)
    expires_at = time.time() + SESSION_DURATION
    _store_session(token, u["id"], expires_at)

    return token, _user_info(u)


def logout(token: str):
    """登出，清除 token"""
    with _session_lock:
        _active_sessions.pop(token, None)
    db.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (_token_hash(token),))


def get_session_user(token: str) -> Optional[dict]:
    """
    根据 token 获取当前用户信息。
    如果 token 无效或已过期，返回 None。
    如果 token 有效，自动滑动续期。
    """
    now = time.time()
    _cleanup_expired()
    with _session_lock:
        session = _active_sessions.get(token)
        if session and session["expires_at"] > now:
            # 续期短缓存与 SQLite
            session["expires_at"] = now + SESSION_DURATION
            session["cached_at"] = now
            db.execute(
                "UPDATE auth_sessions SET expires_at = ?, last_seen_at = ? WHERE token_hash = ?",
                (session["expires_at"], now, _token_hash(token)),
            )
            user = _get_user(session["user_id"])
            return _user_info(user) if user else None

    row = db.query_one(
        "SELECT user_id, expires_at FROM auth_sessions WHERE token_hash = ?",
        (_token_hash(token),),
    )
    if not row or row["expires_at"] <= now:
        if row:
            db.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (_token_hash(token),))
        return None

    expires_at = now + SESSION_DURATION
    db.execute(
        "UPDATE auth_sessions SET expires_at = ?, last_seen_at = ? WHERE token_hash = ?",
        (expires_at, now, _token_hash(token)),
    )
    with _session_lock:
        _active_sessions[token] = {
            "user_id": row["user_id"],
            "expires_at": expires_at,
            "cached_at": now,
        }

    user = _get_user(row["user_id"])
    return _user_info(user) if user else None


def get_all_users(token: str) -> list[dict]:
    """
    获取所有用户列表（仅管理员可用）。
    """
    user = get_session_user(token)
    if not user:
        raise PermissionError("未登录")
    if user["role"] != "admin":
        raise PermissionError("仅管理员可执行此操作")
    return [_user_info(u) for u in _load_users()]


def set_user_active(token: str, target_user_id: str, is_active: bool):
    """启用/禁用用户（仅管理员）"""
    admin = get_session_user(token)
    if not admin:
        raise PermissionError("未登录")
    if admin["role"] != "admin":
        raise PermissionError("仅管理员可执行此操作")
    if admin["id"] == target_user_id:
        raise ValueError("不能操作自己的账号")

    now = time.time()
    affected = db.execute(
        "UPDATE users SET is_active = ?, updated_at = ? WHERE id = ?",
        (1 if is_active else 0, now, target_user_id),
    )
    if not affected:
        raise ValueError("用户不存在")
    return _user_info(_get_user(target_user_id))


def delete_user(token: str, target_user_id: str):
    """删除用户（仅管理员）"""
    admin = get_session_user(token)
    if not admin:
        raise PermissionError("未登录")
    if admin["role"] != "admin":
        raise PermissionError("仅管理员可执行此操作")
    if admin["id"] == target_user_id:
        raise ValueError("不能删除自己的账号")

    affected = db.execute("DELETE FROM users WHERE id = ?", (target_user_id,))
    if not affected:
        raise ValueError("用户不存在")

    # 清理被删除用户的会话
    db.execute("DELETE FROM auth_sessions WHERE user_id = ?", (target_user_id,))
    with _session_lock:
        tokens_to_remove = [
            t for t, s in _active_sessions.items()
            if s["user_id"] == target_user_id
        ]
        for t in tokens_to_remove:
            del _active_sessions[t]


def _user_info(user: dict) -> dict:
    """返回不含敏感字段的用户信息"""
    return {
        "id": user["id"],
        "username": user["username"],
        "contact": user["contact"],
        "role": user["role"],
        "is_active": bool(user["is_active"]),
        "created_at": user["created_at"],
        "last_login_at": user.get("last_login_at"),
    }


def update_profile(
    token: str,
    username: str = None,
    contact: str = None,
    old_password: str = None,
    new_password: str = None,
) -> dict:
    """修改当前登录用户的个人信息（用户名/联系方式/密码）。

    - 修改用户名/联系方式时校验唯一性
    - 修改密码需提供正确的旧密码
    返回更新后的 user_info。
    """
    user = get_session_user(token)
    if not user:
        raise PermissionError("未登录")

    current = _get_user(user["id"])
    if not current:
        raise ValueError("用户不存在")

    updates = {}

    # 用户名
    if username is not None:
        username = username.strip()
        if len(username) < 2:
            raise ValueError("用户名至少 2 个字符")
        if username != current["username"]:
            exists = db.query_one("SELECT id FROM users WHERE username = ?", (username,))
            if exists:
                raise ValueError("用户名已存在")
            updates["username"] = username

    # 联系方式
    if contact is not None:
        contact = contact.strip()
        if not contact:
            raise ValueError("联系方式不能为空")
        if contact != (current.get("contact") or ""):
            exists = db.query_one(
                "SELECT id FROM users WHERE contact = ? AND id != ?", (contact, current["id"])
            )
            if exists:
                raise ValueError("该联系方式已被其他账号使用")
            updates["contact"] = contact

    # 密码
    if new_password is not None and new_password != "":
        if len(new_password) < 6:
            raise ValueError("新密码至少 6 个字符")
        if not old_password or not _verify_password(current, old_password):
            raise ValueError("原密码不正确")
        pw_hash, salt = _hash_password(new_password)
        updates.update({"password_hash": pw_hash, "salt": salt, "hash_algo": "pbkdf2"})

    if not updates:
        return _user_info(current)

    now = time.time()
    set_parts = ", ".join(f"{k} = ?" for k in updates)
    db.execute(
        f"UPDATE users SET {set_parts}, updated_at = ? WHERE id = ?",
        (*updates.values(), now, current["id"]),
    )
    return _user_info(_get_user(current["id"]))


def reset_password(username: str, contact: str, new_password: str) -> dict:
    """忘记密码：通过「用户名 + 注册联系方式」验证身份后重置密码。

    - 联系方式在注册时唯一，作为无邮件/短信服务下的身份凭证
    - 用户名与联系方式必须同时匹配同一账号，否则统一报错（不泄露账号是否存在）
    - 重置成功后失效该用户的全部登录会话，强制其他设备重新登录
    返回更新后的 user_info。
    """
    username = (username or "").strip()
    contact = (contact or "").strip()
    if not username or not contact:
        raise ValueError("请填写用户名和联系方式")
    if not new_password or len(new_password) < 6:
        raise ValueError("新密码至少 6 个字符")

    u = db.query_one("SELECT * FROM users WHERE username = ?", (username,))
    # 统一错误文案，避免暴露"用户名是否存在"
    if not u or (u.get("contact") or "").strip() != contact:
        raise ValueError("用户名或联系方式不匹配")
    if not u["is_active"]:
        raise ValueError("该账号已被禁用，请联系管理员")

    pw_hash, salt = _hash_password(new_password)
    now = time.time()
    db.execute(
        "UPDATE users SET password_hash = ?, salt = ?, hash_algo = 'pbkdf2', updated_at = ? WHERE id = ?",
        (pw_hash, salt, now, u["id"]),
    )

    # 失效该用户全部会话（含短缓存与 SQLite），强制重新登录
    db.execute("DELETE FROM auth_sessions WHERE user_id = ?", (u["id"],))
    with _session_lock:
        for t in [t for t, s in _active_sessions.items() if s["user_id"] == u["id"]]:
            del _active_sessions[t]

    return _user_info(_get_user(u["id"]))
