"""
见字如面 - Agent 评测框架（AgentEvals 轨迹匹配 + DeepEval Agent 六指标）

双模式：
- 离线：kind=agent 评测集逐用例真实运行 Agent（run_chat_agent react 模式，独立
  agent_eval trace），消费 SSE 至结束后对轨迹评分，写 agent_eval_runs/results；
- 在线：游标后已结束的 chat trace 按采样率抽样，跑无参考指标集，写 agent_online_evals，
  聚合回流可观测性质量指标与告警规则。

框架映射（自研实现，零新增重依赖）：
- AgentEvals：trajectory match 四模式 strict/unordered/subset/superset（默认工具名
  匹配，可选 args 规范化匹配）+ trajectory LLM-as-judge（无参考整体评分）；
- DeepEval Agent 三层：执行层 TaskCompletion/StepEfficiency（轨迹级）、动作层
  ToolCorrectness/ArgumentCorrectness（组件级）、推理层 PlanQuality/PlanAdherence
  （仅 trace 含 name='plan' 的 llm span 时评估，否则按 DeepEval 默认 1）。

设计要点：LLM 裁判单点失败/超时降级 None（不计均值）不中断 run；离线并发 1 串行
防 LLM 风暴；原始文本落库不改写，API 返回前经 redact_log_text 脱敏。
"""
import asyncio
import json
import logging
import random
import time
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from app.config import settings
from app.core import db
from app.core.llm import LLMFactory

logger = logging.getLogger("jianziruyang.agent_eval")

_PERIODIC_TASK: Optional[asyncio.Task] = None
_BG_TASKS: set = set()
_CURSOR_KEY = "agent_online_eval_cursor"

METRIC_TIMEOUT = 30
MATCH_MODES = ("strict", "unordered", "subset", "superset")
AVAILABLE_TOOLS = ["search_knowledge", "web_search"]


def _now() -> str:
    return datetime.now().isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _loads(raw, default):
    try:
        value = json.loads(raw) if (raw or "").strip() else default
    except (json.JSONDecodeError, TypeError):
        return default
    return value if isinstance(value, type(default)) else default


# ---------------------------------------------------------------------------
# 轨迹提取（agent_spans → 工具序列 / plan 检测 / 最终答案）
# ---------------------------------------------------------------------------
def trace_trajectory(trace_id: str) -> dict:
    """trace → {tools:[{name,args,output,status}], has_plan, plan_text, answer, trace}。

    工具名归一化：plan_execute 模式的 `tool#stepN` 还原为裸工具名；
    最终答案取 trace 末个有输出的 llm span（离线运行优先用 result 回填答案）。
    """
    spans = db.query(
        "SELECT kind, name, input, output, status FROM agent_spans"
        " WHERE trace_id = ? ORDER BY seq, rowid", (trace_id,))
    tools, has_plan, plan_text, last_llm_output = [], False, "", ""
    for s in spans:
        if s["kind"] == "tool":
            args = _loads(s.get("input"), {})
            tools.append({
                "name": (s.get("name") or "").split("#")[0],
                "args": args,
                "output": (s.get("output") or "")[:2000],
                "status": s.get("status") or "ok",
            })
        elif s["kind"] == "llm":
            if s.get("name") == "plan":
                has_plan = True
                plan_text = s.get("output") or ""
            if (s.get("output") or "").strip():
                last_llm_output = s["output"]
    trace = db.query_one("SELECT * FROM agent_traces WHERE id = ?", (trace_id,))
    return {"tools": tools, "has_plan": has_plan, "plan_text": plan_text,
            "answer": last_llm_output, "trace": trace}


# ---------------------------------------------------------------------------
# AgentEvals 映射：trajectory match 四模式（默认工具名匹配，可选 args 匹配）
# ---------------------------------------------------------------------------
def _norm_args(args) -> str:
    return json.dumps(args or {}, sort_keys=True, ensure_ascii=False)


def _expected_items(expected) -> List[dict]:
    """期望序列元素：str=工具名；dict={name, args}（args 缺省=None 表示不限参数）。"""
    items = []
    for e in expected or []:
        if isinstance(e, dict):
            items.append({"name": str(e.get("name") or ""), "args": e.get("args")})
        elif str(e).strip():
            items.append({"name": str(e).strip(), "args": None})
    return items


