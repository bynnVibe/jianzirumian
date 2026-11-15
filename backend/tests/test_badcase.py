"""BadCase 分析框架回归测试（内存 DB / mock LLM，不出网）。

覆盖计划第四节 BadCase 框架：
  C1 离线源收集与确定性分类（retrieval_miss/empty_context/hallucination/irrelevant/low_quality/error）；
  C2 在线低分源；C3 点踩源（问题回溯）；C4 空回答源（content 空/trace error）；
  C5 Agent 源（工具 span error → tool_failure / 任务完成度低 → low_quality）；
  C6 source_id 去重 + 游标推进；
  C7 to-case 闭环（origin=badcase、case_id 关联、重复转换拒绝、编辑参考答案）；
  C8 repro_check 通过判定（faithfulness≥0.6 且检索命中或无期望 → reproduced + 快照）；
  C9 mark fixed/ignored/open；C10 LLM 归因成功/降级；C11 列表脱敏+过滤+计数。
"""
import asyncio
import json
import sqlite3
from datetime import datetime

import pytest

from app.core import db
from app.services import badcase as bc


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
    from app.services import config_manager
    store = {}
    monkeypatch.setattr(config_manager.runtime_config, "get",
                        lambda key, default=None: store.get(key, default))
    monkeypatch.setattr(config_manager.runtime_config, "set",
                        lambda key, value: store.__setitem__(key, value))
    return store


def _dataset():
    db.execute("INSERT INTO eval_datasets (id, name, created_at) VALUES ('ds1','集',?)",
               (datetime.now().isoformat(),))
    return "ds1"


def _case(case_id="c1", keywords=None, source=""):
    db.execute(
        "INSERT INTO eval_cases (id, dataset_id, question, expected_keywords,"
        " expected_source, created_at) VALUES (?,?,?,?,?,?)",
        (case_id, "ds1", f"问题-{case_id}",
         json.dumps(keywords if keywords is not None else ["关键词"], ensure_ascii=False),
         source, datetime.now().isoformat()))
    return case_id


def _result(rid, case_id="c1", **kw):
    fields = {
        "run_id": "run1", "case_id": case_id, "question": f"问题-{case_id}",
        "answer": "答案", "retrieved_sources": json.dumps([{"text": "片段"}]),
        "retrieval_hit": 1, "faithfulness": 0.9, "answer_relevance": 0.9,
        "hallucination": None, "geval_score": None, "status": "ok", "error": "",
    }
    fields.update(kw)
    cols = ", ".join(["id"] + list(fields))
    ph = ", ".join(["?"] * (len(fields) + 1))
    db.execute(f"INSERT INTO eval_results ({cols}) VALUES ({ph})",
               (rid, *fields.values()))
    return rid


def _mock_llm(monkeypatch, payload, raise_exc=False):
    from app.services import wiki
    calls = []

    async def fake(messages, llm=None):
        calls.append(messages)
        if raise_exc:
            raise RuntimeError("模型不可用")
        return json.dumps(payload, ensure_ascii=False)
    monkeypatch.setattr(wiki, "_llm_complete", fake)
    return calls


# ============================================
# C1 离线源收集与分类
# ============================================
def test_offline_classification(mem_db, rc_store):
    _dataset()
    _case("c-miss", keywords=["期望词"])
    _case("c-noexp", keywords=[], source="")
    _result("r1", "c-miss", retrieval_hit=0)                       # 有期望未命中 → retrieval_miss
    _result("r2", "c-noexp", retrieval_hit=0)                      # 无期望信号 → 不收集
    _result("r3", "c1", retrieved_sources="[]", geval_score=0.4)   # 低分+上下文空 → empty_context
    _result("r4", "c1", faithfulness=0.4)                          # 忠实度低 → hallucination
    _result("r5", "c1", hallucination=0.8)                         # 幻觉高 → hallucination
    _result("r6", "c1", answer_relevance=0.3)                      # 相关性低 → irrelevant
    _result("r7", "c1", geval_score=0.4)                           # GEval 低 → low_quality
    _result("r8", "c1", status="error", error="超时")              # 执行错误 → low_quality
    _result("r9", "c1")                                            # 全优 → 不收集
    stats = bc.collect_round()
    assert stats["by_source"]["offline_eval"] == 7
    cats = {r["source_id"]: r["category"] for r in db.query("SELECT * FROM bad_cases")}
    assert cats["r1"] == "retrieval_miss"
    assert cats["r3"] == "empty_context"
    assert cats["r4"] == "hallucination"
    assert cats["r5"] == "hallucination"
    assert cats["r6"] == "irrelevant"
    assert cats["r7"] == "low_quality"
    assert cats["r8"] == "low_quality"
    assert "r2" not in cats and "r9" not in cats
    # 初始归因为规则模板（degraded）
    row = db.query_one("SELECT analysis FROM bad_cases WHERE source_id = 'r1'")
    assert json.loads(row["analysis"])["degraded"] is True


