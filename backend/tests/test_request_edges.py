"""可信代理、独立诊断和请求资源生命周期边界，不访问外网。"""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from starlette.requests import ClientDisconnect

from app.config import settings
from app.core import db
from app.core.request_budget import ClosingStreamingResponse, until_disconnect
from app.core.trusted_proxy import TrustedProxyMiddleware
from app.services.gateway import channels, keys, proxy, diagnostics, upstream


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


@pytest.mark.parametrize("configured,supplied,status", [("", "", 503), ("x" * 32, "wrong", 403)])
def test_diagnostic_auth_blocks_before_upstream(monkeypatch, configured, supplied, status):
    monkeypatch.setattr(settings, "PROXY_DIAGNOSTIC_TOKEN", configured)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(diagnostics.diagnose("missing", supplied))
    assert exc.value.status_code == status


def test_agent_diagnostic_uses_only_proxy_and_does_not_fallback(monkeypatch):
    from app.api import gateway
    channel = channels.create_channel({"name": "mock", "base_url": "https://upstream.invalid/v1", "models": ["demo"]})
    monkeypatch.setattr(settings, "PROXY_DIAGNOSTIC_TOKEN", "synthetic-internal-token-1234567890")
    monkeypatch.setattr(settings, "LLM_PROXY_URL", "http://proxy.invalid:8001")
    seen, client_type = [], httpx.AsyncClient
    def handler(request):
        seen.append(str(request.url))
        assert request.headers["X-Diagnostic-Token"] == settings.PROXY_DIAGNOSTIC_TOKEN
        return httpx.Response(503)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_type(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(gateway.test_channel(channel["id"], {"role": "ops"}))
    assert exc.value.status_code == 502
    assert seen == [f"http://proxy.invalid:8001/internal/diagnostics/channels/{channel['id']}"]


@pytest.mark.parametrize("valid", [True, False])
def test_proxy_diagnostic_protocol_and_redacted_failure(monkeypatch, valid):
    channel = channels.create_channel({"name": "mock", "base_url": "https://upstream.invalid/v1", "models": ["demo"]})
    monkeypatch.setattr(settings, "PROXY_DIAGNOSTIC_TOKEN", "synthetic-internal-token-1234567890")
    client_type = httpx.AsyncClient
    def handler(request):
        assert request.url.host == "upstream.invalid"
        assert json.loads(request.content)["max_tokens"] == 5
        if not valid:
            raise httpx.ConnectError("不要泄漏这个上游凭据", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_type(transport=httpx.MockTransport(handler), **kw))
    result = asyncio.run(diagnostics.diagnose(channel["id"], settings.PROXY_DIAGNOSTIC_TOKEN))
    assert result["success"] is valid and result["executor"] == "llm-proxy"
    assert "不要泄漏" not in json.dumps(result, ensure_ascii=False)
    assert db.query("SELECT * FROM gateway_logs") == []


def context(stream=False):
    for name, priority in [("first", 10), ("second", 0)]:
        channels.create_channel({"name": name, "base_url": f"https://{name}.invalid/v1", "models": ["demo"], "priority": priority})
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
    assert channels.get_channel(ctx.channel["id"])["health"] != "down"


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
