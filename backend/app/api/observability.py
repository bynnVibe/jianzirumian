"""
见字如面 - Agent 可观测性 API（管理员 / 运维角色）

提供三层可观测性的在线查询入口：
- GET /api/observability/metrics：聚合指标（Token 消耗 / 响应延迟 / 成功率 / 工具调用次数）
- GET /api/observability/traces：trace 列表（一次用户请求 = 一条 trace）
- GET /api/observability/traces/{id}：trace 全链路详情（含每次 LLM 调用的完整 prompt/response）
- GET /api/observability/skill-reviews：Skill 添加/修改预审记录（安全与能力可行性，拒绝行为重点暴露）
"""
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.api.deps import get_current_user, require_ops
from app.core import observability as obs
from app.services import skill_review

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/observability", tags=["observability"])


@router.get("/my-metrics")
def get_my_metrics(
    response: Response,
    days: int = Query(7, ge=1, le=30),
    user: dict = Depends(get_current_user),
):
    """登录用户的个人业务概览；身份只取服务端会话，不接受查询目标用户。"""
    user_id = user.get("id")
    if user.get("role") not in ("user", "admin", "ops") or not user_id or user_id in ("guest", "system", "-"):
        raise HTTPException(status_code=403, detail="请使用正式账号查看个人使用概览")
    response.headers["Cache-Control"] = "no-store"
    return {"success": True, **obs.personal_metrics(user_id, days)}


@router.get("/my-context")
def get_my_context(
    response: Response,
    days: int = Query(7, ge=1, le=30),
    user: dict = Depends(get_current_user),
):
    """当前登录用户的「上下文窗口 + 记忆模块」使用概览；身份只取服务端会话。"""
    user_id = user.get("id")
    if user.get("role") not in ("user", "admin", "ops") or not user_id or user_id in ("guest", "system", "-"):
        raise HTTPException(status_code=403, detail="请使用正式账号查看个人上下文与记忆概览")
    response.headers["Cache-Control"] = "no-store"
    try:
        data = obs.personal_context_overview(user_id, days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, **data}


@router.get("/my-context/sessions")
def get_my_context_sessions(
    response: Response,
    days: int = Query(7, ge=1, le=30),
    limit: int = Query(20, ge=1, le=50),
    user: dict = Depends(get_current_user),
):
    """按对话窗口下钻的上下文构成：个人面板总集口径的会话级子集切片。"""
    user_id = user.get("id")
    if user.get("role") not in ("user", "admin", "ops") or not user_id or user_id in ("guest", "system", "-"):
        raise HTTPException(status_code=403, detail="请使用正式账号查看按对话的上下文下钻")
    response.headers["Cache-Control"] = "no-store"
    try:
        data = obs.personal_session_contexts(user_id, days, limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, **data}


@router.get("/metrics")
async def get_metrics(days: int = Query(7, ge=1, le=90), _admin: dict = Depends(require_ops)):
    """聚合指标：总览 + 每日趋势 + 按 Agent + 按工具（监控与告警数据源）"""
    return {"success": True, **obs.metrics(days)}


@router.get("/skill-reviews")
async def get_skill_reviews(
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _admin: dict = Depends(require_ops),
):
    """Skill 预审记录：汇总（拒绝数/高危/能力不支持）+ 明细列表（按时间倒序）"""
    return {"success": True, **skill_review.list_reviews(limit, offset)}


@router.get("/traces")
async def list_traces(
    agent: str = Query(""),
    status: str = Query(""),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _admin: dict = Depends(require_ops),
):
    """trace 列表（按时间倒序，可按 Agent / 状态过滤）"""
    return {"success": True, **obs.list_traces(agent, status, limit, offset)}


@router.get("/traces/{trace_id}")
async def get_trace(trace_id: str, _admin: dict = Depends(require_ops)):
    """trace 全链路详情：所有步骤 span（llm span 含完整 prompt 与 response）"""
    trace = obs.get_trace(trace_id)
    if not trace:
        return {"success": False, "detail": "trace 不存在"}
    return {"success": True, "trace": trace}


@router.get("/traces/{trace_id}/logs")
def get_trace_logs(
    trace_id: UUID,
    response: Response,
    cursor: str = Query("", max_length=40),
    limit: int = Query(100, ge=1, le=200),
    _admin: dict = Depends(require_ops),
):
    """分页读取请求日志；同步端点在线程池中执行，避免文件扫描阻塞事件循环。"""
    try:
        result = obs.get_trace_logs(str(trace_id), cursor, limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="trace 不存在或已清理")
    response.headers["Cache-Control"] = "no-store"
    return {"success": True, **result}
