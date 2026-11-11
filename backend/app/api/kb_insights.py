"""
见字如面 - 知识库详情 API
按 KB 的文档列表 / 检索测试复用 knowledge 路由；此处提供：
文档表格、思维导图、知识图谱（llm-wiki 绑定）、RAG 评估。
"""
import asyncio
import logging
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.deps import get_current_user, get_optional_user
from app.config import settings
from app.core import db
from app.core.vector_store import vector_store
from app.services import kb as kb_service
from app.services import kb_eval, kb_mindmap, knowledge_graph
from app.services.records import upload_records

logger = logging.getLogger("jianziruyang.kb_insights")

router = APIRouter(prefix="/api/knowledge/bases", tags=["kb-insights"])


def _user_id(user: dict) -> str:
    return user.get("id") or user.get("username") or ""


def _check_kb(kb_id: str, user: dict | None) -> dict:
    kb = kb_service.get_kb(kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if user is None:
        if kb["visibility"] != "public":
            raise HTTPException(status_code=403, detail="个人知识库仅本人可见")
        return kb
    if kb["visibility"] != "public" and kb["owner_id"] != _user_id(user) \
            and user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="无权访问该知识库")
    return kb


# ============================================
# 文档列表（详情页「文档」选项卡）
# ============================================
@router.get("/{kb_id}/documents")
async def list_kb_documents(
    kb_id: str,
    q: str = "",
    current_user: dict | None = Depends(get_optional_user),
):
    kb = _check_kb(kb_id, current_user)
    is_guest = current_user is None

    entries = [e for e in vector_store.get_all_entries() if e.get("kb_id") == kb_id]
    all_records, _ = upload_records.list_records(limit=10000, offset=0)
    record_map = {rec["image_path"]: rec for rec in all_records}

    docs = []
    keyword = (q or "").strip().lower()
    for entry in entries:
        src = entry["source_image"]
        rec = record_map.get(src, {})
        filename = rec.get("filename", Path(src).name)
        title = (rec.get("title") or "").strip() or Path(filename).stem
        if keyword and keyword not in title.lower() and keyword not in filename.lower():
            continue
        try:
            size = os.path.getsize(src) if os.path.exists(src) else 0
        except OSError:
            size = 0
        docs.append({
            "source_image": src,
            "filename": filename,
            "title": title,
            "source_type": rec.get("source_type", "image"),
            "size_bytes": size,
            "chunk_count": entry["chunk_count"],
            "status": "done",
            "updated_at": rec.get("updated_at") or rec.get("created_at", ""),
            "pdf_preview_path": rec.get("pdf_preview_path"),
            "visibility": kb["visibility"],
        })
    docs.sort(key=lambda d: d["updated_at"], reverse=True)
    if is_guest:
        docs = docs[:3]
    return {"success": True, "documents": docs, "total": len(docs)}


# ============================================
# 思维导图
# ============================================
@router.get("/{kb_id}/mindmap")
async def get_kb_mindmap(kb_id: str, current_user: dict = Depends(get_current_user)):
    _check_kb(kb_id, current_user)
    payload = kb_mindmap.get_mindmap(kb_id)
    return {"success": True, **payload, "job": kb_mindmap.job_status(kb_id)}


@router.post("/{kb_id}/mindmap/rebuild")
async def rebuild_kb_mindmap(kb_id: str, current_user: dict = Depends(get_current_user)):
    kb = _check_kb(kb_id, current_user)
    if not kb_mindmap.start_rebuild_background(kb_id, kb["name"], kb["topic"]):
        raise HTTPException(status_code=409, detail="思维导图正在后台生成中，请稍后刷新查看")
    logger.info("[KB_MINDMAP] 用户发起后台生成 kb=%s user=%s", kb_id, _user_id(current_user))
    return {"success": True, "started": True, "status": "running",
            "job": kb_mindmap.job_status(kb_id)}


# ============================================
# 知识图谱
# ============================================
@router.get("/{kb_id}/graph")
async def get_kb_graph(kb_id: str, current_user: dict = Depends(get_current_user)):
    _check_kb(kb_id, current_user)
    return {"success": True, "graph": knowledge_graph.get_graph(kb_id),
            "job": knowledge_graph.job_status(kb_id)}


@router.post("/{kb_id}/graph/rebuild")
async def rebuild_kb_graph(kb_id: str, current_user: dict = Depends(get_current_user)):
    _check_kb(kb_id, current_user)
    if not knowledge_graph.start_rebuild_background(kb_id):
        raise HTTPException(status_code=409, detail="图谱索引正在后台重建中，请稍后刷新查看")
    logger.info("[KB_GRAPH] 用户发起后台重建 kb=%s user=%s", kb_id, _user_id(current_user))
    return {"success": True, "started": True, "status": "running",
            "job": knowledge_graph.job_status(kb_id)}


