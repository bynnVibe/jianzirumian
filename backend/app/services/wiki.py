"""
见字如面 - llm-wiki 知识编译服务

借鉴 llm-wiki（LLM 作为"知识编译器"）的核心思想：传统 RAG 每次提问都要重新
捞取原始碎片，而 llm-wiki 让 LLM 先把资料读一遍、编译成结构化、彼此关联的"个人
百科全书"，之后检索直接命中这些高质量百科卡片。

在当前 RAG 系统（FAISS + BM25 + RRF + Rerank）之上以「增量共存」方式落地：

1. source 知识卡片：文档/图片入库后，后台用 LLM 提炼成结构化百科卡片，
   作为一类高质来源（metadata.is_wiki_page=True）回写向量库；
   检索时优先命中百科卡片，原始 chunk 兜底。
2. digest 综合报告：对一个主题跨素材做深度综合，生成持久化百科条目
   （page_type="digest"），既可浏览，也会回写向量库参与后续检索。

设计原则：编译完全异步、可关闭、失败静默降级——任何异常都不影响现有上传
与问答链路（与原 RAG 共存）。编译产物同时以 Markdown 落盘到 WIKI_DIR，可浏览/导出。
"""
import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from app.config import settings
from app.core import db
from app.core.llm import LLMFactory
from app.core.vector_store import vector_store
from app.services.config_manager import runtime_config

logger = logging.getLogger("jianziruyang.wiki")

# 后台编译任务引用集（防止 task 在 pending 期被 GC）
_BG_TASKS: set = set()

# 相邻编译请求的最小间隔时间戳（event loop 时钟），缓解 LLM 并发限流
_LAST_COMPILE_AT = 0.0

# 知识卡片编译提示词：把资料"编译"成结构化百科条目
_CARD_PROMPT = (
    "你是「知识编译器」，负责把零散资料编译成个人百科全书中的一则词条。要求：\n"
    "- 严格忠于原文，绝不编造资料中没有的内容\n"
    "- 输出纯 Markdown 正文，不要用代码块包裹，不要输出任何解释或前后缀引导语\n"
    "- 用中文撰写，信息密度高，便于后续检索命中\n\n"
    "按以下结构输出：\n"
    "# 词条标题\n"
    "## 概述\n（2-4 句说明这则资料讲的是什么）\n"
    "## 关键要点\n- 以要点列出核心事实/结论（3-8 条）\n"
    "## 相关概念\n- 概念名：一句话说明其关联（从资料中提炼实体/关键词）\n"
    "## 原文精选\n（摘录 2-4 句最有代表性的原文）"
)

_DIGEST_PROMPT = (
    "你是「百科全书主编」，请基于给定的多份知识库资料，围绕主题撰写一则结构化的\n"
    "跨素材综合词条。要求：\n"
    "- 只依据提供的资料，标注引用来源编号 [来源 X]，不得编造\n"
    "- 输出纯 Markdown 正文，不要用代码块包裹，不要输出解释性前后缀\n"
    "- 中文撰写，突出实体间的关联，便于复用与浏览\n\n"
    "按以下结构输出：\n"
    "# 主题标题\n"
    "## 主题概述\n"
    "## 核心结论\n- 要点，附 [来源 X]\n"
    "## 关键实体\n- 实体名：说明\n"
    "## 实体关联\n- A → B：关系描述\n"
    "## 参考来源"
)


def wiki_compile_enabled() -> bool:
    """生效的知识编译开关：系统设置页 > .env 默认"""
    value = runtime_config.get("wiki_compile_enabled")
    return settings.WIKI_COMPILE_ENABLED if value is None else bool(value)


def wiki_priority_enabled() -> bool:
    """检索是否优先命中百科卡片"""
    return runtime_config.wiki_priority_enabled


async def _llm_complete(messages: List[dict]) -> str:
    """调用 LLM 收集完整回复（内部走流式，失败自动降级非流式）。"""
    llm = LLMFactory.create()
    collected = []
    async for chunk in llm.chat(messages, stream=True):
        collected.append(chunk)
    return "".join(collected).strip()


