"""网关超时/挂起场景主备切换回归（内存 DB，不出网）。

针对「路由调用失败后仍不发生主备切换」的根因锁定新契约：
  T1 响应头挂起：首块保护覆盖响应头阶段，超时记失败健康度并切换下一候选
     （旧版等满总预算直接终态 504，不记失败、不切换、trace 无上游步骤）；
  T2 主备规则候选被模型过滤剩主路由：主失败后请求内按备支持模型兜底重选；
     跨请求 fail_count 达阈值后 prepare 备升主持久化 + 告警；
  T3 role-only 空 chunk 不锁定提交：后续静默超期仍切换（旧版首块即提交）；
  T4 已产出正文后挂起：不切换（不可撤回），终态超时且记失败；
  T5 非流式多候选：单尝试预算均分，读超时后仍有预算切换。
"""
import asyncio
import json
import sqlite3

import httpx
import pytest

from app.core import db
from app.services.gateway import keys, proxy, rules as rule_svc, upstreams as up_svc


@pytest.fixture()
def mem_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    conn.isolation_level = None  # 自动提交：夹具直插数据后不残留隐式事务（写事务需 BEGIN IMMEDIATE）
    monkeypatch.setattr(db, "_conn", conn)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def fast_guards(monkeypatch):
    monkeypatch.setattr(proxy, "_FIRST_CHUNK_UTILITY", 0.2)
    monkeypatch.setattr(proxy, "_FIRST_CHUNK_DEFAULT", 0.2)


def _up(conn, uid, models, fail_count=0, health="healthy"):
    conn.execute(
        "INSERT INTO gateway_upstreams (id,name,protocol,base_url,api_key,remark,"
        "created_at,updated_at,enabled,models,fail_count,health,category,weight)"
        " VALUES (?,?,?,?,?,?,?,?,1,?,?,?, 'llm', 100)",
        (uid, uid, "openai", f"http://{uid}.invalid/v1", "k", "", "now", "now",
         json.dumps(models), fail_count, health))


def _rule(conn, rid="r1", ups=("a", "b"), primary="a", threshold=3, pattern="*"):
    conn.execute(
        "INSERT INTO gateway_rules (id, model_pattern, upstreams, lb_strategy, primary_upstream,"
        " failover_threshold, enabled, created_at, updated_at) VALUES (?,?,?,?,?,?,1,'now','now')",
        (rid, pattern, json.dumps(list(ups)), "primary_backup", primary, threshold))


def _body(model="m-a", stream=True, rule_id="r1"):
    return {"model": model, "stream": stream, "_gw_rule_id": rule_id,
            "messages": [{"role": "user", "content": "合成测试问题"}]}


def _mock_http(monkeypatch, handler):
    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        proxy.httpx, "AsyncClient",
        lambda **kw: client_class(transport=httpx.MockTransport(handler), **kw))


def _mock_iterator(monkeypatch, fn):
    monkeypatch.setattr(proxy.upstream, "stream_iterator", fn)


async def _collect(ctx):
    return [sse async for sse in proxy.stream_response(ctx)]


def _good_stream():
    yield 'data: {"choices":[{"delta":{"content":"备路由回答"}}]}\n\n'
    yield "data: [DONE]\n\n"


# ============================================
# T1 响应头挂起 → 记失败 + 切换
# ============================================
def test_stream_header_hang_failover_and_health(mem_db, monkeypatch):
    _up(mem_db, "a", ["m-a"])
    _up(mem_db, "b", ["m-a"])
    _rule(mem_db)
    key = keys.create_key("t1")
    ctx = proxy.prepare(_body(), key["key"])
    seen = []

    async def handler(request):
        seen.append(request.url.host)
        if request.url.host == "a.invalid":
            await asyncio.sleep(5)  # 挂起：不返回响应头
        return httpx.Response(200, content=b"data: x\n\n")

    async def fake_iter(eff, lines):
        if "a.invalid" in str(eff.get("base_url")):
            yield 'data: {"choices":[{"delta":{"content":"主路由回答"}}]}\n\n'
            yield "data: [DONE]\n\n"
        else:
            for s in _good_stream():
                yield s

    _mock_http(monkeypatch, handler)
    _mock_iterator(monkeypatch, fake_iter)
    joined = "".join(asyncio.run(_collect(ctx)))
    assert "备路由回答" in joined and "主路由回答" not in joined
    assert seen == ["a.invalid", "b.invalid"]
    row = db.query_one("SELECT status, retries FROM gateway_logs")
    assert row["status"] == "ok" and row["retries"] == 1
    # 关键：超时/挂起必须记失败健康度（驱动跨请求备升主），旧版 fail_count 永远 0
    a = db.query_one("SELECT fail_count, health, last_error FROM gateway_upstreams WHERE id='a'")
    assert a["fail_count"] >= 1 and a["last_error"]
    steps = json.loads(db.query_one("SELECT trace_steps FROM gateway_logs")["trace_steps"])
    assert any(s["phase"] == "upstream" and s["status"] == "error" for s in steps)


