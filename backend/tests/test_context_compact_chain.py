"""上下文三级压缩链测试（microCompact/autoCompact/SessionMemoryCompact，对齐 Claude Code）。

覆盖：
  九节 checklist（1-5 节既有契约 + 6-9 节 CC 对齐）与全量历史送摘要器；
  边界标记 compact_kind/before_tokens 与压缩事件埋点；
  熔断（连续失败 ≥3 停自动、force 绕过、成功重置）与递归守卫；
  SESSION.md 持久载体（压缩/笔记写入、DB 缺失文件恢复）；
  microCompact 空闲衰减（读时投影、最近 5 条全文、事件埋点）；
  自动模式再水化「抑制追问」（suppressFollowUpQuestions 对齐）。
"""
import asyncio
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core import db
from app.services import memory_store
from app.services import session_summary as ss


@pytest.fixture()
def file_db(tmp_path, monkeypatch):
    """临时文件数据库：SESSION.md 真实落盘（隔离目录内）。"""
    conn = sqlite3.connect(str(tmp_path / "app.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    ss._fail_streak.clear()
    ss._compacting.clear()
    yield conn
    conn.close()


class FakeLLM:
    last_prompt = ""

    def __init__(self, reply="1.意图：测试\n2.结论：无\n3.约束：无\n4.未决：无\n5.下一步：继续"):
        self.reply = reply

    async def chat(self, messages, stream=False, **kwargs):
        FakeLLM.last_prompt = messages[-1]["content"]
        yield self.reply


def _patch_llm(monkeypatch, reply=None):
    llm = FakeLLM(reply) if reply else FakeLLM()
    monkeypatch.setattr("app.core.llm.LLMFactory.create_for_role", lambda *a, **k: llm)
    return llm


def _patch_llm_fail(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("上游不可用")

    monkeypatch.setattr("app.core.llm.LLMFactory.create_for_role", _boom)


def _make_session(n=10, content_len=60, user_id="u1", session_id="s1"):
    msgs = [
        SimpleNamespace(message_id=f"m{i}", role="user" if i % 2 == 0 else "assistant",
                        content=(f"消息{i}：" + "养" * max(0, content_len - 4)))
        for i in range(n)
    ]
    return SimpleNamespace(session_id=session_id, user_id=user_id, messages=msgs)


# ============================================
# autoCompact：九节契约 + 全量历史 + 边界标记
# ============================================

def test_nine_section_checklist(file_db, monkeypatch):
    _patch_llm(monkeypatch)
    monkeypatch.setattr(ss, "CONTEXT_COMPACT_TRIGGER_TOKENS", 10)
    asyncio.run(ss.maybe_summarize(_make_session()))
    prompt = FakeLLM.last_prompt
    for frag in ("1.用户意图与当前任务", "5.下一步",
                 "6.所有用户消息枚举", "7.知识来源与附件状态",
                 "8.当前工作", "9.错误与纠正"):
        assert frag in prompt


def test_full_transcript_not_400_clipped(file_db, monkeypatch):
    """全量历史送摘要器：600 字消息不再被旧版 400 字粗截断。"""
    _patch_llm(monkeypatch)
    monkeypatch.setattr(ss, "CONTEXT_COMPACT_TRIGGER_TOKENS", 10)
    session = _make_session(content_len=600)
    asyncio.run(ss.maybe_summarize(session))
    assert session.messages[0].content[500] in FakeLLM.last_prompt


def test_boundary_markers_and_events(file_db, monkeypatch):
    _patch_llm(monkeypatch)
    monkeypatch.setattr(ss, "CONTEXT_COMPACT_TRIGGER_TOKENS", 10)
    session = _make_session()
    asyncio.run(ss.maybe_summarize(session))
    row = ss.get_summary("s1")
    assert row["compact_kind"] == "auto" and row["before_tokens"] > 0
    asyncio.run(ss.maybe_summarize(session, force=True))
    row = ss.get_summary("s1")
    assert row["compact_kind"] == "manual"
    kinds = [r["kind"] for r in db.query(
        "SELECT kind FROM context_compact_events WHERE session_id='s1' ORDER BY created_at")]
    assert kinds == ["auto", "manual"]
    ev = db.query_one("SELECT * FROM context_compact_events WHERE session_id='s1' AND kind='auto'")
    assert ev["before_tokens"] > 0 and ev["after_tokens"] > 0


# ============================================
# autoCompact：熔断与递归守卫
# ============================================

def test_fail_streak_circuit_breaker(file_db, monkeypatch):
    _patch_llm_fail(monkeypatch)
    monkeypatch.setattr(ss, "CONTEXT_COMPACT_TRIGGER_TOKENS", 10)
    session = _make_session()
    for _ in range(ss.COMPACT_FAIL_STREAK_LIMIT):
        assert asyncio.run(ss.maybe_summarize(session)) is None
    assert ss.get_summary("s1") is None  # 失败不创建空摘要行
    # 第 4 次自动压缩被熔断拦截（不再调用 LLM），降级复用既有会话记忆
    assert asyncio.run(ss.maybe_summarize(session)) is None
    blocked = db.query_one(
        "SELECT * FROM context_compact_events WHERE session_id='s1' AND kind='auto_blocked'")
    assert blocked is not None
    # force 手动绕过熔断；成功后计数清零
    _patch_llm(monkeypatch)
    assert asyncio.run(ss.maybe_summarize(session, force=True)) is not None
    assert ss._fail_streak.get("s1") is None
    assert ss.get_summary("s1")["fail_streak"] == 0


def test_fail_streak_mirrors_to_existing_row(file_db, monkeypatch):
    _patch_llm(monkeypatch)
    monkeypatch.setattr(ss, "CONTEXT_COMPACT_TRIGGER_TOKENS", 10)
    session = _make_session()
    asyncio.run(ss.maybe_summarize(session))  # 先有摘要行
    _patch_llm_fail(monkeypatch)
    for _ in range(ss.COMPACT_FAIL_STREAK_LIMIT):
        asyncio.run(ss.maybe_summarize(session, force=True))
    assert ss.get_summary("s1")["fail_streak"] == ss.COMPACT_FAIL_STREAK_LIMIT


def test_recursion_guard(file_db, monkeypatch):
    _patch_llm(monkeypatch)
    session = _make_session()
    ss._compacting.add("s1")
    assert asyncio.run(ss.maybe_summarize(session, force=True)) is None
    ss._compacting.discard("s1")
    assert asyncio.run(ss.maybe_summarize(session, force=True)) is not None


# ============================================
# SessionMemoryCompact：SESSION.md 持久载体与恢复
# ============================================

def test_session_md_written_and_recovery(file_db, monkeypatch):
    _patch_llm(monkeypatch, reply="1.意图：文件恢复测试")
    monkeypatch.setattr(ss, "CONTEXT_COMPACT_TRIGGER_TOKENS", 10)
    session = _make_session()
    asyncio.run(ss.maybe_summarize(session))
    p = memory_store.session_memory_path("s1")
    assert p.is_file() and "文件恢复测试" in p.read_text(encoding="utf-8")
    # DB 摘要缺失 → 从 SESSION.md 恢复（文件优先）
    db.execute("DELETE FROM chat_session_summaries WHERE session_id='s1'")
    row = ss.get_summary("s1")
    assert row and row["restored_from_file"] is True
    assert row["summary"] == "1.意图：文件恢复测试"
    assert row["covered_until_message_id"] == "m3"
    ctx = ss.build_context_messages(session, max_recent=8)
    assert "【此前对话摘要】" in ctx[0]["content"] and len(ctx) == 1 + 6


def test_session_note_writes_session_md(file_db):
    now = datetime.now().isoformat()
    db.execute("INSERT INTO chat_sessions (session_id, user_id, title, created_at, updated_at)"
               " VALUES ('s1','u1','测试会话',?,?)", (now, now))
    ss.update_session_note("s1", "u1", "用户手写的会话笔记")
    rec = memory_store.read_session_memory("s1")
    assert rec["summary"] == "用户手写的会话笔记"
    assert rec["edited_by_user"] == "1" and rec["compact_kind"] == "note"


# ============================================
# microCompact：空闲衰减（读时投影）
# ============================================

def test_idle_decay_projection(file_db):
    session = _make_session(content_len=300)
    session.updated_at = (datetime.now() - timedelta(hours=2)).isoformat()
    ctx = ss.build_context_messages(session, max_recent=8)
    assert len(ctx) == 10
    # 最近 5 条全文保留；其余衰减到 CLIP_OLD_CHARS + 尾注
    for i in range(5):
        assert ctx[i]["content"].endswith(f"…（空闲衰减压缩，原长 300 字符）")
        assert len(ctx[i]["content"]) < 300
    for i in range(5, 10):
        assert len(ctx[i]["content"]) == 300
    ev = db.query_one("SELECT * FROM context_compact_events WHERE session_id='s1' AND kind='micro'")
    assert ev is not None
    # 原文不动：session.messages 未被修改（读时投影语义）
    assert len(session.messages[0].content) == 300


def test_active_session_no_decay(file_db):
    session = _make_session(content_len=300)
    session.updated_at = datetime.now().isoformat()
    ctx = ss.build_context_messages(session, max_recent=8)
    assert all(len(m["content"]) == 300 for m in ctx)


# ============================================
# 再水化：自动模式抑制追问
# ============================================

def test_auto_compact_suppresses_followup(file_db, monkeypatch):
    _patch_llm(monkeypatch)
    monkeypatch.setattr(ss, "CONTEXT_COMPACT_TRIGGER_TOKENS", 10)
    session = _make_session()
    asyncio.run(ss.maybe_summarize(session))
    ctx = ss.build_context_messages(session, max_recent=8)
    assert "避免不必要的追问确认" in ctx[0]["content"]
    # 手动压缩不附加抑制指令
    asyncio.run(ss.maybe_summarize(session, force=True))
    ctx = ss.build_context_messages(session, max_recent=8)
    assert "避免不必要的追问确认" not in ctx[0]["content"]