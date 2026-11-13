"""后台入库（非阻塞）与三视图单文档范围回归测试。

覆盖：
一、后台入库：schedule_ingest 提交即返回、后台完成删行、失败 error+phase=ingest、
    重试沿用目标库/标题、重复指纹同步拦截、recover 恢复 ingesting 行；
二、单文档范围：_row_in_doc 归属判定、思维导图 scope 存储隔离与提示词、
    图谱 doc_graph 子图过滤（不含手动节点）、百科 list_pages doc_path 过滤。
全部用例仅操作隔离内存 SQLite 与 mock，不触达网络/向量库/LLM。
"""
import asyncio
import json
import sqlite3
import uuid

import pytest

from app.core import db
from app.core.vector_store import vector_store


@pytest.fixture()
def mem_db(monkeypatch):
    """独立内存数据库：避免污染本地开发库"""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    yield conn
    conn.close()


def _uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _insert_wiki_page(kb_id, title, *, source_image="", source_refs=None,
                      analysis=None, page_type="source", visibility="public"):
    pid = _uid("wp")
    now = "2026-11-13T10:00:00"
    db.execute(
        "INSERT INTO wiki_pages (id, page_type, title, content, kb_id, visibility,"
        " owner_id, source_image, source_refs, analysis, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, page_type, title, f"{title}正文", kb_id, visibility, "",
         source_image, json.dumps(source_refs or [], ensure_ascii=False),
         json.dumps(analysis or {}, ensure_ascii=False), now, now),
    )
    return pid


# ============================================
# 一、后台入库：提交即返回，不阻塞主线程
# ============================================

def _mock_ingest_deps(monkeypatch):
    """屏蔽指纹/用量/百科编译/登记/向量化等重活，仅验证调度状态机。"""
    from app.services import ingest_dedup, usage
    from app.services.records import upload_records
    from app.services.wiki import wiki_service

    monkeypatch.setattr(ingest_dedup, "compute_file_fingerprint", lambda path: "fp")
    monkeypatch.setattr(ingest_dedup, "ensure_can_ingest", lambda *a, **k: None)
    monkeypatch.setattr(ingest_dedup, "register", lambda *a, **k: None)
    monkeypatch.setattr(usage, "record_upload", lambda uid: None)
    monkeypatch.setattr(wiki_service, "schedule_compile", lambda **kw: None)
    monkeypatch.setattr(upload_records, "add_record", lambda **kw: {"id": "rec-1", **kw})
    monkeypatch.setattr(
        vector_store, "add_texts",
        lambda texts, metadatas: [f"id-{i}" for i in range(len(texts))],
    )


def _make_done_item(tmp_path):
    from app.services.upload_pipeline import upload_pipeline

    p = tmp_path / "doc.md"
    p.write_text("# 标题\n\n正文内容。", encoding="utf-8")
    item = upload_pipeline.create("doc.md", "markdown", str(p), "u1")
    pages = [{"page_number": 1, "text": "正文内容。", "section_title": "标题"}]
    db.execute(
        "UPDATE pending_uploads SET status='done', parsed_text=?, pages=? WHERE id=?",
        ("正文内容。", json.dumps(pages, ensure_ascii=False), item["id"]),
    )
    return item


def test_schedule_ingest_returns_immediately_then_background_completes(mem_db, tmp_path, monkeypatch):
    from app.services.upload_pipeline import upload_pipeline, _BG_INGEST_TASKS

    _mock_ingest_deps(monkeypatch)
    item = _make_done_item(tmp_path)
    kb_id = _uid("kb")
    db.execute(
        "INSERT INTO knowledge_bases (id, name, topic, visibility, owner_id, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (kb_id, "测试库", "其他", "public", "", "2026-11-13T10:00:00"),
    )

    async def scenario():
        # 提交即返回 ingesting，重活尚未执行（行仍在）
        res = upload_pipeline.schedule_ingest(item["id"], kb_id=kb_id, title="入库标题")
        assert res == {"item_id": item["id"], "status": "ingesting"}
        row = db.query_one("SELECT * FROM pending_uploads WHERE id=?", (item["id"],))
        assert row["status"] == "ingesting" and row["phase"] == "ingest"
        assert row["ingest_kb_id"] == kb_id and row["ingest_title"] == "入库标题"
        # 等待后台任务完成：成功后行被移除
        tasks = set(_BG_INGEST_TASKS)
        assert tasks, "应已登记后台入库任务"
        await asyncio.gather(*tasks)
        assert upload_pipeline.get(item["id"]) is None

    asyncio.run(scenario())


