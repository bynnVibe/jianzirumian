"""可信代理、独立诊断和请求资源生命周期边界，不访问外网。"""
import asyncio
import json
import time
from datetime import datetime, timedelta
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.requests import ClientDisconnect

from app.core import db
from app.core.request_budget import ClosingStreamingResponse, until_disconnect
from app.core.trusted_proxy import TrustedProxyMiddleware
from app.services.gateway import keys, proxy, rules, upstream, upstreams


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(db, "_conn", None)
    conn = db.get_conn()
    yield
    conn.close()


@pytest.mark.parametrize("client_ip,trusted", [("172.30.0.10", True), ("172.30.0.11", True), ("203.0.113.1", False)])
def test_proxy_headers_only_from_explicit_network(client_ip, trusted):
    app = FastAPI()
    app.add_middleware(TrustedProxyMiddleware, trusted_hosts="172.30.0.0/24")
    @app.get("/")
    async def endpoint(request: Request):
        return {"ip": request.client.host, "scheme": request.url.scheme}
    async def run():
        transport = httpx.ASGITransport(app=app, client=(client_ip, 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://mock") as client:
            response = await client.get("/", headers={"X-Forwarded-For": "198.51.100.9", "X-Forwarded-Proto": "https"})
            assert response.json() == {"ip": "198.51.100.9" if trusted else client_ip, "scheme": "https" if trusted else "http"}
    asyncio.run(run())


@pytest.mark.parametrize("trusted,peer,forwarded,expected", [
    ("", "172.30.0.10", "198.51.100.9", "172.30.0.10"),
    ("172.30.0.0/24", "172.30.0.10", "198.51.100.99, 203.0.113.1, 172.30.0.11", "203.0.113.1"),
    ("172.30.0.0/24", "172.30.0.10", "not-an-ip", "172.30.0.10"),
    ("fd00::/64", "fd00::1", "2001:db8::2, fd00::3", "2001:db8::2"),
])
def test_proxy_chain_and_invalid_headers(trusted, peer, forwarded, expected):
    seen = []
    async def app(scope, receive, send):
        seen.append(scope)
    scope = {"type": "http", "scheme": "http", "client": (peer, 1234),
             "headers": [(b"x-forwarded-for", forwarded.encode()), (b"x-forwarded-proto", b"javascript")]}
    asyncio.run(TrustedProxyMiddleware(app, trusted)(scope, None, None))
    assert seen[0]["client"][0] == expected and seen[0]["scheme"] == "http"
    assert scope["client"] == (peer, 1234)


@pytest.mark.parametrize("cidr", ["*", "0.0.0.0/0", "::/0"])
def test_all_sources_cannot_be_trusted(cidr):
    with pytest.raises(ValueError):
        TrustedProxyMiddleware(None, trusted_hosts=cidr)


@pytest.mark.parametrize("valid", [True, False])
def test_upstream_connection_test_reports_and_updates_health(monkeypatch, valid):
    up = upstreams.create_upstream({"name": "mock", "base_url": "https://upstream.invalid/v1",
                                    "models": ["demo"], "tier": "paid"})
    client_type = httpx.AsyncClient
    def handler(request):
        assert request.url.host == "upstream.invalid"
        if not valid:
            raise httpx.ConnectError("合成上游不可达", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_type(transport=httpx.MockTransport(handler), **kw))
    result = asyncio.run(upstreams.test_connection(up["id"]))
    assert result["success"] is valid
    assert upstreams.get_upstream(up["id"])["health"] == ("healthy" if valid else "degraded")
    # 连通性诊断不写业务调用日志
    assert db.query("SELECT * FROM gateway_logs") == []


def context(stream=False):
    ids = []
    for name in ["first", "second"]:
        up = upstreams.create_upstream({"name": name, "base_url": f"https://{name}.invalid/v1",
                                        "models": ["demo"], "tier": "paid"})
        ids.append(up["id"])
    rules.create_rule({"model_pattern": "demo", "upstreams": ids, "lb_strategy": "priority"})
    key = keys.create_key("edge")
    return proxy.prepare({"model": "demo", "stream": stream}, key["key"]), key


@pytest.mark.parametrize("mode", ["timeout", "disconnect"])
def test_json_budget_or_disconnect_closes_client_and_finalizes_once(monkeypatch, mode):
    ctx, key = context()
    closed, clients = [], []
    client_type = httpx.AsyncClient
    async def handler(request):
        try:
            await asyncio.sleep(10)
        finally:
            closed.append(True)
    def client(**kw):
        value = client_type(transport=httpx.MockTransport(handler), **kw)
        clients.append(value)
        return value
    monkeypatch.setattr(httpx, "AsyncClient", client)
    async def run():
        if mode == "timeout":
            ctx.deadline = time.monotonic() + 0.03
            with pytest.raises(proxy.GatewayError) as exc:
                await proxy.json_response(ctx)
            assert exc.value.status_code == 504
        else:
            checks = 0
            async def disconnected():
                nonlocal checks
                checks += 1
                return checks > 1
            with pytest.raises(asyncio.CancelledError):
                await until_disconnect(SimpleNamespace(is_disconnected=disconnected), proxy.json_response(ctx))
    asyncio.run(run())
    assert closed == [True] and len(clients) == 1 and clients[0].is_closed
    assert db.query_one("SELECT status FROM gateway_logs")["status"] == ("timeout" if mode == "timeout" else "cancelled")
    assert keys.get_key(key["id"])["request_count"] == 1
    assert upstreams.get_upstream(ctx.upstream["id"])["health"] != "down"


def test_asgi_send_disconnect_explicitly_closes_generator():
    closed = []
    async def generate():
        try:
            yield "部分文本"
            await asyncio.sleep(10)
        finally:
            closed.append(True)
    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("客户端断开")
    async def receive():
        await asyncio.sleep(10)
        return {"type": "http.disconnect"}
    async def run():
        response = ClosingStreamingResponse(generate())
        # 新旧 Starlette 分别直接抛断线或通过 task group 包装 OSError。
        with pytest.raises((ClientDisconnect, OSError, ExceptionGroup)):
            async with asyncio.timeout(1):
                await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    asyncio.run(run())
    assert closed == [True]


@pytest.mark.parametrize("after_content", [False, True])
def test_sse_retry_only_before_first_chunk(monkeypatch, after_content):
    ctx, key = context(True)
    seen, closed, client_type = [], [], httpx.AsyncClient
    class Stream(httpx.AsyncByteStream):
        def __init__(self, host):
            self.host = host
        async def __aiter__(self):
            if self.host == "first.invalid":
                if after_content:
                    yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                raise httpx.ReadError("合成断线")
            yield b'data: {"choices":[{"delta":{"content":"complete"}}]}\n\n'
            yield b'data: [DONE]\n\n'
        async def aclose(self):
            closed.append(self.host)
    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(200, stream=Stream(request.url.host))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_type(transport=httpx.MockTransport(handler), **kw))
    async def run():
        return "".join([event async for event in proxy.stream_response(ctx)])
    text = asyncio.run(run())
    assert seen == (["first.invalid"] if after_content else ["first.invalid", "second.invalid"])
    assert set(closed) == set(seen)
    assert ("[DONE]" in text) is not after_content
    assert db.query_one("SELECT status FROM gateway_logs")["status"] == ("error" if after_content else "ok")
    assert keys.get_key(key["id"])["request_count"] == 1


@pytest.mark.parametrize("protocol,done", [(p, d) for p in ["openai", "ollama", "anthropic"] for d in [False, True]])
def test_protocol_end_marker_is_explicit(protocol, done):
    lines = {
        "openai": ['data: {"choices":[{"delta":{"content":"x"}}]}', 'data: [DONE]'],
        "ollama": ['{"message":{"content":"x"}}', '{"done":true}'],
        "anthropic": ['data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"x"}}', 'data: {"type":"message_stop"}'],
    }[protocol]
    async def source():
        for line in lines if done else lines[:1]:
            yield line
    async def run():
        return "".join([s async for s in upstream.stream_iterator({"protocol": protocol}, source())])
    assert ("[DONE]" in asyncio.run(run())) is done


@pytest.mark.parametrize("pieces,expected", [
    (["<thi", "nk>内部计划</th", "ink>正文", "结束"], "正文结束"),
    (["  <analysis>分析</analysis><think>推理</think>\n答案"], "答案"),
    (["<think>没有闭合的过程"], ""),
    (["English terminology", " and 中文"], "English terminology and 中文"),
    (["1 < 2", " 是比较表达式"], "1 < 2 是比较表达式"),
])
def test_answer_text_filter_is_incremental_and_conservative(pieces, expected):
    from app.core.llm import AnswerTextFilter
    parser = AnswerTextFilter()
    assert "".join(parser.feed(piece) for piece in pieces) == expected


def _delta(content=None, reasoning=None, finish=None):
    return SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content=content, reasoning_content=reasoning), finish_reason=finish,
    )])


