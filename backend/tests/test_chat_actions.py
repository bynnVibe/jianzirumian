"""确认卡片权限、幂等、重启和聊天取消的隔离回归。"""
import asyncio
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.config import settings
from app.core import db
from app.core.agent_runtime import AgentRuntime
from app.services import chat_actions, tool_orchestrator
from app.services.chat import ChatService, Message
from app.services.tools.base import ToolResult
from app.models.schemas import ConfirmChatActionRequest


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "uploads"))
    for user, role in (("alice", "user"), ("bob", "admin")):
        db.execute("INSERT INTO users(id,username,password_hash,salt,role,created_at,updated_at) VALUES (?,?,?, ?,?,0,0)", (user, user, "synthetic", "synthetic", role))
    runtime = AgentRuntime()
    runtime.acquire()
    monkeypatch.setattr(chat_actions, "runtime", runtime)
    service = ChatService()
    session = service.create_session(user_id="alice")
    file_id = str(uuid.uuid4())
    path = Path(settings.UPLOAD_DIR) / "chat_attachments" / f"{file_id}.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic attachment")
    attachment = {"file_id": file_id, "path": str(path), "filename": "test.png", "kind": "image"}
    message = Message("user", "保存到知识库", attachments=[{k: v for k, v in attachment.items() if k != "path"}])
    session.add_message(message)
    service._persist_session(session)
    yield service, session, message, attachment, runtime
    runtime._lock.release()
    db._conn.close()


def proposal(context):
    _, session, message, attachment, _ = context
    return chat_actions.propose(attachment, "alice", session.session_id, message.message_id)


def test_proposal_never_writes_and_history_restores(context, monkeypatch):
    _, session, message, attachment, _ = context
    write = AsyncMock(side_effect=AssertionError("不应执行写工具"))
    monkeypatch.setattr(tool_orchestrator.image_upload_tool, "run", write)
    text, records = asyncio.run(tool_orchestrator.handle_attachments("保存到知识库", [attachment], "alice", "", session.session_id, message.message_id))
    assert "尚未执行" in text
    assert records[0]["confirmation"]["visibility"] == "private"
    assert db.query("SELECT * FROM knowledge_bases") == []
    assert db.query("SELECT * FROM tool_calls") == []
    restored = chat_actions.history(session.session_id, "alice")
    assert restored[0]["id"] == records[0]["confirmation"]["id"]
    assert "file_path" not in restored[0] and "file_hash" not in restored[0]
    write.assert_not_awaited()


@pytest.mark.parametrize("query", ["不要上传，只识别", "不要保存，帮我总结", "别入库", "解释‘保存到知识库’", '他说"上传"', "是否保存？", "如果可以就保存", "上传还是总结", "如何入库"])
def test_conservative_intent(query):
    assert tool_orchestrator._decide_action(query) != "upload"


def test_confirm_exactly_once_and_result_reused(context, monkeypatch):
    action = proposal(context)
    first, claimed = chat_actions.confirm(action["id"], "alice")
    assert claimed and first["status"] == "running"
    assert first["kb_id"] and first["visibility"] == "private"
    assert not chat_actions.confirm(action["id"], "alice")[1]
    run = AsyncMock(return_value=ToolResult("image_upload", True, chunk_count=2))
    monkeypatch.setattr(tool_orchestrator.image_upload_tool, "run", run)
    asyncio.run(chat_actions.execute(action["id"], "alice"))
    result, claimed = chat_actions.confirm(action["id"], "alice")
    assert result["status"] == "succeeded" and result["result"]["chunk_count"] == 2 and not claimed
    run.assert_awaited_once()


@pytest.mark.parametrize("change", ["owner", "file", "message", "session", "expires", "cancel", "failed", "interrupted"])
def test_invalid_confirm_never_claims(context, change):
    service, session, message, attachment, runtime = context
    action = proposal(context)
    user = "alice"
    if change == "owner":
        user = "bob"
    elif change == "file":
        Path(attachment["path"]).write_bytes(b"changed")
    elif change == "message":
        db.execute("DELETE FROM chat_messages")
    elif change == "session":
        service.delete_session(session.session_id, "alice")
    elif change == "expires":
        db.execute("UPDATE chat_pending_actions SET expires_at='2000-01-01'")
    elif change == "cancel":
        chat_actions.cancel(action["id"], "alice")
    elif change == "failed":
        db.execute("UPDATE chat_pending_actions SET status='failed'")
    elif change == "interrupted":
        chat_actions.confirm(action["id"], "alice")
        runtime.recover()
    with pytest.raises(HTTPException):
        chat_actions.confirm(action["id"], user)
    assert db.query_one("SELECT status FROM chat_pending_actions")["status"] != "running"


def test_private_and_public_target_permissions(context):
    action = proposal(context)
    db.execute("INSERT INTO knowledge_bases(id,name,topic,visibility,owner_id,created_at) VALUES ('private','other','其他','private','bob','now')")
    db.execute("INSERT INTO knowledge_bases(id,name,topic,visibility,owner_id,created_at) VALUES ('public','public','其他','public','','now')")
    for target in ("private", "public", "missing"):
        with pytest.raises(HTTPException):
            chat_actions.confirm(action["id"], "alice", target)
    assert db.query_one("SELECT status FROM chat_pending_actions")["status"] == "pending"


