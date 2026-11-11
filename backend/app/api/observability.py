"""
见字如面 - Agent 可观测性 API（管理员 / 运维角色）

提供三层可观测性的在线查询入口：
- GET /api/observability/metrics：聚合指标（Token 消耗 / 响应延迟 / 成功率 / 工具调用次数）
- GET /api/observability/traces：trace 列表（一次用户请求 = 一条 trace）
- GET /api/observability/traces/{id}：trace 全链路详情（含每次 LLM 调用的完整 prompt/response）
- GET /api/observability/skill-reviews：Skill 添加/修改预审记录（安全与能力可行性，拒绝行为重点暴露）
"""
import asyncio
import json
import logging
import time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.api.deps import get_current_user, require_ops
from app.core import db
from app.core import observability as obs
from app.core.log_setup import list_log_dates, read_day_logs
from app.services import skill_review

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/observability", tags=["observability"])

# 聚合结果进程内 TTL 缓存：重聚合不阻塞事件循环（to_thread），短时间内重复请求直接命中；
# refresh=1 旁路缓存强制重算（前端手动刷新按钮使用）。
_OBS_TTL = 30.0
_obs_cache: dict = {}
_obs_inflight: dict = {}


async def _cached_agg(key: str, fn, *args, refresh: bool = False) -> dict:
    """聚合结果 TTL 缓存 + stale-while-revalidate（需求6：重开面板不再卡冷算）。

    - 命中未过期：直接返回；
    - 已过期但有旧值：立即返回旧值，同时后台异步重算刷新缓存（重开秒出）；
    - 首次无缓存：等待一次重算（inflight 去重并发请求）；
    - refresh=1：旁路缓存强制重算并等待（前端手动刷新按钮）。
    """
    now = time.monotonic()
    ent = _obs_cache.get(key)
    if ent and not refresh and now - ent[0] < _OBS_TTL:
        return ent[1]
    if ent and not refresh:
        # stale-while-revalidate：先回旧值，后台重算不阻塞本次响应
        if key not in _obs_inflight:
            async def _revalidate():
                try:
                    fresh = await asyncio.to_thread(fn, *args)
                    _obs_cache[key] = (time.monotonic(), fresh)
                except Exception as e:  # 后台刷新失败保留旧值即可
                    logger.warning("后台重聚合失败 key=%s: %s", key, e)
                finally:
                    _obs_inflight.pop(key, None)
            _obs_inflight[key] = asyncio.ensure_future(_revalidate())
        return ent[1]
    task = _obs_inflight.get(key)
    if task is None or refresh:
        async def _run():
            try:
                data = await asyncio.to_thread(fn, *args)
            finally:
                _obs_inflight.pop(key, None)
            return data
        task = asyncio.ensure_future(_run())
        _obs_inflight[key] = task
    data = await task
    _obs_cache[key] = (time.monotonic(), data)
    return data


@router.get("/my-metrics")
def get_my_metrics(
    response: Response,
    days: int = Query(7, ge=1, le=30),
    range_mode: str = Query("days", alias="range", pattern="^(days|today)$"),
    user: dict = Depends(get_current_user),
):
    """登录用户的个人业务概览；身份只取服务端会话，不接受查询目标用户。"""
    user_id = user.get("id")
    if user.get("role") not in ("user", "admin", "ops") or not user_id or user_id in ("guest", "system", "-"):
        raise HTTPException(status_code=403, detail="请使用正式账号查看个人使用概览")
    response.headers["Cache-Control"] = "no-store"
    return {"success": True, **obs.personal_metrics(user_id, days, range_mode)}


@router.get("/my-context")
def get_my_context(
    response: Response,
    days: int = Query(7, ge=1, le=30),
    range_mode: str = Query("days", alias="range", pattern="^(days|today)$"),
    user: dict = Depends(get_current_user),
):
    """当前登录用户的「上下文窗口 + 记忆模块」使用概览；身份只取服务端会话。"""
    user_id = user.get("id")
    if user.get("role") not in ("user", "admin", "ops") or not user_id or user_id in ("guest", "system", "-"):
        raise HTTPException(status_code=403, detail="请使用正式账号查看个人上下文与记忆概览")
    response.headers["Cache-Control"] = "no-store"
    try:
        data = obs.personal_context_overview(user_id, days, range_mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, **data}


@router.get("/my-context/sessions")
def get_my_context_sessions(
    response: Response,
    days: int = Query(7, ge=1, le=30),
    limit: int = Query(20, ge=1, le=50),
    range_mode: str = Query("days", alias="range", pattern="^(days|today)$"),
    user: dict = Depends(get_current_user),
):
    """按对话窗口下钻的上下文构成：个人面板总集口径的会话级子集切片。"""
    user_id = user.get("id")
    if user.get("role") not in ("user", "admin", "ops") or not user_id or user_id in ("guest", "system", "-"):
        raise HTTPException(status_code=403, detail="请使用正式账号查看按对话的上下文下钻")
    response.headers["Cache-Control"] = "no-store"
    try:
        data = obs.personal_session_contexts(user_id, days, limit, range_mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, **data}


@router.get("/my-context/memory-drill")
def get_my_memory_drill(
    response: Response,
    kind: str = Query("all", max_length=32),
    user: dict = Depends(get_current_user),
):
    """记忆概览钻取：点击记忆指标/类型条查看明细 + 口径说明；身份只取服务端会话。"""
    from app.services.memory import memory_service
    user_id = user.get("id")
    if user.get("role") not in ("user", "admin", "ops") or not user_id or user_id in ("guest", "system", "-"):
        raise HTTPException(status_code=403, detail="请使用正式账号查看记忆明细")
    response.headers["Cache-Control"] = "no-store"
    return {"success": True, **memory_service.drill_memories(user_id, kind)}


