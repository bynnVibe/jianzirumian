"""
见字如面 - 模型网关代理主流程（OpenAI 兼容）

一次调用的完整链路（每步都记入可视化 trace_steps，便于问题排查与运维）：
    密钥鉴权 → 模型授权 → 安全审计（脱敏/阻断）→ 渠道选路（优先级+权重）
    → 原子请求认领 → 上游调用（首块前可切换渠道）→ Token 计量 → 日志入库 → 响应下游

对外契约统一为 OpenAI 兼容：POST /v1/chat/completions（支持 stream）。
所有终端性失败（鉴权失败/被阻断/无可用渠道）在 prepare 阶段写日志并抛 GatewayError，
由 API 层转换为对应 HTTP 状态码；上游失败在 failover 后仍无渠道可用时抛 502。
"""
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from typing import AsyncGenerator, List, Optional

import httpx

from app.config import settings as app_settings
from app.core.request_budget import remaining, iterate, close_resources
from app.core import db
from app.core import observability as agent_obs
from app.services.gateway import keys as key_svc
from app.services.gateway import logs as log_svc
from app.services.gateway import rules as rule_svc
from app.services.gateway import security
from app.services.gateway import security_events as sec_event_svc
from app.services.gateway import semantic_cache
from app.services.gateway import settings as settings_svc
from app.services.gateway import upstream
from app.services.gateway import upstreams as up_svc

logger = logging.getLogger("jianziruyang.gateway.proxy")

# 上游超时：连接短、读放宽（流式生成可能较久）
_TIMEOUT = httpx.Timeout(connect=10, read=300, write=30, pool=10)
# 触发故障切换的上游状态码（4xx 客户端错误除鉴权/限流外不重试）
_FAILOVER_STATUS = {401, 403, 408, 429}
# 首块保护：上游返回 200 但长时间不产出任何 chunk（排队/挂起）时视为失败上游并切换，
# 避免慢上游吃满调用方（意图改写/证据判断等）的全部超时预算后才暴露。
# 工具类角色（短输出任务）首块期限更紧；回答类放宽。
_UTILITY_ROLES = {"intent_rewrite", "evidence_check", "memory_extract", "session_summary"}
_FIRST_CHUNK_UTILITY = 8.0
_FIRST_CHUNK_DEFAULT = 15.0
# Qwen 系上游：工具类角色默认关思考，避免长思考链拖慢工具化流水线
_NO_THINK_HOSTS = ("modelscope", "dashscope", "aliyuncs")


class GatewayError(Exception):
    """网关终端性错误（携带 HTTP 状态码，供 API 层转换）。"""

    def __init__(self, status_code: int, message: str, code: str = "gateway_error"):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


class Tracer:
    """可视化调用链路：按序记录每个阶段的名称/状态/明细/耗时。"""

    def __init__(self):
        self.steps: List[dict] = []
        self._t_last = time.monotonic()

    def add(self, phase: str, name: str, status: str = "ok", detail: str = "") -> None:
        now = time.monotonic()
        self.steps.append({
            "seq": len(self.steps) + 1, "phase": phase, "name": name,
            "status": status, "detail": (detail or "")[:500],
            "elapsed_ms": int((now - self._t_last) * 1000),
            "at": datetime.now().isoformat(),
        })
        self._t_last = now

    def dump(self) -> List[dict]:
        return self.steps


class Ctx:
    """一次代理调用的上下文（prepare 产出，json/stream 响应消费）。"""

    def __init__(self):
        self.log_id = str(uuid.uuid4())
        self.created_at = datetime.now().isoformat()
        self.t_start = time.monotonic()
        self.deadline = self.t_start + max(0.01, app_settings.PROXY_REQUEST_TIMEOUT)
        self.finalized = False
        self.tracer = Tracer()
        self.settings: dict = {}
        self.key_row: dict = {}
        self.model_req = ""
        self.stream = False
        # 用户密钥配额用尽后自动降级为公共密钥的标记（友好提示/审计用）
        self.degraded_from_user_key = False
        # 请求类型：chat（默认）/ embeddings / rerank（与 chat 通道隔离）
        self.kind = "chat"
        # 业务路由提示（role/显式规则/发起用户）
        self.role = ""
        self.rule_id = ""
        self.user_id = ""
        self.forward_body: dict = {}
        self.findings: List[dict] = []
        self.risk_level = "none"
        self.risk_score = 0
        self.audit_action = ""
        self.candidates: List[dict] = []
        self.client_ip = ""
        # 上游结果（failover 后填充）
        self.rule: Optional[dict] = None
        self.upstream: Optional[dict] = None
        self.model_mapped = ""
        self.upstream_url = ""
        self.retries = 0
        # 语义缓存（需求：构建在网关层）：仅 chat_answer/assistant 且业务层传入
        # _gw_cache_* 提示时启用；命中则合成响应、跳过上游 LLM。
        self.cache_enabled = False
        self.cache_query = ""
        self.cache_scope = "personal"
        self.cache_realtime = False
        self.cache_hit = False


