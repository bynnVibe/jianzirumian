"""Agent 评测框架回归测试（内存 DB / mock LLM 与 Agent，不出网）。

覆盖计划第五节 Agent 评测：
  M1 trajectory match 四模式（strict/unordered/subset/superset）+ args 匹配；
  M2 trace_trajectory 提取（工具名归一化/plan 检测/末 llm 答案）；
  M3 六指标 mock 评分 + 单点降级 None + plan 缺省 1；
  M4 离线 run（mock run_chat_agent + 真实 tracer）：结果落库/summary/并发保护/kind 校验/异常降级；
  M5 在线采样：采样率/单轮上限/游标推进/开关；
  M6 手动单 trace 评测（可附期望工具）；
  M7 aggregate 聚合 + list_online 脱敏。
"""
import asyncio
import json
import sqlite3
from datetime import datetime

import pytest

from app.config import settings
from app.core import db
from app.core import observability as obs
from app.services import agent_eval as ae


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


def _judge_mock(monkeypatch, payload=None, raise_exc=False):
    calls = []

    async def fake(messages):
        calls.append(messages)
        if raise_exc:
            raise RuntimeError("模型不可用")
        return payload if payload is not None else {
            "score": 0.8, "reason": "良好", "verdicts": [1, 1, 1]}
    monkeypatch.setattr(ae, "_judge_json", fake)
    return calls


def _tools(*names):
    return [{"name": n, "args": {"query": "q"}, "output": "o", "status": "ok"} for n in names]


# ============================================
# M1 四模式匹配 + args
# ============================================
def test_match_strict_and_unordered():
    act = _tools("search_knowledge", "web_search")
    assert ae.trajectory_match(act, ["search_knowledge", "web_search"], "strict")
    assert not ae.trajectory_match(act, ["web_search", "search_knowledge"], "strict")
    assert ae.trajectory_match(act, ["web_search", "search_knowledge"], "unordered")
    assert not ae.trajectory_match(act, ["search_knowledge"], "unordered")  # 数量不等
    assert not ae.trajectory_match(_tools("a", "a"), ["a"], "unordered")


def test_match_subset_and_superset():
    act = _tools("search_knowledge", "web_search")
    assert ae.trajectory_match(act, ["search_knowledge", "web_search", "extra"], "subset")
    assert not ae.trajectory_match(act, ["search_knowledge"], "subset")
    assert ae.trajectory_match(act, ["search_knowledge"], "superset")
    assert not ae.trajectory_match(act, ["search_knowledge", "missing"], "superset")
    # 空期望：superset 平凡真；strict 要求实际也空
    assert ae.trajectory_match(act, [], "superset")
    assert not ae.trajectory_match(act, [], "strict")
    assert ae.trajectory_match([], [], "strict")


def test_match_args():
    act = [{"name": "search_knowledge", "args": {"query": "失眠"}}]
    exp = [{"name": "search_knowledge", "args": {"query": "失眠"}}]
    assert ae.trajectory_match(act, exp, "strict", args_match=True)
    exp_diff = [{"name": "search_knowledge", "args": {"query": "其他"}}]
    assert not ae.trajectory_match(act, exp_diff, "strict", args_match=True)
    # args 键序无关（规范化）
    act2 = [{"name": "t", "args": {"b": 2, "a": 1}}]
    exp2 = [{"name": "t", "args": {"a": 1, "b": 2}}]
    assert ae.trajectory_match(act2, exp2, "strict", args_match=True)
    # 期望项无 args → 不限参数
    assert ae.trajectory_match(act, ["search_knowledge"], "strict", args_match=True)


# ============================================
# M2 轨迹提取
# ============================================
def test_trace_trajectory(mem_db):
    tid = obs.start_trace("chat", "u1", "问题")
    obs.record_tool("search_knowledge#step1", {"query": "q1"}, "结果1")
    obs.record_llm("plan", [{"role": "user", "content": "q"}], "计划：先检索再回答")
    obs.record_tool("web_search#step2", {"query": "q2"}, "结果2", status="error")
    obs.record_llm("decision#1", [], "最终答案文本")
    obs.end_trace(status="ok")
    traj = ae.trace_trajectory(tid)
    assert [t["name"] for t in traj["tools"]] == ["search_knowledge", "web_search"]
    assert traj["tools"][0]["args"] == {"query": "q1"}
    assert traj["tools"][1]["status"] == "error"
    assert traj["has_plan"] is True
    assert traj["plan_text"] == "计划：先检索再回答"
    assert traj["answer"] == "最终答案文本"