def test_schedule_ingest_failure_marks_phase_and_retry_reuses_params(mem_db, tmp_path, monkeypatch):
    from app.services.upload_pipeline import upload_pipeline

    _mock_ingest_deps(monkeypatch)
    item = _make_done_item(tmp_path)
    kb_id = _uid("kb")
    db.execute(
        "INSERT INTO knowledge_bases (id, name, topic, visibility, owner_id, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (kb_id, "测试库", "其他", "public", "", "2026-11-13T10:00:00"),
    )

    calls = []

    async def failing_ingest(item_id, **kw):
        calls.append(kw)
        raise RuntimeError("向量化服务异常")

    monkeypatch.setattr(upload_pipeline, "ingest", failing_ingest)
    # 无事件循环场景：schedule_ingest 直接同步执行后台任务，失败落 error+phase=ingest
    upload_pipeline.schedule_ingest(item["id"], kb_id=kb_id, title="重试标题")
    row = db.query_one("SELECT * FROM pending_uploads WHERE id=?", (item["id"],))
    assert row["status"] == "error" and row["phase"] == "ingest"
    assert "向量化服务异常" in row["error"]
    assert row["ingest_kb_id"] == kb_id and row["ingest_title"] == "重试标题"

    # 重试不带参数：沿用上次落库的目标库/标题；成功后行移除
    async def ok_ingest(item_id, **kw):
        calls.append(kw)
        db.execute("DELETE FROM pending_uploads WHERE id=?", (item_id,))
        return {"success": True}

    monkeypatch.setattr(upload_pipeline, "ingest", ok_ingest)
    upload_pipeline.schedule_ingest(item["id"])
    assert calls[-1] == {"kb_id": kb_id, "title": "重试标题"}
    assert upload_pipeline.get(item["id"]) is None


def test_schedule_ingest_sync_precheck_rejections(mem_db, tmp_path, monkeypatch):
    from app.services import ingest_dedup
    from app.services.upload_pipeline import upload_pipeline

    _mock_ingest_deps(monkeypatch)
    item = _make_done_item(tmp_path)

    # 重复指纹：同步预检直接抛出（API 映射 409），状态不变
    def dup(*a, **k):
        raise ingest_dedup.DuplicateIngestError("重复入库")

    monkeypatch.setattr(ingest_dedup, "ensure_can_ingest", dup)
    with pytest.raises(ingest_dedup.DuplicateIngestError):
        upload_pipeline.schedule_ingest(item["id"], kb_id="kb-x")
    assert db.query_one("SELECT status FROM pending_uploads WHERE id=?", (item["id"],))["status"] == "done"
    monkeypatch.setattr(ingest_dedup, "ensure_can_ingest", lambda *a, **k: None)

    # 正在入库中：拒绝重复提交
    db.execute("UPDATE pending_uploads SET status='ingesting' WHERE id=?", (item["id"],))
    with pytest.raises(ValueError, match="后台入库中"):
        upload_pipeline.schedule_ingest(item["id"])

    # 未解析完成 / 内容为空 / 行不存在
    db.execute("UPDATE pending_uploads SET status='pending' WHERE id=?", (item["id"],))
    with pytest.raises(ValueError, match="尚未解析完成"):
        upload_pipeline.schedule_ingest(item["id"])
    db.execute("UPDATE pending_uploads SET status='done', parsed_text='', pages='[]' WHERE id=?", (item["id"],))
    with pytest.raises(ValueError, match="内容为空"):
        upload_pipeline.schedule_ingest(item["id"])
    with pytest.raises(ValueError, match="不存在"):
        upload_pipeline.schedule_ingest("no-such-item")


def test_recover_resumes_ingesting_rows(mem_db, monkeypatch):
    from app.core.agent_runtime import runtime
    from app.services.upload_pipeline import upload_pipeline

    monkeypatch.setattr(runtime, "require_owner", lambda: None)
    monkeypatch.setattr(upload_pipeline, "schedule_parse", lambda item_id: None)
    resumed = []

    async def fake_run_ingest(item_id):
        resumed.append(item_id)

    monkeypatch.setattr(upload_pipeline, "_run_ingest", fake_run_ingest)
    item_id = _uid("pu")
    db.execute(
        "INSERT INTO pending_uploads (id, filename, source_type, file_path, owner_id,"
        " status, phase, ingest_kb_id, ingest_title, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (item_id, "a.md", "markdown", "/tmp/a.md", "u1",
         "ingesting", "ingest", "kb-x", "标题", "2026-11-13T10:00:00", "2026-11-13T10:00:00"),
    )

    async def scenario():
        n = upload_pipeline.recover()
        assert n >= 1
        await asyncio.sleep(0)  # 让恢复任务跑一轮

    asyncio.run(scenario())
    assert resumed == [item_id]