# ============================================
# T2 备不支持原模型：请求内兜底 + 跨请求备升主
# ============================================
def test_stream_in_request_backup_model_fallback(mem_db, monkeypatch):
    _up(mem_db, "a", ["m-a"])
    _up(mem_db, "b", ["m-b"])
    _rule(mem_db, threshold=3)
    key = keys.create_key("t2")
    ctx = proxy.prepare(_body(), key["key"])
    # 选路阶段备被模型过滤：候选仅主路由（复现生产 trace「候选上游 1 个」）
    assert [u["id"] for u in ctx.candidates] == ["a"]

    async def handler(request):
        if request.url.host == "a.invalid":
            await asyncio.sleep(5)
        return httpx.Response(200, content=b"data: x\n\n")

    async def fake_iter(eff, lines):
        if "a.invalid" in str(eff.get("base_url")):
            await asyncio.sleep(5)
            yield "data: [DONE]\n\n"
        else:
            for s in _good_stream():
                yield s

    _mock_http(monkeypatch, handler)
    _mock_iterator(monkeypatch, fake_iter)
    joined = "".join(asyncio.run(_collect(ctx)))
    assert "备路由回答" in joined
    assert ctx.model_req == "m-b"  # 请求内模型兜底改写
    steps = json.loads(db.query_one("SELECT trace_steps FROM gateway_logs")["trace_steps"])
    assert any("请求内兜底" in s["name"] for s in steps)
    # 阈值未达：不升主（跨请求晋升由 T2b 覆盖）
    assert db.query_one("SELECT primary_upstream FROM gateway_rules WHERE id='r1'")["primary_upstream"] == "a"


def test_stream_timeout_promotes_backup_on_next_request(mem_db, monkeypatch):
    _up(mem_db, "a", ["m-a"])
    _up(mem_db, "b", ["m-b"])
    _rule(mem_db, threshold=1)
    key = keys.create_key("t2b")

    async def handler(request):
        if request.url.host == "a.invalid":
            await asyncio.sleep(5)
        return httpx.Response(200, content=b"data: x\n\n")

    async def fake_iter(eff, lines):
        if "a.invalid" in str(eff.get("base_url")):
            await asyncio.sleep(5)
            yield "data: [DONE]\n\n"
        for s in _good_stream():
            yield s

    _mock_http(monkeypatch, handler)
    _mock_iterator(monkeypatch, fake_iter)
    ctx = proxy.prepare(_body(), key["key"])
    joined = "".join(asyncio.run(_collect(ctx)))
    assert "备路由回答" in joined
    # 下一次请求：备升主持久化 + 模型兜底改写 + 告警留痕
    ctx2 = proxy.prepare(_body(), key["key"])
    assert db.query_one("SELECT primary_upstream FROM gateway_rules WHERE id='r1'")["primary_upstream"] == "b"
    assert ctx2.model_req == "m-b"
    assert [u["id"] for u in ctx2.candidates][0] == "b"
    assert db.query_one("SELECT 1 FROM obs_alerts WHERE metric='failover_promotion'")