def _openai_stream(monkeypatch, items):
    from app.core import llm
    closed = AsyncMock()
    class Response:
        response = SimpleNamespace(aclose=closed)
        async def __aiter__(self):
            for item in items:
                if isinstance(item, Exception):
                    raise item
                yield item
    create = AsyncMock(return_value=Response())
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(llm, "_get_openai_client", lambda *args: client)
    return llm.OpenAICompatibleLLM("https://mock.invalid", "synthetic", "demo"), create, closed


def test_openai_emits_body_before_done_and_never_reasoning(monkeypatch, caplog):
    model, create, closed = _openai_stream(monkeypatch, [
        _delta(reasoning="不应输出的内部计划"), _delta("你好"),
        _delta("世界", reasoning="也不应输出"), _delta(finish="stop"),
    ])
    caplog.set_level("INFO", logger="jianziruyang.llm")
    async def run():
        stream = model.chat([], stream=True)
        assert await anext(stream) == "你好"
        closed.assert_not_awaited()
        assert await anext(stream) == "世界"
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    asyncio.run(run())
    closed.assert_awaited_once()
    assert [call.kwargs["stream"] for call in create.await_args_list] == [True]
    assert "content_chunks=2" in caplog.text and "first_content_ms=" in caplog.text
    assert "不应输出" not in caplog.text


