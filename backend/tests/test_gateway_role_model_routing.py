"""网关业务路由/模型解析/内部密钥/日志 role 的回归测试（内存 DB，不出网）。

覆盖四项改造：
  Q1  ROLES 登记记忆抽取等后台角色；未绑定角色回退默认规则并在规则上游清单内解析模型；
  Q4  create_for_role 模型优先级：显式 model > 业务路由绑定 > 网关默认模型（须在清单内）
      > 规则内随机均衡；resolve_chat_rule 供聊天页模型下拉；
  Q2  内部密钥 meta（不含明文）与 rotate（新建→更新 settings→删旧行）；
  Q3  gateway_logs 的 role 入库/摘要/过滤。
"""
import sqlite3

import pytest

from app.core import db


@pytest.fixture()
def mem_db(monkeypatch):
    """独立内存数据库：避免污染本地开发库"""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    yield conn
    conn.close()


@pytest.fixture()
def gateway_env(mem_db):
    """最小可用网关：1 个上游（m1/m2）+ 默认规则；网关默认模型设为清单外的幽灵模型。"""
    from app.services.gateway import settings as settings_svc
    from app.services.gateway import upstreams as up_svc
    from app.services.gateway import rules as rule_svc

    up = up_svc.create_upstream({
        "name": "up-a", "protocol": "openai", "tier": "free", "base_url": "",
        "models": ["m1", "m2"], "api_key": "sk-test",
    })
    rule = rule_svc.create_rule({
        "model_pattern": "*", "upstreams": [up["id"]], "is_default": True, "priority": 10,
    })
    settings_svc.save_settings({"default_model": "ghost-model"})
    return {"upstream": up, "rule": rule}


# ============================================
# Q1 角色登记与未绑定角色回退
# ============================================

def test_roles_include_background_roles():
    from app.services.gateway.rules import ROLES
    for r in ("memory_extract", "session_summary", "knowledge_summarize", "text_polish"):
        assert r in ROLES, f"后台角色 {r} 未登记，业务路由面板无法绑定"


def test_unbound_role_falls_back_to_default_rule(gateway_env):
    """记忆抽取等未绑定角色：回退默认规则，模型须在规则上游清单内（不再直撞 503）。"""
    from app.core.llm import LLMFactory

    llm = LLMFactory.create_for_role("memory_extract")
    assert llm.model in ("m1", "m2")
    assert llm.extra_body.get("_gw_role") == "memory_extract"
    assert llm.extra_body.get("_gw_rule_id") == gateway_env["rule"]["id"]


def test_default_model_not_in_rule_models_random_fallback(gateway_env, monkeypatch):
    """网关默认模型不在规则清单内 → 规则内随机均衡回退（随机可复现）。"""
    from app.core import llm as llm_mod

    monkeypatch.setattr(llm_mod.random, "choice", lambda seq: seq[-1])
    llm = llm_mod.LLMFactory.create_for_role("session_summary")
    assert llm.model == "m2"


def test_default_model_in_rule_models_is_used(gateway_env):
    """网关默认模型在规则清单内 → 直接使用。"""
    from app.services.gateway import settings as settings_svc
    from app.core.llm import LLMFactory

    settings_svc.save_settings({"default_model": "m1"})
    llm = LLMFactory.create_for_role("memory_extract")
    assert llm.model == "m1"


# ============================================
# Q4 模型解析优先级与聊天页规则匹配
# ============================================

def test_explicit_model_wins(gateway_env):
    """调用方显式 model（聊天页用户选模型）优先于一切默认解析。"""
    from app.core.llm import LLMFactory

    llm = LLMFactory.create_for_role("chat_answer", model="m2")
    assert llm.model == "m2"


def test_route_bound_model_used(gateway_env):
    """业务路由绑定模型优先于网关默认模型。"""
    from app.services.gateway import rules as rule_svc
    from app.core.llm import LLMFactory

    rule_svc.upsert_role_route("chat_answer", rule_id=gateway_env["rule"]["id"], model_name="m2")
    llm = LLMFactory.create_for_role("chat_answer")
    assert llm.model == "m2"


