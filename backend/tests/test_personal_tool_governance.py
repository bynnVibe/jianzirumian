"""个人工具治理中心：就绪显性化、生命周期总览、注册门禁（评测通过才可见/可调用）、
监控聚合与复评/审批收口。使用隔离内存库与 HTTP 桩。"""
import asyncio
import sqlite3

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core import db
from app.services import personal_tools as tools, personal_tool_specs as specs, tool_http, tool_eval
from app.services import tool_governance as gov
from app.services.tools import hooks


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    monkeypatch.setenv("PERSONAL_TOOLS_ENABLED", "true")
    monkeypatch.delenv("TOOL_CREDENTIAL_MASTER_KEY", raising=False)
    hooks.reset_rate_limits()
    yield conn
    conn.close()


@pytest.fixture
def upstream(monkeypatch):
    calls = []

    async def request(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/elevation"):
            return {"elevation": [12.5]}, 100
        return {"result": {"value": 3}}, 120

    monkeypatch.setattr(tool_http, "request_json", request)
    return calls


async def _drain():
    while tool_eval._BG_TASKS:
        await asyncio.gather(*list(tool_eval._BG_TASKS))


def install(owner="u1"):
    async def run():
        proposal = tools.propose(owner, specs.connector_spec("elevation"))
        accepted = await tools.confirm(owner, proposal["id"], proposal["content_hash"], accept_terms=True)
        await _drain()
        return accepted
    return asyncio.run(run())


ADMIN = {"id": "admin", "role": "admin"}
OPS = {"id": "ops1", "role": "ops"}
U1 = {"id": "u1", "role": "user"}
U2 = {"id": "u2", "role": "user"}


def test_readiness_surfaces_tool_decision_gap(monkeypatch):
    from app.core.llm import LLMFactory
    from app.core.tool_stream import ToolModelUnavailable

    def unavailable(*args, **kwargs):
        raise ToolModelUnavailable("未配置兼容的原生工具决策模型")
    monkeypatch.setattr(LLMFactory, "create_for_role", unavailable)
    items = {i["key"]: i for i in gov.readiness()}
    assert set(items) == {"feature", "credentials", "sandbox", "tool_decision"}
    assert items["feature"]["ok"] is True
    assert items["credentials"]["ok"] is False
    assert items["sandbox"]["ok"] is False
    assert items["tool_decision"]["ok"] is False
    assert items["tool_decision"]["link"] == "/admin/gateway"

    class FakeLLM:
        model = "fake-fc"
    monkeypatch.setattr(LLMFactory, "create_for_role", lambda *a, **k: FakeLLM())
    assert {i["key"]: i for i in gov.readiness()}["tool_decision"]["ok"] is True


def test_overview_stage_and_eval_gate(upstream):
    accepted = install()
    tid = accepted["tool_id"]
    ov = gov.overview(U1)
    row = ov["tools"][0]
    assert row["stage"] == "registered" and row["registered"] is True
    assert row["eval_summary"]["pass_rate"] == 1.0
    assert row["exec_stats"]["total"] >= 1  # 样例+评测真实调用留痕
    assert tools.catalog("u1") and any(p.get("tool_id") == tid for p in gov.registry(U1)["tools"])

    # 评测未通过 → 注册表与聊天目录同时下架，治理中心显性化失败阶段
    db.execute("UPDATE personal_tools SET eval_status='failed' WHERE id=?", (tid,))
    row = gov.overview(U1)["tools"][0]
    assert row["stage"] == "eval_failed" and row["registered"] is False
    assert not tools.catalog("u1")
    assert not any(p.get("tool_id") == tid for p in gov.registry(U1)["tools"])

    # 集中复评 → 通过后重新注册
    async def reeval_and_drain():
        r = gov.reeval(U1, tid)
        await _drain()
        return r
    run = asyncio.run(reeval_and_drain())
    assert run["run_id"]
    row = gov.overview(U1)["tools"][0]
    assert row["stage"] == "registered"
    assert tools.catalog("u1")


def test_reeval_gates_and_monitoring(upstream):
    accepted = install()
    tid = accepted["tool_id"]
    mon = gov.monitoring(U1, tid)
    assert mon["tool"]["stage"] == "registered"
    assert mon["tool"]["owner_id"] == "u1"  # 前端属主操作区依赖 owner_id
    assert mon["tests"] and mon["eval"]["results"]
    assert all(r["status"] == "ok" for r in mon["eval"]["results"])
    assert mon["executions"]

    # 样例未通过（weather 条款缺失阻塞）→ 复评被拒
    proposal = tools.propose("u1", specs.connector_spec("weather"))
    other = asyncio.run(tools.confirm("u1", proposal["id"], proposal["content_hash"], accept_terms=False))
    with pytest.raises(HTTPException) as exc:
        gov.reeval(U1, other["tool_id"])
    assert exc.value.status_code == 409

    # 普通用户不可监控他人工具；管理员与运维可见全部
    with pytest.raises(HTTPException) as exc:
        gov.monitoring(U2, tid)
    assert exc.value.status_code == 404
    assert gov.overview(U2)["tools"] == []
    assert gov.monitoring(OPS, tid)["tool"]["owner_id"] == "u1"
    assert len(gov.overview(OPS)["tools"]) == 2
    admin_ov = gov.overview(ADMIN)
    assert len(admin_ov["tools"]) == 2 and "reviews" in admin_ov
    assert any(t["id"] == tid for t in admin_ov["tools"])


def test_governance_api_gates(upstream, monkeypatch):
    from app.api import tool_governance as api, deps
    accepted = install()
    tid = accepted["tool_id"]
    app = FastAPI()
    app.include_router(api.router)
    current = {"user": U1}

    async def _fake_user(authorization=""):
        return current["user"]
    monkeypatch.setattr(deps, "get_current_user", _fake_user)
    with TestClient(app) as client:
        # 治理中心已迁入系统管理：普通用户一律 403
        assert client.get("/api/tool-governance/overview").status_code == 403
        # 运维只读：可见全部工具与监控，不可代管写操作
        current["user"] = OPS
        assert client.get("/api/tool-governance/overview").json()["tools"][0]["stage"] == "registered"
        assert client.get(f"/api/tool-governance/tools/{tid}/monitoring").json()["eval"]["results"]
        assert client.post(f"/api/tool-governance/tools/{tid}/reeval").status_code == 404
        # 审批仅管理员
        assert client.post(f"/api/tool-governance/tools/{tid}/approve",
                           json={"owner_id": "u1", "expected_hash": accepted["content_hash"],
                                 "verified_read_only": True}).status_code == 403
        # 管理员：全量可见 + 一键跑完生命周期工作流（测试→评测→启用注册）
        current["user"] = ADMIN
        ov = client.get("/api/tool-governance/overview").json()
        assert ov["is_admin"] is True and "reviews" in ov
        reg = client.get("/api/tool-governance/registry").json()
        personal = [p for p in reg["tools"] if p.get("source") == "personal_tool"]
        assert personal and personal[0]["owner_name"] and personal[0]["blocked_users"] == []
        wf = client.post(f"/api/tool-governance/tools/{tid}/workflow")
        assert wf.status_code == 200 and wf.json()["stage"] == "registered"
        # 黑名单管理：拉黑 u2 后其聊天目录下架，注册表仍展示供管理
        assert client.patch(f"/api/tool-governance/tools/{tid}", json={"blocked_users": ["u2"]}).status_code == 200
        assert tools.catalog("u2") == [] and tools.catalog("u1")
        assert client.patch(f"/api/tool-governance/tools/{tid}", json={"blocked_users": []}).status_code == 200
        assert client.post(f"/api/tool-governance/tools/{tid}/reeval").status_code == 200