@router.delete("/{kb_id}/graph")
async def clear_kb_graph(kb_id: str, current_user: dict = Depends(get_current_user)):
    _check_kb(kb_id, current_user)
    knowledge_graph.clear_graph(kb_id)
    logger.info("[KB_GRAPH] 清空图谱 kb=%s user=%s", kb_id, _user_id(current_user))
    return {"success": True}


class GraphNodeRequest(BaseModel):
    name: str
    type: str = "concept"
    note: str = ""


@router.post("/{kb_id}/graph/nodes")
async def create_kb_graph_node(
    kb_id: str, body: GraphNodeRequest, current_user: dict = Depends(get_current_user),
):
    _check_kb(kb_id, current_user)
    try:
        node = knowledge_graph.add_node(kb_id, body.name, body.type, body.note)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info("[KB_GRAPH] 新建节点 kb=%s name=%s user=%s", kb_id, body.name, _user_id(current_user))
    return {"success": True, "node": node}


@router.delete("/{kb_id}/graph/nodes/{node_id}")
async def delete_kb_graph_node(
    kb_id: str, node_id: str, current_user: dict = Depends(get_current_user),
):
    _check_kb(kb_id, current_user)
    if not knowledge_graph.delete_node(kb_id, node_id):
        raise HTTPException(status_code=404, detail="节点不存在")
    logger.info("[KB_GRAPH] 删除节点 kb=%s node=%s user=%s", kb_id, node_id, _user_id(current_user))
    return {"success": True}


class GraphEdgeRequest(BaseModel):
    source: str
    target: str
    label: str = ""


@router.post("/{kb_id}/graph/edges")
async def create_kb_graph_edge(
    kb_id: str, body: GraphEdgeRequest, current_user: dict = Depends(get_current_user),
):
    _check_kb(kb_id, current_user)
    try:
        edge = knowledge_graph.add_edge(kb_id, body.source, body.target, body.label)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info("[KB_GRAPH] 新建关系 kb=%s %s→%s user=%s", kb_id, body.source, body.target,
                _user_id(current_user))
    return {"success": True, "edge": edge}


@router.delete("/{kb_id}/graph/edges/{edge_id}")
async def delete_kb_graph_edge(
    kb_id: str, edge_id: str, current_user: dict = Depends(get_current_user),
):
    _check_kb(kb_id, current_user)
    if not knowledge_graph.delete_edge(kb_id, edge_id):
        raise HTTPException(status_code=404, detail="关系不存在")
    logger.info("[KB_GRAPH] 删除关系 kb=%s edge=%s user=%s", kb_id, edge_id, _user_id(current_user))
    return {"success": True}


class GraphImportRequest(BaseModel):
    jsonl: str


@router.post("/{kb_id}/graph/import")
async def import_kb_graph(
    kb_id: str, body: GraphImportRequest, current_user: dict = Depends(get_current_user),
):
    _check_kb(kb_id, current_user)
    result = knowledge_graph.import_jsonl(kb_id, body.jsonl)
    logger.info("[KB_GRAPH] 导入JSONL kb=%s +nodes=%d +edges=%d user=%s", kb_id,
                result.get("added_nodes", 0), result.get("added_edges", 0), _user_id(current_user))
    return {"success": True, **result}