# ============================================
# M3 六指标 + 降级 + plan 缺省
# ============================================
def test_score_trace_metrics_and_no_plan_default(mem_db, monkeypatch):
    _judge_mock(monkeypatch)
    scores = asyncio.run(ae.score_trace(
        "问题", "答案", _tools("search_knowledge", "web_search"),
        has_plan=False, expected_tools=["search_knowledge"], match_mode="superset"))
    assert scores["task_completion"] == 0.8
    assert scores["tool_correctness"] is not None
    assert scores["argument_correctness"] == 1.0     # verdicts [1,1] / 2 工具
    assert scores["step_efficiency"] == 0.8
    assert scores["trajectory_judge"] == 0.8
    assert scores["plan_quality"] == 1.0             # 无 plan → 默认 1
    assert scores["plan_adherence"] == 1.0
    assert scores["trajectory_match"] == 1
    assert scores["metric_details"]["plan"]["basis"] == "no_plan_default"


def test_score_trace_degrades_on_llm_failure(mem_db, monkeypatch):
    _judge_mock(monkeypatch, raise_exc=True)
    scores = asyncio.run(ae.score_trace("问题", "答案", _tools("search_knowledge")))
    assert scores["task_completion"] is None
    assert scores["trajectory_judge"] is None
    assert scores["argument_correctness"] is None
    # 确定性部分仍可用：工具成功率 1.0、无重复 → 效率 1.0
    assert scores["tool_correctness"] == 1.0
    assert scores["step_efficiency"] == 1.0
    assert scores["plan_quality"] == 1.0
    assert "error" in scores["metric_details"]["task_completion"]


def test_score_trace_duplicate_penalty(mem_db, monkeypatch):
    _judge_mock(monkeypatch, payload={"score": 0.9, "reason": "ok", "verdicts": [1, 1]})
    dup = _tools("search_knowledge", "search_knowledge")   # 同名同 args 重复
    scores = asyncio.run(ae.score_trace("问题", "答案", dup))
    assert scores["step_efficiency"] == 0.5                # min(0.9, 1/2)
    assert scores["metric_details"]["step_efficiency"]["duplicate_penalty"] == 0.5


def test_score_trace_with_plan(mem_db, monkeypatch):
    _judge_mock(monkeypatch, payload={"score": 0.7, "reason": "计划合理", "verdicts": [1]})
    scores = asyncio.run(ae.score_trace(
        "问题", "答案", _tools("search_knowledge"),
        has_plan=True, plan_text="计划文本"))
    assert scores["plan_quality"] == 0.7
    assert scores["plan_adherence"] == 0.7


# ============================================
# M4 离线 run（mock Agent + 真实 tracer）
# ============================================
def _agent_dataset(with_case=True):
    db.execute("INSERT OR IGNORE INTO eval_datasets (id, name, kind, created_at)"
               " VALUES ('ads','Agent集','agent',?)", (datetime.now().isoformat(),))
    if with_case:
        db.execute(
            "INSERT INTO eval_cases (id, dataset_id, question, expected_tools,"
            " expected_outcome, created_at) VALUES (?,?,?,?,?,?)",
            ("ac1", "ads", "失眠怎么办", json.dumps(["search_knowledge"]),
             "给出食疗建议", datetime.now().isoformat()))
    return "ads"


def _mock_agent(monkeypatch, fail=False):
    from app.services import chat_agent
    calls = []

    async def fake(**kwargs):
        calls.append(kwargs)
        if fail:
            raise RuntimeError("Agent 崩溃")
        obs.record_tool("search_knowledge", {"query": kwargs["query"]}, "检索片段")
        kwargs["result"]["answer"] = "小米粥调理失眠"
        kwargs["result"]["status"] = "ok"
        yield 'data: {"event": "token", "content": "x"}\n\n'
    monkeypatch.setattr(chat_agent, "run_chat_agent", fake)
    return calls


def _drive_run(dataset_id, **kw):
    async def _run():
        run_id = ae.start_agent_run(dataset_id, {"id": "admin"}, **kw)
        await asyncio.gather(*list(ae._BG_TASKS))
        return run_id
    return asyncio.run(_run())