def trajectory_match(actual: List[dict], expected, mode: str = "superset",
                     args_match: bool = False) -> bool:
    """四模式轨迹匹配：strict(有序相等)/unordered(多重集相等)/subset(实际⊆期望)/
    superset(实际⊇期望)。args_match 时期望项带 args 的按规范化 JSON 相等比较。"""
    exp = _expected_items(expected)
    act = [{"name": t.get("name") or "", "args": t.get("args")} for t in actual or []]

    def _pair_ok(a: dict, e: dict) -> bool:
        if a["name"] != e["name"]:
            return False
        if args_match and e["args"] is not None:
            return _norm_args(a["args"]) == _norm_args(e["args"])
        return True

    if mode == "strict":
        return len(act) == len(exp) and all(_pair_ok(a, e) for a, e in zip(act, exp))
    if mode == "unordered":
        if len(act) != len(exp):
            return False
        remaining = list(exp)
        for a in act:
            idx = next((i for i, e in enumerate(remaining) if _pair_ok(a, e)), None)
            if idx is None:
                return False
            remaining.pop(idx)
        return True
    if mode == "subset":
        return all(any(_pair_ok(a, e) for e in exp) for a in act)
    if mode == "superset":
        return all(any(_pair_ok(a, e) for a in act) for e in exp)
    return False


# ---------------------------------------------------------------------------
# DeepEval 映射：LLM 裁判（assistant 角色，超时/失败降级 None）
# ---------------------------------------------------------------------------
async def _judge_json(messages: List[dict]) -> dict:
    llm = LLMFactory.create_for_role("assistant")
    collected: List[str] = []

    async def _call():
        async for chunk in llm.chat(messages, stream=True):
            collected.append(chunk)

    await asyncio.wait_for(_call(), timeout=METRIC_TIMEOUT)
    from app.services import wiki
    return wiki._extract_json("".join(collected))


def _score01(value) -> Optional[float]:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, min(1.0, score)), 4)


def _steps_summary(tools: List[dict]) -> str:
    if not tools:
        return "（无工具调用）"
    lines = []
    for i, t in enumerate(tools[:20]):
        args = json.dumps(t.get("args") or {}, ensure_ascii=False)[:120]
        lines.append(f"{i + 1}. {t['name']}({args}) → {t.get('status') or 'ok'}")
    return "\n".join(lines)


async def _m_task_completion(question: str, answer: str, tools: List[dict]) -> tuple:
    """执行层：任务完成度（LLM judge：task=input，outcome=答案+步骤摘要）。"""
    messages = [
        {"role": "system", "content": (
            "你是 Agent 任务完成度评审。给定用户任务与 Agent 的最终回答及执行步骤，"
            "判断任务是否被完整、正确地完成。只输出 JSON：\n"
            '{"score": 0.0到1.0的小数, "reason": "一句话理由"}')},
        {"role": "user", "content": (
            f"【任务】{question[:800]}\n\n【最终回答】{(answer or '（空）')[:1500]}\n\n"
            f"【执行步骤】\n{_steps_summary(tools)}")},
    ]
    data = await _judge_json(messages)
    return _score01(data.get("score")), {"reason": str(data.get("reason") or "")[:300]}


async def _m_tool_correctness(question: str, tools: List[dict], expected,
                              args_match: bool) -> tuple:
    """动作层：工具正确性。有期望=正确调用数/总调用数；无期望=工具成功率；
    可选 LLM 最优性（可用工具集约束）取 min。"""
    if not tools:
        return (1.0 if not expected else 0.0), {"basis": "no_tools"}
    exp = _expected_items(expected)
    if exp:
        remaining = list(exp)
        correct = 0
        for t in tools:
            idx = next((i for i, e in enumerate(remaining)
                        if trajectory_match([t], [e], "strict", args_match)), None)
            if idx is not None:
                correct += 1
                remaining.pop(idx)
        score = round(correct / len(tools), 4)
        detail = {"basis": "expected", "correct": correct, "total": len(tools)}
    else:
        ok = sum(1 for t in tools if (t.get("status") or "ok") == "ok")
        score = round(ok / len(tools), 4)
        detail = {"basis": "success_rate", "ok": ok, "total": len(tools)}
    # LLM 最优性：调用是否必要（可用工具集外的调用视为不必要）
    try:
        messages = [
            {"role": "system", "content": (
                "你是 Agent 工具使用评审。可用工具集为 "
                f"{json.dumps(AVAILABLE_TOOLS, ensure_ascii=False)}。给定任务与工具调用序列，"
                "逐次判断该调用对完成任务是否必要且工具选择恰当。只输出 JSON：\n"
                '{"verdicts": [1, 0]}（1=必要恰当，0=冗余或选择不当；长度与调用数一致）')},
            {"role": "user", "content": (
                f"【任务】{question[:800]}\n\n【调用序列】\n{_steps_summary(tools)}")},
        ]
        data = await _judge_json(messages)
        verdicts = data.get("verdicts") or []
        if isinstance(verdicts, list) and verdicts:
            vals = [1 if v in (1, True, "1") else 0 for v in verdicts[:len(tools)]]
            llm_score = round(sum(vals) / len(tools), 4)
            score = min(score, llm_score)
            detail["llm_optimality"] = llm_score
    except Exception as e:
        detail["llm_optimality_degraded"] = str(e)[:120]
    return score, detail


