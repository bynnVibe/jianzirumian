"""个人工具使用隔离内存库与 HTTP 桩；不代表真实供应商／隔离 Worker 验收。"""
import asyncio
import base64
import copy
import json
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core import db
from app.services import personal_tools as tools, personal_tool_specs as specs, tool_http, tool_credentials, tool_result_cache, tool_eval
from app.services.tools import hooks
from app.services.tools.base import ToolContext
from app.services.tools.invoke import invoke_tool


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    monkeypatch.setenv("PERSONAL_TOOLS_ENABLED", "true")
    monkeypatch.delenv("TOOL_CREDENTIAL_MASTER_KEY", raising=False)
    monkeypatch.delenv("TOOL_CREDENTIAL_MASTER_KEY_FILE", raising=False)
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
        if url.endswith("/forecast"):
            args = kwargs["params"]
            from zoneinfo import ZoneInfo
            return {"latitude": args["latitude"], "longitude": args["longitude"], "timezone": args["timezone"],
                    "current": {"time": datetime.now(ZoneInfo(args["timezone"])).isoformat(timespec="minutes"),
                                "temperature_2m": 23.5, "weather_code": 3}, "current_units": {"temperature_2m": "°C"}}, 120
        return {"result": {"value": 3}}, 120
    monkeypatch.setattr(tool_http, "request_json", request)
    return calls


async def _drain_eval():
    while tool_eval._BG_TASKS:
        await asyncio.gather(*list(tool_eval._BG_TASKS))


def install(kind="elevation", owner="u1", accept_terms=True):
    async def run():
        proposal = tools.propose(owner, specs.connector_spec(kind))
        accepted = await tools.confirm(owner, proposal["id"], proposal["content_hash"], accept_terms=accept_terms)
        await _drain_eval()
        return accepted, proposal
    accepted, proposal = asyncio.run(run())
    return tools.get(owner, accepted["tool_id"]), proposal


def confirm_and_settle(owner, draft_id, content_hash, **kw):
    async def run():
        accepted = await tools.confirm(owner, draft_id, content_hash, **kw)
        await _drain_eval()
        return accepted
    return asyncio.run(run())


def http_spec():
    spec = specs.connector_spec("elevation")
    spec.update(executor="http", name="只读样例 API", function_name="read_sample_api",
                input_schema=specs.object_schema({"n": {"type": "integer"}}),
                output_schema=specs.object_schema({"value": {"type": "integer"}}),
                config={"url": "https://example.com/data", "method": "GET", "query": {"n": {"input": "n"}},
                        "output_map": {"value": "/result/value"}, "read_only_evidence": "文档说明此端点仅查询，无外部写入"},
                tests=[{"input": {"n": 1}, "assertions": [{"path": "/value", "op": "equals", "value": 3}]}])
    return spec


def test_confirm_real_shape_owner_version_idempotence(upstream, isolated):
    tool, proposal = install()
    assert tool["status"] == "ready" and tool["enabled"]
    # 样例测试 1 次 + 通过后自动评测再真实调用 1 次
    assert len(upstream) == 2
    assert tools.catalog("u1")[0]["function"]["name"] == tool["function_name"]
    # 默认全员可调用：u2 可见并可真实调用 u1 的工具（管理侧仍属主隔离）
    assert tools.catalog("u2")[0]["function"]["name"] == tool["function_name"]
    assert len(tool["function_name"]) <= 64
    asyncio.run(tools.confirm("u1", proposal["id"], proposal["content_hash"], accept_terms=True))
    assert len(upstream) == 2
    with pytest.raises(HTTPException):
        tools.get("u2", tool["id"])
    outcome = asyncio.run(invoke_tool(tool["function_name"], ToolContext("u2", "", "", ""), {"latitude": 1, "longitude": 2}))
    assert outcome.success and len(upstream) == 3
    # 按工具用户黑名单：拉黑 u2 后目录与执行门禁同时拒绝，不影响其他用户
    tools.configure("u1", tool["id"], blocked_users=["u2"])
    assert tools.catalog("u2") == [] and tools.catalog("u1")
    outcome = asyncio.run(invoke_tool(tool["function_name"], ToolContext("u2", "", "", ""), {"latitude": 1, "longitude": 2}))
    assert not outcome.success and len(upstream) == 3
    tools.configure("u1", tool["id"], blocked_users=[])
    outcome = asyncio.run(invoke_tool(tool["function_name"], ToolContext("u1", "", "", ""), {"latitude": True, "longitude": 2}))
    assert not outcome.success
    tools.configure("u1", tool["id"], enabled=False)
    assert not tools.catalog("u1")
    tools.configure("u1", tool["id"], enabled=True)
    assert tools.catalog("u1")
    db.init_db(isolated)
    assert len(tools.listing("u1")) == 1


