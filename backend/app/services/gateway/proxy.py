"""
见字如面 - 模型网关代理主流程（OpenAI 兼容）

一次调用的完整链路（每步都记入可视化 trace_steps，便于问题排查与运维）：
    密钥鉴权 → 模型授权/配额扣减 → 安全审计（脱敏/阻断）→ 渠道选路（优先级+权重）
    → 上游调用（失败自动切换其他渠道）→ Token 计量 → 日志入库 → 响应下游

对外契约统一为 OpenAI 兼容：POST /v1/chat/completions（支持 stream）。
所有终端性失败（鉴权失败/被阻断/无可用渠道）在 prepare 阶段写日志并抛 GatewayError，
由 API 层转换为对应 HTTP 状态码；上游失败在 failover 后仍无渠道可用时抛 502。
"""
import json
import logging
import time
import uuid
from datetime import datetime
from typing import AsyncGenerator, List, Optional

import httpx

from app.core import observability as agent_obs
from app.services.gateway import channels as ch_svc
from app.services.gateway import keys as key_svc
from app.services.gateway import logs as log_svc
from app.services.gateway import security
from app.services.gateway import settings as settings_svc
from app.services.gateway import upstream

logger = logging.getLogger("jianziruyang.gateway.proxy")

# 上游超时：连接短、读放宽（流式生成可能较久）
_TIMEOUT = httpx.Timeout(connect=10, read=300, write=30, pool=10)
# 触发故障切换的上游状态码（4xx 客户端错误除鉴权/限流外不重试）
_FAILOVER_STATUS = {401, 403, 408, 429}


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
        self.tracer = Tracer()
        self.settings: dict = {}
        self.key_row: dict = {}
        self.model_req = ""
        self.stream = False
        self.forward_body: dict = {}
        self.findings: List[dict] = []
        self.risk_level = "none"
        self.risk_score = 0
        self.audit_action = ""
        self.candidates: List[dict] = []
        self.client_ip = ""
        # 上游结果（failover 后填充）
        self.channel: Optional[dict] = None
        self.model_mapped = ""
        self.upstream_url = ""
        self.retries = 0


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
# prepare：鉴权 → 配额 → 安全审计 → 选路（同步；终端失败写日志并抛错）
# ---------------------------------------------------------------------------

def prepare(body: dict, raw_key: str, client_ip: str = "") -> Ctx:
    ctx = Ctx()
    ctx.client_ip = client_ip or ""
    ctx.settings = settings_svc.get_settings()
    ctx.stream = bool(body.get("stream"))
    ctx.model_req = str(body.get("model") or "")
    ctx.forward_body = body
    base = {
        "id": ctx.log_id, "created_at": ctx.created_at, "client_ip": ctx.client_ip,
        "model_requested": ctx.model_req, "stream": ctx.stream,
        "request_body": json.dumps(body, ensure_ascii=False),
        "key_id": "", "key_name": "",
    }

    # 1. 密钥鉴权
    try:
        ctx.key_row = key_svc.authenticate(raw_key)
        base["key_id"] = ctx.key_row["id"]
        base["key_name"] = ctx.key_row.get("name") or ""
        ctx.tracer.add("auth", "密钥鉴权", "ok", f"密钥「{base['key_name']}」校验通过")
    except key_svc.KeyRejected as e:
        ctx.tracer.add("auth", "密钥鉴权", "error", e.message)
        log_svc.write_log({**base, "status": "error", "status_code": 401,
                           "latency_ms": _latency(ctx), "trace_steps": ctx.tracer.dump(),
                           "error": e.message, "risk_level": "none"})
        raise GatewayError(401, e.message, e.code)

    # 2. 模型授权 + 配额扣减
    if not key_svc.check_model_allowed(ctx.key_row, ctx.model_req):
        msg = f"该密钥无权访问模型「{ctx.model_req}」"
        ctx.tracer.add("quota", "模型授权", "error", msg)
        log_svc.write_log({**base, "status": "blocked", "status_code": 403,
                           "latency_ms": _latency(ctx), "trace_steps": ctx.tracer.dump(),
                           "error": msg, "audit_action": "block"})
        raise GatewayError(403, msg, "model_not_allowed")
    key_svc.consume_request(ctx.key_row["id"])
    quota_txt = f"第 {int(ctx.key_row.get('request_count') or 0) + 1} 次调用"
    if int(ctx.key_row.get("request_quota") or 0):
        quota_txt += f" / 配额 {ctx.key_row['request_quota']}"
    ctx.tracer.add("quota", "配额扣减", "ok", quota_txt)

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
    if ctx.audit_action == "block":
        log_svc.write_log({
            **base, "status": "blocked", "status_code": 403, "latency_ms": _latency(ctx),
            "risk_level": risk_level, "risk_score": risk_score, "risks": findings,
            "audit_action": "block", "trace_steps": ctx.tracer.dump(),
            "error": "命中安全审计规则，已阻断",
        })
        raise GatewayError(403, "请求命中安全审计规则，已被网关阻断", "blocked_by_audit")

    # 4. 渠道选路（优先级 + 权重）
    ctx.candidates = ch_svc.select_candidates(ctx.model_req)
    if not ctx.candidates:
        msg = f"没有可用于模型「{ctx.model_req}」的渠道，请先在网关中配置渠道"
        ctx.tracer.add("route", "渠道选路", "error", msg)
        log_svc.write_log({**base, "status": "error", "status_code": 503,
                           "latency_ms": _latency(ctx), "risk_level": risk_level,
                           "risk_score": risk_score, "risks": findings,
                           "trace_steps": ctx.tracer.dump(), "error": msg})
        raise GatewayError(503, msg, "no_channel")
    health_note = "、".join(f"{c['name']}(P{c.get('priority', 0)}/W{c.get('weight', 1)})"
                           for c in ctx.candidates[:4])
    ctx.tracer.add("route", "渠道选路", "ok",
                   f"{len(ctx.candidates)} 个候选：{health_note}")
    ctx._base = base  # 供 finalize 复用
    return ctx


