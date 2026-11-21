"""可观测性告警实时分析引擎回归测试（内存 DB，不出网）。

覆盖计划第二节：
  A1 性能规则：error_rate / p95_latency / token_overuse 触发与样本门槛、severity 分级；
  A2 质量规则：eval_regression（judge_score 归一化）/ dislike_rate / online_quality；
  A3 生命周期：同指纹去重、冷却期、指标回落自动 resolved；
  A4 归因证据采集（确定性）：失败 trace 分布 / 错误 span Top / 慢 trace 分解；
  A5 LLM 分析：成功结构化输出、失败/坏 JSON 降级规则化模板；
  A6 API：/alerts 列表 + ack/resolve/reanalyze + 越权 403。
"""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta

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


def _metrics(total=10, error_rate=0.0, p95=1000, avg_tokens=1000):
    return {
        "overview": {"total": total, "avg_tokens_per_request": avg_tokens,
                     "token_warn_threshold": 6000},
        "golden_signals": {"error_rate": error_rate, "duration": {"p95_ms": p95}},
        "segment_breakdown": [],
    }


def _monitor(likes=0, dislikes=0, runs=None):
    return {"quality": {"likes": likes, "dislikes": dislikes, "eval_runs": runs or []}}


def _patch_sources(monkeypatch, metrics=None, monitor=None, online=None):
    """把规则评估的三个数据源替换为受控输入（不触碰真实聚合）。"""
    from app.core import observability as obs
    from app.services import online_eval
    monkeypatch.setattr(obs, "metrics", lambda *a, **k: metrics or _metrics())
    monkeypatch.setattr(obs, "monitor_metrics", lambda *a, **k: monitor or _monitor())
    monkeypatch.setattr(online_eval, "aggregate", lambda *a, **k: online or {
        "samples": 0, "avg_geval": None})


def _rule(rules, metric):
    return next(r for r in rules if r["metric"] == metric)


# ============================================
# A1 性能规则
# ============================================
def test_error_rate_triggers_and_severity(mem_db, monkeypatch):
    from app.services import alert_analysis as aa
    _patch_sources(monkeypatch, metrics=_metrics(total=10, error_rate=0.2))
    r = _rule(aa._collect_rule_results(), "error_rate")
    assert r["triggered"] and r["severity"] == "critical"  # 0.2 ≥ 2×0.10
    _patch_sources(monkeypatch, metrics=_metrics(total=10, error_rate=0.12))
    r = _rule(aa._collect_rule_results(), "error_rate")
    assert r["triggered"] and r["severity"] == "warning"


def test_error_rate_sample_gate(mem_db, monkeypatch):
    from app.services import alert_analysis as aa
    _patch_sources(monkeypatch, metrics=_metrics(total=3, error_rate=0.9))
    r = _rule(aa._collect_rule_results(), "error_rate")
    assert not r["triggered"]  # 样本 < ALERT_MIN_SAMPLES(5) 不误报
    assert r["evidence"] == {}


def test_p95_latency_rule(mem_db, monkeypatch):
    from app.services import alert_analysis as aa
    _patch_sources(monkeypatch, metrics=_metrics(p95=40000))
    r = _rule(aa._collect_rule_results(), "p95_latency")
    assert r["triggered"] and r["severity"] == "warning"
    _patch_sources(monkeypatch, metrics=_metrics(p95=70000))
    assert _rule(aa._collect_rule_results(), "p95_latency")["severity"] == "critical"
    _patch_sources(monkeypatch, metrics=_metrics(p95=20000))
    assert not _rule(aa._collect_rule_results(), "p95_latency")["triggered"]


def test_token_overuse_rule(mem_db, monkeypatch):
    from app.services import alert_analysis as aa
    _patch_sources(monkeypatch, metrics=_metrics(avg_tokens=7000))
    r = _rule(aa._collect_rule_results(), "token_overuse")
    assert r["triggered"] and r["threshold"] == 6000
    _patch_sources(monkeypatch, metrics=_metrics(avg_tokens=6000))
    assert not _rule(aa._collect_rule_results(), "token_overuse")["triggered"]  # 严格大于