def test_resolve_chat_rule_bound(gateway_env):
    """chat_answer 绑定规则 → 下拉只列该规则上游模型，pinned_model 为绑定模型。"""
    from app.services.gateway import rules as rule_svc

    rule_svc.upsert_role_route("chat_answer", rule_id=gateway_env["rule"]["id"], model_name="m2")
    res = rule_svc.resolve_chat_rule("u1")
    assert res["rule_id"] == gateway_env["rule"]["id"]
    assert res["models"] == ["m1", "m2"]
    assert res["pinned_model"] == "m2"


def test_resolve_chat_rule_falls_back_to_default(gateway_env):
    """未绑定 chat_answer → 回退默认规则。"""
    from app.services.gateway import rules as rule_svc

    res = rule_svc.resolve_chat_rule("u1")
    assert res["rule_id"] == gateway_env["rule"]["id"]
    assert res["models"] == ["m1", "m2"]
    assert res["pinned_model"] == ""


def test_resolve_chat_rule_no_rules(mem_db):
    """无任何启用规则 → 空清单（前端隐藏下拉），不抛异常。"""
    from app.services.gateway import rules as rule_svc

    res = rule_svc.resolve_chat_rule("u1")
    assert res["rule_id"] == ""
    assert res["models"] == []


# ============================================
# Q2 系统内部密钥：meta 与轮换
# ============================================

def test_internal_key_meta_and_rotate(mem_db):
    from app.services.gateway import keys as key_svc
    from app.services.gateway import settings as settings_svc

    # 首次出网自动创建
    plain = key_svc.ensure_internal_key()
    meta = key_svc.get_internal_key_meta()
    assert meta["configured"] and meta["enabled"]
    assert meta["prefix"] and plain.startswith(meta["prefix"][:len(meta["prefix"])])
    assert "key" not in meta  # meta 不含明文

    # 轮换：新明文入 settings，旧密钥行删除
    info = key_svc.rotate_internal_key(created_by="tester")
    new_plain = info["key"]
    assert new_plain != plain
    assert settings_svc.get_settings()["internal_key"] == new_plain
    rows = db.query("SELECT key_hash FROM gateway_keys")
    assert len(rows) == 1  # 仅剩新行
    # 旧密钥立即失效
    with pytest.raises(key_svc.KeyRejected):
        key_svc.authenticate(plain)
    assert key_svc.authenticate(new_plain)["name"] == "系统内部调用"


def test_internal_key_self_heal_after_row_deleted(mem_db):
    """密钥行被删 → 下次出网自动重建。"""
    from app.services.gateway import keys as key_svc

    old = key_svc.ensure_internal_key()
    db.execute("DELETE FROM gateway_keys")
    new = key_svc.ensure_internal_key()
    assert new != old
    assert key_svc.authenticate(new)["enabled"]


# ============================================
# Q3 日志 role：入库/摘要/过滤
# ============================================

def test_logs_role_roundtrip_and_filter(mem_db):
    from app.services.gateway import logs as log_svc

    log_svc.write_log({"model_requested": "m1", "status": "error", "status_code": 503,
                       "role": "memory_extract", "error": "no_upstream"})
    log_svc.write_log({"model_requested": "m2", "status": "ok", "status_code": 200,
                       "role": "chat_answer"})
    log_svc.write_log({"model_requested": "m3", "status": "ok", "status_code": 200})

    res = log_svc.query_logs(role="memory_extract")
    assert res["total"] == 1
    assert res["items"][0]["role"] == "memory_extract"

    detail = log_svc.get_log(log_svc.query_logs(role="chat_answer")["items"][0]["id"])
    assert detail["role"] == "chat_answer"

    # 无 role（外部直连调用）不参与业务场景过滤
    assert log_svc.query_logs(role="")["total"] == 3