def _requested_tool_names(body: dict) -> List[str]:
    tools = body.get("tools") or []
    names = []
    for t in tools if isinstance(tools, list) else []:
        fn = (t or {}).get("function") or {}
        name = fn.get("name") or (t or {}).get("name")
        if name:
            names.append(name)
    return names


def _latency(ctx: Ctx) -> int:
    return int((time.monotonic() - ctx.t_start) * 1000)


# ---------------------------------------------------------------------------
# prepare：鉴权/模型授权 → 安全审计 → 选路 → 原子认领（同步；失败写日志并抛错）
# ---------------------------------------------------------------------------

def prepare(body: dict, raw_key: str, client_ip: str = "", kind: str = "chat") -> Ctx:
    ctx = Ctx()
    ctx.kind = kind or "chat"
    ctx.client_ip = client_ip or ""
    ctx.settings = settings_svc.get_settings()
    ctx.stream = bool(body.get("stream"))
    ctx.model_req = str(body.get("model") or "")
    ctx.forward_body = body
    # 业务路由提示（由 LLMFactory.create_for_role 通过 extra_body 注入，出网时被 upstream 剥离）
    ctx.role = str(body.get("_gw_role") or "")
    if not ctx.role:
        # embeddings/rerank 端点无 extra_body 注入机制，按 kind 默认角色（供 category 过滤选路）
        ctx.role = {"embeddings": "embed", "rerank": "rerank"}.get(ctx.kind, "")
    ctx.rule_id = str(body.get("_gw_rule_id") or "")
    ctx.user_id = str(body.get("_gw_user_id") or "")
    # 语义缓存提示（由业务层在意图识别后经 extra_body 注入）：
    # _gw_cache_query=意图识别后的问题（缓存键）、_gw_cache_scope=public|personal、
    # _gw_cache_realtime=是否实时。仅对回答类角色且无工具调用时启用。
    ctx.cache_query = str(body.get("_gw_cache_query") or "")
    ctx.cache_scope = "public" if str(body.get("_gw_cache_scope") or "") == "public" else "personal"
    ctx.cache_realtime = bool(body.get("_gw_cache_realtime"))
    ctx.cache_enabled = (
        kind == "chat" and ctx.role in ("chat_answer", "assistant")
        and bool(ctx.cache_query) and not _requested_tool_names(body)
        and semantic_cache.semantic_cache_enabled()
    )
    base = {
        "id": ctx.log_id, "created_at": ctx.created_at, "client_ip": ctx.client_ip,
        "model_requested": ctx.model_req, "stream": ctx.stream,
        "request_body": json.dumps(body, ensure_ascii=False),
        "key_id": "", "key_name": "",
        # 业务场景（role）：审计页据此展示「该链路是哪个业务模块发起的」
        "role": ctx.role,
    }

    # 1. 密钥鉴权（用户密钥总配额用尽时自动降级公共密钥，不阻碍系统正常使用）
    try:
        ctx.key_row = key_svc.authenticate(raw_key)
        base["key_id"] = ctx.key_row["id"]
        base["key_name"] = ctx.key_row.get("name") or ""
        ctx.tracer.add("auth", "密钥鉴权", "ok", f"密钥「{base['key_name']}」校验通过")
    except key_svc.KeyRejected as e:
        degraded = False
        if e.code == "quota_exceeded" and raw_key:
            # 用户密钥配额用尽 → 降级切换公共密钥（若该密钥确为用户密钥）
            row = db.query_one("SELECT id, key_type FROM gateway_keys WHERE key_hash=?",
                               (key_svc._hash(raw_key),))
            if row and (row.get("key_type") or "user") == "user":
                pub = key_svc.find_public_key()
                pub_plain = key_svc._plain_of(pub["id"]) if pub else ""
                if pub_plain:
                    try:
                        ctx.key_row = key_svc.authenticate(pub_plain)
                        base["key_id"] = ctx.key_row["id"]
                        base["key_name"] = ctx.key_row.get("name") or ""
                        ctx.degraded_from_user_key = True
                        degraded = True
                        ctx.tracer.add(
                            "auth", "密钥鉴权", "warn",
                            f"用户密钥配额用尽（{e.message}），已自动降级为公共密钥"
                            f"「{base['key_name']}」继续服务")
                        logger.warning("[网关密钥] 用户密钥配额用尽，降级公共密钥: user=%s err=%s",
                                       ctx.user_id or "-", e.message)
                    except key_svc.KeyRejected:
                        pass
        if not degraded:
            ctx.tracer.add("auth", "密钥鉴权", "error", e.message)
            log_svc.write_log({**base, "status": "error", "status_code": e.status_code,
                               "latency_ms": _latency(ctx), "trace_steps": ctx.tracer.dump(),
                               "error": e.message, "risk_level": "none"})
            raise GatewayError(e.status_code, e.message, e.code)

    # 1b. 每用户配额与流控：公共密钥按用户设每日限额（用尽提示联系管理员）；
    #     用户密钥执行每分钟流控（总配额已在鉴权/认领阶段执行）
    try:
        key_svc.enforce_user_quota(ctx.key_row, ctx.user_id)
    except key_svc.QuotaRejected as e:
        ctx.tracer.add("quota", "用户配额/流控", "error", e.message)
        log_svc.write_log({**base, "status": "error", "status_code": e.status_code,
                           "latency_ms": _latency(ctx), "trace_steps": ctx.tracer.dump(),
                           "error": e.message, "risk_level": "none"})
        raise GatewayError(e.status_code, e.message, e.code) from e
    if (ctx.key_row.get("key_type") or "") in ("public", "user"):
        ctx.tracer.add("quota", "用户配额/流控", "ok",
                       f"密钥类型={ctx.key_row.get('key_type')}，"
                       f"用户={ctx.user_id or '-'} 每用户限额/流控校验通过")

    # 2. 模型授权（此时尚不扣减请求次数）
    if not key_svc.check_model_allowed(ctx.key_row, ctx.model_req):
        msg = f"该密钥无权访问模型「{ctx.model_req}」"
        ctx.tracer.add("quota", "模型授权", "error", msg)
        log_svc.write_log({**base, "status": "blocked", "status_code": 403,
                           "latency_ms": _latency(ctx), "trace_steps": ctx.tracer.dump(),
                           "error": msg, "audit_action": "block"})
        raise GatewayError(403, msg, "model_not_allowed")
    ctx.tracer.add("quota", "模型授权", "ok",
                   f"密钥「{base['key_name']}」允许访问模型「{ctx.model_req}」"
                   "（可访问模型清单为空=不限）")

    # 3. 安全审计（脱敏 / 阻断）
    mode = ctx.settings.get("audit_mode", "audit")
    do_mask = (mode == "mask")
    scanned, findings, risk_level, risk_score = security.scan_payload(
        ctx.forward_body, ctx.settings, do_mask=do_mask)
    ctx.findings, ctx.risk_level, ctx.risk_score = findings, risk_level, risk_score
    ctx.audit_action = security.decide_action(findings, mode)
    if do_mask and findings:
        ctx.forward_body = scanned
        base["request_body"] = json.dumps(scanned, ensure_ascii=False)
    sec_status = "blocked" if ctx.audit_action == "block" else ("warn" if findings else "ok")
    ctx.tracer.add(
        "security", "安全审计", sec_status,
        f"模式={mode}，命中 {len(findings)} 项，风险等级={risk_level}"
        + (f"，动作={ctx.audit_action}" if ctx.audit_action else ""))
    # 3b. 风险事件闭环：命中风险即在安全审计中心落事件（前端轮询弹窗告警，
    #     处理策略由模型生成建议，运维处理后留痕）；事件落库失败不阻断主流程
    if findings:
        try:
            sec_event_svc.record_event(
                log_id=ctx.log_id, client_ip=ctx.client_ip, user_id=ctx.user_id,
                role=ctx.role, model=ctx.model_req, key_name=base.get("key_name", ""),
                risk_level=risk_level, risk_score=risk_score, findings=findings,
                audit_action=ctx.audit_action, settings=ctx.settings)
            ctx.tracer.add("security", "风险事件记录", "ok",
                           "已写入安全审计中心（待处理），前端将弹窗告警")
        except Exception as e:
            logger.warning("安全事件落库失败（不阻断请求）: %s", e)
    if ctx.audit_action == "block":
        log_svc.write_log({
            **base, "status": "blocked", "status_code": 403, "latency_ms": _latency(ctx),
            "risk_level": risk_level, "risk_score": risk_score, "risks": findings,
            "audit_action": "block", "trace_steps": ctx.tracer.dump(),
            "error": "命中安全审计规则，已阻断",
        })
        raise GatewayError(403, "请求命中安全审计规则，已被网关阻断", "blocked_by_audit")

    # 4. 规则选路（显式规则 > role 路由 > 模式匹配 > 默认规则）
    ctx.rule, ctx.candidates = rule_svc.select_upstreams(
        ctx.model_req, role=ctx.role or None,
        rule_id=ctx.rule_id or None, user_id=ctx.user_id or None)
    if not ctx.candidates:
        rule_name = (ctx.rule or {}).get("model_pattern") or ""
        if ctx.role and rule_name:
            # 业务路由严格绑定：规则内无上游支持该模型，拒绝跨规则回退，
            # 错误信息带 role/规则/模型上下文，避免误判为未配置
            msg = (f"路由规则「{rule_name}」(role={ctx.role}) 内无支持模型「{ctx.model_req}」的上游，"
                   f"已拒绝跨规则回退；请检查上游模型列表或业务路由绑定")
        else:
            msg = (f"没有可用于模型「{ctx.model_req}」的上游"
                   + (f"（role={ctx.role}，未绑定业务路由或绑定规则的上游不支持该模型）"
                      if ctx.role else "")
                   + "，请先在网关中配置上游与路由规则，或在业务路由中为该角色绑定含该模型的规则")
        ctx.tracer.add("route", "规则选路", "error", msg)
        log_svc.write_log({**base, "status": "error", "status_code": 503,
                           "latency_ms": _latency(ctx), "risk_level": risk_level,
                           "risk_score": risk_score, "risks": findings,
                           "trace_steps": ctx.tracer.dump(), "error": msg})
        raise GatewayError(503, msg, "no_upstream")
    pat = (ctx.rule or {}).get("model_pattern", "")
    health_note = "、".join(f"{c['name']}(W{c.get('weight', 100)})"
                           for c in ctx.candidates[:4])
    ctx.tracer.add("route", "规则选路", "ok",
                   f"role={ctx.role or '-'} 模型「{ctx.model_req}」命中规则「{pat}」，"
                   f"候选上游 {len(ctx.candidates)} 个：{health_note}")
    # 显性化日志：路由规则 → 模型 → 上游候选（与 trace_steps 同源，便于直接查日志文件）
    logger.info("[网关选路] role=%s 路由规则「%s」→ 模型「%s」 上游候选 %d 个：%s",
                ctx.role or "-", pat, ctx.model_req, len(ctx.candidates), health_note)
    ctx._base = base  # 供 finalize 复用
    try:
        remaining(ctx.deadline)
        ctx.key_row = key_svc.consume_request(ctx.key_row["id"], ctx.model_req)
    except key_svc.KeyRejected as e:
        ctx.tracer.add("quota", "原子配额认领", "error", e.message)
        _finalize(ctx, status="error", status_code=e.status_code, error=e.message)
        raise GatewayError(e.status_code, e.message, e.code) from e
    except TimeoutError as e:
        _finalize(ctx, status="timeout", status_code=504, error=str(e))
        raise GatewayError(504, str(e), "request_timeout") from e
    ctx.tracer.add("quota", "原子配额认领", "ok",
                   f"密钥「{ctx.key_row.get('name') or base['key_name']}」已认领第"
                   f" {ctx.key_row['request_count']} 次请求（配额 0=不限）")
    # 每用户请求计量（公共密钥每日限额/流控与「我的配额」展示的数据源）
    try:
        key_svc.record_usage(ctx.key_row["id"], ctx.user_id, requests=1)
    except Exception:
        pass
    return ctx