@router.get("/metrics")
async def get_metrics(
    days: int = Query(7, ge=1, le=90),
    range_mode: str = Query("days", alias="range", pattern="^(days|today)$"),
    refresh: int = Query(0, ge=0, le=1),
    _admin: dict = Depends(require_ops),
):
    """聚合指标：总览 + 每日趋势 + 按 Agent + 按工具 + 黄金信号（监控与告警数据源）"""
    data = await _cached_agg(f"metrics:{days}:{range_mode}", obs.metrics, days, range_mode, refresh=bool(refresh))
    return {"success": True, **data}


@router.get("/tool-routing")
async def get_tool_routing(
    days: int = Query(7, ge=1, le=90),
    range_mode: str = Query("days", alias="range", pattern="^(days|today)$"),
    refresh: int = Query(0, ge=0, le=1),
    _admin: dict = Depends(require_ops),
):
    """Tool Routing 调用链分析（req2）：参数合法率 / 任务成功率 / Schema Token /
    端到端延迟 / 越权暴露率 + 按工具/命名空间聚合 + 拦截事件明细。"""
    data = await _cached_agg(f"tool_routing:{days}:{range_mode}", obs.tool_routing_metrics,
                             days, range_mode, refresh=bool(refresh))
    return {"success": True, **data}


@router.get("/tool-registry")
async def get_tool_registry(_admin: dict = Depends(require_ops)):
    """标准化工具注册表快照（req1）：命名空间/标签/场景/参数 Schema/权限/风险等级/可用状态，
    附 function-calling schema 与 Schema Token 估算。"""
    from app.services.tools import base as tools_base

    return {
        "success": True,
        "tools": tools_base.registry_snapshot(),
        "function_schemas": tools_base.function_schemas(),
        "schema_tokens": tools_base.schema_token_estimate(),
    }


@router.get("/skill-reviews")
async def get_skill_reviews(
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _admin: dict = Depends(require_ops),
):
    """Skill 预审记录：汇总（拒绝数/高危/能力不支持）+ 明细列表（按时间倒序）"""
    data = await asyncio.to_thread(skill_review.list_reviews, limit, offset)
    return {"success": True, **data}


@router.get("/sediment-suggestions")
async def get_sediment_suggestions(
    days: int = Query(30, ge=1, le=180),
    threshold: int = Query(3, ge=2, le=50),
    _admin: dict = Depends(require_ops),
):
    """能力缺口沉淀建议（需求6）：同类场景在统计窗口内出现次数达阈值时，
    提示「xx场景，是否沉淀为 skill/工具」，支撑系统可进化性。"""
    from app.services import capability_gap

    data = await asyncio.to_thread(capability_gap.frequency_suggestions, threshold, days)
    return {"success": True, **data}


@router.get("/sessions/{session_id}")
async def get_session_observability(
    session_id: str,
    day: str = Query("", max_length=10),
    trace_id: str = Query(""),
    user: dict = Depends(get_current_user),
):
    """单个对话的统一可观测视图（需求2）：上下文构成 + 请求追踪 + 调用链 + 记忆合一。

    权限：ops/admin 可查看任意会话；普通用户仅可查看自己所属会话。
    """
    from app.core import db as _db

    if user.get("role") not in ("admin", "ops"):
        row = _db.query_one(
            "SELECT user_id FROM chat_sessions WHERE session_id = ?", (session_id,)
        )
        owner = (row or {}).get("user_id") or ""
        if not owner or owner != user.get("id"):
            raise HTTPException(status_code=403, detail="无权查看该对话的可观测数据")
    data = await asyncio.to_thread(obs.session_observability, session_id, day, trace_id)
    return {"success": True, **data}


@router.get("/traces")
async def list_traces(
    agent: str = Query(""),
    status: str = Query(""),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _admin: dict = Depends(require_ops),
):
    """trace 列表（按时间倒序，可按 Agent / 状态过滤）"""
    data = await asyncio.to_thread(obs.list_traces, agent, status, limit, offset)
    return {"success": True, **data}


@router.get("/traces/{trace_id}")
async def get_trace(trace_id: str, _admin: dict = Depends(require_ops)):
    """trace 全链路详情：所有步骤 span（llm span 含完整 prompt 与 response）"""
    trace = await asyncio.to_thread(obs.get_trace, trace_id)
    if not trace:
        return {"success": False, "detail": "trace 不存在"}
    return {"success": True, "trace": trace}


@router.get("/traces/{trace_id}/logs")
def get_trace_logs(
    trace_id: UUID,
    response: Response,
    cursor: str = Query("", max_length=40),
    limit: int = Query(100, ge=1, le=200),
    _admin: dict = Depends(require_ops),
):
    """分页读取请求日志；同步端点在线程池中执行，避免文件扫描阻塞事件循环。"""
    try:
        result = obs.get_trace_logs(str(trace_id), cursor, limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="trace 不存在或已清理")
    response.headers["Cache-Control"] = "no-store"
    return {"success": True, **result}


@router.get("/logs/meta")
def get_logs_meta(_admin: dict = Depends(require_ops)):
    """日志记录模块：可选日期（降序）与用户列表；同步扫目录在线程池中执行。"""
    users = [r["username"] for r in db.query("SELECT username FROM users ORDER BY username")]
    return {"success": True, "dates": list_log_dates(), "users": users + ["guest", "system", "-"]}


@router.get("/logs")
def get_logs(
    response: Response,
    date: str = Query("", pattern="^$|^\\d{4}-\\d{2}-\\d{2}$"),
    user: str = Query("", max_length=60),
    level: str = Query("", max_length=10),
    q: str = Query("", max_length=120),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _admin: dict = Depends(require_ops),
):
    """单日系统日志查询（按用户/级别/关键词过滤）；同步文件解析在线程池中执行。"""
    day = date or (list_log_dates() or [""])[0]
    result = read_day_logs(day, user, level, q, limit, offset)
    response.headers["Cache-Control"] = "no-store"
    return {"success": True, **result}


