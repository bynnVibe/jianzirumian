"""
见字如面 - 资源变更同步 API（前端查询缓存的失效推送）

- GET /api/sync/versions：授权范围内的一致版本快照 + 水位线（兜底轮询用）；
- GET /api/sync/events：带鉴权的 fetch SSE（Authorization 放请求头，不放 URL）。
  每标签页一条连接；心跳 15s；按资源合并限频（≤1 次/秒）；游标补发 24h 窗口，
  超窗/游标失效发 reset；每次心跳只读校验会话（不续期），注销/过期/撤权即关闭。

事件仅含资源域与版本号，不携带业务正文/秘密/Token。
"""
import asyncio
import json
import time

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse

from app.api.deps import get_current_user
from app.services import auth as auth_service
from app.services import sync_events

router = APIRouter(prefix="/api/sync", tags=["sync"])

_HEARTBEAT_SECONDS = 15
_POLL_SECONDS = 1


def _sse(event: str, data: dict, event_id: int = 0) -> str:
    lines = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"


def _can_ops(user: dict) -> bool:
    return user.get("role") in ("admin", "ops")


@router.get("/versions")
async def versions(user: dict = Depends(get_current_user)):
    """版本快照 + 水位线：SSE 失联退化为轻量轮询时的兜底入口。"""
    snap = await asyncio.to_thread(sync_events.snapshot, user["id"], _can_ops(user))
    return {"success": True, **snap}


@router.get("/events")
async def events(request: Request, authorization: str = Header(""),
                 since: int = 0, last_event_id: str = Header("")):
    """变更 SSE：首连发 snapshot 衔接水位线，其后按游标补发增量。"""
    user = await get_current_user(authorization)
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    try:
        cursor = int(last_event_id or since or 0)
    except (TypeError, ValueError):
        cursor = 0

    async def stream():
        snap = await asyncio.to_thread(sync_events.snapshot, user["id"], _can_ops(user))
        yield _sse("snapshot", snap)
        # 首连从快照水位线起订阅（快照与水位线同事务读取，不漏事件）；
        # 重连携带游标则先补发窗口内增量
        cur = cursor if cursor > 0 else snap["watermark"]
        last_emit = time.monotonic()
        while True:
            if await request.is_disconnected():
                break
            # 只读校验会话与权限（不续期）：注销/过期/撤权立即关闭连接
            current = await asyncio.to_thread(auth_service.peek_session_user, token)
            if not current or not current.get("is_active", True):
                break
            batch = await asyncio.to_thread(
                sync_events.events_after, cur, current["id"], _can_ops(current))
            if batch["reset"]:
                snap = await asyncio.to_thread(
                    sync_events.snapshot, current["id"], _can_ops(current))
                yield _sse("reset", snap)
                cur = snap["watermark"]
                last_emit = time.monotonic()
            elif batch["events"]:
                # 按资源合并取最新版本：高频变化（用量/监控）每秒至多一条通知
                merged = {}
                for ev in batch["events"]:
                    merged[ev["resource"]] = ev
                cur = batch["events"][-1]["id"]
                for ev in merged.values():
                    yield _sse("change", {"resource": ev["resource"],
                                          "version": ev["version"], "id": ev["id"]},
                               event_id=cur)
                last_emit = time.monotonic()
            if time.monotonic() - last_emit >= _HEARTBEAT_SECONDS:
                yield ": heartbeat\n\n"
                last_emit = time.monotonic()
            await asyncio.sleep(_POLL_SECONDS)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",  # Nginx 代理不缓冲 SSE
    })