async def _m_argument_correctness(question: str, tools: List[dict]) -> tuple:
    """动作层：参数正确性（无参考）：逐调用判 args 是否由任务逻辑导出 → 比例。"""
    if not tools:
        return 1.0, {"basis": "no_tools"}
    calls = "\n".join(
        f"{i + 1}. {t['name']} 参数={json.dumps(t.get('args') or {}, ensure_ascii=False)[:200]}"
        for i, t in enumerate(tools[:20]))
    messages = [
        {"role": "system", "content": (
            "你是 Agent 工具参数评审。给定用户任务与逐次工具调用的参数，"
            "判断每次调用的参数是否由任务逻辑合理导出（查询词贴切、无臆造约束）。只输出 JSON：\n"
            '{"verdicts": [1, 0]}（1=合理导出，0=不合理；长度与调用数一致）')},
        {"role": "user", "content": f"【任务】{question[:800]}\n\n【调用参数】\n{calls}"},
    ]
    data = await _judge_json(messages)
    verdicts = data.get("verdicts") or []
    if not isinstance(verdicts, list) or not verdicts:
        return None, {"error": "empty_verdicts"}
    vals = [1 if v in (1, True, "1") else 0 for v in verdicts[:len(tools)]]
    return round(sum(vals) / len(tools), 4), {"verdicts": vals}


async def _m_step_efficiency(question: str, tools: List[dict]) -> tuple:
    """执行层：步骤效率 = min(LLM 冗余判定, 确定性重复调用惩罚)。"""
    if not tools:
        return 1.0, {"basis": "no_tools"}
    seen, unique = set(), 0
    for t in tools:
        key = (t["name"], _norm_args(t.get("args")))
        if key not in seen:
            seen.add(key)
            unique += 1
    deterministic = round(unique / len(tools), 4)
    detail = {"duplicate_penalty": deterministic, "unique": unique, "total": len(tools)}
    try:
        messages = [
            {"role": "system", "content": (
                "你是 Agent 执行效率评审。给定任务与执行步骤，判断轨迹是否简洁高效"
                "（无冗余往返、无无关步骤）。只输出 JSON：\n"
                '{"score": 0.0到1.0的小数, "reason": "一句话理由"}')},
            {"role": "user", "content": (
                f"【任务】{question[:800]}\n\n【执行步骤】\n{_steps_summary(tools)}")},
        ]
        data = await _judge_json(messages)
        llm_score = _score01(data.get("score"))
        if llm_score is not None:
            detail["llm_efficiency"] = llm_score
            detail["reason"] = str(data.get("reason") or "")[:300]
            return min(deterministic, llm_score), detail
    except Exception as e:
        detail["llm_degraded"] = str(e)[:120]
    return deterministic, detail


async def _m_trajectory_judge(question: str, answer: str, tools: List[dict]) -> tuple:
    """AgentEvals 式整体轨迹 LLM 评分（无参考）。"""
    messages = [
        {"role": "system", "content": (
            "你是 Agent 轨迹整体评审。综合任务、执行步骤与最终回答，"
            "对整条轨迹的质量打分（推理连贯、工具得当、回答可靠）。只输出 JSON：\n"
            '{"score": 0.0到1.0的小数, "reason": "一句话理由"}')},
        {"role": "user", "content": (
            f"【任务】{question[:800]}\n\n【执行步骤】\n{_steps_summary(tools)}\n\n"
            f"【最终回答】{(answer or '（空）')[:1500]}")},
    ]
    data = await _judge_json(messages)
    return _score01(data.get("score")), {"reason": str(data.get("reason") or "")[:300]}


