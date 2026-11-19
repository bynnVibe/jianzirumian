"""主备路由兜底/重试收敛/停止传播/生成超时告警 回归（内存 DB，不出网）。

覆盖本次整改：
  F1 primary_backup 切换后显式模型仅主路由支持 → 解析备路由兜底模型（健康/备支持原模型/非主备策略不改写）；
  F2 网关 502/503/504 包裹上游 429 不再误判同模型限流（重试收敛）；
  F3 stop 端点把取消传播到后端会话任务；
  F4 整体生成超预算 → 停止会话所有其他生成任务 + 事件告警（按请求去重）。
"""
import asyncio
import json
import sqlite3

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


def _up(conn, uid, models, fail_count=0, health="healthy"):
    conn.execute(
        "INSERT INTO gateway_upstreams (id,name,protocol,base_url,api_key,remark,"
        "created_at,updated_at,enabled,models,fail_count,health,category,weight)"
        " VALUES (?,?,?,?,?,?,?,?,1,?,?,?, 'llm', 100)",
        (uid, uid, "openai", "http://x", "k", "", "now", "now",
         json.dumps(models), fail_count, health))


def _rule_row(primary="a", threshold=3, strategy="primary_backup"):
    return {"lb_strategy": strategy, "primary_upstream": primary,
            "failover_threshold": threshold, "upstreams": json.dumps(["a", "b"])}


# ============================================
# F1 主备模型兜底
# ============================================
def test_fallback_model_when_primary_down_and_model_only_primary(mem_db, monkeypatch):
    from app.services.gateway import rules as rule_svc
    from app.services.gateway import settings as settings_svc
    monkeypatch.setattr(settings_svc, "get_settings", lambda: {"default_model": ""})
    _up(mem_db, "a", ["m-a"], fail_count=5, health="circuit_open")
    _up(mem_db, "b", ["m-b"])
    assert rule_svc.primary_backup_fallback_model(_rule_row(), "m-a") == "m-b"


def test_fallback_prefers_gateway_default_model(mem_db, monkeypatch):
    from app.services.gateway import rules as rule_svc
    from app.services.gateway import settings as settings_svc
    monkeypatch.setattr(settings_svc, "get_settings", lambda: {"default_model": "m-b"})
    _up(mem_db, "a", ["m-a"], fail_count=3)
    _up(mem_db, "b", ["m-b", "m-c"])
    assert rule_svc.primary_backup_fallback_model(_rule_row(), "m-a") == "m-b"


def test_no_fallback_when_primary_healthy(mem_db, monkeypatch):
    from app.services.gateway import rules as rule_svc
    from app.services.gateway import settings as settings_svc
    monkeypatch.setattr(settings_svc, "get_settings", lambda: {})
    _up(mem_db, "a", ["m-a"], fail_count=2)
    _up(mem_db, "b", ["m-b"])
    assert rule_svc.primary_backup_fallback_model(_rule_row(), "m-a") is None


def test_no_fallback_when_backup_supports_original_model(mem_db, monkeypatch):
    from app.services.gateway import rules as rule_svc
    from app.services.gateway import settings as settings_svc
    monkeypatch.setattr(settings_svc, "get_settings", lambda: {})
    _up(mem_db, "a", ["m-a"], fail_count=9, health="circuit_open")
    _up(mem_db, "b", ["m-a", "m-b"])
    assert rule_svc.primary_backup_fallback_model(_rule_row(), "m-a") is None


def test_no_fallback_for_other_strategy(mem_db):
    from app.services.gateway import rules as rule_svc
    _up(mem_db, "a", ["m-a"], fail_count=9, health="circuit_open")
    _up(mem_db, "b", ["m-b"])
    assert rule_svc.primary_backup_fallback_model(
        _rule_row(strategy="weighted"), "m-a") is None


