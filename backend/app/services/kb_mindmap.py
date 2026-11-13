"""
见字如面 - 思维导图服务
基于 KB 内容（llm-wiki 论点 + 文档预览）由 LLM 生成层级树 JSON，
按内容哈希缓存，支持手动重建。

- 节点溯源：生成时要求 LLM 为每个节点标注来源文档标题（src），
  前端点击节点可跳转至对应文档（需求：思维导图文本框跳转文档）；
- 后台生成：rebuild 经 start_rebuild_background 放入后台任务，接口立即返回，
  页面轮询 job 状态（生成中按钮置灰 + 「正在后台生成中」提示）；
- 双存储：SQLite 本地一份 + Redis 缓存一份（insight_cache），页面加载优先读缓存。
"""
import asyncio
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

from app.core import db
from app.core.insight_cache import cache_delete, cache_get, cache_set, mindmap_key
from app.services import wiki
from app.services.kb_content import _row_in_doc, build_kb_context

logger = logging.getLogger("jianziruyang.kb_mindmap")

_MAX_NODES = 80
_MAX_DEPTH = 4

_MINDMAP_PROMPT = (
    "你是「知识结构化专家」。请基于给定的知识库内容，生成一棵中文思维导图层级树，"
    "只输出一个 JSON 对象，不要输出任何解释、不要用代码块包裹。格式：\n"
    '{"name": "根节点（知识库主题）", "children": [{"name": "分支", "src": "来源标题", "children": [{"name": "叶子要点", "src": "来源标题"}]}]}\n'
    "要求：\n"
    "- 根节点用知识库名称/主题；一级分支 3-6 个，按主题聚类\n"
    "- 深度不超过 4 层，总节点数不超过 60，叶子节点文字不超过 16 字\n"
    "- 每个节点尽量给出 src 字段：该节点内容最相关的来源标题，必须取自【可选来源标题】清单（原样抄写，不要编造）；"
    "确实无法归属时省略 src\n"
    "- 严格忠于给定内容，绝不编造；内容不足的分支不要硬凑"
)

# 单篇文档范围的附加约束（根节点=文档标题，src 统一归属该文档）
_DOC_SCOPE_HINT = (
    "\n\n本次仅针对单篇文档生成：根节点用该文档标题，一级分支按文档章节/主题聚类，"
    "所有节点的 src 统一取该文档标题。"
)


def _scope(kb_id: str, doc_path: str) -> str:
    """存储/缓存/任务键：整库用 kb_id，单篇文档用 kb_id::doc::路径（不污染整库数据）。"""
    return f"{kb_id}::doc::{doc_path}" if doc_path else kb_id


def _doc_record(doc_path: str) -> dict | None:
    from app.services.records import upload_records
    all_records, _ = upload_records.list_records(limit=10000, offset=0)
    for rec in all_records:
        if rec["image_path"] == doc_path:
            return rec
    return None


# 后台生成任务登记：scope(kb_id, doc_path) -> {status, started_at, finished_at, error}
_JOBS: dict = {}
_TASKS: set = set()


def _now() -> str:
    return datetime.now().isoformat()


def job_status(kb_id: str, doc_path: str = "") -> dict | None:
    """当前后台生成任务状态（无任务返回 None）。"""
    job = _JOBS.get(_scope(kb_id, doc_path))
    if not job:
        return None
    return dict(job)


