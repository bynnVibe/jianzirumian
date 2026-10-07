"""
见字如面 - CI/CD 流水线 API（管理员 / 运维角色）

面向 Agent 系统的评估驱动部署（EDD）流水线操作入口：
- POST   /api/pipeline/runs            发起一次流水线运行
- GET    /api/pipeline/runs            运行历史
- GET    /api/pipeline/runs/{id}       运行详情（六阶段状态 + 日志）
- POST   /api/pipeline/runs/{id}/abort 人工中止运行
- GET    /api/pipeline/settings        读取 EDD 阈值/评测集/各阶段命令配置
- PUT    /api/pipeline/settings        保存配置
- GET    /api/pipeline/stages          阶段元信息（名称/标签，供前端流程图渲染）

所有端点均通过 require_ops 校验（admin 与 ops 角色均可访问）。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import require_ops
from app.services import pipeline

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


class RunBody(BaseModel):
    git_ref: str = ""
    trigger: str = "manual"


class SettingsBody(BaseModel):
    edd_threshold: float | None = None
    edd_dataset_id: str | None = None
    run_unit_test: bool | None = None
    run_build_image: bool | None = None
    build_cmd: str | None = None
    canary_cmd: str | None = None
    full_cmd: str | None = None


@router.get("/stages")
async def get_stages(_ops: dict = Depends(require_ops)):
    """流水线阶段元信息（前端流程图渲染用）"""
    return {
        "success": True,
        "stages": [{"name": n, "label": lb} for n, lb in pipeline.STAGES],
    }


@router.get("/settings")
async def get_settings(_ops: dict = Depends(require_ops)):
    """读取流水线配置"""
    return {"success": True, "settings": pipeline.get_settings()}


@router.put("/settings")
async def save_settings(body: SettingsBody, _ops: dict = Depends(require_ops)):
    """保存流水线配置（EDD 阈值/评测集/各阶段开关与命令）"""
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    return {"success": True, "settings": pipeline.save_settings(patch)}


@router.post("/runs")
async def start_run(body: RunBody, ops: dict = Depends(require_ops)):
    """发起一次流水线运行（后台异步执行，立即返回 run_id）"""
    try:
        run_id = pipeline.start_pipeline(ops, git_ref=body.git_ref, trigger=body.trigger or "manual")
    except pipeline.ConcurrentRunError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "run_id": run_id, "status": "running"}


@router.get("/runs")
async def list_runs(limit: int = 20, _ops: dict = Depends(require_ops)):
    """运行历史"""
    limit = max(1, min(limit, 100))
    return {"success": True, "runs": pipeline.list_runs(limit)}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, _ops: dict = Depends(require_ops)):
    """运行详情（六阶段状态 + 日志 + EDD 门控结果）"""
    run = pipeline.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="流水线运行不存在")
    return {"success": True, "run": run}


@router.post("/runs/{run_id}/abort")
async def abort_run(run_id: str, _ops: dict = Depends(require_ops)):
    """人工中止运行（在阶段边界停止）"""
    ok = pipeline.abort_pipeline(run_id)
    if not ok:
        raise HTTPException(status_code=400, detail="该运行不存在或已结束，无法中止")
    return {"success": True, "run_id": run_id, "status": "aborting"}
