"""
见字如面 - API 共享依赖
统一的登录用户提取 / 管理员校验，供各路由模块复用
"""
from fastapi import Header, HTTPException

from app.core import call_context
from app.services import auth as auth_service


async def get_current_user(authorization: str = Header("")) -> dict:
    """从 Authorization 头解析当前登录用户，未登录抛 401。

    async 依赖在请求任务内执行：此处激活的 CallContext 会传入端点与
    其线程池子任务，使所有 API 路径的出网调用归属登录用户（不计入系统内部）。
    """
    token = ""
    if authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token:
        raise HTTPException(status_code=401, detail="未登录，请先登录")
    user = auth_service.get_session_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    call_context.activate(actor_user_id=user["id"], origin="api")
    return user


async def get_optional_user(authorization: str = Header("")) -> dict | None:
    """从 Authorization 头解析当前用户，未登录时返回 None（用于支持游客访问的端点）"""
    token = ""
    if authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token:
        return None
    user = auth_service.get_session_user(token) or None
    if user:
        call_context.activate(actor_user_id=user["id"], origin="api")
    return user


async def require_admin(authorization: str = Header("")) -> dict:
    """要求当前用户为管理员，否则抛 403"""
    user = await get_current_user(authorization)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")
    return user


async def require_ops(authorization: str = Header("")) -> dict:
    """要求当前用户具备运维权限（管理员或运维角色），否则抛 403。

    用于 CI/CD 流水线、可观测性、回归评测等运维类接口：
    admin 与 ops 均可访问，普通用户/游客拒绝。
    """
    user = await get_current_user(authorization)
    if user.get("role") not in ("admin", "ops"):
        raise HTTPException(status_code=403, detail="仅管理员或运维角色可执行此操作")
    return user


async def require_ops_only(authorization: str = Header("")) -> dict:
    """要求当前用户为运维（ops）角色，否则抛 403。

    专用于 CI/CD 流水线：只允许运维账号操作，管理员（admin）同样被拒绝。
    运维账号本身由管理员在「用户管理」中分配角色来控制。
    """
    user = await get_current_user(authorization)
    if user.get("role") != "ops":
        raise HTTPException(status_code=403, detail="仅运维角色可执行此操作")
    return user