@router.get("/{kb_id}/graph/semantic")
async def semantic_search_kb_graph(
    kb_id: str,
    q: str = "",
    top_n: int = Query(10, ge=1, le=100),
    threshold: float = Query(0.5, ge=0, le=1),
    current_user: dict = Depends(get_current_user),
):
    _check_kb(kb_id, current_user)
    try:
        matches = await asyncio.to_thread(
            knowledge_graph.semantic_search, kb_id, q, top_n, threshold
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.warning("[KB_GRAPH] 语义搜索失败 kb=%s: %s", kb_id, e)
        raise HTTPException(status_code=503, detail=f"语义搜索不可用（向量化服务异常）: {e}")
    logger.info("[KB_GRAPH] 语义搜索 kb=%s q=%s 命中=%d user=%s", kb_id, (q or "")[:30],
                len(matches), _user_id(current_user))
    return {"success": True, "matches": matches}


@router.get("/{kb_id}/graph/source-content")
async def get_kb_graph_source_content(
    kb_id: str,
    title: str = Query("", max_length=200),
    path: str = Query("", max_length=500),
    current_user: dict = Depends(get_current_user),
):
    """图谱信息卡引用点击：返回对应文档/百科页的详细内容。

    - 百科页（kind=wiki）：返回完整 Markdown 内容；
    - 原始文档（kind=doc）：md/txt 返回文本；word/pdf 返回预览地址；图片返回图片地址。
    """
    _check_kb(kb_id, current_user)
    title = (title or "").strip()
    if title:
        page = db.query_one(
            "SELECT id, title, content, page_type FROM wiki_pages WHERE kb_id = ? AND title = ?",
            (kb_id, title),
        )
        if page:
            return {"success": True, "kind": "wiki", "title": page["title"],
                    "page_type": page["page_type"], "content": page["content"] or ""}
    # 原始文档：按 path/title 定位上传记录
    all_records, _ = upload_records.list_records(limit=10000, offset=0)
    rec = None
    for r in all_records:
        if path and r["image_path"] == path:
            rec = r
            break
        if title and ((r.get("title") or "").strip() == title
                      or r.get("filename", "") == title
                      or Path(r["image_path"]).name == title):
            rec = r
            break
    if not rec:
        raise HTTPException(status_code=404, detail="未找到对应的文档来源")
    filename = Path(rec["image_path"]).name
    source_type = rec.get("source_type", "image")
    out = {"success": True, "kind": "doc", "title": (rec.get("title") or "").strip() or filename,
           "source_type": source_type, "filename": filename,
           "pdf_preview_name": Path(rec["pdf_preview_path"]).name if rec.get("pdf_preview_path") else "",
           "content": ""}
    if source_type in ("md", "txt") and os.path.exists(rec["image_path"]):
        try:
            out["content"] = Path(rec["image_path"]).read_text(encoding="utf-8", errors="replace")[:20000]
        except OSError:
            out["content"] = ""
    return out


# ============================================
# RAG 评估
# ============================================
@router.get("/{kb_id}/eval/cases")
async def list_kb_eval_cases(kb_id: str, current_user: dict = Depends(get_current_user)):
    _check_kb(kb_id, current_user)
    return {"success": True, "cases": kb_eval.list_cases(kb_id)}


class EvalCaseRequest(BaseModel):
    question: str
    reference_answer: str = ""
    expected_keywords: list[str] = []


@router.post("/{kb_id}/eval/cases")
async def create_kb_eval_case(
    kb_id: str, body: EvalCaseRequest, current_user: dict = Depends(get_current_user),
):
    _check_kb(kb_id, current_user)
    try:
        case = kb_eval.add_case(
            kb_id, body.question, body.reference_answer, body.expected_keywords,
            _user_id(current_user),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "case": case}


@router.delete("/{kb_id}/eval/cases/{case_id}")
async def delete_kb_eval_case(
    kb_id: str, case_id: str, current_user: dict = Depends(get_current_user),
):
    _check_kb(kb_id, current_user)
    if not kb_eval.delete_case(kb_id, case_id):
        raise HTTPException(status_code=404, detail="用例不存在")
    return {"success": True}


@router.post("/{kb_id}/eval/cases/generate")
async def generate_kb_eval_cases(kb_id: str, current_user: dict = Depends(get_current_user)):
    kb = _check_kb(kb_id, current_user)
    try:
        cases = await kb_eval.generate_cases(kb_id, kb["name"], _user_id(current_user))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.warning("[KB_EVAL] 用例生成失败 kb=%s: %s", kb_id, e)
        raise HTTPException(status_code=502, detail=f"用例生成失败: {e}")
    return {"success": True, "cases": cases}


@router.post("/{kb_id}/eval/runs")
async def start_kb_eval_run(kb_id: str, current_user: dict = Depends(get_current_user)):
    _check_kb(kb_id, current_user)
    try:
        run_id = kb_eval.start_run(kb_id, _user_id(current_user))
    except kb_eval.evaluation.ConcurrentRunError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "run_id": run_id}


@router.get("/{kb_id}/eval/runs")
async def list_kb_eval_runs(kb_id: str, current_user: dict = Depends(get_current_user)):
    _check_kb(kb_id, current_user)
    return {"success": True, "runs": kb_eval.list_runs(kb_id)}


@router.get("/{kb_id}/eval/runs/{run_id}")
async def get_kb_eval_run(
    kb_id: str, run_id: str, current_user: dict = Depends(get_current_user),
):
    _check_kb(kb_id, current_user)
    run = kb_eval.get_run(kb_id, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="运行不存在")
    return {"success": True, "run": run}