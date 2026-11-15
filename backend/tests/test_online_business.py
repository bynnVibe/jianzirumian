"""在线业务三率回归测试（内存 DB，纯 SQL 确定性口径，不出网）。

覆盖计划第四节业务指标：
  B1 dislike_rate = 点踩数/助手回答数；无样本三率 None；
  B2 followup_rate 追问窗口边界（窗口内/恰好边界/窗口外/跨会话不算）；
  B3 empty_answer_rate：空 content / 关联 chat trace error / ok trace 不算 /
     跨会话 trace 不算 / 消息之后的 trace 不算；
  B4 窗口过滤（N 天前的消息不计入）；
  B5 告警规则 empty_answer_rate：越限触发 / 样本≥5 门槛 / None 不触发；
  B6 monitor_metrics.quality.online_business 回流。
"""
import sqlite3
from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.core import db
from app.services import online_eval as oe

NOW = datetime.now()


@pytest.fixture()
def mem_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    yield conn
    conn.close()


def _ts(offset_seconds=0):
    return (NOW + timedelta(seconds=offset_seconds)).isoformat()


def _assistant(session="s1", offset=0, content="回答"):
    mid = f"a-{session}-{offset}"
    db.execute(
        "INSERT INTO chat_messages (message_id, session_id, seq, role, content, created_at)"
        " VALUES (?,?,?,?,?,?)", (mid, session, 100 + offset, "assistant", content, _ts(offset)))
    return mid


