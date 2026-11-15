"""测试集构建流水线回归测试（内存 DB / mock LLM，不出网）。

覆盖计划第二节 RAG 测试集构建：
  T1 KB 生成 → candidates(pending)，不直入 eval_cases（expected_source 扩展）；
  T2 approve 可编辑入集（origin 继承）/重复审核拒绝；
  T3 reject 不入集；T4 批量通过仅处理 pending；
  T5 日志挖掘配对不足不触发（游标不推进）；
  T6 达量触发：origin=log_mining、reference_answer=真实回答、游标推进、候选集自动创建/复用；
  T7 LLM 提取失败静默降级（空信号仍落候选）；
  T8 列表脱敏 + 过滤 + pending 计数；
  T9 配对过滤（use_knowledge=0 / 空回答排除）；
  T10 expected_source 只能取自真实来源标题（防编造）。
"""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.core import db
from app.services import testset_builder as tb


@pytest.fixture()
def mem_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    yield conn
    conn.close()


@pytest.fixture()
def rc_store(monkeypatch):
    """runtime_config 单例替换为内存字典（不落盘、不读真实配置）。"""
    from app.services import config_manager
    store = {}
    monkeypatch.setattr(config_manager.runtime_config, "get",
                        lambda key, default=None: store.get(key, default))
    monkeypatch.setattr(config_manager.runtime_config, "set",
                        lambda key, value: store.__setitem__(key, value))
    return store


def _dataset(name="测试集", kind="rag"):
    ds_id = f"ds-{name}"
    db.execute("INSERT INTO eval_datasets (id, name, description, kind, created_at)"
               " VALUES (?,?,?,?,?)", (ds_id, name, "", kind, datetime.now().isoformat()))
    return ds_id


def _kb(name="养生知识库"):
    kb_id = f"kb-{name}"
    db.execute("INSERT INTO knowledge_bases (id, name, created_at) VALUES (?,?,?)",
               (kb_id, name, datetime.now().isoformat()))
    return kb_id


