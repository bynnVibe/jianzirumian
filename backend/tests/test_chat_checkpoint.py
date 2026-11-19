"""整改4：中断任务 checkpoint 生命周期回归（内存 DB，不出网）。

覆盖：停止即打包 pending + 记忆/日志留痕；继续 claim 并发安全；
不继续软删除 abandoned + 48h 保留期；过期惰性清理；resume 上下文注入文本。
"""
import sqlite3
from datetime import datetime, timedelta

import pytest

from app.core import db
from app.services import chat_checkpoint as cp


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    # 测试不触达 Redis：副本写入 best-effort 关闭
    monkeypatch.setattr(cp, "_redis_set", lambda *a, **k: None)
    db.execute("INSERT INTO users(id,username,password_hash,salt,created_at,updated_at)"
               " VALUES('alice','alice','unused','unused','0','0')")
    yield conn
    conn.close()


def test_create_pending_writes_memory_and_list():
    created = cp.create_checkpoint(session_id="s1", user_id="alice", query="系统的网关模块设计",
                                   partial_answer="网关模块分为", sources=[{"title": "来源1"}],
                                   reason="cancelled")
    assert created and created["status"] == "pending"
    pend = cp.list_pending("s1", "alice")
    assert len(pend) == 1 and pend[0]["id"] == created["id"]
    assert pend[0]["sources"] == [{"title": "来源1"}]
    # 记忆留痕：后续可检索该停止任务
    mem = db.query_one("SELECT content FROM user_memories WHERE user_id='alice'")
    assert mem and created["id"] in mem["content"] and "中途停止" in mem["content"]
    # 游客不打包
    assert cp.create_checkpoint(session_id="s1", user_id="guest", query="q") is None


def test_claim_once_and_resume_context():
    created = cp.create_checkpoint(session_id="s1", user_id="alice", query="网关设计",
                                   partial_answer="已完成A部分")
    claimed = cp.claim(created["id"], "alice", "s1")
    assert claimed and claimed["status"] == "continued"
    # 并发/重复认领失败；跨会话认领失败
    assert cp.claim(created["id"], "alice", "s1") is None
    assert cp.list_pending("s1", "alice") == []
    ctx = cp.resume_context(claimed)
    assert "网关设计" in ctx and "已完成A部分" in ctx and "不要重复" in ctx
    # 无正文的 checkpoint：提示完整回答
    created2 = cp.create_checkpoint(session_id="s2", user_id="alice", query="另一问")
    ctx2 = cp.resume_context(cp.claim(created2["id"], "alice", "s2"))
    assert "完整回答" in ctx2


def test_abandon_soft_delete_48h_and_purge():
    created = cp.create_checkpoint(session_id="s1", user_id="alice", query="网关设计")
    assert cp.abandon(created["id"], "alice") is True
    row = db.query_one("SELECT * FROM chat_checkpoints WHERE id=?", (created["id"],))
    assert row["status"] == "abandoned" and row["expires_at"]
    expires = datetime.fromisoformat(row["expires_at"])
    delta = expires - datetime.now()
    assert timedelta(hours=47) < delta <= timedelta(hours=48)
    # 软删除后不再出现在待处理列表；重复放弃失败
    assert cp.list_pending("s1", "alice") == []
    assert cp.abandon(created["id"], "alice") is False
    # 记忆留痕含 48h 与检索提示
    mems = db.query("SELECT content FROM user_memories WHERE user_id='alice' ORDER BY created_at")
    assert any("48h" in m["content"] and "软删除" in m["content"] for m in mems)
    # 保留期内不清理；过期后惰性物理删除
    assert cp.purge_expired() == 0
    db.execute("UPDATE chat_checkpoints SET expires_at=? WHERE id=?",
               ((datetime.now() - timedelta(hours=1)).isoformat(), created["id"]))
    assert cp.purge_expired() == 1
    assert db.query_one("SELECT * FROM chat_checkpoints WHERE id=?", (created["id"],)) is None


def test_abandon_requires_owner():
    created = cp.create_checkpoint(session_id="s1", user_id="alice", query="q")
    assert cp.abandon(created["id"], "bob") is False
    assert cp.claim(created["id"], "bob") is None