def content_hash(kb_id: str, doc_path: str = "") -> str:
    wiki_rows = db.query(
        "SELECT id, updated_at, source_image, source_refs FROM wiki_pages WHERE kb_id = ? ORDER BY id",
        (kb_id,),
    )
    from app.core.vector_store import vector_store
    if doc_path:
        wiki_rows = [r for r in wiki_rows if _row_in_doc(r, doc_path)]
        rec = _doc_record(doc_path)
        src_sig = [doc_path, rec.get("updated_at") or rec.get("created_at", "") if rec else ""]
    else:
        src_sig = sorted(
            meta.get("source_image", "")
            for meta in vector_store._load_metadata().values()
            if meta.get("kb_id") == kb_id
        )
    wiki_sig = [{"id": r["id"], "updated_at": r["updated_at"]} for r in wiki_rows]
    raw = json.dumps([wiki_sig, src_sig], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def doc_titles(kb_id: str, limit: int = 40, doc_path: str = "") -> list:
    """KB 内原始文档标题清单（供生成时 src 溯源与前端跳转）；单篇范围只返回该文档标题。"""
    from app.core.vector_store import vector_store
    from app.services.records import upload_records
    if doc_path:
        rec = _doc_record(doc_path)
        title = ((rec.get("title") or "").strip() or rec.get("filename", "")) if rec else Path(doc_path).stem
        return [title] if title else []
    sources = {
        meta.get("source_image", "")
        for meta in vector_store._load_metadata().values()
        if meta.get("kb_id") == kb_id and not meta.get("is_wiki_page")
    }
    if not sources:
        return []
    titles = []
    all_records, _ = upload_records.list_records(limit=10000, offset=0)
    for rec in all_records:
        if rec["image_path"] not in sources:
            continue
        title = (rec.get("title") or "").strip() or rec.get("filename", "")
        if title and title not in titles:
            titles.append(title)
        if len(titles) >= limit:
            break
    return titles


def get_mindmap(kb_id: str, doc_path: str = "") -> dict:
    """读取思维导图：Redis 缓存优先，未命中回源 SQLite 并回写缓存。"""
    scope = _scope(kb_id, doc_path)
    cached = cache_get(mindmap_key(scope))
    if cached is not None:
        return cached
    row = db.query_one("SELECT data, source_hash, updated_at FROM kb_mindmaps WHERE kb_id = ?", (scope,))
    current = content_hash(kb_id, doc_path)
    if not row:
        payload = {"tree": None, "updated_at": "", "stale": False, "has_content": bool(current)}
    else:
        try:
            tree = json.loads(row["data"] or "{}")
        except (json.JSONDecodeError, ValueError):
            tree = None
        payload = {
            "tree": tree or None,
            "updated_at": row["updated_at"],
            "stale": row["source_hash"] != current,
            "has_content": True,
        }
    cache_set(mindmap_key(scope), payload)
    return payload


def _match_title(src: str, titles) -> str:
    """将 LLM 返回的来源标题容错映射到 doc_titles 中的规范标题。

    依次尝试：原样精确 → 去书名号/引号/空白后精确 → 双向子串（取最长命中）。
    无命中返回空串（节点不携带 src，前端不可跳转）。
    """
    src = (src or "").strip()
    if not src:
        return ""
    title_list = list(titles or ())
    if src in title_list:
        return src

    def _canon(s: str) -> str:
        return s.strip().strip("《》「」\"'“”‘’ 　").lower()

    canon_src = _canon(src)
    if not canon_src:
        return ""
    for t in title_list:
        if _canon(t) == canon_src:
            return t
    # 双向子串：取最长命中的规范标题，避免短标题误匹配
    best = ""
    for t in title_list:
        ct = _canon(t)
        if not ct:
            continue
        if ct in canon_src or canon_src in ct:
            if len(t) > len(best):
                best = t
    return best


def _normalize(node, depth=0, titles=()) -> dict | None:
    if not isinstance(node, dict) or depth >= _MAX_DEPTH:
        return None
    name = str(node.get("name") or "").strip()[:40]
    if not name:
        return None
    out = {"name": name}
    src = _match_title(str(node.get("src") or ""), titles)
    if src:
        out["src"] = src
    children = node.get("children")
    if isinstance(children, list) and depth < _MAX_DEPTH - 1:
        kids = [_normalize(c, depth + 1, titles) for c in children[:12]]
        kids = [k for k in kids if k]
        if kids:
            out["children"] = kids
    return out


def _count_nodes(node) -> int:
    return 1 + sum(_count_nodes(c) for c in node.get("children", []))


async def rebuild(kb_id: str, kb_name: str, topic: str, doc_path: str = "") -> dict:
    scope = _scope(kb_id, doc_path)
    context = build_kb_context(kb_id, 8000, doc_path)
    if not context.strip():
        raise ValueError("该文档暂无入库内容，请先完成入库后再生成思维导图" if doc_path
                         else "知识库暂无内容，请先上传文档后再生成思维导图")
    titles = doc_titles(kb_id, doc_path=doc_path)
    title_hint = ("、".join(f"《{t}》" for t in titles)) if titles else "（无）"
    doc_line = f"【单篇文档】《{titles[0]}》\n" if doc_path and titles else ""
    messages = [
        {"role": "system", "content": _MINDMAP_PROMPT + (_DOC_SCOPE_HINT if doc_path else "")},
        {"role": "user", "content":
            f"【知识库】{kb_name}（主题：{topic}）\n{doc_line}"
            f"【可选来源标题】{title_hint}\n\n【内容】\n{context}"},
    ]
    raw = await wiki._llm_complete(messages)
    tree = wiki._extract_json(raw)
    tree = _normalize(tree, 0, set(titles))
    if not tree or not tree.get("children"):
        raise ValueError("LLM 未返回有效的思维导图结构，请重试")
    if _count_nodes(tree) > _MAX_NODES:
        tree["children"] = tree["children"][:6]

    now = _now()
    db.execute(
        "INSERT INTO kb_mindmaps (kb_id, data, source_hash, updated_at) VALUES (?,?,?,?)"
        " ON CONFLICT(kb_id) DO UPDATE SET data = excluded.data,"
        " source_hash = excluded.source_hash, updated_at = excluded.updated_at",
        (scope, json.dumps(tree, ensure_ascii=False), content_hash(kb_id, doc_path), now),
    )
    cache_delete(mindmap_key(scope))
    logger.info("[KB_MINDMAP] 生成思维导图 kb=%s doc=%s nodes=%d", kb_id, doc_path or "(整库)", _count_nodes(tree))
    return {"tree": tree, "updated_at": now, "stale": False, "has_content": True}


def start_rebuild_background(kb_id: str, kb_name: str, topic: str, doc_path: str = "") -> bool:
    """发起后台生成任务（不阻塞请求）；已有任务在跑时返回 False。"""
    scope = _scope(kb_id, doc_path)
    job = _JOBS.get(scope)
    if job and job.get("status") == "running":
        return False
    _JOBS[scope] = {"status": "running", "started_at": _now(), "finished_at": "", "error": ""}

    async def _run():
        try:
            await rebuild(kb_id, kb_name, topic, doc_path)
            _JOBS[scope] = {"status": "done", "started_at": _JOBS[scope]["started_at"],
                            "finished_at": _now(), "error": ""}
        except Exception as e:
            logger.warning("[KB_MINDMAP] 后台生成失败 kb=%s doc=%s: %s", kb_id, doc_path, e)
            _JOBS[scope] = {"status": "failed", "started_at": _JOBS[scope]["started_at"],
                            "finished_at": _now(), "error": str(e)}

    task = asyncio.create_task(_run())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    logger.info("[KB_MINDMAP] 后台生成任务已启动 kb=%s doc=%s", kb_id, doc_path or "(整库)")
    return True
