"""资源变更同步（D）：触发器原子发布、授权范围隔离、游标补发与 reset、同步 API"""
import sqlite3
import time
from datetime import datetime, timedelta

import pytest

from app.core import db


@pytest.fixture()
def mem_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    yield conn
    conn.close()


def _mk_kb(kb_id="kb1"):
    db.execute("INSERT INTO knowledge_bases (id, name, created_at) VALUES (?,?,?)",
               (kb_id, "测试库", datetime.now().isoformat()))


def _mk_session(sid, uid):
    now = datetime.now().isoformat()
    db.execute("INSERT INTO chat_sessions (session_id, user_id, title, created_at, updated_at)"
               " VALUES (?,?,?,?,?)", (sid, uid, "会话", now, now))


# ============================================
# 触发器：业务写入与版本/事件同事务原子落库
# ============================================

def test_trigger_bumps_revision_and_event(mem_db):
    from app.services import sync_events
    _mk_kb()
    rev = db.query_one("SELECT version FROM resource_revisions WHERE resource='knowledge'")
    assert rev["version"] == 1
    ev = db.query_one("SELECT * FROM resource_change_events WHERE resource='knowledge'")
    assert ev["version"] == 1 and ev["scope"] == "global" and ev["payload"] == "{}"
    db.execute("UPDATE knowledge_bases SET name='改名' WHERE id='kb1'")
    rev = db.query_one("SELECT version FROM resource_revisions WHERE resource='knowledge'")
    assert rev["version"] == 2
    assert db.query_one("SELECT COUNT(*) AS n FROM resource_change_events"
                        " WHERE resource='knowledge'")["n"] == 2
    snap = sync_events.snapshot("u1", False)
    assert snap["versions"]["knowledge"] == 2
    assert snap["watermark"] >= 2


def test_trigger_rollback_atomic(mem_db):
    """业务写入回滚时版本/事件一并回滚（同事务原子性）。"""
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute("INSERT INTO knowledge_bases (id, name, created_at) VALUES ('kbx','x',?)",
                         (datetime.now().isoformat(),))
            conn.execute("INSERT INTO knowledge_bases (id, name, created_at) VALUES ('kbx','dup',?)",
                         (datetime.now().isoformat(),))
    assert db.query_one("SELECT COUNT(*) AS n FROM resource_revisions"
                        " WHERE resource='knowledge'")["n"] == 0
    assert db.query_one("SELECT COUNT(*) AS n FROM resource_change_events")["n"] == 0


def test_gateway_write_publishes_ops_resource(mem_db):
    from app.services.gateway import keys as key_svc
    key_svc.create_key("k")
    ev = db.query_one("SELECT * FROM resource_change_events WHERE resource='gateway'")
    assert ev["scope"] == "ops"


# ============================================
# 授权范围：跨用户/越权隔离
# ============================================

def test_session_events_user_isolation(mem_db):
    from app.services import sync_events
    _mk_session("s1", "u1")
    _mk_session("s2", "u2")
    evs = sync_events.events_after(0, "u1", False)["events"]
    sessions = [e for e in evs if e["resource"] == "sessions"]
    assert len(sessions) == 1  # 仅自己的会话变更
    evs2 = sync_events.events_after(0, "u2", False)["events"]
    assert len([e for e in evs2 if e["resource"] == "sessions"]) == 1
    # 事件 id 不同（各自一条）
    assert sessions[0]["id"] != [e for e in evs2 if e["resource"] == "sessions"][0]["id"]


def test_snapshot_scope_filtering(mem_db):
    from app.services import sync_events
    from app.services.gateway import keys as key_svc
    _mk_kb()
    key_svc.create_key("k")
    regular = sync_events.snapshot("u1", False)["versions"]
    assert "knowledge" in regular and "gateway" not in regular
    ops = sync_events.snapshot("u1", True)["versions"]
    assert "gateway" in ops
    # 会话资源仅登录用户可见（游客无 user_id）
    _mk_session("s1", "u1")
    assert "sessions" in sync_events.snapshot("u1", False)["versions"]
    assert "sessions" not in sync_events.snapshot("", False)["versions"]


def test_message_insert_publishes_session_owner(mem_db):
    from app.services import sync_events
    _mk_session("s1", "u1")
    db.execute("INSERT INTO chat_messages (message_id, session_id, seq, role, content, created_at)"
               " VALUES ('m1','s1',1,'user','hi',?)", (datetime.now().isoformat(),))
    evs = sync_events.events_after(0, "u1", False)["events"]
    assert any(e["resource"] == "sessions" for e in evs)
    # 他人看不到该消息引发的会话变更（u2 无会话）
    evs2 = sync_events.events_after(0, "u2", False)["events"]
    assert not any(e["resource"] == "sessions" for e in evs2)


