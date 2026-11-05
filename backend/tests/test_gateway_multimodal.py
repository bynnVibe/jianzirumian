"""网关多模态上游（OCR/Embedding/ReRank）与离开拦截后端的回归测试（内存 DB，不出网）。

覆盖 Spec WS1/WS2：
  WS1  上游分类建模：category CRUD + 校验 + 按类别过滤 list/all_model_names；
       存量网络 provider 种子化幂等；ROLE_CATEGORY 过滤候选上游（防跨类误选）。
  WS2  代理转发 embeddings/rerank：URL/请求/响应按 kind 适配；/v1/embeddings 与
       /v1/reranks 端到端转发（mock httpx）；三工厂网关未就绪回退直连。
"""
import asyncio
import json
import sqlite3

import pytest

from app.core import db


@pytest.fixture()
def mem_db(monkeypatch):
    """独立内存数据库：避免污染本地开发库。"""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    yield conn
    conn.close()


def _mk_upstream(**over):
    from app.services.gateway import upstreams as up_svc
    payload = {"name": "up", "protocol": "openai", "tier": "paid",
               "base_url": "https://api.example/v1", "api_key": "sk-x",
               "models": ["m1"], "category": "llm", "enabled": True}
    payload.update(over)
    return up_svc.create_upstream(payload)


# ============================================
# WS1 上游分类：CRUD + 校验 + 过滤
# ============================================

def test_category_defaults_to_llm_and_normalizes_invalid(mem_db):
    from app.services.gateway import upstreams as up_svc

    # 未传 category → 默认 llm
    a = up_svc.create_upstream({"name": "a", "protocol": "openai", "tier": "paid",
                                "base_url": "https://a/v1", "models": ["m"]})
    assert a["category"] == "llm"
    # 非法 category → 归一为 llm（不抛异常）
    b = up_svc.create_upstream({"name": "b", "protocol": "openai", "tier": "paid",
                                "base_url": "https://b/v1", "models": ["m"],
                                "category": "not-a-cat"})
    assert b["category"] == "llm"
    # 合法 category 原样保留
    c = _mk_upstream(name="c", category="embedding", base_url="https://c/v1")
    assert c["category"] == "embedding"


def test_list_and_model_names_filter_by_category(mem_db):
    from app.services.gateway import upstreams as up_svc

    _mk_upstream(name="llm1", category="llm", models=["chat-a", "chat-b"])
    _mk_upstream(name="emb1", category="embedding", base_url="https://e/v1",
                 models=["emb-a"])
    _mk_upstream(name="rr1", category="rerank", base_url="https://r/v1",
                 models=["rr-a"])
    _mk_upstream(name="ocr1", category="ocr", base_url="https://o/v1",
                 models=["ocr-a"])

    assert len(up_svc.list_upstreams()) == 4
    assert [u["name"] for u in up_svc.list_upstreams(category="embedding")] == ["emb1"]
    assert [u["name"] for u in up_svc.list_upstreams(category="rerank")] == ["rr1"]
    assert up_svc.all_model_names(category="embedding") == ["emb-a"]
    assert up_svc.all_model_names(category="ocr") == ["ocr-a"]
    # 全量汇总含各类别模型
    allm = up_svc.all_model_names()
    for m in ("chat-a", "emb-a", "rr-a", "ocr-a"):
        assert m in allm


def test_update_and_clone_preserve_category(mem_db):
    from app.services.gateway import upstreams as up_svc

    up = _mk_upstream(name="src", category="rerank", base_url="https://r/v1",
                      models=["rr-a"])
    # 更新其它字段不动 category
    upd = up_svc.update_upstream(up["id"], {"weight": 50})
    assert upd["category"] == "rerank" and upd["weight"] == 50
    # 显式改 category
    upd2 = up_svc.update_upstream(up["id"], {"category": "embedding"})
    assert upd2["category"] == "embedding"
    # 克隆保留 category
    clone = up_svc.clone_upstream(up["id"])
    assert clone["category"] == "embedding"
    assert clone["name"].endswith("副本")


