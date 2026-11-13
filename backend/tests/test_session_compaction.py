"""会话压缩升级回归测试（内存 DB，不出网）。

覆盖 C1/C2/C3/C5 四项改造（含前缀缓存整改阶段一）：
  C1  token headroom 触发与小会话豁免（maybe_summarize 阈值判定）；
  C2  微压缩两档冻结 clip（全文区/截断区边界、位置无关性）与摘要批更新门控；
  C3  结构化摘要契约（五节 checklist prompt、focus 侧重点、SUMMARY_MAX_CHARS）
      与 continuation 再水化包装、token_estimate 真实估算值；
  C5  压缩统计返回（force 强制触发时 covered_messages/before_chars/after_chars）。
"""
import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import db
from app.core.observability import estimate_tokens
from app.services import session_summary as ss


@pytest.fixture()
def mem_db(monkeypatch):
    """独立内存数据库：避免污染本地开发库"""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    yield conn
    conn.close()


class FakeLLM:
    """记录 prompt 并返回固定摘要的 LLM 桩。"""

    last_prompt = ""

    def __init__(self, reply: str = "1.用户意图：测试\n2.关键结论：无\n3.约束：无\n4.未决问题：无\n5.下一步：继续"):
        self.reply = reply

    async def chat(self, messages, stream=False, **kwargs):
        FakeLLM.last_prompt = messages[-1]["content"]
        yield self.reply


def _make_session(n_messages: int, content_len: int = 50, user_id: str = "u1"):
    msgs = [
        SimpleNamespace(
            message_id=f"m{i}",
            role="user" if i % 2 == 0 else "assistant",
            content=(f"消息{i}：" + "养" * max(0, content_len - 4)),
        )
        for i in range(n_messages)
    ]
    return SimpleNamespace(session_id="s1", user_id=user_id, messages=msgs)


def _patch_llm(monkeypatch, reply=None):
    llm = FakeLLM(reply) if reply else FakeLLM()
    monkeypatch.setattr("app.core.llm.LLMFactory.create_for_role", lambda *a, **k: llm)
    return llm


# ============================================
# C1 token headroom 触发与小会话豁免
# ============================================

def test_small_session_exempt(mem_db, monkeypatch):
    """消息数 < COMPACT_MIN_MESSAGES 的会话不压缩（即使字符量超阈值）。"""
    _patch_llm(monkeypatch)
    session = _make_session(ss.COMPACT_MIN_MESSAGES - 1, content_len=2000)
    assert asyncio.run(ss.maybe_summarize(session)) is None
    assert ss.get_summary("s1") is None


def test_token_threshold_triggers(mem_db, monkeypatch):
    """历史 token 估算超过 CONTEXT_COMPACT_TRIGGER_TOKENS 即触发压缩。"""
    _patch_llm(monkeypatch)
    monkeypatch.setattr(ss, "CONTEXT_COMPACT_TRIGGER_TOKENS", 100)
    session = _make_session(10, content_len=60)  # 10 条 × ~60 token > 100
    stats = asyncio.run(ss.maybe_summarize(session))
    assert stats and stats["covered_messages"] == 10 - ss.KEEP_RECENT_MESSAGES
    assert ss.get_summary("s1") is not None


def test_below_all_thresholds_no_compact(mem_db, monkeypatch):
    """token/条数/字符三个阈值都未超时不压缩。"""
    _patch_llm(monkeypatch)
    session = _make_session(9, content_len=20)
    assert asyncio.run(ss.maybe_summarize(session)) is None


def test_fallback_char_threshold_still_works(mem_db, monkeypatch):
    """token 阈值未超但字符兜底阈值超过时仍触发（保留既有语义）。"""
    _patch_llm(monkeypatch)
    monkeypatch.setattr(ss, "CONTEXT_COMPACT_TRIGGER_TOKENS", 10 ** 9)
    monkeypatch.setattr(ss, "SUMMARY_TRIGGER_CHAR_COUNT", 100)
    session = _make_session(10, content_len=60)
    assert asyncio.run(ss.maybe_summarize(session)) is not None


# ============================================
# C2 微压缩两档冻结 clip（前缀缓存友好）
# ============================================

