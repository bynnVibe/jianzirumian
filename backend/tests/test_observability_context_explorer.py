"""Agent 上下文构成 explorer 回归：日期两级筛选 + 对话消耗对比（内存 DB，不出网）。

覆盖需求：
  T1 缺省返回 dates（会话活跃日期降序）、sessions、compare（本对话总耗/上次对话/平均对话）；
  T2 date 参数按日期过滤 sessions，并缺省选中该日期最新会话；
  T3 compare.prev 取活跃时间紧邻的上一会话，首对话 prev=None；
  T4 date 参数非法格式返回 422（Query pattern 校验）。
"""
import asyncio
import sqlite3
import uuid
from datetime import datetime

import pytest
from fastapi import FastAPI

from app.core import db


@pytest.fixture()
def mem_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    yield conn
    conn.close()


def _obs_app():
    from app.api import observability as obs_api
    app = FastAPI()
    app.include_router(obs_api.router)
    return app


def _add_user(uid, username):
    now = datetime.now().isoformat()
    db.execute(
        "INSERT INTO users (id, username, password_hash, salt, role, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (uid, username, "x", "s", "user", now, now))


def _add_session(sid, uid, title, updated_at):
    db.execute(
        "INSERT INTO chat_sessions (session_id, user_id, title, created_at, updated_at)"
        " VALUES (?,?,?,?,?)",
        (sid, uid, title, updated_at, updated_at))


def _add_trace(sid, uid, prompt, completion, status="ok", started_at=None):
    tid = str(uuid.uuid4())
    db.execute(
        "INSERT INTO agent_traces (id, agent, user_id, session_id, input, status,"
        " started_at, prompt_tokens, completion_tokens)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (tid, "chat", uid, sid, "hi", status,
         started_at or datetime.now().isoformat(), prompt, completion))
    return tid


def _seed():
    """三个不同日期的会话，各一条完成 trace。tokens：s1=300 s2=4500 s3=93000。"""
    _add_user("u1", "alice")
    # 越新越靠前：s3(11-15) 最旧、s1(11-17) 最新
    _add_session("s3", "u1", "旧对话", "2026-11-15T10:00:00")
    _add_trace("s3", "u1", 90000, 3000, started_at="2026-11-15T10:00:00")
    _add_session("s2", "u1", "中对话", "2026-11-16T10:00:00")
    _add_trace("s2", "u1", 4000, 500, started_at="2026-11-16T10:00:00")
    _add_session("s1", "u1", "新对话", "2026-11-17T10:00:00")
    _add_trace("s1", "u1", 200, 100, started_at="2026-11-17T10:00:00")


def _get(app, params):
    import httpx

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/api/observability/context-explorer", params=params)
    return asyncio.run(run())


# ============================================
# T1 缺省：dates + compare 结构
# ============================================
def test_context_explorer_default_dates_and_compare(mem_db):
    from app.api import deps
    _seed()
    app = _obs_app()
    app.dependency_overrides[deps.require_ops] = lambda: {"id": "a1", "role": "admin"}
    r = _get(app, {})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    # dates 降序，最新在前
    assert body["dates"][:3] == ["2026-11-17", "2026-11-16", "2026-11-15"]
    # 缺省选中全局最新 trace 所属会话 s1
    assert body["session_id"] == "s1"
    cmp = body["compare"]
    assert cmp["session_tokens"] == 300          # s1: 200+100
    assert cmp["prev"]["session_id"] == "s2"      # 活跃紧邻上一会话
    assert cmp["prev"]["tokens"] == 4500          # s2: 4000+500
    # 平均：(300+4500+93000)/3 = 32600
    assert cmp["avg_tokens"] == 32600


# ============================================
# T2 date 过滤 + 缺省选中该日期最新会话
# ============================================
def test_context_explorer_date_filter(mem_db):
    from app.api import deps
    _seed()
    app = _obs_app()
    app.dependency_overrides[deps.require_ops] = lambda: {"id": "a1", "role": "ops"}
    r = _get(app, {"date": "2026-11-16"})
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == "2026-11-16"
    # sessions 仅含该日期会话
    assert [s["session_id"] for s in body["sessions"]] == ["s2"]
    # 缺省选中该日期最新会话 s2
    assert body["session_id"] == "s2"
    assert body["compare"]["session_tokens"] == 4500
    # s2 的上一会话是 s3
    assert body["compare"]["prev"]["session_id"] == "s3"


# ============================================
# T3 首对话（活跃最旧）prev 为 None
# ============================================
def test_context_explorer_first_session_no_prev(mem_db):
    from app.api import deps
    _seed()
    app = _obs_app()
    app.dependency_overrides[deps.require_ops] = lambda: {"id": "a1", "role": "admin"}
    r = _get(app, {"session_id": "s3"})
    assert r.status_code == 200
    cmp = r.json()["compare"]
    assert cmp["session_tokens"] == 93000
    assert cmp["prev"] is None


# ============================================
# T4 非法 date 格式 -> 422
# ============================================
def test_context_explorer_invalid_date_422(mem_db):
    from app.api import deps
    _seed()
    app = _obs_app()
    app.dependency_overrides[deps.require_ops] = lambda: {"id": "a1", "role": "admin"}
    r = _get(app, {"date": "2026/11/16"})
    assert r.status_code == 422
