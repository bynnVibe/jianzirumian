"""受控缓存合同、失效、权限与网关接线回归；只使用内存数据库。"""
import asyncio
import copy
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.core import db
from app.services import cache_policy as policy
from app.services.gateway import semantic_cache as cache, proxy


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    monkeypatch.setattr(policy.settings, "ANSWER_CACHE_SIGNING_KEY", "test-only-" * 5)
    monkeypatch.setattr(cache, "semantic_cache_enabled", lambda: True)
    for uid in ("u1", "u2"):
        db.execute("INSERT INTO users(id,username,password_hash,salt,contact,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                   (uid, uid, "unused", "unused", uid, datetime.now().isoformat(), datetime.now().isoformat()))

    async def embed(_):
        return [1.0, 0.0]

    monkeypatch.setattr(cache, "_embed", embed)
    yield conn
    conn.close()


def body_for(query="解释光合作用的原理", *, user="u1", kind="model_exact", sources=None):
    messages = [{"role": "system", "content": "根据证据回答"}, {"role": "user", "content": query}]
    body = {"model": "model-a", "messages": messages}
    body["_gw_cache_contract"] = policy.sign_contract(
        user_id=user, query=query, messages=messages, model=body["model"],
        sources=sources or [], revision=policy.revisions(), use_knowledge=kind == "knowledge_semantic",
        decision={"kind": kind, "reason": "", "ttl_seconds": 86400})
    return body


def contract_for(**kw):
    body = body_for(**kw)
    contract, reason = policy.verify_contract(body, kw.get("user", "u1"))
    assert not reason
    return contract


ANSWER = "光合作用是植物利用光能，将二氧化碳和水转变成有机物并释放氧气的过程，包含光反应及碳同化阶段。"


def store(contract, query=None, answer=ANSWER, fingerprint="fp"):
    return asyncio.run(cache.store(contract["owner"], query or contract["query"], answer,
                                   contract=contract, fingerprint=fingerprint))


def lookup(contract, query=None, fingerprint="fp"):
    return asyncio.run(cache.lookup(contract["owner"], query or contract["query"],
                                    contract=contract, fingerprint=fingerprint))


@pytest.mark.parametrize("knowledge,search,kind", [(True, False, "knowledge_semantic"),
    (False, False, "model_exact"), (False, True, "bypass"), (True, True, "bypass")])
def test_mode_matrix(knowledge, search, kind):
    assert policy.decide("解释光合作用", use_knowledge=knowledge, use_search=search,
                         freshness="static", evidence_sufficient=True)["kind"] == kind


@pytest.mark.parametrize("options,reason", [
    ({"force_refresh": True}, "force_refresh"), ({"command": "skills"}, "capability_workflow"),
    ({"attachments": True}, "attachments"),
    ({"has_memory": True}, "personalized_context"), ({"tool_executed": True}, "tool_execution"),
    ({"freshness": "unknown"}, "freshness_unverified"),
    ({"use_knowledge": True}, "insufficient_evidence")])
def test_bypass(options, reason):
    assert policy.decide("解释光合作用", **({"freshness": "static"} | options))["reason"] == reason


def test_history_no_longer_bypasses_but_reference_does():
    # 整改2：同会话历史不再一刀切旁路；仅指代/续问类才依赖上下文旁路
    assert policy.decide("解释光合作用", freshness="static", has_history=True)["kind"] == "model_exact"
    assert policy.decide("刚才那个问题再解释下", freshness="static", has_history=True)["reason"] == "conversation_context"


def test_realtime_health_not_reset():
    from app.services.tools.router import route_tools
    assert route_tools("最新中医治疗进展", use_search=False, allowed_tools=["search_knowledge"]).is_realtime
    assert policy.decide("最新中医治疗进展", freshness="static")["kind"] == "bypass"


def test_contract_tampering_and_owner():
    body = body_for()
    assert policy.verify_contract(body, "u1")[0]
    assert policy.verify_contract(body, "u2")[1] == "invalid_owner"
    body["_gw_cache_contract"]["payload"]["owner"] = "u2"
    assert policy.verify_contract(body, "u2")[1] == "invalid_signature"


@pytest.mark.parametrize("field", ["messages", "model"])
def test_contract_binds_request(field):
    body = body_for()
    body[field] = [] if field == "messages" else "another-model"
    assert policy.verify_contract(body, "u1")[1] == "request_changed"


def test_no_key_no_cache(monkeypatch):
    monkeypatch.setattr(policy.settings, "ANSWER_CACHE_SIGNING_KEY", "")
    assert policy.verify_contract({}, "u1")[1] == "signing_not_configured"
    assert body_for()["_gw_cache_contract"] == {}


def test_exact_roundtrip_no_sliding_expiry():
    contract = contract_for()
    assert store(contract)
    before = db.query_one("SELECT * FROM semantic_cache_v2")
    for _ in range(12):
        assert lookup(contract)["answer"] == ANSWER
    after = db.query_one("SELECT * FROM semantic_cache_v2")
    assert before["expires_at"] == after["expires_at"]
    assert after["hit_count"] == 12
    assert lookup(contract, query="植物如何利用太阳光") is None
    assert lookup(contract, fingerprint="different-upstream") is None
    assert lookup(contract_for(user="u2")) is None


@pytest.mark.parametrize("expiry", ["", "invalid", "2000-01-01T00:00:00+00:00"])
def test_bad_expiry_never_hits(expiry):
    contract = contract_for()
    assert store(contract)
    db.execute("UPDATE semantic_cache_v2 SET expires_at=?", (expiry,))
    assert lookup(contract) is None


def test_safe_normalization():
    assert policy.normalize_question("  温度  -10°C ") == "温度 -10°C"
    for a, b in [("-10", "10"), ("1/2", "12"), ("不能", "能"), ("A", "a")]:
        assert policy.normalize_question(a) != policy.normalize_question(b)


def test_semantic_equivalence_required(monkeypatch):
    c = contract_for(kind="knowledge_semantic", sources=[{"text": "植物使用光能", "citation_index": 1}])
    assert store(c)

    async def no(*args):
        return False

    async def yes(*args):
        return True

    monkeypatch.setattr(policy, "equivalent", no)
    assert lookup(c, query="解释动物的呼吸作用") is None
    monkeypatch.setattr(policy, "equivalent", yes)
    assert lookup(c, query="请说明光合作用的原理")["sources"] == c["sources"]
    db.execute("UPDATE semantic_cache_v2 SET sources='[]'")
    assert lookup(c) is None


def test_permission_revision_invalidates():
    c = contract_for()
    assert store(c)
    db.execute("UPDATE users SET is_active=0 WHERE id='u1'")
    assert lookup(c) is None
    assert policy.verify_contract(body_for(), "u1")[1] == "permission_changed"


def test_context_evidence_model_fingerprints():
    c = contract_for()
    args = dict(model="a", upstream={"id": "u"}, rule={"id": "r"}, body={})
    original = policy.fingerprint(c, **args)
    # 整改2：指纹不再绑定上下文/证据快照（跨轮历史与检索排序差异不影响命中）
    for key in ("context_hash", "evidence_hash"):
        changed = copy.deepcopy(c)
        changed[key] = "changed"
        assert policy.fingerprint(changed, **args) == original
    for key in ("revisions",):
        changed = copy.deepcopy(c)
        changed[key] = "changed"
        assert policy.fingerprint(changed, **args) != original
    assert policy.fingerprint(c, **(args | {"upstream": {"id": "v"}})) != original
    assert policy.fingerprint(c, **(args | {"body": {"temperature": 0.1}})) != original


def test_no_legacy_public_or_unsigned_entry():
    c = contract_for()
    assert not asyncio.run(cache.store("u1", c["query"], ANSWER))
    assert not asyncio.run(cache.store("u1", c["query"], ANSWER, scope="public", contract=c, fingerprint="fp"))
    db.execute("INSERT INTO semantic_cache(id,user_id,question,question_norm,answer,created_at,last_used_at) "
               "VALUES('old','u1','q','q',?,'','')", (ANSWER,))
    assert lookup(c) is None
    assert cache.list_entries("u1") == []


def test_invalid_citation_not_cached():
    assert not store(contract_for(), answer=ANSWER + "[来源 99]")


def test_proxy_hit_sse_and_bypass(monkeypatch):
    ctx = proxy.Ctx()
    ctx.user_id, ctx.model_req, ctx.cache_enabled = "u1", "model-a", True
    ctx.forward_body, ctx.upstream, ctx.rule = body_for(), {"id": "up1"}, {}
    monkeypatch.setattr(proxy.up_svc, "effective", lambda x: x)
    assert asyncio.run(proxy._cache_lookup(ctx)) is None
    asyncio.run(proxy._cache_store(ctx, ANSWER))
    assert asyncio.run(proxy._cache_lookup(ctx)) == ANSWER
    assert ctx.cache_info["hit"]
    assert proxy._synthetic_completion(ctx, ANSWER)["_gw_cache_info"]["expires_at"]
    assert proxy._synthetic_stream(ctx, ANSWER)[-1] == "data: [DONE]\n\n"
    ctx.forward_body = {"_gw_cache_query": ctx.cache_query}
    assert asyncio.run(proxy._cache_lookup(ctx)) is None


def test_monitor_eligible_denominator():
    cache.record_event(hit=True)
    cache.record_event(hit=False)
    cache.record_event(hit=False, outcome="bypass", reason="web_search_enabled")
    cache.record_event(hit=False, outcome="invalidated", reason="expired")
    ov = cache.monitor_stats()["overview"]
    assert ov["requests"] == 2 and ov["hit_rate"] == 0.5 and ov["bypasses"] == 1


def test_migration_idempotent(isolated):
    assert store(contract_for())
    db.init_db(isolated)
    assert cache.count("u1") == 1
    assert db.query_one("SELECT COUNT(*) n FROM schema_migrations WHERE version='answer_cache_v2'")["n"] == 1