# ============================================
# C2/C3/C4 在线 / 点踩 / 空回答源
# ============================================
def test_online_feedback_empty_sources(mem_db, rc_store):
    now = datetime.now().isoformat()
    db.execute(
        "INSERT INTO online_eval_results (id, created_at, session_id, message_id,"
        " question, answer, faithfulness, geval_score, status)"
        " VALUES ('o1',?,'s1','m1','问','答',0.4,0.8,'ok')", (now,))
    db.execute(
        "INSERT INTO online_eval_results (id, created_at, session_id, message_id,"
        " question, answer, faithfulness, geval_score, status)"
        " VALUES ('o2',?,'s1','m2','问','答',0.9,0.3,'ok')", (now,))
    db.execute(
        "INSERT INTO chat_messages (message_id, session_id, seq, role, content, created_at)"
        " VALUES ('fu1','s2',1,'user','点踩的问题',?)", (now,))
    db.execute(
        "INSERT INTO chat_messages (message_id, session_id, seq, role, content, created_at)"
        " VALUES ('fa1','s2',2,'assistant','被点踩的回答',?)", (now,))
    db.execute(
        "INSERT INTO message_feedbacks (id, session_id, message_id, rating, comment, created_at)"
        " VALUES ('f1','s2','fa1','dislike','不好',?)", (now,))
    db.execute(
        "INSERT INTO chat_messages (message_id, session_id, seq, role, content, created_at)"
        " VALUES ('eu1','s3',1,'user','空回答的问题',?)", (now,))
    db.execute(
        "INSERT INTO chat_messages (message_id, session_id, seq, role, content, created_at)"
        " VALUES ('ea1','s3',2,'assistant','',?)", (now,))
    stats = bc.collect_round()
    assert stats["by_source"]["online_eval"] == 2
    assert stats["by_source"]["feedback"] == 1
    assert stats["by_source"]["empty_answer"] == 1
    rows = {r["source_id"]: r for r in db.query("SELECT * FROM bad_cases")}
    assert rows["o1"]["category"] == "hallucination"
    assert rows["o2"]["category"] == "low_quality"
    assert rows["f1"]["question"] == "点踩的问题"
    assert rows["ea1"]["category"] == "empty_answer"
    assert rows["ea1"]["question"] == "空回答的问题"


def test_empty_answer_trace_error(mem_db, rc_store):
    now = datetime.now().isoformat()
    db.execute(
        "INSERT INTO chat_messages (message_id, session_id, seq, role, content, created_at)"
        " VALUES ('ea2','s4',2,'assistant','有内容但 trace 错误',?)", (now,))
    db.execute(
        "INSERT INTO agent_traces (id, agent, session_id, status, started_at)"
        " VALUES ('t4','chat','s4','error',?)", (now,))
    stats = bc.collect_round()
    assert stats["by_source"]["empty_answer"] == 1
    assert db.query_one("SELECT category FROM bad_cases")["category"] == "empty_answer"


# ============================================
# C5 Agent 源
# ============================================
def _agent_result(table, rid, trace_id, tc=0.9, status="ok"):
    now = datetime.now().isoformat()
    if table == "agent_eval_results":
        db.execute(
            "INSERT INTO agent_eval_results (id, run_id, case_id, question, trace_id,"
            " answer, task_completion, status) VALUES (?,?,?,?,?,?,?,?)",
            (rid, "arun1", "ac1", "agent 问题", trace_id, "agent 答案", tc, status))
    else:
        db.execute(
            "INSERT INTO agent_online_evals (id, created_at, trace_id, agent, input,"
            " answer, task_completion, status) VALUES (?,?,?,?,?,?,?,?)",
            (rid, now, trace_id, "chat", "agent 问题", "agent 答案", tc, status))


