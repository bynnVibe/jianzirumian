"""语义缓存监控回归测试（内存 DB，不出网）。

覆盖计划第六节：
  C1 事件埋点 record_event：hit/miss 字段口径、问题截断、失败静默；
  C2 miss 事件按真实 usage 补记 est_tokens（update_event_tokens）；
  C3 monitor_stats 聚合：命中率/命中 vs 未命中对比/节省时间/节省 Token/空间/每日趋势；
  C4 缓存语义详情返回前脱敏，原始落库不改写；
  C5 proxy._cache_lookup 埋点接线：命中记 hit=1+saved、未命中记 hit=0 并留事件 id、
     关闭缓存不记事件、lookup 异常静默走上游。
"""
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
import time
from types import SimpleNamespace

import pytest

from app.core import db
from app.services.gateway import semantic_cache as sc
from app.services import cache_policy


@pytest.fixture()
def mem_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    monkeypatch.setattr(sc.settings, "ANSWER_CACHE_SIGNING_KEY", "isolated-monitor-signing-key-32-chars")
    monkeypatch.setattr(sc, "semantic_cache_enabled", lambda: True)
    yield conn
    conn.close()


def _gw_log(log_id, latency, tokens, status="ok", key_name="k", key_id="kid"):
    db.execute(
        "INSERT INTO gateway_logs (id, key_id, key_name, status, latency_ms,"
        " prompt_tokens, completion_tokens, total_tokens, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (log_id, key_id, key_name, status, latency, tokens // 2, tokens - tokens // 2,
         tokens, datetime.now().isoformat()))


