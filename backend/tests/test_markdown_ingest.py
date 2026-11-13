"""Markdown 章节感知解析/分块/上传链路回归测试

覆盖：标题层级拆解、整模块保留、概述节、代码围栏保护、超长节二次切分、
编码兼容、process_document/preview_document/上传流水线/入库分支（内存 DB + mock 向量化）。
"""
import asyncio
import json
import sqlite3

import pytest
from fastapi import HTTPException

from app.config import settings
from app.core import db
from app.core.vector_store import vector_store
from app.services.knowledge import (
    KnowledgeService,
    knowledge_service,
    read_markdown_text,
    split_markdown_sections,
)


@pytest.fixture()
def mem_db(monkeypatch):
    """独立内存数据库：避免污染本地开发库"""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    yield conn
    conn.close()


SAMPLE_MD = """# 第一章 绪论

这是第一章的引言。

## 1.1 背景

背景内容。

## 1.2 目标

目标内容。

# 第二章 方法

方法总述。
"""


# ---------- split_markdown_sections：章节拆解 ----------

def test_heading_path_hierarchy():
    sections = split_markdown_sections(SAMPLE_MD)
    titles = [s["title"] for s in sections]
    # 第一章含两个子节且总量小于整块上限 → 整模块一块；第二章独立一块
    assert titles == ["第一章 绪论", "第二章 方法"]
    assert "1.1 背景" in sections[0]["text"]
    assert "1.2 目标" in sections[0]["text"]


def test_long_parent_drills_into_children():
    text = "# 父章\n\n父章引言。\n\n## 子节A\n\n" + "甲" * 600 + "\n\n## 子节B\n\n" + "乙" * 600
    sections = split_markdown_sections(text, max_chars=500)
    titles = [s["title"] for s in sections]
    assert titles == ["父章", "父章 > 子节A", "父章 > 子节B"]
    assert sections[0]["text"] == "# 父章\n\n父章引言。"


def test_preamble_becomes_overview():
    sections = split_markdown_sections("开头说明文字。\n\n# 标题\n\n正文")
    assert sections[0]["title"] == "概述"
    assert sections[0]["text"] == "开头说明文字。"
    assert sections[1]["title"] == "标题"


def test_code_fence_not_treated_as_heading():
    text = "# 真标题\n\n```python\n# 这不是标题\nprint(1)\n```\n\n收尾正文。"
    sections = split_markdown_sections(text)
    assert len(sections) == 1
    assert "# 这不是标题" in sections[0]["text"]


def test_long_leaf_kept_whole_for_secondary_split():
    long_leaf = "# 长节\n\n" + "内容" * 800
    sections = split_markdown_sections(long_leaf, max_chars=500)
    assert len(sections) == 1  # 叶子超长仍整节保留，由调用方节内二次切分
    assert len(sections[0]["text"]) > 500


def test_empty_text_returns_no_sections():
    assert split_markdown_sections("") == []
    assert split_markdown_sections("   \n\n  ") == []


# ---------- 编码兼容 ----------

def test_read_markdown_encodings(tmp_path):
    content = "# 标题\n\n中文内容 café"
    p_utf8 = tmp_path / "a.md"
    p_utf8.write_bytes(content.encode("utf-8"))
    assert read_markdown_text(str(p_utf8)) == content

    p_bom = tmp_path / "b.md"
    p_bom.write_bytes(content.encode("utf-8-sig"))
    assert read_markdown_text(str(p_bom)) == content

    p_gb = tmp_path / "c.md"
    p_gb.write_bytes(content.encode("gb18030"))
    assert read_markdown_text(str(p_gb)) == content


# ---------- 章节页 / 分块 ----------