def _max_attempts(ctx: Ctx) -> int:
    return max(1, int(ctx.settings.get("retry_count", 2)) + 1)


def _first_chunk_limit(ctx: Ctx) -> float:
    """首块保护期限：工具类角色紧、回答类宽。"""
    return _FIRST_CHUNK_UTILITY if ctx.role in _UTILITY_ROLES else _FIRST_CHUNK_DEFAULT


def _suppress_utility_thinking(ctx: Ctx, eff: dict, payload) -> None:
    """工具类角色在 Qwen 系上游注入 enable_thinking=False（其他上游不含该 host 不受影响）。"""
    if ctx.role not in _UTILITY_ROLES or not isinstance(payload, dict):
        return
    host = str(eff.get("base_url") or "").lower()
    if any(h in host for h in _NO_THINK_HOSTS):
        payload.setdefault("enable_thinking", False)


def _finalize(ctx: Ctx, *, status: str, status_code: int, prompt_tokens: int = 0,
              completion_tokens: int = 0, response_preview: str = "",
              tool_calls: Optional[list] = None, error: str = "") -> None:
    """组装并写入最终日志，扣减密钥 Token 用量。"""
    if ctx.finalized:
        return
    ctx.finalized = True
    if not (prompt_tokens or completion_tokens) and response_preview and not ctx.cache_hit:
        completion_tokens = agent_obs.estimate_tokens(response_preview)
        prompt_tokens = agent_obs.estimate_tokens(json.dumps(ctx.forward_body.get("messages") or [], ensure_ascii=False))
    total = int(prompt_tokens or 0) + int(completion_tokens or 0)
    # 缓存命中未消耗上游资源 → 不扣减密钥 Token 用量/不计入用户 Token 配额
    if not ctx.cache_hit:
        try:
            key_svc.consume_tokens(ctx.key_row.get("id", ""), total)
        except Exception:
            pass
        try:
            key_svc.record_usage(ctx.key_row.get("id", ""), ctx.user_id, tokens=total)
        except Exception:
            pass
    req_tools = _requested_tool_names(ctx.forward_body)
    all_tools = list(dict.fromkeys(req_tools + [
        (tc.get("function", {}) or {}).get("name", "") for tc in (tool_calls or [])
    ]))
    log_svc.write_log({
        **getattr(ctx, "_base", {}),
        "channel_id": (ctx.rule or {}).get("id", ""),
        "channel_name": (ctx.rule or {}).get("model_pattern", ""),
        "model_mapped": ctx.model_mapped, "upstream_url": ctx.upstream_url,
        "status": status, "status_code": status_code, "retries": ctx.retries,
        "prompt_tokens": int(prompt_tokens or 0), "completion_tokens": int(completion_tokens or 0),
        "total_tokens": total, "latency_ms": _latency(ctx),
        "response_preview": response_preview, "tool_calls": [{"name": t} for t in all_tools if t],
        "risk_level": ctx.risk_level, "risk_score": ctx.risk_score, "risks": ctx.findings,
        "audit_action": ctx.audit_action, "trace_steps": ctx.tracer.dump(), "error": error,
    })


