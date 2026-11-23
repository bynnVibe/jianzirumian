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
from app.services import cache_policy
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
# 预提交阶段单上游尝试上限：慢滴答（持续产出无正文 chunk）也不得吃满总预算，
# 超上限仍无有效产出判定慢上游并切换，为主备切换保留预算。
_PRE_COMMIT_CAP = 90.0
# 超时后仍发起切换尝试的最低剩余预算：低于此值不再试新上游，直接终态超时。
_FAILOVER_RESERVE = 3.0
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
        # 调用归属（签名上下文验签通过后信任；供网关日志归属字段使用）
        self.billing_user_id = ""
        self.trace_id = ""
        self.span_id = ""
        self.job_id = ""
        self.origin = ""
        self.attribution_source = ""
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
        # 监控埋点：未命中事件 id（上游成功后按真实 usage 补记 est_tokens）
        self.cache_event_id = ""
        self.cache_contract = {}
        self.cache_fingerprint = ""
        self.cache_info = {}
        self.cache_bypass_recorded = False


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
    # 签名调用上下文（LLMFactory.create_for_role 注入）：验签通过才信任归属；
    # 有签名但验签失败 → 不信任其声明的用户（防伪造串用他人配额），密钥鉴权照常
    from app.core import gw_context
    signed = gw_context.verify_payload(str(body.get("_gw_ctx") or ""),
                                       str(body.get("_gw_sig") or ""))
    if signed:
        ctx.user_id = str(signed.get("user_id") or "") or ctx.user_id
        ctx.billing_user_id = str(signed.get("billing_user_id") or "") or ctx.user_id
        ctx.trace_id = str(signed.get("trace_id") or "")
        ctx.span_id = str(signed.get("span_id") or "")
        ctx.job_id = str(signed.get("job_id") or "")
        ctx.origin = str(signed.get("origin") or "")
        ctx.attribution_source = "signed_context"
    elif body.get("_gw_sig"):
        ctx.user_id = ""
        ctx.attribution_source = "signature_invalid"
        ctx.tracer.add("auth", "调用上下文验签", "warn",
                       "签名无效或已过期，不信任本次调用声明的用户归属")
    elif ctx.user_id:
        # 旧链路无签名提示：兼容保留（不一刀切拒绝），归属来源显式标记
        ctx.attribution_source = "legacy_hint"
    # 语义缓存提示（由业务层在意图识别后经 extra_body 注入）：
    # _gw_cache_query=意图识别后的问题（缓存键）、_gw_cache_scope=public|personal、
    # _gw_cache_realtime=是否实时。仅对回答类角色且无工具调用时启用。
    ctx.cache_enabled = kind == "chat" and ctx.role in ("chat_answer", "assistant")
    ctx.cache_scope = "personal"
    base = {
        "id": ctx.log_id, "created_at": ctx.created_at, "client_ip": ctx.client_ip,
        "model_requested": ctx.model_req, "stream": ctx.stream,
        "request_body": json.dumps({k: v for k, v in body.items() if k != "_gw_cache_contract"}, ensure_ascii=False),
        "key_id": "", "key_name": "",
        # 业务场景（role）：审计页据此展示「该链路是哪个业务模块发起的」
        "role": ctx.role,
        # 调用归属（签名上下文验签产出）：随每条日志落库，供统计区分
        # 用户业务/真实系统/公共游客/历史未归属；usage_event_id 幂等防重复计量
        "user_id": ctx.user_id,
        "billing_user_id": ctx.billing_user_id or ctx.user_id,
        "trace_id": ctx.trace_id, "span_id": ctx.span_id,
        "job_id": ctx.job_id, "origin": ctx.origin,
        "attribution_source": ctx.attribution_source,
        "usage_event_id": ctx.log_id,
    }

    # 1. 密钥鉴权（绑定即强制不降级：配额用尽明确报错，不静默切换公共密钥）
    try:
        ctx.key_row = key_svc.authenticate(raw_key)
        base["key_id"] = ctx.key_row["id"]
        base["key_name"] = ctx.key_row.get("name") or ""
        ctx.tracer.add("auth", "密钥鉴权", "ok", f"密钥「{base['key_name']}」校验通过")
        # 归属兜底（可靠证据）：无签名上下文时，用户密钥的绑定用户即归属；
        # 内部/公共密钥无用户证据则显式标记来源，不再笼统归入「系统内部」
        key_type = str(ctx.key_row.get("key_type") or "")
        if not ctx.user_id and key_type == "user":
            try:
                bound = json.loads(ctx.key_row.get("bound_users") or "[]")
            except (json.JSONDecodeError, TypeError):
                bound = []
            bound = [str(u) for u in bound if str(u).strip()]
            if bound:
                ctx.user_id = bound[0]
                ctx.billing_user_id = ctx.user_id
                ctx.attribution_source = "key_binding"
        if not ctx.attribution_source:
            ctx.attribution_source = {"internal": "system_internal",
                                      "public": "public_shared"}.get(key_type, "")
        base.update({"user_id": ctx.user_id,
                     "billing_user_id": ctx.billing_user_id or ctx.user_id,
                     "attribution_source": ctx.attribution_source})
    except key_svc.KeyRejected as e:
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
        base["request_body"] = json.dumps(
            {k: v for k, v in scanned.items() if k != "_gw_cache_contract"}, ensure_ascii=False)
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
    # 4a. 整改3：主备升主——主路由失效且存在健康备时，备升主持久化，
    #     后续请求不再重试旧主；切换过程经 trace+日志+可观测告警显性化。
    if ctx.rule:
        try:
            promo = rule_svc.promote_failed_primary(ctx.rule)
        except Exception as e:
            logger.warning("主备升主检查失败（不阻断选路）: %s", e)
            promo = None
        if promo:
            old_name = (promo["old_primary"] or {}).get("name") or promo["old_primary"]["id"]
            new_name = (promo["new_primary"] or {}).get("name") or promo["new_primary"]["id"]
            ctx.tracer.add("route", "主备切换·备升主", "warn",
                           f"规则「{promo['rule_name']}」主路由「{old_name}」失效，"
                           f"备「{new_name}」已升主并持久化，后续请求不再重试旧主")
            logger.warning("[网关选路] 主备切换·备升主: role=%s rule=%s 「%s」→「%s」",
                           ctx.role or "-", promo["rule_id"], old_name, new_name)
            try:
                from app.services import alert_analysis
                alert_analysis.record_failover_promotion(
                    rule_id=promo["rule_id"], rule_name=promo["rule_name"],
                    old_primary=old_name, new_primary=new_name,
                    role=ctx.role or "", user_id=ctx.user_id or "")
            except Exception as e:
                logger.warning("备升主告警写入失败（不阻断选路）: %s", e)
            ctx.rule, ctx.candidates = rule_svc.select_upstreams(
                ctx.model_req, role=ctx.role or None,
                rule_id=ctx.rule_id or None, user_id=ctx.user_id or None)
    # 4b. 主备模型兜底：切换后原模型若仅主路由支持，候选会被模型过滤剩主路由，
    #     显式模型请求与调用方重试将永远命中熔断主路由；改写为备路由支持模型并重选。
    if ctx.rule:
        fb_model = rule_svc.primary_backup_fallback_model(
            ctx.rule, ctx.model_req,
            category=rule_svc.ROLE_CATEGORY.get(ctx.role, "llm"))
        if fb_model and fb_model != ctx.model_req \
                and key_svc.check_model_allowed(ctx.key_row, fb_model):
            old_model = ctx.model_req
            ctx.model_req = fb_model
            ctx.forward_body["model"] = fb_model
            ctx.rule, ctx.candidates = rule_svc.select_upstreams(
                fb_model, role=ctx.role or None,
                rule_id=ctx.rule_id or None, user_id=ctx.user_id or None)
            ctx.tracer.add("route", "主备模型兜底", "warn",
                           f"主路由不可用且模型「{old_model}」仅主路由支持，"
                           f"回退备路由支持模型「{fb_model}」并重新选路")
            logger.warning("[网关选路] 主备模型兜底: role=%s 模型「%s」→「%s」",
                           ctx.role or "-", old_model, fb_model)
    messages = body.get("messages")
    needs_tools = bool(body.get("tools")) or any(
        isinstance(m, dict) and (m.get("role") == "tool" or m.get("tool_calls"))
        for m in (messages if isinstance(messages, list) else []))
    if needs_tools and ctx.candidates:
        ctx.cache_enabled = False
        ctx.candidates = [up for up in ctx.candidates
                          if up_svc.supports_tools(up, ctx.model_req, body.get("tool_choice", "auto"))]
        if not ctx.candidates:
            msg = "当前路由没有声明支持原生工具调用的模型／上游，请在上游管理中配置原生工具模型"
            ctx.tracer.add("route", "工具能力校验", "error", msg)
            log_svc.write_log({**base, "status": "error", "status_code": 503,
                               "latency_ms": _latency(ctx), "trace_steps": ctx.tracer.dump(), "error": msg})
            raise GatewayError(503, msg, "tool_model_unavailable")
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