def test_seed_network_providers_idempotent(mem_db, monkeypatch):
    """存量网络 provider 幂等种子化：建上游 + 专用规则 + 绑定 role；再跑不重复。"""
    from app.services import config_manager as cm
    from app.services.gateway import upstreams as up_svc
    from app.services.gateway import rules as rule_svc

    monkeypatch.setattr(cm.runtime_config, "_data", {
        "embedding_provider": "openai", "rerank_provider": "dashscope",
        "ocr_provider": "aliyun",
    })
    conn_map = {
        ("embedding", "openai"): {"base_url": "https://emb/v1", "api_key": "sk-e",
                                  "model": "emb-1"},
        ("rerank", "dashscope"): {"base_url": "https://rr/v1", "api_key": "sk-r",
                                  "model": "rr-1"},
        ("ocr", "aliyun"): {"base_url": "https://ocr/v1", "api_key": "sk-o",
                            "model": "ocr-1"},
    }
    monkeypatch.setattr(cm, "effective_provider_config",
                        lambda cat, prov: conn_map.get((cat, prov), {}))
    monkeypatch.setattr(up_svc, "_seeded", False)

    created = up_svc.seed_network_providers_from_settings(force=True)
    assert created == 3
    assert len(up_svc.list_upstreams(category="embedding")) == 1
    assert len(up_svc.list_upstreams(category="rerank")) == 1
    assert len(up_svc.list_upstreams(category="ocr")) == 1
    emb = up_svc.list_upstreams(category="embedding")[0]
    assert emb["models"] == ["emb-1"] and emb["base_url"] == "https://emb/v1"
    # role 已绑定专用规则与模型
    assert (rule_svc.get_role_route("embed") or {}).get("model_name") == "emb-1"
    assert (rule_svc.get_role_route("embed") or {}).get("rule_id")

    # 幂等：再跑不新增上游、不覆盖已绑定 role
    monkeypatch.setattr(up_svc, "_seeded", False)
    created2 = up_svc.seed_network_providers_from_settings(force=True)
    assert created2 == 0
    assert len(up_svc.list_upstreams(category="embedding")) == 1


def test_role_category_filters_candidates(mem_db):
    """select_upstreams 按 role→ROLE_CATEGORY 过滤：LLM 角色选不中 embedding 上游，反之亦然。"""
    from app.services.gateway import upstreams as up_svc
    from app.services.gateway import rules as rule_svc

    llm = _mk_upstream(name="llm", category="llm", base_url="https://l/v1",
                       models=["chat-a"])
    emb = _mk_upstream(name="emb", category="embedding", base_url="https://e/v1",
                       models=["emb-a"])
    # 同一默认规则同时含 llm 与 embedding 上游
    rule_svc.create_rule({"model_pattern": "*", "upstreams": [llm["id"], emb["id"]],
                          "is_default": True, "priority": 10})

    # embed 角色 → 只命中 embedding 上游
    _, cand = rule_svc.select_upstreams("emb-a", role="embed")
    assert [c["id"] for c in cand] == [emb["id"]]
    # chat 角色 → 只命中 llm 上游
    _, cand2 = rule_svc.select_upstreams("chat-a", role="chat_answer")
    assert [c["id"] for c in cand2] == [llm["id"]]
    # 跨类误选防护：chat 角色请求 embedding 模型 → 无候选（不会发往 embedding 上游）
    _, cand3 = rule_svc.select_upstreams("emb-a", role="chat_answer")
    assert cand3 == []


# ============================================
# WS2 传输适配：URL / 请求 / 响应（按 kind）
# ============================================

def test_build_upstream_url_by_kind():
    from app.services.gateway import upstreams as up_svc

    oai = {"protocol": "openai", "base_url": "https://api.example/v1"}
    oll = {"protocol": "ollama", "base_url": "http://localhost:11434"}
    assert up_svc.build_upstream_url(oai, "embeddings") == "https://api.example/v1/embeddings"
    assert up_svc.build_upstream_url(oll, "embeddings") == "http://localhost:11434/api/embeddings"
    assert up_svc.build_upstream_url(oai, "rerank") == "https://api.example/v1/reranks"
    # chat 主链路零改动
    assert up_svc.build_upstream_url(oai, "chat") == "https://api.example/v1/chat/completions"
    assert up_svc.build_upstream_url(oll) == "http://localhost:11434/api/chat"


def test_build_request_embeddings_openai_and_ollama():
    from app.services.gateway import upstream as transport

    oai = {"protocol": "openai", "api_key": "sk-x"}
    headers, payload = transport.build_request(
        oai, {"model": "req", "input": ["a", "b"], "_gw_role": "embed"},
        "emb-1", stream=False, kind="embeddings")
    assert headers["Authorization"] == "Bearer sk-x"
    assert payload["model"] == "emb-1" and payload["input"] == ["a", "b"]
    assert "_gw_role" not in payload and "stream" not in payload

    oll = {"protocol": "ollama", "api_key": ""}
    h2, p2 = transport.build_request(oll, {"model": "req", "input": "hi"},
                                     "emb-1", stream=False, kind="embeddings")
    assert h2 == {} and p2 == {"model": "emb-1", "input": "hi"}


