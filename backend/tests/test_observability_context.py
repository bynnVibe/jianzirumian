"""可观测性「当前用户上下文窗口 + 记忆模块概览」离线回归。

覆盖：MemoryService.usage_overview 的条数/分类型/画像卡/检索命中/字符与存储水位聚合、
游客与空数据兜底；obs.personal_context_overview 的上下文窗口分布、segment 构成占比、
历史压缩统计与记忆概览组合，以及用户隔离与非法用户拒绝。全部仅操作隔离 SQLite，不出网。
"""
import uuid
from datetime import datetime, timedelta

import pytest

from app.core import db
from app.core import observability as obs
from app.services.memory import MemoryService


def _uid():
    return f"u-{uuid.uuid4().hex[:8]}"


def _now():
    return datetime.now().isoformat()


def _add_memory(user_id, content, mtype="fact", importance=0.5, last_used=False):
    mid = str(uuid.uuid4())
    now = _now()
    db.execute(
        "INSERT INTO user_memories (id, user_id, memory_type, content, keywords,"
        " source_session_id, source_message_id, importance, created_at, updated_at, last_used_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (mid, user_id, mtype, content, "[]", "", "", importance, now, now,
         now if last_used else None),
    )
    return mid


def _add_trace(user_id, prompt_tokens, status="ok"):
    tid = str(uuid.uuid4())
    db.execute(
        "INSERT INTO agent_traces (id, agent, user_id, input, status, started_at,"
        " prompt_tokens, completion_tokens)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (tid, "chat", user_id, "hi", status, _now(), prompt_tokens, 10),
    )
    return tid


def _add_segment(trace_id, segment, tokens, chars):
    db.execute(
        "INSERT INTO agent_token_segments (id, trace_id, agent, segment, tokens, chars, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), trace_id, "chat", segment, tokens, chars, _now()),
    )


def _cleanup(user_id, trace_ids):
    db.execute("DELETE FROM user_memories WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM chat_session_summaries WHERE user_id = ?", (user_id,))
    for tid in trace_ids:
        db.execute("DELETE FROM agent_token_segments WHERE trace_id = ?", (tid,))
        db.execute("DELETE FROM agent_traces WHERE id = ?", (tid,))


# ============================================
# 记忆模块概览
# ============================================

def test_memory_usage_overview_aggregates():
    uid = _uid()
    try:
        _add_memory(uid, "用户偏好清淡饮食", "preference", 0.9, last_used=True)
        _add_memory(uid, "用户有高血压", "fact", 0.85)
        _add_memory(uid, "计划三个月减重", "goal", 0.4)
        ov = MemoryService.usage_overview(uid)
        assert ov["total"] == 3
        assert ov["storage_limit"] == 200
        # preference(0.9) 与 fact(0.85>=0.8) 均计入画像卡候选
        assert ov["profile_count"] == 2
        assert ov["retrieved_count"] == 1        # 仅偏好条被检索命中
        assert ov["never_used_count"] == 2
        by = {t["type"]: t["count"] for t in ov["by_type"]}
        assert by == {"preference": 1, "fact": 1, "goal": 1, "feedback": 0}
        assert ov["total_chars"] == len("用户偏好清淡饮食") + len("用户有高血压") + len("计划三个月减重")
        assert 0 < ov["storage_usage"] < 1
        assert ov["avg_importance"] == round((0.9 + 0.85 + 0.4) / 3, 3)
    finally:
        _cleanup(uid, [])


def test_memory_usage_overview_empty_and_guest():
    # 无记忆用户：结构完整、全零
    ov = MemoryService.usage_overview(_uid())
    assert ov["total"] == 0 and ov["profile_count"] == 0
    assert len(ov["by_type"]) == 4
    # 游客/系统：直接返回空概览，不查库
    assert MemoryService.usage_overview("guest")["total"] == 0
    assert MemoryService.usage_overview("")["total"] == 0


# ============================================
# 上下文窗口 + 记忆组合概览
# ============================================

def test_personal_context_overview_window_and_segments():
    uid = _uid()
    t1 = _add_trace(uid, 1000)
    t2 = _add_trace(uid, 3000)
    _add_segment(t1, "system", 200, 400)
    _add_segment(t1, "history", 800, 1600)
    _add_segment(t2, "knowledge", 2000, 4000)
    _add_memory(uid, "用户偏好清淡饮食", "preference", 0.9)
    db.execute(
        "INSERT INTO chat_session_summaries (session_id, user_id, summary,"
        " covered_until_message_id, token_estimate, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), uid, "摘要内容", "m1", 120, _now(), _now()),
    )
    try:
        data = obs.personal_context_overview(uid, 7)
        cw = data["context_window"]
        assert cw["requests"] == 2
        assert cw["avg_prompt_tokens"] == 2000.0     # (1000+3000)/2
        assert cw["peak_prompt_tokens"] == 3000
        assert cw["total_prompt_tokens"] == 4000
        assert cw["history_rounds"] >= 1
        # segment 构成按 token 降序：knowledge(2000) > history(800) > system(200)
        segs = {s["segment"]: s for s in cw["segments"]}
        assert cw["segments"][0]["segment"] == "knowledge"
        assert segs["knowledge"]["label"] == "知识上下文"
        assert segs["history"]["peak_tokens"] == 800
        total_seg = sum(s["tokens"] for s in cw["segments"])
        assert total_seg == 3000
        assert round(sum(s["share"] for s in cw["segments"]), 3) == pytest.approx(1.0, abs=0.01)
        # 压缩概览
        assert data["compression"]["sessions_with_summary"] == 1
        assert data["compression"]["summary_tokens"] == 120
        # 记忆概览已组合进来
        assert data["memory"]["total"] == 1
        assert data["days"] == 7
    finally:
        _cleanup(uid, [t1, t2])