def _filter_circuit(ctx: Ctx) -> tuple[List[dict], List[dict]]:
    """候选上游熔断过滤：返回 (可调用候选, 熔断冷却中的上游)。

    熔断开启且冷却期未过的上游不再锤击；冷却已过的允许一次半开探测。
    """
    allowed: List[dict] = []
    opened: List[dict] = []
    for up in ctx.candidates[:_max_attempts(ctx)]:
        if (up.get("health") or "") in up_svc.OPEN_HEALTH_STATES \
                and not up_svc.circuit_probe_allowed(up):
            opened.append(up)
        else:
            allowed.append(up)
    return allowed, opened


def _circuit_message(opened: List[dict]) -> str:
    names = "、".join(str(u.get("name") or u.get("id")) for u in opened)
    return (f"上游模型已熔断：{names} 连续失败超过 {up_svc._DOWN_THRESHOLD} 次，已暂停调用，"
            f"请 {up_svc._CIRCUIT_COOLDOWN_SECONDS} 秒后重试或联系管理员检查上游配置")


def _circuit_error(ctx: Ctx, opened: List[dict]) -> None:
    """全部候选熔断冷却中：fail-fast 抛 503，用户可见熔断原因。"""
    msg = _circuit_message(opened)
    ctx.tracer.add("response", "响应下游", "error", msg)
    ctx.tracer.add("log", "日志入库", "ok", "已留存熔断失败链路")
    _finalize(ctx, status="error", status_code=503, error=msg, response_preview="")
    raise GatewayError(503, msg, "upstream_circuit_open")


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


