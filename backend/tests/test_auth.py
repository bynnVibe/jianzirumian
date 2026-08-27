"""认证服务测试：注册/登录/旧哈希迁移/token 过期"""
import time

import pytest

from app.services import auth


@pytest.fixture(autouse=True)
def isolate_users(tmp_path, monkeypatch):
    """将用户文件与内存会话隔离到临时目录，避免污染真实数据"""
    monkeypatch.setattr(auth, "USERS_FILE", tmp_path / "users.json")
    auth._active_sessions.clear()
    yield
    auth._active_sessions.clear()


def test_first_user_is_admin():
    u1 = auth.register("alice", "pass123", "alice@test.com")
    u2 = auth.register("bob", "pass456", "bob@test.com")
    assert u1["role"] == "admin"
    assert u2["role"] == "user"


def test_duplicate_username_rejected():
    auth.register("alice", "pass123", "alice@test.com")
    with pytest.raises(ValueError):
        auth.register("alice", "other", "other@test.com")


def test_login_success_and_wrong_password():
    auth.register("alice", "pass123", "alice@test.com")
    token, info = auth.login("alice", "pass123")
    assert token and info["username"] == "alice"

    token2, info2 = auth.login("alice", "wrong")
    assert token2 is None and info2 is None


def test_new_user_uses_pbkdf2():
    auth.register("alice", "pass123", "alice@test.com")
    users = auth._load_users()
    assert users[0]["hash_algo"] == "pbkdf2"
    # 哈希值不应等于旧版单轮 SHA-256
    legacy = auth._hash_password_legacy("pass123", users[0]["salt"])
    assert users[0]["password_hash"] != legacy


def test_legacy_sha256_user_can_login_and_is_upgraded():
    # 手工构造一个旧格式（单轮 SHA-256、无 hash_algo 字段）的存量用户
    salt = "abcd1234"
    legacy_hash = auth._hash_password_legacy("oldpass", salt)
    auth._save_users([{
        "id": "legacy-user-1",
        "username": "legacy",
        "password_hash": legacy_hash,
        "salt": salt,
        "contact": "legacy@test.com",
        "role": "admin",
        "is_active": True,
        "created_at": time.time(),
        "updated_at": time.time(),
    }])

    token, info = auth.login("legacy", "oldpass")
    assert token and info["username"] == "legacy"

    # 登录成功后自动升级为 PBKDF2，且新哈希仍可登录
    users = auth._load_users()
    assert users[0]["hash_algo"] == "pbkdf2"
    token2, _ = auth.login("legacy", "oldpass")
    assert token2


def test_token_expiry():
    auth.register("alice", "pass123", "alice@test.com")
    token, _ = auth.login("alice", "pass123")
    assert auth.get_session_user(token) is not None

    # 将会话置为已过期
    auth._active_sessions[token]["expires_at"] = time.time() - 1
    assert auth.get_session_user(token) is None


def test_token_sliding_renewal():
    auth.register("alice", "pass123", "alice@test.com")
    token, _ = auth.login("alice", "pass123")
    old_expiry = auth._active_sessions[token]["expires_at"]
    time.sleep(0.01)
    auth.get_session_user(token)
    assert auth._active_sessions[token]["expires_at"] > old_expiry
