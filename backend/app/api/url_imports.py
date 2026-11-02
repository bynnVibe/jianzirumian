"""公开链接导入 API；客户端只能编辑草稿并确认服务端版本。"""
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from app.api.deps import get_current_user
from app.config import settings
from app.services.url_imports import url_imports, formal_user

router = APIRouter(prefix="/api/knowledge/url-imports", tags=["公开链接导入"])


class CreateUrlImport(BaseModel):
    model_config = {"extra": "forbid"}
    url: str = Field(min_length=1, max_length=4096)


class EditUrlImport(BaseModel):
    model_config = {"extra": "forbid"}
    expected_revision: int = Field(ge=1)
    title: str = Field(default="", max_length=300)
    parsed_text: str = Field(max_length=500000)


class RetryUrlImport(BaseModel):
    model_config = {"extra": "forbid"}
    expected_revision: int = Field(ge=1)


class ConfirmUrlImport(BaseModel):
    model_config = {"extra": "forbid"}
    revision: int = Field(ge=1)
    kb_id: str = Field(min_length=1, max_length=100)


@router.post("", status_code=202)
async def create(body: CreateUrlImport, user=Depends(get_current_user)):
    return {"success": True, "item": url_imports.create(body.url, formal_user(user))}


@router.get("")
async def list_items(response: Response, user=Depends(get_current_user)):
    response.headers["Cache-Control"] = "no-store"
    return {"items": url_imports.list(formal_user(user)), "renderer_enabled": settings.URL_RENDERER_ENABLED}


@router.get("/{task_id}")
async def get_item(task_id: str, response: Response, user=Depends(get_current_user)):
    response.headers["Cache-Control"] = "no-store"
    return {"item": url_imports.get(task_id, formal_user(user))}


@router.patch("/{task_id}")
async def edit(task_id: str, body: EditUrlImport, user=Depends(get_current_user)):
    return {"item": url_imports.save(task_id, formal_user(user), body.expected_revision, body.title, body.parsed_text)}


@router.post("/{task_id}/retry", status_code=202)
async def retry(task_id: str, body: RetryUrlImport, user=Depends(get_current_user)):
    return {"item": url_imports.retry(task_id, formal_user(user), body.expected_revision)}


@router.post("/{task_id}/confirm")
async def confirm(task_id: str, body: ConfirmUrlImport, response: Response, user=Depends(get_current_user)):
    value = url_imports.confirm(task_id, formal_user(user), body.revision, body.kb_id)
    response.status_code = 200 if value["status"] == "succeeded" else 202
    return {"item": value}


@router.delete("/{task_id}")
async def delete(task_id: str, user=Depends(get_current_user)):
    url_imports.delete(task_id, formal_user(user))
    return {"success": True}