def test_execution_rechecks_target_permission(context, monkeypatch):
    action, _ = chat_actions.confirm(proposal(context)["id"], "alice")
    db.execute("UPDATE knowledge_bases SET owner_id='bob' WHERE id=?", (action["kb_id"],))
    run = AsyncMock(side_effect=AssertionError("权限变化后不应执行"))
    monkeypatch.setattr(tool_orchestrator.image_upload_tool, "run", run)
    asyncio.run(chat_actions.execute(action["id"], "alice"))
    assert chat_actions.get_action(action["id"], "alice")["status"] == "failed"
    run.assert_not_awaited()


def test_confirmation_rejects_arbitrary_payload():
    for data in ({"confirmed": True}, {"file_path": "/secret"}, {"tool_name": "arbitrary"}):
        with pytest.raises(ValidationError):
            ConfirmChatActionRequest(**data)


@pytest.mark.parametrize("mode,expected", [("cancel", "cancelled"), ("timeout", "timeout")])
def test_chat_partial_is_interrupted_and_generator_closed(context, monkeypatch, mode, expected):
    service, session, _, _, _ = context
    closed = []
    async def inner(*args):
        try:
            yield 'data: {"event":"token","content":"partial"}\n\n'
            await asyncio.sleep(1)
        finally:
            closed.append(True)
    monkeypatch.setattr(service, "_chat_stream_inner", inner)
    monkeypatch.setattr(settings, "CHAT_REQUEST_TIMEOUT", 0.03)
    async def run():
        stream = service.chat_stream(session.session_id, "q", user_id="alice")
        if mode == "cancel":
            await anext(stream)
            await stream.aclose()
        else:
            events = [event async for event in stream]
            assert "request_timeout" in events[-1]
    asyncio.run(run())
    assert closed == [True]
    assert db.query_one("SELECT status FROM agent_traces")["status"] == expected
    assert "内容不完整" in session.messages[-1].content
    restored = ChatService().get_session(session.session_id)
    assert restored.messages[-1].interrupted is True


def test_concurrent_confirmation_api_schedules_once(context, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from app.api import chat as api
    action = proposal(context)
    scheduled = []
    monkeypatch.setattr(chat_actions, "schedule", lambda *args: scheduled.append(args))
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_current_user] = lambda: {"id": "alice"}
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            responses = await asyncio.gather(*[client.post(f"/api/chat/actions/{action['id']}/confirm", json={}) for _ in range(8)])
            assert all(r.status_code == 202 for r in responses)
            assert len(scheduled) == 1
            app.dependency_overrides[api.get_current_user] = lambda: {"id": "bob", "role": "admin"}
            assert (await client.get(f"/api/chat/actions/{action['id']}")).status_code == 404
    asyncio.run(run())


def test_duplicate_executor_does_not_repeat_write(context, monkeypatch):
    action, _ = chat_actions.confirm(proposal(context)["id"], "alice")
    writes = []
    async def write(ctx):
        writes.append(ctx.action_id)
        await asyncio.sleep(0.01)
        return ToolResult("image_upload", True)
    monkeypatch.setattr(tool_orchestrator.image_upload_tool, "run", write)
    async def run():
        await asyncio.gather(*[chat_actions.execute(action["id"], "alice") for _ in range(5)])
    asyncio.run(run())
    assert writes == [action["id"]]


def test_one_attachment_failure_does_not_replay_success(context, monkeypatch):
    service, session, message, attachment, _ = context
    first = proposal(context)
    other = dict(attachment, file_id=str(uuid.uuid4()))
    other["path"] = str(Path(attachment["path"]).with_name(other["file_id"] + ".png"))
    Path(other["path"]).write_bytes(b"second")
    message.attachments.append({k: v for k, v in other.items() if k != "path"})
    service._persist_session(session)
    second = chat_actions.propose(other, "alice", session.session_id, message.message_id)
    writes = []
    async def write(ctx):
        writes.append(ctx.action_id)
        return ToolResult("image_upload", ctx.action_id == first["id"], error="" if ctx.action_id == first["id"] else "合成失败")
    monkeypatch.setattr(tool_orchestrator.image_upload_tool, "run", write)
    for action in (first, second):
        chat_actions.confirm(action["id"], "alice")
        asyncio.run(chat_actions.execute(action["id"], "alice"))
    assert chat_actions.confirm(first["id"], "alice")[0]["status"] == "succeeded"
    with pytest.raises(HTTPException):
        chat_actions.confirm(second["id"], "alice")
    assert writes == [first["id"], second["id"]]


def test_delete_during_generation_never_restores_messages(context, monkeypatch):
    service, session, _, _, _ = context
    async def inner(*args):
        yield 'data: {"event":"token","content":"partial"}\n\n'
        await asyncio.sleep(10)
    monkeypatch.setattr(service, "_chat_stream_inner", inner)
    async def run():
        async def consume():
            async for _ in service.chat_stream(session.session_id, "q", user_id="alice"):
                pass
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.01)
        assert service.delete_session(session.session_id, "alice")
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert db.query("SELECT * FROM chat_messages") == []
    assert db.query("SELECT * FROM chat_sessions") == []
