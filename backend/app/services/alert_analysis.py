"""
见字如面 - 可观测性告警实时分析引擎（质量 / 性能指标越限 → 归因 + 建议）

现有可观测性「只有监控没有后续动作」的整改核心：后台周期扫描四方面指标中的
质量/性能规则，越限即落 obs_alerts 告警事件，并基于真实运行数据做确定性归因
证据采集，再经 LLM 生成「原因分析 + 优化建议」（失败降级规则化模板，闭环不断）。

规则（阈值见 config.py，样本门槛防小样本误报）：
- performance：error_rate / p95_latency / token_overuse
- quality：eval_regression（回归均分低于下限）/ dislike_rate / online_quality（在线评测均分）
  / empty_answer_rate（空回答率越限）

去重与生命周期：
- fingerprint = type:metric；同指纹存在 open 告警不重复建单；
- 冷却期内（ALERT_COOLDOWN_MINUTES）同指纹不重复建单；
- 指标回落（规则不再触发）→ open 告警自动 resolved（resolved_by=system）。

设计原则：扫描/归因在线程池执行不阻塞事件循环；LLM 失败静默降级；
证据与建议文本经 redact_log_text 脱敏后落库（PII 单一事实源）。
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from app.config import settings
from app.core import db
from app.core.log_setup import redact_log_text

logger = logging.getLogger("jianziruyang.alert_analysis")

_PERIODIC_TASK: Optional[asyncio.Task] = None

# 后台扫描固定窗口：近 7 天（与面板默认口径一致）
_SCAN_DAYS = 7

METRIC_LABELS = {
    "error_rate": "请求错误率",
    "p95_latency": "P95 延迟",
    "token_overuse": "平均 Token/请求",
    "eval_regression": "回归评测均分",
    "dislike_rate": "点踩占比",
    "online_quality": "在线评测均分",
    "empty_answer_rate": "空回答率",
    "agent_task_completion": "Agent 任务完成均分",
    "generation_timeout": "生成整体超时",
    "failover_promotion": "主备路由·备升主",
}


def _now() -> str:
    return datetime.now().isoformat()


def _redact_obj(obj):
    """递归脱敏证据副本（字符串经 redact_log_text，数字/结构不动）。"""
    if isinstance(obj, str):
        return redact_log_text(obj)
    if isinstance(obj, dict):
        return {k: _redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_obj(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# 归因证据采集（确定性，基于真实运行数据）
# ---------------------------------------------------------------------------
def _evidence_errors(since: str) -> dict:
    """错误率归因：近 20 条失败 trace 的 agent/状态/错误分布 + 错误 span Top。"""
    rows = db.query(
        "SELECT agent, status, error FROM agent_traces"
        " WHERE started_at >= ? AND status IN ('error','timeout','interrupted')"
        " ORDER BY started_at DESC LIMIT 20", (since,))
    by_agent, by_status, errors = {}, {}, []
    for r in rows:
        by_agent[r["agent"] or "-"] = by_agent.get(r["agent"] or "-", 0) + 1
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        if r.get("error"):
            errors.append(r["error"][:160])
    span_rows = db.query(
        "SELECT s.name, COUNT(*) AS n FROM agent_spans s JOIN agent_traces t ON t.id = s.trace_id"
        " WHERE s.started_at >= ? AND s.status = 'error' GROUP BY s.name ORDER BY n DESC LIMIT 5",
        (since,))
    return {"failed_samples": len(rows), "by_agent": by_agent, "by_status": by_status,
            "error_messages": errors[:8],
            "error_spans": [{"name": r["name"], "count": r["n"]} for r in span_rows]}


def _evidence_latency(since: str) -> dict:
    """延迟归因：最慢 5 条 trace 的 span 耗时分解（llm/tool/其他占比）。"""
    traces = db.query(
        "SELECT id, agent, latency_ms FROM agent_traces"
        " WHERE started_at >= ? AND status != 'running'"
        " ORDER BY latency_ms DESC LIMIT 5", (since,))
    items = []
    for t in traces:
        spans = db.query(
            "SELECT kind, COALESCE(SUM(latency_ms),0) AS ms, COUNT(*) AS n"
            " FROM agent_spans WHERE trace_id = ? GROUP BY kind", (t["id"],))
        items.append({"trace_id": t["id"], "agent": t["agent"],
                      "latency_ms": t["latency_ms"],
                      "breakdown": [{"kind": s["kind"], "ms": s["ms"], "n": s["n"]} for s in spans]})
    return {"slowest_traces": items}


def _evidence_tokens(metrics: dict) -> dict:
    """Token 归因：增量最大段 + 段构成（复用 metrics 的 segment_breakdown 口径）。"""
    segs = metrics.get("segment_breakdown") or []
    up = [s for s in segs if s.get("trend") == "up"]
    top = sorted(up or segs, key=lambda s: -(s.get("delta_pct") or 0))[:3]
    return {"avg_tokens": metrics.get("overview", {}).get("avg_tokens_per_request"),
            "warn_threshold": metrics.get("overview", {}).get("token_warn_threshold"),
            "top_segments": [{"segment": s.get("segment"), "label": s.get("label"),
                              "tokens": s.get("tokens"), "share": s.get("share"),
                              "delta_pct": s.get("delta_pct")} for s in top]}


def _evidence_eval(since: str) -> dict:
    """回归归因：最近完成 run 的低分指标与低分用例明细。"""
    run = db.query_one(
        "SELECT id, summary FROM eval_runs WHERE status IN ('done','finished')"
        " AND started_at >= ? ORDER BY started_at DESC LIMIT 1", (since,))
    if not run:
        return {"run_id": "", "low_cases": []}
    try:
        summary = json.loads(run.get("summary") or "{}")
    except (json.JSONDecodeError, TypeError):
        summary = {}
    low = db.query(
        "SELECT question, faithfulness, answer_relevance, context_precision, context_recall"
        " FROM eval_results WHERE run_id = ? AND status = 'ok'"
        " ORDER BY COALESCE(answer_relevance, 1) ASC LIMIT 5", (run["id"],))
    return {"run_id": run["id"], "summary": summary,
            "low_cases": [{"question": (r["question"] or "")[:80],
                           "faithfulness": r["faithfulness"],
                           "answer_relevance": r["answer_relevance"]} for r in low]}


def _evidence_dislikes(since: str) -> dict:
    """点踩归因：近期点踩反馈的评论摘要（定位不满意的具体回答）。"""
    rows = db.query(
        "SELECT comment FROM message_feedbacks WHERE created_at >= ? AND rating = 'dislike'"
        " ORDER BY created_at DESC LIMIT 10", (since,))
    return {"recent_comments": [(r.get("comment") or "")[:120] for r in rows if r.get("comment")]}


def _evidence_online() -> dict:
    """在线评测归因：近期低分样本的问题与理由。"""
    rows = db.query(
        "SELECT question, geval_score, geval_reason FROM online_eval_results"
        " WHERE status = 'ok' AND geval_score < 0.6 ORDER BY created_at DESC LIMIT 5")
    return {"low_samples": [{"question": (r["question"] or "")[:80],
                             "geval_score": r["geval_score"],
                             "reason": (r["geval_reason"] or "")[:120]} for r in rows]}


def _evidence_empty_answers(since: str) -> dict:
    """空回答归因：近期空回答/关联错误 trace 的助手消息样本（会话 + trace 状态/错误）。"""
    rows = db.query(
        "SELECT m.session_id, t.status AS trace_status, t.error AS trace_error"
        " FROM chat_messages m LEFT JOIN agent_traces t ON t.id = ("
        "   SELECT t2.id FROM agent_traces t2 WHERE t2.agent = 'chat'"
        "   AND t2.session_id = m.session_id AND t2.started_at <= m.created_at"
        "   ORDER BY t2.started_at DESC LIMIT 1)"
        " WHERE m.role = 'assistant' AND m.created_at >= ?"
        " AND (m.content = '' OR t.status = 'error')"
        " ORDER BY m.created_at DESC LIMIT 8", (since,))
    return {"empty_samples": [{"session_id": r["session_id"],
                               "trace_status": r.get("trace_status") or "",
                               "error": (r.get("trace_error") or "")[:120]} for r in rows]}


def _evidence_agent_low() -> dict:
    """Agent 低分归因：近期任务完成度低分样本的输入与工具序列。"""
    rows = db.query(
        "SELECT trace_id, input, task_completion, tools_called FROM agent_online_evals"
        " WHERE status = 'ok' AND task_completion < 0.6 ORDER BY created_at DESC LIMIT 5")
    return {"low_samples": [{"trace_id": r["trace_id"],
                             "input": (r["input"] or "")[:80],
                             "task_completion": r["task_completion"],
                             "tools_called": (r["tools_called"] or "")[:200]} for r in rows]}


# ---------------------------------------------------------------------------
# 规则评估
# ---------------------------------------------------------------------------
def _collect_rule_results() -> List[dict]:
    """扫描窗口内全部规则 → [{type, metric, value, threshold, triggered, severity, evidence}]。"""
    from app.core import observability as obs
    from app.services import online_eval

    metrics = obs.metrics(_SCAN_DAYS, "days")
    monitor = obs.monitor_metrics(_SCAN_DAYS, "days")
    online = online_eval.aggregate(_SCAN_DAYS)
    since = obs._range_since(_SCAN_DAYS, "days")
    golden = metrics.get("golden_signals") or {}
    overview = metrics.get("overview") or {}
    min_n = int(settings.ALERT_MIN_SAMPLES)
    total = int(overview.get("total") or 0)
    out = []

    # ---- 性能 ----
    err = float(golden.get("error_rate") or 0)
    thr = float(settings.ALERT_ERROR_RATE_THRESHOLD)
    out.append({"type": "performance", "metric": "error_rate", "value": err, "threshold": thr,
                "triggered": total >= min_n and err >= thr,
                "severity": "critical" if err >= thr * 2 else "warning",
                "evidence": _evidence_errors(since) if total >= min_n and err >= thr else {}})
    p95 = int((golden.get("duration") or {}).get("p95_ms") or 0)
    thr = int(settings.ALERT_P95_LATENCY_MS)
    out.append({"type": "performance", "metric": "p95_latency", "value": p95, "threshold": thr,
                "triggered": total >= min_n and p95 >= thr,
                "severity": "critical" if p95 >= thr * 2 else "warning",
                "evidence": _evidence_latency(since) if total >= min_n and p95 >= thr else {}})
    avg_tokens = float(overview.get("avg_tokens_per_request") or 0)
    thr = int(settings.TOKEN_AVG_WARN_PER_REQUEST)
    out.append({"type": "performance", "metric": "token_overuse", "value": avg_tokens, "threshold": thr,
                "triggered": total >= min_n and avg_tokens > thr, "severity": "warning",
                "evidence": _evidence_tokens(metrics) if total >= min_n and avg_tokens > thr else {}})

    # ---- 质量 ----
    quality = monitor.get("quality") or {}
    runs = quality.get("eval_runs") or []
    run_avgs = []
    for run in runs:
        avg = run.get("avg") or {}
        # judge_score 为 1-5 分制，归一化后与 0-1 指标一同参与比较
        vals = [avg.get(k) for k in ("answer_relevance", "context_precision", "faithfulness")
                if avg.get(k) is not None]
        if avg.get("judge_score") is not None:
            vals.append(avg["judge_score"] / 5)
        if vals:
            run_avgs.append(min(vals))
    min_run = min(run_avgs) if run_avgs else None
    thr = float(settings.ALERT_EVAL_SCORE_MIN)
    triggered = min_run is not None and min_run < thr
    out.append({"type": "quality", "metric": "eval_regression",
                "value": round(min_run, 4) if min_run is not None else 0, "threshold": thr,
                "triggered": triggered, "severity": "warning",
                "evidence": _evidence_eval(since) if triggered else {}})
    likes = int(quality.get("likes") or 0)
    dislikes = int(quality.get("dislikes") or 0)
    fb_total = likes + dislikes
    drate = round(dislikes / fb_total, 4) if fb_total else 0.0
    thr = float(settings.ALERT_DISLIKE_RATE_THRESHOLD)
    triggered = fb_total >= min_n and drate > thr
    out.append({"type": "quality", "metric": "dislike_rate", "value": drate, "threshold": thr,
                "triggered": triggered, "severity": "warning",
                "evidence": _evidence_dislikes(since) if triggered else {}})
    online_avg = online.get("avg_geval")
    thr = float(settings.ALERT_ONLINE_SCORE_MIN)
    triggered = (online.get("samples") or 0) >= 3 and online_avg is not None and online_avg < thr
    out.append({"type": "quality", "metric": "online_quality",
                "value": online_avg if online_avg is not None else 0, "threshold": thr,
                "triggered": triggered, "severity": "warning",
                "evidence": _evidence_online() if triggered else {}})
    # 空回答率（业务指标）：样本≥5 防小样本误报，阈值 config 可调
    biz = quality.get("online_business") or {}
    erate = biz.get("empty_answer_rate")
    thr = float(getattr(settings, "ALERT_EMPTY_ANSWER_RATE_THRESHOLD", 0.10))
    triggered = (biz.get("samples") or 0) >= 5 and erate is not None and erate > thr
    out.append({"type": "quality", "metric": "empty_answer_rate",
                "value": erate if erate is not None else 0, "threshold": thr,
                "triggered": triggered, "severity": "warning",
                "evidence": _evidence_empty_answers(since) if triggered else {}})
    # Agent 任务完成均分（在线轨迹采样）：样本≥3 防小样本误报
    agent_agg = quality.get("online_agent") or {}
    ascore = agent_agg.get("avg_task_completion")
    thr = float(getattr(settings, "ALERT_AGENT_SCORE_MIN", 0.6))
    triggered = (agent_agg.get("samples") or 0) >= 3 and ascore is not None and ascore < thr
    out.append({"type": "quality", "metric": "agent_task_completion",
                "value": ascore if ascore is not None else 0, "threshold": thr,
                "triggered": triggered, "severity": "warning",
                "evidence": _evidence_agent_low() if triggered else {}})
    return out


# ---------------------------------------------------------------------------
# 原因分析 + 优化建议（LLM 驱动，失败降级规则化模板）
# ---------------------------------------------------------------------------
_ANALYZE_TIMEOUT = 30

_FALLBACK_TIPS = {
    "error_rate": "建议查看失败 trace 的错误 span 与错误信息：上游超时优先检查网关重试与上游健康度；工具报错优先核对参数 Schema 与权限拦截码。",
    "p95_latency": "建议按最慢 trace 的 span 耗时分解定位瓶颈：LLM 段占比高考虑更换更快模型或压缩 prompt；工具段占比高检查检索/联网超时配置。",
    "token_overuse": "建议查看增量最大的 Token 段：历史/工具结果段膨胀交给四层压缩链（L0 超大结果落盘+去重、L1 微压缩清除可重现结果、L2 ≥95K Auto 摘要替换历史、L3 ≥119040 兜底），并核对单轮输出帽 4K 是否生效；知识上下文段膨胀可收紧检索 top-k 与片段裁剪。",
    "eval_regression": "建议对照低分用例核查知识库覆盖与检索命中：忠实度低优先检查生成是否脱离上下文；相关性低优先检查检索排序与 prompt 组装。",
    "dislike_rate": "建议阅读点踩评论定位共性问题（答非所问/信息缺失/表述不清），针对性补充知识库或调整系统提示词。",
    "online_quality": "建议查看低分在线样本的问题与理由，确认是知识库缺口还是生成偏差；必要时用评测集回归验证修复效果。",
    "empty_answer_rate": "建议核查空回答样本关联的 trace 错误 span：上游超时优先检查网关健康度与重试；检索为空优先确认知识库覆盖与检索阈值；并将样本转入 BadCase 闭环复现修复。",
    "agent_task_completion": "建议查看低分轨迹的工具序列与回答：工具失败优先核对参数与上游健康度；步骤冗余优先收紧 ReAct 提示词；必要时转 BadCase 用例离线复现。",
}


def _fallback_analysis(rule: dict) -> dict:
    metric = rule["metric"]
    label = METRIC_LABELS.get(metric, metric)
    return {
        "cause": f"【规则化分析】{label}当前值 {rule['value']} 越过阈值 {rule['threshold']}，"
                 f"证据见告警明细（模型分析不可用时的降级结论）。",
        "suggestions": [_FALLBACK_TIPS.get(metric, "建议结合证据明细研判后处置。")],
        "degraded": True,
    }


async def _llm_analysis(rule: dict) -> dict:
    """证据打包 → LLM 生成结构化原因分析/优化建议；失败降级规则化模板。"""
    label = METRIC_LABELS.get(rule["metric"], rule["metric"])
    evidence_text = json.dumps(rule.get("evidence") or {}, ensure_ascii=False)[:3000]
    prompt = (
        "你是 Agent 系统的可观测性分析助手。以下是一条指标告警及其真实运行证据，"
        "请基于证据给出原因分析与优化建议。\n\n"
        f"告警指标：{label}（{rule['type']}）\n"
        f"当前值：{rule['value']}，阈值：{rule['threshold']}\n"
        f"运行证据（JSON）：\n{evidence_text}\n\n"
        "只输出 JSON，不要输出任何解释、前后缀或 markdown 标记，格式：\n"
        '{"cause": "基于证据的原因分析（150字以内）", '
        '"suggestions": ["优化建议1", "优化建议2"]}\n'
        "建议须具体可执行（指向配置项/模块/操作），不超过 3 条。"
    )
    try:
        from app.core.llm import LLMFactory
        llm = LLMFactory.create_for_role("assistant")

        async def _gen():
            collected = ""
            async for chunk in llm.chat([{"role": "user", "content": prompt}], stream=False):
                collected += chunk
            return collected

        raw = await asyncio.wait_for(_gen(), timeout=_ANALYZE_TIMEOUT)
        import re
        m = re.search(r"\{[\s\S]*\}", raw or "")
        if m:
            data = json.loads(m.group(0))
            cause = str(data.get("cause") or "").strip()
            suggestions = [str(s).strip() for s in (data.get("suggestions") or []) if str(s).strip()]
            if cause and suggestions:
                return {"cause": cause[:600], "suggestions": suggestions[:3], "degraded": False}
    except Exception as e:
        logger.warning("告警 LLM 分析失败，降级规则化建议: %s", e)
    return _fallback_analysis(rule)


# ---------------------------------------------------------------------------
# 告警生命周期
# ---------------------------------------------------------------------------
def _create_alert(rule: dict, analysis: dict, fingerprint: Optional[str] = None,
                  window: Optional[str] = None) -> str:
    alert_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO obs_alerts (id, type, metric, severity, status, fingerprint, value,"
        " threshold, window, evidence, analysis, analyzed_at, created_at)"
        " VALUES (?,?,?,?, 'open', ?,?,?,?,?,?,?,?)",
        (alert_id, rule["type"], rule["metric"], rule["severity"],
         fingerprint or f"{rule['type']}:{rule['metric']}", float(rule["value"]), float(rule["threshold"]),
         window or f"{_SCAN_DAYS}d",
         json.dumps(_redact_obj(rule.get("evidence") or {}), ensure_ascii=False)[:8000],
         json.dumps(_redact_obj(analysis), ensure_ascii=False)[:4000],
         _now(), _now()))
    logger.info("[告警分析] 新建告警 %s %s=%s（阈值 %s）",
                rule["type"], rule["metric"], rule["value"], rule["threshold"])
    return alert_id


def record_generation_timeout(session_id: str, user_id: str = "", message_id: str = "",
                              elapsed_seconds: float = 0.0, query: str = "") -> Optional[str]:
    """业务兜底告警：单次生成整体超预算（默认 5 分钟），会话生成任务已自动停止。

    事件类告警：按请求（session+message）去重；规则化结论不做 LLM 分析；
    不参与指标扫描自动恢复，需人工 ack/resolve。
    """
    fp = f"event:generation_timeout:{session_id}:{message_id or '-'}"
    if db.query_one("SELECT id FROM obs_alerts WHERE fingerprint=? AND status IN ('open','acked')", (fp,)):
        return None
    threshold = float(settings.CHAT_REQUEST_TIMEOUT)
    rule = {
        "type": "event", "metric": "generation_timeout", "severity": "high",
        "value": round(float(elapsed_seconds), 1), "threshold": threshold,
        "evidence": {"session_id": session_id, "user_id": user_id or "-",
                     "message_id": message_id or "-", "query": (query or "")[:120],
                     "elapsed_seconds": round(float(elapsed_seconds), 1),
                     "budget_seconds": threshold,
                     "action": "已自动停止该会话所有生成任务"},
    }
    analysis = {
        "cause": f"单次生成整体耗时 {rule['value']}s 超过预算 {threshold:.0f}s（默认 5 分钟），"
                 "系统已自动停止该会话所有生成任务并中断响应。",
        "suggestions": ["检查模型网关上游健康度与主备路由切换配置，避免重试放大消耗预算",
                        "打开该请求 trace 明细，核对检索/故障切换/重试各阶段耗时分布",
                        "如长文本生成为业务常态，评估调高 CHAT_REQUEST_TIMEOUT 后放量"],
        "degraded": True,
    }
    return _create_alert(rule, analysis, fingerprint=fp, window="event")


def record_failover_promotion(*, rule_id: str, rule_name: str = "", old_primary: str = "",
                              new_primary: str = "", role: str = "", user_id: str = "") -> Optional[str]:
    """整改3：主备切换·备升主显性化（事件告警，按 rule+新主 去重）。

    切换已持久化到路由规则；告警告知用户旧主失效、新主领衔，后续不再重试旧主。
    """
    fp = f"event:failover_promotion:{rule_id}:{new_primary}"
    if db.query_one("SELECT id FROM obs_alerts WHERE fingerprint=? AND status IN ('open','acked')", (fp,)):
        return None
    rule = {
        "type": "event", "metric": "failover_promotion", "severity": "medium",
        "value": 1, "threshold": 0,
        "evidence": {"rule_id": rule_id, "rule_name": rule_name or "-",
                     "old_primary": old_primary, "new_primary": new_primary,
                     "role": role or "-", "user_id": user_id or "-",
                     "action": "备已升主并持久化到路由规则，后续请求不再重试旧主"},
    }
    analysis = {
        "cause": f"路由规则「{rule_name or rule_id}」的主路由「{old_primary}」连续失败达阈值或熔断，"
                 f"系统已将健康备「{new_primary}」升为主并持久化，后续请求直接走新主。",
        "suggestions": ["核查旧主上游的配置与可用性，恢复后如需回切请在路由规则中手动调整主上游",
                        "切换已持久化：后续请求不再对旧主重试，避免重复消耗故障切换预算",
                        "如需自动回切策略，评估 failover_threshold 与上游健康检查配置"],
        "degraded": True,
    }
    return _create_alert(rule, analysis, fingerprint=fp, window="event")


def scan_once_sync() -> dict:
    """一轮扫描（同步，供 asyncio.to_thread 调用）：建单 + 自动恢复。

    返回 {created: [...], resolved: [...]}；LLM 分析在异步包装层执行。
    本函数只做规则评估与生命周期管理，证据随规则结果携带。
    """
    rules = _collect_rule_results()
    cooldown = timedelta(minutes=max(0, int(settings.ALERT_COOLDOWN_MINUTES)))
    created, resolved, pending_analysis = [], [], []
    for rule in rules:
        fp = f"{rule['type']}:{rule['metric']}"
        open_row = db.query_one(
            "SELECT id FROM obs_alerts WHERE fingerprint = ? AND status IN ('open','acked')"
            " ORDER BY created_at DESC LIMIT 1", (fp,))
        if rule["triggered"]:
            if open_row:
                continue  # 同指纹已有未处置告警，不重复建单
            recent = db.query_one(
                "SELECT created_at FROM obs_alerts WHERE fingerprint = ?"
                " ORDER BY created_at DESC LIMIT 1", (fp,))
            if recent and recent.get("created_at"):
                try:
                    last = datetime.fromisoformat(recent["created_at"])
                    if datetime.now() - last < cooldown:
                        continue  # 冷却期内不重复建单
                except ValueError:
                    pass
            pending_analysis.append(rule)
        else:
            if open_row:
                db.execute(
                    "UPDATE obs_alerts SET status='resolved', resolved_at=?, resolved_by='system'"
                    " WHERE id=?", (_now(), open_row["id"]))
                resolved.append(open_row["id"])
    return {"pending_analysis": pending_analysis, "resolved": resolved, "created": created}


async def scan_once() -> dict:
    """一轮完整扫描：规则评估（线程池）→ 新告警 LLM 分析 → 落库。"""
    result = await asyncio.to_thread(scan_once_sync)
    for rule in result["pending_analysis"]:
        analysis = await _llm_analysis(rule)
        alert_id = await asyncio.to_thread(_create_alert, rule, analysis)
        result["created"].append(alert_id)
    result.pop("pending_analysis", None)
    return result


async def reanalyze(alert_id: str) -> Optional[dict]:
    """强制重跑某告警的原因分析（证据刷新 + LLM 重新生成）。"""
    row = db.query_one("SELECT * FROM obs_alerts WHERE id = ?", (alert_id,))
    if not row:
        return None
    rules = await asyncio.to_thread(_collect_rule_results)
    rule = next((r for r in rules if f"{r['type']}:{r['metric']}" == row["fingerprint"]), None)
    if rule is None:
        rule = {"type": row["type"], "metric": row["metric"],
                "value": row["value"], "threshold": row["threshold"],
                "evidence": json.loads(row.get("evidence") or "{}")}
    analysis = await _llm_analysis(rule)
    db.execute(
        "UPDATE obs_alerts SET evidence=?, analysis=?, analyzed_at=? WHERE id=?",
        (json.dumps(_redact_obj(rule.get("evidence") or {}), ensure_ascii=False)[:8000],
         json.dumps(_redact_obj(analysis), ensure_ascii=False)[:4000], _now(), alert_id))
    return get_alert(alert_id)


# ---------------------------------------------------------------------------
# 查询 / 处置
# ---------------------------------------------------------------------------
def _row_to_dict(row: dict) -> dict:
    try:
        evidence = json.loads(row.get("evidence") or "{}")
    except (json.JSONDecodeError, TypeError):
        evidence = {}
    try:
        analysis = json.loads(row.get("analysis") or "{}")
    except (json.JSONDecodeError, TypeError):
        analysis = {}
    return {
        "id": row["id"],
        "type": row["type"],
        "metric": row["metric"],
        "metric_label": METRIC_LABELS.get(row["metric"], row["metric"]),
        "severity": row["severity"],
        "status": row["status"],
        "value": row["value"],
        "threshold": row["threshold"],
        "window": row.get("window") or "",
        "evidence": evidence,
        "analysis": analysis,
        "analyzed_at": row.get("analyzed_at") or "",
        "created_at": row.get("created_at") or "",
        "acked_at": row.get("acked_at") or "",
        "resolved_at": row.get("resolved_at") or "",
        "resolved_by": row.get("resolved_by") or "",
    }


def get_alert(alert_id: str) -> Optional[dict]:
    row = db.query_one("SELECT * FROM obs_alerts WHERE id = ?", (alert_id,))
    return _row_to_dict(row) if row else None


def list_alerts(status: str = "", limit: int = 50, offset: int = 0) -> dict:
    where, params = "", []
    if status in ("open", "acked", "resolved"):
        where = " WHERE status = ?"
        params = [status]
    total = int((db.query_one(
        f"SELECT COUNT(*) AS n FROM obs_alerts{where}", tuple(params)) or {}).get("n") or 0)
    rows = db.query(
        f"SELECT * FROM obs_alerts{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        tuple(params) + (max(1, min(200, limit)), max(0, offset)))
    unacked = int((db.query_one(
        "SELECT COUNT(*) AS n FROM obs_alerts WHERE status = 'open'") or {}).get("n") or 0)
    return {"alerts": [_row_to_dict(r) for r in rows], "total": total, "unacked": unacked}


def ack_alert(alert_id: str) -> Optional[dict]:
    row = db.query_one("SELECT id, status FROM obs_alerts WHERE id = ?", (alert_id,))
    if not row:
        return None
    if row["status"] == "open":
        db.execute("UPDATE obs_alerts SET status='acked', acked_at=? WHERE id=?",
                   (_now(), alert_id))
    return get_alert(alert_id)


def resolve_alert(alert_id: str, resolved_by: str = "") -> Optional[dict]:
    row = db.query_one("SELECT id FROM obs_alerts WHERE id = ?", (alert_id,))
    if not row:
        return None
    db.execute(
        "UPDATE obs_alerts SET status='resolved', resolved_at=?, resolved_by=? WHERE id=?",
        (_now(), resolved_by or "", alert_id))
    return get_alert(alert_id)


# ---------------------------------------------------------------------------
# 周期任务（lifespan 启停；测试环境不启动，显式调用 scan_once）
# ---------------------------------------------------------------------------
async def _periodic_loop():
    interval = max(15, int(settings.ALERT_SCAN_INTERVAL_SECONDS))
    while True:
        await asyncio.sleep(interval)
        try:
            await scan_once()
        except Exception as e:
            logger.warning("[告警分析] 周期扫描异常（下轮重试）: %s", e)


def start_periodic() -> None:
    global _PERIODIC_TASK
    if _PERIODIC_TASK and not _PERIODIC_TASK.done():
        return
    try:
        _PERIODIC_TASK = asyncio.create_task(_periodic_loop())
        logger.info("[告警分析] 周期扫描已启动（间隔 %ds）", settings.ALERT_SCAN_INTERVAL_SECONDS)
    except RuntimeError:
        logger.warning("[告警分析] 无运行中的事件循环，周期任务未启动")


async def stop_periodic() -> None:
    global _PERIODIC_TASK
    if _PERIODIC_TASK and not _PERIODIC_TASK.done():
        _PERIODIC_TASK.cancel()
        try:
            await _PERIODIC_TASK
        except (asyncio.CancelledError, Exception):
            pass
    _PERIODIC_TASK = None