@router.get("/monitor")
async def get_monitor(
    days: int = Query(7, ge=1, le=90),
    range_mode: str = Query("days", alias="range", pattern="^(days|today)$"),
    refresh: int = Query(0, ge=0, le=1),
    _admin: dict = Depends(require_ops),
):
    """指标监控四方面：质量 / 安全 / 业务聚合（性能方面复用 /metrics 口径）。"""
    data = await _cached_agg(f"monitor:{days}:{range_mode}", obs.monitor_metrics,
                             days, range_mode, refresh=bool(refresh))
    return {"success": True, **data}


@router.get("/tool-chains")
async def get_tool_chains(
    days: int = Query(7, ge=1, le=90),
    range_mode: str = Query("days", alias="range", pattern="^(days|today)$"),
    limit: int = Query(50, ge=1, le=200),
    refresh: int = Query(0, ge=0, le=1),
    _admin: dict = Depends(require_ops),
):
    """工具调用链路：按 trace 聚合的工具 span 序列（调用链 / 失败数 / 耗时）。"""
    data = await _cached_agg(f"tool_chains:{days}:{range_mode}:{limit}", obs.tool_chains,
                             days, range_mode, limit, refresh=bool(refresh))
    return {"success": True, **data}


@router.get("/dashboard")
async def get_dashboard(user: dict = Depends(get_current_user)):
    """DashBoard 偏好：当前用户选中的指标卡；未保存过返回空数组（前端用默认集）。"""
    row = db.query_one("SELECT cards FROM obs_dashboard_prefs WHERE user_id = ?", (user["id"],))
    try:
        cards = json.loads(row["cards"]) if row else []
    except (TypeError, ValueError):
        cards = []
    if not isinstance(cards, list) or not all(isinstance(c, str) for c in cards):
        cards = []
    return {"success": True, "cards": cards}


@router.put("/dashboard")
async def put_dashboard(body: dict, user: dict = Depends(get_current_user)):
    """保存 DashBoard 选卡偏好（按用户持久化）。"""
    cards = body.get("cards")
    if not isinstance(cards, list) or not all(isinstance(c, str) for c in cards) or len(cards) > 40:
        raise HTTPException(status_code=400, detail="cards 必须是不超过 40 项的字符串数组")
    from datetime import datetime as _dt

    db.execute(
        "INSERT INTO obs_dashboard_prefs (user_id, cards, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(user_id) DO UPDATE SET cards = excluded.cards, updated_at = excluded.updated_at",
        (user["id"], json.dumps(cards[:40], ensure_ascii=False), _dt.now().isoformat(timespec="seconds")),
    )
    return {"success": True, "cards": cards[:40]}


# ---------------------------------------------------------------------------
# 指标钻取：DashBoard/概况面板数字点击 → 明细列表（通用列定义 + 行）
# ---------------------------------------------------------------------------
_DRILL_TYPES = (
    "sessions|messages|active_users|uploads|kbs|wiki|memories|feedbacks"
    "|eval_runs|sec_events|tool_calls|tool_blocked|traces|skill_reviews"
    "|llm_spans|tool_spans"
)

# 需求2：各钻取类型的口径说明（呈现在明细表格下方，告知用户该数值如何算出）
_DRILL_EXPLAIN = {
    "traces": "请求总数 = 统计区间内状态非 running 的 Agent 请求（trace）条数；每条 trace 代表一次用户请求的完整运行。",
    "llm_spans": "LLM 调用 = 区间内所有请求产生的 LLM 调用 span 总数（一次请求可能多次调用 LLM，故可能大于请求数）；Token 消耗 = 各 span 的 prompt+completion 之和。",
    "tool_spans": "工具调用 = 区间内所有请求产生的工具调用 span 总数（含知识检索/联网搜索/附件工具等每一步）。",
    "tool_calls": "工具路由调用 = tool_routing 表中登记的受治理工具调用记录（含命名空间/风险/拦截码）。",
    "tool_blocked": "工具约束拦截 = tool_routing 中 allowed=0 或 risk_blocked=1 或 rate_limited=1 的记录数。",
    "sessions": "会话总数 = 区间内创建的对话会话（chat_sessions）条数。",
    "messages": "消息总数 = 区间内用户+助手消息（chat_messages）条数。",
    "active_users": "活跃用户 = 区间内产生过消息的去重用户数（按消息归属会话的 owner 统计）。",
    "uploads": "上传记录 = 区间内图片/文档上传入库（upload_records）条数。",
    "kbs": "知识库 = 区间内创建的知识库（knowledge_bases）个数。",
    "wiki": "百科页面 = 区间内创建的 llm-wiki 编译页面（wiki_pages）个数。",
    "memories": "记忆条数 = 区间内抽取的跨会话用户记忆（user_memories）条数。",
    "feedbacks": "点赞/点踩 = 区间内用户反馈（message_feedbacks）记录，rating 区分赞/踩。",
    "eval_runs": "回归评测运行 = 区间内发起的评测运行（eval_runs）次数，摘要含成功率与相关度均值。",
    "skill_reviews": "Skill 预审 = 区间内添加/修改技能时的安全与能力可行性评审记录，verdict=reject 计为高危拒绝。",
    "sec_events": "网关安全事件 = 区间内网关安全审计（gateway_security_events）事件总数，按 risk_level 分级。",
}


def _drill_explain(drill_type: str) -> str:
    return _DRILL_EXPLAIN.get(drill_type, "该指标为对应数据表在统计区间内的记录聚合。")


def _count_since(table: str, col: str, since: str, extra: str = "") -> int:
    """通用计数：since 非空时按 col>=since 过滤，extra 为附加布尔条件。"""
    conds, params = [], []
    if since:
        conds.append(f"{col} >= ?")
        params.append(since)
    if extra:
        conds.append(extra)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    try:
        return int(db.query_one(f"SELECT COUNT(*) AS c FROM {table}{where}", tuple(params))["c"])
    except Exception:
        return 0