async def _m_plan(question: str, tools: List[dict], answer: str,
                  has_plan: bool, plan_text: str) -> tuple:
    """推理层：PlanQuality/PlanAdherence。无 plan span → 默认 1（DeepEval 惯例）。"""
    if not has_plan:
        return 1.0, 1.0, {"basis": "no_plan_default"}
    detail = {}
    pq = pa = None
    try:
        messages = [
            {"role": "system", "content": (
                "你是 Agent 规划质量评审。给定任务与 Agent 制定的计划，"
                "判断计划是否完整、可行、覆盖任务要点。只输出 JSON：\n"
                '{"score": 0.0到1.0的小数, "reason": "一句话理由"}')},
            {"role": "user", "content": (
                f"【任务】{question[:800]}\n\n【计划】{plan_text[:1500]}")},
        ]
        data = await _judge_json(messages)
        pq = _score01(data.get("score"))
        detail["plan_quality_reason"] = str(data.get("reason") or "")[:300]
    except Exception as e:
        detail["plan_quality_degraded"] = str(e)[:120]
    try:
        messages = [
            {"role": "system", "content": (
                "你是 Agent 计划执行评审。给定计划与实际执行步骤/最终回答，"
                "判断执行是否忠实遵循计划。只输出 JSON：\n"
                '{"score": 0.0到1.0的小数, "reason": "一句话理由"}')},
            {"role": "user", "content": (
                f"【计划】{plan_text[:1200]}\n\n【执行步骤】\n{_steps_summary(tools)}\n\n"
                f"【最终回答】{(answer or '（空）')[:800]}")},
        ]
        data = await _judge_json(messages)
        pa = _score01(data.get("score"))
        detail["plan_adherence_reason"] = str(data.get("reason") or "")[:300]
    except Exception as e:
        detail["plan_adherence_degraded"] = str(e)[:120]
    return pq, pa, detail


async def score_trace(question: str, answer: str, tools: List[dict],
                      has_plan: bool = False, plan_text: str = "",
                      expected_tools=None, args_match: bool = False,
                      match_mode: str = "superset") -> dict:
    """对一条轨迹跑六指标（并发，单点降级 None）+ 可选轨迹匹配。

    返回 {task_completion, tool_correctness, argument_correctness, step_efficiency,
    plan_quality, plan_adherence, trajectory_judge, trajectory_match, metric_details}。
    trajectory_match 仅在有期望工具序列时计算（0/1），否则 None。
    """
    results = await asyncio.gather(
        _m_task_completion(question, answer, tools),
        _m_tool_correctness(question, tools, expected_tools, args_match),
        _m_argument_correctness(question, tools),
        _m_step_efficiency(question, tools),
        _m_trajectory_judge(question, answer, tools),
        _m_plan(question, tools, answer, has_plan, plan_text),
        return_exceptions=True,
    )
    names = ("task_completion", "tool_correctness", "argument_correctness",
             "step_efficiency", "trajectory_judge")
    scores, details = {}, {}
    for name, res in zip(names, results[:5]):
        if isinstance(res, Exception):
            scores[name], details[name] = None, {"error": str(res)[:200]}
        else:
            scores[name], details[name] = res
    plan_res = results[5]
    if isinstance(plan_res, Exception):
        scores["plan_quality"] = scores["plan_adherence"] = None
        details["plan"] = {"error": str(plan_res)[:200]}
    else:
        scores["plan_quality"], scores["plan_adherence"], details["plan"] = plan_res
    match = None
    if expected_tools:
        match = 1 if trajectory_match(tools, expected_tools, match_mode, args_match) else 0
    return {**scores, "trajectory_match": match, "metric_details": details}