def _max_attempts(ctx: Ctx) -> int:
    return max(1, int(ctx.settings.get("retry_count", 2)) + 1)


def _finalize(ctx: Ctx, *, status: str, status_code: int, prompt_tokens: int = 0,
              completion_tokens: int = 0, response_preview: str = "",
              tool_calls: Optional[list] = None, error: str = "") -> None:
    """组装并写入最终日志，扣减密钥 Token 用量。"""
    total = int(prompt_tokens or 0) + int(completion_tokens or 0)
    if not total and response_preview:
        total = agent_obs.estimate_tokens(response_preview)
    try:
        key_svc.consume_tokens(ctx.key_row.get("id", ""), total)
    except Exception:
        pass
    req_tools = _requested_tool_names(ctx.forward_body)
    all_tools = list(dict.fromkeys(req_tools + [
        (tc.get("function", {}) or {}).get("name", "") for tc in (tool_calls or [])
    ]))
    log_svc.write_log({
        **getattr(ctx, "_base", {}),
        "channel_id": (ctx.channel or {}).get("id", ""),
        "channel_name": (ctx.channel or {}).get("name", ""),
        "model_mapped": ctx.model_mapped, "upstream_url": ctx.upstream_url,
        "status": status, "status_code": status_code, "retries": ctx.retries,
        "prompt_tokens": int(prompt_tokens or 0), "completion_tokens": int(completion_tokens or 0),
        "total_tokens": total, "latency_ms": _latency(ctx),
        "response_preview": response_preview, "tool_calls": [{"name": t} for t in all_tools if t],
        "risk_level": ctx.risk_level, "risk_score": ctx.risk_score, "risks": ctx.findings,
        "audit_action": ctx.audit_action, "trace_steps": ctx.tracer.dump(), "error": error,
    })


# ---------------------------------------------------------------------------
# 非流式响应
# ---------------------------------------------------------------------------

async def json_response(ctx: Ctx) -> tuple[int, dict]:
    last_err = ""
    for idx, ch in enumerate(ctx.candidates[:_max_attempts(ctx)]):
        ctx.retries = idx
        mapped = ch_svc.map_model(ch, ctx.model_req)
        url = ch_svc.build_upstream_url(ch)
        try:
            headers, payload = upstream.build_request(ch, ctx.forward_body, mapped, stream=False)
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(url, headers=headers, json=payload)
            sc = resp.status_code
            # 需要故障切换的状态：连接类/鉴权/限流/5xx；其余 4xx 直接返回不重试
            if sc in _FAILOVER_STATUS or sc >= 500:
                raise RuntimeError(f"上游返回 {sc}: {resp.text[:200]}")
            try:
                raw = resp.json()
            except Exception:
                raise RuntimeError(f"上游响应非 JSON: {resp.text[:200]}")
            normalized = upstream.normalize_response(ch, sc, raw)
            ch_svc.mark_success(ch["id"])
            ctx.channel, ctx.model_mapped, ctx.upstream_url = ch, mapped, url
            ctx.tracer.add("upstream", f"调用渠道 {ch['name']}", "ok",
                           f"{ch.get('protocol')} → {mapped}，HTTP {sc}")
            usage = normalized.get("usage") or {}
            pt, ct = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
            preview, tool_calls = _extract_preview(normalized)
            ctx.tracer.add("response", "响应下游", "ok",
                           f"tokens={pt}+{ct}，预览 {len(preview)} 字")
            ctx.tracer.add("log", "日志入库", "ok", "已留存调用记录与链路")
            _finalize(ctx, status="ok" if sc < 400 else "error", status_code=sc,
                      prompt_tokens=pt, completion_tokens=ct,
                      response_preview=preview, tool_calls=tool_calls)
            return sc, normalized
        except Exception as e:
            last_err = str(e)
            ch_svc.mark_failure(ch["id"], last_err)
            ctx.tracer.add("upstream", f"调用渠道 {ch['name']}", "error", last_err)
            logger.warning("网关上游失败，切换下一渠道: channel=%s err=%s", ch.get("name"), last_err)
            continue
    # 所有候选均失败
    ctx.tracer.add("response", "响应下游", "error", f"全部渠道失败: {last_err}")
    ctx.tracer.add("log", "日志入库", "ok", "已留存失败链路")
    _finalize(ctx, status="error", status_code=502, error=f"所有渠道均调用失败: {last_err}",
              response_preview="")
    raise GatewayError(502, f"所有渠道均调用失败: {last_err}", "all_channels_failed")


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
# 流式响应
# ---------------------------------------------------------------------------