def _user(session="s1", offset=0):
    db.execute(
        "INSERT INTO chat_messages (message_id, session_id, seq, role, content, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (f"u-{session}-{offset}", session, 100 + offset, "user", "追问", _ts(offset)))


def _trace(session="s1", offset=-10, status="ok", agent="chat"):
    tid = f"t-{session}-{offset}-{status}"
    db.execute(
        "INSERT INTO agent_traces (id, agent, session_id, status, started_at)"
        " VALUES (?,?,?,?,?)", (tid, agent, session, status, _ts(offset)))
    return tid


def _dislike(offset=0):
    db.execute(
        "INSERT INTO message_feedbacks (id, session_id, message_id, rating, created_at)"
        " VALUES (?,?,?,?,?)",
        (f"f-{offset}", "s1", f"a-s1-{offset}", "dislike", _ts(offset)))


# ============================================
# B1 点踩率
# ============================================
def test_dislike_rate_and_empty_window(mem_db):
    empty = oe.business_quality(7)
    assert empty["samples"] == 0
    assert empty["dislike_rate"] is None
    assert empty["followup_rate"] is None
    assert empty["empty_answer_rate"] is None
    _assistant(offset=0)
    _assistant(offset=10)
    _dislike(offset=10)
    biz = oe.business_quality(7)
    assert biz["samples"] == 2
    assert biz["dislikes"] == 1
    assert biz["dislike_rate"] == 0.5


# ============================================
# B2 追问窗口边界（默认 300s）
# ============================================
def test_followup_window_boundary(mem_db, monkeypatch):
    monkeypatch.setattr(settings, "FOLLOWUP_WINDOW_SECONDS", 300)
    _assistant(session="s1", offset=0)
    _user(session="s1", offset=299)          # 窗口内 → 追问
    _assistant(session="s2", offset=0)
    _user(session="s2", offset=300)          # 恰好边界（<=）→ 追问
    _assistant(session="s3", offset=0)
    _user(session="s3", offset=301)          # 窗口外 → 不算
    _assistant(session="s4", offset=0)
    _user(session="s5", offset=10)           # 跨会话 → 不算
    biz = oe.business_quality(7)
    assert biz["samples"] == 4
    assert biz["followups"] == 2
    assert biz["followup_rate"] == 0.5
    assert biz["followup_window_seconds"] == 300


def test_followup_only_after_answer(mem_db):
    _assistant(offset=100)
    _user(offset=50)                          # 回答之前的用户消息不算追问
    assert oe.business_quality(7)["followups"] == 0


# ============================================
# B3 空回答率（content 空 / 关联 trace error）
# ============================================
def test_empty_answer_content_and_trace_error(mem_db):
    _assistant(session="s1", offset=0, content="")        # 空 content → 计
    _assistant(session="s2", offset=0, content="回答")
    _trace(session="s2", offset=-10, status="error")      # 关联 error trace → 计
    _assistant(session="s3", offset=0, content="回答")
    _trace(session="s3", offset=-10, status="ok")         # ok trace → 不计
    _assistant(session="s4", offset=0, content="回答")
    _trace(session="s9", offset=-10, status="error")      # 跨会话 trace → 不计
    _assistant(session="s5", offset=0, content="回答")
    _trace(session="s5", offset=10, status="error")       # 消息之后的 trace → 不计
    biz = oe.business_quality(7)
    assert biz["samples"] == 5
    assert biz["empty_answers"] == 2
    assert biz["empty_answer_rate"] == 0.4


def test_empty_answer_latest_trace_wins(mem_db):
    """同会话多条 trace：取消息前最近一条（旧 error 被新 ok 覆盖 → 不计）。"""
    _assistant(session="s1", offset=0, content="回答")
    _trace(session="s1", offset=-100, status="error")
    _trace(session="s1", offset=-10, status="ok")
    assert oe.business_quality(7)["empty_answers"] == 0


# ============================================
# B4 窗口过滤
# ============================================
def test_window_excludes_old_messages(mem_db):
    old = (NOW - timedelta(days=10)).isoformat()
    db.execute(
        "INSERT INTO chat_messages (message_id, session_id, seq, role, content, created_at)"
        " VALUES (?,?,?,?,?,?)", ("old", "s1", 1, "assistant", "", old))
    _assistant(offset=0)
    biz = oe.business_quality(7)
    assert biz["samples"] == 1
    assert biz["empty_answers"] == 0


# ============================================
# B5 告警规则 empty_answer_rate
# ============================================
def _patch_sources(monkeypatch, biz):
    from app.core import observability as obs
    from app.services import online_eval
    monkeypatch.setattr(obs, "metrics", lambda *a, **k: {
        "overview": {"total": 0, "avg_tokens_per_request": 0},
        "golden_signals": {"error_rate": 0, "duration": {"p95_ms": 0}},
        "segment_breakdown": []})
    monkeypatch.setattr(obs, "monitor_metrics", lambda *a, **k: {
        "quality": {"likes": 0, "dislikes": 0, "eval_runs": [], "online_business": biz}})
    monkeypatch.setattr(online_eval, "aggregate", lambda *a, **k: {
        "samples": 0, "avg_geval": None})


def _rule(rules, metric):
    return next(r for r in rules if r["metric"] == metric)


def test_empty_answer_rate_rule(mem_db, monkeypatch):
    from app.services import alert_analysis as aa
    monkeypatch.setattr(settings, "ALERT_EMPTY_ANSWER_RATE_THRESHOLD", 0.10)
    # 样本 10、空回答率 0.2 > 0.10 → 触发（含证据）
    _patch_sources(monkeypatch, {"samples": 10, "empty_answer_rate": 0.2})
    r = _rule(aa._collect_rule_results(), "empty_answer_rate")
    assert r["triggered"] and r["type"] == "quality"
    assert r["threshold"] == 0.10
    assert "empty_samples" in r["evidence"]
    # 样本不足 5 → 不触发（小样本门槛）
    _patch_sources(monkeypatch, {"samples": 4, "empty_answer_rate": 0.5})
    assert not _rule(aa._collect_rule_results(), "empty_answer_rate")["triggered"]
    # 无样本（None）→ 不触发
    _patch_sources(monkeypatch, {"samples": 0, "empty_answer_rate": None})
    assert not _rule(aa._collect_rule_results(), "empty_answer_rate")["triggered"]
    # 恰好等于阈值 → 不触发（严格大于）
    _patch_sources(monkeypatch, {"samples": 10, "empty_answer_rate": 0.10})
    assert not _rule(aa._collect_rule_results(), "empty_answer_rate")["triggered"]


# ============================================
# B6 monitor_metrics 回流
# ============================================
def test_monitor_metrics_online_business(mem_db):
    from app.core import observability as obs
    _assistant(offset=0, content="")
    _assistant(offset=10)
    monitor = obs.monitor_metrics(7, "days")
    biz = monitor["quality"]["online_business"]
    assert biz["samples"] == 2
    assert biz["empty_answer_rate"] == 0.5