# ============================================
# A2 质量规则
# ============================================
def test_eval_regression_rule_and_judge_normalization(mem_db, monkeypatch):
    from app.services import alert_analysis as aa
    _patch_sources(monkeypatch, monitor=_monitor(runs=[{"avg": {"answer_relevance": 0.4}}]))
    assert _rule(aa._collect_rule_results(), "eval_regression")["triggered"]
    # judge_score 为 1-5 分制：2.0/5=0.4 < 0.6 同样触发
    _patch_sources(monkeypatch, monitor=_monitor(runs=[{"avg": {"judge_score": 2.0}}]))
    assert _rule(aa._collect_rule_results(), "eval_regression")["triggered"]
    _patch_sources(monkeypatch, monitor=_monitor(runs=[{"avg": {"answer_relevance": 0.9}}]))
    assert not _rule(aa._collect_rule_results(), "eval_regression")["triggered"]


def test_dislike_rate_rule_and_sample_gate(mem_db, monkeypatch):
    from app.services import alert_analysis as aa
    _patch_sources(monkeypatch, monitor=_monitor(likes=2, dislikes=8))
    r = _rule(aa._collect_rule_results(), "dislike_rate")
    assert r["triggered"] and r["value"] == 0.8
    _patch_sources(monkeypatch, monitor=_monitor(likes=1, dislikes=2))  # 样本 3 < 5
    assert not _rule(aa._collect_rule_results(), "dislike_rate")["triggered"]


def test_online_quality_rule(mem_db, monkeypatch):
    from app.services import alert_analysis as aa
    _patch_sources(monkeypatch, online={"samples": 3, "avg_geval": 0.4})
    assert _rule(aa._collect_rule_results(), "online_quality")["triggered"]
    _patch_sources(monkeypatch, online={"samples": 2, "avg_geval": 0.1})  # 样本 < 3
    assert not _rule(aa._collect_rule_results(), "online_quality")["triggered"]


# ============================================
# A3 生命周期：去重 / 冷却 / 自动恢复
# ============================================
def _fake_analysis(monkeypatch, aa):
    async def fake(rule):
        return {"cause": "测试原因", "suggestions": ["测试建议"], "degraded": False}
    monkeypatch.setattr(aa, "_llm_analysis", fake)


def test_scan_creates_and_dedups_by_fingerprint(mem_db, monkeypatch):
    from app.services import alert_analysis as aa
    _patch_sources(monkeypatch, metrics=_metrics(total=10, error_rate=0.3))
    _fake_analysis(monkeypatch, aa)
    first = asyncio.run(aa.scan_once())
    assert len(first["created"]) == 1
    second = asyncio.run(aa.scan_once())
    assert second["created"] == []  # 同指纹 open 告警不重复建单
    assert aa.list_alerts()["total"] == 1


def test_cooldown_blocks_recreate_until_expired(mem_db, monkeypatch):
    from app.services import alert_analysis as aa
    _patch_sources(monkeypatch, metrics=_metrics(total=10, error_rate=0.3))
    _fake_analysis(monkeypatch, aa)
    asyncio.run(aa.scan_once())
    alert_id = aa.list_alerts()["alerts"][0]["id"]
    aa.resolve_alert(alert_id, "tester")  # 处置后规则仍触发 → 受冷却期约束
    assert asyncio.run(aa.scan_once())["created"] == []
    old = (datetime.now() - timedelta(minutes=31)).isoformat()
    db.execute("UPDATE obs_alerts SET created_at = ? WHERE id = ?", (old, alert_id))
    assert len(asyncio.run(aa.scan_once())["created"]) == 1  # 冷却期已过


