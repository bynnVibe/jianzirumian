"""
见字如面 - API 共享依赖
统一的登录用户提取 / 管理员校验，供各路由模块复用
"""
from fastapi import Header, HTTPException

from app.services import auth as auth_service


def get_current_user(authorization: str = Header("")) -> dict:
    """从 Authorization 头解析当前登录用户，未登录抛 401"""
    token = ""
    if authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token:
        raise HTTPException(status_code=401, detail="未登录，请先登录")
    user = auth_service.get_session_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    return user


def get_optional_user(authorization: str = Header("")) -> dict | None:
    """从 Authorization 头解析当前用户，未登录时返回 None（用于支持游客访问的端点）"""
    token = ""
    if authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token:
        return None
    return auth_service.get_session_user(token) or None


def require_admin(authorization: str = Header("")) -> dict:
    """要求当前用户为管理员，否则抛 403"""
    user = get_current_user(authorization)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")
    return user