def _strip_noise(text: str) -> str:
    """清洗 LLM 输出：去除可能的代码块围栏与首尾空白。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    return text


def _first_heading(text: str, fallback: str) -> str:
    """取正文首个一级标题作为词条标题，回退给定标题。"""
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()[:120]
    return (fallback or "百科词条")[:120]


def _safe_filename(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", (title or "wiki")).strip("_")
    return (name or "wiki")[:80]


class WikiService:
    """llm-wiki 知识编译服务"""

    # ---------- 后台调度 ----------

    def schedule_compile(
        self,
        *,
        source_image: str,
        full_text: str,
        kb_id: str,
        visibility: str,
        owner_id: str,
        source_type: str,
        title: str = "",
        pdf_preview_path: Optional[str] = None,
    ) -> None:
        """资料入库成功后调度后台编译，不阻塞上传响应。"""
        if not wiki_compile_enabled():
            logger.info("[WIKI] 知识编译已关闭，跳过: %s", Path(source_image).name)
            return
        task = asyncio.create_task(
            self.compile_source(
                source_image=source_image,
                full_text=full_text,
                kb_id=kb_id,
                visibility=visibility,
                owner_id=owner_id,
                source_type=source_type,
                title=title,
                pdf_preview_path=pdf_preview_path,
            )
        )
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)

    async def compile_source(
        self,
        *,
        source_image: str,
        full_text: str,
        kb_id: str,
        visibility: str,
        owner_id: str,
        source_type: str,
        title: str = "",
        pdf_preview_path: Optional[str] = None,
    ) -> Optional[dict]:
        """把单个资料编译成百科知识卡片并回写向量库；失败静默返回 None。"""
        content = (full_text or "").strip()
        if len(content) < settings.WIKI_MIN_SOURCE_CHARS:
            logger.info("[WIKI] 资料过短(%d字)不编译: %s", len(content), Path(source_image).name)
            return None
        if len(content) > settings.WIKI_MAX_SOURCE_CHARS:
            content = content[: settings.WIKI_MAX_SOURCE_CHARS]

        base_title = title.strip() or Path(source_image).stem
        try:
            # 对免费/低配额 LLM 友好：相邻两次编译请求之间保留最小间隔
            global _LAST_COMPILE_AT
            wait = settings.WIKI_COMPILE_INTERVAL - (asyncio.get_running_loop().time() - _LAST_COMPILE_AT)
            if wait > 0:
                await asyncio.sleep(wait)
            _LAST_COMPILE_AT = asyncio.get_running_loop().time()
            messages = [
                {"role": "system", "content": _CARD_PROMPT},
                {"role": "user", "content": f"资料标题：{base_title}\n\n资料内容：\n{content}"},
            ]
            card = _strip_noise(await _llm_complete(messages))
            if not card or len(card) < 40:
                logger.warning("[WIKI] 编译结果为空/过短，跳过: %s", base_title)
                return None
        except Exception as e:
            logger.warning("[WIKI] 编译调用 LLM 失败(%s): %s", base_title, e)
            return None

        wiki_title = _first_heading(card, base_title)
        # 重新编译：先清理该来源旧卡片
        self.delete_page_by_source(source_image, commit_only=False)

        page = self._save_page(
            page_type="source",
            title=wiki_title,
            content=card,
            kb_id=kb_id,
            visibility=visibility,
            owner_id=owner_id,
            source_image=source_image,
            source_refs=[source_image],
        )
        # 百科卡片以原始来源类型回写，使前端来源卡能跳回原文；打 is_wiki_page 标记
        page["doc_ids"] = self._index_page(
            page, source_type=source_type, pdf_preview_path=pdf_preview_path
        )
        self._update_doc_ids(page["id"], page["doc_ids"])
        self._mirror_md(page)
        logger.info(
            "[WIKI] 知识卡片编译完成 | %s | chunks=%d", wiki_title, len(page["doc_ids"])
        )
        return page

    async def synthesize(self, query: str, user_id: str) -> dict:
        """跨素材深度综合，生成 digest 百科条目并持久化。"""
        query = (query or "").strip()
        if not query:
            raise ValueError("主题不能为空")

        from app.services.knowledge import KnowledgeService

        # 复用现有混合检索获取原材料（同步重计算放入线程）
        results = await asyncio.to_thread(
            KnowledgeService.retrieve_for_user, query, user_id
        )
        if not results:
            raise ValueError("知识库中未检索到与该主题相关的素材")

        material_parts = []
        source_refs: List[str] = []
        kb_ids = set()
        visibility = "public"
        owner_id = ""
        for i, (text, meta, score) in enumerate(results[:12], start=1):
            material_parts.append(f"[来源 {i}] {text}")
            src = meta.get("source_image", "")
            if src and src not in source_refs:
                source_refs.append(src)
            if meta.get("kb_id"):
                kb_ids.add(meta["kb_id"])
            # 综合报告可见性：只要含私人素材即视为私人，归属首个 owner
            if meta.get("visibility") == "private":
                visibility = "private"
                owner_id = meta.get("owner_id", "") or owner_id

        material = "\n\n".join(material_parts)
        kb_id = next(iter(kb_ids)) if len(kb_ids) == 1 else ""

        messages = [
            {"role": "system", "content": _DIGEST_PROMPT},
            {"role": "user", "content": f"主题：{query}\n\n【知识库资料】\n{material}"},
        ]
        report = _strip_noise(await _llm_complete(messages))
        if not report or len(report) < 40:
            raise ValueError("综合报告生成失败：模型未返回有效内容")

        title = _first_heading(report, query)
        page = self._save_page(
            page_type="digest",
            title=title,
            content=report,
            kb_id=kb_id,
            visibility=visibility,
            owner_id=owner_id,
            source_image="",
            source_refs=source_refs,
        )
        page["doc_ids"] = self._index_page(page, source_type="wiki")
        self._update_doc_ids(page["id"], page["doc_ids"])
        self._mirror_md(page)
        logger.info("[WIKI] digest 综合报告生成完成 | %s | 引用来源=%d", title, len(source_refs))
        return page

    # ---------- 存储 ----------

    def _save_page(
        self,
        *,
        page_type: str,
        title: str,
        content: str,
        kb_id: str,
        visibility: str,
        owner_id: str,
        source_image: str,
        source_refs: List[str],
    ) -> dict:
        now = datetime.now().isoformat()
        page = {
            "id": str(uuid.uuid4()),
            "page_type": page_type,
            "title": title,
            "content": content,
            "kb_id": kb_id or "",
            "visibility": visibility or "public",
            "owner_id": owner_id or "",
            "source_image": source_image or "",
            "source_refs": source_refs,
            "doc_ids": [],
            "created_at": now,
            "updated_at": now,
        }
        db.execute(
            "INSERT INTO wiki_pages (id, page_type, title, content, kb_id, visibility,"
            " owner_id, source_image, source_refs, doc_ids, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                page["id"], page_type, title, content, page["kb_id"], page["visibility"],
                page["owner_id"], page["source_image"],
                json.dumps(source_refs, ensure_ascii=False), "[]", now, now,
            ),
        )
        return page

    def _index_page(self, page: dict, *, source_type: str, pdf_preview_path: Optional[str] = None) -> List[str]:
        """把百科卡片向量化回写，供检索命中（同步 Embedding，放入调用线程执行）。"""
        try:
            chunks = vector_store.split_text(
                page["content"],
                page["source_image"],
                visibility=page["visibility"],
                owner_id=page["owner_id"],
                kb_id=page["kb_id"],
                source_type=source_type,
                pdf_preview_path=pdf_preview_path,
                is_wiki_page=True,
                wiki_page_id=page["id"],
                wiki_title=page["title"],
            )
            if not chunks:
                return []
            return vector_store.add_texts([c[0] for c in chunks], [c[1] for c in chunks])
        except Exception as e:
            logger.warning("[WIKI] 百科卡片向量化失败(%s): %s", page["title"], e)
            return []

    def _update_doc_ids(self, page_id: str, doc_ids: List[str]):
        db.execute(
            "UPDATE wiki_pages SET doc_ids = ? WHERE id = ?",
            (json.dumps(doc_ids, ensure_ascii=False), page_id),
        )

    def _mirror_md(self, page: dict):
        """编译产物以 Markdown 落盘镜像，可浏览/导出为知识站点。"""
        try:
            folder = Path(settings.WIKI_DIR) / (page["page_type"] + "s")
            folder.mkdir(parents=True, exist_ok=True)
            fp = folder / f"{_safe_filename(page['title'])}-{page['id'][:8]}.md"
            header = (
                f"<!-- wiki-page: {page['page_type']} | kb: {page['kb_id'] or '-'} "
                f"| visibility: {page['visibility']} | id: {page['id']} -->\n\n"
            )
            fp.write_text(header + page["content"] + "\n", encoding="utf-8")
        except Exception as e:
            logger.warning("[WIKI] 镜像 Markdown 失败: %s", e)

    # ---------- 查询 / 删除 ----------

    def list_pages(
        self,
        user_id: str,
        is_admin: bool,
        *,
        kb_id: str = "",
        page_type: str = "",
        limit: int = 100,
    ) -> List[dict]:
        sql = "SELECT * FROM wiki_pages WHERE 1=1"
        params: list = []
        if kb_id:
            sql += " AND kb_id = ?"
            params.append(kb_id)
        if page_type:
            sql += " AND page_type = ?"
            params.append(page_type)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        rows = db.query(sql, tuple(params))
        pages = [_row_to_page(r) for r in rows]
        # 权限：公共所有人可见，私人仅属主与管理员可见
        return [
            p for p in pages
            if p["visibility"] != "private" or is_admin or p["owner_id"] == user_id
        ]

    def get_page(self, page_id: str) -> Optional[dict]:
        row = db.query_one("SELECT * FROM wiki_pages WHERE id = ?", (page_id,))
        return _row_to_page(row) if row else None

    def delete_page(self, page_id: str) -> bool:
        """删除百科页面：向量 chunk + Markdown 镜像 + 数据库行。"""
        page = self.get_page(page_id)
        if not page:
            return False
        self._drop_vectors(page)
        self._remove_mirror(page)
        db.execute("DELETE FROM wiki_pages WHERE id = ?", (page_id,))
        return True

    def delete_page_by_source(self, source_image: str, commit_only: bool = False) -> int:
        """删除某来源编译出的百科卡片（重新编译/来源删除时联动）。"""
        rows = db.query(
            "SELECT * FROM wiki_pages WHERE page_type='source' AND source_image = ?",
            (source_image,),
        )
        count = 0
        for row in rows:
            page = _row_to_page(row)
            self._drop_vectors(page)
            self._remove_mirror(page)
            db.execute("DELETE FROM wiki_pages WHERE id = ?", (page["id"],))
            count += 1
        if count:
            logger.info("[WIKI] 删除来源百科卡片 %d 则: %s", count, Path(source_image).name)
        return count

    def _drop_vectors(self, page: dict):
        """按 doc_ids 删除该页写入向量库的 chunk。"""
        doc_ids = page.get("doc_ids") or []
        if not doc_ids:
            return
        store = vector_store._load_store()
        if store is None:
            return
        try:
            store.delete(doc_ids)
            vector_store._save()
        except Exception as e:
            logger.warning("[WIKI] 删除向量条目失败: %s", e)
        meta_map = vector_store._load_metadata()
        changed = False
        for did in doc_ids:
            if did in meta_map:
                meta_map.pop(did)
                changed = True
        if changed:
            vector_store._save_metadata(meta_map)

    def _remove_mirror(self, page: dict):
        folder = Path(settings.WIKI_DIR) / (page["page_type"] + "s")
        fp = folder / f"{_safe_filename(page['title'])}-{page['id'][:8]}.md"
        try:
            if fp.exists():
                fp.unlink()
        except OSError:
            pass

    async def recompile_all(self, user_id: str = "", is_admin: bool = True) -> dict:
        """为存量上传记录补编译（后台逐条调度，错开避免瞬时压垮 LLM）。"""
        from app.services.records import upload_records

        if not wiki_compile_enabled():
            return {"scheduled": 0, "message": "知识编译未开启，请先在系统设置中启用"}

        records, _ = upload_records.list_records(limit=10000, offset=0)
        meta_map = vector_store._load_metadata()

        def _kb_for_source(src: str) -> str:
            for m in meta_map.values():
                if m.get("source_image") == src and m.get("kb_id"):
                    return m.get("kb_id", "")
            return ""

        scheduled = 0
        for rec in records:
            src = rec.get("image_path", "")
            text = (rec.get("ocr_text") or "").strip()
            if not src or len(text) < settings.WIKI_MIN_SOURCE_CHARS:
                continue
            self.schedule_compile(
                source_image=src,
                full_text=text,
                kb_id=_kb_for_source(src),
                visibility=rec.get("visibility", "public"),
                owner_id=rec.get("owner_id", ""),
                source_type=rec.get("source_type", "image"),
                title=rec.get("title", "") or rec.get("filename", ""),
                pdf_preview_path=rec.get("pdf_preview_path"),
            )
            scheduled += 1
            await asyncio.sleep(0.05)  # 轻微错峰，避免同时打满 LLM 并发
        return {"scheduled": scheduled, "message": f"已调度 {scheduled} 条资料后台编译"}

    def status(self) -> dict:
        total = db.query_one("SELECT COUNT(*) AS n FROM wiki_pages")
        return {
            "compile_enabled": wiki_compile_enabled(),
            "priority_enabled": wiki_priority_enabled(),
            "page_count": (total or {}).get("n", 0),
        }


def _row_to_page(row: dict) -> dict:
    return {
        "id": row["id"],
        "page_type": row["page_type"],
        "title": row["title"],
        "content": row["content"],
        "kb_id": row["kb_id"],
        "visibility": row["visibility"],
        "owner_id": row["owner_id"],
        "source_image": row["source_image"],
        "source_refs": json.loads(row["source_refs"] or "[]"),
        "doc_ids": json.loads(row["doc_ids"] or "[]"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


wiki_service = WikiService()