def test_auto_resolve_on_recovery(mem_db, monkeypatch):
    from app.services import alert_analysis as aa
    _patch_sources(monkeypatch, metrics=_metrics(total=10, error_rate=0.3))
    _fake_analysis(monkeypatch, aa)
    asyncio.run(aa.scan_once())
    _patch_sources(monkeypatch, metrics=_metrics(total=10, error_rate=0.0))
    result = asyncio.run(aa.scan_once())
    assert len(result["resolved"]) == 1
    row = db.query_one("SELECT status, resolved_by FROM obs_alerts")
    assert row["status"] == "resolved" and row["resolved_by"] == "system"


# ============================================
# A4 归因证据采集（确定性）
# ============================================
def test_evidence_errors_distribution(mem_db):
    from app.services import alert_analysis as aa
    now = datetime.now().isoformat()
    for i in range(3):
        db.execute(
            "INSERT INTO agent_traces (id, agent, status, error, started_at)"
            " VALUES (?,?,?,?,?)",
            (f"t{i}", "chat", "error", f"上游超时 {i}", now))
    db.execute("INSERT INTO agent_spans (id, trace_id, kind, name, status, started_at)"
               " VALUES ('s1','t0','llm','上游调用','error',?)", (now,))
    ev = aa._evidence_errors(now)
    assert ev["failed_samples"] == 3
    assert ev["by_agent"] == {"chat": 3}
    assert ev["by_status"] == {"error": 3}
    assert ev["error_spans"] == [{"name": "上游调用", "count": 1}]


def test_evidence_latency_breakdown(mem_db):
    from app.services import alert_analysis as aa
    now = datetime.now().isoformat()
    db.execute("INSERT INTO agent_traces (id, agent, status, latency_ms, started_at)"
               " VALUES ('t1','chat','ok',50000,?)", (now,))
    db.execute("INSERT INTO agent_spans (id, trace_id, kind, name, latency_ms, started_at)"
               " VALUES ('s1','t1','llm','生成',45000,?)", (now,))
    db.execute("INSERT INTO agent_spans (id, trace_id, kind, name, latency_ms, started_at)"
               " VALUES ('s2','t1','tool','检索',5000,?)", (now,))
    ev = aa._evidence_latency(now)
    item = ev["slowest_traces"][0]
    assert item["latency_ms"] == 50000
    kinds = {b["kind"]: b["ms"] for b in item["breakdown"]}
    assert kinds == {"llm": 45000, "tool": 5000}


def test_evidence_redacted_before_persist(mem_db, monkeypatch):
    from app.services import alert_analysis as aa
    rule = {"type": "performance", "metric": "error_rate", "value": 0.3, "threshold": 0.1,
            "severity": "critical",
            "evidence": {"error_messages": ["用户手机号13812345678报错"]}}
    alert_id = aa._create_alert(rule, {"cause": "c", "suggestions": ["s"]})
    row = db.query_one("SELECT evidence FROM obs_alerts WHERE id = ?", (alert_id,))
    assert "13812345678" not in row["evidence"]
    assert "[手机号已脱敏]" in row["evidence"]


# ============================================
# A5 LLM 分析：成功 / 降级
# ============================================
class _FakeLLM:
    def __init__(self, raw):
        self._raw = raw

    async def chat(self, messages, stream=False):
        yield self._raw


def test_llm_analysis_success(mem_db, monkeypatch):
    from app.services import alert_analysis as aa
    from app.core import llm as llm_mod
    raw = '{"cause": "上游超时集中", "suggestions": ["检查网关重试", "核对上游健康度"]}'
    monkeypatch.setattr(llm_mod.LLMFactory, "create_for_role",
                        classmethod(lambda cls, role: _FakeLLM(raw)))
    rule = {"type": "performance", "metric": "error_rate", "value": 0.3,
            "threshold": 0.1, "evidence": {}}
    out = asyncio.run(aa._llm_analysis(rule))
    assert out["degraded"] is False
    assert out["cause"] == "上游超时集中" and len(out["suggestions"]) == 2


