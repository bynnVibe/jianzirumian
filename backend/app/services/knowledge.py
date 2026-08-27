"""
见字如面 - 知识上传服务
流程: 上传图片 → OCR 识别 → 文本分割 → 向量化 → 存储
"""
import logging
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from app.config import settings
from app.core.ocr import OCRFactory
from app.models.schemas import DocumentPage
from app.services.records import upload_records

logger = logging.getLogger("jianziruyang.knowledge")
from app.core.reranker import normalize_score, rerank
from app.core.sparse_retriever import sparse_retriever
from app.core.vector_store import vector_store
from app.services.web_search import SearchResult

# 相似度过滤阈值：重排序后相关度（百分比）低于该值的知识片段
# 不进入 LLM 上下文、不随来源返回，避免低相关内容干扰回答
MIN_RELEVANCE_PCT = 40


# 文档类型 -> 文件扩展名映射
DOC_TYPE_EXTENSIONS = {
    "word": {".docx"},
    "pdf": {".pdf"},
}


def clean_ocr_text(text: str) -> str:
    """清洗 OCR 识别结果中的噪声和误识别 LaTeX 标记。

    主要处理：
    1. 保护真正的行内/块级公式（$...$ / $$...$$）不被破坏。
    2. 去除被误识别为 LaTeX 环境的标记，如 \\begin{align*}、\\end{align*}。
    3. 去除表格对齐符 \\& 和孤立的换行符 \\\\。
    4. 合并多余空行、规范化空白字符。
    """
    if not text:
        return text

    formulas = []

    def _protect_formula(match: re.Match) -> str:
        formulas.append(match.group(0))
        return f"__FORMULA_{len(formulas) - 1}__"

    # 保护 $$...$$ 块级公式和 $...$ 行内公式
    text = re.sub(r"\$\$[\s\S]*?\$\$", _protect_formula, text)
    text = re.sub(r"\$[\s\S]*?\$", _protect_formula, text)

    # 1. 去除常见的 LaTeX 环境标记（当环境中主要是普通文字时）
    text = re.sub(r"\\begin\{(align|equation|gather|multline)(\*)?\}\s*", "", text)
    text = re.sub(r"\\end\{(align|equation|gather|multline)(\*)?\}\s*", "", text)

    # 2. 去除表格对齐符 \\\\&、行首孤立的 & 和孤立的 \\\\ 换行符（保留自然换行）
    text = text.replace(r"\\&", "")
    text = re.sub(r"^\s*&\s*", "", text, flags=re.MULTILINE)
    text = text.replace("\\\\", "")

    # 3. 规范化空白：多个空格/制表符合并为一个空格
    text = re.sub(r"[ \t]+", " ", text)

    # 4. 规范化换行：去除行首行尾空白，合并多余空行
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # 5. 恢复被保护的公式
    for i, formula in enumerate(formulas):
        text = text.replace(f"__FORMULA_{i}__", formula)

    return text.strip()