def _pair(i, use_knowledge=1, answer="真实回答内容", ts=None, sources="[]"):
    """一对用户问 + 助手答（seq 相邻，助手 use_knowledge 可控）。"""
    ts = ts or (datetime.now() - timedelta(minutes=60 - i)).isoformat()
    db.execute(
        "INSERT INTO chat_messages (message_id, session_id, seq, role, content,"
        " created_at) VALUES (?,?,?,?,?,?)",
        (f"u{i}", "s1", i * 2 - 1, "user", f"问题{i}", ts))
    db.execute(
        "INSERT INTO chat_messages (message_id, session_id, seq, role, content, sources,"
        " use_knowledge, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (f"m{i}", "s1", i * 2, "assistant", answer, sources, use_knowledge, ts))
    return ts


def _mock_llm(monkeypatch, payload: dict, raise_exc=False):
    """替换 wiki._llm_complete，记录调用。"""
    from app.services import wiki
    calls = []

    async def fake(messages, llm=None):
        calls.append(messages)
        if raise_exc:
            raise RuntimeError("模型不可用")
        return json.dumps(payload, ensure_ascii=False)
    monkeypatch.setattr(wiki, "_llm_complete", fake)
    return calls


def _mock_kb_context(monkeypatch, text="知识库内容：失眠食疗方。"):
    from app.services import kb_content
    monkeypatch.setattr(kb_content, "build_kb_context", lambda kb_id, max_chars=8000, doc_path="": text)


# ============================================
# T1 KB 生成 → 候选池（不直入评测集）
# ============================================
def test_generate_from_kb_writes_pending_candidates(mem_db, monkeypatch):
    ds_id, kb_id = _dataset(), _kb()
    _mock_kb_context(monkeypatch)
    _mock_llm(monkeypatch, {"cases": [
        {"question": "失眠吃什么？", "reference_answer": "小米粥",
         "expected_keywords": ["小米粥", "失眠"], "expected_source": "失眠食疗"},
        {"question": "  ", "reference_answer": "无效用例"},
    ]})
    result = asyncio.run(tb.generate_from_kb(ds_id, kb_id, 5, {"id": "admin"}))
    assert result["created"] == 1
    # 候选 pending，eval_cases 仍为空（人工筛选审核后才入集）
    assert db.query_one("SELECT COUNT(*) AS n FROM eval_cases")["n"] == 0
    cand = db.query_one("SELECT * FROM eval_case_candidates")
    assert cand["status"] == "pending"
    assert cand["origin"] == "llm_gen"
    assert cand["expected_source"] == "失眠食疗"
    assert json.loads(cand["expected_keywords"]) == ["小米粥", "失眠"]
    assert json.loads(cand["source_ref"])["kb_id"] == kb_id


def test_generate_from_kb_validation(mem_db, monkeypatch):
    ds_id, kb_id = _dataset(), _kb()
    _mock_kb_context(monkeypatch)
    _mock_llm(monkeypatch, {"cases": [{"question": "q"}]})
    with pytest.raises(ValueError):
        asyncio.run(tb.generate_from_kb("不存在", kb_id, 5, {}))
    with pytest.raises(ValueError):
        asyncio.run(tb.generate_from_kb(ds_id, "不存在", 5, {}))
    _mock_kb_context(monkeypatch, text="   ")
    with pytest.raises(ValueError):
        asyncio.run(tb.generate_from_kb(ds_id, kb_id, 5, {}))


# ============================================
# T2/T3/T4 审核闭环
# ============================================
def _pending_candidate(ds_id, question="原始问题", origin="llm_gen"):
    return tb._insert_candidate(ds_id, "", origin, question, "原始参考答案",
                                ["原关键词"], "原来源", {}, "admin")["id"]


def test_approve_with_edits_inherits_origin(mem_db):
    ds_id = _dataset()
    cid = _pending_candidate(ds_id, origin="log_mining")
    result = tb.approve_candidate(cid, {"id": "reviewer"}, {
        "question": "编辑后的问题", "reference_answer": "编辑后的答案",
        "expected_keywords": ["新关键词"], "expected_source": "新来源",
        "review_note": "已核对",
    })
    case = db.query_one("SELECT * FROM eval_cases WHERE id = ?", (result["case_id"],))
    assert case["question"] == "编辑后的问题"
    assert case["reference_answer"] == "编辑后的答案"
    assert json.loads(case["expected_keywords"]) == ["新关键词"]
    assert case["expected_source"] == "新来源"
    assert case["origin"] == "log_mining"          # origin 继承候选来源
    cand = db.query_one("SELECT * FROM eval_case_candidates WHERE id = ?", (cid,))
    assert cand["status"] == "approved"
    assert cand["reviewed_by"] == "reviewer"
    assert cand["review_note"] == "已核对"
    # 重复审核拒绝
    with pytest.raises(ValueError):
        tb.approve_candidate(cid, {"id": "reviewer"})


def test_approve_without_edits_keeps_original(mem_db):
    ds_id = _dataset()
    cid = _pending_candidate(ds_id)
    result = tb.approve_candidate(cid, {"id": "r"})
    case = db.query_one("SELECT * FROM eval_cases WHERE id = ?", (result["case_id"],))
    assert case["question"] == "原始问题"
    assert case["origin"] == "llm_gen"


def test_reject_and_batch_approve(mem_db):
    ds_id = _dataset()
    c1 = _pending_candidate(ds_id, question="问题一")
    c2 = _pending_candidate(ds_id, question="问题二")
    c3 = _pending_candidate(ds_id, question="问题三")
    tb.reject_candidate(c3, {"id": "r"}, note="质量差")
    assert db.query_one("SELECT status FROM eval_case_candidates WHERE id = ?",
                        (c3,))["status"] == "rejected"
    with pytest.raises(ValueError):
        tb.reject_candidate(c3, {"id": "r"})
    result = tb.batch_approve([c1, c2, c3, "不存在"], {"id": "r"})
    assert result == {"approved": 2, "skipped": 2}   # c3 已拒绝 + 不存在 → skipped
    assert db.query_one("SELECT COUNT(*) AS n FROM eval_cases")["n"] == 2


# ============================================
# T5/T6 日志挖掘阈值与游标
# ============================================
def test_mine_below_threshold_not_triggered(mem_db, rc_store, monkeypatch):
    monkeypatch.setattr(settings, "LOG_MINING_MIN_PAIRS", 3)
    _pair(1)
    _pair(2)
    result = asyncio.run(tb.mine_from_logs())
    assert result["triggered"] is False
    assert result["pairs"] == 2
    assert result["created"] == 0
    assert rc_store.get(tb._CURSOR_KEY) is None     # 游标不推进，等待积累
    assert db.query_one("SELECT COUNT(*) AS n FROM eval_case_candidates")["n"] == 0


def test_mine_triggered_creates_candidates_and_cursor(mem_db, rc_store, monkeypatch):
    monkeypatch.setattr(settings, "LOG_MINING_MIN_PAIRS", 2)
    sources = json.dumps([{"document_title": "失眠食疗", "chunks": [{"text": "片段"}]}])
    _pair(1, sources=sources)
    _pair(2, sources=sources)
    _mock_llm(monkeypatch, {"expected_keywords": ["小米粥"], "expected_source": "失眠食疗"})
    result = asyncio.run(tb.mine_from_logs())
    assert result["triggered"] is True
    assert result["created"] == 2
    # 自动创建「日志挖掘候选集」(kind=rag)
    ds = db.query_one("SELECT * FROM eval_datasets WHERE id = ?", (result["dataset_id"],))
    assert ds["name"] == "日志挖掘候选集"
    assert ds["kind"] == "rag"
    cands = db.query("SELECT * FROM eval_case_candidates ORDER BY created_at")
    assert all(c["origin"] == "log_mining" and c["status"] == "pending" for c in cands)
    assert cands[0]["reference_answer"] == "真实回答内容"   # 真实回答为参考答案
    assert json.loads(cands[0]["source_ref"])["message_id"] == "m1"
    assert rc_store.get(tb._CURSOR_KEY)                # 游标已推进
    # 第二轮：无新配对 → 不触发；候选集复用不重复创建
    result2 = asyncio.run(tb.mine_from_logs())
    assert result2["triggered"] is False
    assert db.query_one("SELECT COUNT(*) AS n FROM eval_datasets")["n"] == 1


def test_mine_respects_explicit_dataset(mem_db, rc_store, monkeypatch):
    monkeypatch.setattr(settings, "LOG_MINING_MIN_PAIRS", 1)
    ds_id = _dataset("指定期")
    _pair(1)
    _mock_llm(monkeypatch, {"expected_keywords": [], "expected_source": ""})
    result = asyncio.run(tb.mine_from_logs(ds_id))
    assert result["dataset_id"] == ds_id
    with pytest.raises(ValueError):
        asyncio.run(tb.mine_from_logs("不存在"))


# ============================================
# T7 LLM 降级 / T9 配对过滤 / T10 来源防编造
# ============================================
def test_mine_llm_failure_degrades_to_empty_signals(mem_db, rc_store, monkeypatch):
    monkeypatch.setattr(settings, "LOG_MINING_MIN_PAIRS", 1)
    _pair(1)
    _mock_llm(monkeypatch, {}, raise_exc=True)
    result = asyncio.run(tb.mine_from_logs())
    assert result["created"] == 1                    # 降级不阻断：问答对仍落候选
    cand = db.query_one("SELECT * FROM eval_case_candidates")
    assert json.loads(cand["expected_keywords"]) == []
    assert cand["expected_source"] == ""
    assert cand["reference_answer"] == "真实回答内容"


def test_pairs_exclude_non_knowledge_and_empty(mem_db, rc_store, monkeypatch):
    monkeypatch.setattr(settings, "LOG_MINING_MIN_PAIRS", 1)
    _pair(1, use_knowledge=0)                        # 非知识问答排除
    _pair(2, answer="")                              # 空回答排除
    result = asyncio.run(tb.mine_from_logs())
    assert result["triggered"] is False
    assert result["pairs"] == 0


def test_extract_source_only_from_real_titles(mem_db, rc_store, monkeypatch):
    monkeypatch.setattr(settings, "LOG_MINING_MIN_PAIRS", 1)
    _pair(1, sources=json.dumps([{"document_title": "真实标题"}]))
    _mock_llm(monkeypatch, {"expected_keywords": ["k"], "expected_source": "编造标题"})
    asyncio.run(tb.mine_from_logs())
    cand = db.query_one("SELECT * FROM eval_case_candidates")
    assert cand["expected_source"] == ""             # 不在真实标题中 → 丢弃


# ============================================
# T8 列表脱敏 + 过滤 + 计数
# ============================================
def test_list_candidates_redacts_and_filters(mem_db):
    ds_id = _dataset()
    tb._insert_candidate(ds_id, "", "llm_gen", "联系电话13812345678的问题",
                         "答案邮箱 test@example.com", [], "", {}, "admin")
    c2 = _pending_candidate(ds_id, question="正常问题")
    tb.reject_candidate(c2, {"id": "r"})
    result = tb.list_candidates(dataset_id=ds_id)
    assert result["total"] == 2
    assert result["pending_count"] == 1
    item = next(i for i in result["items"] if i["status"] == "pending")
    assert "13812345678" not in item["question"]
    assert "[手机号已脱敏]" in item["question"]
    assert "test@example.com" not in item["reference_answer"]
    # 原始落库不改写（单一事实源）
    raw = db.query_one("SELECT question FROM eval_case_candidates WHERE status = 'pending'")
    assert "13812345678" in raw["question"]
    # 状态过滤
    assert tb.list_candidates(dataset_id=ds_id, status="rejected")["total"] == 1