# ============================================
# T3 role-only 空 chunk 不锁定提交
# ============================================
def test_stream_role_chunk_then_silence_switches(mem_db, monkeypatch):
    _up(mem_db, "a", ["m-a"])
    _up(mem_db, "b", ["m-a"])
    _rule(mem_db)
    key = keys.create_key("t3")
    ctx = proxy.prepare(_body(), key["key"])

    async def handler(request):
        return httpx.Response(200, content=b"data: x\n\n")

    async def fake_iter(eff, lines):
        if "a.invalid" in str(eff.get("base_url")):
            yield 'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
            await asyncio.sleep(5)  # 空 chunk 后挂起（旧版首块即提交→等满超时）
            yield 'data: {"choices":[{"delta":{"content":"主路由回答"}}]}\n\n'
            yield "data: [DONE]\n\n"
        else:
            for s in _good_stream():
                yield s

    _mock_http(monkeypatch, handler)
    _mock_iterator(monkeypatch, fake_iter)
    joined = "".join(asyncio.run(_collect(ctx)))
    assert "备路由回答" in joined and "主路由回答" not in joined
    assert db.query_one("SELECT status FROM gateway_logs")["status"] == "ok"


# ============================================
# T4 已提交正文后挂起：不切换，终态超时
# ============================================
def test_stream_committed_content_hang_no_switch(mem_db, monkeypatch):
    monkeypatch.setattr(proxy.app_settings, "PROXY_REQUEST_TIMEOUT", 1.0)
    _up(mem_db, "a", ["m-a"])
    _up(mem_db, "b", ["m-a"])
    _rule(mem_db)
    key = keys.create_key("t4")
    ctx = proxy.prepare(_body(), key["key"])
    seen = []

    async def handler(request):
        seen.append(request.url.host)
        return httpx.Response(200, content=b"data: x\n\n")

    async def fake_iter(eff, lines):
        yield 'data: {"choices":[{"delta":{"content":"部分正文"}}]}\n\n'
        await asyncio.sleep(5)
        yield "data: [DONE]\n\n"

    _mock_http(monkeypatch, handler)
    _mock_iterator(monkeypatch, fake_iter)
    joined = "".join(asyncio.run(_collect(ctx)))
    assert "部分正文" in joined and "备路由回答" not in joined
    assert seen == ["a.invalid"]  # 已提交不可撤回，不切换
    row = db.query_one("SELECT status, status_code FROM gateway_logs")
    assert row["status"] == "timeout" and row["status_code"] == 504
    assert db.query_one("SELECT fail_count FROM gateway_upstreams WHERE id='a'")["fail_count"] >= 1


# ============================================
# T5 非流式多候选读超时切换
# ============================================
def test_json_read_timeout_failover_with_shared_budget(mem_db, monkeypatch):
    monkeypatch.setattr(proxy.app_settings, "PROXY_REQUEST_TIMEOUT", 0.8)
    _up(mem_db, "a", ["m-a"])
    _up(mem_db, "b", ["m-a"])
    _rule(mem_db)
    key = keys.create_key("t5")
    ctx = proxy.prepare(_body(stream=False), key["key"])
    seen = []

    async def handler(request):
        seen.append(request.url.host)
        if request.url.host == "a.invalid":
            await asyncio.sleep(5)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "备路由回答"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3}})

    _mock_http(monkeypatch, handler)
    status, result = asyncio.run(proxy.json_response(ctx))
    assert status == 200 and result["choices"][0]["message"]["content"] == "备路由回答"
    assert seen == ["a.invalid", "b.invalid"]
    assert db.query_one("SELECT retries FROM gateway_logs")["retries"] == 1


# ============================================
# T6 request_fallback_model 单元契约
# ============================================
def test_request_fallback_model_counts_in_request_failures(mem_db):
    _up(mem_db, "a", ["m-a"])
    _up(mem_db, "b", ["m-b"])
    _rule(mem_db)
    rule = rule_svc._row_to_dict(dict(db.query_one("SELECT * FROM gateway_rules WHERE id='r1'")))
    # DB fail_count=0 但本次请求已失败 a → 视同失效，解析备模型
    assert rule_svc.request_fallback_model(rule, "m-a", ["a"]) == "m-b"
    # 无请求内失败且 DB 健康 → 不改写
    assert rule_svc.request_fallback_model(rule, "m-a", []) is None
    # 备支持原模型 → 不改写（常规主备路径直接切换候选）
    mem_db.execute("UPDATE gateway_upstreams SET models=? WHERE id='b'", (json.dumps(["m-a", "m-b"]),))
    assert rule_svc.request_fallback_model(rule, "m-a", ["a"]) is None