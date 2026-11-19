"""个人工具聊天集成：隔离数据库与模型桩，不替代真实上游验收。"""
import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import db
from app.core.tool_stream import ToolModelUnavailable
from app.services import personal_tools, personal_tool_specs as specs, tool_workflow
from app.services.chat import ChatService, Message, LLMFactory


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    monkeypatch.setenv("PERSONAL_TOOLS_ENABLED", "true")
    yield
    conn.close()


async def events(stream):
    return [json.loads(line.removeprefix("data: ")) async for line in stream]


@pytest.mark.parametrize("query,command", [("@tool 当前天气", ""), ("@tools 当前天气", "tool"), ("当前天气", "tool")])
def test_command_only_proposes_and_binds_original_turn(monkeypatch, query, command):
    service = ChatService()
    session = service.create_session(user_id="alice")
    async def build(owner, sid, mid, description):
        assert description == "当前天气"
        return personal_tools.propose(owner, specs.connector_spec("weather"), sid, mid)
    monkeypatch.setattr(tool_workflow, "build_draft", build)
    result = asyncio.run(events(service.chat_stream(session.session_id, query, user_id="alice", command=command)))
    proposal = next(e["draft"] for e in result if e["event"] == "tool_draft")
    answer = result[-1]["message"]
    assert proposal["message_id"] == answer["message_id"]
    assert not db.query("SELECT * FROM personal_tools")
    assert result[-1]["event"] == "done" and "保存配置不代表工具可用" in answer["content"]
    session.add_message(Message("assistant", "其他轮次"))
    service._persist_session(session)
    assert personal_tools.history("alice", session.session_id)[0]["message_id"] == answer["message_id"]
    service.delete_session(session.session_id, user_id="alice")
    assert db.query_one("SELECT status FROM personal_tool_drafts")["status"] == "expired"


def turn(service, session, message):
    return service._personal_tool_turn(session, message, [{"role": "user", "content": "读取数据"}],
        [{"type": "function", "function": {"name": "test", "parameters": specs.object_schema({"n": {"type": "number"}})}}],
        "rule", "model", True, False, [{"index": 1}], [], True, [])


def session_with_user():
    service = ChatService()
    session = service.create_session(user_id="alice")
    message = Message("user", "读取数据")
    session.add_message(message)
    service._persist_session(session)
    return service, session, message


def test_native_turn_persists_only_public_records_and_requires_tools(monkeypatch):
    from app.services.tools import native
    from app.services import module_recall, usage
    created = []
    def create(*args, **kwargs):
        created.append((args, kwargs))
        return object()
    async def run(llm, messages, ctx, tools, *, result, stream_final):
        assert ctx.user_id == "alice" and ctx.force_refresh and stream_final
        assert ctx.message_id != user.message_id
        result.update(answer="结果[工具来源 1]", status="unverified", records=[{"tool": "test", "state": "end"}],
                      tool_sources=[{"index": 1, "url": "https://example.com"}], tool_results=[{"raw": "不能下发的返回值"}])
        yield {"event": "token", "content": result["answer"]}
    monkeypatch.setattr(LLMFactory, "create_for_role", create)
    monkeypatch.setattr(native, "run_tool_loop", run)
    monkeypatch.setattr(module_recall, "flush_turn_modules", lambda *a: None)
    monkeypatch.setattr(usage, "record_usage", lambda *a, **kw: None)
    service, session, user = session_with_user()
    result = asyncio.run(events(turn(service, session, user)))
    assert created == [(("tool_decision", "alice", "rule", "model"), {"require_tools": True})]
    assert result[-1]["event"] == "done"
    answer = result[-1]["message"]
    assert answer["cache_info"] == {} and answer["tool_sources"][0]["index"] == 1
    assert "不能下发的返回值" not in json.dumps(answer, ensure_ascii=False)
    restored = ChatService().get_session(session.session_id).messages[-1]
    assert restored.to_dict()["tool_sources"] == answer["tool_sources"]


def test_incompatible_model_saves_one_interrupted_turn(monkeypatch):
    def create(*a, **kw):
        raise ToolModelUnavailable("not supported")
    monkeypatch.setattr(LLMFactory, "create_for_role", create)
    service, session, user = session_with_user()
    result = asyncio.run(events(turn(service, session, user)))
    assert result[-1]["code"] == "tool_model_unavailable"
    assert len(session.messages) == 2 and session.messages[-1].interrupted


@pytest.mark.parametrize("deleted", [False, True])
def test_cancel_persists_terminal_without_resurrecting_deleted_session(monkeypatch, deleted):
    from app.services.tools import native
    async def run(llm, messages, ctx, tools, *, result, stream_final):
        try:
            result.update(answer="部分答案", status="running")
            yield {"event": "token", "content": "部分答案"}
            await asyncio.sleep(10)
        finally:
            result.update(status="cancelled", records=[{"execution_status": "cancelled"}])
    monkeypatch.setattr(LLMFactory, "create_for_role", lambda *a, **kw: object())
    monkeypatch.setattr(native, "run_tool_loop", run)
    service, session, user = session_with_user()
    async def check():
        stream = turn(service, session, user)
        await anext(stream)
        await anext(stream)
        task = asyncio.create_task(anext(stream))
        await asyncio.sleep(.01)
        if deleted:
            service.delete_session(session.session_id, user_id="alice")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await stream.aclose()
    asyncio.run(check())
    rows = db.query("SELECT role,interrupted,agent_process FROM chat_messages")
    if deleted:
        assert rows == [] and db.query("SELECT * FROM chat_sessions") == []
    else:
        assert len(rows) == 2 and rows[-1]["interrupted"]
        assert "cancelled" in rows[-1]["agent_process"]


@pytest.mark.parametrize("intent", [{"kind": "unsupported", "read_only": False},
    {"kind": "weather", "read_only": True, "matches": False}, {"kind": "other", "read_only": True}])
def test_discovery_rejects_unsupported_without_fetch(monkeypatch, intent):
    monkeypatch.setattr(tool_workflow, "_model_json", AsyncMock(return_value=intent))
    fetch = AsyncMock(side_effect=AssertionError("不应读取网络"))
    monkeypatch.setattr(tool_workflow.SafeFetcher, "fetch", fetch)
    with pytest.raises(ValueError):
        asyncio.run(tool_workflow.build_draft("alice", "", "", "发送邮件"))
    fetch.assert_not_awaited()


def test_discovery_fetches_terms_and_records_content(monkeypatch):
    monkeypatch.setattr(tool_workflow, "_model_json", AsyncMock(return_value={"kind": "weather", "read_only": True, "matches": True}))
    calls = []
    async def fetch(self, url):
        calls.append(url)
        return SimpleNamespace(url=url, text=lambda: "<p>真实读取资料的测试桩</p>")
    monkeypatch.setattr(tool_workflow.SafeFetcher, "fetch", fetch)
    proposal = asyncio.run(tool_workflow.build_draft("alice", "", "", "按坐标查当前天气"))
    assert any(url.endswith("/terms") for url in calls)
    assert len(proposal["spec"]["provenance"]) == 2
    assert proposal["status"] == "awaiting_confirmation" and not personal_tools.listing("alice")


def test_http_without_documents_rejected(monkeypatch):
    monkeypatch.setattr(tool_workflow, "_model_json", AsyncMock(return_value={"kind": "http", "read_only": True}))
    monkeypatch.setattr(tool_workflow, "_search", AsyncMock(return_value=[]))
    with pytest.raises(ValueError, match="文档"):
        asyncio.run(tool_workflow.build_draft("alice", "", "", "读取某服务"))