# ---------------------------------------------------------------------------
# 语义缓存（构建在网关层）：命中查找 / 成功回写 / 合成 OpenAI 兼容响应
# ---------------------------------------------------------------------------

def _cache_min_answer() -> int:
    return int(getattr(app_settings, "SEMANTIC_CACHE_MIN_ANSWER_CHARS", 30))


async def _cache_lookup(ctx: Ctx) -> Optional[str]:
    """出网前查缓存；命中返回缓存回答文本并记入链路，未命中/异常返回 None。"""
    if not ctx.cache_enabled:
        return None
    try:
        hit = await semantic_cache.lookup(
            ctx.user_id, ctx.cache_query, scope=ctx.cache_scope, is_realtime=ctx.cache_realtime)
    except Exception as e:
        logger.warning("[网关语义缓存] 查询失败，走上游: %s", e)
        return None
    if hit and (hit.get("answer") or "").strip():
        ctx.tracer.add("cache", "语义缓存", "ok",
                       f"命中（scope={hit.get('scope')} 相似度={hit.get('score')} "
                       f"ttl={hit.get('ttl_label')}），跳过上游 LLM")
        return hit.get("answer") or ""
    ctx.tracer.add("cache", "语义缓存", "ok",
                   f"未命中（scope={ctx.cache_scope} realtime={ctx.cache_realtime}），调用上游")
    return None


