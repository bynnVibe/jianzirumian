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


class MemoryCreateRequest(BaseModel):
    content: str
    memory_type: str = ""
    importance: float | None = None


class MemoryUpdateRequest(BaseModel):
    content: str = ""
    memory_type: str = ""
    importance: float | None = None


def _infer_type(content: str, explicit: str = "") -> str:
    """长期记忆四类型（user/feedback/project/reference）：显式指定优先，默认 user。"""
    if explicit in MEMORY_TYPES:
        return explicit
    return "user"


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


# ---------------------------------------------------------------------------
# 记忆 Markdown 视图（/api/me/memory）：静态记忆（全局 User/Project/Local，全量加载）
# + 个人定制叠加层（个人指令/KB 共享规则/本人私人笔记）+ 长期记忆文件视图。
# 静态层仅管理员可写；KB Project 限属主/管理员；个人指令/Local 限本人。
# ---------------------------------------------------------------------------

class LayerUpdateRequest(BaseModel):
    content: str = ""
    kb_id: str = ""


def _layer_writable(layer: str, user: dict, kb_id: str) -> bool:
    """按层写权限：project=KB 属主/admin；user/local=本人（local 需 kb_id）。"""
    from app.services import kb as kb_service

    uid = user["id"]
    is_admin = user.get("role") == "admin"
    if layer == "user":
        return True
    if layer == "project":
        if not kb_id:
            return False
        row = kb_service.get_kb(kb_id)
        return bool(row) and (is_admin or row.get("owner_id") == uid)
    if layer == "local":
        return bool(kb_id)
    return False


@router.get("/memory/layers")
async def my_memory_layers(kb_id: str = "", user: dict = Depends(get_current_user)):
    """记忆存储概览：静态三层正文 + 个人叠加层正文 + 长期记忆文件视图（只读）。"""
    from app.services import memory_store

    uid = user["id"]
    contents = {layer: memory_store.read_layer(layer, user_id=uid, kb_id=kb_id)
                for layer in ("user", "project", "local")}
    static_contents = {layer: memory_store.read_static(layer)
                       for layer in memory_store.STATIC_LAYERS}
    return {
        "layers": memory_store.layers_overview(uid, kb_id),
        "contents": contents,
        "static": static_contents,
        "static_writable": user.get("role") == "admin",
        "auto": memory_store.auto_files(uid),
        "writable": {layer: _layer_writable(layer, user, kb_id)
                     for layer in ("user", "project", "local")},
        "max_write_chars": memory_store.MAX_LAYER_WRITE_CHARS,
    }


@router.put("/memory/static/{layer}")
async def update_static_layer(layer: str, body: LayerUpdateRequest, user: dict = Depends(get_current_user)):
    """编辑全局静态记忆（User/Project/Local，全量加载的规矩层）；仅管理员，敏感内容拒绝。"""
    from app.services import memory_store
    from app.services.memory import _is_sensitive

    if layer not in memory_store.STATIC_LAYERS:
        raise HTTPException(status_code=400, detail="静态层仅支持 user/project/local")
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="静态记忆为全局规矩，仅管理员可编辑")
    content = (body.content or "").strip()
    if content and _is_sensitive(content):
        raise HTTPException(status_code=400, detail="内容包含敏感信息（密码/证件号等），不予记录")
    if len(content) > memory_store.MAX_LAYER_WRITE_CHARS:
        raise HTTPException(status_code=400,
                            detail=f"内容过长（上限 {memory_store.MAX_LAYER_WRITE_CHARS} 字符）")
    if not memory_store.write_static(layer, content):
        raise HTTPException(status_code=500, detail="静态记忆文件写入失败")
    memory_store.log_memory_event(user["id"], "update", f"static_{layer}", detail="静态层编辑")
    return {"success": True, "layer": layer, "content": content}


@router.put("/memory/layers/{layer}")
async def update_memory_layer(layer: str, body: LayerUpdateRequest, user: dict = Depends(get_current_user)):
    """编辑个人定制叠加层 markdown（个人指令/KB Project/本人 Local）；敏感内容拒绝。

    User 层委托 user_instructions（DB 镜像同源，500 字预算）；其余层文件即持久记录。
    长期记忆不开放直编：经 /memories CRUD 演进（每记忆独立文件 + MEMORY.md 索引重建）。
    """
    from app.services import memory_store
    from app.services.memory import _is_sensitive

    if layer not in ("user", "project", "local"):
        raise HTTPException(status_code=400, detail="该层不支持直接编辑（长期记忆请经记忆 CRUD 演进）")
    if not _layer_writable(layer, user, body.kb_id):
        raise HTTPException(status_code=403, detail="无权编辑该记忆层")
    content = (body.content or "").strip()
    if content and _is_sensitive(content):
        raise HTTPException(status_code=400, detail="内容包含敏感信息（密码/证件号等），不予记录")
    if layer == "user":
        from app.services import user_instructions

        res = user_instructions.set_instruction(user["id"], content)
        if res is None:
            raise HTTPException(status_code=400, detail="指令写入失败")
        return {"success": True, "layer": layer, "content": res["content"]}
    if len(content) > memory_store.MAX_LAYER_WRITE_CHARS:
        raise HTTPException(status_code=400,
                            detail=f"内容过长（上限 {memory_store.MAX_LAYER_WRITE_CHARS} 字符）")
    if not memory_store.write_layer(layer, content, user_id=user["id"], kb_id=body.kb_id):
        raise HTTPException(status_code=500, detail="记忆文件写入失败")
    memory_store.log_memory_event(user["id"], "update", layer, detail=body.kb_id)
    return {"success": True, "layer": layer, "content": content}


