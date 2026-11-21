"""工具标准化评测流水线：测试通过 → 自动评测 → 全过入注册表；权限收紧与评测集保护。

使用隔离内存库与 HTTP 桩；不代表真实供应商验收。
"""
import asyncio
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import db
from app.services import personal_tools as tools, personal_tool_specs as specs, tool_http, tool_eval
from app.services.tools import hooks


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    monkeypatch.setenv("PERSONAL_TOOLS_ENABLED", "true")
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


async def install_and_settle(owner="u1", kind="elevation"):
    """确认安装（自动跑样例测试）并等待后台评测任务全部结束。"""
    proposal = tools.propose(owner, specs.connector_spec(kind))
    accepted = await tools.confirm(owner, proposal["id"], proposal["content_hash"], accept_terms=True)
    while tool_eval._BG_TASKS:
        await asyncio.gather(*list(tool_eval._BG_TASKS))
    return accepted


def test_pass_evaluates_and_enters_standardized_registry(upstream):
    accepted = asyncio.run(install_and_settle())
    tool = tools.get("u1", accepted["tool_id"])
    assert tool["status"] == "ready" and tool["enabled"]
    assert tool["eval_status"] == "passed"
    ds_id = tool_eval.dataset_id_for(tool["id"])
    assert tool["eval_dataset_id"] == ds_id and tool["eval_run_id"]
    ds = db.query_one("SELECT * FROM eval_datasets WHERE id=?", (ds_id,))
    assert ds["kind"] == "tool"
    cases = db.query("SELECT * FROM eval_cases WHERE dataset_id=?", (ds_id,))
    spec = specs.connector_spec("elevation")
    assert len(cases) == len(spec["tests"])
    assert cases[0]["origin"] == "tool_test"
    assert json.loads(cases[0]["expected_tools"]) == [tool["function_name"]]
    run = db.query_one("SELECT * FROM eval_runs WHERE id=?", (tool["eval_run_id"],))
    assert run["status"] == "finished"
    summary = json.loads(run["summary"])
    assert summary["pass_rate"] == 1.0 and summary["total"] == len(cases)
    results = db.query("SELECT * FROM eval_results WHERE run_id=?", (run["id"],))
    assert len(results) == len(cases) and all(r["status"] == "ok" for r in results)
    registry = tools.standardized_registry()
    assert len(registry) == 1
    entry = registry[0]
    assert entry["name"] == tool["function_name"] and entry["namespace"] == "personal"
    assert entry["source"] == "personal_tool" and entry["eval_status"] == "passed"
    assert entry["status"] == "active" and entry["risk_level"] == "low"


def test_failed_eval_keeps_tool_out_of_registry(upstream, monkeypatch):
    async def scenario():
        proposal = tools.propose("u1", specs.connector_spec("elevation"))
        accepted = await tools.confirm("u1", proposal["id"], proposal["content_hash"], accept_terms=True)
        # 样例测试已通过、评测已排程；此时上游数据劣化 → 评测断言失败
        async def bad(url, **kwargs):
            return {"elevation": [None]}, 100
        monkeypatch.setattr(tool_http, "request_json", bad)
        while tool_eval._BG_TASKS:
            await asyncio.gather(*list(tool_eval._BG_TASKS))
        return accepted
    accepted = asyncio.run(scenario())
    tool = tools.get("u1", accepted["tool_id"])
    assert tool["eval_status"] == "failed"
    run = db.query_one("SELECT * FROM eval_runs WHERE id=?", (tool["eval_run_id"],))
    assert run["status"] == "finished"
    assert json.loads(run["summary"])["pass_rate"] == 0.0
    assert tools.standardized_registry() == []


def test_rerun_via_evaluation_start_run(upstream):
    from app.services import evaluation
    accepted = asyncio.run(install_and_settle())
    tool = tools.get("u1", accepted["tool_id"])

    async def rerun():
        run_id = evaluation.start_run(tool["eval_dataset_id"], {"id": "admin"})
        while tool_eval._BG_TASKS:
            await asyncio.gather(*list(tool_eval._BG_TASKS))
        return run_id
    run_id = asyncio.run(rerun())
    run = db.query_one("SELECT * FROM eval_runs WHERE id=?", (run_id,))
    assert run["status"] == "finished"
    assert tools.get("u1", tool["id"])["eval_status"] == "passed"


