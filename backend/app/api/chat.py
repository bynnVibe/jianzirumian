"""
见字如面 - 聊天 API 路由 (SSE 流式)
"""
import json
import mimetypes
import re
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.api.deps import get_current_user, get_optional_user
from app.config import settings
from app.core import db
from app.models.schemas import (
    ChatHistoryResponse,
    ChatRequest,
    FeedbackRequest,
    MessageResponse,
    SessionInfo,
    SessionList,
)
from app.services.chat import chat_service, Message
from app.services.guest_limiter import guest_limiter
from app.services.tool_orchestrator import DOC_EXTS, IMAGE_EXTS, detect_kind

router = APIRouter(prefix="/api/chat", tags=["chat"])

# 聊天框附件大小上限（字节）：比知识库上传页略宽松，图片/文档共用
MAX_ATTACHMENT_SIZE = 20 * 1024 * 1024

# 聊天附件独立存放子目录（与知识库上传隔离，便于游客可见的受控分发）
ATTACHMENT_DIR = Path(settings.UPLOAD_DIR) / "chat_attachments"
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


async def _save_attachment(file: UploadFile) -> dict:
    """保存聊天框附件，返回 {file_id, path, filename, kind}；kind 为 None 表示不支持的类型"""
    ext = Path(file.filename or "").suffix.lower()
    kind = detect_kind(file.filename or "")
    if not kind:
        raise HTTPException(
            status_code=400,
            detail=f"附件 {file.filename} 不是支持的类型（支持图片或 {', '.join(DOC_EXTS.keys())}）",
        )

    content = await file.read()
    if len(content) > MAX_ATTACHMENT_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"附件 {file.filename} 超过大小限制 ({MAX_ATTACHMENT_SIZE // 1024 // 1024}MB)",
        )

    file_id = str(uuid.uuid4())
    save_path = ATTACHMENT_DIR / f"{file_id}{ext}"
    save_path.parent.mkdir(parents=True, exist_ok=True)
    with open(save_path, "wb") as f:
        f.write(content)

    if kind == "word":
        from app.api.knowledge import _normalize_docx
        _normalize_docx(save_path)

    return {"file_id": file_id, "path": str(save_path), "filename": file.filename, "kind": kind}


@router.post("/send")
async def send_message(
    request: Request,
    body: ChatRequest,
    user: dict | None = Depends(get_optional_user),
):
    """发送消息（流式响应）

    返回 SSE (Server-Sent Events) 流:
      data: {"event": "status", "phase": "retrieving", ...}
      data: {"event": "token", "content": "..."}
      data: {"event": "done", "session_id": "...", "message": {...}}
    """
    is_guest = getattr(request.state, "is_guest", False)

    # 游客查询限制检查
    if is_guest:
        client_ip = request.client.host if request.client else "unknown"
        allowed, remaining = guest_limiter.check_limit(client_ip)
        if not allowed:
            return JSONResponse(
                status_code=403,
                content={"detail": "游客查询次数已用完，请登录后继续使用"},
            )

    user_id = user["id"] if user else "guest"

    session_id = body.session_id

    # 如果未指定 session_id, 自动创建新会话；若指定则由 ChatService 按需加载校验
    if not session_id:
        session = chat_service.create_session(user_id=user_id)
        session_id = session.session_id

    async def event_stream():
        async for event in chat_service.chat_stream(
            session_id=session_id,
            query=body.query,
            use_knowledge=body.use_knowledge,
            use_search=body.use_search,
            llm_provider=body.llm_provider,
            user_id=user_id,
        ):
            yield f"{event}\n"

        # 流式响应结束后，游客计数+1
        if is_guest:
            client_ip = request.client.host if request.client else "unknown"
            guest_limiter.increment(client_ip)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Session-Id": session_id,
        },
    )


@router.post("/send-with-files")
async def send_message_with_files(
    request: Request,
    message: str = Form(...),
    session_id: str = Form(""),
    use_knowledge: bool = Form(True),
    use_search: bool = Form(False),
    llm_provider: str = Form(""),
    kb_id: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    user: dict | None = Depends(get_optional_user),
):
    """发送带附件的消息（multipart/form-data）

    支持拖入图片/Word/PDF 结合文本触发工具调用（识别/入库/总结，取决于用户文本意图）。
    旧版无附件前端仍使用 /send 纯 JSON 接口，两路共存。

    返回 SSE 事件流，附件处理过程额外包含:
      data: {"event": "status", "phase": "tool_processing", ...}
      data: {"event": "status", "phase": "tool_done", "tool_calls": [...]}
    """
    is_guest = getattr(request.state, "is_guest", False)

    if is_guest:
        client_ip = request.client.host if request.client else "unknown"
        allowed, remaining = guest_limiter.check_limit(client_ip)
        if not allowed:
            return JSONResponse(
                status_code=403,
                content={"detail": "游客查询次数已用完，请登录后继续使用"},
            )

    user_id = user["id"] if user else "guest"

    if not session_id:
        session = chat_service.create_session(user_id=user_id)
        session_id = session.session_id

    attachments = []
    for f in files:
        if not f.filename:
            continue
        attachments.append(await _save_attachment(f))

    async def event_stream():
        async for event in chat_service.chat_stream(
            session_id=session_id,
            query=message,
            use_knowledge=use_knowledge,
            use_search=use_search,
            llm_provider=llm_provider or None,
            user_id=user_id,
            attachments=attachments,
            kb_id=kb_id,
        ):
            yield f"{event}\n"

        if is_guest:
            client_ip = request.client.host if request.client else "unknown"
            guest_limiter.increment(client_ip)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Session-Id": session_id,
        },
    )