# ---------------------------------------------------------------------------
# 离线：kind=agent 评测集真实运行 Agent → 轨迹评分
# ---------------------------------------------------------------------------
def start_agent_run(dataset_id: str, user: dict, match_mode: str = "superset",
                    args_match: bool = False) -> str:
    """发起 Agent 离线评测（后台逐用例真实运行，立即返回 run_id）。"""
    from app.services.evaluation import ConcurrentRunError
    dataset = db.query_one("SELECT * FROM eval_datasets WHERE id = ?", (dataset_id,))
    if not dataset:
        raise ValueError("评测集不存在")
    if (dataset.get("kind") or "rag") != "agent":
        raise ValueError("该评测集不是 Agent 类型（kind=agent）")
    if match_mode not in MATCH_MODES:
        raise ValueError(f"无效匹配模式（{'/'.join(MATCH_MODES)}）")
    cases = db.query(
        "SELECT * FROM eval_cases WHERE dataset_id = ? ORDER BY created_at, rowid",
        (dataset_id,))
    if not cases:
        raise ValueError("评测集内没有用例，无法发起评测")
    if db.query_one("SELECT id FROM agent_eval_runs WHERE dataset_id = ? AND status = 'running'",
                    (dataset_id,)):
        raise ConcurrentRunError("该评测集已有正在执行的 Agent 评测，请等待完成")
    run_id = _new_id()
    user_id = (user or {}).get("id") or (user or {}).get("username") or ""
    db.execute(
        "INSERT INTO agent_eval_runs (id, dataset_id, status, summary, created_by, started_at)"
        " VALUES (?,?,?,?,?,?)", (run_id, dataset_id, "running", "{}", user_id, _now()))
    task = asyncio.create_task(_run_all(run_id, cases, user_id, match_mode, args_match))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    logger.info("[Agent评测] 发起离线 run=%s dataset=%s cases=%d mode=%s",
                run_id, dataset_id, len(cases), match_mode)
    return run_id


def _insert_result(run_id: str, case: dict, r: dict) -> None:
    db.execute(
        "INSERT INTO agent_eval_results (id, run_id, case_id, question, trace_id, answer,"
        " tools_called, expected_tools, match_mode, trajectory_match, task_completion,"
        " tool_correctness, argument_correctness, step_efficiency, plan_quality,"
        " plan_adherence, trajectory_judge, metric_details, latency_ms, status, error)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (_new_id(), run_id, case["id"], r.get("question") or "", r.get("trace_id") or "",
         (r.get("answer") or "")[:4000],
         json.dumps(r.get("tools_called") or [], ensure_ascii=False)[:8000],
         json.dumps(r.get("expected_tools") or [], ensure_ascii=False),
         r.get("match_mode") or "superset", int(r.get("trajectory_match") or 0),
         r.get("task_completion"), r.get("tool_correctness"),
         r.get("argument_correctness"), r.get("step_efficiency"),
         r.get("plan_quality"), r.get("plan_adherence"), r.get("trajectory_judge"),
         json.dumps(r.get("metric_details") or {}, ensure_ascii=False)[:8000],
         int(r.get("latency_ms") or 0), r.get("status") or "ok",
         (r.get("error") or "")[:500]))


async def _run_case(case: dict, user_id: str, match_mode: str, args_match: bool) -> dict:
    """单用例：真实运行 Agent（独立 trace）→ 轨迹评分；异常/超时降级 error。"""
    from app.core import observability as obs
    from app.core.llm import FINAL_ANSWER_RULES
    from app.services import chat_agent, prompt_store

    question = case["question"]
    expected = _loads(case.get("expected_tools"), [])
    base = {"question": question, "expected_tools": expected, "match_mode": match_mode,
            "trace_id": "", "answer": "", "tools_called": [], "latency_ms": 0}
    started = time.monotonic()
    trace_id = ""
    try:
        trace_id = obs.start_trace("agent_eval", user_id, question)
        base["trace_id"] = trace_id
        result: dict = {}
        system_prompt = [{"role": "system",
                          "content": prompt_store.get_active_body() + FINAL_ANSWER_RULES}]

        async def _consume():
            async for _ev in chat_agent.run_chat_agent(
                    mode="react", query=question, user_id=user_id, rule_id="",
                    session_id="", use_knowledge=True, use_search=True,
                    skip_search_prompt=False, history_ctx=[],
                    system_prompt=system_prompt, result=result):
                pass

        timeout = max(10, int(getattr(settings, "AGENT_EVAL_CASE_TIMEOUT", 180)))
        await asyncio.wait_for(_consume(), timeout=timeout)
        status = result.get("status") or "ok"
        obs.end_trace(status="ok" if status != "error" else "error",
                      error=result.get("error") or "")
        answer = result.get("answer") or ""
        traj = trace_trajectory(trace_id)
        scores = await score_trace(
            question, answer, traj["tools"], traj["has_plan"], traj["plan_text"],
            expected, args_match, match_mode)
        return {**base, "answer": answer, "tools_called": traj["tools"],
                "latency_ms": int((time.monotonic() - started) * 1000),
                "status": "error" if status == "error" else "ok",
                "error": (result.get("error") or "")[:500], **scores}
    except Exception as e:
        if trace_id:
            try:
                obs.end_trace(status="error", error=str(e))
            except Exception:
                pass
        logger.warning("[Agent评测] 用例执行失败 case=%s: %s", case.get("id"), e)
        return {**base, "latency_ms": int((time.monotonic() - started) * 1000),
                "status": "error", "error": str(e)[:500],
                "task_completion": None, "tool_correctness": None,
                "argument_correctness": None, "step_efficiency": None,
                "plan_quality": None, "plan_adherence": None,
                "trajectory_judge": None, "trajectory_match": 0,
                "metric_details": {"error": str(e)[:200]}}