def test_fallback_accepts_materialized_rule_dict(mem_db, monkeypatch):
    """proxy 传入的是 _row_to_dict 产物（upstreams 已为 list），不得因解析差异失效。"""
    from app.services.gateway import rules as rule_svc
    from app.services.gateway import settings as settings_svc
    monkeypatch.setattr(settings_svc, "get_settings", lambda: {})
    _up(mem_db, "a", ["m-a"], fail_count=5, health="circuit_open")
    _up(mem_db, "b", ["m-b"])
    materialized = rule_svc._row_to_dict({**_rule_row(), "id": "r1"})
    assert isinstance(materialized["upstreams"], list)
    assert rule_svc.primary_backup_fallback_model(materialized, "m-a") == "m-b"


# ============================================
# F2 重试收敛
# ============================================
class _FakeStatusError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


def test_gateway_502_wrapping_429_is_not_rate_limit():
    from app.core.openrouter import is_rate_limit_error
    wrapped = _FakeStatusError(
        502, "Error code: 502 - {'error': {'message': '所有上游均调用失败: 上游返回 429: "
             "{\"error\":{\"message\":\"You’ve reached the API rate limit for free users.\"}}'}}")
    assert not is_rate_limit_error(wrapped)
    assert not is_rate_limit_error(_FakeStatusError(503, "上游返回 503"))
    assert not is_rate_limit_error(Exception("所有上游均调用失败: 上游返回 429 rate limit"))


def test_true_429_still_rate_limit():
    from app.core.openrouter import is_rate_limit_error
    assert is_rate_limit_error(_FakeStatusError(429, "rate limit exceeded"))
    assert is_rate_limit_error(Exception("429 too many requests"))


# ============================================
# F3 stop 端点传播取消
# ============================================
def test_stop_endpoint_cancels_session_tasks(mem_db):
    import httpx
    from app.api import chat as chat_api, deps
    from app.services.chat import chat_service
    app = FastAPI()
    app.include_router(chat_api.router)
    app.dependency_overrides[deps.get_optional_user] = lambda: {"id": "alice"}
    session = chat_service.create_session(user_id="alice")

    async def run():
        bg = asyncio.create_task(asyncio.sleep(30))
        chat_service._active_tasks.setdefault(session.session_id, set()).add(bg)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(f"/api/chat/stop/{session.session_id}")
            assert r.status_code == 200 and r.json()["ok"] is True
            assert (await client.post("/api/chat/stop/missing")).status_code == 404
        await asyncio.sleep(0.05)
        assert bg.cancelled()
    asyncio.run(run())


# ============================================
# F4 生成超时兜底 + 事件告警
# ============================================
def test_generation_timeout_alert_dedup_and_label(mem_db):
    from app.services import alert_analysis as aa
    first = aa.record_generation_timeout("s1", "alice", "m1", 300.0, "失眠要怎么治理")
    assert first
    assert aa.record_generation_timeout("s1", "alice", "m1", 300.0, "失眠要怎么治理") is None
    assert aa.record_generation_timeout("s1", "alice", "m2", 300.0, "另一问")
    row = db.query_one("SELECT * FROM obs_alerts WHERE id=?", (first,))
    assert row["type"] == "event" and row["metric"] == "generation_timeout"
    assert row["status"] == "open" and row["severity"] == "high"
    assert row["window"] == "event"
    listed = aa.list_alerts(status="open")
    item = next(a for a in listed["alerts"] if a["id"] == first)
    assert item["metric_label"] == "生成整体超时"
    assert item["analysis"]["cause"] and item["analysis"]["suggestions"]


