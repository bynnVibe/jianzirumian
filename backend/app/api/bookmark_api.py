"""
见字如面 - 知识收藏 API 路由
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Header, Request
from pydantic import BaseModel

from app.api.deps import get_optional_user
from app.services import auth as auth_service
from app.services.bookmark import (
    create_bookmark,
    list_bookmarks,
    delete_bookmark,
)

logger = logging.getLogger("jianziruyang.bookmark")
router = APIRouter(prefix="/api/bookmarks", tags=["bookmarks"])


def _get_user(authorization: str = Header("")) -> dict:
    """从请求头获取当前用户"""
    token = ""
    if authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token:
        raise HTTPException(401, "未登录")
    user = auth_service.get_session_user(token)
    if not user:
        raise HTTPException(401, "登录已过期，请重新登录")
    return user


class CreateBookmarkRequest(BaseModel):
    title: str
    tags: list[str] = []
    content: str
    source_type: str = "knowledge"
    source_info: dict = {}


@router.post("")
async def create(body: CreateBookmarkRequest, authorization: str = Header("")):
    """创建知识收藏"""
    user = _get_user(authorization)
    if not body.content.strip():
        raise HTTPException(400, "收藏内容不能为空")
    if not body.title.strip():
        raise HTTPException(400, "收藏标题不能为空")

    bookmark = create_bookmark(
        user_id=user["id"],
        title=body.title,
        tags=body.tags,
        content=body.content,
        source_type=body.source_type,
        source_info=body.source_info,
    )
    return {"success": True, "bookmark": bookmark}


@router.get("")
async def list_all(
    request: Request,
    current_user: dict | None = Depends(get_optional_user),
):
    """获取当前用户的收藏列表（游客返回空列表）"""
    is_guest = getattr(request.state, "is_guest", False)
    if is_guest or not current_user:
        return {"success": True, "bookmarks": []}
    bookmarks = list_bookmarks(current_user["id"])
    return {"success": True, "bookmarks": bookmarks}


@router.delete("/{bookmark_id}")
async def remove(bookmark_id: str, authorization: str = Header("")):
    """删除收藏"""
    user = _get_user(authorization)
    ok = delete_bookmark(user["id"], bookmark_id)
    if not ok:
        raise HTTPException(404, "收藏不存在或无权删除")
    return {"success": True, "message": "收藏已删除"}