def test_clip_two_tiers_frozen(mem_db, monkeypatch):
    """两档冻结：最近 keep_full 全文；其余统一 mid 截断 + 原长尾注（无深度压缩档）。"""
    monkeypatch.setattr(ss.settings, "KEEP_RECENT_FULL", 2)
    monkeypatch.setattr(ss.settings, "CLIP_MID_CHARS", 30)
    long_text = "字" * 100
    msgs = [{"role": "user", "content": long_text} for _ in range(6)]
    clipped = ss._clip_history(msgs)
    # 全文区：最近 2 条不截断
    assert clipped[4]["content"] == long_text and clipped[5]["content"] == long_text
    # 截断区：index 0..3 统一截断至 30 字 + 原长尾注（不再按位置分深浅档）
    expected = "字" * 30 + "\n…（较早消息已截断，原长 100 字符）"
    for i in range(4):
        assert clipped[i]["content"] == expected


def test_clip_position_independent(mem_db, monkeypatch):
    """位置无关：窗口追加新消息后，已冻结消息的渲染逐字节不变（前缀 append-only）。"""
    monkeypatch.setattr(ss.settings, "KEEP_RECENT_FULL", 2)
    monkeypatch.setattr(ss.settings, "CLIP_MID_CHARS", 30)
    long_text = "字" * 100
    base = [{"role": "user", "content": long_text} for _ in range(6)]
    clipped1 = ss._clip_history(base)
    extended = base + [{"role": "user", "content": "新问"},
                       {"role": "assistant", "content": "新答"}]
    clipped2 = ss._clip_history(extended)
    # 冻结区（全文区之外）渲染不变；原全文区 index 4..5 退出全文区时一次性截断迁移
    for i in range(4):
        assert clipped1[i] == clipped2[i]
    assert clipped2[4]["content"].startswith("字" * 30)
    assert clipped2[6]["content"] == "新问" and clipped2[7]["content"] == "新答"


def test_clip_system_and_short_messages_untouched(mem_db, monkeypatch):
    """system 消息与不足上限的短消息不截断。"""
    monkeypatch.setattr(ss.settings, "KEEP_RECENT_FULL", 2)
    monkeypatch.setattr(ss.settings, "CLIP_MID_CHARS", 30)
    msgs = [
        {"role": "system", "content": "摘" * 100},
        {"role": "user", "content": "短消息"},
        {"role": "assistant", "content": "短回复"},
        {"role": "user", "content": "问"},
        {"role": "assistant", "content": "答"},
    ]
    clipped = ss._clip_history(msgs)
    assert clipped[0]["content"] == "摘" * 100
    assert clipped[1]["content"] == "短消息"


# ============================================
# C3 结构化摘要契约 + continuation 包装
# ============================================

def test_summary_prompt_structured_sections(mem_db, monkeypatch):
    """prompt 含五节 checklist、字数上限与增量已有摘要。"""
    _patch_llm(monkeypatch)
    monkeypatch.setattr(ss, "CONTEXT_COMPACT_TRIGGER_TOKENS", 10)
    session = _make_session(10, content_len=40)
    asyncio.run(ss.maybe_summarize(session))
    prompt = FakeLLM.last_prompt
    for frag in ("1.用户意图与当前任务", "2.已给出的关键结论/建议", "3.用户约束与偏好",
                 "4.未决问题", "5.下一步"):
        assert frag in prompt
    assert str(ss.settings.SUMMARY_MAX_CHARS) in prompt
    # 第二次压缩：prior 摘要作为【已有摘要】增量输入
    asyncio.run(ss.maybe_summarize(session, force=True))
    assert "【已有摘要】" in FakeLLM.last_prompt


def test_summary_prompt_focus_hint(mem_db, monkeypatch):
    """手动压缩的 focus 侧重点拼入 prompt。"""
    _patch_llm(monkeypatch)
    session = _make_session(10, content_len=40)
    asyncio.run(ss.maybe_summarize(session, force=True, focus="保留饮食禁忌相关内容"))
    assert "【压缩侧重点】保留饮食禁忌相关内容" in FakeLLM.last_prompt