def _trace_agg(since: str) -> dict:
    """与卡片 overview 同源：agent_traces（status≠running）在区间内的聚合值。"""
    where = " WHERE status != 'running'" + (" AND started_at >= ?" if since else "")
    params = (since,) if since else ()
    return db.query_one(
        "SELECT COUNT(*) AS total, COALESCE(SUM(status='ok'),0) AS ok,"
        " COALESCE(SUM(status='error'),0) AS errors,"
        " COALESCE(AVG(latency_ms),0) AS avg_latency,"
        " COALESCE(SUM(prompt_tokens),0) AS pt, COALESCE(SUM(completion_tokens),0) AS ct,"
        " COALESCE(SUM(llm_calls),0) AS llm, COALESCE(SUM(tool_calls),0) AS tool"
        f" FROM agent_traces{where}", params) or {}


def _p95_latency(since: str) -> int:
    latencies = [r["latency_ms"] for r in db.query(
        "SELECT latency_ms FROM agent_traces WHERE status != 'running'"
        + (" AND started_at >= ?" if since else "")
        + " ORDER BY started_at DESC LIMIT 5000", (since,) if since else ())]
    if not latencies:
        return 0
    latencies.sort()
    return int(latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))])


def _metric_explain(metric: str, drill_type: str, since: str) -> str:
    """按具体指标（而非仅钻取类型）返回口径说明，并代入真实数值解释「这个数怎么来的」。

    需求：同一 drill_type 下多个指标（如 traces 下的请求总数/成功率/平均延迟）此前共用
    一段说明；现按指标 label 分别给出各自计算口径 + 实际数值来源，逻辑保持一致。
    """
    m = (metric or "").strip()
    base = "统计区间内 agent_traces 表（status≠running，即已结束的运行）"
    try:
        if m == "请求总数":
            a = _trace_agg(since)
            return (f"请求总数 = {a.get('total', 0)}。计算口径：{base}的记录条数，每条 trace 对应一次完整的"
                    f"用户请求运行（start_trace→end_trace）。下方明细逐条列出这些请求，分页展示。")
        if m == "成功率":
            a = _trace_agg(since)
            tot, ok = int(a.get('total', 0)), int(a.get('ok', 0))
            rate = round(ok / tot * 100, 1) if tot else 0.0
            return (f"成功率 = {rate}%。计算口径：状态为 ok 的请求数({ok}) ÷ 已结束请求总数({tot})，"
                    f"即 {ok} ÷ {tot} = {rate}%。error/timeout/cancelled/interrupted 计入分母但不计入分子。"
                    f"下方明细列出全部请求，可在「状态」列核对。")
        if m == "失败请求":
            a = _trace_agg(since)
            return (f"失败请求 = {a.get('errors', 0)}。计算口径：{base}中 status='error' 的记录条数"
                    f"（不含 timeout/cancelled/interrupted）。下方明细可在「状态」列筛选核对。")
        if m == "平均延迟":
            a = _trace_agg(since)
            return (f"平均延迟 = {int(a.get('avg_latency', 0))} ms。计算口径：{base}所有请求端到端 latency_ms "
                    f"的算术平均 AVG(latency_ms)（共 {a.get('total', 0)} 条）。latency_ms 为 start_trace→end_trace 的墙钟耗时。")
        if m == "P95 延迟":
            a = _trace_agg(since)
            return (f"P95 延迟 = {_p95_latency(since)} ms。计算口径：把{base}的 latency_ms 升序排列"
                    f"（共 {a.get('total', 0)} 条），取第 95 百分位（索引 ⌊N×0.95⌋）的值，即 95% 的请求耗时不超过此值。")
        if m == "平均 Token/请求":
            a = _trace_agg(since)
            tot = int(a.get('total', 0))
            tt = int(a.get('pt', 0)) + int(a.get('ct', 0))
            avg = round(tt / tot, 1) if tot else 0.0
            return (f"平均 Token/请求 = {avg}。计算口径：区间 Token 总量({tt}) ÷ 已结束请求数({tot})，"
                    f"即 {tt} ÷ {tot} = {avg}。Token 总量 = prompt_tokens 之和 + completion_tokens 之和。")
        if m == "Token 消耗":
            a = _trace_agg(since)
            pt, ct = int(a.get('pt', 0)), int(a.get('ct', 0))
            return (f"Token 消耗 = {pt + ct}。计算口径：{base}的 prompt_tokens 之和({pt}) + completion_tokens "
                    f"之和({ct})，即 {pt} + {ct} = {pt + ct}。每条 trace 的 token 由该请求内各次 LLM 调用 span 上报的"
                    f"真实 usage 累加；LLM 未上报 usage（如流式）时按字符估算兜底（CJK 1:1、其余每 4 字符≈1 token）。"
                    f"下方明细为对应的每次 LLM 调用 span（含单次 prompt/completion），按时间倒序分页展示。")
        if m == "LLM 调用":
            a = _trace_agg(since)
            return (f"LLM 调用 = {a.get('llm', 0)}。计算口径：{base}的 llm_calls 之和；每条 trace 的 llm_calls 在 "
                    f"end_trace 时按其 agent_spans 中 kind='llm' 的 span 数聚合而来，故等于区间内 LLM 调用 span 总数"
                    f"（一次请求可能多次调用 LLM）。下方明细逐条列出这些 LLM 调用 span。")
        if m == "工具调用":
            a = _trace_agg(since)
            return (f"工具调用 = {a.get('tool', 0)}。计算口径：{base}的 tool_calls 之和；每条 trace 的 tool_calls 在 "
                    f"end_trace 时按其 agent_spans 中 kind='tool' 的 span 数聚合而来（含知识检索/联网搜索/附件处理等每一步）。"
                    f"下方明细逐条列出这些工具调用 span。")
        if m == "点赞":
            c = _count_since("message_feedbacks", "created_at", since, "rating = 'like'")
            return (f"点赞 = {c}。计算口径：区间内 message_feedbacks 表中 rating='like' 的记录条数。"
                    f"下方明细列出全部反馈（含赞与踩），可在「评价」列区分。")
        if m == "点踩":
            c = _count_since("message_feedbacks", "created_at", since, "rating = 'dislike'")
            return (f"点踩 = {c}。计算口径：区间内 message_feedbacks 表中 rating='dislike' 的记录条数。"
                    f"下方明细列出全部反馈（含赞与踩），可在「评价」列区分。")
        if m == "点赞 / 点踩":
            lk = _count_since("message_feedbacks", "created_at", since, "rating = 'like'")
            dk = _count_since("message_feedbacks", "created_at", since, "rating = 'dislike'")
            return (f"点赞 / 点踩 = {lk} / {dk}。计算口径：区间内 message_feedbacks 表按 rating 拆分统计——"
                    f"rating='like' 计 {lk} 条、rating='dislike' 计 {dk} 条。下方明细列出全部反馈，「评价」列区分赞/踩。")
        if m in ("回归评测运行", "最近回归·答案相关度"):
            c = _count_since("eval_runs", "started_at", since)
            return (f"回归评测运行 = {c}。计算口径：区间内 eval_runs 表登记的评测运行次数（按 started_at 过滤）。"
                    f"「最近回归·答案相关度」取最近一次运行 eval_results 的 AVG(answer_relevance)。下方明细列出各次运行及结果摘要。")
        if m in ("Skill 预审高危", "Skill 预审（高危）"):
            high = _count_since("skill_review_logs", "created_at", since, "risk = 'high'")
            tot = _count_since("skill_review_logs", "created_at", since)
            return (f"Skill 预审高危 = {high}。计算口径：区间内 skill_review_logs 表中 risk='high'（命中安全规则）的评审"
                    f"记录条数；同期评审总数为 {tot} 条。下方明细列出全部预审记录，拒绝行浅红高亮，含风险与原因。")
        if m == "网关安全事件":
            c = _count_since("gateway_security_events", "created_at", since)
            return (f"网关安全事件 = {c}。计算口径：区间内 gateway_security_events 表登记的网关安全审计事件总数。"
                    f"下方明细按「风险」列（risk_level）分级展示。")
        if m == "安全事件待处置":
            c = _count_since("gateway_security_events", "created_at", since, "status = 'pending'")
            return (f"安全事件待处置 = {c}。计算口径：区间内 gateway_security_events 表中 status='pending'（尚未处置）的"
                    f"事件条数。下方明细可在「处置状态」列核对。")
        if m == "待处置 / 高危":
            pend = _count_since("gateway_security_events", "created_at", since, "status = 'pending'")
            hc = _count_since("gateway_security_events", "created_at", since, "risk_level IN ('high','critical')")
            return (f"待处置 / 高危 = {pend} / {hc}。计算口径：区间内 gateway_security_events 表分别统计——"
                    f"status='pending' 计 {pend} 条、risk_level∈(high,critical) 计 {hc} 条。下方明细列出全部事件。")
        if m == "高危/极高事件":
            hc = _count_since("gateway_security_events", "created_at", since, "risk_level IN ('high','critical')")
            return (f"高危/极高事件 = {hc}。计算口径：区间内 gateway_security_events 表中 risk_level∈(high,critical) 的"
                    f"事件条数。下方明细可在「风险」列核对。")
        if m == "工具约束拦截":
            c = _count_since("tool_routing", "created_at", since,
                             "(allowed = 0 OR risk_blocked = 1 OR rate_limited = 1)")
            return (f"工具约束拦截 = {c}。计算口径：区间内 tool_routing 表中 allowed=0 或 risk_blocked=1 或 "
                    f"rate_limited=1 的记录条数（被风险/权限/限流拦截的工具调用）。下方明细含拦截码。")
        if m == "会话总数":
            c = _count_since("chat_sessions", "created_at", since)
            return f"会话总数 = {c}。计算口径：区间内 chat_sessions 表创建的对话会话条数（按 created_at 过滤）。下方明细逐条列出会话及其消息数。"
        if m == "消息总数":
            c = _count_since("chat_messages", "created_at", since)
            return f"消息总数 = {c}。计算口径：区间内 chat_messages 表的用户+助手消息条数（按 created_at 过滤，不区分角色）。下方明细逐条列出消息。"
        if m == "活跃用户":
            day_since = since[:10] if since else ""
            c = _count_since("usage_daily", "day", day_since)
            distinct = 0
            try:
                w = " WHERE day >= ?" if day_since else ""
                distinct = int(db.query_one(
                    f"SELECT COUNT(DISTINCT user_id) AS c FROM usage_daily{w}",
                    (day_since,) if day_since else ())["c"])
            except Exception:
                distinct = 0
            return (f"活跃用户 = {distinct}。计算口径：区间内 usage_daily 表中产生过用量的去重用户数"
                    f"COUNT(DISTINCT user_id)（按 day 过滤）。下方明细按用户聚合其在 chat_messages 中的消息数/会话数。")
        if m == "上传记录":
            c = _count_since("upload_records", "created_at", since)
            return f"上传记录 = {c}。计算口径：区间内 upload_records 表登记的图片/文档上传入库记录条数。下方明细含文件、类型、分块数与上传者。"
        if m == "知识库":
            c = _count_since("knowledge_bases", "created_at", since)
            return f"知识库 = {c}。计算口径：区间内 knowledge_bases 表创建的知识库个数（按 created_at 过滤）。下方明细逐个列出知识库及其主题、可见性、创建者。"
        if m == "百科页面":
            c = _count_since("wiki_pages", "created_at", since)
            return f"百科页面 = {c}。计算口径：区间内 wiki_pages 表创建的 llm-wiki 编译页面个数（按 created_at 过滤）。下方明细逐个列出页面及其类型、所有者。"
        if m == "记忆条数":
            c = _count_since("user_memories", "created_at", since)
            return f"记忆条数 = {c}。计算口径：区间内 user_memories 表抽取的跨会话用户记忆条数（按 created_at 过滤）。下方明细逐条列出记忆内容、类型与重要度。"
    except Exception:
        pass
    # 未匹配到具体指标 label 时回退到按钻取类型的通用口径说明
    return _drill_explain(drill_type)