def _cache_entry(entry_id, user_id, question="黄芪功效", answer="补气固表",
                 expires_at="", hit_count=1):
    now = datetime.now().isoformat()
    db.execute(
        "INSERT INTO semantic_cache_v2 (id, user_id, question, question_norm, answer,"
        " sources, embedding, hit_count, scope, expires_at, created_at, last_used_at,"
        " cache_kind,fingerprint,evidence_hash,revisions,model,upstream_id)"
        " VALUES (?,?,?,?,?,'[]','[0.1]',?,'personal',?,?,?,'model_exact',?,'evidence','{}','model','up')",
        (entry_id, user_id, question, question, answer, hit_count,
         expires_at or (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(), now, now, entry_id))


# ============================================
# C1 事件埋点
# ============================================
def test_record_event_hit_and_miss_fields(mem_db):
    hit_id = sc.record_event(user_id="u1", scope="public", hit=True, entry_id="e1",
                             score=0.93, latency_ms=40, est_tokens=120,
                             saved_tokens=120, question="黄芪功效")
    miss_id = sc.record_event(user_id="u1", hit=False, latency_ms=15, question=" misses ")
    assert hit_id and miss_id
    hit = db.query_one("SELECT * FROM semantic_cache_events WHERE id = ?", (hit_id,))
    assert hit["hit"] == 1 and hit["entry_id"] == "e1" and hit["score"] == 0.93
    assert hit["est_tokens"] == 120 and hit["saved_tokens"] == 120
    miss = db.query_one("SELECT * FROM semantic_cache_events WHERE id = ?", (miss_id,))
    assert miss["hit"] == 0 and miss["saved_tokens"] == 0 and miss["scope"] == "personal"


def test_record_event_question_truncated_and_failure_silent(mem_db):
    long_q = "问" * 300
    eid = sc.record_event(hit=False, question=long_q)
    row = db.query_one("SELECT question FROM semantic_cache_events WHERE id = ?", (eid,))
    assert len(row["question"]) == 120
    # 表被破坏时静默返回 None，不抛异常（不阻断主链路）
    db.execute("DROP TABLE semantic_cache_events")
    assert sc.record_event(hit=True) is None


# ============================================
# C2 miss 补记 usage
# ============================================
def test_update_event_tokens(mem_db):
    eid = sc.record_event(hit=False, question="q")
    sc.update_event_tokens(eid, 888)
    row = db.query_one("SELECT est_tokens FROM semantic_cache_events WHERE id = ?", (eid,))
    assert row["est_tokens"] == 888
    sc.update_event_tokens("", 1)  # 空 id 无操作不报错
    sc.update_event_tokens("missing", 1)  # 不存在也不报错


# ============================================
# C3 聚合口径
# ============================================
def test_monitor_stats_aggregation(mem_db):
    # 2 命中（时延 50/150，各节省 100 Token）+ 2 未命中事件
    sc.record_event(hit=True, latency_ms=50, saved_tokens=100, est_tokens=100)
    sc.record_event(hit=True, latency_ms=150, saved_tokens=100, est_tokens=100)
    sc.record_event(hit=False, latency_ms=10)
    sc.record_event(hit=False, latency_ms=20)
    # 未命中对照：真实出网成功请求（total_tokens>0）；缓存合成响应（0 Token）排除
    _gw_log("g1", latency=1000, tokens=500)
    _gw_log("g2", latency=2000, tokens=700)
    _gw_log("g3", latency=5, tokens=0)
    _gw_log("g4", latency=9999, tokens=999, status="error")
    stats = sc.monitor_stats(7)
    ov = stats["overview"]
    assert ov["requests"] == 4 and ov["hits"] == 2 and ov["misses"] == 2
    assert ov["hit_rate"] == 0.5
    assert ov["saved_tokens"] == 200
    assert ov["saved_time_ms"] == int(2 * (1500 - 100))  # Σ(未命中均值−命中时延)
    cmp_ = stats["comparison"]
    assert cmp_["hit_avg_latency_ms"] == 100.0
    assert cmp_["miss_avg_latency_ms"] == 1500.0
    assert cmp_["miss_avg_tokens"] == 600.0 and cmp_["miss_samples"] == 2
    assert cmp_["hit_tokens"] == 0
    assert len(stats["daily"]) == 1
    assert stats["daily"][0]["hits"] == 2 and stats["daily"][0]["hit_rate"] == 0.5
    sc.record_event(hit=False, outcome="bypass", reason="force_refresh")
    sc.record_event(hit=False, outcome="invalidated", reason="expired")
    revised = sc.monitor_stats(7)
    assert revised["overview"]["requests"] == 4
    assert revised["overview"]["hit_rate"] == 0.5
    assert revised["overview"]["bypass_rate"] == 0.2
    assert revised["overview"]["savings_are_estimates"] is True


def test_monitor_stats_space_and_entries(mem_db):
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    _cache_entry("e1", "u2")
    _cache_entry("e2", "u1")
    _cache_entry("e3", "u1", question="过期条目问题", expires_at=past)  # 已过期
    stats = sc.monitor_stats(7)
    space = stats["space"]
    assert space["entries"] == 3
    assert space["public_entries"] == 0 and space["personal_entries"] == 3
    assert space["expired_entries"] == 1
    assert space["bytes"] > 0 and space["mb"] == round(space["bytes"] / 1048576, 3)
    assert len(stats["entries"]) == 3
    assert all("ttl_label" in e for e in stats["entries"])


def test_monitor_stats_empty(mem_db):
    stats = sc.monitor_stats(7)
    assert stats["overview"]["requests"] == 0
    assert stats["overview"]["hit_rate"] == 0.0
    assert stats["comparison"]["miss_samples"] == 0
    assert stats["space"]["entries"] == 0 and stats["daily"] == []


# ============================================
# C4 语义详情脱敏
# ============================================
def test_monitor_stats_entries_redacted(mem_db):
    _cache_entry("e1", "u1", question="手机号13812345678的问题",
                 answer="答案含13812345678")
    stats = sc.monitor_stats(7)
    entry = stats["entries"][0]
    assert "13812345678" not in entry["question"]
    assert "13812345678" not in entry["answer"]
    raw = db.query_one("SELECT question FROM semantic_cache_v2 WHERE id = 'e1'")
    assert "13812345678" in raw["question"]  # 原始落库不改写


# ============================================
# C5 proxy._cache_lookup 埋点接线
# ============================================
def _ctx(enabled=True):
    db.execute("INSERT INTO users(id,username,password_hash,salt,created_at,updated_at,is_active) "
               "VALUES ('u1','monitor','unused','salt',0,0,1)")
    messages = [{"role": "user", "content": "黄芪有什么功效"}]
    body = {"messages": messages, "model": "model"}
    body["_gw_cache_contract"] = cache_policy.sign_contract(
        user_id="u1", query=messages[0]["content"], messages=messages, model="model",
        decision=cache_policy.decide(messages[0]["content"], freshness="static"),
        sources=[], revision=cache_policy.revisions())
    return SimpleNamespace(
        cache_enabled=enabled, user_id="u1", cache_query=messages[0]["content"],
        forward_body=body, model_req="model", upstream={}, rule={}, deadline=time.monotonic() + 20,
        cache_bypass_recorded=False, cache_scope="personal", cache_realtime=False, cache_event_id="",
        tracer=SimpleNamespace(add=lambda *a, **k: None))


def test_cache_lookup_hit_records_saved_tokens(mem_db, monkeypatch):
    from app.services.gateway import proxy

    async def fake_lookup(user_id, question, **kw):
        return {"id": "e1", "answer": "黄芪补气固表，适合气虚人群日常调理。",
                "scope": "personal", "score": 0.95, "ttl_label": "硬期限 24 小时（命中不续期）"}
    monkeypatch.setattr(sc, "lookup", fake_lookup)
    ctx = _ctx()
    answer = asyncio.run(proxy._cache_lookup(ctx))
    assert answer, getattr(ctx, "cache_info", {})
    assert answer.startswith("黄芪补气固表")
    row = db.query_one("SELECT * FROM semantic_cache_events")
    assert row["hit"] == 1 and row["entry_id"] == "e1" and row["score"] == 0.95
    assert row["est_tokens"] > 0 and row["saved_tokens"] == row["est_tokens"]
    assert ctx.cache_event_id == ""  # 命中无需补记


def test_cache_lookup_miss_records_event_id(mem_db, monkeypatch):
    from app.services.gateway import proxy

    async def fake_lookup(user_id, question, **kw):
        return None
    monkeypatch.setattr(sc, "lookup", fake_lookup)
    ctx = _ctx()
    assert asyncio.run(proxy._cache_lookup(ctx)) is None
    row = db.query_one("SELECT * FROM semantic_cache_events")
    assert row["hit"] == 0 and row["saved_tokens"] == 0
    assert row["outcome"] == "miss"
    assert ctx.cache_event_id == row["id"]  # 供上游成功后补记 usage
    sc.update_event_tokens(ctx.cache_event_id, 666)
    assert db.query_one("SELECT est_tokens FROM semantic_cache_events")["est_tokens"] == 666


def test_cache_lookup_disabled_records_nothing(mem_db, monkeypatch):
    from app.services.gateway import proxy
    called = []

    async def fake_lookup(*a, **k):
        called.append(1)
        return None
    monkeypatch.setattr(sc, "lookup", fake_lookup)
    assert asyncio.run(proxy._cache_lookup(_ctx(enabled=False))) is None
    assert called == []
    assert db.query_one("SELECT COUNT(*) AS n FROM semantic_cache_events")["n"] == 0


def test_cache_lookup_exception_silent(mem_db, monkeypatch):
    from app.services.gateway import proxy

    async def boom(*a, **k):
        raise RuntimeError("缓存后端不可用")
    monkeypatch.setattr(sc, "lookup", boom)
    assert asyncio.run(proxy._cache_lookup(_ctx())) is None  # 静默走上游
    assert db.query_one("SELECT COUNT(*) AS n FROM semantic_cache_events")["n"] == 0