def _compute_summary(results: List[dict]) -> dict:
    ok = [r for r in results if r.get("status") == "ok"]
    n = len(ok)

    def _avg(key):
        vals = [r[key] for r in ok if r.get(key) is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    matched = [r for r in ok if r.get("trajectory_match") is not None]
    return {
        "total": len(results),
        "ok": n,
        "error": len(results) - n,
        "avg_task_completion": _avg("task_completion"),
        "avg_tool_correctness": _avg("tool_correctness"),
        "avg_argument_correctness": _avg("argument_correctness"),
        "avg_step_efficiency": _avg("step_efficiency"),
        "avg_plan_quality": _avg("plan_quality"),
        "avg_plan_adherence": _avg("plan_adherence"),
        "avg_trajectory_judge": _avg("trajectory_judge"),
        "match_pass_rate": (round(sum(r["trajectory_match"] for r in matched) / len(matched), 4)
                            if matched else None),
    }


async def _run_all(run_id: str, cases: List[dict], user_id: str,
                   match_mode: str, args_match: bool) -> None:
    """串行逐用例执行（并发 1 防 LLM 风暴），结束写 summary + 触发 BadCase 收集。"""
    results = []
    try:
        for case in cases:
            r = await _run_case(case, user_id, match_mode, args_match)
            _insert_result(run_id, case, r)
            results.append(r)
        summary = _compute_summary(results)
        db.execute(
            "UPDATE agent_eval_runs SET status = 'finished', finished_at = ?, summary = ?"
            " WHERE id = ?", (_now(), json.dumps(summary, ensure_ascii=False), run_id))
        logger.info("[Agent评测] run 完成 %s | %s", run_id, summary)
        try:
            from app.services import badcase
            badcase.collect_round()
        except Exception as e:
            logger.warning("[Agent评测] BadCase 增量收集失败（不阻断）: %s", e)
    except Exception as e:
        logger.error("[Agent评测] run 整体失败 %s: %s", run_id, e)
        db.execute(
            "UPDATE agent_eval_runs SET status = 'failed', finished_at = ?, summary = ?"
            " WHERE id = ?",
            (_now(), json.dumps({"error": str(e)[:500]}, ensure_ascii=False), run_id))


def list_runs(dataset_id: str = "") -> List[dict]:
    where, params = "1=1", []
    if dataset_id:
        where += " AND dataset_id = ?"
        params.append(dataset_id)
    rows = db.query(
        f"SELECT * FROM agent_eval_runs WHERE {where} ORDER BY started_at DESC LIMIT 100",
        tuple(params))
    ds_names = {r["id"]: r["name"] for r in db.query("SELECT id, name FROM eval_datasets")}
    runs = []
    for r in rows:
        completed = db.query_one(
            "SELECT COUNT(*) AS c FROM agent_eval_results WHERE run_id = ?", (r["id"],))["c"]
        runs.append({**r, "summary": _loads(r.get("summary"), {}),
                     "dataset_name": ds_names.get(r["dataset_id"], "(评测集已删除)"),
                     "completed": completed})
    return runs


def get_run(run_id: str) -> Optional[dict]:
    run = db.query_one("SELECT * FROM agent_eval_runs WHERE id = ?", (run_id,))
    if not run:
        return None
    results = db.query(
        "SELECT * FROM agent_eval_results WHERE run_id = ? ORDER BY rowid", (run_id,))
    items = []
    for r in results:
        d = dict(r)
        d["tools_called"] = _loads(d.get("tools_called"), [])
        d["expected_tools"] = _loads(d.get("expected_tools"), [])
        d["metric_details"] = _loads(d.get("metric_details"), {})
        items.append(d)
    ds = db.query_one("SELECT name FROM eval_datasets WHERE id = ?", (run["dataset_id"],))
    # 嵌套结构（与 RAG get_run 一致）：前端 AgentEvalPanel 消费 run/results/dataset_name
    return {
        "run": {**run, "summary": _loads(run.get("summary"), {})},
        "results": items,
        "dataset_name": ds["name"] if ds else "(评测集已删除)",
    }


# ---------------------------------------------------------------------------
# 在线：chat trace 采样评测（无参考指标集）+ 聚合回流
# ---------------------------------------------------------------------------
def agent_online_enabled() -> bool:
    from app.services.config_manager import runtime_config
    value = runtime_config.get("agent_online_eval_enabled")
    default = bool(getattr(settings, "AGENT_ONLINE_EVAL_ENABLED", True))
    return default if value is None else bool(value)


def _pick_traces(cursor: str, batch: int = 50) -> List[dict]:
    where = "agent = 'chat' AND status != 'running' AND ended_at != ''"
    params: list = []
    if cursor:
        where += " AND started_at > ?"
        params.append(cursor)
    return db.query(
        f"SELECT * FROM agent_traces WHERE {where} ORDER BY started_at ASC LIMIT ?",
        (*params, batch))


async def _eval_trace_row(trace: dict, expected_tools=None, match_mode: str = "superset",
                          args_match: bool = False) -> dict:
    """对单条 trace 评分并写 agent_online_evals；返回落库行。"""
    traj = trace_trajectory(trace["id"])
    question = trace.get("input") or ""
    answer = traj["answer"]
    row_id = _new_id()
    try:
        scores = await score_trace(
            question, answer, traj["tools"], traj["has_plan"], traj["plan_text"],
            expected_tools, args_match, match_mode)
        status, error = "ok", ""
    except Exception as e:
        logger.warning("[Agent在线] 轨迹评分失败 trace=%s: %s", trace["id"], e)
        scores, status, error = {}, "error", str(e)[:500]
    tools_json = json.dumps(traj["tools"], ensure_ascii=False)[:8000]
    db.execute(
        "INSERT INTO agent_online_evals (id, created_at, trace_id, agent, user_id, input,"
        " answer, tools_called, task_completion, tool_correctness, argument_correctness,"
        " step_efficiency, plan_quality, plan_adherence, trajectory_judge, metric_details,"
        " status, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (row_id, _now(), trace["id"], trace.get("agent") or "", trace.get("user_id") or "",
         question[:2000], answer[:4000], tools_json,
         scores.get("task_completion"), scores.get("tool_correctness"),
         scores.get("argument_correctness"), scores.get("step_efficiency"),
         scores.get("plan_quality"), scores.get("plan_adherence"),
         scores.get("trajectory_judge"),
         json.dumps(scores.get("metric_details") or {}, ensure_ascii=False)[:8000],
         status, error))
    return {"id": row_id, "trace_id": trace["id"], "status": status, **scores}


