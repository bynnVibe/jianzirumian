"""记忆/上下文治理观测指标测试（需求4：五层写路径 + 三级压缩链监控重规划）。

覆盖：
  个人概览 compression 新指标（kind_counts/chain_events/tokens_saved/compress_ratio/
  fail_streak_max/breaker_sessions/idle_decay_minutes）与 memory.write_events 聚合；
  管理员 monitor_metrics 的 memory_context 全局块；
  管理员钻取白名单（_DRILL_TYPES）与 memory_events/compact_events 两个钻取分支。
"""
import re
import sqlite3
from datetime import datetime

import pytest

from app.core import db, observability as obs
from app.api import observability as api_obs
from app.services import memory_store


@pytest.fixture()
def file_db(tmp_path, monkeypatch):
    """临时文件数据库：markdown 持久层真实落盘（隔离目录内，不污染开发库）。"""
    conn = sqlite3.connect(str(tmp_path / "app.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    yield conn
    conn.close()


def _seed_events():
    """写路径 + 压缩链事件与摘要边界标记样例（u1 两条写事件、五类压缩事件）。"""
    memory_store.log_memory_event("u1", "add", "auto", memory_type="fact", detail="花生过敏")
    memory_store.log_memory_event("u1", "update", "user", detail="指令更新")
    memory_store.log_compact_event("s1", "u1", "micro", detail="空闲 90 分钟，衰减 3 条旧消息")
    memory_store.log_compact_event("s1", "u1", "auto", before_tokens=8000, after_tokens=1200)
    memory_store.log_compact_event("s2", "u1", "manual", before_tokens=6000, after_tokens=1000)
    memory_store.log_compact_event("s3", "u1", "auto_fail", detail="LLM 空摘要")
    memory_store.log_compact_event("s3", "u1", "auto_blocked", detail="连续失败熔断")
    now = datetime.now().isoformat()
    db.execute(
        "INSERT INTO chat_session_summaries (session_id, user_id, summary, covered_until_message_id,"
        " token_estimate, compact_kind, before_tokens, fail_streak, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("s1", "u1", "摘要A", "m9", 300, "auto", 8000, 0, now, now))
    db.execute(
        "INSERT INTO chat_session_summaries (session_id, user_id, summary, covered_until_message_id,"
        " token_estimate, compact_kind, before_tokens, fail_streak, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("s3", "u1", "摘要C", "m5", 200, "manual", 6000, 3, now, now))


# ============================================
# 个人概览：compression 新指标 + memory.write_events
# ============================================

def test_personal_compression_chain_metrics(file_db):
    _seed_events()
    ov = obs.personal_context_overview("u1")
    comp = ov["compression"]
    assert comp["kind_counts"] == {"auto": 1, "manual": 1, "note": 0}
    assert comp["chain_events"] == {
        "micro": 1, "auto": 1, "manual": 1, "auto_fail": 1, "auto_blocked": 1}
    # tokens_saved = Σ(before − after)（仅 auto/manual）= 6800 + 5000
    assert comp["tokens_saved"] == 11800
    assert comp["compress_ratio"] == round(1 - 2200 / 14000, 3)
    assert comp["fail_streak_max"] == 3
    assert comp["breaker_sessions"] == 1
    assert comp["idle_decay_minutes"] == 60
    # 既有口径保持
    assert comp["sessions_with_summary"] == 2
    assert comp["summary_tokens"] == 500


def test_personal_memory_write_events_and_layers(file_db):
    _seed_events()
    ov = obs.personal_context_overview("u1")
    we = ov["memory"]["write_events"]
    assert we["total"] == 2
    assert we["by_action"] == {"add": 1, "update": 1}
    assert we["by_layer"] == {"auto": 1, "user": 1}
    # 五层概览随个人面板下发（前端记忆视图数据源）
    layers = {l["layer"] for l in ov["memory"]["layers"]}
    assert layers == {"managed", "user", "project", "local", "auto"}


def test_personal_overview_empty_structure(file_db):
    ov = obs.personal_context_overview("nobody")
    comp = ov["compression"]
    assert comp["chain_events"] == {
        "micro": 0, "auto": 0, "manual": 0, "auto_fail": 0, "auto_blocked": 0}
    assert comp["tokens_saved"] == 0 and comp["compress_ratio"] == 0.0
    assert ov["memory"]["write_events"]["total"] == 0


# ============================================
# 管理员全局块 + 钻取
# ============================================

def test_admin_memory_context_block(file_db):
    _seed_events()
    m = obs.monitor_metrics(days=7)
    mc = m["memory_context"]
    assert mc["write_events_total"] == 2
    assert mc["write_by_action"] == {"add": 1, "update": 1}
    labels = {r["layer"]: r["label"] for r in mc["write_by_layer"]}
    assert labels["auto"] == memory_store.LAYER_LABELS["auto"]
    assert mc["compact_events"] == {
        "total": 5, "micro": 1, "auto": 1, "manual": 1, "fail": 1, "blocked": 1}
    assert mc["tokens_saved"] == 11800
    assert mc["breaker_sessions"] == 1


def test_drill_whitelist_covers_new_types():
    for t in ("memories", "memory_events", "compact_events"):
        assert re.fullmatch(f"^({api_obs._DRILL_TYPES})$", t), t


def test_drill_memory_events_branch(file_db):
    _seed_events()
    cols, rows, total = api_obs._drill_query("memory_events", 50, {"u1": "张三"})
    assert total == 2
    keys = {c["key"] for c in cols}
    assert {"action_label", "layer_label", "username"} <= keys
    by_action = {r["action_label"]: r for r in rows}
    assert by_action["新增"]["layer_label"] == memory_store.LAYER_LABELS["auto"]
    assert by_action["更新"]["username"] == "张三"


def test_drill_compact_events_branch(file_db):
    _seed_events()
    cols, rows, total = api_obs._drill_query("compact_events", 50, {"u1": "张三"})
    assert total == 5
    labels = {r["label"] for r in rows}
    assert labels == {"微压缩·空闲衰减", "自动压缩", "手动压缩", "自动压缩失败", "熔断拦截"}
    auto = next(r for r in rows if r["label"] == "自动压缩")
    assert auto["before_tokens"] == 8000 and auto["after_tokens"] == 1200