"""
见字如面 - 用户个人指令 API（/api/me/instructions，五层记忆架构 L1）

类比 Claude Code CLAUDE.md 的 User 层：用户预写的静态规则，每用户一条，
注入位置靠后、优先级高于系统默认约定（Managed < User）。本人限定，敏感内容拒绝。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.services import user_instructions

router = APIRouter(prefix="/api/me", tags=["instructions"])


class InstructionUpdateRequest(BaseModel):
    content: str = ""


@router.get("/instructions")
async def get_my_instructions(user: dict = Depends(get_current_user)):
    """读取我的个人指令（无则空串），附字符上限供前端字数提示。"""
    content = user_instructions.get_instruction(user["id"])
    return {"content": content, "max_chars": user_instructions.MAX_INSTRUCTION_CHARS}


@router.put("/instructions")
async def update_my_instructions(body: InstructionUpdateRequest, user: dict = Depends(get_current_user)):
    """写入/更新我的个人指令；敏感内容拒绝（400），超长按上限截断；空串=清空指令。"""
    res = user_instructions.set_instruction(user["id"], body.content)
    if res is None:
        raise HTTPException(status_code=400, detail="指令包含敏感信息（密码/证件号等），不予保存")
    return {"success": True, **res, "max_chars": user_instructions.MAX_INSTRUCTION_CHARS}