@router.get("/memory/auto-files")
async def my_memory_auto_files(user: dict = Depends(get_current_user)):
    """长期记忆文件视图：MEMORY.md 索引正文 + 每记忆独立文件（含元数据）。"""
    from app.services import memory_store

    return memory_store.auto_files(user["id"])


@router.get("/memory/session-files")
async def my_memory_session_files(session_id: str, user: dict = Depends(get_current_user)):
    """短期记忆文件视图（本人会话限定）：transcript/summary/task_state 概览与正文。"""
    from app.core import db
    from app.services import memory_store

    uid = user["id"]
    row = db.query_one("SELECT user_id FROM chat_sessions WHERE session_id = ?", (session_id,))
    if not row or row.get("user_id") != uid:
        raise HTTPException(status_code=404, detail="会话不存在或无权查看")
    return {
        "overview": memory_store.session_files_overview(session_id, uid),
        "contents": memory_store.read_session_files(session_id, uid),
    }


def _extract_run_reason(detail: str) -> str:
    """从运行事件 detail 判定本轮结果（供空态解释）。

    以「整轮是否写入」为准而非仅看模型层：显式信号层落库而模型判空时，
    本轮仍属正常产出（否则面板会误报「无值得保存的内容」）。
    """
    m = re.search(r"新增(\d+)更新(\d+)删除(\d+)", detail or "")
    if m and sum(int(g) for g in m.groups()) > 0:
        return "produced"
    r = re.search(r"模型层:([a-z_]+)", detail or "")
    if r:
        return r.group(1)
    # 无模型层原因后缀且零写入：有候选但被查重/敏感词门禁拦下或超单轮上限
    return "filtered"


@router.get("/memory/extract-status")
async def my_memory_extract_status(days: int = 7, user: dict = Depends(get_current_user)):
    """自动抽取运行状态（近 N 天）：让「暂无记忆」可解释。

    区分四种情形：从未运行（runs=0）/ 跑了但模型判空（empty）/
    模型不可用或调用失败（llm_unavailable・llm_error）/ 正常产出（produced）。
    口径与可观测性同源：memory_write_events 中 action=extract 为运行事件，
    add/update/delete/merge 为实际写入事件（两者拆开，避免运行数污染写路径指标）。
    """
    from datetime import datetime, timedelta

    from app.core import db

    uid = user["id"]
    days = max(1, min(90, int(days or 7)))
    empty = {"enabled": False, "days": days, "runs": 0, "writes": 0,
             "writes_by_action": {}, "by_reason": {},
             "last_run_at": None, "last_detail": "", "last_reason": ""}
    if not uid or uid in {"guest", "system", "-"}:
        return empty
    since = (datetime.now() - timedelta(days=days)).isoformat()
    rows = db.query(
        "SELECT action, detail, created_at FROM memory_write_events"
        " WHERE user_id = ? AND created_at >= ? ORDER BY created_at DESC",
        (uid, since),
    )
    runs = [r for r in rows if r.get("action") == "extract"]
    writes_by_action: dict = {}
    for r in rows:
        act = r.get("action") or ""
        if act in ("add", "update", "delete", "merge"):
            writes_by_action[act] = writes_by_action.get(act, 0) + 1
    by_reason: dict = {}
    for r in runs:
        reason = _extract_run_reason(r.get("detail") or "")
        by_reason[reason] = by_reason.get(reason, 0) + 1
    last = runs[0] if runs else {}
    last_detail = last.get("detail") or ""
    return {
        "enabled": True,
        "days": days,
        "runs": len(runs),
        "writes": sum(writes_by_action.values()),
        "writes_by_action": writes_by_action,
        "by_reason": by_reason,
        "last_run_at": last.get("created_at"),
        "last_detail": last_detail,
        "last_reason": _extract_run_reason(last_detail) if runs else "",
    }