def test_agent_sources_tool_failure_and_low_score(mem_db, rc_store):
    _agent_result("agent_eval_results", "ae1", "tr1", tc=0.9)
    db.execute("INSERT INTO agent_spans (id, trace_id, seq, kind, name, status, started_at)"
               " VALUES ('sp1','tr1',1,'tool','search_knowledge','error',?)",
               (datetime.now().isoformat(),))
    _agent_result("agent_eval_results", "ae2", "tr2", tc=0.3)     # 完成度低 → low_quality
    _agent_result("agent_eval_results", "ae3", "tr3", tc=0.9)     # 全优 → 不收集
    _agent_result("agent_online_evals", "ao1", "tr4", tc=0.2)
    stats = bc.collect_round()
    assert stats["by_source"]["agent_eval"] == 2
    assert stats["by_source"]["agent_online"] == 1
    rows = {r["source_id"]: r for r in db.query("SELECT * FROM bad_cases")}
    assert rows["ae1"]["category"] == "tool_failure"
    assert rows["ae2"]["category"] == "low_quality"
    assert rows["ao1"]["source_type"] == "agent_online"
    assert "ae3" not in rows


# ============================================
# C6 去重 + 游标
# ============================================
def test_dedup_and_cursor(mem_db, rc_store):
    _dataset()
    _case()
    _result("r1", faithfulness=0.3)
    first = bc.collect_round()
    assert first["collected"] == 1
    second = bc.collect_round()                     # 游标已推进 → 不重复扫描
    assert second["collected"] == 0
    assert db.query_one("SELECT COUNT(*) AS n FROM bad_cases")["n"] == 1
    assert rc_store.get(bc._CURSOR_KEY)["offline_eval"] >= 1
    # 即使游标重置，source_id UNIQUE 仍去重
    rc_store[bc._CURSOR_KEY] = {}
    third = bc.collect_round()
    assert third["collected"] == 0
    assert db.query_one("SELECT COUNT(*) AS n FROM bad_cases")["n"] == 1


# ============================================
# C7/C8 转用例 + 复现判定闭环
# ============================================
def _badcase_with_case():
    """收集一条离线 BadCase 并转用例，返回 (badcase_id, case_id)。"""
    _dataset()
    _case("c1", keywords=["关键词"])
    _result("r1", "c1", faithfulness=0.3)
    bc.collect_round()
    bc_id = db.query_one("SELECT id FROM bad_cases")["id"]
    result = bc.to_case(bc_id, "ds1", {"id": "u"},
                        {"reference_answer": "编辑后的参考答案",
                         "expected_keywords": ["关键词"]})
    return bc_id, result["case_id"]


def test_to_case_closed_loop(mem_db, rc_store):
    bc_id, case_id = _badcase_with_case()
    case = db.query_one("SELECT * FROM eval_cases WHERE id = ?", (case_id,))
    assert case["origin"] == "badcase"
    assert case["reference_answer"] == "编辑后的参考答案"
    assert db.query_one("SELECT case_id FROM bad_cases WHERE id = ?",
                        (bc_id,))["case_id"] == case_id
    with pytest.raises(ValueError):
        bc.to_case(bc_id, "ds1", {})                # 重复转换拒绝
    with pytest.raises(ValueError):
        bc.to_case("不存在", "ds1", {})
    # 未转换的 BadCase + 无效数据集 → 拒绝
    bc._insert("offline_eval", "r-extra", question="额外", category="low_quality")
    extra_id = db.query_one("SELECT id FROM bad_cases WHERE source_id = 'r-extra'")["id"]
    with pytest.raises(ValueError):
        bc.to_case(extra_id, "无集", {})