async def run_round() -> dict:
    """一轮在线 trace 采样评测（周期任务与测试共用入口）。"""
    from app.services.config_manager import runtime_config
    if not agent_online_enabled():
        return {"enabled": False, "evaluated": 0}
    cursor = str(runtime_config.get(_CURSOR_KEY) or "")
    candidates = await asyncio.to_thread(_pick_traces, cursor)
    if not candidates:
        return {"enabled": True, "evaluated": 0, "candidates": 0}
    rate = max(0.0, min(1.0, float(getattr(settings, "AGENT_ONLINE_SAMPLE_RATE", 0.1))))
    cap = max(1, int(getattr(settings, "AGENT_ONLINE_MAX_PER_ROUND", 5)))
    sampled = [t for t in candidates if random.random() < rate][:cap]
    evaluated = 0
    for trace in sampled:
        try:
            row = await _eval_trace_row(trace)
            if row.get("status") == "ok":
                evaluated += 1
        except Exception as e:
            logger.warning("[Agent在线] 单条评测异常（跳过）: %s", e)
    new_cursor = max(t.get("started_at") or "" for t in candidates)
    if new_cursor > cursor:
        runtime_config.set(_CURSOR_KEY, new_cursor)
    logger.info("[Agent在线] 本轮候选 %d 条，采样评测 %d 条", len(candidates), evaluated)
    return {"enabled": True, "candidates": len(candidates), "evaluated": evaluated}