def test_function_name_functional_unique_and_stable(upstream, isolated):
    tool, _ = install()
    # 连接器以功能名注册，不再基于随机码
    assert tool["function_name"] == "get_elevation"
    other, _ = install(owner="u2")
    assert other["function_name"] == "get_elevation_2"
    assert {t["function"]["name"] for t in tools.catalog("u1")} == {"get_elevation", "get_elevation_2"}
    # 功能名跨版本稳定：同工具升级新版本不改名
    spec = specs.connector_spec("elevation")
    spec["limitations"] += "；第二版本"
    proposal = tools.propose("u1", spec, tool_id=tool["id"])
    accepted = confirm_and_settle("u1", proposal["id"], proposal["content_hash"], accept_terms=True)
    assert tools.get("u1", accepted["tool_id"])["function_name"] == "get_elevation"
    # resolve 按功能名反查；非法/不存在名称拒绝
    assert tools.resolve("u1", "get_elevation") is not None
    assert tools.resolve("u1", "Get_Elevation") is None
    assert tools.resolve("u1", "no_such_tool") is None


def test_declaration_rejects_missing_or_random_function_name():
    spec = http_spec()
    del spec["function_name"]
    with pytest.raises(ValueError):
        tools.propose("u1", spec)
    spec["function_name"] = "pt_" + "a" * 32 + "_" + "b" * 16
    with pytest.raises(ValueError):
        tools.propose("u1", spec)


def test_terms_missing_or_invalid_response_never_ready(upstream, monkeypatch):
    tool, _ = install(accept_terms=False)
    assert tool["status"] == "needs_config" and len(upstream) == 0
    tools.configure("u1", tool["id"], accept_terms=True)
    async def invalid(*a, **k):
        return {"elevation": [None]}, 0
    monkeypatch.setattr(tool_http, "request_json", invalid)
    tool = asyncio.run(tools.test("u1", tool["id"]))
    assert tool["status"] == "needs_config" and not tool["enabled"]
    assert tool["tests"][0]["status"] == "failed"
    with pytest.raises(HTTPException):
        tools.configure("u1", tool["id"], enabled=True)


def test_hash_cancel_expiry_and_unknown_session():
    proposal = tools.propose("u1", specs.connector_spec("elevation"))
    with pytest.raises(HTTPException):
        asyncio.run(tools.confirm("u1", proposal["id"], "a" * 64))
    tools.cancel_draft("u1", proposal["id"])
    with pytest.raises(HTTPException):
        asyncio.run(tools.confirm("u1", proposal["id"], proposal["content_hash"]))
    p = tools.propose("u1", specs.connector_spec("weather"))
    db.execute("UPDATE personal_tool_drafts SET expires_at='2000-01-01' WHERE id=?", (p["id"],))
    assert tools.draft("u1", p["id"])["status"] == "expired"
    with pytest.raises(HTTPException):
        tools.propose("u1", specs.connector_spec("weather"), session_id="not-owned")


