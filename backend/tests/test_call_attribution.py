"""全链路用户归属（C2）：签名上下文、严格凭据解析、配额不降级、线程池上下文传播"""
import asyncio
import hashlib
import hmac
import json
import sqlite3
import time

import pytest

from app.core import db
from app.core import call_context
from app.core import gw_context


@pytest.fixture()
def mem_db(monkeypatch):
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


def _environment():
    from app.services.gateway import rules as rule_svc
    up = _mk_upstream()
    rule_svc.create_rule({"model_pattern": "*", "upstreams": [up["id"]],
                          "is_default": True, "priority": 10})
    return up


# ============================================
# 签名上下文：签发/验签/篡改/过期
# ============================================

def test_sign_verify_roundtrip(mem_db):
    ctx_json, sig = gw_context.sign_payload({"user_id": "u1", "role": "chat_answer"})
    data = gw_context.verify_payload(ctx_json, sig)
    assert data["user_id"] == "u1" and data["role"] == "chat_answer"
    assert data["ts"]


def test_tampered_payload_rejected(mem_db):
    ctx_json, sig = gw_context.sign_payload({"user_id": "u1"})
    forged = json.dumps({**json.loads(ctx_json), "user_id": "u2"},
                        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert gw_context.verify_payload(forged, sig) == {}
    assert gw_context.verify_payload(ctx_json, "0" * 64) == {}
    assert gw_context.verify_payload("", sig) == {}


def test_expired_signature_rejected(mem_db):
    data = {"user_id": "u1", "ts": str(int(time.time()) - 120)}
    ctx_json = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sig = hmac.new(gw_context._secret(), ctx_json.encode(), hashlib.sha256).hexdigest()
    assert gw_context.verify_payload(ctx_json, sig) == {}


def test_signed_extra_carries_call_context(mem_db):
    token = call_context.activate(actor_user_id="u1", trace_id="tr-1",
                                  job_id="job-9", origin="background")
    try:
        extra = gw_context.signed_extra("u1", "memory_extract")
        assert extra["_gw_user_id"] == "u1"
        data = gw_context.verify_payload(extra["_gw_ctx"], extra["_gw_sig"])
        assert data["trace_id"] == "tr-1"
        assert data["job_id"] == "job-9"
        assert data["origin"] == "background"
        assert data["billing_user_id"] == "u1"
        assert gw_context.signed_extra("", "chat_answer") == {}
    finally:
        call_context.deactivate(token)


# ============================================
# create_for_role：上下文兜底归属 + 按用户凭据 + 签名注入
# ============================================

def test_create_for_role_inherits_context_user_and_key(mem_db, monkeypatch):
    from app.core.llm import LLMFactory
    from app.services.gateway import keys as key_svc
    _environment()
    uk = key_svc.create_key("u1 专属", key_type="user", bound_users=["u1"])
    captured = {}

    def fake_build(provider, cfg, **kwargs):
        captured.update(provider=provider, cfg=cfg, **kwargs)
        return object()

    monkeypatch.setattr(LLMFactory, "_build", staticmethod(fake_build))
    token = call_context.activate(actor_user_id="u1", trace_id="tr-9", origin="chat")
    try:
        LLMFactory.create_for_role("chat_answer")  # 未显式传 user_id
    finally:
        call_context.deactivate(token)
    assert captured["cfg"]["api_key"] == uk["key"]  # 凭据归属 u1，非内部密钥
    extra = captured["extra_body"]
    assert extra["_gw_user_id"] == "u1"
    data = gw_context.verify_payload(extra["_gw_ctx"], extra["_gw_sig"])
    assert data["user_id"] == "u1" and data["trace_id"] == "tr-9"


def test_create_for_role_real_user_resolution_failure_not_silent(mem_db, monkeypatch):
    from app.core.llm import LLMFactory
    from app.services.gateway import keys as key_svc
    _environment()

    def boom(user_id=""):
        raise RuntimeError("凭据存储故障")

    monkeypatch.setattr(key_svc, "resolve_outbound_key", boom)
    with pytest.raises(RuntimeError):
        LLMFactory.create_for_role("chat_answer", "u1")
    # 空用户（系统/游客链路）仍允许兜底内部密钥
    monkeypatch.setattr(key_svc, "ensure_internal_key", lambda: "sk-internal")
    captured = {}
    monkeypatch.setattr(LLMFactory, "_build",
                        staticmethod(lambda p, cfg, **kw: captured.update(cfg=cfg) or object()))
    LLMFactory.create_for_role("chat_answer")
    assert captured["cfg"]["api_key"] == "sk-internal"


# ============================================
# proxy.prepare：验签信任归属 / 伪造不信任 / 配额不降级
# ============================================

def test_prepare_trusts_signed_attribution(mem_db):
    from app.services.gateway import keys as key_svc
    from app.services.gateway import proxy
    _environment()
    key = key_svc.create_key("k")
    token = call_context.activate(actor_user_id="u1", trace_id="tr-7", origin="chat")
    try:
        extra = gw_context.signed_extra("u1", "chat_answer")
    finally:
        call_context.deactivate(token)
    body = {"model": "m1", "messages": [], **extra}
    ctx = proxy.prepare(body, key["key"])
    assert ctx.user_id == "u1"
    assert ctx.billing_user_id == "u1"
    assert ctx.trace_id == "tr-7"
    assert ctx.attribution_source == "signed_context"


def test_prepare_forged_signature_not_trusted(mem_db):
    from app.services.gateway import keys as key_svc
    from app.services.gateway import proxy
    _environment()
    key = key_svc.create_key("k")
    body = {"model": "m1", "messages": [], "_gw_user_id": "victim",
            "_gw_ctx": json.dumps({"user_id": "victim", "ts": str(int(time.time()))}),
            "_gw_sig": "f" * 64}
    ctx = proxy.prepare(body, key["key"])
    assert ctx.user_id == ""  # 伪造归属不信任，不串用他人配额
    assert ctx.attribution_source == "signature_invalid"


def test_prepare_legacy_unsigned_hint_compatible(mem_db):
    from app.services.gateway import keys as key_svc
    from app.services.gateway import proxy
    _environment()
    key = key_svc.create_key("k")
    ctx = proxy.prepare({"model": "m1", "messages": [], "_gw_user_id": "u1"}, key["key"])
    assert ctx.user_id == "u1"
    assert ctx.attribution_source == "legacy_hint"


def test_prepare_quota_exhausted_never_degrades(mem_db):
    from app.services.gateway import keys as key_svc
    from app.services.gateway import proxy
    _environment()
    key_svc.create_key("公共池", key_type="public")
    uk = key_svc.create_key("专属", key_type="user", bound_users=["u1"], request_quota=1)
    key_svc.consume_request(uk["id"], "m1")  # 用尽请求配额
    with pytest.raises(proxy.GatewayError) as ei:
        proxy.prepare({"model": "m1", "messages": [], "_gw_user_id": "u1"}, uk["key"])
    assert ei.value.code == "quota_exceeded"  # 明确报错，不静默切换公共密钥


def test_quota_status_exhausted_bound_stays_user(mem_db):
    from app.services.gateway import keys as key_svc
    key_svc.create_key("公共池", key_type="public")
    uk = key_svc.create_key("专属", key_type="user", bound_users=["u1"], token_quota=100)
    key_svc.consume_tokens(uk["id"], 100)
    status = key_svc.user_quota_status("u1")
    assert status["key_type"] == "user"
    assert status["exhausted"] is True
    assert "不会自动降级" in status["message"]


# ============================================
# Embedding 按用户凭据 + 缓存不冻结单用户 key
# ============================================

def test_embeddings_client_per_user_key(mem_db, monkeypatch):
    from app.core import embeddings as emb
    from app.services.gateway import keys as key_svc
    from app.services.gateway import rules as rule_svc
    e = _mk_upstream(name="emb", category="embedding", base_url="https://e/v1",
                     models=["emb-1"])
    rule_svc.create_rule({"model_pattern": "*", "upstreams": [e["id"]],
                          "is_default": True, "priority": 10})
    k1 = key_svc.create_key("u1 专属", key_type="user", bound_users=["u1"])
    k2 = key_svc.create_key("u2 专属", key_type="user", bound_users=["u2"])
    built = []

    class FakeEmb:
        def __init__(self, **kwargs):
            built.append(kwargs)
            self.api_key = kwargs.get("api_key")

    monkeypatch.setattr(emb, "OpenAIEmbeddings", FakeEmb)
    emb._instance_cache.clear()
    t1 = call_context.activate(actor_user_id="u1")
    try:
        c1 = emb.EmbeddingFactory._gateway_embeddings_client("embed")
        c1_again = emb.EmbeddingFactory._gateway_embeddings_client("embed")
    finally:
        call_context.deactivate(t1)
    t2 = call_context.activate(actor_user_id="u2")
    try:
        c2 = emb.EmbeddingFactory._gateway_embeddings_client("embed")
    finally:
        call_context.deactivate(t2)
    assert c1.api_key == k1["key"]
    assert c2.api_key == k2["key"]
    assert c1 is c1_again  # 同用户复用缓存
    assert c1 is not c2    # 不同用户不共用客户端


# ============================================
# 重活线程池传播 CallContext
# ============================================

def test_heavy_pool_propagates_call_context(mem_db):
    from app.core.heavy_executor import run_in_heavy_pool

    async def run():
        token = call_context.activate(actor_user_id="u-bg", origin="background")
        try:
            return await run_in_heavy_pool(call_context.current_user_id)
        finally:
            call_context.deactivate(token)

    assert asyncio.run(run()) == "u-bg"


# ============================================
# 后台编译队列：发起人随队列持久化，worker 逐任务恢复归属
# （admin 触发笔记整理 → 编译出网计入 admin 绑定密钥而非系统内部）
# ============================================

def test_wiki_schedule_persists_actor(mem_db, monkeypatch):
    from app.services import wiki as wiki_mod
    monkeypatch.setattr(wiki_mod, "wiki_compile_enabled", lambda: True)
    monkeypatch.setattr(wiki_mod.wiki_service, "_ensure_worker", lambda: None)
    token = call_context.activate(actor_user_id="admin")
    try:
        wiki_mod.wiki_service.schedule_compile(
            source_image="note.png", full_text="养生笔记内容" * 200, kb_id="kb1",
            visibility="private", owner_id="owner-x", source_type="image", title="笔记")
    finally:
        call_context.deactivate(token)
    row = db.query_one("SELECT payload FROM wiki_compile_queue")
    payload = json.loads(row["payload"])
    assert payload["actor_user_id"] == "admin"  # 请求上下文优先于 owner


def test_wiki_worker_restores_actor_attribution(mem_db, monkeypatch):
    from datetime import datetime
    from app.services import wiki as wiki_mod
    seen = []

    async def _fake_core(self, **payload):
        seen.append(call_context.current_user_id())
        return {"id": "p1"}

    monkeypatch.setattr(wiki_mod.WikiService, "_compile_core", _fake_core)
    now = datetime.now().isoformat()
    # 任务1：显式发起人 admin；任务2：旧队列无 actor 字段 → 回落 owner_id
    for qid, actor, owner in (("q1", "admin", "owner-x"), ("q2", "", "u2")):
        payload = {"source_image": "img", "full_text": "t", "kb_id": "kb1",
                   "visibility": "private", "owner_id": owner, "source_type": "image",
                   "title": "笔记", "pdf_preview_path": None, "force": False,
                   "source_metadata": {}}
        if actor:
            payload["actor_user_id"] = actor
        db.execute(
            "INSERT INTO wiki_compile_queue (id, source_image, source_hash, payload,"
            " status, attempts, error, created_at, updated_at)"
            " VALUES (?, 'img', 'sha', ?, 'pending', 0, '', ?, ?)",
            (qid, json.dumps(payload, ensure_ascii=False), now, now))
    asyncio.run(wiki_mod.wiki_service._worker_loop())
    assert seen == ["admin", "u2"]  # 逐任务恢复发起人，不再全走系统内部
    assert call_context.current_user_id() == ""  # worker 结束后上下文还原，不串归属
    statuses = {r["id"]: r["status"] for r in db.query("SELECT id, status FROM wiki_compile_queue")}
    assert statuses == {"q1": "done", "q2": "done"}