"""个人工具管理、草稿确认与受保护凭据表单；浏览器不提交执行权限结论。"""
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.api.deps import get_current_user, require_admin
from app.services import personal_tools as tools

router = APIRouter(prefix="/api/personal-tools", tags=["personal-tools"])


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Proposal(StrictBody):
    spec: dict
    tool_id: str = Field(default="", max_length=32)


class Confirm(StrictBody):
    expected_hash: str = Field(min_length=64, max_length=64)
    accept_terms: bool = False
    allow_same_name: bool = False


class Configuration(StrictBody):
    enabled: bool | None = None
    accept_terms: bool | None = None


class Credential(StrictBody):
    value: SecretStr


class Approval(StrictBody):
    owner_id: str = Field(min_length=1, max_length=100)
    expected_hash: str = Field(min_length=64, max_length=64)
    verified_read_only: bool
    expected_profile_hash: str = Field(default="", max_length=64)


def invalid(exc):
    return HTTPException(400, str(exc))


@router.get("")
def listing(user=Depends(get_current_user)):
    return {"tools": tools.listing(user["id"])}


@router.post("/drafts")
def propose(body: Proposal, user=Depends(get_current_user)):
    try:
        return tools.propose(user["id"], body.spec, tool_id=body.tool_id)
    except ValueError as exc:
        raise invalid(exc) from None


@router.get("/drafts/{draft_id}")
def draft(draft_id: str, user=Depends(get_current_user)):
    return tools.draft(user["id"], draft_id)


@router.post("/drafts/{draft_id}/confirm")
async def confirm(draft_id: str, body: Confirm, user=Depends(get_current_user)):
    try:
        return await tools.confirm(user["id"], draft_id, **body.model_dump())
    except ValueError as exc:
        raise invalid(exc) from None


@router.post("/drafts/{draft_id}/cancel")
def cancel(draft_id: str, user=Depends(get_current_user)):
    return tools.cancel_draft(user["id"], draft_id)


@router.get("/reviews")
def reviews(user=Depends(require_admin)):
    return {"reviews": tools.pending_reviews()}


@router.post("/{tool_id}/approve")
def approve(tool_id: str, body: Approval, user=Depends(require_admin)):
    if not body.verified_read_only:
        raise HTTPException(400, "必须核验 API 无副作用／脚本仅隔离计算后才能批准")
    try:
        return tools.approve(body.owner_id, tool_id, body.expected_hash, user, body.expected_profile_hash)
    except ValueError as exc:
        raise invalid(exc) from None


@router.get("/{tool_id}")
def detail(tool_id: str, user=Depends(get_current_user)):
    return tools.get(user["id"], tool_id)


@router.patch("/{tool_id}")
def configure(tool_id: str, body: Configuration, user=Depends(get_current_user)):
    try:
        return tools.configure(user["id"], tool_id, **body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise invalid(exc) from None


@router.put("/{tool_id}/credential")
def credential(tool_id: str, body: Credential, response: Response, user=Depends(get_current_user)):
    response.headers["Cache-Control"] = "no-store"
    try:
        tools.configure(user["id"], tool_id, credential=body.value.get_secret_value())
    except ValueError as exc:
        raise invalid(exc) from None
    return {"configured": True, "message": "凭据已加密保存，本版本需要重新测试"}


@router.post("/{tool_id}/test")
async def test(tool_id: str, user=Depends(get_current_user)):
    return await tools.test(user["id"], tool_id)


@router.get("/{tool_id}/executions")
def executions(tool_id: str, user=Depends(get_current_user)):
    return {"executions": tools.executions(user["id"], tool_id)}


@router.post("/executions/{execution_id}/cancel")
async def cancel_execution(execution_id: str, user=Depends(get_current_user)):
    result = tools.cancel_execution(user["id"], execution_id)
    if result["status"] in ("running", "cancellation_requested", "cleanup_failed"):
        reconciled = await tools.reconcile_execution(user["id"], execution_id)
        result = reconciled or result
    return result
