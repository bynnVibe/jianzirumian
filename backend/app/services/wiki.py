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

工业级增强（对齐 LLM-Wiki 方法论）：
- 两步思维链编译：Step1 输出结构化分析 JSON（实体/论点/与现有 Wiki 关联/矛盾）
  → Step2 基于分析生成卡片，分析与生成解耦，各自质量更高、可缓存可审查。
- SHA256 增量缓存：wiki_pages.source_hash 记录来源文本哈希，未变更自动跳过。
- 持久化编译队列：串行处理防并发 LLM 调用；队列落库，重启后自动恢复未完成
  任务；失败任务自动重试（最多 WIKI_COMPILE_MAX_ATTEMPTS 次）。
- 标准化目录结构：编译产物由 app.services.wiki_store 落盘为可浏览/导出的知识站点。

设计原则：编译完全异步、可关闭、失败静默降级——任何异常都不影响现有上传
与问答链路（与原 RAG 共存）。
"""
import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from app.config import settings
from app.core import db
from app.core import observability as obs
from app.core.llm import LLMFactory
from app.core.vector_store import vector_store
from app.services import wiki_store
from app.services.config_manager import runtime_config

logger = logging.getLogger("jianziruyang.wiki")

# 后台 worker / 编译任务引用集（防止 task 在 pending 期被 GC）
_BG_TASKS: set = set()
_WORKER_TASK: Optional[asyncio.Task] = None

# 相邻编译请求的最小间隔时间戳（event loop 时钟），缓解 LLM 并发限流
_LAST_COMPILE_AT = 0.0


class WikiCompileError(Exception):
    """可重试的编译失败（LLM 调用异常/结果为空），供队列 worker 捕获重试。"""


# Step1 分析提示词：把资料"读一遍"输出结构化分析 JSON（两步思维链第一步）
_ANALYSIS_PROMPT = (
    "你是「知识分析师」，这是两步思维链知识编译的第一步。请阅读资料并输出结构化分析，"
    "只输出一个 JSON 对象，不要输出任何解释、不要用代码块包裹。字段定义：\n"
    "{\n"
    '  "entities": [{"name": "实体名", "type": "person|org|product|concept", "note": "一句话说明"}],\n'
    '  "claims": ["资料的核心论点或结论，3-8 条"],\n'
    '  "wiki_relations": {"existing": ["已存在于知识库中的相关词条名"], "new": ["建议新增的词条名"]},\n'
    '  "contradictions": ["与现有知识或常识的矛盾/张力，若无则为空数组"],\n'
    '  "structure": {"title": "建议的词条标题", "keywords": ["主题关键词，3-8 个"]}\n'
    "}\n"
    "要求：严格忠于原文，绝不编造；实体与关键词用于构建知识图谱的实体页/主题页。"
)

# Step2 生成提示词：基于 Step1 分析把资料"编译"成结构化百科条目
_CARD_PROMPT = (
    "你是「知识编译器」，这是两步思维链知识编译的第二步。请基于给定的结构化分析结果"
    "与原始资料，把它编译成个人百科全书中的一则词条。要求：\n"
    "- 严格忠于原文，绝不编造资料中没有的内容\n"
    "- 输出纯 Markdown 正文，不要用代码块包裹，不要输出任何解释或前后缀引导语\n"
    "- 用中文撰写，信息密度高，便于后续检索命中\n\n"
    "按以下结构输出：\n"
    "# 词条标题\n"
    "## 概述\n（2-4 句说明这则资料讲的是什么）\n"
    "## 关键要点\n- 以要点列出核心事实/结论（3-8 条）\n"
    "## 相关概念\n- 概念名：一句话说明其关联（从分析结果的实体/关键词中提炼）\n"
    "## 原文精选\n（摘录 2-4 句最有代表性的原文）"
)

# 单步回退提示词（两步关闭或分析失败时使用）
_CARD_PROMPT_SINGLE = (
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


async def _llm_complete(messages: List[dict], llm: "BaseLLM" = None) -> str:
    """调用 LLM 收集完整回复（内部走流式，失败自动降级非流式）。

    llm 为空时使用系统主 LLM；知识助手会传入自己的专用模型实例。
    """
    if llm is None:
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


def _extract_json(text: str) -> dict:
    """从 LLM 输出中尽力解析出 JSON 对象（容忍代码块/前后缀噪声）。"""
    text = _strip_noise(text)
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        pass
    # 退化：截取首个 { 到末个 } 的子串再试
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return {}
    return {}


def _first_heading(text: str, fallback: str) -> str:
    """取正文首个一级标题作为词条标题，回退给定标题。"""
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()[:120]
    return (fallback or "百科词条")[:120]


def _now() -> str:
    return datetime.now().isoformat()


class WikiService:
    """llm-wiki 知识编译服务"""

    # ---------- 持久化编译队列 ----------

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
        force: bool = False,
    ) -> None:
        """资料入库成功后把编译任务写入持久化队列并唤醒 worker，不阻塞上传响应。"""
        if not wiki_compile_enabled():
            logger.info("[WIKI] 知识编译已关闭，跳过: %s", Path(source_image).name)
            return

        content = (full_text or "").strip()
        if len(content) < settings.WIKI_MIN_SOURCE_CHARS:
            logger.info("[WIKI] 资料过短(%d字)不入队: %s", len(content), Path(source_image).name)
            return

        sha = wiki_store.source_hash(content)
        # SHA256 增量：来源文本未变更则不重复入队（force 重编译绕过）
        if not force and settings.WIKI_INCREMENTAL_ENABLED and self._hash_unchanged(source_image, sha):
            logger.info("[WIKI] 来源未变更(哈希一致)，跳过入队: %s", Path(source_image).name)
            wiki_store.append_log("skip", "source", title or Path(source_image).name, f"hash={sha[:12]}")
            return

        payload = {
            "source_image": source_image,
            "full_text": content,
            "kb_id": kb_id or "",
            "visibility": visibility or "public",
            "owner_id": owner_id or "",
            "source_type": source_type,
            "title": title or "",
            "pdf_preview_path": pdf_preview_path,
            "force": force,
        }
        self._enqueue(source_image, sha, payload)
        self._ensure_worker()

    def _hash_unchanged(self, source_image: str, sha: str) -> bool:
        row = db.query_one(
            "SELECT source_hash FROM wiki_pages WHERE page_type='source' AND source_image = ?"
            " ORDER BY updated_at DESC LIMIT 1",
            (source_image,),
        )
        return bool(row and row.get("source_hash") and row["source_hash"] == sha)

    def _enqueue(self, source_image: str, sha: str, payload: dict) -> str:
        qid = str(uuid.uuid4())
        now = _now()
        db.execute(
            "INSERT INTO wiki_compile_queue (id, source_image, source_hash, payload,"
            " status, attempts, error, created_at, updated_at)"
            " VALUES (?,?,?,?, 'pending', 0, '', ?, ?)",
            (qid, source_image, sha, json.dumps(payload, ensure_ascii=False), now, now),
        )
        return qid

    def _ensure_worker(self) -> None:
        """确保后台串行 worker 在运行（需在事件循环内调用）。"""
        global _WORKER_TASK
        if _WORKER_TASK and not _WORKER_TASK.done():
            return
        try:
            _WORKER_TASK = asyncio.create_task(self._worker_loop())
            _BG_TASKS.add(_WORKER_TASK)
            _WORKER_TASK.add_done_callback(_BG_TASKS.discard)
        except RuntimeError:
            # 无运行中的事件循环（理论上不应发生），退化为一次性任务
            logger.warning("[WIKI] 无事件循环，编译 worker 未能启动")

    async def _worker_loop(self) -> None:
        """串行消费编译队列：一次一个任务，失败重试至上限，处理完自动退出。"""
        while True:
            row = db.query_one(
                "SELECT * FROM wiki_compile_queue WHERE status='pending'"
                " ORDER BY created_at LIMIT 1"
            )
            if not row:
                break
            qid = row["id"]
            db.execute(
                "UPDATE wiki_compile_queue SET status='processing', updated_at=? WHERE id=?",
                (_now(), qid),
            )
            try:
                payload = json.loads(row["payload"] or "{}")
            except Exception:
                payload = {}
            try:
                page = await self._compile_core(**payload)
                status = "done" if page else "skipped"
                db.execute(
                    "UPDATE wiki_compile_queue SET status=?, error='', updated_at=? WHERE id=?",
                    (status, _now(), qid),
                )
            except Exception as e:
                attempts = int(row["attempts"] or 0) + 1
                if attempts < settings.WIKI_COMPILE_MAX_ATTEMPTS:
                    db.execute(
                        "UPDATE wiki_compile_queue SET status='pending', attempts=?, error=?, updated_at=?"
                        " WHERE id=?",
                        (attempts, str(e)[:500], _now(), qid),
                    )
                    logger.warning("[WIKI] 编译失败，重试(%d/%d): %s",
                                   attempts, settings.WIKI_COMPILE_MAX_ATTEMPTS, e)
                else:
                    db.execute(
                        "UPDATE wiki_compile_queue SET status='failed', attempts=?, error=?, updated_at=?"
                        " WHERE id=?",
                        (attempts, str(e)[:500], _now(), qid),
                    )
                    wiki_store.append_log("fail", "source", payload.get("title", ""), f"error={str(e)[:120]}")
                    logger.error("[WIKI] 编译重试耗尽，标记 failed: %s", e)
            await asyncio.sleep(0.1)
        logger.info("[WIKI] 编译队列已清空，worker 退出")

    def recover_queue(self) -> int:
        """服务启动时恢复队列：清理已完成条目、唤醒 worker 处理遗留 pending。

        db._migrate 已把上次进程遗留的 processing 退回 pending。
        """
        try:
            db.execute(
                "DELETE FROM wiki_compile_queue WHERE status IN ('done', 'skipped')"
            )
            row = db.query_one(
                "SELECT COUNT(*) AS n FROM wiki_compile_queue WHERE status IN ('pending', 'processing')"
            )
            pending = (row or {}).get("n", 0)
            if pending:
                logger.info("[WIKI] 启动恢复编译队列：%d 条待处理任务", pending)
                self._ensure_worker()
            return pending
        except Exception as e:
            logger.warning("[WIKI] 恢复编译队列失败: %s", e)
            return 0

    def queue_status(self) -> dict:
        rows = db.query(
            "SELECT status, COUNT(*) AS n FROM wiki_compile_queue GROUP BY status"
        )
        counts = {r["status"]: r["n"] for r in rows}
        return {
            "pending": counts.get("pending", 0),
            "processing": counts.get("processing", 0),
            "failed": counts.get("failed", 0),
            "max_attempts": settings.WIKI_COMPILE_MAX_ATTEMPTS,
        }

    # ---------- 两步思维链编译 ----------

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
        force: bool = True,
    ) -> Optional[dict]:
        """手动/同步编译单个资料（API 直接调用）；失败静默返回 None。

        默认 force=True：手动触发视为强制重编译，绕过 SHA256 增量缓存。
        """
        try:
            return await self._compile_core(
                source_image=source_image,
                full_text=full_text,
                kb_id=kb_id,
                visibility=visibility,
                owner_id=owner_id,
                source_type=source_type,
                title=title,
                pdf_preview_path=pdf_preview_path,
                force=force,
            )
        except Exception as e:
            logger.warning("[WIKI] 编译失败(%s): %s", title or Path(source_image).name, e)
            return None

    async def _compile_core(
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
        force: bool = False,
    ) -> Optional[dict]:
        """编译核心：两步思维链 + 增量跳过。跳过返回 None，LLM 失败抛 WikiCompileError。"""
        content = (full_text or "").strip()
        if len(content) < settings.WIKI_MIN_SOURCE_CHARS:
            logger.info("[WIKI] 资料过短(%d字)不编译: %s", len(content), Path(source_image).name)
            return None

        sha = wiki_store.source_hash(content)
        if not force and settings.WIKI_INCREMENTAL_ENABLED and self._hash_unchanged(source_image, sha):
            logger.info("[WIKI] 来源未变更(哈希一致)，跳过编译: %s", Path(source_image).name)
            wiki_store.append_log("skip", "source", title or Path(source_image).name, f"hash={sha[:12]}")
            return None

        if len(content) > settings.WIKI_MAX_SOURCE_CHARS:
            content = content[: settings.WIKI_MAX_SOURCE_CHARS]

        base_title = (title or "").strip() or Path(source_image).stem
        # 可观测性：一次编译 = 一条 trace（Step1/Step2 各记一条 llm span）
        obs.start_trace("wiki_compile", owner_id, base_title)
        _trace_status, _trace_error = "ok", ""
        try:
            await self._throttle()

            # Step1：结构化分析（best-effort，失败降级为单步生成）
            analysis: dict = {}
            if settings.WIKI_TWO_STEP_ENABLED:
                analysis = await self._analyze(content, base_title)

            # Step2：基于分析生成卡片
            card = await self._generate(content, base_title, analysis)
            if not card or len(card) < 40:
                raise WikiCompileError("编译结果为空/过短")

            wiki_title = _first_heading(card, base_title)
            # 重新编译：先清理该来源旧卡片
            self.delete_page_by_source(source_image, rebuild=False)

            page = self._save_page(
                page_type="source",
                title=wiki_title,
                content=card,
                kb_id=kb_id,
                visibility=visibility,
                owner_id=owner_id,
                source_image=source_image,
                source_refs=[source_image],
                source_hash=sha,
                analysis=analysis,
            )
            # 百科卡片以原始来源类型回写，使前端来源卡能跳回原文；打 is_wiki_page 标记
            page["doc_ids"] = self._index_page(
                page, source_type=source_type, pdf_preview_path=pdf_preview_path
            )
            self._update_doc_ids(page["id"], page["doc_ids"])

            # 落盘镜像 + 缓存 + 日志 + 派生视图重建
            wiki_store.write_page(page)
            wiki_store.update_cache(source_image, sha, page["id"])
            wiki_store.append_log(
                "ingest", "source", wiki_title,
                f"hash={sha[:12]} chunks={len(page['doc_ids'])} entities={len(analysis.get('entities', []))}"
            )
            self._rebuild_views()
            logger.info("[WIKI] 知识卡片编译完成 | %s | chunks=%d", wiki_title, len(page["doc_ids"]))
            return page
        except Exception as e:
            _trace_status, _trace_error = "error", str(e)
            raise
        finally:
            obs.end_trace(_trace_status, _trace_error)

    async def _throttle(self) -> None:
        """对免费/低配额 LLM 友好：相邻两次编译请求之间保留最小间隔。"""
        global _LAST_COMPILE_AT
        loop = asyncio.get_running_loop()
        wait = settings.WIKI_COMPILE_INTERVAL - (loop.time() - _LAST_COMPILE_AT)
        if wait > 0:
            await asyncio.sleep(wait)
        _LAST_COMPILE_AT = loop.time()

    async def _analyze(self, content: str, base_title: str) -> dict:
        """Step1：输出结构化分析 JSON。任何异常降级为空分析（不阻断编译）。"""
        try:
            existing = self._existing_titles()
            ctx = ("知识库现有词条（用于判断关联，最多 80 条）：\n"
                   + "、".join(existing)) if existing else "（知识库暂无其他词条）"
            messages = [
                {"role": "system", "content": _ANALYSIS_PROMPT},
                {"role": "user", "content": f"{ctx}\n\n资料标题：{base_title}\n\n资料内容：\n{content}"},
            ]
            t0 = time.monotonic()
            try:
                raw = await _llm_complete(messages)
            except Exception as e:
                obs.record_llm("wiki_analyze", messages, "", t0=t0,
                               status="error", error=str(e), fail_trace=False)
                raise
            obs.record_llm("wiki_analyze", messages, raw, t0=t0, fail_trace=False)
            analysis = _extract_json(raw)
            if not isinstance(analysis, dict):
                return {}
            return analysis
        except Exception as e:
            logger.warning("[WIKI] Step1 分析失败，降级为单步编译: %s", e)
            return {}

    async def _generate(self, content: str, base_title: str, analysis: dict) -> str:
        """Step2：基于分析结果生成百科卡片正文。LLM 异常向上抛出供队列重试。"""
        if analysis:
            system_prompt = _CARD_PROMPT
            analysis_text = json.dumps(analysis, ensure_ascii=False, indent=2)
            user_content = (
                f"资料标题：{base_title}\n\n"
                f"【Step1 结构化分析结果】\n{analysis_text}\n\n"
                f"【原始资料】\n{content}"
            )
        else:
            system_prompt = _CARD_PROMPT_SINGLE
            user_content = f"资料标题：{base_title}\n\n资料内容：\n{content}"
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        t0 = time.monotonic()
        try:
            raw = await _llm_complete(messages)
        except Exception as e:
            obs.record_llm("wiki_generate", messages, "", t0=t0, status="error", error=str(e))
            raise
        obs.record_llm("wiki_generate", messages, raw, t0=t0)
        return _strip_noise(raw)

    def _existing_titles(self, limit: int = 80) -> List[str]:
        rows = db.query(
            "SELECT title FROM wiki_pages WHERE page_type='source'"
            " ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        return [r["title"] for r in rows if r.get("title")]

    async def synthesize(self, query: str, user_id: str) -> dict:
        """跨素材深度综合，生成 digest 百科条目并持久化（外层记可观测性 trace）。"""
        query = (query or "").strip()
        if not query:
            raise ValueError("主题不能为空")
        obs.start_trace("wiki_synthesize", user_id, query)
        status, err = "ok", ""
        try:
            return await self._synthesize_inner(query, user_id)
        except Exception as e:
            status, err = "error", str(e)
            raise
        finally:
            obs.end_trace(status, err)

    async def _synthesize_inner(self, query: str, user_id: str) -> dict:
        """综合报告内层实现（检索素材 → LLM 生成 → 落库索引）。"""
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
        t0 = time.monotonic()
        try:
            raw = await _llm_complete(messages)
        except Exception as e:
            obs.record_llm("digest_generate", messages, "", t0=t0, status="error", error=str(e))
            raise
        obs.record_llm("digest_generate", messages, raw, t0=t0)
        report = _strip_noise(raw)
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
            source_hash="",
            analysis={},
        )
        page["doc_ids"] = self._index_page(page, source_type="wiki")
        self._update_doc_ids(page["id"], page["doc_ids"])
        wiki_store.write_page(page)
        wiki_store.append_log("digest", "digest", title, f"refs={len(source_refs)}")
        self._rebuild_views()
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
        source_hash: str = "",
        analysis: Optional[dict] = None,
    ) -> dict:
        now = _now()
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
            "source_hash": source_hash or "",
            "analysis": analysis or {},
            "created_at": now,
            "updated_at": now,
        }
        db.execute(
            "INSERT INTO wiki_pages (id, page_type, title, content, kb_id, visibility,"
            " owner_id, source_image, source_refs, doc_ids, source_hash, analysis,"
            " created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                page["id"], page_type, title, content, page["kb_id"], page["visibility"],
                page["owner_id"], page["source_image"],
                json.dumps(source_refs, ensure_ascii=False), "[]",
                page["source_hash"], json.dumps(page["analysis"], ensure_ascii=False),
                now, now,
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

    def _rebuild_views(self) -> None:
        """基于全部页面重建 index.md / overview.md / entities / topics 派生视图。"""
        try:
            rows = db.query("SELECT * FROM wiki_pages")
            wiki_store.rebuild_views([_row_to_page(r) for r in rows])
        except Exception as e:
            logger.warning("[WIKI] 重建派生视图失败: %s", e)

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
        """删除百科页面：向量 chunk + Markdown 镜像 + 缓存 + 数据库行，并重建视图。"""
        page = self.get_page(page_id)
        if not page:
            return False
        self._drop_vectors(page)
        wiki_store.remove_page(page)
        if page.get("source_image"):
            wiki_store.remove_cache(page["source_image"])
        db.execute("DELETE FROM wiki_pages WHERE id = ?", (page_id,))
        wiki_store.append_log("delete", page["page_type"], page["title"], f"id={page_id[:8]}")
        self._rebuild_views()
        return True

    def delete_page_by_source(self, source_image: str, rebuild: bool = True, commit_only: bool = False) -> int:
        """删除某来源编译出的百科卡片（重新编译/来源删除时联动）。"""
        rows = db.query(
            "SELECT * FROM wiki_pages WHERE page_type='source' AND source_image = ?",
            (source_image,),
        )
        count = 0
        for row in rows:
            page = _row_to_page(row)
            self._drop_vectors(page)
            wiki_store.remove_page(page)
            db.execute("DELETE FROM wiki_pages WHERE id = ?", (page["id"],))
            count += 1
        if count:
            wiki_store.remove_cache(source_image)
            wiki_store.append_log("delete", "source", Path(source_image).name, f"count={count}")
            logger.info("[WIKI] 删除来源百科卡片 %d 则: %s", count, Path(source_image).name)
            if rebuild:
                self._rebuild_views()
        return count

    def update_page_content(self, page_id: str, new_content: str, new_title: Optional[str] = None) -> Optional[dict]:
        """应用对百科页面正文的编辑（知识助手人工确认后调用）。

        流程：删旧向量 → 更新 DB 正文/标题 → 重新向量化 → 落盘镜像 → 写日志 → 重建派生视图。
        返回更新后的页面 dict；页面不存在返回 None。
        """
        page = self.get_page(page_id)
        if not page:
            return None
        new_content = (new_content or "").strip()
        if not new_content:
            raise ValueError("编辑内容不能为空")

        title = (new_title or "").strip() or _first_heading(new_content, page["title"])
        # 重新向量化前需要原始来源类型（用于前端来源卡跳回原文）
        source_type = "wiki"
        pdf_preview_path = None
        if page.get("source_image"):
            from app.services.records import upload_records

            rec = upload_records.get_record_by_source(page["source_image"])
            if rec:
                source_type = rec.get("source_type", "image")
                pdf_preview_path = rec.get("pdf_preview_path")

        self._drop_vectors(page)
        now = _now()
        db.execute(
            "UPDATE wiki_pages SET content = ?, title = ?, updated_at = ? WHERE id = ?",
            (new_content, title, now, page_id),
        )
        page["content"] = new_content
        page["title"] = title
        page["updated_at"] = now
        page["doc_ids"] = self._index_page(
            page, source_type=source_type, pdf_preview_path=pdf_preview_path
        )
        self._update_doc_ids(page_id, page["doc_ids"])
        wiki_store.write_page(page)
        wiki_store.append_log("edit", page["page_type"], title, f"id={page_id[:8]}")
        self._rebuild_views()
        logger.info("[WIKI] 百科页面编辑已应用 | %s | chunks=%d", title, len(page["doc_ids"]))
        return page

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

    async def recompile_all(self, user_id: str = "", is_admin: bool = True, force: bool = False) -> dict:
        """为存量上传记录补编译（写入持久化队列，串行 worker 逐条处理）。"""
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
                force=force,
            )
            scheduled += 1
        self._ensure_worker()
        return {"scheduled": scheduled, "message": f"已入队 {scheduled} 条资料后台编译"}

    def status(self) -> dict:
        total = db.query_one("SELECT COUNT(*) AS n FROM wiki_pages")
        return {
            "compile_enabled": wiki_compile_enabled(),
            "priority_enabled": wiki_priority_enabled(),
            "two_step_enabled": settings.WIKI_TWO_STEP_ENABLED,
            "incremental_enabled": settings.WIKI_INCREMENTAL_ENABLED,
            "page_count": (total or {}).get("n", 0),
            "queue": self.queue_status(),
        }


def _row_to_page(row: dict) -> dict:
    try:
        analysis = json.loads(row["analysis"] or "{}")
    except Exception:
        analysis = {}
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
        "source_hash": row["source_hash"] if "source_hash" in row.keys() else "",
        "analysis": analysis,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


wiki_service = WikiService()
