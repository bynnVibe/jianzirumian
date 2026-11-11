"""网关三大改造的回归测试（内存 DB，不出网）：

  D1 密钥分层：公共密钥/用户密钥/内部密钥的出网解析优先级、用户密钥强制绑定、
     公共密钥每用户日限额与降级、user_quota_status 友好提示；
  D2 主备路由：primary_backup 策略校验（主路由须属上游列表）+ 连续失败达阈值切备 +
     成功回切；
  D3 安全事件闭环：record_event 落库 → list_events(alert_only) 阈值过滤 →
     ack → 模型建议（降级规则化）→ resolve 留痕 → reopen。
"""
import asyncio
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


# ============================================
# D1 密钥分层与出网解析
# ============================================

def test_user_key_requires_bound_users(mem_db):
    """用户密钥生成时强制绑定至少 1 个用户。"""
    from app.services.gateway import keys as key_svc
    with pytest.raises(ValueError):
        key_svc.create_key("空绑定", key_type="user", bound_users=[])


def test_resolve_outbound_priority_user_over_public(mem_db):
    """出网解析优先级：用户绑定密钥 > 公共密钥 > 内部密钥。"""
    from app.services.gateway import keys as key_svc

    pub = key_svc.create_key("公共池", key_type="public", daily_request_limit=100)
    uk = key_svc.create_key("张三专属", key_type="user", bound_users=["u1"])

    plain, ktype = key_svc.resolve_outbound_key("u1")
    assert ktype == "user"
    assert plain == uk["key"]

    # 未绑定用户密钥的用户 → 公共密钥
    plain2, ktype2 = key_svc.resolve_outbound_key("u9")
    assert ktype2 == "public"
    assert plain2 == pub["key"]


def test_resolve_outbound_falls_back_internal(mem_db):
    """无任何公共/用户密钥 → 兜底内部密钥（永不为空）。"""
    from app.services.gateway import keys as key_svc
    plain, ktype = key_svc.resolve_outbound_key("u1")
    assert ktype == "internal"
    assert plain


def test_user_key_exhausted_degrades_to_public(mem_db):
    """用户密钥总配额用尽 → 出网自动降级公共密钥（不阻断使用）。"""
    from app.services.gateway import keys as key_svc

    pub = key_svc.create_key("公共池", key_type="public")
    uk = key_svc.create_key("张三专属", key_type="user", bound_users=["u1"], token_quota=100)
    # 用尽用户密钥总配额
    key_svc.consume_tokens(uk["id"], 100)

    plain, ktype = key_svc.resolve_outbound_key("u1")
    assert ktype == "public"
    assert plain == pub["key"]

    # 友好提示：降级但不阻断
    status = key_svc.user_quota_status("u1")
    assert status["key_type"] == "public"
    assert status["degraded"] is True
    assert status["exhausted"] is False
    assert "降级" in status["message"]


def test_public_daily_limit_rejects_with_contact_admin(mem_db):
    """公共密钥每用户日请求限额用尽 → QuotaRejected(daily_limit_exceeded)，提示联系管理员。"""
    from app.services.gateway import keys as key_svc

    pub = key_svc.create_key("公共池", key_type="public", daily_request_limit=2)
    row = key_svc.get_key(pub["id"])
    # 未达限额：放行
    key_svc.enforce_user_quota(row, "u1")
    # 该用户当日已用满 2 次
    key_svc.record_usage(pub["id"], "u1", requests=2)
    with pytest.raises(key_svc.QuotaRejected) as ei:
        key_svc.enforce_user_quota(key_svc.get_key(pub["id"]), "u1")
    assert ei.value.code == "daily_limit_exceeded"
    assert "联系管理员" in str(ei.value)

    # 每用户独立计量：另一用户不受影响
    key_svc.enforce_user_quota(key_svc.get_key(pub["id"]), "u2")


