"""可观测性「当前用户上下文窗口 + 记忆模块概览」离线回归。

覆盖：MemoryService.usage_overview 的条数/分类型/画像卡/检索命中/字符与存储水位聚合、
游客与空数据兜底；obs.personal_context_overview 的上下文窗口分布、segment 构成占比、
历史压缩统计与记忆概览组合，以及用户隔离与非法用户拒绝。全部仅操作隔离 SQLite，不出网。
"""
import uuid
from datetime import datetime

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
