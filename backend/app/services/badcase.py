"""
见字如面 - BadCase 分析框架（多源收集 → 确定性分类 → 转用例复现闭环）

闭环链路：离线测评/在线观测发现问题 → 收集归因 → 转评测用例 → 用户跑回归 →
run 结束自动复现判定（repro_check）→ 人工标记已修复/忽略。

多源收集（游标存 runtime_config，增量扫描不重复）：
- offline_eval：检索未命中(有期望信号)/忠实度低/幻觉高/相关性低/GEval 低/执行错误；
- online_eval：在线采样低分；feedback：点踩；empty_answer：空回答或关联 trace 错误；
- agent_eval / agent_online：任务完成度低或工具 span 错误。

设计要点：
- 收集/分类/闭环全确定性（零 LLM 成本）；LLM 归因为可选增强（30s 超时降级模板）；
- source_type+source_id 唯一去重（INSERT OR IGNORE）；
- 原始文本落库不改写，API 返回前经 redact_log_text 脱敏（单一事实源）；
- 周期任务 lifespan 启停；evaluation._run_all 结束增量触发 collect_round+repro_check。
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import List, Optional

from app.config import settings
from app.core import db

logger = logging.getLogger("jianziruyang.badcase")

_PERIODIC_TASK: Optional[asyncio.Task] = None
_CURSOR_KEY = "badcase_cursors"
_BATCH = 200

CATEGORY_LABELS = {
    "retrieval_miss": "检索未命中",
    "empty_context": "上下文为空",
    "hallucination": "幻觉",
    "irrelevant": "答非所问",
    "empty_answer": "空回答",
    "tool_failure": "工具失败",
    "low_quality": "质量低分",
}

# 规则化归因模板（cause + suggestions）：收集即落，LLM 分析可选增强
_CATEGORY_RULES = {
    "retrieval_miss": (
        "检索未命中期望关键词/来源：知识库覆盖不足、语义匹配偏差或排序靠后。",
        ["核对知识库是否涵盖期望来源对应内容", "调整检索 top-k 与重排阈值",
         "补充知识后重跑回归验证 Hit@K/MRR"],
    ),
    "empty_context": (
        "检索上下文为空：该问题未召回任何可用片段。",
        ["确认知识库覆盖与文档解析完整性", "检查检索阈值是否过严", "补充对应知识条目"],
    ),
    "hallucination": (
        "忠实度偏低或幻觉分偏高：答案可能包含上下文无法推导的内容。",
        ["收紧系统提示词的「有据可查」约束", "检查生成是否脱离检索片段", "补充权威来源降低编造动机"],
    ),
    "irrelevant": (
        "回答相关性偏低：答案偏离用户问题意图。",
        ["检查问题理解与检索 query 改写", "优化知识优先 prompt 组装", "核对期望关键词是否准确"],
    ),
    "empty_answer": (
        "回答为空或关联执行 trace 错误：问答主链路中断。",
        ["查看关联 trace 的错误 span 与错误信息", "检查上游模型健康度与超时配置", "修复后用本用例回归验证"],
    ),
    "tool_failure": (
        "Agent 工具调用出现错误：参数 Schema、权限拦截或上游服务异常。",
        ["核对工具参数 Schema 与期望工具序列", "检查权限拦截码与上游健康度", "修复后重跑轨迹评测"],
    ),
    "low_quality": (
        "综合质量评分低于阈值（GEval/judge/任务完成度）。",
        ["阅读低分样本的问题与理由定位共性", "针对性补充知识库或调整提示词", "转用例后回归验证修复效果"],
    ),
}


def _now() -> str:
    return datetime.now().isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _rule_analysis(category: str) -> dict:
    cause, suggestions = _CATEGORY_RULES.get(category, _CATEGORY_RULES["low_quality"])
    return {"cause": cause, "suggestions": list(suggestions), "degraded": True}


def _loads(raw, default):
    try:
        value = json.loads(raw) if (raw or "").strip() else default
    except (json.JSONDecodeError, TypeError):
        return default
    return value if isinstance(value, type(default)) else default


def _question_for(session_id: str, message_id: str) -> str:
    """助手消息之前最近一条用户消息（与在线评测同口径）。"""
    row = db.query_one(
        "SELECT content FROM chat_messages WHERE session_id = ? AND role = 'user'"
        " AND seq < (SELECT seq FROM chat_messages WHERE message_id = ?)"
        " ORDER BY seq DESC LIMIT 1", (session_id, message_id))
    return (row or {}).get("content") or ""


def _has_expected(keywords_json: str, source: str) -> bool:
    return bool(_loads(keywords_json, [])) or bool((source or "").strip())


def _insert(source_type: str, source_id: str, *, run_id: str = "", session_id: str = "",
            message_id: str = "", question: str = "", answer: str = "",
            contexts: str = "[]", metrics: Optional[dict] = None,
            category: str = "low_quality") -> bool:
    """落一条 BadCase（source_id 唯一去重）；返回是否新增。"""
    n = db.execute(
        "INSERT OR IGNORE INTO bad_cases (id, created_at, source_type, source_id, run_id,"
        " session_id, message_id, question, answer, contexts, metrics, category, analysis,"
        " status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'open')",
        (_new_id(), _now(), source_type, source_id, run_id, session_id, message_id,
         (question or "")[:2000], (answer or "")[:4000], contexts or "[]",
         json.dumps(metrics or {}, ensure_ascii=False)[:8000], category,
         json.dumps(_rule_analysis(category), ensure_ascii=False)),
    )
    return n > 0


# ---------------------------------------------------------------------------
# 多源收集（各源独立游标；rowid 源用整数游标，时间源用 created_at 游标）
# ---------------------------------------------------------------------------
def _classify_offline(r: dict, has_expected: bool) -> Optional[str]:
    """离线结果 → 是否收集 + 确定性分类（优先级：检索>上下文>幻觉>相关>低分）。"""
    faith, halluc = r.get("faithfulness"), r.get("hallucination")
    rel, geval = r.get("answer_relevance"), r.get("geval_score")
    bad = (r.get("status") == "error"
           or (int(r.get("retrieval_hit") or 0) == 0 and has_expected)
           or (faith is not None and faith < 0.6)
           or (halluc is not None and halluc > 0.5)
           or (rel is not None and rel < 0.6)
           or (geval is not None and geval < 0.6))
    if not bad:
        return None
    if int(r.get("retrieval_hit") or 0) == 0 and has_expected:
        return "retrieval_miss"
    sources = _loads(r.get("retrieved_sources"), [])
    has_text = False
    for s in sources:
        if not isinstance(s, dict):
            continue
        for c in (s.get("chunks") or [s]):
            if isinstance(c, dict) and (c.get("text") or "").strip():
                has_text = True
                break
        if has_text:
            break
    if not has_text:
        return "empty_context"
    if (halluc is not None and halluc > 0.5) or (faith is not None and faith < 0.6):
        return "hallucination"
    if rel is not None and rel < 0.6:
        return "irrelevant"
    return "low_quality"


def _collect_offline(cursor) -> tuple:
    cursor = int(cursor or 0)
    rows = db.query(
        "SELECT r.rowid AS rid, r.*, c.expected_keywords AS c_keywords,"
        " c.expected_source AS c_source FROM eval_results r"
        " LEFT JOIN eval_cases c ON c.id = r.case_id"
        " WHERE r.rowid > ? ORDER BY r.rowid ASC LIMIT ?", (cursor, _BATCH))
    added = 0
    for r in rows:
        has_expected = _has_expected(r.get("c_keywords"), r.get("c_source"))
        category = _classify_offline(r, has_expected)
        if not category:
            continue
        metrics = {k: r.get(k) for k in (
            "faithfulness", "answer_relevance", "context_precision", "context_recall",
            "hallucination", "geval_score", "judge_score", "retrieval_hit",
            "hit_at_k", "mrr", "status", "error")}
        added += _insert("offline_eval", r["id"], run_id=r.get("run_id") or "",
                         question=r.get("question") or "", answer=r.get("answer") or "",
                         contexts=r.get("retrieved_sources") or "[]",
                         metrics=metrics, category=category)
    return added, (max(r["rid"] for r in rows) if rows else None)


def _collect_online(cursor) -> tuple:
    cursor = str(cursor or "")
    where = "status = 'ok' AND (geval_score < 0.6 OR faithfulness < 0.6)"
    params: list = []
    if cursor:
        where += " AND created_at > ?"
        params.append(cursor)
    rows = db.query(
        f"SELECT * FROM online_eval_results WHERE {where}"
        " ORDER BY created_at ASC LIMIT ?", (*params, _BATCH))
    added = 0
    for r in rows:
        faith = r.get("faithfulness")
        category = "hallucination" if (faith is not None and faith < 0.6) else "low_quality"
        msg = db.query_one("SELECT sources FROM chat_messages WHERE message_id = ?",
                           (r.get("message_id") or "",))
        added += _insert(
            "online_eval", r["id"], session_id=r.get("session_id") or "",
            message_id=r.get("message_id") or "", question=r.get("question") or "",
            answer=r.get("answer") or "", contexts=(msg or {}).get("sources") or "[]",
            metrics={"faithfulness": faith, "geval_score": r.get("geval_score"),
                     "geval_reason": (r.get("geval_reason") or "")[:300]},
            category=category)
    return added, (max(r["created_at"] for r in rows) if rows else None)


def _collect_feedback(cursor) -> tuple:
    cursor = str(cursor or "")
    where = "f.rating = 'dislike'"
    params: list = []
    if cursor:
        where += " AND f.created_at > ?"
        params.append(cursor)
    rows = db.query(
        f"SELECT f.id, f.session_id, f.message_id, f.comment, f.created_at,"
        " m.content AS answer FROM message_feedbacks f"
        f" LEFT JOIN chat_messages m ON m.message_id = f.message_id WHERE {where}"
        " ORDER BY f.created_at ASC LIMIT ?", (*params, _BATCH))
    added = 0
    for r in rows:
        added += _insert(
            "feedback", r["id"], session_id=r.get("session_id") or "",
            message_id=r.get("message_id") or "",
            question=_question_for(r.get("session_id") or "", r.get("message_id") or ""),
            answer=r.get("answer") or "",
            metrics={"rating": "dislike", "comment": (r.get("comment") or "")[:300]},
            category="low_quality")
    return added, (max(r["created_at"] for r in rows) if rows else None)


def _collect_empty(cursor) -> tuple:
    cursor = str(cursor or "")
    where = "m.role = 'assistant'"
    params: list = []
    if cursor:
        where += " AND m.created_at > ?"
        params.append(cursor)
    rows = db.query(
        "SELECT m.message_id, m.session_id, m.content, m.created_at,"
        " (SELECT t.status FROM agent_traces t WHERE t.agent = 'chat'"
        "  AND t.session_id = m.session_id AND t.started_at <= m.created_at"
        "  ORDER BY t.started_at DESC LIMIT 1) AS trace_status"
        f" FROM chat_messages m WHERE {where}"
        " ORDER BY m.created_at ASC LIMIT ?", (*params, _BATCH))
    added = 0
    for r in rows:
        if (r.get("content") or "") != "" and r.get("trace_status") != "error":
            continue
        added += _insert(
            "empty_answer", r["message_id"], session_id=r.get("session_id") or "",
            message_id=r["message_id"],
            question=_question_for(r.get("session_id") or "", r["message_id"]),
            answer=r.get("content") or "",
            metrics={"trace_status": r.get("trace_status") or ""},
            category="empty_answer")
    return added, (max(r["created_at"] for r in rows) if rows else None)


def _agent_category(r: dict) -> Optional[str]:
    """Agent 结果 → 是否收集 + 分类（工具 span 错误优先）。"""
    tc = r.get("task_completion")
    tool_error = bool(db.query_one(
        "SELECT 1 AS x FROM agent_spans WHERE trace_id = ? AND kind = 'tool'"
        " AND status = 'error' LIMIT 1", (r.get("trace_id") or "",)))
    if r.get("status") != "error" and not tool_error and not (tc is not None and tc < 0.6):
        return None
    return "tool_failure" if tool_error else "low_quality"


def _agent_metrics(r: dict) -> dict:
    return {k: r.get(k) for k in (
        "task_completion", "tool_correctness", "argument_correctness",
        "step_efficiency", "plan_quality", "plan_adherence", "trajectory_judge",
        "trajectory_match", "status", "error")}


def _collect_agent_eval(cursor) -> tuple:
    cursor = int(cursor or 0)
    rows = db.query(
        "SELECT rowid AS rid, * FROM agent_eval_results WHERE rowid > ?"
        " ORDER BY rowid ASC LIMIT ?", (cursor, _BATCH))
    added = 0
    for r in rows:
        category = _agent_category(r)
        if not category:
            continue
        added += _insert("agent_eval", r["id"], run_id=r.get("run_id") or "",
                         question=r.get("question") or "", answer=r.get("answer") or "",
                         contexts=r.get("tools_called") or "[]",
                         metrics=_agent_metrics(r), category=category)
    return added, (max(r["rid"] for r in rows) if rows else None)


def _collect_agent_online(cursor) -> tuple:
    cursor = str(cursor or "")
    where = "1=1"
    params: list = []
    if cursor:
        where += " AND created_at > ?"
        params.append(cursor)
    rows = db.query(
        f"SELECT * FROM agent_online_evals WHERE {where}"
        " ORDER BY created_at ASC LIMIT ?", (*params, _BATCH))
    added = 0
    for r in rows:
        category = _agent_category(r)
        if not category:
            continue
        added += _insert("agent_online", r["id"], session_id="",
                         question=r.get("input") or "", answer=r.get("answer") or "",
                         contexts=r.get("tools_called") or "[]",
                         metrics=_agent_metrics(r), category=category)
    return added, (max(r["created_at"] for r in rows) if rows else None)


_COLLECTORS = (
    ("offline_eval", _collect_offline),
    ("online_eval", _collect_online),
    ("feedback", _collect_feedback),
    ("empty_answer", _collect_empty),
    ("agent_eval", _collect_agent_eval),
    ("agent_online", _collect_agent_online),
)


def collect_round() -> dict:
    """一轮多源增量收集（周期任务/run 结束/测试共用入口，同步纯 DB）。"""
    from app.services.config_manager import runtime_config
    cursors = runtime_config.get(_CURSOR_KEY)
    if not isinstance(cursors, dict):
        cursors = {}
    stats = {}
    for name, fn in _COLLECTORS:
        try:
            added, new_cursor = fn(cursors.get(name))
            stats[name] = added
            if new_cursor is not None:
                cursors[name] = new_cursor
        except Exception as e:
            logger.warning("[BadCase] %s 源收集异常（跳过）: %s", name, e)
            stats[name] = 0
    runtime_config.set(_CURSOR_KEY, cursors)
    total = sum(stats.values())
    if total:
        logger.info("[BadCase] 本轮收集 %d 条：%s", total, stats)
    return {"collected": total, "by_source": stats}


# ---------------------------------------------------------------------------
# 闭环：转用例 → 回归复现判定 → 人工标记
# ---------------------------------------------------------------------------
def repro_check(run_id: str) -> dict:
    """run 结束复现判定：关联用例通过（faithfulness≥0.6 且检索命中或无期望）→ reproduced。

    faithfulness 未测（None）按降级惯例不阻断；快照写入 analysis.repro。
    """
    linked = db.query("SELECT * FROM bad_cases WHERE case_id != '' AND status = 'open'")
    checked = reproduced = 0
    for bc in linked:
        r = db.query_one(
            "SELECT * FROM eval_results WHERE run_id = ? AND case_id = ?"
            " ORDER BY rowid DESC LIMIT 1", (run_id, bc["case_id"]))
        if not r:
            continue
        checked += 1
        case = db.query_one(
            "SELECT expected_keywords, expected_source FROM eval_cases WHERE id = ?",
            (bc["case_id"],)) or {}
        has_expected = _has_expected(case.get("expected_keywords"), case.get("expected_source"))
        faith = r.get("faithfulness")
        faith_ok = faith is None or faith >= 0.6
        retrieval_ok = (not has_expected) or int(r.get("retrieval_hit") or 0) == 1
        if r.get("status") == "ok" and faith_ok and retrieval_ok:
            analysis = _loads(bc.get("analysis"), {})
            analysis["repro"] = {"run_id": run_id, "faithfulness": faith,
                                 "retrieval_hit": int(r.get("retrieval_hit") or 0),
                                 "at": _now()}
            db.execute(
                "UPDATE bad_cases SET status = 'reproduced', repro_run_id = ?, analysis = ?"
                " WHERE id = ?",
                (run_id, json.dumps(analysis, ensure_ascii=False), bc["id"]))
            reproduced += 1
    return {"checked": checked, "reproduced": reproduced}


def to_case(badcase_id: str, dataset_id: str, user: dict,
            edits: Optional[dict] = None) -> dict:
    """转评测用例（origin=badcase，可编辑参考答案）；记录 case_id 建立闭环关联。"""
    bc = db.query_one("SELECT * FROM bad_cases WHERE id = ?", (badcase_id,))
    if not bc:
        raise ValueError("BadCase 不存在")
    if bc.get("case_id"):
        raise ValueError("该 BadCase 已转用例，不能重复转换")
    if not db.query_one("SELECT id FROM eval_datasets WHERE id = ?", (dataset_id,)):
        raise ValueError("评测集不存在")
    edits = edits or {}
    question = str(edits.get("question") or bc["question"]).strip()
    if not question:
        raise ValueError("用例问题不能为空")
    reference = str(edits.get("reference_answer")
                    if edits.get("reference_answer") is not None else bc["answer"])
    keywords = edits.get("expected_keywords") or []
    keywords = [str(k).strip() for k in keywords if str(k).strip()][:6]
    source = str(edits.get("expected_source") or "")
    case_id = _new_id()
    db.execute(
        "INSERT INTO eval_cases (id, dataset_id, question, expected_keywords,"
        " reference_answer, expected_source, origin, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (case_id, dataset_id, question[:500], json.dumps(keywords, ensure_ascii=False),
         reference[:2000], source[:200], "badcase", _now()))
    db.execute("UPDATE bad_cases SET case_id = ? WHERE id = ?", (case_id, badcase_id))
    return {"case_id": case_id, "dataset_id": dataset_id}


def mark(badcase_id: str, status: str, user: dict, note: str = "") -> dict:
    """人工标记：fixed(已修复)/ignored(忽略)/open(重新打开)。"""
    if status not in ("fixed", "ignored", "open"):
        raise ValueError("无效状态（fixed/ignored/open）")
    bc = db.query_one("SELECT id FROM bad_cases WHERE id = ?", (badcase_id,))
    if not bc:
        raise ValueError("BadCase 不存在")
    db.execute(
        "UPDATE bad_cases SET status = ?, note = ?, resolved_at = ? WHERE id = ?",
        (status, (note or "")[:500], _now() if status in ("fixed", "ignored") else "",
         badcase_id))
    return {"id": badcase_id, "status": status}


# ---------------------------------------------------------------------------
# 查询与 LLM 归因增强
# ---------------------------------------------------------------------------
def list_badcases(status: str = "", category: str = "", source_type: str = "",
                  limit: int = 50, offset: int = 0) -> dict:
    """BadCase 列表（过滤+分页+分类/状态计数；返回前脱敏，原始落库不改写）。"""
    from app.core.log_setup import redact_log_text
    where, params = "1=1", []
    for col, val in (("status", status), ("category", category), ("source_type", source_type)):
        if val:
            where += f" AND {col} = ?"
            params.append(val)
    total = int((db.query_one(
        f"SELECT COUNT(*) AS n FROM bad_cases WHERE {where}", tuple(params)) or {}).get("n") or 0)
    rows = db.query(
        f"SELECT * FROM bad_cases WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (*params, max(1, min(200, limit)), max(0, offset)))
    items = []
    for r in rows:
        analysis = _loads(r.get("analysis"), {})
        items.append({
            "id": r["id"],
            "created_at": (r.get("created_at") or "")[:16],
            "source_type": r.get("source_type") or "",
            "source_id": r.get("source_id") or "",
            "run_id": r.get("run_id") or "",
            "session_id": r.get("session_id") or "",
            "message_id": r.get("message_id") or "",
            "question": redact_log_text((r.get("question") or "").replace("\n", " ")[:120]),
            "answer": redact_log_text((r.get("answer") or "").replace("\n", " ")[:200]),
            "metrics": _loads(r.get("metrics"), {}),
            "category": r.get("category") or "low_quality",
            "analysis": analysis,
            "status": r.get("status") or "open",
            "case_id": r.get("case_id") or "",
            "repro_run_id": r.get("repro_run_id") or "",
            "note": r.get("note") or "",
        })
    category_counts = {r["category"]: r["n"] for r in db.query(
        "SELECT category, COUNT(*) AS n FROM bad_cases GROUP BY category")}
    status_counts = {r["status"]: r["n"] for r in db.query(
        "SELECT status, COUNT(*) AS n FROM bad_cases GROUP BY status")}
    return {"items": items, "total": total,
            "category_counts": category_counts, "status_counts": status_counts}


_ANALYZE_TIMEOUT = 30

_ANALYZE_PROMPT = (
    "你是 RAG/Agent 质量归因专家。给定一条 BadCase 的问题、回答、指标快照与确定性分类，"
    "请分析根因并给出可执行的修复建议。只输出一个 JSON 对象，不要输出任何解释、"
    "不要用代码块包裹。格式：\n"
    '{"cause": "根因分析（150 字内）", "suggestions": ["建议1", "建议2", "建议3"]}\n'
    "要求：根因紧扣指标证据，建议面向知识库/检索/提示词/工具配置的具体动作。"
)


async def analyze(badcase_id: str) -> dict:
    """LLM 归因增强（30s 超时/失败降级规则化模板）；分析文本脱敏后落库。"""
    from app.core.log_setup import redact_log_text
    from app.services import wiki
    bc = db.query_one("SELECT * FROM bad_cases WHERE id = ?", (badcase_id,))
    if not bc:
        raise ValueError("BadCase 不存在")
    category = bc.get("category") or "low_quality"
    try:
        messages = [
            {"role": "system", "content": _ANALYZE_PROMPT},
            {"role": "user", "content": (
                f"【分类】{CATEGORY_LABELS.get(category, category)}\n"
                f"【问题】{(bc.get('question') or '')[:500]}\n"
                f"【回答】{(bc.get('answer') or '')[:1500]}\n"
                f"【指标快照】{(bc.get('metrics') or '{}')[:1500]}")},
        ]
        raw = await asyncio.wait_for(wiki._llm_complete(messages), timeout=_ANALYZE_TIMEOUT)
        data = wiki._extract_json(raw)
        cause = str(data.get("cause") or "").strip()
        suggestions = [str(s).strip() for s in (data.get("suggestions") or []) if str(s).strip()][:5]
        if not cause:
            raise ValueError("LLM 未返回有效根因")
        analysis = {"cause": redact_log_text(cause)[:1000],
                    "suggestions": [redact_log_text(s)[:300] for s in suggestions],
                    "degraded": False}
    except Exception as e:
        logger.warning("[BadCase] LLM 归因降级（规则模板）id=%s: %s", badcase_id, e)
        analysis = _rule_analysis(category)
    db.execute("UPDATE bad_cases SET analysis = ? WHERE id = ?",
               (json.dumps(analysis, ensure_ascii=False), badcase_id))
    return analysis


# ---------------------------------------------------------------------------
# 周期任务（lifespan 启停；测试环境不启动，显式调用 collect_round）
# ---------------------------------------------------------------------------
async def _periodic_loop():
    interval = max(60, int(getattr(settings, "BADCASE_SCAN_INTERVAL_SECONDS", 300)))
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(collect_round)
        except Exception as e:
            logger.warning("[BadCase] 周期收集异常（下轮重试）: %s", e)


def start_periodic() -> None:
    global _PERIODIC_TASK
    if _PERIODIC_TASK and not _PERIODIC_TASK.done():
        return
    try:
        _PERIODIC_TASK = asyncio.create_task(_periodic_loop())
        logger.info("[BadCase] 周期收集任务已启动（间隔 %ds）",
                    getattr(settings, "BADCASE_SCAN_INTERVAL_SECONDS", 300))
    except RuntimeError:
        logger.warning("[BadCase] 无运行中的事件循环，周期任务未启动")


async def stop_periodic() -> None:
    global _PERIODIC_TASK
    if _PERIODIC_TASK and not _PERIODIC_TASK.done():
        _PERIODIC_TASK.cancel()
        try:
            await _PERIODIC_TASK
        except (asyncio.CancelledError, Exception):
            pass
    _PERIODIC_TASK = None