async def _cache_store(ctx: Ctx, text: str) -> None:
    """上游成功产出正文后回写缓存（失败静默降级，不阻塞响应）。"""
    if not ctx.cache_enabled:
        return
    ans = (text or "").strip()
    if len(ans) < _cache_min_answer():
        return
    try:
        await semantic_cache.store(
            ctx.user_id, ctx.cache_query, ans, None,
            scope=ctx.cache_scope, is_realtime=ctx.cache_realtime)
    except Exception as e:
        logger.warning("[网关语义缓存] 回写失败: %s", e)


def _synthetic_completion(ctx: Ctx, text: str) -> dict:
    """由缓存文本合成 OpenAI 兼容的非流式 chat.completion 响应。"""
    return {
        "id": f"chatcmpl-cache-{ctx.log_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": ctx.model_req,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "_gw_cache": "hit",
    }


def _synthetic_stream(ctx: Ctx, text: str) -> List[str]:
    """由缓存文本合成 OpenAI 兼容的流式 SSE 块序列（含 [DONE]）。"""
    cid = f"chatcmpl-cache-{ctx.log_id}"
    created = int(time.time())

    def _chunk(delta: dict, finish: Optional[str]) -> str:
        obj = {
            "id": cid, "object": "chat.completion.chunk", "created": created,
            "model": ctx.model_req,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

    return [
        _chunk({"role": "assistant", "content": text}, None),
        _chunk({}, "stop"),
        "data: [DONE]\n\n",
    ]


# ---------------------------------------------------------------------------
# 非流式响应
# ---------------------------------------------------------------------------

async def json_response(ctx: Ctx) -> tuple[int, dict]:
    try:
        async with asyncio.timeout(remaining(ctx.deadline)):
            return await _json_response(ctx)
    except (TimeoutError, httpx.TimeoutException) as e:
        _finalize(ctx, status="timeout", status_code=504, error="请求总预算耗尽")
        raise GatewayError(504, "请求总预算耗尽", "request_timeout") from e
    except asyncio.CancelledError:
        _finalize(ctx, status="cancelled", status_code=499, error="客户端取消")
        raise


async def _json_response(ctx: Ctx) -> tuple[int, dict]:
    # 语义缓存命中：直接合成响应返回，不调用任何上游
    cached = await _cache_lookup(ctx)
    if cached is not None:
        ctx.cache_hit = True
        _finalize(ctx, status="ok", status_code=200, response_preview=cached)
        return 200, _synthetic_completion(ctx, cached)
    last_err = ""
    for idx, up in enumerate(ctx.candidates[:_max_attempts(ctx)]):
        ctx.retries = idx
        # 上游自身持有出网配置；请求模型名原样出网（无模型映射）
        eff = up_svc.effective(up)
        url = up_svc.build_upstream_url(eff)
        mapped = ctx.model_req
        ctx.upstream, ctx.model_mapped, ctx.upstream_url = up, mapped, url
        t0 = time.monotonic()
        try:
            headers, payload = upstream.build_request(eff, ctx.forward_body, mapped, stream=False)
            _suppress_utility_thinking(ctx, eff, payload)
            async with httpx.AsyncClient(timeout=_network_timeout(ctx)) as client:
                resp = await client.post(url, headers=headers, json=payload)
            sc = resp.status_code
            # 需要故障切换的状态：连接类/鉴权/限流/5xx；其余 4xx 直接返回不重试
            if sc in _FAILOVER_STATUS or sc >= 500:
                raise RuntimeError(f"上游返回 {sc}: {resp.text[:200]}")
            try:
                raw = resp.json()
            except Exception:
                raise RuntimeError(f"上游响应非 JSON: {resp.text[:200]}")
            normalized = upstream.normalize_response(eff, sc, raw)
            up_svc.mark_success(up["id"], int((time.monotonic() - t0) * 1000))
            ctx.tracer.add("upstream", f"调用上游 {up['name']}", "ok",
                           f"{eff.get('protocol')} → {mapped}，HTTP {sc}")
            logger.info("[网关出网] 路由规则「%s」→ 模型「%s」 via 上游「%s」 HTTP %s %dms",
                        (ctx.rule or {}).get("model_pattern", ""), mapped, up["name"], sc,
                        int((time.monotonic() - t0) * 1000))
            usage = normalized.get("usage") or {}
            pt, ct = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
            preview, tool_calls = _extract_preview(normalized)
            # 成功产出正文且非工具调用 → 回写语义缓存（按 public/personal + 实时阶梯 TTL）
            if sc < 400 and not tool_calls:
                await _cache_store(ctx, preview)
            ctx.tracer.add("response", "响应下游", "ok",
                           f"tokens={pt}+{ct}，预览 {len(preview)} 字")
            ctx.tracer.add("log", "日志入库", "ok", "已留存调用记录与链路")
            _finalize(ctx, status="ok" if sc < 400 else "error", status_code=sc,
                      prompt_tokens=pt, completion_tokens=ct,
                      response_preview=preview, tool_calls=tool_calls)
            return sc, normalized
        except Exception as e:
            last_err = str(e)
            up_svc.mark_failure(up["id"], last_err)
            ctx.tracer.add("upstream", f"调用上游 {up['name']}", "error", last_err)
            logger.warning("网关上游失败，切换下一个: upstream=%s err=%s", up.get("name"), last_err)
            continue
    # 所有候选均失败
    ctx.tracer.add("response", "响应下游", "error", f"全部上游失败: {last_err}")
    ctx.tracer.add("log", "日志入库", "ok", "已留存失败链路")
    _finalize(ctx, status="error", status_code=502, error=f"所有上游均调用失败: {last_err}",
              response_preview="")
    raise GatewayError(502, f"所有上游均调用失败: {last_err}", "all_upstreams_failed")


def _extract_preview(normalized: dict) -> tuple[str, list]:
    """从 OpenAI 形态响应提取文本预览与工具调用。"""
    try:
        msg = (normalized.get("choices") or [{}])[0].get("message") or {}
        content = msg.get("content") or ""
        tool_calls = msg.get("tool_calls") or []
        return (content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)), tool_calls
    except Exception:
        return "", []


