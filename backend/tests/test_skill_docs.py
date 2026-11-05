"""Skill 文档库（渐进式/懒加载）回归测试（读 backend/skills 落盘文档，不出网）。

覆盖：
  - index_docs 发现层：扫描到三个内置技能文档，含标题/简述/kind/scope/builtin_key；
  - load_doc 激活层：frontmatter 解析 + 主体 + references 列表（不含正文）；
  - load_reference 执行层：按 path 拉取单篇；目录逃逸/非 .md/越权 slug 一律拒绝；
  - resolve_builtin_slug：内置技能行 id 反查 doc_slug；
  - synthesize_doc：自定义技能即时合成，无 references；
  - doc_for_skill：内置读落盘、自定义走合成。
"""
import sqlite3

import pytest

from app.core import db
from app.services import skill_docs as sd
from app.services.skill import SkillService, BUILTIN_SKILLS


@pytest.fixture()
def skill_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    yield conn
    conn.close()


# ============================================
# 发现层 / 激活层 / 执行层
# ============================================

def test_index_docs_discovers_builtin_skills():
    docs = {d["slug"]: d for d in sd.index_docs()}
    for seed in BUILTIN_SKILLS:
        slug = seed["doc_slug"]
        assert slug in docs, f"缺少技能文档目录：{slug}"
        assert docs[slug]["builtin_key"] == seed["builtin_key"]
        assert docs[slug]["title"]
        assert docs[slug]["description"]


def test_load_doc_parses_frontmatter_body_and_references():
    doc = sd.load_doc("web-content-fetch-fallback")
    assert doc is not None
    assert doc["name"] == "web-content-fetch-fallback"
    assert doc["title"] == "网页链接抓取回退策略"
    assert doc["kind"] == "playbook" and doc["scope"] == "url_import"
    assert doc["source"] == "file"
    # 主体不含 frontmatter，且保留了正文标题
    assert not doc["body"].lstrip().startswith("---")
    assert "网页链接抓取回退策略" in doc["body"]
    # references 只回路径/标题，不含正文
    paths = {r["path"] for r in doc["references"]}
    assert "references/site-routing.md" in paths
    assert "references/fallback-chain.md" in paths
    assert all("content" not in r for r in doc["references"])


def test_load_reference_returns_single_doc():
    ref = sd.load_reference("article-content-clean", "references/noise-rules.md")
    assert ref is not None
    assert ref["path"] == "references/noise-rules.md"
    assert ref["content"].strip()
    assert ref["title"]


def test_load_reference_blocks_traversal_and_non_md():
    assert sd.load_reference("web-content-fetch-fallback", "../../config.py") is None
    assert sd.load_reference("web-content-fetch-fallback", "references/../../config.py") is None
    assert sd.load_reference("web-content-fetch-fallback", "SKILL.md") is None
    assert sd.load_reference("web-content-fetch-fallback", "references/missing.md") is None


def test_skill_dir_rejects_bad_slug():
    assert sd._skill_dir("../app") is None
    assert sd._skill_dir("nope-not-exist") is None
    assert sd._skill_dir("") is None


# ============================================
# 内置反查 / 自定义合成
# ============================================

def test_resolve_builtin_slug_by_row_id(skill_db):
    svc = SkillService()
    svc.ensure_builtin("u1")
    row = next(s for s in svc.list_skills("u1") if s["name"] == "网页链接抓取回退策略")
    assert sd.resolve_builtin_slug("u1", row["id"]) == "web-content-fetch-fallback"
    # 非内置 id 反查为空
    assert sd.resolve_builtin_slug("u1", "not-a-builtin-id") is None


def test_list_skills_exposes_doc_slug_for_builtin_only(skill_db):
    svc = SkillService()
    custom = svc.create_skill("u1", "自建", "prompt", "内容", "", True)["skill"]
    rows = {s["id"]: s for s in svc.list_skills("u1")}
    # 内置技能带 doc_slug
    builtin_slugs = {s["doc_slug"] for s in rows.values() if s["builtin"]}
    assert builtin_slugs == {seed["doc_slug"] for seed in BUILTIN_SKILLS}
    # 自定义技能 doc_slug 为 None
    assert rows[custom["id"]]["doc_slug"] is None


def test_doc_for_skill_builtin_reads_file(skill_db):
    svc = SkillService()
    svc.ensure_builtin("u1")
    row = next(s for s in svc.list_skills("u1") if s["name"] == "链接正文清洗策略")
    doc = sd.doc_for_skill("u1", row)
    assert doc["source"] == "file"
    assert doc["slug"] == "article-content-clean"
    assert doc["references"]


def test_synthesize_doc_for_custom_skill():
    doc = sd.synthesize_doc({
        "name": "我的策略", "description": "一句话简述",
        "kind": "playbook", "scope": "url_import", "content": "[]",
    })
    assert doc["source"] == "synthesized"
    assert doc["references"] == []
    assert "我的策略" in doc["body"]
    assert "一句话简述" in doc["body"]
    # playbook 内容以 json 代码块呈现
    assert "```json" in doc["body"]


def test_doc_for_skill_custom_synthesizes(skill_db):
    svc = SkillService()
    custom = svc.create_skill("u1", "自建提示", "prompt", "先给结论再展开", "", True)["skill"]
    doc = sd.doc_for_skill("u1", custom)
    assert doc["source"] == "synthesized"
    assert "先给结论再展开" in doc["body"]