def test_custom_api_approval_and_encrypted_credentials(upstream, monkeypatch):
    spec = http_spec()
    spec["config"]["auth"] = {"kind": "bearer", "name": "Authorization"}
    proposal = tools.propose("u1", spec)
    accepted = asyncio.run(tools.confirm("u1", proposal["id"], proposal["content_hash"]))
    tid = accepted["tool_id"]
    assert tools.get("u1", tid)["status"] == "needs_config" and upstream == []
    with pytest.raises(ValueError):
        tools.configure("u1", tid, credential="private-token-value")
    with pytest.raises(HTTPException):
        tools.approve("u1", tid, proposal["content_hash"], {"id": "u1", "role": "user"})
    tools.approve("u1", tid, proposal["content_hash"], {"id": "admin", "role": "admin"})
    monkeypatch.setenv("TOOL_CREDENTIAL_MASTER_KEY", base64.b64encode(b"k" * 32).decode())
    tools.configure("u1", tid, credential="private-token-value")
    encrypted = db.query_one("SELECT * FROM personal_tool_credentials")
    assert "private-token-value" not in str(encrypted)
    assert tool_credentials.load("u1", tid, proposal["content_hash"], encrypted["id"]) == "private-token-value"
    with pytest.raises(ValueError):
        tool_credentials.load("u2", tid, proposal["content_hash"], encrypted["id"])
    tested = asyncio.run(tools.test("u1", tid))
    assert tested["status"] == "ready"
    assert upstream[0][1]["headers"] == {"Authorization": "Bearer private-token-value"}
    assert "private-token-value" not in str(tools.get("u1", tid))
    tools.configure("u1", tid, credential="new-private-token", enabled=False)
    with pytest.raises(HTTPException):
        tools.configure("u1", tid, enabled=True)


@pytest.mark.parametrize("url", ["http://example.com/x", "https://127.0.0.1/x", "https://[::1]/x", "https://169.254.169.254/x",
                                "https://example.com/x?api_key=secret", "https://user:pass@example.com/x"])
def test_unsafe_templates_rejected(url):
    spec = http_spec()
    spec["config"]["url"] = url
    with pytest.raises(ValueError):
        specs.review(spec)


@pytest.mark.parametrize("code", ["import os\ndef main(data): return {}", "def main(data): return open('/etc/passwd').read()",
                                  "def main(data): return data.__class__", "import subprocess\ndef main(data): return {}"])
def test_script_safety_not_bypassable_by_confirmation(code):
    spec = http_spec()
    spec.update(executor="python", config={"code": code})
    with pytest.raises(ValueError):
        tools.propose("u1", spec)


def test_script_without_worker_safely_blocked():
    spec = http_spec()
    spec.update(executor="python", config={"code": "def main(data):\n    return {'value': data['n'] + 2}"})
    proposal = tools.propose("u1", spec)
    accepted = asyncio.run(tools.confirm("u1", proposal["id"], proposal["content_hash"]))
    tools.approve("u1", accepted["tool_id"], proposal["content_hash"], {"id": "admin", "role": "admin"})
    tool = asyncio.run(tools.test("u1", accepted["tool_id"]))
    assert tool["status"] == "needs_config"
    assert "sandbox_unavailable" in tool["tests"][0]["report_json"]
    assert not tools.catalog("u1")


def test_cache_fixed_expiry_force_refresh_and_exact_parameters(upstream):
    tool, _ = install("weather")
    args = tool["spec"]["tests"][0]["input"]
    ctx = ToolContext("u1", "", "", "")
    first = asyncio.run(invoke_tool(tool["function_name"], ctx, args))
    assert first.success
    saved = db.query_one("SELECT * FROM tool_result_cache")
    assert saved
    second = asyncio.run(invoke_tool(tool["function_name"], ctx, args))
    assert second.extra["cache"]["hit"] and len(upstream) == 2
    assert db.query_one("SELECT * FROM tool_result_cache")["expires_at"] == saved["expires_at"]
    ctx.force_refresh = True
    assert asyncio.run(invoke_tool(tool["function_name"], ctx, args)).success
    assert len(upstream) == 3
    ctx.force_refresh = False
    assert asyncio.run(invoke_tool(tool["function_name"], ctx, {**args, "latitude": 23})).success
    assert len(upstream) == 4
    db.execute("UPDATE tool_result_cache SET expires_at='invalid'")
    assert asyncio.run(invoke_tool(tool["function_name"], ctx, args)).success
    assert len(upstream) == 5