def test_extract_markdown_pages(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text(SAMPLE_MD, encoding="utf-8")
    pages = KnowledgeService._extract_markdown_pages(str(p))
    assert [pg["page_number"] for pg in pages] == [1, 2]
    assert pages[0]["section_title"] == "第一章 绪论"
    assert pages[0]["markdown"] == pages[0]["text"]
    assert pages[0]["tables"] == []


def test_markdown_chunks_short_section_single_block(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text(SAMPLE_MD, encoding="utf-8")
    pages = KnowledgeService._extract_markdown_pages(str(p))
    chunks = KnowledgeService._markdown_chunks(pages, str(p), "public", "u1", "kb1")
    assert len(chunks) == 2  # 两个短章节各一整块
    text, meta = chunks[0]
    assert "1.1 背景" in text
    assert meta["source_type"] == "markdown"
    assert meta["section_title"] == "第一章 绪论"
    assert meta["page_number"] == 1
    assert meta["kb_id"] == "kb1"
    assert meta["visibility"] == "public"
    assert meta["owner_id"] == "u1"
    assert meta["source_image"] == str(p)


def test_markdown_chunks_long_section_prefixed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CHUNK_SIZE", 100)
    monkeypatch.setattr(settings, "CHUNK_OVERLAP", 10)
    p = tmp_path / "long.md"
    p.write_text("# 长章节\n\n" + "知识点。" * 200, encoding="utf-8")
    pages = KnowledgeService._extract_markdown_pages(str(p))
    chunks = KnowledgeService._markdown_chunks(pages, str(p), "private", "u2", "kb2")
    assert len(chunks) > 1
    for text, meta in chunks:
        assert text.startswith("【长章节】")  # 子块携带标题路径，章节上下文不丢失
        assert meta["source_type"] == "markdown"
        assert meta["section_title"] == "长章节"


# ---------- process_document / preview_document ----------

def test_process_document_markdown(mem_db, tmp_path, monkeypatch):
    p = tmp_path / "doc.md"
    p.write_text(SAMPLE_MD, encoding="utf-8")
    captured = {}

    def fake_add_texts(texts, metadatas):
        captured["texts"] = texts
        captured["metadatas"] = metadatas
        return [f"id-{i}" for i in range(len(texts))]

    monkeypatch.setattr(vector_store, "add_texts", fake_add_texts)
    result = asyncio.run(knowledge_service.process_document(
        file_path=str(p), doc_type="markdown", visibility="public", owner_id="u1",
    ))
    assert result["chunk_count"] == 2
    assert result["doc_ids"] == ["id-0", "id-1"]
    assert all(m["source_type"] == "markdown" for m in captured["metadatas"])


def test_preview_document_markdown(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text(SAMPLE_MD, encoding="utf-8")
    result = asyncio.run(knowledge_service.preview_document(str(p), "markdown"))
    assert result["doc_type"] == "markdown"
    assert result["total_pages"] == 2
    assert result["pdf_preview_path"] is None
    assert result["pages"][0]["section_title"] == "第一章 绪论"


# ---------- API 类型识别 ----------

def test_detect_source_type_markdown():
    from app.api.knowledge import _detect_source_type

    assert _detect_source_type("笔记.md") == "markdown"
    assert _detect_source_type("笔记.MARKDOWN") == "markdown"
    assert _detect_source_type("文档.docx") == "word"
    assert _detect_source_type("文档.pdf") == "pdf"
    with pytest.raises(HTTPException):
        _detect_source_type("程序.exe")


def test_detect_kind_chat_attachment():
    from app.services.tool_orchestrator import detect_kind

    assert detect_kind("a.md") == "markdown"
    assert detect_kind("a.markdown") == "markdown"
    assert detect_kind("a.txt") is None


# ---------- 上传流水线：解析 + 入库 ----------

def test_pipeline_parse_markdown(tmp_path):
    from app.services.upload_pipeline import upload_pipeline

    p = tmp_path / "doc.md"
    p.write_text(SAMPLE_MD, encoding="utf-8")
    text, pages, pdf_preview = asyncio.run(upload_pipeline._parse_markdown(str(p)))
    assert pdf_preview is None
    assert len(pages) == 2
    assert "第一章" in text and "第二章" in text

    empty = tmp_path / "empty.md"
    empty.write_text("   \n", encoding="utf-8")
    with pytest.raises(ValueError):
        asyncio.run(upload_pipeline._parse_markdown(str(empty)))


def test_pipeline_ingest_markdown_sections(mem_db, tmp_path, monkeypatch):
    from app.services import ingest_dedup, usage
    from app.services.records import upload_records
    from app.services.upload_pipeline import upload_pipeline
    from app.services.wiki import wiki_service

    p = tmp_path / "doc.md"
    p.write_text(SAMPLE_MD, encoding="utf-8")

    monkeypatch.setattr(ingest_dedup, "compute_file_fingerprint", lambda path: "fp")
    monkeypatch.setattr(ingest_dedup, "ensure_can_ingest", lambda *a, **k: None)
    monkeypatch.setattr(ingest_dedup, "register", lambda *a, **k: None)
    monkeypatch.setattr(usage, "record_upload", lambda uid: None)
    monkeypatch.setattr(wiki_service, "schedule_compile", lambda **kw: None)
    monkeypatch.setattr(
        upload_records, "add_record",
        lambda **kw: {"id": "rec-1", **kw},
    )
    captured = {}

    def fake_add_texts(texts, metadatas):
        captured["texts"] = texts
        captured["metadatas"] = metadatas
        return [f"id-{i}" for i in range(len(texts))]

    monkeypatch.setattr(vector_store, "add_texts", fake_add_texts)

    item = upload_pipeline.create("doc.md", "markdown", str(p), "u1")
    pages = KnowledgeService._extract_markdown_pages(str(p))
    parsed_text = "\n\n".join(pg["text"] for pg in pages)
    db.execute(
        "UPDATE pending_uploads SET status='done', parsed_text=?, pages=? WHERE id=?",
        (parsed_text, json.dumps(pages, ensure_ascii=False), item["id"]),
    )

    result = asyncio.run(upload_pipeline.ingest(item["id"]))
    assert result["success"] is True
    assert result["source_type"] == "markdown"
    assert result["chunk_count"] == 2  # 两个章节各一整块，而非按字符切碎
    assert all(m["source_type"] == "markdown" for m in captured["metadatas"])
    assert captured["metadatas"][0]["section_title"] == "第一章 绪论"
    # 入库成功后流水线记录移除
    assert upload_pipeline.get(item["id"]) is None