def test_personal_context_overview_isolates_users_and_rejects_guest():
    uid = _uid()
    other = _uid()
    t1 = _add_trace(uid, 500)
    _add_segment(t1, "system", 100, 200)
    t2 = _add_trace(other, 9000)
    _add_segment(t2, "system", 9000, 9000)
    try:
        data = obs.personal_context_overview(uid, 7)
        # 只统计本人请求，不受其他用户高 token 影响
        assert data["context_window"]["requests"] == 1
        assert data["context_window"]["peak_prompt_tokens"] == 500
        assert data["context_window"]["total_prompt_tokens"] == 500
    finally:
        _cleanup(uid, [t1])
        _cleanup(other, [t2])
    with pytest.raises(ValueError):
        obs.personal_context_overview("guest", 7)


# ============================================
# 对话窗口上下文用量（聊天页缩略图数据源）
# ============================================

def _add_trace_with_session(user_id, session_id, prompt_tokens, started_at=None, status="ok"):
    tid = str(uuid.uuid4())
    db.execute(
        "INSERT INTO agent_traces (id, agent, user_id, input, status, started_at,"
        " prompt_tokens, completion_tokens, session_id)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (tid, "chat", user_id, "hi", status, started_at or _now(), prompt_tokens, 10, session_id),
    )
    return tid


def test_session_context_usage_latest_trace_and_shares():
    uid = _uid()
    sid = f"s-{uuid.uuid4().hex[:8]}"
    old = _add_trace_with_session(uid, sid, 500, started_at="2020-01-01T00:00:00")
    latest = _add_trace_with_session(uid, sid, 2000)
    other = _add_trace_with_session(uid, f"s-{uuid.uuid4().hex[:8]}", 9000)
    _add_segment(latest, "history", 1200, 2400)
    _add_segment(latest, "system", 400, 800)
    _add_segment(latest, "knowledge", 400, 800)
    _add_segment(old, "system", 500, 1000)
    try:
        data = obs.session_context_usage(sid)
        assert data["has_data"] is True
        # 取最近一条 trace 的真实 prompt_tokens，而非旧 trace/其他会话
        assert data["total_tokens"] == 2000
        assert data["segments"][0]["segment"] == "history"
        shares = {s["segment"]: s["share"] for s in data["segments"]}
        assert shares["history"] == 0.6 and shares["system"] == 0.2
        assert sum(s["tokens"] for s in data["segments"]) == 2000
        assert data["context_window"] > 0
        assert data["used_pct"] == round(2000 / data["context_window"] * 100, 1)
        assert data["trace_at"]
    finally:
        _cleanup(uid, [old, latest, other])


def test_session_context_usage_empty_and_running_excluded():
    uid = _uid()
    sid = f"s-{uuid.uuid4().hex[:8]}"
    running = _add_trace_with_session(uid, sid, 0, status="running")
    try:
        # 仅 running 的 trace 不计入：新会话展示空概览
        data = obs.session_context_usage(sid)
        assert data["has_data"] is False and data["segments"] == []
        assert data["total_tokens"] == 0 and data["used_pct"] == 0.0
        assert data["context_window"] > 0
        assert obs.session_context_usage("")["has_data"] is False
    finally:
        _cleanup(uid, [running])


# ============================================
# 按对话窗口下钻（观测性页子集切片）
# ============================================

def test_personal_session_contexts_groups_and_drills_down():
    uid = _uid()
    sid = f"s-{uuid.uuid4().hex[:8]}"
    other = f"s-{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT INTO chat_sessions (session_id, user_id, title, created_at, updated_at)"
        " VALUES (?,?,?,?,?)",
        (sid, uid, "血压饮食咨询", _now(), _now()),
    )
    t1 = _add_trace_with_session(uid, sid, 1000, started_at=(datetime.now() - timedelta(hours=1)).isoformat())
    t2 = _add_trace_with_session(uid, sid, 3000)
    t3 = _add_trace_with_session(uid, other, 700)
    # 未关联会话的历史存量 trace：不计入下钻列表
    t4 = _add_trace(uid, 999)
    _add_segment(t2, "knowledge", 1800, 3600)
    _add_segment(t2, "history", 1200, 2400)
    try:
        data = obs.personal_session_contexts(uid, 7, 20)
        by_sid = {s["session_id"]: s for s in data["sessions"]}
        assert set(by_sid) == {sid, other}
        s = by_sid[sid]
        assert s["title"] == "血压饮食咨询"
        assert s["requests"] == 2
        assert s["avg_prompt_tokens"] == 2000.0 and s["peak_prompt_tokens"] == 3000
        assert s["total_prompt_tokens"] == 4000
        # 展开即该会话最近一次请求的 segment 构成（与聊天页缩略图同口径）
        assert [x["segment"] for x in s["latest_segments"]] == ["knowledge", "history"]
        assert s["latest_segments"][0]["share"] == 0.6
        assert by_sid[other]["title"] == "新对话"
    finally:
        db.execute("DELETE FROM chat_sessions WHERE session_id = ?", (sid,))
        _cleanup(uid, [t1, t2, t3, t4])


def test_personal_session_contexts_rejects_guest():
    with pytest.raises(ValueError):
        obs.personal_session_contexts("guest", 7)
