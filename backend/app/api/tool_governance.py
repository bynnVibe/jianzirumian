"""工具治理中心 API：监控／评测／审批／配置的统一入口（仅管理员/运维）。

聊天草稿确认仍走 /api/personal-tools/drafts/*；安装后的全部管理操作
收口于此，内部复用 personal_tools / tool_eval 单一写点。治理页已迁入
系统管理控制台 /admin/tools，普通用户无权访问本组端点。
"""
from fastapi import APIRouter, Depends, HTTPException, Response

from app.api.deps import require_admin, require_ops
from app.api.personal_tools import Approval, Configuration, Credential
from app.services import personal_tools as tools, tool_governance as gov

router = APIRouter(prefix="/api/tool-governance", tags=["tool-governance"])


def invalid(exc):
    return HTTPException(400, str(exc))


@router.get("/overview")
def overview(user=Depends(require_ops)):
    return gov.overview(user)


@router.get("/registry")
def registry(user=Depends(require_ops)):
    return gov.registry(user)


@router.get("/tools/{tool_id}/monitoring")
def monitoring(tool_id: str, user=Depends(require_ops)):
    return gov.monitoring(user, tool_id)


@router.post("/tools/{tool_id}/test")
async def test(tool_id: str, user=Depends(require_ops)):
    return await tools.test(gov._actor_owner(user, tool_id), tool_id)


@router.post("/tools/{tool_id}/workflow")
async def workflow(tool_id: str, user=Depends(require_ops)):
    """一键跑完生命周期工作流：样例测试→标准化评测→启用注册。"""
    try:
        return await gov.run_workflow(user, tool_id)
    except ValueError as exc:
        raise invalid(exc) from None


@router.post("/tools/{tool_id}/reeval")
async def reeval(tool_id: str, user=Depends(require_ops)):
    try:
        return gov.reeval(user, tool_id)
    except ValueError as exc:
        raise invalid(exc) from None


@router.patch("/tools/{tool_id}")
def configure(tool_id: str, body: Configuration, user=Depends(require_ops)):
    owner = gov._actor_owner(user, tool_id)
    try:
        return tools.configure(owner, tool_id, **body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise invalid(exc) from None


@router.put("/tools/{tool_id}/credential")
def credential(tool_id: str, body: Credential, response: Response, user=Depends(require_ops)):
    owner = gov._actor_owner(user, tool_id)
    response.headers["Cache-Control"] = "no-store"
    try:
        tools.configure(owner, tool_id, credential=body.value.get_secret_value())
    except ValueError as exc:
        raise invalid(exc) from None
    return {"configured": True, "message": "凭据已加密保存，本版本需要重新测试"}


@router.post("/tools/{tool_id}/approve")
def approve(tool_id: str, body: Approval, user=Depends(require_admin)):
    if not body.verified_read_only:
        raise HTTPException(400, "必须核验 API 无副作用／脚本仅隔离计算后才能批准")
    try:
        return tools.approve(body.owner_id, tool_id, body.expected_hash, user, body.expected_profile_hash)
    except ValueError as exc:
        raise invalid(exc) from None


@router.post("/executions/{execution_id}/cancel")
async def cancel_execution(execution_id: str, user=Depends(require_ops)):
    return await gov.cancel_execution(user, execution_id)