# ============================================
# 二、单文档范围：归属判定
# ============================================

def test_row_in_doc_direct_and_refs():
    from app.services.kb_content import _row_in_doc

    assert _row_in_doc({"source_image": "/a.md", "source_refs": "[]"}, "/a.md") is True
    assert _row_in_doc({"source_image": "/b.md", "source_refs": '["/a.md","/c.md"]'}, "/a.md") is True
    assert _row_in_doc({"source_image": "/b.md", "source_refs": "[]"}, "/a.md") is False
    # 损坏 JSON 不抛异常，按无引用处理
    assert _row_in_doc({"source_image": "/b.md", "source_refs": "{bad"}, "/a.md") is False


# ============================================
# 三、思维导图：scope 存储隔离 + 单文档提示词
# ============================================

def test_mindmap_scope_storage_isolation(mem_db):
    from app.services import kb_mindmap

    kb_id = _uid("kb")
    doc = "/uploads/doc-a.md"
    scope = kb_mindmap._scope(kb_id, doc)
    assert scope == f"{kb_id}::doc::{doc}"
    assert kb_mindmap._scope(kb_id, "") == kb_id

    now = "2026-11-13T10:00:00"
    whole_tree = {"name": "整库根", "children": [{"name": "整库分支"}]}
    doc_tree = {"name": "文档根", "children": [{"name": "文档分支"}]}
    db.execute("INSERT INTO kb_mindmaps (kb_id, data, source_hash, updated_at) VALUES (?,?,?,?)",
               (kb_id, json.dumps(whole_tree, ensure_ascii=False), "h1", now))
    db.execute("INSERT INTO kb_mindmaps (kb_id, data, source_hash, updated_at) VALUES (?,?,?,?)",
               (scope, json.dumps(doc_tree, ensure_ascii=False), "h2", now))

    assert kb_mindmap.get_mindmap(kb_id)["tree"]["name"] == "整库根"
    assert kb_mindmap.get_mindmap(kb_id, doc)["tree"]["name"] == "文档根"
    # 任务登记同样按 scope 隔离
    kb_mindmap._JOBS[scope] = {"status": "running"}
    assert kb_mindmap.job_status(kb_id) is None
    assert kb_mindmap.job_status(kb_id, doc)["status"] == "running"
    kb_mindmap._JOBS.clear()


def test_mindmap_rebuild_doc_scope_prompt_and_context(mem_db, monkeypatch):
    from app.services import kb_mindmap, wiki
    from app.services.records import upload_records

    kb_id = _uid("kb")
    doc_a, doc_b = "/uploads/a.md", "/uploads/b.md"
    _insert_wiki_page(kb_id, "A文档百科", source_image=doc_a,
                      analysis={"claims": ["A要点"], "structure": {"keywords": ["甲"]}})
    _insert_wiki_page(kb_id, "B文档百科", source_image=doc_b,
                      analysis={"claims": ["B要点"]})
    monkeypatch.setattr(
        upload_records, "list_records",
        lambda limit, offset: ([
            {"image_path": doc_a, "title": "文档A", "filename": "a.md",
             "ocr_text": "A正文" * 50, "updated_at": "2026-11-13T10:00:00"},
            {"image_path": doc_b, "title": "文档B", "filename": "b.md",
             "ocr_text": "B正文", "updated_at": "2026-11-13T10:00:00"},
        ], 2),
    )
    monkeypatch.setattr(vector_store, "_load_metadata", lambda: {})

    captured = {}

    async def fake_llm(messages):
        captured["messages"] = messages
        return json.dumps({"name": "文档A", "children": [{"name": "章节一", "src": "文档A"}]},
                          ensure_ascii=False)

    monkeypatch.setattr(wiki, "_llm_complete", fake_llm)
    result = asyncio.run(kb_mindmap.rebuild(kb_id, "测试库", "其他", doc_a))
    assert result["tree"]["name"] == "文档A"

    system = captured["messages"][0]["content"]
    user = captured["messages"][1]["content"]
    assert "单篇文档" in system and "根节点用该文档标题" in system
    assert "【单篇文档】《文档A》" in user
    # 上下文只含 A 文档内容，B 文档被过滤
    assert "A要点" in user and "B要点" not in user and "B正文" not in user
    # 单文档结果存于 scope 键，不覆盖整库
    scope = kb_mindmap._scope(kb_id, doc_a)
    assert db.query_one("SELECT data FROM kb_mindmaps WHERE kb_id=?", (scope,)) is not None
    assert db.query_one("SELECT data FROM kb_mindmaps WHERE kb_id=?", (kb_id,)) is None
    # 单文档标题清单只含该文档
    assert kb_mindmap.doc_titles(kb_id, doc_path=doc_a) == ["文档A"]
    # 内容哈希随文档范围变化，且整库/单篇互不相同
    assert kb_mindmap.content_hash(kb_id, doc_a) != kb_mindmap.content_hash(kb_id)
    assert kb_mindmap.content_hash(kb_id, doc_a) != kb_mindmap.content_hash(kb_id, doc_b)