def _drill_usernames() -> dict:
    return {r["id"]: r["username"] for r in db.query("SELECT id, username FROM users")}


def _drill_query(drill_type: str, limit: int, names: dict, since: str = "", offset: int = 0):
    """按钻取类型返回 (columns, rows, total)；列定义供前端通用表格渲染。

    since（ISO 时间戳）非空时，明细与总数都按对应时间列过滤，使钻取口径与
    DashBoard 卡片（今日 / 近 N 天）一致（需求2/3）。
    """
    def _w(col: str):
        """返回 (where_sql, params)：since 非空时按 col >= since 过滤。"""
        return (f" WHERE {col} >= ?", (since,)) if since else ("", ())

    if drill_type == "sessions":
        cols = [{"key": "title", "label": "会话标题"}, {"key": "username", "label": "用户"},
                {"key": "msg_count", "label": "消息数"}, {"key": "created_at", "label": "创建时间"},
                {"key": "updated_at", "label": "最近更新"}]
        w, p = _w("s.created_at")
        rows = db.query(
            "SELECT s.session_id, s.title, s.user_id, s.created_at, s.updated_at,"
            " (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.session_id) AS msg_count"
            f" FROM chat_sessions s{w} ORDER BY s.updated_at DESC LIMIT ? OFFSET ?", (*p, limit, offset))
        total = db.query_one(f"SELECT COUNT(*) AS c FROM chat_sessions s{w}", p)["c"]
        for r in rows:
            r["username"] = names.get(r.pop("user_id", ""), "游客")
            r["created_at"] = (r["created_at"] or "")[:16]
            r["updated_at"] = (r["updated_at"] or "")[:16]
        return cols, rows, total
    if drill_type == "messages":
        cols = [{"key": "created_at", "label": "时间"}, {"key": "title", "label": "所属会话"},
                {"key": "role", "label": "角色"}, {"key": "content", "label": "内容摘要"}]
        w, p = _w("m.created_at")
        rows = db.query(
            "SELECT m.created_at, m.role, m.content, COALESCE(s.title, '-') AS title"
            " FROM chat_messages m LEFT JOIN chat_sessions s ON s.session_id = m.session_id"
            f"{w} ORDER BY m.rowid DESC LIMIT ? OFFSET ?", (*p, limit, offset))
        total = db.query_one(f"SELECT COUNT(*) AS c FROM chat_messages m{w}", p)["c"]
        for r in rows:
            r["created_at"] = (r["created_at"] or "")[:16]
            r["content"] = (r["content"] or "").replace("\n", " ")[:120]
        return cols, rows, total
    if drill_type == "active_users":
        # 需求9：chat_messages 无 user_id 列，JOIN chat_sessions 取归属用户
        cols = [{"key": "username", "label": "用户"}, {"key": "messages", "label": "消息数"},
                {"key": "sessions", "label": "会话数"}]
        w, p = _w("m.created_at")
        rows = db.query(
            "SELECT s.user_id AS user_id, COUNT(m.message_id) AS messages,"
            " COUNT(DISTINCT m.session_id) AS sessions"
            " FROM chat_messages m LEFT JOIN chat_sessions s ON s.session_id = m.session_id"
            f"{w} GROUP BY s.user_id ORDER BY messages DESC LIMIT ? OFFSET ?", (*p, limit, offset))
        total = db.query_one(
            "SELECT COUNT(DISTINCT s.user_id) AS c FROM chat_messages m"
            f" LEFT JOIN chat_sessions s ON s.session_id = m.session_id{w}", p)["c"]
        for r in rows:
            r["username"] = names.get(r.pop("user_id", ""), "游客")
        return cols, rows, total
    if drill_type == "uploads":
        cols = [{"key": "filename", "label": "文件"}, {"key": "source_type", "label": "类型"},
                {"key": "visibility", "label": "可见性"}, {"key": "username", "label": "上传者"},
                {"key": "chunk_count", "label": "分块"}, {"key": "created_at", "label": "时间"}]
        w, p = _w("created_at")
        rows = db.query(
            "SELECT filename, source_type, visibility, owner_id, chunk_count, created_at"
            f" FROM upload_records{w} ORDER BY created_at DESC LIMIT ? OFFSET ?", (*p, limit, offset))
        total = db.query_one(f"SELECT COUNT(*) AS c FROM upload_records{w}", p)["c"]
        for r in rows:
            r["username"] = names.get(r.pop("owner_id", ""), "-")
            r["created_at"] = (r["created_at"] or "")[:16]
        return cols, rows, total
    if drill_type == "kbs":
        cols = [{"key": "name", "label": "知识库"}, {"key": "topic", "label": "主题"},
                {"key": "visibility", "label": "可见性"}, {"key": "username", "label": "创建者"},
                {"key": "created_at", "label": "创建时间"}]
        w, p = _w("created_at")
        rows = db.query(
            "SELECT id, name, topic, visibility, owner_id, created_at"
            f" FROM knowledge_bases{w} ORDER BY created_at DESC LIMIT ? OFFSET ?", (*p, limit, offset))
        total = db.query_one(f"SELECT COUNT(*) AS c FROM knowledge_bases{w}", p)["c"]
        for r in rows:
            r["username"] = names.get(r.pop("owner_id", ""), "系统")
            r["created_at"] = (r["created_at"] or "")[:16]
        return cols, rows, total
    if drill_type == "wiki":
        cols = [{"key": "title", "label": "页面"}, {"key": "page_type", "label": "类型"},
                {"key": "visibility", "label": "可见性"}, {"key": "username", "label": "所有者"},
                {"key": "updated_at", "label": "更新时间"}]
        w, p = _w("created_at")
        rows = db.query(
            "SELECT title, page_type, visibility, owner_id, updated_at"
            f" FROM wiki_pages{w} ORDER BY updated_at DESC LIMIT ? OFFSET ?", (*p, limit, offset))
        total = db.query_one(f"SELECT COUNT(*) AS c FROM wiki_pages{w}", p)["c"]
        for r in rows:
            r["username"] = names.get(r.pop("owner_id", ""), "系统")
            r["updated_at"] = (r["updated_at"] or "")[:16]
        return cols, rows, total
    if drill_type == "memories":
        cols = [{"key": "username", "label": "用户"}, {"key": "memory_type", "label": "类型"},
                {"key": "content", "label": "记忆内容"}, {"key": "importance", "label": "重要度"},
                {"key": "updated_at", "label": "更新时间"}]
        w, p = _w("created_at")
        rows = db.query(
            "SELECT user_id, memory_type, content, importance, updated_at"
            f" FROM user_memories{w} ORDER BY updated_at DESC LIMIT ? OFFSET ?", (*p, limit, offset))
        total = db.query_one(f"SELECT COUNT(*) AS c FROM user_memories{w}", p)["c"]
        for r in rows:
            r["username"] = names.get(r.pop("user_id", ""), "-")
            r["content"] = (r["content"] or "").replace("\n", " ")[:120]
            r["updated_at"] = (r["updated_at"] or "")[:16]
        return cols, rows, total
    if drill_type == "feedbacks":
        cols = [{"key": "created_at", "label": "时间"}, {"key": "username", "label": "用户"},
                {"key": "rating", "label": "评价"}, {"key": "title", "label": "所属会话"},
                {"key": "comment", "label": "评论"}]
        w, p = _w("f.created_at")
        rows = db.query(
            "SELECT f.created_at, f.user_id, f.rating, f.comment, COALESCE(s.title, '-') AS title"
            " FROM message_feedbacks f LEFT JOIN chat_sessions s ON s.session_id = f.session_id"
            f"{w} ORDER BY f.created_at DESC LIMIT ? OFFSET ?", (*p, limit, offset))
        total = db.query_one(f"SELECT COUNT(*) AS c FROM message_feedbacks f{w}", p)["c"]
        for r in rows:
            r["username"] = names.get(r.pop("user_id", ""), "游客")
            r["created_at"] = (r["created_at"] or "")[:16]
            r["comment"] = (r["comment"] or "").replace("\n", " ")[:120]
        return cols, rows, total
    if drill_type == "eval_runs":
        cols = [{"key": "started_at", "label": "开始时间"}, {"key": "dataset_name", "label": "评测集"},
                {"key": "status", "label": "状态"}, {"key": "summary", "label": "结果摘要"},
                {"key": "created_by", "label": "发起人"}]
        w, p = _w("r.started_at")
        rows = db.query(
            "SELECT r.started_at, r.status, r.summary, r.created_by, COALESCE(d.name, '-') AS dataset_name"
            " FROM eval_runs r LEFT JOIN eval_datasets d ON d.id = r.dataset_id"
            f"{w} ORDER BY r.started_at DESC LIMIT ? OFFSET ?", (*p, limit, offset))
        total = db.query_one(f"SELECT COUNT(*) AS c FROM eval_runs r{w}", p)["c"]
        for r in rows:
            r["started_at"] = (r["started_at"] or "")[:16]
            try:
                s = json.loads(r.get("summary") or "{}")
            except (TypeError, ValueError):
                s = {}
            r["summary"] = (f"共{s.get('total')}例·成功{s.get('ok')}·相关{s.get('avg_answer_relevance')}"
                             if s.get("total") is not None else "-")
        return cols, rows, total
    if drill_type == "skill_reviews":
        # 需求2：Skill 预审（高危）钻取明细
        cols = [{"key": "created_at", "label": "时间"}, {"key": "username", "label": "用户"},
                {"key": "action", "label": "操作"}, {"key": "skill_name", "label": "技能"},
                {"key": "verdict", "label": "结果"}, {"key": "risk", "label": "风险"},
                {"key": "reasons", "label": "原因"}]
        w, p = _w("created_at")
        rows = db.query(
            "SELECT created_at, username, user_id, action, skill_name, verdict, risk, reasons"
            f" FROM skill_review_logs{w} ORDER BY created_at DESC LIMIT ? OFFSET ?", (*p, limit, offset))
        total = db.query_one(f"SELECT COUNT(*) AS c FROM skill_review_logs{w}", p)["c"]
        for r in rows:
            r["username"] = r.get("username") or names.get(r.get("user_id", ""), "-") or "-"
            r.pop("user_id", None)
            r["created_at"] = (r["created_at"] or "")[:16]
            try:
                rs = json.loads(r.get("reasons") or "[]")
            except (TypeError, ValueError):
                rs = r.get("reasons") or ""
            r["reasons"] = "；".join(rs) if isinstance(rs, list) else str(rs or "-")
            r["verdict"] = "已拒绝" if r.get("verdict") == "reject" else "通过"
        return cols, rows, total
    if drill_type == "sec_events":
        cols = [{"key": "created_at", "label": "时间"}, {"key": "client_ip", "label": "来源 IP"},
                {"key": "username", "label": "用户"}, {"key": "risk_level", "label": "风险"},
                {"key": "risk_score", "label": "分值"}, {"key": "status", "label": "处置状态"},
                {"key": "audit_action", "label": "审计动作"}]
        w, p = _w("created_at")
        rows = db.query(
            "SELECT created_at, client_ip, user_id, role, risk_level, risk_score, status, audit_action"
            f" FROM gateway_security_events{w} ORDER BY created_at DESC LIMIT ? OFFSET ?", (*p, limit, offset))
        total = db.query_one(f"SELECT COUNT(*) AS c FROM gateway_security_events{w}", p)["c"]
        for r in rows:
            r["username"] = names.get(r.pop("user_id", ""), "-") or "-"
            r.pop("role", None)
            r["created_at"] = (r["created_at"] or "")[:16]
        return cols, rows, total
    if drill_type in ("tool_calls", "tool_blocked"):
        cols = [{"key": "created_at", "label": "时间"}, {"key": "username", "label": "用户"},
                {"key": "tool_name", "label": "工具"}, {"key": "namespace", "label": "命名空间"},
                {"key": "risk_level", "label": "风险"}, {"key": "status", "label": "状态"},
                {"key": "latency_ms", "label": "耗时(ms)"}, {"key": "blocked_code", "label": "拦截码"}]
        conds, params = [], []
        if since:
            conds.append("created_at >= ?")
            params.append(since)
        if drill_type == "tool_blocked":
            conds.append("(allowed = 0 OR risk_blocked = 1 OR rate_limited = 1)")
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        rows = db.query(
            "SELECT created_at, user_id, tool_name, namespace, risk_level, status, latency_ms, blocked_code"
            f" FROM tool_routing{where} ORDER BY created_at DESC LIMIT ? OFFSET ?", (*params, limit, offset))
        total = db.query_one(f"SELECT COUNT(*) AS c FROM tool_routing{where}", tuple(params))["c"]
        for r in rows:
            r["username"] = names.get(r.pop("user_id", ""), "-") or "-"
            r["created_at"] = (r["created_at"] or "")[:16]
        return cols, rows, total
    # 需求3：span 级钻取，使明细条数与卡片 overview.llm_calls/tool_calls（span 计数）一致
    if drill_type in ("llm_spans", "tool_spans"):
        kind = "llm" if drill_type == "llm_spans" else "tool"
        if kind == "llm":
            cols = [{"key": "started_at", "label": "时间"}, {"key": "username", "label": "用户"},
                    {"key": "name", "label": "调用名称"}, {"key": "status", "label": "状态"},
                    {"key": "prompt_tokens", "label": "Prompt"}, {"key": "completion_tokens", "label": "Completion"},
                    {"key": "latency_ms", "label": "耗时(ms)"}, {"key": "input", "label": "输入摘要"}]
        else:
            cols = [{"key": "started_at", "label": "时间"}, {"key": "username", "label": "用户"},
                    {"key": "name", "label": "工具名称"}, {"key": "status", "label": "状态"},
                    {"key": "latency_ms", "label": "耗时(ms)"}, {"key": "input", "label": "输入摘要"}]
        conds = ["s.kind = ?", "t.status != 'running'"]
        params: list = [kind]
        if since:
            conds.append("s.started_at >= ?")
            params.append(since)
        where = " WHERE " + " AND ".join(conds)
        sel = ("s.started_at, s.name, s.status, s.latency_ms, s.input, t.user_id"
               + (", s.prompt_tokens, s.completion_tokens" if kind == "llm" else ""))
        rows = db.query(
            f"SELECT {sel} FROM agent_spans s JOIN agent_traces t ON t.id = s.trace_id"
            f"{where} ORDER BY s.started_at DESC LIMIT ? OFFSET ?", (*params, limit, offset))
        total = db.query_one(
            "SELECT COUNT(*) AS c FROM agent_spans s JOIN agent_traces t ON t.id = s.trace_id"
            f"{where}", tuple(params))["c"]
        for r in rows:
            r["username"] = names.get(r.pop("user_id", ""), "-") or "-"
            r["started_at"] = (r["started_at"] or "")[:16]
            r["input"] = (r["input"] or "").replace("\n", " ")[:80]
        return cols, rows, total
    # traces（需求2：排除 running，与卡片 overview.total 口径一致）
    cols = [{"key": "started_at", "label": "时间"}, {"key": "username", "label": "用户"},
            {"key": "agent", "label": "Agent"}, {"key": "input", "label": "请求输入"},
            {"key": "status", "label": "状态"}, {"key": "llm_calls", "label": "LLM"},
            {"key": "tool_calls", "label": "工具"}, {"key": "latency_ms", "label": "耗时(ms)"}]
    conds = ["status != 'running'"]
    params = []
    if since:
        conds.append("started_at >= ?")
        params.append(since)
    where = " WHERE " + " AND ".join(conds)
    rows = db.query(
        "SELECT id, started_at, user_id, agent, input, status, llm_calls, tool_calls, latency_ms"
        f" FROM agent_traces{where} ORDER BY started_at DESC LIMIT ? OFFSET ?", (*params, limit, offset))
    total = db.query_one(f"SELECT COUNT(*) AS c FROM agent_traces{where}", tuple(params))["c"]
    for r in rows:
        r["username"] = names.get(r.pop("user_id", ""), "-") or "-"
        r["started_at"] = (r["started_at"] or "")[:16]
        r["input"] = (r["input"] or "").replace("\n", " ")[:120]
    return cols, rows, total