def test_cancellation_persists_terminal_and_recovery(upstream, monkeypatch):
    tool, _ = install()
    async def wait(*a, **k):
        await asyncio.sleep(10)
    monkeypatch.setattr(tool_http, "request_json", wait)
    async def check():
        task = asyncio.create_task(invoke_tool(tool["function_name"], ToolContext("u1", "", "", ""), {"latitude": 1, "longitude": 2}))
        await asyncio.sleep(.02)
        record = tools.executions("u1", tool["id"])[0]
        with pytest.raises(HTTPException):
            tools.cancel_execution("u2", record["id"])
        assert tools.cancel_execution("u1", record["id"])["status"] == "cancellation_requested"
        with pytest.raises(asyncio.CancelledError):
            await task
        assert tools.executions("u1", tool["id"])[0]["execution_status"] == "cancelled"
    asyncio.run(check())
    db.execute("UPDATE personal_tools SET status='testing'")
    tools.recover()
    assert tools.get("u1", tool["id"])["status"] == "needs_config"


def test_api_owner_and_admin_guard(upstream, monkeypatch):
    from app.api import personal_tools as api
    from app.api import deps
    app = FastAPI()
    app.include_router(api.router)
    original = deps.get_current_user
    admin = {"id": "u1", "role": "admin"}
    app.dependency_overrides[original] = lambda: admin
    async def _fake_admin(authorization=""):
        return admin
    monkeypatch.setattr(deps, "get_current_user", _fake_admin)
    with TestClient(app) as client:
        proposal = client.post("/api/personal-tools/drafts", json={"spec": specs.connector_spec("elevation")}).json()
        accepted = client.post(f"/api/personal-tools/drafts/{proposal['id']}/confirm", json={"expected_hash": proposal["content_hash"], "accept_terms": True})
        assert accepted.status_code == 200
        tid = accepted.json()["tool_id"]
        assert client.get(f"/api/personal-tools/{tid}").json()["status"] == "ready"
        assert client.get("/api/personal-tools/reviews").status_code == 200
        # 普通用户：直接提交/确认独立工具草稿被拒（@tool 仅管理员），属主隔离不变
        regular = {"id": "u2", "role": "user"}
        app.dependency_overrides[original] = lambda: regular
        async def _fake_regular(authorization=""):
            return regular
        monkeypatch.setattr(deps, "get_current_user", _fake_regular)
        assert client.post("/api/personal-tools/drafts", json={"spec": specs.connector_spec("elevation")}).status_code == 403
        assert client.get("/api/personal-tools/reviews").status_code == 403
        assert client.get(f"/api/personal-tools/{tid}").status_code == 404
        assert client.get("/api/personal-tools").json()["tools"] == []


@pytest.mark.parametrize("mutate", [
    lambda s: s["config"]["query"].update(api_key={"literal": "do-not-save"}),
    lambda s: s.update(sources=["https://example.com/docs?access_token=private"]),
    lambda s: s.update(description="Bearer abcdef1234567890"),
    lambda s: s.update(tests=[None]),
    lambda s: s["tests"][0]["assertions"][0].update(path="/bad~path"),
    lambda s: s["config"].update(output_map={"other": "/value"}),
])
def test_declarations_reject_secrets_and_malformed_shapes(mutate):
    spec = http_spec()
    mutate(spec)
    with pytest.raises(ValueError):
        tools.propose("u1", spec)
    assert not db.query("SELECT id FROM personal_tool_drafts")