# ---------------------------------------------------------------------------
# 非流式响应（embeddings / rerank）：与 chat 通道隔离，复用 failover/健康/计量/日志
# ---------------------------------------------------------------------------

async def json_response_kind(ctx: Ctx, kind: str) -> tuple[int, dict]:
    try:
        async with asyncio.timeout(remaining(ctx.deadline)):
            return await _json_response_kind(ctx, kind)
    except (TimeoutError, httpx.TimeoutException) as e:
        _finalize(ctx, status="timeout", status_code=504, error="请求总预算耗尽")
        raise GatewayError(504, "请求总预算耗尽", "request_timeout") from e
    except asyncio.CancelledError:
        _finalize(ctx, status="cancelled", status_code=499, error="客户端取消")
        raise


async def _json_response_kind(ctx: Ctx, kind: str) -> tuple[int, dict]:
    last_err = ""
    for idx, up in enumerate(ctx.candidates[:_max_attempts(ctx)]):
        ctx.retries = idx
        eff = up_svc.effective(up)
        url = up_svc.build_upstream_url(eff, kind)
        mapped = ctx.model_req
        ctx.upstream, ctx.model_mapped, ctx.upstream_url = up, mapped, url
        t0 = time.monotonic()
        try:
            headers, payload = upstream.build_request(eff, ctx.forward_body, mapped,
                                                      stream=False, kind=kind)
            async with httpx.AsyncClient(timeout=_network_timeout(ctx)) as client:
                resp = await client.post(url, headers=headers, json=payload)
            sc = resp.status_code
            if sc in _FAILOVER_STATUS or sc >= 500:
                raise RuntimeError(f"上游返回 {sc}: {resp.text[:200]}")
            try:
                raw = resp.json()
            except Exception:
                raise RuntimeError(f"上游响应非 JSON: {resp.text[:200]}")
            normalized = upstream.normalize_response(eff, sc, raw, kind)
            up_svc.mark_success(up["id"], int((time.monotonic() - t0) * 1000))
            ctx.tracer.add("upstream", f"调用上游 {up['name']}", "ok",
                           f"{eff.get('protocol')} → {mapped}（{kind}），HTTP {sc}")
            logger.info("[网关出网] 路由规则「%s」→ 模型「%s」 via 上游「%s」 %s HTTP %s %dms",
                        (ctx.rule or {}).get("model_pattern", ""), mapped, up["name"], kind, sc,
                        int((time.monotonic() - t0) * 1000))
            pt, ct, preview = _meter_kind(ctx, kind, normalized)
            ctx.tracer.add("response", "响应下游", "ok", f"tokens={pt}+{ct}，{preview[:40]}")
            ctx.tracer.add("log", "日志入库", "ok", "已留存调用记录与链路")
            _finalize(ctx, status="ok" if sc < 400 else "error", status_code=sc,
                      prompt_tokens=pt, completion_tokens=ct, response_preview=preview)
            return sc, normalized
        except Exception as e:
            last_err = str(e)
            up_svc.mark_failure(up["id"], last_err)
            ctx.tracer.add("upstream", f"调用上游 {up['name']}", "error", last_err)
            logger.warning("网关上游失败（%s），切换下一个: upstream=%s err=%s",
                           kind, up.get("name"), last_err)
            continue
    ctx.tracer.add("response", "响应下游", "error", f"全部上游失败: {last_err}")
    ctx.tracer.add("log", "日志入库", "ok", "已留存失败链路")
    _finalize(ctx, status="error", status_code=502, error=f"所有上游均调用失败: {last_err}",
              response_preview="")
    raise GatewayError(502, f"所有上游均调用失败: {last_err}", "all_upstreams_failed")


