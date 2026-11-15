"""
见字如面 - 个人中心 API（/api/me）

「我的记忆」管理（借鉴 Claude Code /memory 可管理性）：
记忆是纯文本，用户可随时浏览、编辑、删除或主动写入（"记住：…"）。
"""
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.services.memory import MEMORY_TYPES, memory_service, note_manual_write
from app.services.agent_memory import agent_memory_service, AGENT_KEYS, MAX_AGENT_MEMORIES
from app.services import module_recall

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
    # 主副互斥（对齐 hasMemoryWritesSince）：登记手动写入时间戳，
    # 近期后台自动抽取跳过，避免主链路手动写与副链路抽取竞态重复写。
    note_manual_write(user["id"])
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


@router.get("/module-recall")
async def my_module_recall(days: int = 30, user: dict = Depends(get_current_user)):
    """记忆/技能召回使用与效果度量：近 N 天召回率 + 召回/未召回两组点赞率对照。"""
    days = max(1, min(365, int(days or 30)))
    return module_recall.recall_overview(user["id"], days=days)


# ---------------------------------------------------------------------------
# L4 助手分域记忆（/api/me/agent-memories）：按 (user_id, agent_key) 行级隔离
# ---------------------------------------------------------------------------

class AgentMemoryCreateRequest(BaseModel):
    agent: str = "wiki_assistant"
    content: str
    memory_type: str = ""
    importance: float | None = None


class AgentMemoryUpdateRequest(BaseModel):
    agent: str = "wiki_assistant"
    content: str = ""
    memory_type: str = ""
    importance: float | None = None


def _check_agent(agent: str) -> str:
    if agent not in AGENT_KEYS:
        raise HTTPException(status_code=400, detail=f"未知的助手域：{agent}")
    return agent


@router.get("/agent-memories")
async def list_my_agent_memories(agent: str = "wiki_assistant", user: dict = Depends(get_current_user)):
    """浏览我的助手分域记忆（按 agent 域隔离，本人限定）。"""
    _check_agent(agent)
    return {
        "agent": agent,
        "memories": agent_memory_service.list_memories(user["id"], agent),
        "storage_limit": MAX_AGENT_MEMORIES,
    }


@router.post("/agent-memories")
async def create_my_agent_memory(body: AgentMemoryCreateRequest, user: dict = Depends(get_current_user)):
    """主动写入一条助手域记忆；敏感内容拒绝落库。"""
    agent = _check_agent(body.agent)
    content = body.content.strip()
    if len(content) < 2:
        raise HTTPException(status_code=400, detail="记忆内容过短")
    res = agent_memory_service.add_memory(
        user["id"], agent, content,
        memory_type=_infer_type(content, body.memory_type),
        importance=body.importance if body.importance is not None else _MANUAL_IMPORTANCE,
    )
    if not res:
        raise HTTPException(status_code=400, detail="记忆内容为空或包含敏感信息（密码/证件号等），不予记录")
    return {"success": True, "agent": agent, **res}


@router.put("/agent-memories/{memory_id}")
async def update_my_agent_memory(memory_id: str, body: AgentMemoryUpdateRequest, user: dict = Depends(get_current_user)):
    """编辑一条助手域记忆（内容/类型/重要度）；行级隔离 user_id + agent_key。"""
    agent = _check_agent(body.agent)
    res = agent_memory_service.update_memory(
        user["id"], agent, memory_id,
        content=body.content.strip() or None,
        importance=body.importance,
        memory_type=body.memory_type or None,
    )
    if not res:
        raise HTTPException(status_code=404, detail="记忆不存在、无权修改或内容包含敏感信息")
    return {"success": True, "agent": agent, **res}


@router.delete("/agent-memories/{memory_id}")
async def delete_my_agent_memory(memory_id: str, agent: str = "wiki_assistant", user: dict = Depends(get_current_user)):
    """删除一条助手域记忆（硬删除）；行级隔离，跨域/越权删除返回 404。"""
    agent = _check_agent(agent)
    if not agent_memory_service.delete_memory(user["id"], agent, memory_id):
        raise HTTPException(status_code=404, detail="记忆不存在或无权删除")
    return {"success": True, "message": "助手记忆已删除"}