@router.get("/drill")
async def drill_down(
    drill_type: str = Query(..., alias="type", pattern=f"^({_DRILL_TYPES})$"),
    limit: int = Query(200, ge=1, le=500),
    days: int = Query(7, ge=1, le=90),
    range_mode: str = Query("days", alias="range", pattern="^(days|today)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    metric: str = Query("", max_length=64),
    _admin: dict = Depends(require_ops),
):
    """指标数字钻取：返回明细列定义 + 行 + 总数 + 口径说明（与 DashBoard 卡片同时间窗口径）。

    需求2：服务端分页（page/page_size），单页不呈现过多数据；并返回 explanation 说明算法。
    metric（指标 label）非空时按具体指标给出口径说明并代入真实数值（解释该数怎么算出）；
    同一 drill_type 下不同指标不再共用同一段说明。
    """
    since = obs._range_since(days, range_mode)
    offset = (page - 1) * page_size
    columns, rows, total = await asyncio.to_thread(
        _drill_query, drill_type, page_size, _drill_usernames(), since, offset)
    explanation = await asyncio.to_thread(_metric_explain, metric, drill_type, since)
    return {"success": True, "type": drill_type, "columns": columns, "rows": rows,
            "total": total, "since": since, "page": page, "page_size": page_size,
            "metric": metric, "explanation": explanation}
