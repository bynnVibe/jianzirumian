"""聊天附件写入提案：明确确认、持久状态、原子领取，不自动重放。"""
import asyncio
import hashlib
import json
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import HTTPException

from app.config import settings
from app.core import db, observability as obs
from app.core.agent_runtime import runtime
from app.services.tools.base import ToolContext

_TASKS = set()
_EXECUTING = set()
_PUBLIC_FIELDS = ("id", "owner_id", "session_id", "message_id", "file_id", "filename", "tool_name",
                  "kind", "kb_id", "visibility", "status", "error", "created_at", "updated_at", "expires_at")


def _now():
    return datetime.now().isoformat()


def _user(conn, user_id):
    row = conn.execute("SELECT * FROM users WHERE id=? AND is_active=1", (user_id,)).fetchone()
    if not row or user_id == "guest":
        raise HTTPException(403, "仅登录用户可创建或确认入库提案")
    return dict(row)


def _message(conn, row):
    message = conn.execute("SELECT m.attachments FROM chat_messages m JOIN chat_sessions s "
                           "ON s.session_id=m.session_id WHERE m.message_id=? AND m.session_id=? "
                           "AND s.user_id=? AND m.role='user'",
                           (row["message_id"], row["session_id"], row["owner_id"])).fetchone()
    if not message:
        raise HTTPException(409, "原会话或消息已删除，不能确认")
    attachments = json.loads(message["attachments"] or "[]")
    if not any(a.get("file_id") == row["file_id"] and a.get("filename") == row["filename"]
               and a.get("kind") == row["kind"] for a in attachments):
        raise HTTPException(409, "附件与原消息不匹配")


def _load(conn, action_id, user_id):
    _user(conn, user_id)
    row = conn.execute("SELECT * FROM chat_pending_actions WHERE id=? AND owner_id=?", (action_id, user_id)).fetchone()
    if not row:
        raise HTTPException(404, "提案不存在或不属于当前用户")
    row = dict(row)
    _message(conn, row)
    return row


def _target(conn, kb_id, user):
    target = conn.execute("SELECT * FROM knowledge_bases WHERE id=?", (kb_id,)).fetchone()
    if not target:
        raise HTTPException(409, "目标知识库已不存在")
    target = dict(target)
    allowed = user["role"] == "admin" if target["visibility"] == "public" else target["owner_id"] == user["id"]
    if not allowed:
        raise HTTPException(403, "无权写入目标知识库")
    return target


def _snapshot(row):
    path = Path(row["file_path"])
    root = (Path(settings.UPLOAD_DIR) / "chat_attachments").resolve()
    if path.is_symlink() or path.resolve().parent != root or path.stem != row["file_id"]:
        raise HTTPException(409, "附件路径校验失败")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise HTTPException(409, "附件不存在或已清理") from exc
    digest = hashlib.sha256(content).hexdigest()
    if row.get("file_hash") and row["file_hash"] != digest:
        raise HTTPException(409, "附件内容已变化，请重新上传")
    return content, digest


def serialize(row):
    result = {key: row[key] for key in _PUBLIC_FIELDS}
    result["result"] = json.loads(row.get("result") or "{}")
    user = db.query_one("SELECT role FROM users WHERE id=?", (row["owner_id"],)) or {}
    result["targets"] = db.query("SELECT id,name,visibility FROM knowledge_bases WHERE "
                                 "(visibility='private' AND owner_id=?) OR (visibility='public' AND ?='admin') ORDER BY created_at",
                                 (row["owner_id"], user.get("role", "")))
    target = next((t for t in result["targets"] if t["id"] == row["kb_id"]), None)
    result["target_name"] = target["name"] if target else ("确认后创建个人库" if not row["kb_id"] else "目标库已不可写")
    return result


def propose(attachment, user_id, session_id, message_id):
    now = _now()
    row = {"id": str(uuid.uuid4()), "owner_id": user_id, "session_id": session_id,
           "message_id": message_id, "file_id": attachment["file_id"], "filename": attachment["filename"],
           "file_path": attachment["path"], "kind": attachment["kind"], "file_hash": ""}
    _, row["file_hash"] = _snapshot(row)
    with db.transaction() as conn:
        _user(conn, user_id)
        _message(conn, row)
        target = conn.execute("SELECT id FROM knowledge_bases WHERE visibility='private' AND owner_id=? ORDER BY created_at LIMIT 1", (user_id,)).fetchone()
        row.update(tool_name="image_upload" if row["kind"] == "image" else "document_import",
                   kb_id=target["id"] if target else "", visibility="private", status="pending", result="{}", error="",
                   created_at=now, updated_at=now, expires_at=(datetime.now() + timedelta(hours=24)).isoformat())
        columns = list(row)
        conn.execute(f"INSERT OR IGNORE INTO chat_pending_actions ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", tuple(row.values()))
        row = dict(conn.execute("SELECT * FROM chat_pending_actions WHERE session_id=? AND message_id=? AND file_id=?", (session_id, message_id, row["file_id"])).fetchone())
    return serialize(row)


def get_action(action_id, user_id):
    with db.transaction() as conn:
        row = _load(conn, action_id, user_id)
        if row["status"] == "pending" and row["expires_at"] <= _now():
            conn.execute("UPDATE chat_pending_actions SET status='expired',updated_at=? WHERE id=? AND status='pending'", (_now(), action_id))
            row["status"] = "expired"
    return serialize(row)


