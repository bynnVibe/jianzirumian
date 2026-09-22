"""
见字如面 - Agent 可观测性 API（仅管理员）

提供三层可观测性的在线查询入口：
- GET /api/observability/metrics：聚合指标（Token 消耗 / 响应延迟 / 成功率 / 工具调用次数）
- GET /api/observability/traces：trace 列表（一次用户请求 = 一条 trace）
- GET /api/observability/traces/{id}：trace 全链路详情（含每次 LLM 调用的完整 prompt/response）
"""
import logging

from fastapi import APIRouter, Depends, Query

from app.api.deps import require_admin
from app.core import observability as obs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/observability", tags=["observability"])


@router.get("/metrics")
async def get_metrics(days: int = Query(7, ge=1, le=90), _admin: dict = Depends(require_admin)):
    """聚合指标：总览 + 每日趋势 + 按 Agent + 按工具（监控与告警数据源）"""
    return {"success": True, **obs.metrics(days)}


@router.get("/traces")
async def list_traces(
    agent: str = Query(""),
    status: str = Query(""),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _admin: dict = Depends(require_admin),
):
    """trace 列表（按时间倒序，可按 Agent / 状态过滤）"""
    return {"success": True, **obs.list_traces(agent, status, limit, offset)}


@router.get("/traces/{trace_id}")
async def get_trace(trace_id: str, _admin: dict = Depends(require_admin)):
    """trace 全链路详情：所有步骤 span（llm span 含完整 prompt 与 response）"""
    trace = obs.get_trace(trace_id)
    if not trace:
        return {"success": False, "detail": "trace 不存在"}
    return {"success": True, "trace": trace}