def _meter_kind(ctx: Ctx, kind: str, normalized: dict) -> tuple[int, int, str]:
    """embeddings/rerank 计量：返回 (prompt_tokens, completion_tokens, 预览)。

    embeddings 用 usage.prompt_tokens/total_tokens；rerank 无 token 则按 query+documents 估算。
    """
    usage = normalized.get("usage") or {}
    pt = int(usage.get("prompt_tokens") or 0)
    total = int(usage.get("total_tokens") or 0)
    body = ctx.forward_body or {}
    if kind == "embeddings":
        data = normalized.get("data") or []
        dim = 0
        if data and isinstance(data[0].get("embedding"), list):
            dim = len(data[0]["embedding"])
        preview = f"embedding × {len(data)}" + (f"（{dim} 维）" if dim else "")
        if not (pt or total):
            texts = body.get("input")
            if isinstance(texts, str):
                texts = [texts]
            pt = sum(agent_obs.estimate_tokens(str(t)) for t in (texts or []))
        return pt, 0, preview
    if kind == "rerank":
        results = normalized.get("results") or []
        preview = f"rerank × {len(results)}"
        if not (pt or total):
            q = str(body.get("query") or "")
            docs = body.get("documents") or []
            pt = agent_obs.estimate_tokens(q) + sum(
                agent_obs.estimate_tokens(str(d)) for d in docs)
        return pt, 0, preview
    preview, _ = _extract_preview(normalized)
    return pt, int(usage.get("completion_tokens") or 0), preview


# ---------------------------------------------------------------------------
# 流式响应
# ---------------------------------------------------------------------------

def _network_timeout(ctx):
    budget = remaining(ctx.deadline)
    return httpx.Timeout(budget, connect=min(10, budget), pool=min(10, budget))


