"""
见字如面 - 知识库内容摘要构建
为思维导图生成与 RAG 评估用例生成提供统一的 KB 内容上下文
（llm-wiki 百科论点 + 原始文档标题/正文预览）。
"""
import json

from app.core import db
from app.core.vector_store import vector_store


def build_kb_context(kb_id: str, max_chars: int = 8000) -> str:
    parts = []

    wiki_rows = db.query(
        "SELECT page_type, title, analysis FROM wiki_pages WHERE kb_id = ?"
        " ORDER BY updated_at DESC LIMIT 30",
        (kb_id,),
    )
    for row in wiki_rows:
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
    if sources:
        from app.services.records import upload_records
        all_records, _ = upload_records.list_records(limit=10000, offset=0)
        for rec in all_records:
            if rec["image_path"] not in sources:
                continue
            title = (rec.get("title") or "").strip() or rec.get("filename", "")
            preview = (rec.get("ocr_text") or "")[:400].replace("\n", " ")
            parts.append(f"[文档] {title}｜{preview}")

    text = "\n".join(parts)
    return text[:max_chars]