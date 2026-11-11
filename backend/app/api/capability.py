"""
见字如面 - 能力缺口 CLI 兜底 API（需求5：HITL 选择方案 + 沙箱执行 + 风险确认）

聊天流在检测到能力缺口时会随 SSE 下发 capability_gap 事件（携带候选方案）；
用户在前端选定方案后调用本模块 POST /api/capability-gap/execute 执行：
  - 低风险方案直接在沙箱（CliRunner）执行并返回 stdout；
  - high/critical 风险方案首次调用返回 needs_confirmation，前端弹窗二次确认后
    携带 confirmed=true 再次调用才真正执行。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import get_current_user
from app.services import capability_gap
from app.services.agent_orchestrator import orchestrator
from app.services.sandbox import sandbox_controller

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/capability-gap", tags=["capability-gap"])


class ExecuteBody(BaseModel):
    gap_id: str = Field(..., min_length=1)
    solution_id: str = Field(..., min_length=1)
    confirmed: bool = False
    url: str = Field("", max_length=500)


class OrchestrateBody(BaseModel):
    """图5 主干编排入口：需求契约 → 能力目录 → 策略/预算 → 统一执行 → 证据验证。"""

    query: str = Field(..., min_length=1, max_length=1000)
    timezone: str = Field("", max_length=64)
    allow_external: bool = False
    approved_capability: str = Field("", max_length=128)


@router.post("/execute")
async def execute(body: ExecuteBody, user: dict = Depends(get_current_user)):
    """执行用户选定的能力缺口方案（沙箱 + 风险确认），并把结果回喂 Agent 判定下一步（需求8）。"""
    if user.get("id") in ("guest",):
        # 游客不允许触发命令行执行（安全边界）
        raise HTTPException(status_code=403, detail="游客不能执行命令行方案，请先登录")
    logger.info("[CAPABILITY_GAP] 用户=%s 执行方案 gap=%s solution=%s confirmed=%s",
                user.get("username") or user.get("id"), body.gap_id, body.solution_id, body.confirmed)
    result = await capability_gap.execute_and_continue(
        body.gap_id, body.solution_id, confirmed=body.confirmed, url=body.url
    )
    if result.get("resolved"):
        logger.info("[CAPABILITY_GAP] gap=%s 已解决并回喂回答（%d 字）", body.gap_id,
                    len(result.get("followup_answer") or ""))
    elif result.get("next_solutions"):
        logger.info("[CAPABILITY_GAP] gap=%s 未解决，回喂判定需下一步方案=%s", body.gap_id,
                    [s["id"] for s in result["next_solutions"]])
    if not result.get("success") and not result.get("needs_confirmation"):
        # 执行失败仍返回 200 + success=false，前端在卡片内展示错误而非整页报错
        return {"success": False, **result}
    return {"success": True, **result}


@router.post("/orchestrate")
async def orchestrate(body: OrchestrateBody, user: dict = Depends(get_current_user)):
    """图5 主干：经需求契约/能力目录/策略引擎/预算账本/统一执行入口/证据验证运行一轮。

    返回三层状态（execution/validation/resolution）与 SSE 事件序列，防假成功：
    缺参数不猜测、隔离不可用不回退主机、判定异常保留 unknown/unresolved。
    """
    if user.get("id") in ("guest",):
        raise HTTPException(status_code=403, detail="游客不能触发能力编排，请先登录")
    logger.info("[ORCHESTRATE] 用户=%s query=%s external=%s",
                user.get("username") or user.get("id"), body.query[:60], body.allow_external)
    out = await orchestrator.run(
        body.query,
        user=user,
        allow_external=body.allow_external,
        timezone=body.timezone,
        approved_capability=body.approved_capability,
    )
    return out


@router.get("/sandbox/profiles")
async def sandbox_profiles(user: dict = Depends(get_current_user)):
    """可信 Sandbox Controller 的运行配置（fixed_adapter / isolated 可用性）。

    隔离运行时未配置时 isolated.available=false，动态执行将返回 sandbox_unavailable
    而非回退主机子进程（§3.1 第 8 条）。
    """
    return {"profiles": sandbox_controller.list_profiles()}