def test_message_delete_after_session_gone(mem_db):
    """会话已删时消息删除不违约（属主反查兜底空串）。"""
    _mk_session("s1", "u1")
    db.execute("INSERT INTO chat_messages (message_id, session_id, seq, role, content, created_at)"
               " VALUES ('m1','s1',1,'user','hi',?)", (datetime.now().isoformat(),))
    db.execute("DELETE FROM chat_sessions WHERE session_id='s1'")
    db.execute("DELETE FROM chat_messages WHERE message_id='m1'")  # 不应抛错


# ============================================
# 游标补发：增量 / 超窗 reset / 非法游标
# ============================================

def test_events_after_incremental(mem_db):
    from app.services import sync_events
    _mk_kb()
    first = sync_events.events_after(0, "u1", False)
    assert not first["reset"] and len(first["events"]) == 1
    cursor = first["events"][-1]["id"]
    assert sync_events.events_after(cursor, "u1", False)["events"] == []
    db.execute("UPDATE knowledge_bases SET name='再改' WHERE id='kb1'")
    batch = sync_events.events_after(cursor, "u1", False)
    assert len(batch["events"]) == 1 and batch["events"][0]["version"] == 2


def test_events_after_window_expired_reset(mem_db):
    from app.services import sync_events
    old = (datetime.now() - timedelta(hours=25)).isoformat()
    db.execute("INSERT INTO resource_change_events (resource, version, scope, user_id, payload, created_at)"
               " VALUES ('knowledge', 1, 'global', '', '{}', ?)", (old,))
    old_id = db.query_one("SELECT MAX(id) AS m FROM resource_change_events")["m"]
    batch = sync_events.events_after(old_id, "u1", False)
    assert batch["reset"] is True  # 超窗 → 客户端按快照定向重验


def test_events_after_invalid_cursor_reset(mem_db):
    from app.services import sync_events
    _mk_kb()
    assert sync_events.events_after(999999, "u1", False)["reset"] is True


def test_purge_old_keeps_window(mem_db):
    from app.services import sync_events
    old = (datetime.now() - timedelta(hours=25)).isoformat()
    db.execute("INSERT INTO resource_change_events (resource, version, scope, user_id, payload, created_at)"
               " VALUES ('knowledge', 1, 'global', '', '{}', ?)", (old,))
    _mk_kb()
    assert sync_events.purge_old() == 1
    assert db.query_one("SELECT COUNT(*) AS n FROM resource_change_events")["n"] == 1


# ============================================
# 同步 API：鉴权 + 版本快照 + SSE 首帧
# ============================================

def _sync_app():
    from fastapi import FastAPI
    from app.api import sync as sync_api
    app = FastAPI()
    app.include_router(sync_api.router)
    return app


def test_versions_requires_auth(mem_db):
    from fastapi.testclient import TestClient
    with TestClient(_sync_app()) as client:
        assert client.get("/api/sync/versions").status_code == 401


def test_versions_returns_scoped_snapshot(mem_db):
    from fastapi.testclient import TestClient
    from app.api import deps
    _mk_kb()

    app = _sync_app()
    app.dependency_overrides[deps.get_current_user] = lambda: {"id": "u1", "role": "user", "is_active": True}
    with TestClient(app) as client:
        res = client.get("/api/sync/versions", headers={"Authorization": "Bearer t"})
        assert res.status_code == 200
        body = res.json()
        assert body["success"] and body["versions"]["knowledge"] == 1
        assert "gateway" not in body["versions"]
        assert body["watermark"] >= 1


def test_events_sse_first_frame_snapshot(mem_db, monkeypatch):
    import json as _json
    from fastapi.testclient import TestClient
    from app.api import sync as sync_api
    _mk_kb()

    async def _fake_user(authorization=""):
        return {"id": "u1", "role": "user", "is_active": True}

    # /events 端点直接调用模块内导入的依赖函数，打桩模块属性
    monkeypatch.setattr(sync_api, "get_current_user", _fake_user)
    with TestClient(_sync_app()) as client:
        with client.stream("GET", "/api/sync/events",
                           headers={"Authorization": "Bearer t"}) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            event_name, data_line = "", ""
            for line in resp.iter_lines():
                if line.startswith("event: "):
                    event_name = line[7:]
                elif line.startswith("data: "):
                    data_line = line[6:]
                    break
            assert event_name == "snapshot"
            data = _json.loads(data_line)
            assert data["versions"]["knowledge"] == 1 and data["watermark"] >= 1


def test_peek_session_no_renewal(mem_db):
    """SSE 心跳只读校验：不滑动续期、不写 last_seen。"""
    from app.services import auth as auth_service
    auth_service.register("peekuser", "pw123456", "")
    token, _info = auth_service.login("peekuser", "pw123456")
    assert token
    before = db.query_one("SELECT expires_at, last_seen_at FROM auth_sessions")
    time.sleep(0.05)
    user = auth_service.peek_session_user(token)
    assert user and user["username"] == "peekuser"
    after = db.query_one("SELECT expires_at, last_seen_at FROM auth_sessions")
    assert after["expires_at"] == before["expires_at"]
    assert after["last_seen_at"] == before["last_seen_at"]
    assert auth_service.peek_session_user("invalid-token") is None