def test_offline_run_end_to_end(mem_db, rc_store, monkeypatch):
    _agent_dataset()
    calls = _mock_agent(monkeypatch)
    _judge_mock(monkeypatch)
    run_id = _drive_run("ads", match_mode="superset")
    assert calls and calls[0]["mode"] == "react"
    assert calls[0]["use_knowledge"] is True and calls[0]["use_search"] is True
    run = db.query_one("SELECT * FROM agent_eval_runs WHERE id = ?", (run_id,))
    assert run["status"] == "finished"
    summary = json.loads(run["summary"])
    assert summary["total"] == 1 and summary["ok"] == 1
    assert summary["match_pass_rate"] == 1.0
    assert summary["avg_task_completion"] == 0.8
    result = db.query_one("SELECT * FROM agent_eval_results WHERE run_id = ?", (run_id,))
    assert result["status"] == "ok"
    assert result["trace_id"]
    assert result["answer"] == "小米粥调理失眠"
    assert json.loads(result["tools_called"])[0]["name"] == "search_knowledge"
    assert result["trajectory_match"] == 1
    assert result["match_mode"] == "superset"
    # trace 真实落库且已结束
    trace = db.query_one("SELECT * FROM agent_traces WHERE id = ?", (result["trace_id"],))
    assert trace["agent"] == "agent_eval" and trace["status"] == "ok"


def test_get_run_nested_shape(mem_db, rc_store, monkeypatch):
    """get_run 返回嵌套 {run, results, dataset_name}（前端 AgentEvalPanel 消费口径，与 RAG 一致）。"""
    _agent_dataset()
    _mock_agent(monkeypatch)
    _judge_mock(monkeypatch)
    run_id = _drive_run("ads")
    data = ae.get_run(run_id)
    assert set(data.keys()) == {"run", "results", "dataset_name"}
    assert data["run"]["id"] == run_id
    assert data["run"]["status"] == "finished"
    assert data["run"]["summary"]["total"] == 1
    assert data["dataset_name"] == "Agent集"
    assert len(data["results"]) == 1
    row = data["results"][0]
    assert isinstance(row["tools_called"], list)
    assert isinstance(row["expected_tools"], list)
    assert isinstance(row["metric_details"], dict)
    assert ae.get_run("not-exist") is None


def test_offline_run_case_error_isolated(mem_db, rc_store, monkeypatch):
    _agent_dataset()
    _mock_agent(monkeypatch, fail=True)
    _judge_mock(monkeypatch)
    run_id = _drive_run("ads")
    run = db.query_one("SELECT * FROM agent_eval_runs WHERE id = ?", (run_id,))
    assert run["status"] == "finished"               # 单用例错误不中断 run
    result = db.query_one("SELECT * FROM agent_eval_results WHERE run_id = ?", (run_id,))
    assert result["status"] == "error"
    assert "Agent 崩溃" in result["error"]
    assert json.loads(run["summary"])["error"] == 1


def test_offline_run_validation(mem_db, monkeypatch):
    from app.services.evaluation import ConcurrentRunError
    _agent_dataset(with_case=False)
    with pytest.raises(ValueError):
        ae.start_agent_run("ads", {})                # 无用例
    with pytest.raises(ValueError):
        ae.start_agent_run("不存在", {})
    db.execute("INSERT INTO eval_datasets (id, name, kind, created_at)"
               " VALUES ('rds','RAG集','rag',?)", (datetime.now().isoformat(),))
    db.execute("INSERT INTO eval_cases (id, dataset_id, question, created_at)"
               " VALUES ('rc1','rds','q',?)", (datetime.now().isoformat(),))
    with pytest.raises(ValueError):
        ae.start_agent_run("rds", {})                # kind 非 agent
    _agent_dataset()
    _mock_agent(monkeypatch)
    _judge_mock(monkeypatch)
    db.execute("INSERT INTO agent_eval_runs (id, dataset_id, status, started_at)"
               " VALUES ('running1','ads','running',?)", (datetime.now().isoformat(),))
    with pytest.raises(ConcurrentRunError):
        ae.start_agent_run("ads", {})
    with pytest.raises(ValueError):
        ae.start_agent_run("ads", {}, match_mode="bad-mode")


