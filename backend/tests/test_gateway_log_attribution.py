"""网关日志归属（C3）：归属字段落库、usage_event_id 幂等、四分类统计与筛选"""
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


def _add_user(uid, username):
    db.execute("INSERT INTO users (id, username, password_hash, salt, created_at, updated_at)"
               " VALUES (?,?,?,?,?,?)", (uid, username, "x", "x", time.time(), time.time()))


# ============================================
# write_log：归属字段落库 + usage_event_id 幂等
# ============================================

def test_write_log_persists_attribution(mem_db):
    from app.services.gateway import logs as log_svc
    log_id = log_svc.write_log({
        "key_id": "k1", "key_name": "专属", "model_requested": "m1",
        "status": "ok", "total_tokens": 120,
        "user_id": "u1", "billing_user_id": "u1", "trace_id": "tr-1",
        "span_id": "sp-1", "job_id": "job-1", "origin": "chat",
        "attribution_source": "signed_context", "usage_event_id": "evt-1",
    })
    detail = log_svc.get_log(log_id)
    assert detail["user_id"] == "u1" and detail["billing_user_id"] == "u1"
    assert detail["trace_id"] == "tr-1" and detail["span_id"] == "sp-1"
    assert detail["job_id"] == "job-1" and detail["origin"] == "chat"
    assert detail["attribution_source"] == "signed_context"
    assert detail["usage_event_id"] == "evt-1"
    assert detail["attribution"] == "user"


def test_usage_event_id_idempotent(mem_db):
    from app.services.gateway import logs as log_svc
    first = log_svc.write_log({"model_requested": "m1", "total_tokens": 100,
                               "usage_event_id": "evt-dup"})
    # 同一用量事件重试落库：返回已有 log_id，不重复计量
    second = log_svc.write_log({"id": "another-id", "model_requested": "m1",
                                "total_tokens": 100, "usage_event_id": "evt-dup"})
    assert second == first
    assert db.query_one("SELECT COUNT(*) AS n FROM gateway_logs")["n"] == 1
    assert db.query_one("SELECT SUM(total_tokens) AS t FROM gateway_logs")["t"] == 100


def test_legacy_rows_unattributed(mem_db):
    from app.services.gateway import logs as log_svc
    log_id = log_svc.write_log({"key_id": "gone", "key_name": "旧密钥",
                                "model_requested": "m1", "total_tokens": 50})
    summary = log_svc.query_logs()["items"][0]
    assert summary["user_id"] == ""
    assert summary["attribution"] == "unattributed"
    assert log_svc.get_log(log_id)["attribution"] == "unattributed"


# ============================================
# proxy.prepare → _finalize：归属随每条日志落库
# ============================================

def test_finalize_signed_context_attribution(mem_db):
    from app.services.gateway import keys as key_svc
    from app.services.gateway import proxy, logs as log_svc
    _environment()
    key = key_svc.create_key("k")
    token = call_context.activate(actor_user_id="u1", trace_id="tr-5", origin="chat")
    try:
        extra = gw_context.signed_extra("u1", "chat_answer")
    finally:
        call_context.deactivate(token)
    ctx = proxy.prepare({"model": "m1", "messages": [], **extra}, key["key"])
    proxy._finalize(ctx, status="ok", status_code=200,
                    prompt_tokens=10, completion_tokens=5)
    row = db.query_one("SELECT * FROM gateway_logs WHERE id=?", (ctx.log_id,))
    assert row["user_id"] == "u1" and row["billing_user_id"] == "u1"
    assert row["trace_id"] == "tr-5" and row["origin"] == "chat"
    assert row["attribution_source"] == "signed_context"
    assert row["usage_event_id"] == ctx.log_id
    assert log_svc.get_log(ctx.log_id)["attribution"] == "user"