@pytest.mark.parametrize("kind", ["empty", "reasoning_only", "broken_before", "broken_after", "missing_end"])
def test_openai_no_silent_nonstream_fallback(monkeypatch, kind):
    items = {
        "empty": [_delta(finish="stop")],
        "reasoning_only": [_delta(reasoning="只有推理", finish="stop")],
        "broken_before": [RuntimeError("合成断线")],
        "broken_after": [_delta("部分正文"), RuntimeError("合成断线")],
        "missing_end": [_delta("部分正文")],
    }[kind]
    model, create, closed = _openai_stream(monkeypatch, items)
    seen = []
    async def run():
        with pytest.raises((ValueError, RuntimeError)):
            async for text in model.chat([], stream=True):
                seen.append(text)
    asyncio.run(run())
    assert seen == (["部分正文"] if kind in ("broken_after", "missing_end") else [])
    assert create.await_count == 1 and create.await_args.kwargs["stream"] is True
    closed.assert_awaited_once()


def test_openai_early_close_releases_response(monkeypatch):
    model, _, closed = _openai_stream(monkeypatch, [_delta("部分正文"), _delta("后续正文")])
    async def run():
        stream = model.chat([], stream=True)
        assert await anext(stream) == "部分正文"
        await stream.aclose()
    asyncio.run(run())
    closed.assert_awaited_once()


def test_document_sources_group_sort_cite_and_history(monkeypatch):
    from app.services import knowledge, wiki, evaluation
    from app.services.records import upload_records
    from app.models.schemas import MessageResponse
    monkeypatch.setattr(knowledge, "rerank", lambda query, results: results)
    monkeypatch.setattr(wiki, "wiki_priority_enabled", lambda: True)
    db.execute("INSERT INTO upload_records(id,filename,title,image_path,created_at,updated_at) VALUES (?,?,?,?,?,?)",
               ("doc", "笔记.docx", "养生笔记", "/a.docx", "now", "now"))
    metadata = {"source_image": "/a.docx", "kb_id": "kb1", "source_type": "word", "pdf_preview_path": "/a.pdf"}
    full_text = "完整原文" * 100
    results = [
        ("百科摘要", {**metadata, "is_wiki_page": True, "wiki_title": "百科词条"}, .5),
        ("其他文档", {"source_image": "/b.docx", "document_title": "养生笔记"}, .8),
        (full_text, {**metadata, "page_number": 3}, .95),
        ("另一个知识库", {**metadata, "kb_id": "kb2"}, .85),
        ("低相关不应出现", metadata, .1),
        ("无身份片段1", {"wiki_title": "同名"}, .7),
        ("无身份片段2", {"wiki_title": "同名"}, .7),
    ]
    context, sources = knowledge.knowledge_service.format_knowledge_context("问题", results)
    assert len(sources) == 5
    assert [s["relevance"] for s in sources] == [95, 85, 80, 70, 70]
    assert [s["index"] for s in sources] == list(range(1, 6))
    assert sources[0]["document_title"] == "养生笔记"
    assert sources[0]["chunk_count"] == 2
    assert [c["text"] for c in sources[0]["chunks"]] == ["百科摘要", full_text]
    assert context.count("[来源 1]") == 1 and "第 3 页" in context
    assert full_text in context and "低相关不应出现" not in context
    restored = MessageResponse(role="assistant", content="答案[来源 1]", sources=sources).model_dump()
    assert restored["sources"][0]["chunks"][1]["text"] == full_text
    assert restored["sources"][0]["chunks"][1]["pdf_preview_path"] == "/a.pdf"
    assert evaluation._compute_retrieval_hit([], "百科词条", "", sources) == 1
    old = upload_records.enrich_sources([{"index": 8, "text": "旧片段", "source_image": "/a.docx"}])
    assert old[0]["document_title"] == "养生笔记" and old[0]["index"] == 8