def test_token_estimate_is_real_estimate(mem_db, monkeypatch):
    """落库 token_estimate 为真实 token 估算值，而非字符数。"""
    summary_text = "1.意图：养生咨询\n2.结论：无"
    _patch_llm(monkeypatch, reply=summary_text)
    monkeypatch.setattr(ss, "CONTEXT_COMPACT_TRIGGER_TOKENS", 10)
    session = _make_session(10, content_len=40)
    asyncio.run(ss.maybe_summarize(session))
    row = ss.get_summary("s1")
    assert row["token_estimate"] == estimate_tokens(summary_text)


def test_build_context_continuation_wrapper(mem_db, monkeypatch):
    """有摘要时注入「摘要 + continuation 指令」system 消息，tail 为覆盖点之后消息。"""
    _patch_llm(monkeypatch, reply="1.意图：测试摘要")
    monkeypatch.setattr(ss, "CONTEXT_COMPACT_TRIGGER_TOKENS", 10)
    session = _make_session(10, content_len=40)
    asyncio.run(ss.maybe_summarize(session))
    ctx = ss.build_context_messages(session, max_recent=8)
    assert ctx[0]["role"] == "system"
    assert "【此前对话摘要】" in ctx[0]["content"]
    assert "已知信息不要重复询问" in ctx[0]["content"]
    # covered_until = m3（10-6），tail 从 m4 起
    assert len(ctx) == 1 + 6
    assert ctx[1]["content"].startswith("消息4")


# ============================================
# C5 压缩统计返回
# ============================================

def test_force_compact_returns_stats(mem_db, monkeypatch):
    """force 强制触发无视阈值，返回 covered_messages/before_chars/after_chars。"""
    reply = "1.意图：手动压缩结果"
    _patch_llm(monkeypatch, reply=reply)
    session = _make_session(10, content_len=40)
    stats = asyncio.run(ss.maybe_summarize(session, force=True))
    assert stats == {
        "covered_messages": 4,
        "before_chars": sum(len(m.content) for m in session.messages[:4]),
        "after_chars": len(reply),
    }


def test_force_compact_too_few_messages_returns_none(mem_db, monkeypatch):
    """不足 KEEP_RECENT_MESSAGES 条时无可压缩区间，force 也返回 None。"""
    _patch_llm(monkeypatch)
    session = _make_session(ss.KEEP_RECENT_MESSAGES)
    assert asyncio.run(ss.maybe_summarize(session, force=True)) is None


def test_llm_failure_degrades_silently(mem_db, monkeypatch):
    """LLM 抛异常时静默降级（返回 None，不落摘要，不抛异常）。"""
    def _boom(*a, **k):
        raise RuntimeError("上游不可用")

    monkeypatch.setattr("app.core.llm.LLMFactory.create_for_role", _boom)
    monkeypatch.setattr(ss, "CONTEXT_COMPACT_TRIGGER_TOKENS", 10)
    session = _make_session(10, content_len=40)
    assert asyncio.run(ss.maybe_summarize(session)) is None
    assert ss.get_summary("s1") is None


# ============================================
# 前缀缓存整改阶段一：摘要批更新门控 + 无摘要冻结窗口
# ============================================

def _append_messages(session, n):
    """追加 n 条新消息（message_id 接续，避免与存量 id 冲突）。"""
    start = len(session.messages)
    for i in range(start, start + n):
        session.messages.append(SimpleNamespace(
            message_id=f"m{i}", role="user" if i % 2 == 0 else "assistant",
            content=f"消息{i}：" + "养" * 36,
        ))