def test_provenance_timestamp_does_not_change_version_but_confirmation_binds_it(upstream):
    spec = specs.connector_spec("elevation")
    spec["provenance"] = [{"url": spec["sources"][0], "content_hash": "a" * 64, "fetched_at": "2026-01-01T00:00:00Z"}]
    first = tools.propose("u1", spec)
    installed = confirm_and_settle("u1", first["id"], first["content_hash"], accept_terms=True)
    spec["provenance"][0]["fetched_at"] = "2026-01-02T00:00:00Z"
    second = tools.propose("u1", spec)
    assert first["content_hash"] != second["content_hash"]
    assert first["review"]["version"] == second["review"]["version"]
    duplicate = asyncio.run(tools.confirm("u1", second["id"], second["content_hash"], accept_terms=True))
    # 重复版本确认幂等：不再测试/评测，上游调用保持 样例测试1次+自动评测1次
    assert installed["tool_id"] == duplicate["tool_id"] and len(upstream) == 2
    spec["provenance"][0]["content_hash"] = "b" * 64
    assert specs.review(spec)["version"] != first["review"]["version"]


def test_generic_http_structure_does_not_prove_requirement(upstream):
    result = asyncio.run(tool_http.execute(http_spec(), {"n": 1}))
    assert result["validation_status"] == "passed"
    assert result["requirement_status"] == "unverified"


def test_review_scan_upgrade_invalidates_approval(upstream, monkeypatch):
    proposal = tools.propose("u1", http_spec())
    accepted = asyncio.run(tools.confirm("u1", proposal["id"], proposal["content_hash"]))
    tools.approve("u1", accepted["tool_id"], proposal["content_hash"], {"id": "admin", "role": "admin"})
    assert asyncio.run(tools.test("u1", accepted["tool_id"]))["status"] == "ready"
    monkeypatch.setattr(specs, "SCAN_VERSION", "next-scan")
    assert tools.catalog("u1") == []
    current = tools.get("u1", accepted["tool_id"])
    assert "admin_review_required" in current["blockers"]
    assert current["status"] == "needs_config" and not current["enabled"] and not current["approved"]
    assert tools.pending_reviews()[0]["tool_id"] == accepted["tool_id"]
    tools.approve("u1", accepted["tool_id"], current["version"], {"id": "admin", "role": "admin"})
    assert tools.pending_reviews() == []


def test_installed_draft_includes_real_state(upstream):
    tool, proposal = install()
    current = tools.draft("u1", proposal["id"])
    assert current["tool"]["status"] == "ready"
    tools.configure("u1", tool["id"], enabled=False)
    repeated = asyncio.run(tools.confirm("u1", proposal["id"], proposal["content_hash"]))
    assert repeated["tool"]["status"] == "disabled"
    assert len(upstream) == 2


def test_drafts_restore_original_turn_and_expire_when_cleared(monkeypatch):
    from app.services.chat import ChatService, Message
    service = ChatService()
    session = service.create_session(user_id="u1")
    message = Message("assistant", "工具草稿")
    session.add_message(message)
    service._persist_session(session)
    proposal = tools.propose("u1", specs.connector_spec("elevation"), session.session_id, message.message_id)
    session.add_message(Message("assistant", "另一轮"))
    service._persist_session(session)
    monkeypatch.setenv("PERSONAL_TOOLS_ENABLED", "false")
    restored = tools.history("u1", session.session_id)
    assert restored[0]["message_id"] == message.message_id
    tools.invalidate_session(session.session_id)
    assert tools.history("u1", session.session_id)[0]["status"] == "expired"
    monkeypatch.setenv("PERSONAL_TOOLS_ENABLED", "true")
    with pytest.raises(HTTPException):
        asyncio.run(tools.confirm("u1", proposal["id"], proposal["content_hash"]))


def install_python(monkeypatch):
    monkeypatch.setenv("SANDBOX_PYTHON_IMAGE", "example.com/approved/python@sha256:" + "a" * 64)
    monkeypatch.setenv("SANDBOX_SECCOMP_SHA256", "b" * 64)
    monkeypatch.setattr(tools.worker_client, "configured", lambda: True)
    spec = http_spec()
    spec.update(executor="python", config={"code": "def main(data): return {'value': data['n'] + 2}"})
    proposal = tools.propose("u1", spec)
    accepted = asyncio.run(tools.confirm("u1", proposal["id"], proposal["content_hash"]))
    profile_hash = specs.fingerprint(tools.worker_client.runtime_profile())
    tools.approve("u1", accepted["tool_id"], proposal["review"]["version"], {"id": "admin", "role": "admin"}, profile_hash)
    return tools.get("u1", accepted["tool_id"])


