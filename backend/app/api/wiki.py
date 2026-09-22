"""
见字如面 - llm-wiki 知识百科 API

百科页面浏览/删除、digest 跨素材综合报告生成、存量资料补编译、编译配置，
以及知识助手 Agent（侧栏问答 + 人工确认编辑）。
"""
import asyncio
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.deps import get_current_user, require_admin
from app.services import wiki_assistant
from app.services.wiki import wiki_compile_enabled, wiki_priority_enabled, wiki_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/wiki", tags=["wiki"])


def _user_id(user: dict) -> str:
    return user.get("id") or user.get("username") or ""


@router.get("/status")
async def wiki_status(current_user: dict = Depends(get_current_user)):
    """知识编译状态（开关/已编译页面数）"""
    return {"success": True, **wiki_service.status()}


@router.get("/pages")
async def list_wiki_pages(
    kb_id: str = Query(""),
    page_type: str = Query("", pattern="^(|source|digest)$"),
    current_user: dict = Depends(get_current_user),
):
    """可见百科页面列表（私人页面仅属主/管理员可见）"""
    pages = wiki_service.list_pages(
        _user_id(current_user),
        current_user.get("role") == "admin",
        kb_id=kb_id,
        page_type=page_type,
    )
    # 列表不返回全文，减小载荷
    return {
        "success": True,
        "pages": [
            {
                "id": p["id"],
                "page_type": p["page_type"],
                "title": p["title"],
                "kb_id": p["kb_id"],
                "visibility": p["visibility"],
                "owner_id": p["owner_id"],
                "source_image": p["source_image"],
                "source_refs": p["source_refs"],
                "excerpt": p["content"][:120],
                "updated_at": p["updated_at"],
            }
            for p in pages
        ],
        "total": len(pages),
    }


@router.get("/pages/{page_id}")
async def get_wiki_page(page_id: str, current_user: dict = Depends(get_current_user)):
    """百科页面详情（完整 Markdown 内容）"""
    page = wiki_service.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="百科页面不存在")
    is_admin = current_user.get("role") == "admin"
    if page["visibility"] == "private" and not is_admin and page["owner_id"] != _user_id(current_user):
        raise HTTPException(status_code=404, detail="百科页面不存在")
    return {"success": True, "page": page}


@router.delete("/pages/{page_id}")
async def delete_wiki_page(page_id: str, current_user: dict = Depends(get_current_user)):
    """删除百科页面（含其向量条目）；私人页面仅属主可删"""
    page = wiki_service.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="百科页面不存在")
    is_admin = current_user.get("role") == "admin"
    if page["visibility"] == "private" and not is_admin and page["owner_id"] != _user_id(current_user):
        raise HTTPException(status_code=403, detail="只能删除自己个人知识库的百科页面")
    if page["page_type"] == "source" and not is_admin:
        # 来源卡片跟随知识条目权限：公共库条目仅管理员可删
        from app.services import kb as kb_service

        kb_row = kb_service.get_kb(page["kb_id"]) if page["kb_id"] else None
        if kb_row and kb_row.get("visibility") == "public":
            raise HTTPException(status_code=403, detail="仅管理员可删除公共知识库的百科卡片")
    wiki_service.delete_page(page_id)
    return {"success": True, "message": "百科页面已删除"}


class DigestRequest(BaseModel):
    """跨素材综合报告请求"""
    query: str


@router.post("/digest")
async def generate_digest(body: DigestRequest, current_user: dict = Depends(get_current_user)):
    """llm-wiki digest 工作流：对主题跨素材深度综合，生成持久化百科条目。"""
    if not body.query.strip():
        raise HTTPException(status_code=400, detail="主题不能为空")
    try:
        page = await wiki_service.synthesize(body.query, _user_id(current_user))
        return {"success": True, "page": page}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("digest 生成失败")
        raise HTTPException(status_code=500, detail=f"综合报告生成失败: {e}")


class RecompileRequest(BaseModel):
    """单来源重新编译请求"""
    source_image: str
    # 强制重编译：绕过 SHA256 增量缓存（默认 True，手动触发即视为强制）
    force: bool = True


@router.post("/compile")
async def recompile_source(body: RecompileRequest, current_user: dict = Depends(get_current_user)):
    """手动重新编译某个来源的百科卡片（私人来源仅属主可触发）"""
    from app.core.vector_store import vector_store
    from app.services.records import upload_records

    rec = upload_records.get_record_by_source(body.source_image)
    is_admin = current_user.get("role") == "admin"
    if rec and rec.get("visibility") == "private" and not is_admin \
            and rec.get("owner_id") != _user_id(current_user):
        raise HTTPException(status_code=403, detail="只能编译自己上传的资料")

    full_text: Optional[str] = rec.get("ocr_text") if rec else None
    if not full_text:
        # 记录缺失时回退向量库元数据拼接（历史遗留数据）
        full_text = ""
    if not full_text.strip():
        raise HTTPException(status_code=404, detail="未找到该来源的资料文本")

    kb_id = ""
    for m in vector_store._load_metadata().values():
        if m.get("source_image") == body.source_image:
            kb_id = m.get("kb_id", "")
            break

    page = await wiki_service.compile_source(
        source_image=body.source_image,
        full_text=full_text,
        kb_id=kb_id,
        visibility=(rec or {}).get("visibility", "public"),
        owner_id=(rec or {}).get("owner_id", ""),
        source_type=(rec or {}).get("source_type", "image"),
        title=(rec or {}).get("title", "") or (rec or {}).get("filename", ""),
        pdf_preview_path=(rec or {}).get("pdf_preview_path"),
        force=body.force,
    )
    if not page:
        raise HTTPException(status_code=400, detail="编译未执行（内容过短、编译被关闭或 LLM 失败）")
    return {"success": True, "page": page}