# ============================================
# M5 在线采样
# ============================================
def _chat_trace(i, status="ok"):
    tid = f"ct{i}"
    ts = datetime.now().isoformat()
    db.execute(
        "INSERT INTO agent_traces (id, agent, user_id, input, status, started_at, ended_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (tid, "chat", "u1", f"问题{i}", status, ts, ts))
    obs.start_trace("chat", "u1", f"问题{i}")        # 仅为挂 span（复用当前上下文）
    return tid


def _seed_trace_with_span(i):
    """直接插 trace + span（不依赖 contextvar）。"""
    tid = f"ct{i}"
    ts = datetime.now().isoformat()
    db.execute(
        "INSERT INTO agent_traces (id, agent, user_id, input, status, started_at, ended_at)"
        " VALUES (?,?,?,?,?,?,?)", (tid, "chat", "u1", f"问题{i}", "ok", ts, ts))
    db.execute(
        "INSERT INTO agent_spans (id, trace_id, seq, kind, name, input, output, status, started_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (f"sp{i}", tid, 1, "tool", "search_knowledge", '{"query": "q"}', "结果", "ok", ts))
    db.execute(
        "INSERT INTO agent_spans (id, trace_id, seq, kind, name, input, output, status, started_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (f"sl{i}", tid, 2, "llm", "decision#1", "", f"答案{i}", "ok", ts))
    return tid


def test_online_round_cap_and_cursor(mem_db, rc_store, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_ONLINE_SAMPLE_RATE", 1.0)
    monkeypatch.setattr(settings, "AGENT_ONLINE_MAX_PER_ROUND", 2)
    _judge_mock(monkeypatch)
    for i in range(3):
        _seed_trace_with_span(i)
    result = asyncio.run(ae.run_round())
    assert result["enabled"] is True
    assert result["candidates"] == 3
    assert result["evaluated"] == 2                  # 单轮上限
    assert rc_store.get(ae._CURSOR_KEY)              # 游标推进
    rows = db.query("SELECT * FROM agent_online_evals")
    assert len(rows) == 2
    assert all(r["status"] == "ok" and r["agent"] == "chat" for r in rows)
    assert rows[0]["task_completion"] == 0.8
    # 第二轮：游标后无新 trace
    result2 = asyncio.run(ae.run_round())
    assert result2["candidates"] == 0


def test_online_round_disabled(mem_db, rc_store, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_ONLINE_EVAL_ENABLED", False)
    _seed_trace_with_span(1)
    result = asyncio.run(ae.run_round())
    assert result["enabled"] is False
    assert db.query_one("SELECT COUNT(*) AS n FROM agent_online_evals")["n"] == 0


def test_online_skips_running_traces(mem_db, rc_store, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_ONLINE_SAMPLE_RATE", 1.0)
    _judge_mock(monkeypatch)
    ts = datetime.now().isoformat()
    db.execute(
        "INSERT INTO agent_traces (id, agent, status, started_at, ended_at)"
        " VALUES ('running','chat','running',?,'')", (ts,))
    result = asyncio.run(ae.run_round())
    assert result["candidates"] == 0


# ============================================
# M6 手动单 trace 评测
# ============================================
def test_evaluate_trace_manual_with_expected(mem_db, monkeypatch):
    _judge_mock(monkeypatch)
    tid = _seed_trace_with_span(1)
    result = asyncio.run(ae.evaluate_trace(
        tid, expected_tools=["search_knowledge"], match_mode="strict"))
    assert result["status"] == "ok"
    assert result["trajectory_match"] == 1
    assert result["task_completion"] == 0.8
    with pytest.raises(ValueError):
        asyncio.run(ae.evaluate_trace("不存在"))
    with pytest.raises(ValueError):
        asyncio.run(ae.evaluate_trace(tid, match_mode="bad"))


# ============================================
# M7 聚合 + 脱敏
# ============================================
def test_aggregate_and_list_redaction(mem_db, monkeypatch):
    _judge_mock(monkeypatch)
    tid = _seed_trace_with_span(1)
    db.execute("UPDATE agent_spans SET output = '联系13812345678的答案' WHERE id = 'sl1'")
    asyncio.run(ae.evaluate_trace(tid))
    agg = ae.aggregate(7)
    assert agg["samples"] == 1
    assert agg["avg_task_completion"] == 0.8
    listing = ae.list_online()
    assert listing["total"] == 1
    item = listing["items"][0]
    assert "13812345678" not in item["answer"]
    assert "[手机号已脱敏]" in item["answer"]
    assert item["tools_called"][0]["name"] == "search_knowledge"