def test_finalize_user_key_binding_fallback(mem_db):
    from app.services.gateway import keys as key_svc
    from app.services.gateway import proxy
    _environment()
    uk = key_svc.create_key("专属", key_type="user", bound_users=["u9"])
    # 无签名上下文：用户密钥的绑定用户即可靠归属证据
    ctx = proxy.prepare({"model": "m1", "messages": []}, uk["key"])
    assert ctx.user_id == "u9" and ctx.attribution_source == "key_binding"
    proxy._finalize(ctx, status="ok", status_code=200, prompt_tokens=1, completion_tokens=1)
    row = db.query_one("SELECT * FROM gateway_logs WHERE id=?", (ctx.log_id,))
    assert row["user_id"] == "u9" and row["attribution_source"] == "key_binding"


def test_finalize_internal_and_public_sources(mem_db):
    from app.services.gateway import keys as key_svc
    from app.services.gateway import proxy
    _environment()
    internal = key_svc.ensure_internal_key()
    ctx = proxy.prepare({"model": "m1", "messages": []}, internal)
    assert ctx.attribution_source == "system_internal" and ctx.user_id == ""
    pub = key_svc.create_key("公共池", key_type="public")
    ctx2 = proxy.prepare({"model": "m1", "messages": []}, pub["key"])
    assert ctx2.attribution_source == "public_shared" and ctx2.user_id == ""


# ============================================
# stats 四分类 + by_user + query_logs 筛选
# ============================================

def _seed_logs(mem_db):
    from app.services.gateway import keys as key_svc
    from app.services.gateway import logs as log_svc
    _add_user("u1", "alice")
    uk = key_svc.create_key("u1 专属", key_type="user", bound_users=["u1"])
    pub = key_svc.create_key("公共池", key_type="public")
    key_svc.ensure_internal_key()
    internal_id = db.query_one("SELECT id FROM gateway_keys WHERE key_type='internal'")["id"]
    log_svc.write_log({"key_id": uk["id"], "key_name": uk["name"], "model_requested": "m1",
                       "total_tokens": 300, "user_id": "u1", "attribution_source": "signed_context"})
    log_svc.write_log({"key_id": internal_id, "key_name": "内部", "model_requested": "m1",
                       "total_tokens": 100, "attribution_source": "system_internal"})
    log_svc.write_log({"key_id": pub["id"], "key_name": pub["name"], "model_requested": "m1",
                       "total_tokens": 60, "attribution_source": "public_shared"})
    log_svc.write_log({"key_id": "deleted-key", "key_name": "已删密钥", "model_requested": "m1",
                       "total_tokens": 40})


def test_stats_by_attribution_and_by_user(mem_db):
    from app.services.gateway import logs as log_svc
    _seed_logs(mem_db)
    stats = log_svc.stats(7)
    cats = {r["category"]: r for r in stats["by_attribution"]}
    assert cats["user"]["total"] == 1 and cats["user"]["tokens"] == 300
    assert cats["system"]["total"] == 1 and cats["system"]["tokens"] == 100
    assert cats["public"]["total"] == 1 and cats["public"]["tokens"] == 60
    assert cats["unattributed"]["total"] == 1 and cats["unattributed"]["tokens"] == 40
    assert stats["by_user"][0]["user_id"] == "u1"
    assert stats["by_user"][0]["username"] == "alice"
    assert stats["by_user"][0]["tokens"] == 300


def test_query_logs_attribution_filters(mem_db):
    from app.services.gateway import logs as log_svc
    _seed_logs(mem_db)
    assert log_svc.query_logs(attribution="user")["total"] == 1
    assert log_svc.query_logs(attribution="system")["total"] == 1
    assert log_svc.query_logs(attribution="public")["total"] == 1
    assert log_svc.query_logs(attribution="unattributed")["total"] == 1
    assert log_svc.query_logs(user_id="u1")["total"] == 1
    assert log_svc.query_logs(user_id="nobody")["total"] == 0
    items = log_svc.query_logs(attribution="user")["items"]
    assert items[0]["attribution"] == "user" and items[0]["user_id"] == "u1"