def _usage_cached(usage: dict) -> int:
    """厂商反馈的前缀缓存命中 Token：OpenAI 形态 prompt_tokens_details.cached_tokens，
    DeepSeek 形态 prompt_cache_hit_tokens；一律以厂商 usage 为准，缺失记 0。"""
    det = usage.get("prompt_tokens_details") or {}
    cached = int(det.get("cached_tokens") or 0)
    if not cached:
        cached = int(usage.get("prompt_cache_hit_tokens") or 0)
    return max(0, cached)


def _finalize(ctx: Ctx, *, status: str, status_code: int, prompt_tokens: int = 0,
              completion_tokens: int = 0, cached_tokens: int = 0, response_preview: str = "",
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
        "cached_tokens": int(cached_tokens or 0),
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
    """出网前查缓存；命中返回缓存回答文本并记入链路，未命中/异常返回 None。

    监控埋点：命中/未命中各记一条 semantic_cache_events（失败静默）；
    miss 事件 id 存 ctx.cache_event_id，上游成功后按真实 usage 补记。
    """
    if not ctx.cache_enabled:
        return None
    t0 = time.monotonic()
    try:
        contract, reason = cache_policy.verify_contract(ctx.forward_body, ctx.user_id)
        if not semantic_cache.semantic_cache_enabled():
            contract, reason = {}, "disabled"
        ctx.cache_contract = contract
        if not contract:
            if not ctx.cache_bypass_recorded:
                semantic_cache.record_event(user_id=ctx.user_id, hit=False, outcome="bypass", reason=reason)
                ctx.cache_bypass_recorded = True
            ctx.cache_info = {"hit": False, "reason": reason, "policy_version": 2}
            return None
        ctx.cache_query = contract["query"]
        ctx.cache_fingerprint = cache_policy.fingerprint(
            contract, model=ctx.model_req, upstream=up_svc.effective(ctx.upstream),
            rule=ctx.rule or {}, body=ctx.forward_body)
        async with asyncio.timeout(min(8, remaining(ctx.deadline))):
            hit = await semantic_cache.lookup(
                ctx.user_id, ctx.cache_query, contract=contract, fingerprint=ctx.cache_fingerprint)
    except Exception as e:
        ctx.cache_contract = {}
        logger.warning("[网关语义缓存] 查询失败，走上游: %s", e)
        return None
    latency_ms = int((time.monotonic() - t0) * 1000)
    if hit and (hit.get("answer") or "").strip():
        answer = hit.get("answer") or ""
        # 命中即省去上游消耗：估算缓存答案+问题的 Token 作为节省量
        est = agent_obs.estimate_tokens(answer) + agent_obs.estimate_tokens(ctx.cache_query)
        if ctx.cache_event_id:
            db.execute("UPDATE semantic_cache_events SET hit=1,outcome='hit',entry_id=?,score=?,"
                       "saved_tokens=?,latency_ms=? WHERE id=?",
                       (hit["id"], hit.get("score", 1), est, latency_ms, ctx.cache_event_id))
        else:
            semantic_cache.record_event(
                user_id=ctx.user_id, hit=True, entry_id=hit["id"], score=hit.get("score"),
                latency_ms=latency_ms, est_tokens=est, saved_tokens=est, question=ctx.cache_query)
        ctx.cache_info = {k: hit.get(k) for k in ("score", "cache_kind", "created_at", "expires_at", "evidence_hash", "model")}
        ctx.cache_info.update({"hit": True, "policy_version": 2})
        ctx.tracer.add("cache", "语义缓存", "ok",
                       f"命中（scope={hit.get('scope')} 相似度={hit.get('score')} "
                       f"ttl={hit.get('ttl_label')}），跳过上游 LLM")
        return answer
    if not ctx.cache_event_id:
        ctx.cache_event_id = semantic_cache.record_event(
            user_id=ctx.user_id, scope=ctx.cache_scope, hit=False,
            latency_ms=latency_ms, question=ctx.cache_query) or ""
    ctx.tracer.add("cache", "语义缓存", "ok",
                   f"未命中（scope={ctx.cache_scope} realtime={ctx.cache_realtime}），调用上游")
    return None


async def _cache_store(ctx: Ctx, text: str) -> None:
    """上游成功产出正文后回写缓存（失败静默降级，不阻塞响应）。"""
    if not ctx.cache_enabled or not ctx.cache_contract:
        return
    ans = (text or "").strip()
    if len(ans) < _cache_min_answer():
        return
    try:
        await semantic_cache.store(
            ctx.user_id, ctx.cache_query, ans, contract=ctx.cache_contract,
            fingerprint=ctx.cache_fingerprint, upstream_id=(ctx.upstream or {}).get("id", ""))
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
        "_gw_cache": "hit", "_gw_cache_info": ctx.cache_info,
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
            "_gw_cache_info": ctx.cache_info,
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
    last_err = ""
    candidates, opened = _filter_circuit(ctx)
    if not candidates and opened:
        _circuit_error(ctx, opened)
    for idx, up in enumerate(candidates):
        ctx.retries = idx
        # 上游自身持有出网配置；请求模型名原样出网（无模型映射）
        eff = up_svc.effective(up)
        url = up_svc.build_upstream_url(eff)
        mapped = ctx.model_req
        ctx.upstream, ctx.model_mapped, ctx.upstream_url = up, mapped, url
        t0 = time.monotonic()
        try:
            cached_answer = await _cache_lookup(ctx)
            if cached_answer is not None:
                ctx.cache_hit = True
                _finalize(ctx, status="ok", status_code=200, response_preview=cached_answer)
                return 200, _synthetic_completion(ctx, cached_answer)
            headers, payload = upstream.build_request(eff, ctx.forward_body, mapped, stream=False)
            _suppress_utility_thinking(ctx, eff, payload)
            async with httpx.AsyncClient(timeout=_network_timeout(ctx, _json_attempt_cap(ctx, candidates))) as client:
                resp = await asyncio.wait_for(client.post(url, headers=headers, json=payload),
                                              timeout=_json_attempt_timeout(ctx, candidates))
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
            # 监控埋点补记：miss 事件按本次真实 usage 补记 Token（失败静默）
            if ctx.cache_event_id and (pt or ct):
                semantic_cache.update_event_tokens(ctx.cache_event_id, pt + ct)
            cached = _usage_cached(usage)
            preview, tool_calls = _extract_preview(normalized)
            # 成功产出正文且非工具调用 → 回写语义缓存（按 public/personal + 实时阶梯 TTL）
            if sc < 400 and not tool_calls and (normalized.get("choices") or [{}])[0].get("finish_reason") == "stop":
                await _cache_store(ctx, preview)
            ctx.tracer.add("response", "响应下游", "ok",
                           f"tokens={pt}+{ct}，预览 {len(preview)} 字")
            ctx.tracer.add("log", "日志入库", "ok", "已留存调用记录与链路")
            _finalize(ctx, status="ok" if sc < 400 else "error", status_code=sc,
                      prompt_tokens=pt, completion_tokens=ct, cached_tokens=cached,
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
    candidates, opened = _filter_circuit(ctx)
    if not candidates and opened:
        _circuit_error(ctx, opened)
    for idx, up in enumerate(candidates):
        ctx.retries = idx
        eff = up_svc.effective(up)
        url = up_svc.build_upstream_url(eff, kind)
        mapped = ctx.model_req
        ctx.upstream, ctx.model_mapped, ctx.upstream_url = up, mapped, url
        t0 = time.monotonic()
        try:
            headers, payload = upstream.build_request(eff, ctx.forward_body, mapped,
                                                      stream=False, kind=kind)
            async with httpx.AsyncClient(timeout=_network_timeout(ctx, _json_attempt_cap(ctx, candidates))) as client:
                resp = await asyncio.wait_for(client.post(url, headers=headers, json=payload),
                                              timeout=_json_attempt_timeout(ctx, candidates))
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

def _budget_left(ctx) -> float:
    """剩余预算（不抛错）：失败分支据此判断是否还能切换，预算耗尽时返回 0。

    request_budget.remaining 在耗尽时抛 TimeoutError，不能在 except 分支里调用，
    否则失败处理自身被打断（既不记失败也不下发终态错误）。
    """
    return max(0.0, ctx.deadline - time.monotonic())


def _network_timeout(ctx, cap: float = 0.0):
    budget = _budget_left(ctx)
    if cap > 0:
        budget = min(budget, cap)
    return httpx.Timeout(budget, connect=min(10, budget), pool=min(10, budget))


def _json_attempt_cap(ctx, candidates) -> float:
    """非流式单尝试预算：多候选时按候选数均分总预算，保证超时后仍有预算切换；
    单候选保持全预算（长生成不被误截）。"""
    if len(candidates) <= 1:
        return 0.0
    return max(_FIRST_CHUNK_DEFAULT, _budget_left(ctx) / len(candidates))


def _json_attempt_timeout(ctx, candidates) -> float:
    """非流式单尝试硬期限：不依赖传输层超时实现，保证慢上游可被判定并切换。"""
    cap = _json_attempt_cap(ctx, candidates)
    return cap if cap > 0 else max(0.5, _budget_left(ctx))


def _extend_backup_candidates(ctx: Ctx, queue: List[dict], tried_ids: List[str]) -> None:
    """主备规则请求内切换：候选被模型过滤剩主路由时，主失败后按备支持模型重选补入队列。

    队列仍有未试候选时不改写（备支持原模型的常规主备路径）。
    """
    rule = ctx.rule
    if not rule or (rule.get("lb_strategy") or "") != "primary_backup":
        return
    if any(u["id"] not in tried_ids for u in queue):
        return
    fb_model = rule_svc.request_fallback_model(
        rule, ctx.model_req, tried_ids,
        category=rule_svc.ROLE_CATEGORY.get(ctx.role, "llm"))
    if not fb_model or fb_model == ctx.model_req \
            or not key_svc.check_model_allowed(ctx.key_row, fb_model):
        return
    old_model = ctx.model_req
    ctx.model_req = fb_model
    ctx.forward_body["model"] = fb_model
    _, new_cands = rule_svc.select_upstreams(
        fb_model, role=ctx.role or None, rule_id=rule["id"], user_id=ctx.user_id or None)
    added = [u for u in new_cands if u["id"] not in tried_ids]
    if not added:
        return
    queue.extend(added)
    ctx.tracer.add("route", "主备切换·请求内兜底", "warn",
                   f"主上游失败，模型「{old_model}」→「{fb_model}」，补入备候选 {len(added)} 个")
    logger.warning("[网关选路] 主备切换·请求内兜底: role=%s 模型「%s」→「%s」 补入 %d 个备候选",
                   ctx.role or "-", old_model, fb_model, len(added))


async def stream_response(ctx: Ctx) -> AsyncGenerator[str, None]:
    """收到协议终止信号才成功；产出有效正文/工具调用后不切换，所有重试共用预算。

    未提交（尚无有效正文）前超时/慢上游同样走 failover：记失败健康度
    （驱动跨请求备升主），预算有余即切换下一候选；主备规则候选被模型
    过滤剩主路由时，主失败后按备支持模型重选补入队列（请求内兜底）。
    """
    acc_text: List[str] = []
    usage = {"pt": 0, "ct": 0, "cached": 0, "finish_reason": ""}
    tool_names: List[str] = []
    final_status, final_code, final_err = "error", 502, "所有上游均调用失败"
    committed = False
    candidates, opened = _filter_circuit(ctx)
    if not candidates and opened:
        # 全部候选熔断冷却中：不再锤击上游，直接下发用户可见的熔断错误
        final_status, final_code, final_err = "error", 503, _circuit_message(opened)
        ctx.tracer.add("upstream", "熔断检查", "error", final_err)
    try:
        queue = list(candidates)
        tried: List[str] = []
        idx = 0
        while idx < len(queue) and len(tried) < _max_attempts(ctx):
            up = queue[idx]
            idx += 1
            tried.append(up["id"])
            ctx.retries = len(tried) - 1
            eff = up_svc.effective(up)
            url = up_svc.build_upstream_url(eff)
            mapped = ctx.model_req
            ctx.upstream, ctx.model_mapped, ctx.upstream_url = up, mapped, url
            t0 = time.monotonic()
            client = resp = iterator = None
            try:
                cached_answer = await _cache_lookup(ctx)
                if cached_answer is not None:
                    ctx.cache_hit = True
                    acc_text.append(cached_answer)
                    final_status, final_code, final_err = "ok", 200, ""
                    for sse in _synthetic_stream(ctx, cached_answer):
                        yield sse
                    return
                headers, payload = upstream.build_request(eff, ctx.forward_body, mapped, stream=True)
                _suppress_utility_thinking(ctx, eff, payload)
                client = httpx.AsyncClient(timeout=_network_timeout(ctx))
                first_chunk_limit = _first_chunk_limit(ctx)
                # 响应头阶段同样受首块保护：上游挂起不返回响应头不得吃满总预算
                # （旧版在此等满预算后直接终态超时，不记失败也不切换）
                req = client.build_request("POST", url, headers=headers, json=payload)
                try:
                    resp = await asyncio.wait_for(
                        client.send(req, stream=True),
                        timeout=min(first_chunk_limit, max(0.5, _budget_left(ctx))))
                except (TimeoutError, asyncio.TimeoutError):
                    raise RuntimeError(
                        f"上游 {first_chunk_limit:.0f}s 内未返回响应头，判定慢上游并切换")
                if resp.status_code >= 400:
                    body_txt = (await resp.aread()).decode("utf-8", "ignore")[:200]
                    raise RuntimeError(f"上游 {resp.status_code}: {body_txt}")
                ctx.tracer.add("upstream", f"调用上游 {up['name']}", "ok", f"{mapped}（流式）")
                iterator = iterate(upstream.stream_iterator(eff, resp.aiter_lines()), ctx.deadline)
                while True:
                    try:
                        if committed:
                            sse = await iterator.__anext__()
                        else:
                            # 提交前仍可切换：块间静默保护 + 预提交尝试上限，
                            # 慢滴答上游（只产无正文 chunk）超上限也判慢上游切换
                            cap_left = _PRE_COMMIT_CAP - (time.monotonic() - t0)
                            if cap_left <= 0:
                                raise RuntimeError(
                                    f"上游 {_PRE_COMMIT_CAP:.0f}s 仍无有效产出，判定慢上游并切换")
                            sse = await asyncio.wait_for(
                                iterator.__anext__(),
                                timeout=min(first_chunk_limit, cap_left,
                                            max(0.5, _budget_left(ctx))))
                    except StopAsyncIteration:
                        break
                    except (TimeoutError, asyncio.TimeoutError):
                        if committed or _budget_left(ctx) <= 0.5:
                            raise TimeoutError("请求超时或总预算耗尽")
                        raise RuntimeError(
                            f"上游 {first_chunk_limit:.0f}s 内未产出首块，判定慢上游并切换")
                    had_payload = _account_sse(sse, acc_text, usage, tool_names)
                    if any(line.strip() == "data: [DONE]" for line in sse.splitlines()):
                        final_status, final_code, final_err = "ok", resp.status_code, ""
                        up_svc.mark_success(up["id"], int((time.monotonic() - t0) * 1000))
                        logger.info("[网关出网] 路由规则「%s」→ 模型「%s」 via 上游「%s」 流式 %dms",
                                    (ctx.rule or {}).get("model_pattern", ""), mapped, up["name"],
                                    int((time.monotonic() - t0) * 1000))
                        # 成功结束且非工具调用 → 回写语义缓存
                        if not tool_names and usage["finish_reason"] == "stop":
                            await _cache_store(ctx, "".join(acc_text))
                        yield sse
                        break
                    for line in sse.splitlines():
                        if line.startswith("data:"):
                            obj = json.loads(line[5:].strip())
                            if obj.get("error"):
                                raise RuntimeError(str(obj["error"]))
                    # 仅当 chunk 携带有效正文/工具调用才提交：role-only 等空 chunk
                    # 不得提前锁定切换（旧版首块即提交，后续挂起只能等满超时）
                    committed = committed or had_payload
                    yield sse
                if final_status != "ok":
                    raise RuntimeError("上游流缺少正常结束标志")
                break
            except (TimeoutError, httpx.TimeoutException):
                final_status, final_code = "timeout", 504
                final_err = "请求超时或总预算耗尽"
                up_svc.mark_failure(up["id"], final_err)
                ctx.tracer.add("upstream", f"调用上游 {up['name']}", "error", final_err)
                logger.warning("网关上游超时: upstream=%s committed=%s 剩余预算=%.1fs",
                               up.get("name"), committed, _budget_left(ctx))
                if committed or _budget_left(ctx) <= _FAILOVER_RESERVE:
                    break
                # 未提交且预算有余：超时同样视为上游失败，切换下一候选（含主备兜底）
                _extend_backup_candidates(ctx, queue, tried)
            except Exception as e:
                final_err = str(e)
                final_status, final_code = "error", 502
                up_svc.mark_failure(up["id"], final_err)
                ctx.tracer.add("upstream", f"调用上游 {up['name']}", "error", final_err)
                if committed:
                    break
                _extend_backup_candidates(ctx, queue, tried)
            finally:
                await close_resources(iterator, resp, client)
        if final_status != "ok":
            err_code = ("request_timeout" if final_status == "timeout"
                        else "upstream_circuit_open" if final_code == 503
                        else "upstream_error")
            error = {"error": {"message": final_err, "type": final_status, "code": err_code}}
            yield "data: " + json.dumps(error, ensure_ascii=False) + "\n\n"
    except (asyncio.CancelledError, GeneratorExit):
        if final_status != "ok":
            final_status, final_code, final_err = "cancelled", 499, "客户端取消或断开"
        raise
    finally:
        ctx.tracer.add("response", "响应下游", final_status, final_err)
        _finalize(ctx, status=final_status, status_code=final_code,
                  prompt_tokens=usage["pt"], completion_tokens=usage["ct"],
                  cached_tokens=usage["cached"],
                  response_preview="".join(acc_text), error=final_err,
                  tool_calls=[{"function": {"name": n}} for n in tool_names])
        # 监控埋点补记：miss 事件按本次真实 usage 补记 Token（失败静默）
        if final_status == "ok" and ctx.cache_event_id and (usage["pt"] or usage["ct"]):
            semantic_cache.update_event_tokens(ctx.cache_event_id, usage["pt"] + usage["ct"])


def _account_sse(sse: str, acc_text: List[str], usage: dict, tool_names: List[str]) -> bool:
    """从 OpenAI 形态 SSE 块累计内容/usage/工具名，用于最终计量。

    返回本块是否携带有效产出（正文/工具调用）：供流式提交判定，
    role-only/usage-only 空块不锁定 failover。
    """
    had_payload = False
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
                # 前缀缓存命中量（OpenAI 形态 prompt_tokens_details.cached_tokens / DeepSeek prompt_cache_hit_tokens）
                cached = _usage_cached(u)
                if cached:
                    usage["cached"] = cached
            for choice in obj.get("choices") or []:
                if choice.get("finish_reason"):
                    usage["finish_reason"] = choice["finish_reason"]
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    had_payload = True
                    acc_text.append(delta["content"])
                for tc in delta.get("tool_calls") or []:
                    had_payload = True
                    name = (tc.get("function") or {}).get("name")
                    if name and name not in tool_names:
                        tool_names.append(name)
    except Exception:
        pass
    return had_payload