@router.get("/attachment/{file_id}")
async def get_attachment(file_id: str):
    """分发聊天附件原文件（游客可用）

    附件存于独立子目录且文件名为不可猜测的 UUID，
    仅允许 UUID 格式 id + 白名单扩展名，防止路径穿越与任意文件读取。
    """
    if not _UUID_RE.match(file_id or ""):
        raise HTTPException(status_code=404, detail="附件不存在")

    matches = [p for p in ATTACHMENT_DIR.glob(f"{file_id}.*")
               if p.suffix.lower() in IMAGE_EXTS or p.suffix.lower() in DOC_EXTS]
    if not matches:
        raise HTTPException(status_code=404, detail="附件不存在或已清理")

    path = matches[0]
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    # inline：浏览器内嵌预览（图片/PDF）；前端下载按钮用 download 属性强制另存
    return FileResponse(path, media_type=media_type, filename=path.name,
                        content_disposition_type="inline")


@router.get("/history/{session_id}", response_model=ChatHistoryResponse)
async def get_chat_history(session_id: str, user: dict = Depends(get_current_user)):
    """获取聊天历史（仅限本人会话，含当前用户的反馈状态）"""
    session = chat_service.get_user_session(session_id, user["id"])
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")

    # 当前用户对该会话消息的反馈
    feedback_rows = db.query(
        "SELECT message_id, rating, comment FROM message_feedbacks"
        " WHERE session_id = ? AND user_id = ?",
        (session_id, user["id"]),
    )
    feedback_map = {r["message_id"]: {"rating": r["rating"], "comment": r["comment"]}
                    for r in feedback_rows}

    messages = []
    for m in session.messages:
        d = m.to_dict()
        d["feedback"] = feedback_map.get(m.message_id)
        messages.append(d)

    return ChatHistoryResponse(
        session_id=session.session_id,
        title=session.title,
        messages=messages,
    )


@router.delete("/history/{session_id}")
async def clear_chat_history(session_id: str, user: dict = Depends(get_current_user)):
    """清空聊天历史（仅限本人会话）"""
    session = chat_service.get_user_session(session_id, user["id"])
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")
    session.messages = []
    db.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
    db.execute("DELETE FROM chat_session_summaries WHERE session_id = ?", (session_id,))
    chat_service._persist_session(session)
    return {"message": "聊天历史已清空"}


@router.post("/feedback")
async def submit_feedback(body: FeedbackRequest, user: dict = Depends(get_current_user)):
    """对回答点赞/点踩（可附备注），同一用户对同一消息仅保留一条反馈"""
    if body.rating not in ("like", "dislike"):
        raise HTTPException(status_code=400, detail="rating 必须是 like 或 dislike")

    session = chat_service.get_user_session(body.session_id, user["id"])
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")
    if not any(m.message_id == body.message_id for m in session.messages):
        raise HTTPException(status_code=404, detail="消息不存在")

    from datetime import datetime
    import uuid as uuid_lib
    db.execute(
        "INSERT INTO message_feedbacks (id, session_id, message_id, user_id, rating, comment, created_at)"
        " VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(message_id, user_id) DO UPDATE SET"
        " rating = excluded.rating, comment = excluded.comment",
        (
            str(uuid_lib.uuid4()), body.session_id, body.message_id,
            user["id"], body.rating, body.comment.strip(),
            datetime.now().isoformat(),
        ),
    )
    return {"success": True, "message": "反馈已记录"}


@router.post("/sessions", response_model=SessionInfo)
async def create_session(user: dict | None = Depends(get_optional_user)):
    """创建新会话"""
    user_id = user["id"] if user else "guest"
    session = chat_service.create_session(user_id=user_id)
    return SessionInfo(
        session_id=session.session_id,
        title=session.title,
        created_at=session.created_at,
        updated_at=session.updated_at,
        message_count=0,
    )


@router.get("/sessions", response_model=SessionList)
async def list_sessions(user: dict = Depends(get_current_user)):
    """获取会话列表"""
    sessions = chat_service.list_sessions(user_id=user["id"])
    return SessionList(
        sessions=[SessionInfo(**s) for s in sessions],
        current_session_id=None,  # 当前会话由前端自行维护
    )


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, user: dict = Depends(get_current_user)):
    """删除会话"""
    ok = chat_service.delete_session(session_id, user_id=user["id"])
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在或无权删除")
    return {"message": "会话已删除"}


@router.post("/sessions/{session_id}/switch")
async def switch_session(session_id: str, user: dict = Depends(get_current_user)):
    """切换会话（仅校验存在性与归属，当前会话状态由前端维护）"""
    session = chat_service.get_user_session(session_id, user["id"])
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")
    return SessionInfo(
        session_id=session.session_id,
        title=session.title,
        created_at=session.created_at,
        updated_at=session.updated_at,
        message_count=len(session.messages),
    )