def test_summary_batch_gate_skips_small_delta(mem_db, monkeypatch):
    """首次摘要后新增可摘要消息不足 N 条 → 不重算（摘要前缀稳定）。"""
    _patch_llm(monkeypatch)
    monkeypatch.setattr(ss, "CONTEXT_COMPACT_TRIGGER_TOKENS", 10)
    session = _make_session(10, content_len=40)
    assert asyncio.run(ss.maybe_summarize(session)) is not None
    first = ss.get_summary("s1")
    # 覆盖点 m3；+2 条 → 新增可摘要 2 条 < 6 → 门控拦截
    _append_messages(session, 2)
    assert asyncio.run(ss.maybe_summarize(session)) is None
    assert ss.get_summary("s1")["updated_at"] == first["updated_at"]
    # 再 +4 条 → 新增可摘要 6 条 ≥ 6 → 重算，覆盖点推进到 m9
    _append_messages(session, 4)
    stats = asyncio.run(ss.maybe_summarize(session))
    assert stats and stats["covered_messages"] == 10
    assert ss.get_summary("s1")["covered_until_message_id"] == "m9"


def test_summary_batch_gate_force_bypass(mem_db, monkeypatch):
    """force=True（手动 compact）绕过批门控，零新增也重算。"""
    _patch_llm(monkeypatch)
    monkeypatch.setattr(ss, "CONTEXT_COMPACT_TRIGGER_TOKENS", 10)
    session = _make_session(10, content_len=40)
    assert asyncio.run(ss.maybe_summarize(session)) is not None
    assert asyncio.run(ss.maybe_summarize(session, force=True)) is not None


def test_no_summary_full_freeze_window(mem_db, monkeypatch):
    """无摘要时取全量冻结窗口：不再按 max_recent 滑动截断，未达上限前前缀 append-only。"""
    session = _make_session(10, content_len=40)
    ctx1 = ss.build_context_messages(session, max_recent=3)
    assert len(ctx1) == 10
    _append_messages(session, 2)
    ctx2 = ss.build_context_messages(session, max_recent=3)
    assert len(ctx2) == 12
    keep_full = int(ss.settings.KEEP_RECENT_FULL)
    for i in range(len(ctx1) - keep_full):
        assert ctx1[i] == ctx2[i]


def test_no_summary_window_cap(mem_db, monkeypatch):
    """超过冻结窗口上限时取最近 cap 条（固定容量，控制 Token）。"""
    session = _make_session(ss.FREEZE_MAX_MESSAGES + 4, content_len=20)
    ctx = ss.build_context_messages(session, max_recent=3)
    assert len(ctx) == ss.FREEZE_MAX_MESSAGES
    assert ctx[0]["content"].startswith("消息4")


# ============================================
# C4 手动 compact API（强制触发与统计返回）
# ============================================

def test_compact_api_forces_and_returns_stats(mem_db, monkeypatch):
    """POST /api/chat/sessions/{id}/compact：无视阈值强制压缩，返回统计与 focus 透传。"""
    from fastapi import HTTPException

    from app.api.chat import compact_session
    from app.services.chat import Message, chat_service

    _patch_llm(monkeypatch, reply="1.意图：手动压缩\n2.结论：无")
    session = chat_service.create_session(user_id="u1")
    for i in range(10):
        session.messages.append(Message(
            role="user" if i % 2 == 0 else "assistant",
            content=f"消息{i}：" + "养" * 36,
        ))
    chat_service._persist_session(session)

    class _Req:
        async def json(self):
            return {"focus": "保留饮食相关内容"}

    res = asyncio.run(compact_session(session.session_id, _Req(), user={"id": "u1"}))
    assert res["success"] is True
    assert res["covered_messages"] == 4
    assert res["before_chars"] > res["after_chars"]
    assert "【压缩侧重点】保留饮食相关内容" in FakeLLM.last_prompt
    assert ss.get_summary(session.session_id) is not None


def test_compact_api_rejects_foreign_or_short_session(mem_db, monkeypatch):
    """非本人会话 404；消息不足时 400。"""
    from fastapi import HTTPException

    from app.api.chat import compact_session
    from app.services.chat import chat_service

    _patch_llm(monkeypatch)
    session = chat_service.create_session(user_id="u1")

    class _Req:
        async def json(self):
            return {}

    with pytest.raises(HTTPException) as ei:
        asyncio.run(compact_session(session.session_id, _Req(), user={"id": "other"}))
    assert ei.value.status_code == 404
    with pytest.raises(HTTPException) as ei:
        asyncio.run(compact_session(session.session_id, _Req(), user={"id": "u1"}))
    assert ei.value.status_code == 400