def test_tool_dataset_resists_manual_mutation(upstream):
    accepted = asyncio.run(install_and_settle())
    tool = tools.get("u1", accepted["tool_id"])
    ds_id = tool["eval_dataset_id"]
    case_id = db.query_one("SELECT id FROM eval_cases WHERE dataset_id=?", (ds_id,))["id"]
    from app.api.eval import router
    from app.api.deps import require_ops
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_ops] = lambda: {"id": "admin", "role": "admin"}
    with TestClient(app) as client:
        assert client.post(f"/api/eval/datasets/{ds_id}/cases",
                           json={"question": "手工用例"}).status_code == 409
        assert client.delete(f"/api/eval/cases/{case_id}").status_code == 409
        assert client.delete(f"/api/eval/datasets/{ds_id}").status_code == 409
        assert client.post(f"/api/eval/datasets/{ds_id}/cases/import",
                           json={"cases": [{"question": "x"}]}).status_code == 409
        # 复跑评测不受保护限制
        assert client.post(f"/api/eval/datasets/{ds_id}/runs", json={}).status_code in (200, 409)


def test_chat_tool_command_admin_only(monkeypatch):
    from app.api import chat as chat_api
    from app.api import deps
    app = FastAPI()
    app.include_router(chat_api.router)
    app.dependency_overrides[deps.get_optional_user] = lambda: {"id": "u1", "role": "user"}

    async def fake_stream(**kw):
        yield 'data: {"event":"done"}\n'
    monkeypatch.setattr(chat_api.chat_service, "chat_stream", fake_stream)
    with TestClient(app) as client:
        r = client.post("/api/chat/send", json={"query": "@tool 添加一个天气查询工具"})
        assert r.status_code == 403 and "管理员" in r.json()["detail"]
        r = client.post("/api/chat/send-with-files", data={"message": "@tool 添加一个天气查询工具"})
        assert r.status_code == 403
        # @skills 对普通用户保持开放（越过权限闸，进入后续流程不因权限 403）
        r = client.post("/api/chat/send", json={"query": "@skills 添加一个养生技能"})
        assert r.status_code != 403


def test_personal_tool_api_gates(monkeypatch):
    from app.api import personal_tools as pt_api
    from app.api import deps
    app = FastAPI()
    app.include_router(pt_api.router)
    regular = {"id": "u1", "role": "user"}
    app.dependency_overrides[deps.get_current_user] = lambda: regular
    async def _fake_regular(authorization=""):
        return regular
    monkeypatch.setattr(deps, "get_current_user", _fake_regular)
    with TestClient(app) as client:
        r = client.post("/api/personal-tools/drafts", json={"spec": specs.connector_spec("elevation")})
        assert r.status_code == 403
        # 独立工具草稿：普通用户确认被拒
        proposal = tools.propose("u1", specs.connector_spec("elevation"))
        r = client.post(f"/api/personal-tools/drafts/{proposal['id']}/confirm",
                        json={"expected_hash": proposal["content_hash"], "accept_terms": True})
        assert r.status_code == 403
        # Skill 依赖工具草稿：普通用户可确认（权限闸放行，后续走 Skill 依赖校验）
        db.execute("UPDATE personal_tool_drafts SET parent_skill_draft_id='skill-x' WHERE id=?", (proposal["id"],))
        r = client.post(f"/api/personal-tools/drafts/{proposal['id']}/confirm",
                        json={"expected_hash": proposal["content_hash"], "accept_terms": True})
        assert r.status_code != 403


def test_observability_registry_merges_personal_tools(upstream):
    accepted = asyncio.run(install_and_settle())
    tool = tools.get("u1", accepted["tool_id"])
    from app.api.observability import router
    from app.api.deps import require_ops
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_ops] = lambda: {"id": "admin", "role": "admin"}
    with TestClient(app) as client:
        data = client.get("/api/observability/tool-registry").json()
        names = [t["name"] for t in data["tools"]]
        assert tool["function_name"] in names
        entry = next(t for t in data["tools"] if t["name"] == tool["function_name"])
        assert entry["source"] == "personal_tool"
        assert any(s["function"]["name"] == tool["function_name"] for s in data["function_schemas"])
        assert data["schema_tokens"] > 0