async def evaluate_trace(trace_id: str, expected_tools=None, match_mode: str = "superset",
                         args_match: bool = False) -> dict:
    """手动单 trace 评测（可附期望工具序列）；trace 不存在抛 ValueError。"""
    trace = db.query_one("SELECT * FROM agent_traces WHERE id = ?", (trace_id,))
    if not trace:
        raise ValueError("trace 不存在")
    if match_mode not in MATCH_MODES:
        raise ValueError(f"无效匹配模式（{'/'.join(MATCH_MODES)}）")
    return await _eval_trace_row(trace, expected_tools, match_mode, args_match)


def aggregate(days: int = 7) -> dict:
    """近 N 天在线 Agent 评测聚合（质量指标回流 + 告警规则消费）。"""
    since = (datetime.now() - timedelta(days=max(1, days) - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat()
    row = db.query_one(
        "SELECT COUNT(*) AS n, AVG(task_completion) AS tc, AVG(tool_correctness) AS tcor,"
        " AVG(step_efficiency) AS se, AVG(trajectory_judge) AS tj,"
        " COALESCE(SUM(task_completion < 0.6), 0) AS low"
        " FROM agent_online_evals WHERE created_at >= ? AND status = 'ok'", (since,)) or {}
    n = int(row.get("n") or 0)

    def _avg(key):
        return round(float(row[key]), 4) if row.get(key) is not None else None

    return {"samples": n, "avg_task_completion": _avg("tc"),
            "avg_tool_correctness": _avg("tcor"), "avg_step_efficiency": _avg("se"),
            "avg_trajectory_judge": _avg("tj"), "low_count": int(row.get("low") or 0)}


def list_online(limit: int = 50, offset: int = 0) -> dict:
    """在线 Agent 评测明细（返回前脱敏，原始落库不改写）。"""
    from app.core.log_setup import redact_log_text
    total = int((db.query_one(
        "SELECT COUNT(*) AS n FROM agent_online_evals") or {}).get("n") or 0)
    rows = db.query(
        "SELECT * FROM agent_online_evals ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (max(1, min(200, limit)), max(0, offset)))
    items = []
    for r in rows:
        items.append({
            "id": r["id"],
            "created_at": (r.get("created_at") or "")[:16],
            "trace_id": r.get("trace_id") or "",
            "agent": r.get("agent") or "",
            "input": redact_log_text((r.get("input") or "").replace("\n", " ")[:120]),
            "answer": redact_log_text((r.get("answer") or "").replace("\n", " ")[:200]),
            "tools_called": _loads(r.get("tools_called"), []),
            "task_completion": r.get("task_completion"),
            "tool_correctness": r.get("tool_correctness"),
            "argument_correctness": r.get("argument_correctness"),
            "step_efficiency": r.get("step_efficiency"),
            "plan_quality": r.get("plan_quality"),
            "plan_adherence": r.get("plan_adherence"),
            "trajectory_judge": r.get("trajectory_judge"),
            "metric_details": _loads(r.get("metric_details"), {}),
            "status": r.get("status") or "ok",
        })
    return {"items": items, "total": total}


# ---------------------------------------------------------------------------
# 周期任务（lifespan 启停；测试环境不启动，显式调用 run_round）
# ---------------------------------------------------------------------------
async def _periodic_loop():
    interval = max(60, int(getattr(settings, "AGENT_ONLINE_INTERVAL_SECONDS", 300)))
    while True:
        await asyncio.sleep(interval)
        try:
            await run_round()
        except Exception as e:
            logger.warning("[Agent在线] 周期轮异常（下轮重试）: %s", e)


def start_periodic() -> None:
    global _PERIODIC_TASK
    if not agent_online_enabled():
        return
    if _PERIODIC_TASK and not _PERIODIC_TASK.done():
        return
    try:
        _PERIODIC_TASK = asyncio.create_task(_periodic_loop())
        logger.info("[Agent在线] 周期评测任务已启动（间隔 %ds，采样率 %.2f）",
                    getattr(settings, "AGENT_ONLINE_INTERVAL_SECONDS", 300),
                    getattr(settings, "AGENT_ONLINE_SAMPLE_RATE", 0.1))
    except RuntimeError:
        logger.warning("[Agent在线] 无运行中的事件循环，周期任务未启动")


async def stop_periodic() -> None:
    global _PERIODIC_TASK
    if _PERIODIC_TASK and not _PERIODIC_TASK.done():
        _PERIODIC_TASK.cancel()
        try:
            await _PERIODIC_TASK
        except (asyncio.CancelledError, Exception):
            pass
    _PERIODIC_TASK = None