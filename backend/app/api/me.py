"""
见字如面 - 个人中心 API（/api/me）

「我的记忆」管理（借鉴 Claude Code /memory 可管理性）：
记忆是纯文本，用户可随时浏览、编辑、删除或主动写入（"记住：…"）。
"""
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.services.memory import MEMORY_TYPES, memory_service

router = APIRouter(prefix="/api/me", tags=["me"])

# 手动写入的重要度：用户主动要求记住的内容视为高价值（进入常驻画像卡候选）
_MANUAL_IMPORTANCE = 0.85
# 手动写入类型推断：偏好/禁忌类措辞归 preference，其余按 fact
_PREFERENCE_RE = re.compile(r"喜欢|偏好|爱吃|不吃|忌口|过敏|讨厌|不要|习惯")


class MemoryCreateRequest(BaseModel):
    content: str
    memory_type: str = ""
    importance: float | None = None


class MemoryUpdateRequest(BaseModel):
    content: str = ""
    memory_type: str = ""
    importance: float | None = None


def _infer_type(content: str, explicit: str = "") -> str:
    if explicit in MEMORY_TYPES:
        return explicit
    return "preference" if _PREFERENCE_RE.search(content) else "fact"


@router.get("/memories")
async def list_my_memories(user: dict = Depends(get_current_user)):
    """浏览我的记忆（按更新时间倒序）。"""
    return {"memories": memory_service.list_memories(user["id"])}


@router.post("/memories")
async def create_my_memory(body: MemoryCreateRequest, user: dict = Depends(get_current_user)):
    """主动写入一条记忆（等价 "remember X"）；敏感内容拒绝落库。"""
    content = body.content.strip()
    if len(content) < 2:
        raise HTTPException(status_code=400, detail="记忆内容过短")
    res = memory_service.add_memory(
        user["id"], content,
        memory_type=_infer_type(content, body.memory_type),
        importance=body.importance if body.importance is not None else _MANUAL_IMPORTANCE,
    )
    if not res:
        raise HTTPException(status_code=400, detail="记忆内容为空或包含敏感信息（密码/证件号等），不予记录")
    return {"success": True, **res}


@router.put("/memories/{memory_id}")
async def update_my_memory(memory_id: str, body: MemoryUpdateRequest, user: dict = Depends(get_current_user)):
    """编辑一条记忆（内容/类型/重要度）。"""
    res = memory_service.update_memory(
        user["id"], memory_id,
        content=body.content.strip() or None,
        importance=body.importance,
        memory_type=body.memory_type or None,
    )
    if not res:
        raise HTTPException(status_code=404, detail="记忆不存在、无权修改或内容包含敏感信息")
    return {"success": True, **res}


@router.delete("/memories/{memory_id}")
async def delete_my_memory(memory_id: str, user: dict = Depends(get_current_user)):
    """删除一条记忆（硬删除，与 Claude 记忆文件可直删一致）。"""
    if not memory_service.delete_memory(user["id"], memory_id):
        raise HTTPException(status_code=404, detail="记忆不存在或无权删除")
    return {"success": True, "message": "记忆已删除"}