def test_public_daily_limit_is_per_user(mem_db):
    """日限额按「每用户」独立计量（get_day_usage 区分 user_id）。"""
    from app.services.gateway import keys as key_svc
    pub = key_svc.create_key("公共池", key_type="public", daily_token_limit=1000)
    key_svc.record_usage(pub["id"], "u1", tokens=400)
    key_svc.record_usage(pub["id"], "u2", tokens=900)
    assert key_svc.get_day_usage(pub["id"], "u1")["tokens"] == 400
    assert key_svc.get_day_usage(pub["id"], "u2")["tokens"] == 900

    status = key_svc.user_quota_status("u3")
    assert status["key_type"] == "public"
    assert status["exhausted"] is False


def test_internal_key_not_rate_limited(mem_db):
    """内部密钥为系统兜底通道，不受每用户配额限制。"""
    from app.services.gateway import keys as key_svc
    from app.services.gateway.keys import _row_to_dict

    key_svc.ensure_internal_key()
    internal_rows = db.query("SELECT * FROM gateway_keys WHERE key_type='internal'")
    info = _row_to_dict(internal_rows[0])
    # internal 类型 enforce 直接返回，不抛异常即通过
    key_svc.enforce_user_quota(info, "u1")



# ============================================
# D2 主备路由策略
# ============================================

def _rows(*specs):
    """构造上游排序输入行：specs = [(id, fail_count, health), ...]"""
    return [{"id": i, "name": i, "fail_count": f, "health": h, "weight": 100}
            for (i, f, h) in specs]


def test_primary_backup_requires_primary_in_upstreams(mem_db):
    """主备路由：主路由必须来自已选上游列表，否则报错。"""
    from app.services.gateway import rules as rule_svc
    with pytest.raises(ValueError):
        rule_svc.create_rule({
            "model_pattern": "*", "upstreams": ["a", "b"],
            "lb_strategy": "primary_backup", "primary_upstream": "zzz",
        })


def test_primary_backup_defaults_and_persists(mem_db):
    """未显式指定主路由时默认取上游列表首个；阈值默认 3。"""
    from app.services.gateway import rules as rule_svc
    rule = rule_svc.create_rule({
        "model_pattern": "*", "upstreams": ["a", "b", "c"],
        "lb_strategy": "primary_backup",
    })
    assert rule["lb_strategy"] == "primary_backup"
    assert rule["primary_upstream"] == "a"
    assert rule["failover_threshold"] == 3


def test_primary_first_when_healthy(mem_db):
    """主路由健康（失败数 < 阈值）→ 主路由领衔。"""
    from app.services.gateway import rules as rule_svc
    rule = {"primary_upstream": "a", "failover_threshold": 3}
    rows = _rows(("a", 0, "healthy"), ("b", 0, "healthy"), ("c", 0, "healthy"))
    ordered = rule_svc._order_by_strategy(rows, "primary_backup", rule)
    assert ordered[0]["id"] == "a"


def test_failover_to_backup_on_threshold(mem_db):
    """主路由连续失败达阈值 → 后续请求切换到备用路由领衔。"""
    from app.services.gateway import rules as rule_svc
    rule = {"primary_upstream": "a", "failover_threshold": 3}
    rows = _rows(("a", 3, "healthy"), ("b", 0, "healthy"), ("c", 0, "healthy"))
    ordered = rule_svc._order_by_strategy(rows, "primary_backup", rule)
    assert ordered[0]["id"] != "a"
    assert ordered[-1]["id"] == "a"  # 主路由降级到末位兜底


def test_failover_when_primary_down(mem_db):
    """主路由已熔断(down) → 即使失败数未达阈值也切备。"""
    from app.services.gateway import rules as rule_svc
    rule = {"primary_upstream": "a", "failover_threshold": 5}
    rows = _rows(("a", 1, "down"), ("b", 0, "healthy"))
    ordered = rule_svc._order_by_strategy(rows, "primary_backup", rule)
    assert ordered[0]["id"] == "b"