def test_mindmap_rebuild_doc_empty_content_raises(mem_db, monkeypatch):
    from app.services import kb_mindmap
    from app.services.records import upload_records

    monkeypatch.setattr(upload_records, "list_records", lambda limit, offset: ([], 0))
    monkeypatch.setattr(vector_store, "_load_metadata", lambda: {})
    with pytest.raises(ValueError, match="该文档暂无入库内容"):
        asyncio.run(kb_mindmap.rebuild(_uid("kb"), "库", "其他", "/uploads/none.md"))


# ============================================
# 四、知识图谱：单文档子图实时聚合（不含手动节点）
# ============================================

def test_doc_graph_filters_and_excludes_manual(mem_db):
    from app.services import knowledge_graph

    kb_id = _uid("kb")
    doc_a, doc_b = "/uploads/a.md", "/uploads/b.md"
    _insert_wiki_page(kb_id, "A页", source_image=doc_a,
                      analysis={"entities": [{"name": "实体A", "type": "concept"}],
                                "entity_relations": [{"source": "实体A", "target": "实体A2", "label": "属于"}]})
    # digest 跨素材引用 A：也应归入 A 子图
    _insert_wiki_page(kb_id, "综合页", page_type="digest", source_refs=[doc_a, doc_b],
                      analysis={"entities": [{"name": "综合实体", "type": "topic"}]})
    _insert_wiki_page(kb_id, "B页", source_image=doc_b,
                      analysis={"entities": [{"name": "实体B", "type": "concept"}]})
    # 整库图谱含手动节点
    manual = {"nodes": [{"id": "n-m", "name": "手动节点", "type": "concept", "note": "", "origin": "manual"}],
              "edges": []}
    db.execute("INSERT INTO kb_graphs (kb_id, data, updated_at) VALUES (?,?,?)",
               (kb_id, json.dumps(manual, ensure_ascii=False), "2026-11-13T10:00:00"))

    sub = knowledge_graph.doc_graph(kb_id, doc_a)
    names = {n["name"] for n in sub["nodes"]}
    assert names == {"实体A", "实体A2", "综合实体"}
    assert "手动节点" not in names and "实体B" not in names
    assert any(e["label"] == "属于" for e in sub["edges"])
    assert sub["updated_at"] == ""
    # 子图实时计算不落库：整库数据不被污染
    stored = json.loads(db.query_one("SELECT data FROM kb_graphs WHERE kb_id=?", (kb_id,))["data"])
    assert [n["name"] for n in stored["nodes"]] == ["手动节点"]
    # get_graph 带 doc_path 直接走子图
    assert {n["name"] for n in knowledge_graph.get_graph(kb_id, doc_a)["nodes"]} == names
    # 整库重建仍保留手动节点并聚合全部 wiki 页
    rebuilt = knowledge_graph.rebuild_from_wiki(kb_id)
    all_names = {n["name"] for n in rebuilt["nodes"]}
    assert {"手动节点", "实体A", "实体B", "综合实体"} <= all_names


# ============================================
# 五、知识百科：list_pages / API 按 doc_path 过滤
# ============================================