def test_chat_timeout_stops_session_tasks_and_alerts(mem_db, monkeypatch):
    from app.config import settings
    from app.services.chat import chat_service
    session = chat_service.create_session(user_id="alice")

    async def inner(*args):
        yield 'data: {"event":"token","content":"partial"}\n\n'
        await asyncio.sleep(5)
    monkeypatch.setattr(chat_service, "_chat_stream_inner", inner)
    monkeypatch.setattr(settings, "CHAT_REQUEST_TIMEOUT", 0.05)

    async def run():
        bg = asyncio.create_task(asyncio.sleep(30))
        chat_service._active_tasks.setdefault(session.session_id, set()).add(bg)
        events = [e async for e in chat_service.chat_stream(session.session_id, "q", user_id="alice")]
        assert "request_timeout" in events[-1]
        await asyncio.sleep(0.05)
        assert bg.cancelled()  # 会话注册的其他生成任务被兜底停止
    asyncio.run(run())
    row = db.query_one("SELECT * FROM obs_alerts WHERE metric='generation_timeout'")
    assert row and row["type"] == "event" and row["status"] == "open"
    assert db.query_one("SELECT status FROM agent_traces")["status"] == "timeout"


# ============================================
# F5 整改3：备升主持久化 + 可观测显性化
# ============================================
def _insert_rule(conn, rid="r1", primary="a", threshold=3):
    conn.execute(
        "INSERT INTO gateway_rules (id, model_pattern, upstreams, lb_strategy, primary_upstream,"
        " failover_threshold, enabled, created_at, updated_at) VALUES (?,?,?,?,?,?,1,'now','now')",
        (rid, "*", json.dumps(["a", "b"]), "primary_backup", primary, threshold))


def test_promote_failed_primary_persists_and_no_repeat(mem_db):
    from app.services.gateway import rules as rule_svc
    _up(mem_db, "a", ["m-a"], fail_count=5, health="circuit_open")
    _up(mem_db, "b", ["m-b"])
    _insert_rule(mem_db)
    rule = dict(db.query_one("SELECT * FROM gateway_rules WHERE id='r1'"))
    promo = rule_svc.promote_failed_primary(rule)
    assert promo and promo["new_primary"]["id"] == "b"
    # 持久化：规则主上游已改为 b
    assert db.query_one("SELECT primary_upstream FROM gateway_rules WHERE id='r1'")["primary_upstream"] == "b"
    # 升主后新主健康 → 不再重复升主
    rule2 = dict(db.query_one("SELECT * FROM gateway_rules WHERE id='r1'"))
    assert rule_svc.promote_failed_primary(rule2) is None
    # 升主后旧主降备仍失效：请求模型仅旧主支持 → 兜底改写为健康上游模型
    from app.services.gateway import settings as settings_svc
    assert rule_svc.primary_backup_fallback_model(rule2, "m-a") == "m-b"


def test_promote_skips_when_primary_healthy_or_no_healthy_backup(mem_db):
    from app.services.gateway import rules as rule_svc
    _up(mem_db, "a", ["m-a"], fail_count=1)
    _up(mem_db, "b", ["m-b"])
    _insert_rule(mem_db)
    rule = dict(db.query_one("SELECT * FROM gateway_rules WHERE id='r1'"))
    assert rule_svc.promote_failed_primary(rule) is None
    # 主失效但备也熔断 → 不升主
    mem_db.execute("UPDATE gateway_upstreams SET fail_count=9, health='circuit_open'")
    rule = dict(db.query_one("SELECT * FROM gateway_rules WHERE id='r1'"))
    assert rule_svc.promote_failed_primary(rule) is None


def test_failover_promotion_alert_dedup_and_label(mem_db):
    from app.services import alert_analysis as aa
    first = aa.record_failover_promotion(rule_id="r1", rule_name="*", old_primary="agnes", new_primary="deepseek")
    assert first
    assert aa.record_failover_promotion(rule_id="r1", rule_name="*", old_primary="agnes", new_primary="deepseek") is None
    row = db.query_one("SELECT * FROM obs_alerts WHERE id=?", (first,))
    assert row["metric"] == "failover_promotion" and row["severity"] == "medium"
    listed = aa.list_alerts(status="open")
    item = next(a for a in listed["alerts"] if a["id"] == first)
    assert item["metric_label"] == "主备路由·备升主"
    assert "deepseek" in item["analysis"]["cause"]