def history(session_id, user_id):
    db.execute("UPDATE chat_pending_actions SET status='expired',updated_at=? WHERE session_id=? AND owner_id=? AND status='pending' AND expires_at<=?", (_now(), session_id, user_id, _now()))
    return [serialize(row) for row in db.query("SELECT * FROM chat_pending_actions WHERE session_id=? AND owner_id=? ORDER BY created_at", (session_id, user_id))]


def confirm(action_id, user_id, kb_id=None):
    runtime.require_owner()
    get_action(action_id, user_id)
    with db.transaction() as conn:
        row = _load(conn, action_id, user_id)
        if row["status"] in ("running", "succeeded"):
            claimed = False
        elif row["status"] != "pending":
            raise HTTPException(410 if row["status"] == "expired" else 409, f"提案状态为 {row['status']}，不会重新执行")
        elif row["expires_at"] <= _now():
            raise HTTPException(410, "提案已过期")
        else:
            _snapshot(row)
            user = _user(conn, user_id)
            selected = row["kb_id"] if kb_id is None else kb_id
            target = _target(conn, selected, user) if selected else None
            if target is None:
                selected = f"kb-{uuid.uuid4().hex[:12]}"
            claimed = conn.execute("UPDATE chat_pending_actions SET status='running',kb_id=?,visibility=?,updated_at=? WHERE id=? AND status='pending' AND expires_at>?",
                                   (selected, target["visibility"] if target else "private", _now(), action_id, _now())).rowcount == 1
            if not claimed:
                raise HTTPException(409, "提案状态已变化")
            if target is None:
                conn.execute("INSERT INTO knowledge_bases(id,name,topic,visibility,owner_id,created_at) VALUES (?,?,?,'private',?,?)", (selected, "聊天个人库", "其他", user_id, _now()))
            row = dict(conn.execute("SELECT * FROM chat_pending_actions WHERE id=?", (action_id,)).fetchone())
    return serialize(row), claimed


def cancel(action_id, user_id):
    get_action(action_id, user_id)
    with db.transaction() as conn:
        row = _load(conn, action_id, user_id)
        if row["status"] != "pending":
            raise HTTPException(409, "只能取消尚未执行的提案")
        conn.execute("UPDATE chat_pending_actions SET status='cancelled',updated_at=? WHERE id=? AND status='pending'", (_now(), action_id))
        row["status"] = "cancelled"
    return serialize(row)


def invalidate_session(session_id):
    db.execute("UPDATE chat_pending_actions SET status='cancelled',updated_at=?,error='原会话或消息已删除' WHERE session_id=? AND status='pending'", (_now(), session_id))


def validate_execution(ctx):
    """实际写工具再次校验批准记录、身份与目标，拒绝只传 confirmed 的调用。"""
    runtime.require_owner()
    with db.transaction() as conn:
        row = _load(conn, ctx.action_id, ctx.user_id)
        if row["status"] != "running" or row["kb_id"] != ctx.kb_id or row["visibility"] != ctx.visibility:
            raise HTTPException(409, "无有效的已领取入库批准")
        target = _target(conn, ctx.kb_id, _user(conn, ctx.user_id))
        if target["visibility"] != row["visibility"]:
            raise HTTPException(409, "目标知识库可见性已变化")
    return row


async def execute(action_id, user_id):
    runtime.require_owner()
    row = db.query_one("SELECT * FROM chat_pending_actions WHERE id=? AND owner_id=?", (action_id, user_id))
    if action_id in _EXECUTING or not row or row["status"] != "running":
        return
    _EXECUTING.add(action_id)
    obs.start_trace("chat_confirmation", user_id, action_id)
    status, result, error = "failed", {}, ""
    try:
        ctx = ToolContext(user_id=user_id, file_path=row["file_path"], filename=row["filename"], kind=row["kind"],
                          kb_id=row["kb_id"], visibility=row["visibility"], session_id=row["session_id"], message_id=row["message_id"], action_id=action_id)
        validate_execution(ctx)
        content, _ = await asyncio.to_thread(_snapshot, row)
        destination = Path(settings.UPLOAD_DIR) / "confirmed_chat" / action_id / Path(row["file_path"]).name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        ctx.file_path = str(destination)
        from app.services.tools.image_upload import image_upload_tool
        from app.services.tools.document_import import document_import_tool
        tool = image_upload_tool if row["tool_name"] == "image_upload" else document_import_tool
        outcome = await tool.run(ctx)
        result = asdict(outcome)
        status = "succeeded" if outcome.success else "failed"
        error = outcome.error
        obs.record_tool(tool.name, {"action_id": action_id}, result, status="ok" if outcome.success else "error")
    except (asyncio.CancelledError, GeneratorExit):
        status, error = "interrupted", "执行中断，结果可能部分落库；请核对后再操作，不会自动重试"
        raise
    except Exception as exc:
        error = str(exc)
    finally:
        _EXECUTING.discard(action_id)
        db.execute("UPDATE chat_pending_actions SET status=?,result=?,error=?,updated_at=? WHERE id=? AND status='running'",
                   (status, json.dumps(result, ensure_ascii=False), error, _now(), action_id))
        obs.end_trace(status="ok" if status == "succeeded" else "interrupted" if status == "interrupted" else "error", error=error)


def schedule(action_id, user_id):
    task = asyncio.create_task(execute(action_id, user_id))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