def test_build_request_rerank():
    from app.services.gateway import upstream as transport

    ch = {"protocol": "openai", "api_key": "sk-r"}
    headers, payload = transport.build_request(
        ch, {"model": "req", "query": "q", "documents": ["d1", "d2"], "top_n": 2,
             "_gw_role": "rerank"},
        "rr-1", stream=False, kind="rerank")
    assert headers["Authorization"] == "Bearer sk-r"
    assert payload == {"model": "rr-1", "query": "q", "documents": ["d1", "d2"], "top_n": 2}


def test_normalize_embeddings_and_rerank():
    from app.services.gateway import upstream as transport

    oai = {"protocol": "openai", "name": "emb-up"}
    # openai 兼容已是 list 形态 → 透传
    raw = {"object": "list", "model": "emb-1",
           "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
           "usage": {"prompt_tokens": 2, "total_tokens": 2}}
    assert transport.normalize_response(oai, 200, raw, "embeddings") is raw
    # ollama {embedding:[..]} → 归一为 OpenAI list 形态
    oll = {"protocol": "ollama", "name": "oll-up"}
    out = transport.normalize_response(oll, 200, {"embedding": [0.5, 0.6]}, "embeddings")
    assert out["object"] == "list"
    assert out["data"][0]["embedding"] == [0.5, 0.6]

    # rerank：顶层 results 与 output.results 两种结构
    rr = transport.normalize_response(oai, 200, {"model": "rr-1", "results": [
        {"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}]}, "rerank")
    assert rr["results"][0] == {"index": 1, "relevance_score": 0.9}
    rr2 = transport.normalize_response(oai, 200, {"output": {"results": [
        {"index": 0, "score": 0.7}]}}, "rerank")
    assert rr2["results"] == [{"index": 0, "relevance_score": 0.7}]


def test_probe_uses_category_kind_not_chat(mem_db, monkeypatch):
    """连通性探测须按上游分类选择端点：embedding→/embeddings、rerank→/reranks。

    回归：改造前 _probe 一律发 chat 请求到 /chat/completions，而 embedding/rerank
    上游 Base URL 不提供该端点，导致测试按钮误报 400/404。
    """
    from app.services.gateway import upstreams as up_svc

    emb = _mk_upstream(name="emb", category="embedding", models=["text-embedding-v4"])
    rr = _mk_upstream(name="rr", category="rerank", models=["qwen3-rerank"])
    llm = _mk_upstream(name="llm", category="llm", models=["demo"])

    cap = _install_fake_async_client(monkeypatch, up_svc, 200, {"ok": True})

    asyncio.run(up_svc.test_connection(emb["id"]))
    assert cap["url"].endswith("/embeddings")
    assert cap["body"]["input"] == ["ping"] and "messages" not in cap["body"]

    asyncio.run(up_svc.test_connection(rr["id"]))
    assert cap["url"].endswith("/reranks")
    assert cap["body"]["query"] == "ping" and cap["body"]["documents"] == ["pong"]

    asyncio.run(up_svc.test_connection(llm["id"]))
    assert cap["url"].endswith("/chat/completions")
    assert cap["body"]["messages"] == [{"role": "user", "content": "ping"}]


# ============================================
# WS2 代理端到端转发（mock httpx，不出网）
# ============================================

def _install_fake_async_client(monkeypatch, module, status_code, payload):
    """把 module.httpx.AsyncClient 替换为返回固定响应的假客户端，并捕获出网 URL/请求体。"""
    captured = {}

    class _Resp:
        def __init__(self):
            self.status_code = status_code
            self.text = json.dumps(payload)

        def json(self):
            return payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured.update(url=url, headers=headers, body=json)
            return _Resp()

    monkeypatch.setattr(module.httpx, "AsyncClient", _Client)
    return captured


def test_proxy_forwards_embeddings(mem_db, monkeypatch):
    from app.services.gateway import keys as key_svc
    from app.services.gateway import proxy
    from app.services.gateway import rules as rule_svc

    emb = _mk_upstream(name="emb", category="embedding", base_url="https://e/v1",
                       models=["emb-1"])
    rule_svc.create_rule({"model_pattern": "*", "upstreams": [emb["id"]],
                          "is_default": True, "priority": 10})
    key = key_svc.ensure_internal_key()
    captured = _install_fake_async_client(monkeypatch, proxy, 200, {
        "object": "list", "model": "emb-1",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
        "usage": {"prompt_tokens": 3, "total_tokens": 3}})

    body = {"model": "emb-1", "input": ["hello world"]}
    ctx = proxy.prepare(body, key, "127.0.0.1", kind="embeddings")
    assert ctx.kind == "embeddings" and ctx.role == "embed"
    status, data = asyncio.run(proxy.json_response_kind(ctx, "embeddings"))

    assert status == 200
    assert data["object"] == "list"
    assert data["data"][0]["embedding"] == [0.1, 0.2, 0.3]
    assert captured["url"] == "https://e/v1/embeddings"
    assert captured["body"]["model"] == "emb-1"
    # 计量入日志（role=embed）
    logs = db.query("SELECT role, status, prompt_tokens FROM gateway_logs")
    assert any(r["role"] == "embed" and r["status"] == "ok" for r in logs)


def test_proxy_forwards_rerank(mem_db, monkeypatch):
    from app.services.gateway import keys as key_svc
    from app.services.gateway import proxy
    from app.services.gateway import rules as rule_svc

    rr = _mk_upstream(name="rr", category="rerank", base_url="https://r/v1",
                      models=["rr-1"])
    rule_svc.create_rule({"model_pattern": "*", "upstreams": [rr["id"]],
                          "is_default": True, "priority": 10})
    key = key_svc.ensure_internal_key()
    captured = _install_fake_async_client(monkeypatch, proxy, 200, {
        "model": "rr-1", "results": [
            {"index": 1, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.2}]})

    body = {"model": "rr-1", "query": "q", "documents": ["d0", "d1"], "top_n": 2}
    ctx = proxy.prepare(body, key, "127.0.0.1", kind="rerank")
    assert ctx.kind == "rerank" and ctx.role == "rerank"
    status, data = asyncio.run(proxy.json_response_kind(ctx, "rerank"))

    assert status == 200
    assert data["results"][0] == {"index": 1, "relevance_score": 0.9}
    assert captured["url"] == "https://r/v1/reranks"
    assert captured["body"]["query"] == "q"


# ============================================
# WS2 三工厂网关未就绪回退直连
# ============================================

def test_embedding_gateway_client_none_when_no_upstream(mem_db):
    from app.core.embeddings import EmbeddingFactory

    # 空库无 embedding 上游 → 网关客户端返回 None（触发回退直连）
    assert EmbeddingFactory._gateway_embeddings_client("embed") is None


def test_embedding_create_for_role_falls_back_to_direct(mem_db, monkeypatch):
    from app.core import embeddings as emb_mod

    calls = {}

    def fake_create(provider=None, config=None):
        calls["args"] = (provider,) if provider is not None else ()
        calls["config"] = config
        return "DIRECT"

    monkeypatch.setattr(emb_mod.EmbeddingFactory, "create", staticmethod(fake_create))
    out = emb_mod.EmbeddingFactory.create_for_role(
        "embed", fallback_provider="custom", fallback_config={"model": "x"})
    assert out == "DIRECT"
    assert calls["args"] == ("custom",) and calls["config"] == {"model": "x"}


def test_embedding_gateway_client_positive(mem_db):
    from app.core.embeddings import EmbeddingFactory
    from app.services.gateway import rules as rule_svc

    emb = _mk_upstream(name="emb", category="embedding", base_url="https://e/v1",
                       models=["emb-1"])
    rule_svc.create_rule({"model_pattern": "*", "upstreams": [emb["id"]],
                          "is_default": True, "priority": 10})
    inst = EmbeddingFactory._gateway_embeddings_client("embed")
    assert inst is not None
    assert getattr(inst, "model", None) == "emb-1"


def test_reranker_gateway_none_when_no_upstream(mem_db):
    from app.core import reranker

    assert reranker._score_via_gateway("q", ["a", "b"]) is None


def test_reranker_gateway_positive_scores(mem_db, monkeypatch):
    from app.core import reranker
    from app.services.gateway import rules as rule_svc

    rr = _mk_upstream(name="rr", category="rerank", base_url="https://r/v1",
                      models=["rr-1"])
    rule_svc.create_rule({"model_pattern": "*", "upstreams": [rr["id"]],
                          "is_default": True, "priority": 10})

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"results": [{"index": 1, "relevance_score": 0.8},
                                {"index": 0, "relevance_score": 0.3}]}

    class _Client:
        def post(self, url, json=None, headers=None):
            assert url.endswith("/reranks")
            return _Resp()

    monkeypatch.setattr(reranker, "_get_http_client", lambda: _Client())
    scores = reranker._score_via_gateway("q", ["d0", "d1"])
    assert scores == [0.3, 0.8]


def test_ocr_gateway_none_when_no_upstream(mem_db):
    from app.core import ocr

    out = asyncio.run(ocr._ocr_via_gateway("data:image/png;base64,AAA"))
    assert out is None
