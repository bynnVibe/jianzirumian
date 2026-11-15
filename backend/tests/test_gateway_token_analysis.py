"""网关 Token 用量分析回归测试（内存 DB，不出网）。

覆盖计划第四节（自可观测性迁移合并至网关）：
  T1 logs.stats by_key：按密钥聚合 prompt/completion/总量/请求数/风险数 + 密钥类型 join；
  T2 GET /api/gateway/token-analysis：两源合并（网关日志 + Agent 段构成）+ 配额水位；
  T3 越权 403（普通用户不可访问）；
  T4 参数口径：days 上限 90、range=today。
"""
import asyncio
import sqlite3
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


def _log(log_id, key_id, key_name, prompt, completion, risk="none",
         model="qwen-max", channel="阿里云"):
    db.execute(
        "INSERT INTO gateway_logs (id, key_id, key_name, model_requested, channel_name,"
        " status, prompt_tokens, completion_tokens, total_tokens, latency_ms,"
        " risk_level, created_at) VALUES (?,?,?,?,?,'ok',?,?,?,?,?,?)",
        (log_id, key_id, key_name, model, channel, prompt, completion,
         prompt + completion, 100, risk, datetime.now().isoformat()))


def _gw_app():
    from app.api import gateway as gw_api
    app = FastAPI()
    app.include_router(gw_api.router)
    return app


# ============================================
# T1 by_key 聚合口径
# ============================================
def test_stats_by_key_aggregation(mem_db):
    from app.services.gateway import keys as key_svc
    from app.services.gateway import logs as log_svc
    pub = key_svc.create_key("公共池", key_type="public")
    uk = key_svc.create_key("张三专属", key_type="user", bound_users=["u1"])
    _log("l1", pub["id"], "公共池", 100, 200)
    _log("l2", pub["id"], "公共池", 50, 50, risk="medium")
    _log("l3", uk["id"], "张三专属", 10, 20)
    _log("l4", "", "", 999, 999)  # 无密钥日志不计入 by_key
    stats = log_svc.stats(7)
    by_key = {r["name"]: r for r in stats["by_key"]}
    assert set(by_key) == {"公共池", "张三专属"}
    pub_row = by_key["公共池"]
    assert pub_row["key_type"] == "public"
    assert pub_row["total"] == 2 and pub_row["tokens"] == 400
    assert pub_row["prompt_tokens"] == 150 and pub_row["completion_tokens"] == 250
    assert pub_row["risky"] == 1
    assert by_key["张三专属"]["key_type"] == "user"
    assert by_key["张三专属"]["tokens"] == 30
    assert stats["overview"]["tokens"] == 400 + 30 + 1998


# ============================================
# T2 两源合并 + 配额水位
# ============================================
def test_token_analysis_endpoint_merges_sources(mem_db):
    import httpx
    from app.api import deps
    from app.services.gateway import keys as key_svc
    app = _gw_app()
    app.dependency_overrides[deps.require_ops] = lambda: {"id": "a1", "role": "admin"}
    pub = key_svc.create_key("公共池", key_type="public", token_quota=1000)
    key_svc.consume_tokens(pub["id"], 400)
    _log("l1", pub["id"], "公共池", 100, 200)
    _log("l2", pub["id"], "公共池", 50, 50)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/gateway/token-analysis", params={"days": 7})
            assert r.status_code == 200
            body = r.json()
            assert body["success"] is True and body["days"] == 7
            gw = body["gateway"]
            assert gw["overview"]["tokens"] == 400
            assert gw["by_key"][0]["name"] == "公共池"
            assert gw["by_model"][0]["name"] == "qwen-max"
            assert gw["by_channel"][0]["name"] == "阿里云"
            assert isinstance(gw["daily"], list)
            # 配额水位：quota=1000 used=400 remaining=600
            quota = {k["name"]: k for k in body["keys_quota"]}
            assert quota["公共池"]["token_quota"] == 1000
            assert quota["公共池"]["token_used"] == 400
            assert quota["公共池"]["token_remaining"] == 600
            # Agent 侧段构成口径并入（空库为零值但结构完整）
            agent = body["agent"]
            assert set(agent["overview"]) == {
                "total_tokens", "avg_tokens_per_request",
                "token_warn_threshold", "token_overuse"}
            assert "token_comparison" in agent and "segment_breakdown" in agent
    asyncio.run(run())


def test_token_analysis_agent_side_data(mem_db):
    """Agent 侧有 trace 数据时，token_comparison/segment_breakdown 随端点返回。"""
    import httpx
    from app.api import deps
    app = _gw_app()
    app.dependency_overrides[deps.require_ops] = lambda: {"id": "a1", "role": "ops"}
    now = datetime.now().isoformat()
    db.execute(
        "INSERT INTO agent_traces (id, agent, status, prompt_tokens, completion_tokens,"
        " latency_ms, started_at) VALUES ('t1','chat','ok',3000,4000,1000,?)", (now,))

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = (await client.get("/api/gateway/token-analysis")).json()
            assert body["agent"]["overview"]["total_tokens"] == 7000
            assert body["agent"]["overview"]["avg_tokens_per_request"] == 7000
    asyncio.run(run())


# ============================================
# T3 越权
# ============================================
def test_token_analysis_forbidden_for_regular_user(mem_db, monkeypatch):
    import httpx
    from app.api import deps
    app = _gw_app()
    monkeypatch.setattr(deps, "get_current_user",
                        lambda authorization="": {"id": "u1", "role": "user"})

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/api/gateway/token-analysis")).status_code == 403
    asyncio.run(run())


# ============================================
# T4 参数口径
# ============================================
def test_token_analysis_days_clamp_and_today(mem_db):
    import httpx
    from app.api import deps
    app = _gw_app()
    app.dependency_overrides[deps.require_ops] = lambda: {"id": "a1", "role": "admin"}

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = (await client.get("/api/gateway/token-analysis",
                                     params={"days": 365})).json()
            assert body["days"] == 90  # 上限收敛
            body = (await client.get("/api/gateway/token-analysis",
                                     params={"range": "today"})).json()
            assert body["success"] is True
    asyncio.run(run())