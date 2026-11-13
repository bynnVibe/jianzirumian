"""
见字如面 - 知识库内容摘要构建
为思维导图生成与 RAG 评估用例生成提供统一的 KB 内容上下文
（llm-wiki 百科论点 + 原始文档标题/正文预览）。
"""
import json

from app.core import db
from app.core.vector_store import vector_store


def _row_in_doc(row, doc_path: str) -> bool:
    """wiki 页是否归属指定文档：source 卡片直 hit，或 digest 跨素材引用包含该文档。"""
    if row["source_image"] == doc_path:
        return True
    try:
        refs = json.loads(row["source_refs"] or "[]")
    except (json.JSONDecodeError, ValueError):
        refs = []
    return doc_path in refs


def build_kb_context(kb_id: str, max_chars: int = 8000, doc_path: str = "") -> str:
    parts = []

    wiki_rows = db.query(
        "SELECT page_type, title, analysis, source_image, source_refs FROM wiki_pages WHERE kb_id = ?"
        " ORDER BY updated_at DESC LIMIT 30",
        (kb_id,),
    )
    for row in wiki_rows:
        if doc_path and not _row_in_doc(row, doc_path):
            continue
        try:
            analysis = json.loads(row["analysis"] or "{}")
        except (json.JSONDecodeError, ValueError):
            analysis = {}
        claims = analysis.get("claims") or []
        keywords = (analysis.get("structure") or {}).get("keywords") or []
        line = f"[百科] {row['title']}"
        if keywords:
            line += "｜关键词: " + "、".join(str(k) for k in keywords[:8])
        if claims:
            line += "｜要点: " + "；".join(str(c) for c in claims[:6])
        parts.append(line)

    sources = {
        meta.get("source_image", "")
        for meta in vector_store._load_metadata().values()
        if meta.get("kb_id") == kb_id and not meta.get("is_wiki_page")
    }
    if doc_path:
        sources = sources & {doc_path}
    if sources:
        from app.services.records import upload_records
        all_records, _ = upload_records.list_records(limit=10000, offset=0)
        for rec in all_records:
            if rec["image_path"] not in sources:
                continue
            title = (rec.get("title") or "").strip() or rec.get("filename", "")
            # 单文档范围：给足该文档自身正文预算（整库时仅 400 字预览）
            preview_len = max_chars if doc_path else 400
            preview = (rec.get("ocr_text") or "")[:preview_len].replace("\n", " ")
            parts.append(f"[文档] {title}｜{preview}")

    text = "\n".join(parts)
    return text[:max_chars]