"""
见字如面 - 知识库上传流水线服务

把「上传即入库」改造为两段式流水线：

1. 文件上传：图片/Word/PDF/Markdown 统一上传，仅落盘 + 登记待处理，不做任何解析；
2. 后台解析：多线程并发解析（图片 OCR、Word/PDF 文档解析、Markdown 章节解析），产出可预览的文本；
3. 知识入库：用户在「知识入库」模块检查解析结果，确认后点击入库，
   才向量化写入知识库 + 后台编译 llm-wiki 百科卡片——与原有「原始文档 +
   处理后文本」的对应逻辑完全一致（上传记录、页码元数据、Word 转 PDF 预览等）。
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from app.core import db
from app.core.ocr import OCRFactory
from app.core.vector_store import vector_store
from app.services.knowledge import clean_ocr_text
from app.services.records import upload_records

logger = logging.getLogger("jianziruyang.pipeline")

# 后台解析任务引用集（防止 task 在 pending 期被 GC）
_BG_PARSE_TASKS: set = set()

# 后台入库任务引用集（入库向量化重活后台执行，不阻塞请求线程）
_BG_INGEST_TASKS: set = set()

# 解析并发上限：多个文件多线程并发解析，同时避免 OCR/文档解析压垮本机
_PARSE_SEMAPHORE: Optional[asyncio.Semaphore] = None
_PARSE_CONCURRENCY = 3


def _get_semaphore() -> asyncio.Semaphore:
    """信号量须绑定运行中的事件循环，延迟到首次调度时创建"""
    global _PARSE_SEMAPHORE
    if _PARSE_SEMAPHORE is None:
        _PARSE_SEMAPHORE = asyncio.Semaphore(_PARSE_CONCURRENCY)
    return _PARSE_SEMAPHORE


def _row_to_item(row: dict, with_text: bool = False) -> dict:
    """数据库行 → 业务 dict。默认不带全文（列表接口用），详情接口再附带全文"""
    pages = json.loads(row["pages"] or "[]")
    item = {
        "id": row["id"],
        "filename": row["filename"],
        "source_type": row["source_type"],
        "file_path": row["file_path"],
        "status": row["status"],
        "phase": row["phase"],
        "text_length": len(row["parsed_text"] or ""),
        "page_count": len(pages),
        "pdf_preview_path": row["pdf_preview_path"],
        "ocr_provider": row["ocr_provider"],
        "error": row["error"],
        "owner_id": row["owner_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if with_text:
        item["parsed_text"] = row["parsed_text"] or ""
        item["pages"] = pages
    return item


class UploadPipelineService:
    """知识库上传流水线：上传登记 → 后台解析 → 确认入库"""

    # ---------- 登记与查询 ----------

    def create(
        self,
        filename: str,
        source_type: str,
        file_path: str,
        owner_id: str,
        ocr_provider: str = "",
    ) -> dict:
        """登记一个待解析文件（文件已落盘）"""
        now = datetime.now().isoformat()
        item_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO pending_uploads (id, filename, source_type, file_path, status,"
            " ocr_provider, owner_id, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (item_id, filename, source_type, file_path, "pending",
             ocr_provider, owner_id, now, now),
        )
        row = db.query_one("SELECT * FROM pending_uploads WHERE id = ?", (item_id,))
        return _row_to_item(row, with_text=True)

    def get(self, item_id: str) -> Optional[dict]:
        row = db.query_one("SELECT * FROM pending_uploads WHERE id = ?", (item_id,))
        return _row_to_item(row, with_text=True) if row else None

    def list_items(self, owner_id: str = "", is_admin: bool = False) -> List[dict]:
        """待处理文件列表：普通用户只看自己的，管理员看全部"""
        if is_admin:
            rows = db.query("SELECT * FROM pending_uploads ORDER BY created_at DESC")
        else:
            rows = db.query(
                "SELECT * FROM pending_uploads WHERE owner_id = ? ORDER BY created_at DESC",
                (owner_id,),
            )
        return [_row_to_item(r) for r in rows]

    # ---------- 后台解析调度 ----------

    def recover(self) -> int:
        """服务启动时恢复未完成解析：parsing 回退为 pending，
        所有 pending 重新调度（重启不丢任务）。返回恢复条数"""
        from app.core.agent_runtime import runtime
        runtime.require_owner()
        affected = 0
        rows = db.query(
            "SELECT id FROM pending_uploads WHERE status = 'pending' ORDER BY created_at"
        )
        for r in rows:
            self.schedule_parse(r["id"])
        # 重启前正在后台入库的行：参数已落库，直接恢复后台入库任务
        ing_rows = db.query(
            "SELECT id FROM pending_uploads WHERE status = 'ingesting' ORDER BY created_at"
        )
        for r in ing_rows:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                break
            task = loop.create_task(self._run_ingest(r["id"]))
            _BG_INGEST_TASKS.add(task)
            task.add_done_callback(_BG_INGEST_TASKS.discard)
        if affected or rows or ing_rows:
            logger.info("上传流水线恢复: %d 条解析中回退，%d 条重新调度，%d 条恢复后台入库",
                        affected, len(rows), len(ing_rows))
        return len(rows) + len(ing_rows)

    def schedule_parse(self, item_id: str) -> None:
        """把解析任务丢进后台（多线程并发，受信号量限制），不阻塞上传响应"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("无运行中的事件循环，跳过后台解析调度: %s", item_id)
            return
        task = loop.create_task(self._run_parse(item_id))
        _BG_PARSE_TASKS.add(task)
        task.add_done_callback(_BG_PARSE_TASKS.discard)

    async def _run_parse(self, item_id: str) -> None:
        """执行单个文件的解析（线程池内完成重活，失败仅标记不影响主流程）"""
        sem = _get_semaphore()
        async with sem:
            row = db.query_one("SELECT * FROM pending_uploads WHERE id = ?", (item_id,))
            if not row:
                return
            now = datetime.now().isoformat()
            db.execute(
                "UPDATE pending_uploads SET status = 'parsing', phase = 'parse', error = '', updated_at = ? WHERE id = ?",
                (now, item_id),
            )
            try:
                if row["source_type"] == "image":
                    parsed_text, pages, pdf_preview_path = await self._parse_image(
                        row["file_path"], row["ocr_provider"] or None
                    )
                elif row["source_type"] == "word":
                    parsed_text, pages, pdf_preview_path = await self._parse_word(row["file_path"])
                elif row["source_type"] == "pdf":
                    parsed_text, pages, pdf_preview_path = await self._parse_pdf(row["file_path"])
                elif row["source_type"] == "markdown":
                    parsed_text, pages, pdf_preview_path = await self._parse_markdown(row["file_path"])
                else:
                    raise ValueError(f"不支持的文件类型: {row['source_type']}")

                now = datetime.now().isoformat()
                db.execute(
                    "UPDATE pending_uploads SET status = 'done', parsed_text = ?, pages = ?,"
                    " pdf_preview_path = ?, updated_at = ? WHERE id = ?",
                    (
                        parsed_text,
                        json.dumps(pages, ensure_ascii=False),
                        pdf_preview_path,
                        now,
                        item_id,
                    ),
                )
                logger.info(
                    "文件解析完成 [%s] %s: %d 字 / %d 页",
                    row["source_type"], row["filename"], len(parsed_text), len(pages),
                )
            except Exception as e:
                logger.warning("文件解析失败 [%s]: %s", row["filename"], e)
                now = datetime.now().isoformat()
                db.execute(
                    "UPDATE pending_uploads SET status = 'error', error = ?, updated_at = ? WHERE id = ?",
                    (str(e)[:500], now, item_id),
                )

    @staticmethod
    async def _parse_image(file_path: str, ocr_provider: str = None) -> tuple:
        """图片：OCR 识别文本"""
        ocr = OCRFactory.create(ocr_provider)
        text = await ocr.recognize(file_path)
        text = clean_ocr_text(text)
        if not text.strip():
            raise ValueError("OCR 未识别到文字，请确认图片清晰且包含文字")
        return text, [], None

    @staticmethod
    async def _parse_word(file_path: str) -> tuple:
        """Word：优先 LibreOffice 转 PDF 后按 PDF 分页（页码与原始排版对齐），
        无 LibreOffice 时降级 docx 逻辑分页"""
        from app.services.knowledge import knowledge_service

        def _work():
            pdf_path = knowledge_service._convert_word_to_pdf(file_path)
            if pdf_path:
                pages = knowledge_service._extract_pdf_pages(pdf_path)
                return pdf_path, pages
            return None, knowledge_service._extract_docx_pages(file_path)

        pdf_preview_path, pages = await asyncio.to_thread(_work)
        parsed_text = "\n\n".join(p.text for p in pages if p.text.strip())
        if not parsed_text.strip():
            raise ValueError("文档解析结果为空，请确认文档包含文字内容")
        return parsed_text, [p.model_dump() for p in pages], pdf_preview_path

    @staticmethod
    async def _parse_pdf(file_path: str) -> tuple:
        """PDF：按页解析（含表格 Markdown 化），原始文件即预览文件"""
        from app.services.knowledge import knowledge_service

        pages = await asyncio.to_thread(knowledge_service._extract_pdf_pages, file_path)
        parsed_text = "\n\n".join(p.text for p in pages if p.text.strip())
        if not parsed_text.strip():
            raise ValueError("PDF 解析结果为空，请确认文档包含文字内容（扫描件需走图片上传）")
        return parsed_text, [p.model_dump() for p in pages], file_path

    @staticmethod
    async def _parse_markdown(file_path: str) -> tuple:
        """Markdown：按标题层级拆为章节页（page_number=章节序号），无 PDF 预览"""
        from app.services.knowledge import knowledge_service

        pages = await asyncio.to_thread(knowledge_service._extract_markdown_pages, file_path)
        parsed_text = "\n\n".join(p["text"] for p in pages if p["text"].strip())
        if not parsed_text.strip():
            raise ValueError("Markdown 解析结果为空，请确认文档包含文字内容")
        return parsed_text, pages, None

    # ---------- 用户编辑解析结果 ----------

    def update_text(
        self,
        item_id: str,
        parsed_text: Optional[str] = None,
        pages: Optional[List[dict]] = None,
    ) -> Optional[dict]:
        """保存用户在入库前编辑过的解析结果（文本或分页）"""
        row = db.query_one("SELECT * FROM pending_uploads WHERE id = ?", (item_id,))
        if not row:
            return None
        now = datetime.now().isoformat()
        new_text = row["parsed_text"] if parsed_text is None else parsed_text
        new_pages = row["pages"] if pages is None else json.dumps(pages, ensure_ascii=False)
        db.execute(
            "UPDATE pending_uploads SET parsed_text = ?, pages = ?, updated_at = ? WHERE id = ?",
            (new_text, new_pages, now, item_id),
        )
        return self.get(item_id)

    # ---------- 入库 ----------

    async def ingest(
        self,
        item_id: str,
        kb_id: str = "",
        title: str = "",
        parsed_text: Optional[str] = None,
        pages: Optional[List[dict]] = None,
    ) -> dict:
        """确认入库：向量化写入知识库 + 创建上传记录 + 后台编译 wiki 卡片。

        与旧上传链路的对应关系保持一致：
        - 图片：整文切块入库（无页码）；
        - Word/PDF：逐页切块入库（带 page_number + pdf_preview_path，来源可跳页）；
        - Markdown：按章节整块入库，超长节内二次切分（带 section_title 标题路径）。
        """
        from app.services import ingest_dedup
        from app.services.knowledge import knowledge_service
        from app.services.usage import record_upload
        from app.services.wiki import wiki_service

        row = db.query_one("SELECT * FROM pending_uploads WHERE id = ?", (item_id,))
        if not row:
            raise ValueError("待入库文件不存在")
        if row["status"] not in ("done", "error", "ingesting"):
            raise ValueError("文件尚未解析完成，请稍候")

        # 用户可在入库时提交最新编辑结果（优先于已存文本）
        text = parsed_text if parsed_text is not None else row["parsed_text"]
        item_pages = pages if pages is not None else json.loads(row["pages"] or "[]")
        text = (text or "").strip()
        if not text and not item_pages:
            raise ValueError("解析内容为空，无法入库")

        file_path = row["file_path"]
        source_type = row["source_type"]
        owner_id = row["owner_id"]
        save_title = (title or "").strip() or Path(row["filename"]).stem

        kb_id, visibility, owner_id = knowledge_service._resolve_kb(kb_id, "public", owner_id)

        # 重复入库校验：入口即校验来源指纹，同一知识库内相同内容已入库则拦截，
        # 不再重复向量化 / 编译百科卡片，减少系统重复工作与资源消耗。
        fingerprint = ingest_dedup.compute_file_fingerprint(file_path)
        ingest_dedup.ensure_can_ingest(
            fingerprint, kb_id, ingest_dedup.KIND_FILE, save_title
        )

        if source_type == "image":
            text = clean_ocr_text(text)

            def _vectorize():
                chunks = vector_store.split_text(text, file_path, visibility, owner_id, kb_id=kb_id)
                if not chunks:
                    return []
                return vector_store.add_texts([c[0] for c in chunks], [c[1] for c in chunks])

            doc_ids = await asyncio.to_thread(_vectorize)
            chunk_count = len(doc_ids)
            pdf_preview_path = None
        else:
            doc_type = source_type  # word | pdf | markdown
            pdf_preview_path = row["pdf_preview_path"]
            if doc_type == "pdf" and not pdf_preview_path:
                pdf_preview_path = file_path

            if not item_pages:
                # 兼容：无分页信息时整文作为一页入库
                item_pages = [{"page_number": 1, "text": text}]

            if doc_type == "markdown":
                # Markdown 章节感知分块：短章节整块入库，超长节内二次切分（子块带标题路径前缀）
                def _save_markdown():
                    chunks = knowledge_service._markdown_chunks(
                        item_pages, file_path, visibility, owner_id, kb_id
                    )
                    if not chunks:
                        return []
                    return vector_store.add_texts(
                        [c[0] for c in chunks], [c[1] for c in chunks]
                    )

                doc_ids = await asyncio.to_thread(_save_markdown)
            else:
                all_doc_ids: List[str] = []
                for page in item_pages:
                    page_text = (page.get("text") or "").strip()
                    page_number = page.get("page_number") or 0
                    if not page_text or not page_number:
                        continue

                    def _save_one(t=page_text, pn=page_number):
                        chunks = vector_store.split_text(
                            t, file_path, visibility, owner_id,
                            page_number=pn,
                            pdf_preview_path=pdf_preview_path,
                            source_type=doc_type,
                            kb_id=kb_id,
                        )
                        if not chunks:
                            return []
                        return vector_store.add_texts(
                            [c[0] for c in chunks], [c[1] for c in chunks]
                        )

                    all_doc_ids.extend(await asyncio.to_thread(_save_one))

                doc_ids = all_doc_ids
            chunk_count = len(doc_ids)
            text = "\n\n".join((p.get("text") or "").strip() for p in item_pages)

        if chunk_count == 0:
            raise ValueError("入库失败：文本切块为空")

        # 上传记录（与旧链路字段一致，保证知识库管理/检索来源展示正常）
        from app.config import settings

        record = upload_records.add_record(
            filename=row["filename"],
            image_path=file_path,
            ocr_text=text,
            ocr_provider=(
                row["ocr_provider"] or settings.OCR_PROVIDER
            ) if source_type == "image" else f"document:{source_type}",
            chunk_count=chunk_count,
            doc_ids=doc_ids,
            visibility=visibility,
            owner_id=owner_id,
            source_type=source_type,
            pages=item_pages if source_type != "image" else None,
            pdf_preview_path=pdf_preview_path,
            title=save_title,
        )
        # 入库成功：登记来源指纹（幂等），供下次同库重复入库时入口拦截
        ingest_dedup.register(
            fingerprint, ingest_dedup.KIND_FILE, kb_id,
            source_type=source_type, title=save_title, owner_id=owner_id,
            record_id=record["id"], source_ref=file_path,
        )
        record_upload(owner_id)

        # llm-wiki：后台编译百科卡片（与旧链路相同参数，保证卡片能命中知识库路由）
        wiki_service.schedule_compile(
            source_image=file_path,
            full_text=text,
            kb_id=kb_id,
            visibility=visibility,
            owner_id=owner_id,
            source_type=source_type,
            title=save_title,
            pdf_preview_path=pdf_preview_path,
        )

        # 入库成功：移除流水线记录（原始文件由上传记录接管，不删除）
        db.execute("DELETE FROM pending_uploads WHERE id = ?", (item_id,))
        logger.info(
            "知识入库完成 [%s] %s → 知识库 %s，%d 段",
            source_type, row["filename"], kb_id, chunk_count,
        )

        return {
            "filename": row["filename"],
            "title": save_title,
            "image_path": file_path,
            "ocr_text": text,
            "chunk_count": chunk_count,
            "doc_ids": doc_ids,
            "success": True,
            "visibility": visibility,
            "source_type": source_type,
            "kb_id": kb_id,
            "pdf_preview_path": pdf_preview_path,
        }

    # ---------- 后台入库调度 ----------

    def schedule_ingest(
        self,
        item_id: str,
        kb_id: str = "",
        title: str = "",
        parsed_text: Optional[str] = None,
        pages: Optional[List[dict]] = None,
    ) -> dict:
        """提交后台入库：同步只做轻量预检（重复指纹/空内容），立即返回；
        向量化等重活由后台任务执行，不阻塞请求线程与前端界面。

        入库参数（目标库/标题）与用户最新编辑先落库，后台任务按行读取，
        重启后 recover 可恢复；失败标记 status=error + phase=ingest 供前端重试。
        """
        from app.services import ingest_dedup
        from app.services.knowledge import knowledge_service

        row = db.query_one("SELECT * FROM pending_uploads WHERE id = ?", (item_id,))
        if not row:
            raise ValueError("待入库文件不存在")
        if row["status"] == "ingesting":
            raise ValueError("该文件正在后台入库中，请在待入库列表查看进度")
        if row["status"] not in ("done", "error"):
            raise ValueError("文件尚未解析完成，请稍候")

        # 用户可在入库时提交最新编辑结果（先落库，后台任务按行读取）
        if parsed_text is not None or pages is not None:
            self.update_text(item_id, parsed_text=parsed_text, pages=pages)
            row = db.query_one("SELECT * FROM pending_uploads WHERE id = ?", (item_id,))

        text = (row["parsed_text"] or "").strip()
        item_pages = json.loads(row["pages"] or "[]")
        if not text and not item_pages:
            raise ValueError("解析内容为空，无法入库")

        # 重试入库时沿用上次提交的目标库/标题
        kb_id = (kb_id or "").strip() or row["ingest_kb_id"]
        title = (title or "").strip() or row["ingest_title"]
        save_title = title or Path(row["filename"]).stem
        eff_kb, _vis, _owner = knowledge_service._resolve_kb(kb_id, "public", row["owner_id"])
        fingerprint = ingest_dedup.compute_file_fingerprint(row["file_path"])
        ingest_dedup.ensure_can_ingest(fingerprint, eff_kb, ingest_dedup.KIND_FILE, save_title)

        now = datetime.now().isoformat()
        db.execute(
            "UPDATE pending_uploads SET status = 'ingesting', phase = 'ingest', error = '',"
            " ingest_kb_id = ?, ingest_title = ?, updated_at = ? WHERE id = ?",
            (eff_kb, title, now, item_id),
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            task = loop.create_task(self._run_ingest(item_id))
            _BG_INGEST_TASKS.add(task)
            task.add_done_callback(_BG_INGEST_TASKS.discard)
        else:
            # 无事件循环的同步调用场景：直接执行，保证入库不悬挂
            asyncio.run(self._run_ingest(item_id))
        logger.info("知识入库已提交后台 [%s] %s → 知识库 %s", row["source_type"], row["filename"], eff_kb)
        return {"item_id": item_id, "status": "ingesting"}

    async def _run_ingest(self, item_id: str) -> None:
        """后台入库任务：成功时 ingest() 移除行；失败标记 error+phase=ingest 供重试。"""
        row = db.query_one("SELECT * FROM pending_uploads WHERE id = ?", (item_id,))
        if not row or row["status"] != "ingesting":
            return
        try:
            await self.ingest(item_id, kb_id=row["ingest_kb_id"], title=row["ingest_title"])
        except Exception as e:
            logger.warning("后台入库失败 [%s]: %s", row["filename"], e)
            now = datetime.now().isoformat()
            db.execute(
                "UPDATE pending_uploads SET status = 'error', phase = 'ingest', error = ?, updated_at = ? WHERE id = ?",
                (str(e)[:500], now, item_id),
            )

    # ---------- 删除 ----------

    def delete(self, item_id: str) -> bool:
        """删除待入库文件（连同磁盘文件）。入库后的文件不在此表，不受影响"""
        from app.core.file_cache import file_cache

        row = db.query_one("SELECT * FROM pending_uploads WHERE id = ?", (item_id,))
        if not row:
            return False
        for p in (row["file_path"], row["pdf_preview_path"]):
            if not p:
                continue
            try:
                fp = Path(p)
                if fp.is_file():
                    fp.unlink()
                file_cache.invalidate_file(fp.name, "public", row["owner_id"] or "")
            except OSError as e:
                logger.warning("删除待入库文件失败 %s: %s", p, e)
        db.execute("DELETE FROM pending_uploads WHERE id = ?", (item_id,))
        return True


# 全局单例
upload_pipeline = UploadPipelineService()