def test_llm_analysis_fallback_on_failure_and_bad_json(mem_db, monkeypatch):
    from app.services import alert_analysis as aa
    from app.core import llm as llm_mod

    def boom(cls, role):
        raise RuntimeError("模型不可用")
    monkeypatch.setattr(llm_mod.LLMFactory, "create_for_role", classmethod(boom))
    rule = {"type": "performance", "metric": "p95_latency", "value": 40000,
            "threshold": 30000, "evidence": {}}
    out = asyncio.run(aa._llm_analysis(rule))
    assert out["degraded"] is True and out["cause"] and out["suggestions"]
    # 坏 JSON 同样降级（不抛异常）
    monkeypatch.setattr(llm_mod.LLMFactory, "create_for_role",
                        classmethod(lambda cls, role: _FakeLLM("这不是 JSON")))
    out = asyncio.run(aa._llm_analysis(rule))
    assert out["degraded"] is True


# ============================================
# A6 处置 + API（含越权）
# ============================================
def test_ack_resolve_reanalyze_service(mem_db, monkeypatch):
    from app.services import alert_analysis as aa
    rule = {"type": "quality", "metric": "dislike_rate", "value": 0.8, "threshold": 0.3,
            "severity": "warning", "evidence": {}}
    alert_id = aa._create_alert(rule, {"cause": "旧原因", "suggestions": ["旧建议"]})
    assert aa.ack_alert(alert_id)["status"] == "acked"
    assert aa.ack_alert(alert_id)["status"] == "acked"  # 幂等
    assert aa.resolve_alert(alert_id, "ops-1")["resolved_by"] == "ops-1"
    assert aa.ack_alert("missing") is None

    async def new_analysis(r):
        return {"cause": "新原因", "suggestions": ["新建议"], "degraded": False}
    monkeypatch.setattr(aa, "_llm_analysis", new_analysis)
    _patch_sources(monkeypatch)
    out = asyncio.run(aa.reanalyze(alert_id))
    assert out["analysis"]["cause"] == "新原因"
    assert asyncio.run(aa.reanalyze("missing")) is None


def _obs_app():
    from app.api import observability as obs_api
    app = FastAPI()
    app.include_router(obs_api.router)
    return app, obs_api


def test_alerts_api_lifecycle(mem_db, monkeypatch):
    import httpx
    from app.api import deps
    from app.services import alert_analysis as aa
    app, obs_api = _obs_app()
    app.dependency_overrides[deps.require_ops] = lambda: {"id": "a1", "role": "admin",
                                                         "username": "admin"}
    rule = {"type": "performance", "metric": "error_rate", "value": 0.3, "threshold": 0.1,
            "severity": "critical", "evidence": {}}
    alert_id = aa._create_alert(rule, {"cause": "c", "suggestions": ["s"]})

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/observability/alerts", params={"status": "open"})
            assert r.status_code == 200
            body = r.json()
            assert body["total"] == 1 and body["unacked"] == 1
            assert body["alerts"][0]["metric_label"] == "请求错误率"
            r = await client.post(f"/api/observability/alerts/{alert_id}/ack")
            assert r.status_code == 200 and r.json()["alert"]["status"] == "acked"
            r = await client.post(f"/api/observability/alerts/{alert_id}/resolve")
            assert r.status_code == 200
            assert r.json()["alert"]["resolved_by"] == "admin"
            r = await client.post("/api/observability/alerts/missing/ack")
            assert r.status_code == 404
    asyncio.run(run())


def test_alerts_api_forbidden_for_regular_user(mem_db, monkeypatch):
    import httpx
    from app.api import deps
    app, _ = _obs_app()
    async def _fake_user(authorization=""):
        return {"id": "u1", "role": "user"}
    monkeypatch.setattr(deps, "get_current_user", _fake_user)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/api/observability/alerts")).status_code == 403
            assert (await client.post("/api/observability/alerts/x/ack")).status_code == 403
    asyncio.run(run())