"""会话归属与权限依赖测试"""
import pytest
from fastapi import HTTPException

from app.api import deps
from app.services.chat import ChatService


# ============================================
# 会话归属（数据隔离）
# ============================================

@pytest.fixture
def svc(tmp_path, monkeypatch):
    """独立的 ChatService 实例，持久化到临时目录"""
    monkeypatch.setattr(ChatService, "SESSIONS_FILE", tmp_path / "sessions.json")
    return ChatService()


def test_get_user_session_blocks_other_user(svc):
    session = svc.create_session(user_id="user-a")
    assert svc.get_user_session(session.session_id, "user-a") is session
    assert svc.get_user_session(session.session_id, "user-b") is None


def test_delete_session_blocks_other_user(svc):
    session = svc.create_session(user_id="user-a")
    assert svc.delete_session(session.session_id, user_id="user-b") is False
    assert svc.delete_session(session.session_id, user_id="user-a") is True


def test_list_sessions_is_isolated_by_user(svc):
    svc.create_session(user_id="user-a")
    svc.create_session(user_id="user-a")
    svc.create_session(user_id="user-b")
    assert len(svc.list_sessions(user_id="user-a")) == 2
    assert len(svc.list_sessions(user_id="user-b")) == 1


def test_create_session_with_explicit_id_no_ghost(svc):
    """指定 session_id 创建时不应产生多余的孤儿会话条目"""
    session = svc.create_session(user_id="user-a", session_id="my-session-id")
    assert session.session_id == "my-session-id"
    assert list(svc.sessions.keys()) == ["my-session-id"]


# ============================================
# 鉴权依赖（get_current_user / require_admin）
# ============================================

def test_get_current_user_requires_token():
    with pytest.raises(HTTPException) as exc:
        deps.get_current_user("")
    assert exc.value.status_code == 401


def test_get_current_user_rejects_invalid_token(monkeypatch):
    monkeypatch.setattr(deps.auth_service, "get_session_user", lambda t: None)
    with pytest.raises(HTTPException) as exc:
        deps.get_current_user("Bearer invalid-token")
    assert exc.value.status_code == 401


def test_require_admin_rejects_normal_user(monkeypatch):
    monkeypatch.setattr(
        deps.auth_service, "get_session_user",
        lambda t: {"id": "u1", "role": "user"},
    )
    with pytest.raises(HTTPException) as exc:
        deps.require_admin("Bearer some-token")
    assert exc.value.status_code == 403


def test_require_admin_allows_admin(monkeypatch):
    monkeypatch.setattr(
        deps.auth_service, "get_session_user",
        lambda t: {"id": "u1", "role": "admin"},
    )
    user = deps.require_admin("Bearer some-token")
    assert user["role"] == "admin"