def test_wiki_list_pages_doc_path_filter(mem_db):
    from app.services.wiki import wiki_service

    kb_id = _uid("kb")
    doc_a, doc_b = "/uploads/a.md", "/uploads/b.md"
    pa = _insert_wiki_page(kb_id, "A卡片", source_image=doc_a)
    pd = _insert_wiki_page(kb_id, "跨素材综合", page_type="digest", source_refs=[doc_a, doc_b])
    _insert_wiki_page(kb_id, "B卡片", source_image=doc_b)

    pages_a = wiki_service.list_pages("u1", True, kb_id=kb_id, doc_path=doc_a)
    assert {p["id"] for p in pages_a} == {pa, pd}
    pages_b = wiki_service.list_pages("u1", True, kb_id=kb_id, doc_path=doc_b)
    assert {p["title"] for p in pages_b} == {"跨素材综合", "B卡片"}
    # 不带 doc_path：整库全量
    assert len(wiki_service.list_pages("u1", True, kb_id=kb_id)) == 3
    # 私人页权限不受 doc_path 影响
    _insert_wiki_page(kb_id, "私人A", source_image=doc_a, visibility="private")
    assert len(wiki_service.list_pages("other", False, kb_id=kb_id, doc_path=doc_a)) == 2


def test_wiki_api_pages_doc_path_query(mem_db, monkeypatch):
    from app.api import wiki as wiki_api
    from app.core import insight_cache

    kb_id = _uid("kb")
    doc_a, doc_b = "/uploads/a.md", "/uploads/b.md"
    _insert_wiki_page(kb_id, "A卡片", source_image=doc_a)
    _insert_wiki_page(kb_id, "B卡片", source_image=doc_b)
    monkeypatch.setattr(insight_cache, "cache_get", lambda key: None)
    monkeypatch.setattr(insight_cache, "cache_set", lambda *a, **k: True)

    user = {"id": "u1", "username": "u1", "role": "admin"}
    res = asyncio.run(wiki_api.list_wiki_pages(kb_id=kb_id, page_type="", doc_path=doc_a, current_user=user))
    assert [r["title"] for r in res["pages"]] == ["A卡片"]

    # 缓存命中分支同样按 doc_path 过滤
    rows = {"rows": [
        {"id": "1", "page_type": "source", "title": "A卡片", "kb_id": kb_id, "visibility": "public",
         "owner_id": "", "source_image": doc_a, "source_refs": [], "excerpt": "", "updated_at": ""},
        {"id": "2", "page_type": "source", "title": "B卡片", "kb_id": kb_id, "visibility": "public",
         "owner_id": "", "source_image": doc_b, "source_refs": [], "excerpt": "", "updated_at": ""},
    ]}
    monkeypatch.setattr(insight_cache, "cache_get", lambda key: rows)
    res = asyncio.run(wiki_api.list_wiki_pages(kb_id=kb_id, page_type="", doc_path=doc_b, current_user=user))
    assert [r["title"] for r in res["pages"]] == ["B卡片"]


def test_kb_insights_endpoints_doc_path(mem_db, monkeypatch):
    from app.api import kb_insights
    from app.services import knowledge_graph

    kb_id = _uid("kb")
    doc_a = "/uploads/a.md"
    db.execute(
        "INSERT INTO knowledge_bases (id, name, topic, visibility, owner_id, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (kb_id, "测试库", "其他", "public", "", "2026-11-13T10:00:00"),
    )
    _insert_wiki_page(kb_id, "A页", source_image=doc_a,
                      analysis={"entities": [{"name": "实体A", "type": "concept"}]})
    user = {"id": "u1", "username": "u1", "role": "admin"}

    # 图谱 GET：单文档走子图，job 恒为 None
    res = asyncio.run(kb_insights.get_kb_graph(kb_id, doc_path=doc_a, current_user=user))
    assert res["job"] is None
    assert {n["name"] for n in res["graph"]["nodes"]} == {"实体A"}

    # 图谱 rebuild：单文档实时返回 started=False，不启动后台任务
    started = []
    monkeypatch.setattr(knowledge_graph, "start_rebuild_background", lambda kb: started.append(kb) or True)
    res = asyncio.run(kb_insights.rebuild_kb_graph(kb_id, doc_path=doc_a, current_user=user))
    assert res["started"] is False and res["job"] is None
    assert {n["name"] for n in res["graph"]["nodes"]} == {"实体A"}
    assert started == []

    # 思维导图 GET：未生成时 tree=None，job 按 scope 隔离
    res = asyncio.run(kb_insights.get_kb_mindmap(kb_id, doc_path=doc_a, current_user=user))
    assert res["success"] is True and res["tree"] is None