async def stream_response(ctx: Ctx) -> AsyncGenerator[str, None]:
    """流式代理：连接阶段支持故障切换，一旦开始产出内容即锁定该渠道。

    结束时（finally）统一写日志，保证异常中断也有链路留存。
    """
    acc_text: List[str] = []
    usage = {"pt": 0, "ct": 0}
    tool_names: List[str] = []
    final_status, final_code, final_err = "error", 502, "所有渠道均调用失败"
    committed = False

    try:
        for idx, ch in enumerate(ctx.candidates[:_max_attempts(ctx)]):
            ctx.retries = idx
            mapped = ch_svc.map_model(ch, ctx.model_req)
            url = ch_svc.build_upstream_url(ch)
            headers, payload = upstream.build_request(ch, ctx.forward_body, mapped, stream=True)
            client = httpx.AsyncClient(timeout=_TIMEOUT)
            try:
                req = client.build_request("POST", url, headers=headers, json=payload)
                resp = await client.send(req, stream=True)
                if resp.status_code in _FAILOVER_STATUS or resp.status_code >= 400:
                    body_txt = (await resp.aread()).decode("utf-8", "ignore")[:200]
                    await resp.aclose(); await client.aclose()
                    ch_svc.mark_failure(ch["id"], f"HTTP {resp.status_code}: {body_txt}")
                    ctx.tracer.add("upstream", f"调用渠道 {ch['name']}", "error",
                                   f"HTTP {resp.status_code}")
                    final_err = f"上游 {resp.status_code}: {body_txt}"
                    continue
                # 连接成功，锁定渠道
                ctx.channel, ctx.model_mapped, ctx.upstream_url = ch, mapped, url
                ch_svc.mark_success(ch["id"])
                committed = True
                final_status, final_code, final_err = "ok", resp.status_code, ""
                ctx.tracer.add("upstream", f"调用渠道 {ch['name']}", "ok",
                               f"{ch.get('protocol')} → {mapped}（流式）")
                try:
                    async for sse in upstream.stream_iterator(ch, resp.aiter_lines()):
                        _account_sse(sse, acc_text, usage, tool_names)
                        yield sse
                finally:
                    await resp.aclose()
                    await client.aclose()
                break
            except Exception as e:
                try:
                    await client.aclose()
                except Exception:
                    pass
                if committed:
                    final_status, final_err = "error", str(e)
                    ctx.tracer.add("upstream", f"渠道 {ch['name']} 流式中断", "error", str(e))
                    break
                ch_svc.mark_failure(ch["id"], str(e))
                ctx.tracer.add("upstream", f"调用渠道 {ch['name']}", "error", str(e))
                final_err = str(e)
                continue
        if not committed:
            # 无一渠道可用：向下游返回一个错误 SSE 块，保持协议一致
            err_obj = {"error": {"message": final_err, "type": "gateway_error"}}
            yield "data: " + json.dumps(err_obj, ensure_ascii=False) + "\n\n"
            ctx.tracer.add("response", "响应下游", "error", final_err)
    finally:
        preview = "".join(acc_text)
        pt, ct = usage["pt"], usage["ct"]
        if not (pt or ct) and preview:
            ct = agent_obs.estimate_tokens(preview)
            pt = agent_obs.estimate_tokens(json.dumps(ctx.forward_body.get("messages") or [],
                                                      ensure_ascii=False))
        ctx.tracer.add("response", "响应下游",
                       "ok" if final_status == "ok" else "error",
                       f"tokens={pt}+{ct}，预览 {len(preview)} 字")
        ctx.tracer.add("log", "日志入库", "ok", "已留存流式调用链路")
        _finalize(ctx, status=final_status, status_code=final_code, prompt_tokens=pt,
                  completion_tokens=ct, response_preview=preview, error=final_err,
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