@pytest.mark.parametrize("partial", [False, True])
def test_assistant_stream_error_does_not_append_complete_answer(monkeypatch, partial):
    from app.services import wiki_assistant as assistant
    completed = AsyncMock(side_effect=['{"action":"final"}'])
    monkeypatch.setattr(assistant, "_llm_complete", completed)
    calls, closed = [], []
    class Model:
        async def chat(self, messages, stream=False):
            calls.append(stream)
            assert assistant.FINAL_ANSWER_RULES in messages[0]["content"]
            try:
                if partial:
                    yield "部分正文"
                raise RuntimeError("合成断线")
            finally:
                closed.append(True)
    monkeypatch.setattr(assistant.LLMFactory, "create_assistant_llm", lambda: Model())
    state = {"status": "ok", "error": ""}
    async def run():
        return [json.loads(event[6:]) async for event in assistant._run_assistant_inner(
            "问题", assistant.AssistantContext("test"), [], state,
        )]
    events = asyncio.run(run())
    assert completed.await_count == 1 and calls == [True] and closed == [True]
    assert [e["content"] for e in events if e["event"] == "token"] == (["部分正文"] if partial else [])
    assert events[-1]["event"] == "error" and state["status"] == "error"


@pytest.fixture
def personal_observability(monkeypatch):
    from app.core import observability as obs
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 30, 12)
    monkeypatch.setattr(obs, "datetime", FixedDateTime)
    return obs, FixedDateTime.now().replace(hour=0)


def _personal_trace(user_id, started_at, status="ok", latency=1000, agent="chat"):
    trace_id = str(uuid4())
    db.execute(
        "INSERT INTO agent_traces(id,user_id,agent,status,latency_ms,started_at,input,error,tool_calls,prompt_tokens)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (trace_id, user_id, agent, status, latency, started_at.isoformat(),
         "不应泄漏的问题正文", "不应泄漏的错误原文", 17, 9876),
    )
    db.execute(
        "INSERT INTO agent_spans(id,trace_id,name,input,output,started_at) VALUES (?,?,?,?,?,?)",
        (str(uuid4()), trace_id, "private-tool-name", "private-prompt", "private-answer", started_at.isoformat()),
    )
    return trace_id


def test_personal_metrics_isolate_business_fields_and_statuses(personal_observability):
    obs, today = personal_observability
    for status, latency in [("ok", 1000), ("error", 2000), ("timeout", 3000),
                            ("interrupted", 4000), ("running", 999000), ("cancelled", 999000)]:
        _personal_trace("alice", today, status, latency)
    _personal_trace("alice", today - timedelta(days=2), agent="wiki_assistant")
    _personal_trace("alice", today - timedelta(days=2), agent="private-agent-name")
    _personal_trace("bob", today, latency=999000)
    _personal_trace("guest", today)
    result = obs.personal_metrics("alice", 7)
    assert result["overview"] == {
        "total": 8, "completed": 6, "succeeded": 3, "failed": 3,
        "cancelled": 1, "processing": 1, "completion_rate": .5, "avg_response_seconds": 2.0,
    }
    assert len(result["daily"]) == 7
    assert [row["total"] for row in result["daily"]] == [0, 0, 0, 0, 2, 0, 6]
    assert {row["activity"] for row in result["by_activity"]} == {"智能问答", "知识助手", "其他服务"}
    assert result["by_activity"][0]["total"] == 6
    assert sum(row["total"] for row in result["by_activity"]) == result["overview"]["total"]
    assert set(result) == {"days", "retention_days", "overview", "daily", "by_activity"}
    fields = set(result["overview"])
    assert all(set(row) == fields | {"day"} for row in result["daily"])
    assert all(set(row) == fields | {"activity"} for row in result["by_activity"])
    text = json.dumps(result, ensure_ascii=False)
    for sensitive in ["alice", "bob", "不应泄漏", "private-", "agent", "tokens", "tool", "input", "error", "trace"]:
        assert sensitive not in text