class KnowledgeService:
    """知识库服务"""

    @staticmethod
    def _resolve_kb(kb_id: str, visibility: str, owner_id: str) -> tuple[str, str, str]:
        """解析上传目标知识库：返回 (kb_id, visibility, owner_id)

        可见性由所属知识库决定；未指定 kb_id 时归入默认公共知识库。
        """
        from app.services import kb as kb_service

        if not kb_id:
            kb_id = kb_service.DEFAULT_KB_ID
        kb_row = kb_service.get_kb(kb_id)
        if kb_row:
            return kb_row["id"], kb_row["visibility"], kb_row["owner_id"]
        # 库不存在时回退默认库，避免上传失败
        return kb_service.DEFAULT_KB_ID, visibility, owner_id

    @staticmethod
    def _hybrid_search(query: str, user_id: str, kb_ids: set, k: int = None) -> list:
        """FAISS 稠密 + BM25 稀疏召回，经 RRF 融合后返回候选。"""
        from app.services.config_manager import runtime_config

        mode = getattr(runtime_config, "retrieval_mode", "hybrid")
        top_k = k or settings.TOP_K_RETRIEVAL
        dense_k = int(getattr(runtime_config, "dense_top_k", top_k * 2) or top_k * 2)
        sparse_k = int(getattr(runtime_config, "sparse_top_k", top_k * 2) or top_k * 2)
        rrf_k = int(getattr(runtime_config, "rrf_k", 60) or 60)
        candidate_k = int(getattr(runtime_config, "rerank_top_k", top_k * 3) or top_k * 3)

        dense = vector_store.similarity_search(
            query, k=dense_k, current_user_id=user_id, kb_ids=kb_ids
        )
        if mode == "dense":
            for text, meta, score in dense:
                meta["retrieval_mode"] = "dense"
                meta["dense_score"] = score
            return dense[:candidate_k]

        sparse = sparse_retriever.search(
            query, k=sparse_k, current_user_id=user_id, kb_ids=kb_ids
        )
        merged: dict[str, dict] = {}

        def add(items: list, channel: str):
            for rank, (text, meta, score) in enumerate(items, start=1):
                key = vector_store.result_key(text, meta)
                item = merged.setdefault(key, {
                    "text": text,
                    "meta": dict(meta),
                    "rrf": 0.0,
                    "dense_score": None,
                    "sparse_score": None,
                })
                item["rrf"] += 1.0 / (rrf_k + rank)
                if channel == "dense":
                    item["dense_score"] = score
                else:
                    item["sparse_score"] = score

        add(dense, "dense")
        add(sparse, "sparse")
        ranked = sorted(merged.values(), key=lambda x: x["rrf"], reverse=True)[:candidate_k]
        output = []
        for item in ranked:
            meta = item["meta"]
            meta["retrieval_mode"] = "hybrid"
            meta["dense_score"] = item["dense_score"]
            meta["sparse_score"] = item["sparse_score"]
            meta["rrf_score"] = item["rrf"]
            output.append((item["text"], meta, item["rrf"]))
        return output

    @staticmethod
    def retrieve_for_user(query: str, user_id: str) -> list:
        """按主题知识库路由检索：

        - 可见库 ≤ 3 个：全量检索，结果直接进 reranker
        - 可见库 > 3 个：大范围检索后按 kb_id 聚合相关度，
          只保留前 3 个最相关知识库的结果进 reranker
        """
        from app.services import kb as kb_service

        accessible = kb_service.list_accessible_kbs(user_id)
        accessible_ids = {k["id"] for k in accessible}
        if not accessible_ids:
            return []

        top_k = settings.TOP_K_RETRIEVAL
        if len(accessible_ids) <= 3:
            return KnowledgeService._hybrid_search(query, user_id, accessible_ids, top_k)

        # 库数 > 3：先大范围稠密检索，按库聚合相关度挑前 3；选库后仍使用 hybrid 召回
        # 权限前置：大范围检索也只在用户可见库内进行
        broad = vector_store.search_broad(
            query, k=top_k * 10, current_user_id=user_id, kb_ids=accessible_ids
        )
        selected = kb_service.select_kbs_for_query(broad, accessible_ids, top_n=3)
        logger.info("检索路由: 可见库 %d 个，选中知识库 %s", len(accessible_ids), selected)
        if not selected:
            return []
        return KnowledgeService._hybrid_search(query, user_id, set(selected), top_k)

    @staticmethod
    async def process_image(
        image_path: str,
        ocr_provider: str = None,
        visibility: str = "public",
        owner_id: str = "",
        kb_id: str = "",
        override_text: str = "",
    ) -> dict:
        """处理单张图片: OCR + 向量化存储

        Args:
            image_path: 图片文件路径
            ocr_provider: OCR 提供商 ("local", "aliyun", "custom_api")
            visibility: 知识可见性（由所属知识库覆盖）
            owner_id: 所有者用户 ID（由所属知识库覆盖）
            kb_id: 目标知识库 ID，缺省归入默认公共知识库
            override_text: 用户已编辑/润色的文本；提供时直接使用，跳过重新 OCR

        Returns:
            {
                "ocr_text": str,          # OCR 识别的原始文本
                "chunk_count": int,       # 分割的文本块数
                "doc_ids": List[str],     # 向量库中的文档 ID
                "source_image": str,      # 原始图片路径
            }
        """
        kb_id, visibility, owner_id = KnowledgeService._resolve_kb(kb_id, visibility, owner_id)

        # 1. 获取文本：优先使用用户已编辑/润色的文本，否则重新 OCR
        if override_text and override_text.strip():
            ocr_text = clean_ocr_text(override_text)
        else:
            ocr = OCRFactory.create(ocr_provider)
            ocr_text = await ocr.recognize(image_path)
            ocr_text = clean_ocr_text(ocr_text)
        if not ocr_text.strip():
            return {
                "ocr_text": "",
                "chunk_count": 0,
                "doc_ids": [],
                "source_image": image_path,
            }

        # 2. 文本分割 + 3. 向量化存储（含 Embedding 调用，同步重计算移入线程池）
        import asyncio

        def _vectorize():
            chunks = vector_store.split_text(ocr_text, image_path, visibility, owner_id, kb_id=kb_id)
            if not chunks:
                return 0, []
            texts = [c[0] for c in chunks]
            metadatas = [c[1] for c in chunks]
            return len(chunks), vector_store.add_texts(texts, metadatas)

        chunk_count, doc_ids = await asyncio.to_thread(_vectorize)

        return {
            "ocr_text": ocr_text,
            "chunk_count": chunk_count,
            "doc_ids": doc_ids,
            "source_image": image_path,
        }

    @staticmethod
    def _extract_docx_text(file_path: str) -> str:
        """从 Word 文档中提取文本，并将表格转为 Markdown 表格。

        兼容非标准 .docx 结构，优先按段落/表格文档流输出 Markdown。
        """
        from docx.table import Table

        try:
            elements = KnowledgeService._extract_docx_elements(file_path)
        except Exception as err:
            logger.warning("Word 文档流解析失败，尝试兜底解析: %s", err)
            return KnowledgeService._extract_docx_text_fallback(file_path)

        parts: List[str] = []
        for elem_type, elem in elements:
            if elem_type == "p":
                text = elem.text.strip() if not isinstance(elem, str) else elem.strip()
                if text:
                    parts.append(text)
            elif elem_type == "t":
                rows = None
                if isinstance(elem, Table):
                    rows = [[cell.text.strip() for cell in row.cells] for row in elem.rows]
                elif isinstance(elem, list):
                    rows = elem
                elif isinstance(elem, str):
                    continue
                if rows:
                    md = KnowledgeService._table_to_markdown(rows)
                    if md:
                        parts.append("\n" + md + "\n")

        return "\n".join(parts)

    @staticmethod
    def _extract_docx_text_fallback(file_path: str) -> str:
        """通过 zipfile + XML 直接解析 docx 的 word/document.xml。

        兼容 Windows 环境生成的 docx（zip 条目使用反斜杠路径）。
        """
        import zipfile
        from xml.etree import ElementTree as ET

        NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

        with zipfile.ZipFile(file_path) as zf:
            namelist = zf.namelist()
            # Windows 生成的 docx 可能使用反斜杠作为 zip 路径分隔符
            doc_path = None
            for candidate in ("word/document.xml", "word\\document.xml"):
                if candidate in namelist:
                    doc_path = candidate
                    break
            if not doc_path:
                raise ValueError("找不到 word/document.xml，不是有效的 docx 文件")
            xml_content = zf.read(doc_path)

        root = ET.fromstring(xml_content)
        texts = []
        for paragraph in root.findall(".//w:p", NS):
            # 收集段落中所有 <w:t> 的文本，包括嵌套在 <w:r>/<w:instrText> 中的内容
            para_text = "".join(
                node.text or "" for node in paragraph.findall(".//w:t", NS)
            )
            if para_text.strip():
                texts.append(para_text.strip())

        return "\n".join(texts)

    @staticmethod
    def _extract_pdf_text(file_path: str) -> str:
        """从 PDF 文档中提取文本，同时将页面中的表格转为 Markdown 表格。"""
        pages = KnowledgeService._extract_pdf_pages(file_path)
        return "\n\n".join(p.markdown for p in pages if p.markdown.strip())

    @staticmethod
    def _table_to_markdown(rows: List[List[str]]) -> str:
        """将表格行数据转为 Markdown 表格。"""
        if not rows:
            return ""
        # 统一列数
        col_count = max(len(row) for row in rows)
        normalized = []
        for row in rows:
            # PyMuPDF 表格提取可能返回 None 单元格（空/合并单元格），需容错
            cells = [(c or "").replace("|", "\\|").replace("\n", " ").strip() for c in row]
            # 补齐空单元格
            cells += [""] * (col_count - len(cells))
            normalized.append(cells)
        lines = ["| " + " | ".join(row) + " |" for row in normalized]
        if len(normalized) > 1:
            lines.insert(1, "| " + " | ".join(["---"] * col_count) + " |")
        return "\n".join(lines)

    @staticmethod
    def _build_page_markdown(text: str, tables: List[str]) -> str:
        """将页面文本和表格拼接为 Markdown。"""
        parts = [text.strip()] if text.strip() else []
        for table_md in tables:
            if table_md.strip():
                parts.append("\n" + table_md.strip() + "\n")
        return "\n\n".join(parts)

    @staticmethod
    def _extract_text_excluding_tables(page, table_bboxes: List[tuple]) -> str:
        """提取页面文本，并剔除与表格区域重叠的文本块，避免 Markdown 表格与行内文本重复。"""
        if not table_bboxes:
            return page.get_text().strip()

        def _overlap_ratio(block_bbox, table_bbox):
            bx0, by0, bx1, by1 = block_bbox
            tx0, ty0, tx1, ty1 = table_bbox
            ix0 = max(bx0, tx0)
            iy0 = max(by0, ty0)
            ix1 = min(bx1, tx1)
            iy1 = min(by1, ty1)
            if ix1 <= ix0 or iy1 <= iy0:
                return 0.0
            inter = (ix1 - ix0) * (iy1 - iy0)
            block_area = (bx1 - bx0) * (by1 - by0) or 1
            return inter / block_area

        blocks = page.get_text("blocks")
        kept = []
        for b in blocks:
            if len(b) < 5:
                continue
            bbox = b[:4]
            txt = b[4]
            # 当文本块与任一表格区域重叠超过 50% 时视为表格内部文本，跳过
            if any(_overlap_ratio(bbox, tb) > 0.5 for tb in table_bboxes):
                continue
            if txt and txt.strip():
                kept.append(txt.strip())
        return "\n".join(kept)

    @staticmethod
    def _extract_pdf_pages(file_path: str) -> List[DocumentPage]:
        """按页解析 PDF 文档，同时提取表格为 Markdown。"""
        import pymupdf as fitz

        pages: List[DocumentPage] = []
        with fitz.open(file_path) as doc:
            for page_idx, page in enumerate(doc, start=1):
                tables_md: List[str] = []
                table_bboxes: List[tuple] = []
                try:
                    for table in page.find_tables():
                        rows = table.extract()
                        if rows:
                            md = KnowledgeService._table_to_markdown(rows)
                            if md:
                                tables_md.append(md)
                                if table.bbox:
                                    table_bboxes.append(table.bbox)
                except Exception as err:
                    logger.warning("PDF 第 %d 页表格识别失败: %s", page_idx, err)

                text = KnowledgeService._extract_text_excluding_tables(page, table_bboxes)
                markdown = KnowledgeService._build_page_markdown(text, tables_md)
                pages.append(
                    DocumentPage(
                        page_number=page_idx,
                        # text 直接使用包含 Markdown 表格的完整内容，便于前端编辑/保存
                        text=markdown,
                        tables=tables_md,
                        markdown=markdown,
                    )
                )
        return pages

    @staticmethod
    def _extract_docx_elements_from_standard(file_path: str) -> List[tuple]:
        """使用 python-docx 提取段落和表格的文档流。"""
        from docx import Document

        doc = Document(file_path)
        para_map = {p._element: p for p in doc.paragraphs}
        table_map = {t._element: t for t in doc.tables}

        elements: List[tuple] = []
        for child in doc.element.body:
            tag = child.tag
            if tag.endswith("p") and child in para_map:
                elements.append(("p", para_map[child]))
            elif tag.endswith("tbl") and child in table_map:
                elements.append(("t", table_map[child]))
        return elements

    @staticmethod
    def _extract_docx_elements_from_xml(file_path: str) -> List[tuple]:
        """XML 兜底：从 word/document.xml 直接提取段落和表格。"""
        import zipfile
        from xml.etree import ElementTree as ET

        NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

        with zipfile.ZipFile(file_path) as zf:
            namelist = zf.namelist()
            doc_path = None
            for candidate in ("word/document.xml", "word\\document.xml"):
                if candidate in namelist:
                    doc_path = candidate
                    break
            if not doc_path:
                raise ValueError("找不到 word/document.xml")
            xml_content = zf.read(doc_path)

        root = ET.fromstring(xml_content)
        body = root.find(".//w:body", NS) or root
        elements: List[tuple] = []
        for child in body:
            tag = child.tag.split("}")[-1]
            if tag == "p":
                text = "".join(node.text or "" for node in child.findall(".//w:t", NS))
                if text.strip():
                    elements.append(("p", text.strip()))
            elif tag == "tbl":
                rows = []
                for row in child.findall(".//w:tr", NS):
                    cells = []
                    for cell in row.findall(".//w:tc", NS):
                        cell_text = "".join(
                            node.text or "" for node in cell.findall(".//w:t", NS)
                        )
                        cells.append(cell_text.strip())
                    if any(cells):
                        rows.append(cells)
                if rows:
                    elements.append(("t", rows))
        return elements

    @staticmethod
    def _extract_docx_elements(file_path: str) -> List[tuple]:
        """提取 Word 文档的段落/表格文档流，优先标准方式，失败则 XML 兜底。"""
        try:
            return KnowledgeService._extract_docx_elements_from_standard(file_path)
        except Exception as err:
            logger.warning("python-docx 标准提取失败，使用 XML 兜底: %s", err)
            return KnowledgeService._extract_docx_elements_from_xml(file_path)

    @staticmethod
    def _split_docx_elements_into_pages(elements: List[tuple]) -> List[List[tuple]]:
        """按分页符或一级标题将文档流分组为逻辑页。"""
        NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        PB_TAG = f"{NS}br"
        LAST_PB_TAG = f"{NS}lastRenderedPageBreak"

        def _is_page_break_paragraph(elem) -> bool:
            # elem 可能是段落对象（标准方式）或字符串（XML 兜底）
            if isinstance(elem, str):
                return "\x0c" in elem
            # python-docx Paragraph
            p_elem = elem._element
            for br in p_elem.findall(f".//{PB_TAG}"):
                if br.get(f"{NS}type") == "page":
                    return True
            if p_elem.find(f".//{LAST_PB_TAG}") is not None:
                return True
            return False

        # 1. 尝试按分页符分组
        pages: List[List[tuple]] = []
        current: List[tuple] = []
        for elem_type, elem in elements:
            if elem_type == "p" and _is_page_break_paragraph(elem):
                # 结束当前页并开始新页
                if current:
                    pages.append(current)
                    current = []
                # 保留分页符段落中可能存在的文本（去掉分页符本身）
                text = elem.text.strip() if not isinstance(elem, str) else elem.strip()
                # 处理段落文本中间可能包含的硬分页符
                parts = [p.strip() for p in text.split("\x0c")]
                for i, part in enumerate(parts):
                    if part:
                        current.append(("p", part))
                    # 每个分页符都触发一次分页
                    if i < len(parts) - 1 and current:
                        pages.append(current)
                        current = []
            else:
                current.append((elem_type, elem))

        if current:
            pages.append(current)

        if len(pages) > 1:
            return pages

        # 2. 无分页符时，尝试按中文一级标题分组
        heading_pattern = re.compile(r"^[一二三四五六七八九十百千]+[、.．)）]\s*")
        new_pages: List[List[tuple]] = []
        current = []
        for elem in elements:
            elem_type, value = elem
            if elem_type == "p":
                text = value.text.strip() if not isinstance(value, str) else value.strip()
                if heading_pattern.match(text):
                    if current:
                        new_pages.append(current)
                        current = []
            current.append(elem)
        if current:
            new_pages.append(current)

        if len(new_pages) > 1:
            return new_pages

        # 3. 仍无分组，则整个文档作为一页
        if elements:
            return [elements]
        return []

    @staticmethod
    def _extract_docx_pages(file_path: str) -> List[DocumentPage]:
        """按逻辑页解析 Word 文档，同时提取表格为 Markdown。"""
        from docx.table import Table

        elements = KnowledgeService._extract_docx_elements(file_path)
        page_groups = KnowledgeService._split_docx_elements_into_pages(elements)

        # 表格对象映射（仅标准解析时可用）
        table_objects: dict = {}
        try:
            from docx import Document

            doc = Document(file_path)
            table_objects = {id(t): t for t in doc.tables}
        except Exception:
            pass

        pages: List[DocumentPage] = []
        for page_idx, group in enumerate(page_groups, start=1):
            texts: List[str] = []
            tables_md: List[str] = []
            for elem_type, elem in group:
                if elem_type == "p":
                    text = elem.text.strip() if not isinstance(elem, str) else elem.strip()
                    if text:
                        texts.append(text)
                elif elem_type == "t":
                    rows = None
                    if isinstance(elem, Table):
                        rows = [[cell.text.strip() for cell in row.cells] for row in elem.rows]
                    elif isinstance(elem, list):
                        rows = elem
                    elif isinstance(elem, str):
                        # 标准解析时 elem 是 Table 对象；兜底时 elem 是 rows
                        continue
                    if rows:
                        md = KnowledgeService._table_to_markdown(rows)
                        if md:
                            tables_md.append(md)

            text = "\n".join(texts)
            markdown = KnowledgeService._build_page_markdown(text, tables_md)
            pages.append(
                DocumentPage(
                    page_number=page_idx,
                    # text 直接使用包含 Markdown 表格的完整内容
                    text=markdown,
                    tables=tables_md,
                    markdown=markdown,
                )
            )

        return pages

    @staticmethod
    def _convert_word_to_pdf(file_path: str) -> Optional[str]:
        """使用 LibreOffice (soffice) 将 Word 原始文档转换为 PDF。

        转换后的 PDF 保留原文档排版，用于「原始文档分页预览」与页码跳转；
        不做解析文本渲染回退（呈现给用户的必须是原始文档内容）。
        转换结果与源文件同名缓存在同目录，重复调用直接命中。
        LibreOffice 不可用时返回 None，由调用方降级为其他预览方式。
        """
        import shutil
        import subprocess

        output_dir = Path(file_path).parent
        expected_pdf = output_dir / (Path(file_path).stem + ".pdf")

        # 缓存命中：已转换过直接复用
        if expected_pdf.exists():
            return str(expected_pdf)

        if not shutil.which("soffice"):
            logger.info("未找到 soffice (LibreOffice)，跳过 Word 转 PDF")
            return None

        try:
            env = dict(os.environ)
            env.setdefault("HOME", "/tmp")  # soffice 需要可写 HOME 存放用户配置
            subprocess.run(
                [
                    "soffice",
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(output_dir),
                    file_path,
                ],
                check=True,
                timeout=180,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            if expected_pdf.exists():
                logger.info("Word 已转换为原始排版 PDF: %s", expected_pdf.name)
                return str(expected_pdf)
        except Exception as err:
            logger.warning("LibreOffice 转 PDF 失败: %s", err)

        return None

    @staticmethod
    async def process_document(
        file_path: str,
        doc_type: str,
        visibility: str = "public",
        owner_id: str = "",
        kb_id: str = "",
    ) -> dict:
        """处理文档：提取文本 → 分割 → 向量化存储

        Args:
            file_path: 文档文件路径
            doc_type: 文档类型 ("word" | "pdf")
            visibility: 知识可见性 ("public", "private")
            owner_id: 所有者用户 ID

        Returns:
            {
                "text": str,              # 提取的原始文本
                "chunk_count": int,       # 分割的文本块数
                "doc_ids": List[str],     # 向量库中的文档 ID
                "source_path": str,       # 原始文档路径
            }
        """
        import asyncio

        kb_id, visibility, owner_id = KnowledgeService._resolve_kb(kb_id, visibility, owner_id)

        def _extract():
            if doc_type == "word":
                return KnowledgeService._extract_docx_text(file_path)
            if doc_type == "pdf":
                return KnowledgeService._extract_pdf_text(file_path)
            raise ValueError(f"不支持的文档类型: {doc_type}")

        text = await asyncio.to_thread(_extract)
        if not text.strip():
            return {
                "text": text,
                "chunk_count": 0,
                "doc_ids": [],
                "source_path": file_path,
            }

        # 分割 + 向量化（含 Embedding 调用）整体移入线程池，避免阻塞事件循环
        def _vectorize():
            chunks = vector_store.split_text(text, file_path, visibility, owner_id, kb_id=kb_id)
            if not chunks:
                return 0, []
            texts = [c[0] for c in chunks]
            metadatas = [c[1] for c in chunks]
            return len(chunks), vector_store.add_texts(texts, metadatas)

        chunk_count, doc_ids = await asyncio.to_thread(_vectorize)

        return {
            "text": text,
            "chunk_count": chunk_count,
            "doc_ids": doc_ids,
            "source_path": file_path,
        }

    @staticmethod
    async def preview_document(file_path: str, doc_type: str) -> dict:
        """预览文档，按页返回内容，不入库。"""
        import asyncio

        if doc_type == "word":
            def _parse_word():
                # 优先用 LibreOffice 转为原始排版 PDF 后按 PDF 分页，
                # 保证页码与用户预览的原始文档完全一致；
                # 无 LibreOffice 时降级为 docx 逻辑分页（前端 mammoth 渲染）
                pdf_path = KnowledgeService._convert_word_to_pdf(file_path)
                if pdf_path:
                    return pdf_path, KnowledgeService._extract_pdf_pages(pdf_path)
                return None, KnowledgeService._extract_docx_pages(file_path)

            pdf_preview_path, pages = await asyncio.to_thread(_parse_word)
        elif doc_type == "pdf":
            pdf_preview_path = file_path
            pages = await asyncio.to_thread(
                KnowledgeService._extract_pdf_pages, file_path
            )
        else:
            raise ValueError(f"不支持的文档类型: {doc_type}")

        return {
            "filename": Path(file_path).name,
            "file_path": file_path,
            "doc_type": doc_type,
            "total_pages": len(pages),
            "pages": [p.model_dump() for p in pages],
            "pdf_preview_path": pdf_preview_path,
        }

    @staticmethod
    async def save_document_pages(
        file_path: str,
        doc_type: str,
        pages: List[dict],
        visibility: str = "public",
        owner_id: str = "",
        kb_id: str = "",
        title: str = "",
    ) -> dict:
        """保存用户确认的页面到知识库（非流式，内部复用流式实现）。"""
        last: dict = {}
        async for ev in KnowledgeService.save_document_pages_stream(
            file_path=file_path,
            doc_type=doc_type,
            pages=pages,
            visibility=visibility,
            owner_id=owner_id,
            kb_id=kb_id,
            title=title,
        ):
            last = ev

        return {
            "text": last.get("text", ""),
            "chunk_count": last.get("chunk_count", 0),
            "doc_ids": last.get("doc_ids", []),
            "source_path": file_path,
            "pdf_preview_path": last.get("pdf_preview_path"),
            "pages": last.get("pages", []),
        }

    @staticmethod
    async def save_document_pages_stream(
        file_path: str,
        doc_type: str,
        pages: List[dict],
        visibility: str = "public",
        owner_id: str = "",
        kb_id: str = "",
        title: str = "",
    ):
        """逐页保存文档页面到知识库，以异步生成器流式产出进度事件（供 SSE 实时进度）。

        事件结构：
          save_start  {total, pdf_preview_path}
          page_saved  {page_number, chunk_count, saved, total}
          page_error  {page_number, error}
          save_done   {success, text, chunk_count, doc_ids, pages, pdf_preview_path}
        """
        import asyncio
        from pathlib import Path

        save_title = title.strip() or Path(file_path).stem
        kb_id, visibility, owner_id = KnowledgeService._resolve_kb(kb_id, visibility, owner_id)

        # Word 类型：尝试 LibreOffice 转原始排版 PDF，存入元数据供原始文档分页预览/页码跳转；
        # PDF 类型：pdf_preview_path 即文件本身
        pdf_preview_path = None
        if doc_type == "pdf":
            pdf_preview_path = file_path
        elif doc_type == "word":
            pdf_preview_path = await asyncio.to_thread(
                KnowledgeService._convert_word_to_pdf, file_path
            )

        total = len(pages)
        yield {"event": "save_start", "total": total, "pdf_preview_path": pdf_preview_path}

        all_doc_ids: List[str] = []
        saved_pages: List[dict] = []

        for page in pages:
            text = page.get("text", "").strip()
            page_number = page.get("page_number", 0)
            if not text or not page_number:
                yield {"event": "page_error", "page_number": page_number, "error": "页面内容为空"}
                continue

            try:
                def _save_one():
                    chunks = vector_store.split_text(
                        text,
                        file_path,
                        visibility,
                        owner_id,
                        page_number=page_number,
                        pdf_preview_path=pdf_preview_path,
                        source_type=doc_type,
                        kb_id=kb_id,
                    )
                    if not chunks:
                        return []
                    return vector_store.add_texts(
                        [c[0] for c in chunks], [c[1] for c in chunks]
                    )

                doc_ids = await asyncio.to_thread(_save_one)
            except Exception as e:
                logger.warning("保存第 %s 页失败: %s", page_number, e)
                yield {"event": "page_error", "page_number": page_number, "error": str(e)}
                continue

            all_doc_ids.extend(doc_ids)
            saved_pages.append({"page_number": page_number, "text": text})
            yield {
                "event": "page_saved",
                "page_number": page_number,
                "chunk_count": len(doc_ids),
                "saved": len(saved_pages),
                "total": total,
            }

        # 全部页处理完毕后统一创建一条上传记录
        record_text = "\n\n".join(p["text"] for p in saved_pages)
        if saved_pages:
            upload_records.add_record(
                filename=Path(file_path).name,
                image_path=file_path,
                ocr_text=record_text,
                ocr_provider=f"document:{doc_type}",
                chunk_count=len(all_doc_ids),
                doc_ids=all_doc_ids,
                visibility=visibility,
                owner_id=owner_id,
                source_type=doc_type,
                pages=saved_pages,
                pdf_preview_path=pdf_preview_path,
                title=save_title,
            )
            from app.services.usage import record_upload
            record_upload(owner_id)

            # llm-wiki：后台把该文档编译成结构化百科卡片（不阻塞 save_done）
            from app.services.wiki import wiki_service
            wiki_service.schedule_compile(
                source_image=file_path,
                full_text=record_text,
                kb_id=kb_id,
                visibility=visibility,
                owner_id=owner_id,
                source_type=doc_type,
                title=save_title,
                pdf_preview_path=pdf_preview_path,
            )

        yield {
            "event": "save_done",
            "success": len(saved_pages) > 0,
            "text": record_text,
            "chunk_count": len(all_doc_ids),
            "doc_ids": all_doc_ids,
            "pages": saved_pages,
            "pdf_preview_path": pdf_preview_path,
            "title": save_title,
        }

    @staticmethod
    async def preview_ocr(image_path: str, ocr_provider: str = None) -> str:
        """仅预览 OCR 识别结果（不存入向量库）"""
        ocr = OCRFactory.create(ocr_provider)
        text = await ocr.recognize(image_path)
        return clean_ocr_text(text)

    @staticmethod
    def format_knowledge_context(
        query: str,
        results: list,
    ) -> tuple[str, list[dict]]:
        """将检索结果格式化为 LLM 上下文，使用 rerank 重排序

        Args:
            query: 用户查询（用于 rerank）
            results: [(text, metadata, score), ...]

        Returns:
            (context_text, sources)
            context_text: 拼接后的上下文文本
            sources: 来源列表 [{text, source_image, score, relevance}]
        """
        # 1. Rerank 重排序
        reranked = rerank(query, results)

        # 1.5 相似度过滤：丢弃相关度低于 MIN_RELEVANCE_PCT 的片段，
        # 后续来源编号按过滤后的顺序重新生成
        filtered = [
            (text, metadata, score)
            for text, metadata, score in reranked
            if normalize_score(score) >= MIN_RELEVANCE_PCT
        ]
        if len(filtered) < len(reranked):
            logger.info(
                "相似度过滤: %d → %d 条（阈值 %d%%）",
                len(reranked), len(filtered), MIN_RELEVANCE_PCT,
            )

        # 1.6 llm-wiki 知识优先：预编译百科卡片排序前置（稳定排序，不改变同组内相对顺序），
        # 让 LLM 优先读到结构化、信息密度更高的百科知识，原始 chunk 作兜底
        from app.services.wiki import wiki_priority_enabled
        wiki_hits = [tm for tm in filtered if tm[1].get("is_wiki_page")]
        if wiki_priority_enabled() and wiki_hits:
            filtered.sort(key=lambda tm: 0 if tm[1].get("is_wiki_page") else 1)
            titles = sorted({tm[1].get("wiki_title") or "(未名词条)" for tm in wiki_hits})
            logger.info(
                "llm-wiki 检索优先: 百科卡片前置 %d 条 | 命中标题: %s",
                len(wiki_hits), "、".join(titles),
            )
        elif wiki_priority_enabled():
            logger.info("llm-wiki 检索优先: 本次检索未命中百科卡片，全部为原始知识片段")

        # kb_id → 库名映射（来源信息展示用）
        from app.core import db as sqlite_db
        kb_names = {
            r["id"]: r["name"]
            for r in sqlite_db.query("SELECT id, name FROM knowledge_bases")
        }

        context_parts = []
        sources = []
        seen_images = set()

        for i, (text, metadata, score) in enumerate(filtered):
            # 相关性分数归一化为百分比
            relevance_pct = normalize_score(score)

            # 上下文
            context_parts.append(f"[来源 {i + 1}]:\n{text}\n")

            # 来源信息
            source_image = metadata.get("source_image", "")
            kb_id_val = metadata.get("kb_id", "")
            source_info = {
                "index": i + 1,
                "text": text[:200] + "..." if len(text) > 200 else text,
                "source_image": source_image,
                "score": round(score, 4),
                "relevance": relevance_pct,
                "page_number": metadata.get("page_number"),
                "pdf_preview_path": metadata.get("pdf_preview_path"),
                "source_type": metadata.get("source_type", "image"),
                "kb_id": kb_id_val,
                "kb_name": kb_names.get(kb_id_val, ""),
                "retrieval_mode": metadata.get("retrieval_mode"),
                "is_wiki_page": bool(metadata.get("is_wiki_page")),
                "wiki_title": metadata.get("wiki_title", ""),
                "dense_score": metadata.get("dense_score"),
                "sparse_score": metadata.get("sparse_score"),
                "rrf_score": metadata.get("rrf_score"),
            }
            sources.append(source_info)

            if source_image:
                seen_images.add(source_image)

        context_text = "\n---\n".join(context_parts)
        return context_text, sources

    @staticmethod
    def build_knowledge_prompt(query: str, context_text: str, use_knowledge: bool) -> str:
        """构建检索增强的提问 prompt

        Args:
            query: 用户问题
            context_text: 知识库检索到的上下文
            use_knowledge: 是否启用知识检索

        Returns:
            构造后的 prompt
        """
        if not use_knowledge:
            return (
                f"用户的问题如下：\n{query}\n\n"
                f"请基于你自身的知识回答用户的问题。"
            )

        if not context_text.strip():
            return (
                f"用户的问题如下：\n{query}\n\n"
                f"注意：已在知识库中检索，但未找到相关内容。"
                f"请委婉告知用户知识库中暂未收录该内容。"
                f"不要自己编造答案。"
            )

        return (
            f"请基于以下知识库内容回答用户的问题。\n\n"
            f"【知识库内容】\n{context_text}\n\n"
            f"【用户问题】\n{query}\n\n"
            f"要求：\n"
            f"1. 优先使用知识库内容作答\n"
            f"2. 回答中必须引用原文，标注来源编号 [来源 X]\n"
            f"3. 如果知识库内容不足以回答问题，请明确指出知识库中未覆盖的部分\n"
            f"4. 不要编造知识库中没有的内容"
        )

    @staticmethod
    def build_search_context(results: list[SearchResult]) -> str:
        """构建联网搜索结果上下文"""
        if not results:
            return ""

        parts = []
        for i, r in enumerate(results):
            parts.append(f"[网络来源 {i + 1}]: {r.title}\n链接: {r.url}\n摘要: {r.snippet}\n")

        return "\n---\n".join(parts)


knowledge_service = KnowledgeService()