def test_auto_failback_after_recovery(mem_db):
    """主路由恢复（fail_count 清零）→ 自动回切主路由领衔。"""
    from app.services.gateway import rules as rule_svc
    rule = {"primary_upstream": "a", "failover_threshold": 3}
    recovered = _rows(("a", 0, "healthy"), ("b", 0, "healthy"))
    ordered = rule_svc._order_by_strategy(recovered, "primary_backup", rule)
    assert ordered[0]["id"] == "a"


# ============================================
# D3 安全事件闭环
# ============================================

def _mk_event(sec, risk_level="critical", risk_score=100, role="chat_answer"):
    return sec.record_event(
        log_id="log-1", client_ip="1.2.3.4", user_id="u1", role=role,
        model="m1", key_name="公共池", risk_level=risk_level, risk_score=risk_score,
        findings=[{"rule_name": "Unicode 隐写字符", "category": "unicode_steg",
                   "category_label": "Unicode 隐写", "severity": risk_level,
                   "matched": "\\u200b"}],
        audit_action="audit", settings={})


def test_record_and_list_events(mem_db):
    from app.services.gateway import security_events as sec
    eid = _mk_event(sec)
    assert eid
    res = sec.list_events()
    assert res["total"] == 1
    assert res["pending"] == 1
    assert res["events"][0]["finding_count"] == 1


def test_alert_only_filters_by_severity(mem_db):
    """alert_only 只返回 pending+未 acked+达阈值的事件。"""
    from app.services.gateway import security_events as sec
    _mk_event(sec, risk_level="low", risk_score=20)       # 低于 medium 阈值
    crit = _mk_event(sec, risk_level="critical", risk_score=100)

    res = sec.list_events(alert_only=True, min_severity="medium")
    ids = [e["id"] for e in res["events"]]
    assert crit in ids
    assert res["unacked_alerts"] == 1  # 仅 critical 计入告警

    # 确认后不再进入告警队列
    sec.ack_events([crit])
    res2 = sec.list_events(alert_only=True, min_severity="medium")
    assert res2["events"] == []
    assert res2["unacked_alerts"] == 0


def test_generate_suggestion_fallback(mem_db, monkeypatch):
    """模型不可用时降级为规则化建议（闭环不断），并落库 suggested_at。"""
    from app.services.gateway import security_events as sec
    import app.core.llm as llm_mod

    def _boom(*a, **k):
        raise RuntimeError("模型不可用")
    monkeypatch.setattr(llm_mod.LLMFactory, "create_for_role", _boom)

    eid = _mk_event(sec)
    result = asyncio.run(sec.generate_suggestion(eid))
    assert result["success"] is True
    ev = result["event"]
    assert ev["suggestion"]
    assert "规则化建议" in ev["suggestion"]
    assert "阻断" in ev["suggestion"]  # critical → 推荐阻断
    assert ev["suggested_at"]


def test_resolve_leaves_trail_and_reopen(mem_db):
    """处理留痕：status→resolved + resolution/resolved_by/resolved_at；可重开。"""
    from app.services.gateway import security_events as sec
    eid = _mk_event(sec)
    ev = sec.resolve_event(eid, resolution="blocked", note="已阻断并轮换密钥",
                           resolved_by="admin")
    assert ev["status"] == "resolved"
    assert ev["resolution"] == "blocked"
    assert ev["resolution_label"] == "已阻断"
    assert ev["resolved_by"] == "admin"
    assert ev["resolved_at"]
    assert ev["acked"] is True

    # 已处理不再计入 pending
    assert sec.list_events()["pending"] == 0

    # 重开回到 pending
    re_ev = sec.reopen_event(eid)
    assert re_ev["status"] == "pending"
    assert re_ev["resolution"] == ""
    assert sec.list_events()["pending"] == 1


def test_resolve_rejects_unknown_resolution(mem_db):
    from app.services.gateway import security_events as sec
    eid = _mk_event(sec)
    with pytest.raises(ValueError):
        sec.resolve_event(eid, resolution="not-a-valid-action")