@pytest.mark.parametrize("days", [1, 7, 30])
def test_personal_metrics_calendar_boundaries(personal_observability, days):
    obs, today = personal_observability
    start = today - timedelta(days=days - 1)
    end = today + timedelta(days=1)
    for when in [start - timedelta(microseconds=1), start, end - timedelta(microseconds=1), end]:
        _personal_trace("alice", when)
    result = obs.personal_metrics("alice", days)
    assert result["overview"]["total"] == 2
    assert len(result["daily"]) == days
    assert result["daily"][0]["day"] == start.date().isoformat()
    assert result["daily"][-1]["day"] == today.date().isoformat()


@pytest.mark.parametrize("active", [False, True])
def test_personal_metrics_no_completed_operations(personal_observability, active):
    obs, today = personal_observability
    if active:
        _personal_trace("alice", today, "running")
        _personal_trace("alice", today, "cancelled")
    result = obs.personal_metrics("alice")
    assert result["overview"]["total"] == (2 if active else 0)
    assert result["overview"]["completed"] == 0
    assert result["overview"]["completion_rate"] is None
    assert result["overview"]["avg_response_seconds"] is None
    assert result["daily"][0]["completion_rate"] is None


@pytest.mark.parametrize("agent,activity", [
    ("wiki_compile", "笔记整理"), ("wiki_synthesize", "综合报告"), ("chat_confirmation", "资料入库"),
])
def test_personal_metrics_business_activity_names(personal_observability, agent, activity):
    obs, today = personal_observability
    _personal_trace("alice", today, agent=agent)
    assert obs.personal_metrics("alice")["by_activity"][0]["activity"] == activity


@pytest.mark.parametrize("role", ["user", "admin", "ops"])
def test_observability_api_identity_and_technical_permissions(monkeypatch, personal_observability, role):
    from app.api import deps, observability as api
    obs, today = personal_observability
    trace_id = _personal_trace("alice", today)
    _personal_trace("bob", today)
    _personal_trace("bob", today, "error")
    monkeypatch.setattr(deps.auth_service, "get_session_user", lambda token: {"id": "alice", "role": role})
    log_calls = []
    def logs(*args):
        log_calls.append(args)
        return {"entries": [], "next_cursor": None}
    monkeypatch.setattr(obs, "get_trace_logs", logs)
    app = FastAPI()
    app.include_router(api.router)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock") as client:
            headers = {"Authorization": "Bearer synthetic-session"}
            response = await client.get("/api/observability/my-metrics?user_id=bob&days=7", headers=headers)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["overview"]["total"] == 1
            assert set(response.json()) == {"success", "days", "retention_days", "overview", "daily", "by_activity"}
            assert "不应泄漏" not in response.text and "alice" not in response.text and "bob" not in response.text
            for path in ["metrics", "traces", f"traces/{trace_id}", f"traces/{trace_id}/logs"]:
                technical = await client.get(f"/api/observability/{path}", headers=headers)
                assert technical.status_code == (403 if role == "user" else 200)
                if path == "metrics" and role != "user":
                    assert technical.json()["overview"]["total"] == 3
            for days in [0, 31, "invalid"]:
                assert (await client.get(f"/api/observability/my-metrics?days={days}", headers=headers)).status_code == 422
    asyncio.run(run())
    assert len(log_calls) == (0 if role == "user" else 1)


@pytest.mark.parametrize("session,token,status", [
    (None, "", 401), (None, "expired", 401),
    ({"id": "guest", "role": "guest"}, "guest-session", 403),
    ({"id": "guest", "role": "user"}, "guest-session", 403),
    ({"id": "system", "role": "user"}, "system-session", 403),
    ({"id": "", "role": "user"}, "empty-session", 403),
])
def test_personal_metrics_requires_real_account(monkeypatch, session, token, status):
    from app.api import deps, observability as api
    monkeypatch.setattr(deps.auth_service, "get_session_user", lambda _: session)
    def forbidden(*args):
        pytest.fail("鉴权失败不应查询统计数据")
    monkeypatch.setattr(api.obs, "personal_metrics", forbidden)
    app = FastAPI()
    app.include_router(api.router)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock") as client:
            response = await client.get("/api/observability/my-metrics", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == status
    asyncio.run(run())