@router.post("/recompile-all")
async def recompile_all(force: bool = Query(False), _admin: dict = Depends(require_admin)):
    """为存量资料批量补编译百科卡片（写入持久化队列，仅管理员）

    force=true 时绕过 SHA256 增量缓存，强制重编译全部来源。
    """
    result = await wiki_service.recompile_all(force=force)
    return {"success": True, **result}


@router.get("/queue")
async def wiki_queue_status(_admin: dict = Depends(require_admin)):
    """持久化编译队列状态（待处理/处理中/失败数，仅管理员）"""
    return {"success": True, **wiki_service.queue_status()}



class WikiConfigRequest(BaseModel):
    """知识编译配置"""
    compile_enabled: Optional[bool] = None
    priority_enabled: Optional[bool] = None


@router.get("/config")
async def get_wiki_config(current_user: dict = Depends(get_current_user)):
    return {
        "success": True,
        "compile_enabled": wiki_compile_enabled(),
        "priority_enabled": wiki_priority_enabled(),
    }


@router.post("/config")
async def save_wiki_config(body: WikiConfigRequest, _admin: dict = Depends(require_admin)):
    """保存知识编译配置（仅管理员）"""
    from app.services.config_manager import runtime_config

    if body.compile_enabled is not None:
        runtime_config.wiki_compile_enabled = body.compile_enabled
    if body.priority_enabled is not None:
        runtime_config.wiki_priority_enabled = body.priority_enabled
    return {
        "success": True,
        "message": "知识编译配置已保存",
        "compile_enabled": wiki_compile_enabled(),
        "priority_enabled": wiki_priority_enabled(),
    }


# ============================================
# 知识助手 Agent（侧栏问答 + 人工确认编辑）
# ============================================

class AssistantChatRequest(BaseModel):
    """知识助手问答请求"""
    message: str
    page_id: str = ""
    page_title: str = ""
    route_name: str = ""
    route_label: str = ""
    # 助手面板内的历史对话 [{role, content}]，用于多轮上下文
    history: Optional[List[dict]] = None


@router.post("/assistant/chat")
async def assistant_chat(body: AssistantChatRequest, current_user: dict = Depends(get_current_user)):
    """知识助手 Agent 问答（SSE 流式）。

    事件：status / thought / tool / token / confirm / done / error。
    Agent 自行判断调用检索类工具；提议修改文档时发 confirm 事件等待用户确认。
    """
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="请输入问题")
    ctx = wiki_assistant.AssistantContext(
        user_id=_user_id(current_user),
        is_admin=current_user.get("role") == "admin",
        page_id=body.page_id,
        page_title=body.page_title,
        route_name=body.route_name,
        route_label=body.route_label,
    )
    history = body.history if isinstance(body.history, list) else []

    async def event_stream():
        async for ev in wiki_assistant.run_assistant(body.message, ctx, history):
            yield ev

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/assistant/edits")
async def list_assistant_edits(
    page_id: str = Query(""), current_user: dict = Depends(get_current_user)
):
    """待确认编辑列表（管理员看全部，普通用户看自己发起的；可按 page_id 过滤）"""
    edits = wiki_assistant.list_pending_edits(
        _user_id(current_user), current_user.get("role") == "admin", page_id=page_id
    )
    return {"success": True, "edits": edits}


@router.post("/assistant/edits/{edit_id}/approve")
async def approve_assistant_edit(edit_id: str, current_user: dict = Depends(get_current_user)):
    """确认并应用 Agent 提议的编辑（更新词条正文 + 重索引 + 镜像 + 日志）"""
    try:
        result = await asyncio.to_thread(wiki_assistant.approve_edit, edit_id, current_user)
        return {"success": True, **result, "message": "修改已应用"}
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("应用编辑失败")
        raise HTTPException(status_code=500, detail=f"应用编辑失败: {e}")


@router.post("/assistant/edits/{edit_id}/reject")
async def reject_assistant_edit(edit_id: str, current_user: dict = Depends(get_current_user)):
    """驳回 Agent 提议的编辑（仅发起者或管理员）"""
    try:
        result = wiki_assistant.reject_edit(edit_id, current_user)
        return {"success": True, **result, "message": "已驳回修改建议"}
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