async def stream_response(ctx: Ctx) -> AsyncGenerator[str, None]:
    """收到协议终止信号才成功；产出首块后不切换，所有重试共用预算。"""
    # 语义缓存命中：合成流式响应直接下发，不调用任何上游
    cached = await _cache_lookup(ctx)
    if cached is not None:
        ctx.cache_hit = True
        for sse in _synthetic_stream(ctx, cached):
            yield sse
        ctx.tracer.add("response", "响应下游", "ok", "语义缓存命中，合成流式响应")
        _finalize(ctx, status="ok", status_code=200, response_preview=cached)
        return
    acc_text: List[str] = []
    usage = {"pt": 0, "ct": 0}
    tool_names: List[str] = []
    final_status, final_code, final_err = "error", 502, "所有上游均调用失败"
    committed = False
    try:
        for idx, up in enumerate(ctx.candidates[:_max_attempts(ctx)]):
            ctx.retries = idx
            eff = up_svc.effective(up)
            url = up_svc.build_upstream_url(eff)
            mapped = ctx.model_req
            ctx.upstream, ctx.model_mapped, ctx.upstream_url = up, mapped, url
            t0 = time.monotonic()
            client = resp = iterator = None
            try:
                headers, payload = upstream.build_request(eff, ctx.forward_body, mapped, stream=True)
                _suppress_utility_thinking(ctx, eff, payload)
                client = httpx.AsyncClient(timeout=_network_timeout(ctx))
                async with asyncio.timeout(remaining(ctx.deadline)):
                    req = client.build_request("POST", url, headers=headers, json=payload)
                    resp = await client.send(req, stream=True)
                    if resp.status_code >= 400:
                        body_txt = (await resp.aread()).decode("utf-8", "ignore")[:200]
                        raise RuntimeError(f"上游 {resp.status_code}: {body_txt}")
                ctx.tracer.add("upstream", f"调用上游 {up['name']}", "ok", f"{mapped}（流式）")
                iterator = iterate(upstream.stream_iterator(eff, resp.aiter_lines()), ctx.deadline)
                first_chunk_limit = _first_chunk_limit(ctx)
                while True:
                    try:
                        if committed:
                            sse = await iterator.__anext__()
                        else:
                            # 首块前仍可切换：上游在期限内不产出任何 chunk 视为慢上游失败
                            sse = await asyncio.wait_for(
                                iterator.__anext__(),
                                timeout=min(first_chunk_limit, max(0.5, remaining(ctx.deadline))))
                    except StopAsyncIteration:
                        break
                    except (TimeoutError, asyncio.TimeoutError):
                        if committed or remaining(ctx.deadline) <= 0.5:
                            raise TimeoutError("请求超时或总预算耗尽")
                        raise RuntimeError(
                            f"上游 {first_chunk_limit:.0f}s 内未产出首块，判定慢上游并切换")
                    _account_sse(sse, acc_text, usage, tool_names)
                    if any(line.strip() == "data: [DONE]" for line in sse.splitlines()):
                        final_status, final_code, final_err = "ok", resp.status_code, ""
                        up_svc.mark_success(up["id"], int((time.monotonic() - t0) * 1000))
                        logger.info("[网关出网] 路由规则「%s」→ 模型「%s」 via 上游「%s」 流式 %dms",
                                    (ctx.rule or {}).get("model_pattern", ""), mapped, up["name"],
                                    int((time.monotonic() - t0) * 1000))
                        # 成功结束且非工具调用 → 回写语义缓存
                        if not tool_names:
                            await _cache_store(ctx, "".join(acc_text))
                        yield sse
                        break
                    for line in sse.splitlines():
                        if line.startswith("data:"):
                            obj = json.loads(line[5:].strip())
                            if obj.get("error"):
                                raise RuntimeError(str(obj["error"]))
                    committed = True
                    yield sse
                if final_status != "ok":
                    raise RuntimeError("上游流缺少正常结束标志")
                break
            except (TimeoutError, httpx.TimeoutException):
                final_status, final_code, final_err = "timeout", 504, "请求超时或总预算耗尽"
                break
            except Exception as e:
                final_err = str(e)
                up_svc.mark_failure(up["id"], final_err)
                ctx.tracer.add("upstream", f"调用上游 {up['name']}", "error", final_err)
                if committed:
                    break
            finally:
                await close_resources(iterator, resp, client)
        if final_status != "ok":
            error = {"error": {"message": final_err, "type": final_status,
                               "code": "request_timeout" if final_status == "timeout" else "upstream_error"}}
            yield "data: " + json.dumps(error, ensure_ascii=False) + "\n\n"
    except (asyncio.CancelledError, GeneratorExit):
        if final_status != "ok":
            final_status, final_code, final_err = "cancelled", 499, "客户端取消或断开"
        raise
    finally:
        ctx.tracer.add("response", "响应下游", final_status, final_err)
        _finalize(ctx, status=final_status, status_code=final_code,
                  prompt_tokens=usage["pt"], completion_tokens=usage["ct"],
                  response_preview="".join(acc_text), error=final_err,
                  tool_calls=[{"function": {"name": n}} for n in tool_names])


def _account_sse(sse: str, acc_text: List[str], usage: dict, tool_names: List[str]) -> None:
    """从 OpenAI 形态 SSE 块累计内容/usage/工具名，用于最终计量。"""
    try:
        for line in sse.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                continue
            obj = json.loads(data)
            if obj.get("usage"):
                u = obj["usage"]
                usage["pt"] = int(u.get("prompt_tokens") or usage["pt"])
                usage["ct"] = int(u.get("completion_tokens") or usage["ct"])
            for choice in obj.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    acc_text.append(delta["content"])
                for tc in delta.get("tool_calls") or []:
                    name = (tc.get("function") or {}).get("name")
                    if name and name not in tool_names:
                        tool_names.append(name)
    except Exception:
        pass