def test_script_runtime_upgrade_invalidates_and_requires_retest(monkeypatch):
    tool = install_python(monkeypatch)
    async def execute(*args):
        return {"output": {"value": 3}, "execution_status": "completed", "validation_status": "passed", "requirement_status": "unverified"}
    monkeypatch.setattr(tools.worker_client, "execute", execute)
    assert asyncio.run(tools.test("u1", tool["id"]))["status"] == "ready"
    old_hash = specs.fingerprint(tools.worker_client.runtime_profile())
    monkeypatch.setenv("SANDBOX_SECCOMP_SHA256", "c" * 64)
    assert not tools.catalog("u1")
    current = tools.get("u1", tool["id"])
    assert not current["approved"] and current["status"] == "needs_config"
    assert tools.pending_reviews()[0]["tool_id"] == tool["id"]
    with pytest.raises(HTTPException):
        tools.approve("u1", tool["id"], tool["version"], {"id": "admin", "role": "admin"}, old_hash)
    tools.approve("u1", tool["id"], tool["version"], {"id": "admin", "role": "admin"}, specs.fingerprint(tools.worker_client.runtime_profile()))
    assert tools.get("u1", tool["id"])["status"] == "needs_config"
    with pytest.raises(HTTPException):
        tools.configure("u1", tool["id"], enabled=True)
    assert asyncio.run(tools.test("u1", tool["id"]))["status"] == "ready"


def test_script_unconfirmed_cancel_blocks_until_remote_termination(monkeypatch):
    tool = install_python(monkeypatch)
    async def unconfirmed(*args):
        raise tools.worker_client.CancellationUnconfirmed()
    monkeypatch.setattr(tools.worker_client, "execute", unconfirmed)
    with pytest.raises(tools.worker_client.CancellationUnconfirmed):
        asyncio.run(tools.test("u1", tool["id"]))
    run = tools.executions("u1", tool["id"])[0]
    assert run["execution_status"] == "cancellation_requested" and not run["finished_at"]
    assert tools.get("u1", tool["id"])["tests"][0]["status"] == "cancellation_requested"
    with pytest.raises(HTTPException):
        asyncio.run(tools.test("u1", tool["id"]))
    async def confirmed(*args):
        return True
    monkeypatch.setattr(tools.worker_client, "cancel", confirmed)
    assert asyncio.run(tools.reconcile_execution("u1", run["id"]))["status"] == "interrupted"
    assert tools.executions("u1", tool["id"])[0]["finished_at"]


def test_corrupt_version_does_not_crash_listing_or_recovery(upstream):
    tool, _ = install()
    db.execute("UPDATE personal_tool_versions SET spec_json='not-json'")
    tools.recover()
    current = tools.get("u1", tool["id"])
    assert current["status"] == "rejected" and current["spec"] == {}
    assert tools.listing("u1")[0]["blockers"] == ["invalid_stored_spec"]
    assert not tools.catalog("u1")


def test_configuration_cannot_race_active_execution(upstream, monkeypatch):
    tool, _ = install()
    async def wait(*args, **kwargs):
        await asyncio.sleep(10)
    monkeypatch.setattr(tool_http, "request_json", wait)
    async def check():
        task = asyncio.create_task(invoke_tool(tool["function_name"], ToolContext("u1", "", "", ""), {"latitude": 1, "longitude": 2}))
        await asyncio.sleep(.02)
        with pytest.raises(HTTPException):
            tools.configure("u1", tool["id"], enabled=False)
        with pytest.raises(HTTPException):
            await tools.test("u1", tool["id"])
        assert tools.get("u1", tool["id"])["status"] == "ready"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(check())