def test_repro_check_pass_and_fail(mem_db, rc_store):
    bc_id, case_id = _badcase_with_case()
    # 回归通过：faithfulness≥0.6 且检索命中 → reproduced + 快照
    _result("r-pass", case_id, run_id="run-pass", faithfulness=0.8, retrieval_hit=1)
    out = bc.repro_check("run-pass")
    assert out == {"checked": 1, "reproduced": 1}
    row = db.query_one("SELECT * FROM bad_cases WHERE id = ?", (bc_id,))
    assert row["status"] == "reproduced"
    assert row["repro_run_id"] == "run-pass"
    assert json.loads(row["analysis"])["repro"]["run_id"] == "run-pass"


def test_repro_check_fail_conditions(mem_db, rc_store):
    bc_id, case_id = _badcase_with_case()
    # 忠实度仍低 → 不通过
    _result("r-faith", case_id, run_id="run-f", faithfulness=0.4, retrieval_hit=1)
    assert bc.repro_check("run-f")["reproduced"] == 0
    # 有期望但检索未命中 → 不通过
    _result("r-hit", case_id, run_id="run-h", faithfulness=0.9, retrieval_hit=0)
    assert bc.repro_check("run-h")["reproduced"] == 0
    assert db.query_one("SELECT status FROM bad_cases WHERE id = ?",
                        (bc_id,))["status"] == "open"
    # 无期望信号 + 检索未命中 + faithfulness 未测(None) → 通过（降级不阻断）
    db.execute("UPDATE eval_cases SET expected_keywords = '[]', expected_source = ''"
               " WHERE id = ?", (case_id,))
    _result("r-noexp", case_id, run_id="run-n", faithfulness=None, retrieval_hit=0)
    assert bc.repro_check("run-n")["reproduced"] == 1


# ============================================
# C9 标记 / C10 LLM 归因 / C11 列表
# ============================================
def test_mark_statuses(mem_db, rc_store):
    _dataset()
    _case()
    _result("r1", faithfulness=0.3)
    bc.collect_round()
    bc_id = db.query_one("SELECT id FROM bad_cases")["id"]
    bc.mark(bc_id, "fixed", {"id": "u"}, note="已修复")
    row = db.query_one("SELECT * FROM bad_cases WHERE id = ?", (bc_id,))
    assert row["status"] == "fixed" and row["resolved_at"]
    bc.mark(bc_id, "ignored", {"id": "u"})
    assert db.query_one("SELECT status FROM bad_cases WHERE id = ?",
                        (bc_id,))["status"] == "ignored"
    bc.mark(bc_id, "open", {"id": "u"})
    row = db.query_one("SELECT * FROM bad_cases WHERE id = ?", (bc_id,))
    assert row["status"] == "open" and row["resolved_at"] == ""
    with pytest.raises(ValueError):
        bc.mark(bc_id, "bad-status", {})


def test_analyze_llm_and_degrade(mem_db, rc_store, monkeypatch):
    _dataset()
    _case()
    _result("r1", faithfulness=0.3)
    bc.collect_round()
    bc_id = db.query_one("SELECT id FROM bad_cases")["id"]
    _mock_llm(monkeypatch, {"cause": "检索片段与问题不匹配", "suggestions": ["补充知识"]})
    analysis = asyncio.run(bc.analyze(bc_id))
    assert analysis["degraded"] is False
    assert analysis["cause"] == "检索片段与问题不匹配"
    # LLM 失败 → 规则模板降级
    _mock_llm(monkeypatch, {}, raise_exc=True)
    analysis = asyncio.run(bc.analyze(bc_id))
    assert analysis["degraded"] is True
    assert analysis["cause"]
    with pytest.raises(ValueError):
        asyncio.run(bc.analyze("不存在"))


def test_list_redacts_filters_counts(mem_db, rc_store):
    _dataset()
    _case()
    _result("r1", faithfulness=0.3, question="电话13812345678的问题")
    bc.collect_round()
    listing = bc.list_badcases()
    assert listing["total"] == 1
    assert listing["category_counts"]["hallucination"] == 1
    assert listing["status_counts"]["open"] == 1
    item = listing["items"][0]
    assert "13812345678" not in item["question"]
    assert "[手机号已脱敏]" in item["question"]
    assert "13812345678" in db.query_one("SELECT question FROM bad_cases")["question"]
    assert bc.list_badcases(category="retrieval_miss")["total"] == 0
    assert bc.list_badcases(status="open")["total"] == 1
    assert bc.list_badcases(source_type="offline_eval")["total"] == 1