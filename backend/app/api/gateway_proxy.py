"""
见字如面 - 模型网关 OpenAI 兼容代理端点（/v1）

对下游工具/应用暴露标准 OpenAI 协议入口，凭本地网关密钥（sk-jzrm-...）鉴权，
不校验站点登录态（在 main.py 的 AuthMiddleware 白名单中放行 /v1/）。

- POST /v1/chat/completions  聊天补全（支持 stream 流式）
- GET  /v1/models            网关聚合的可用模型清单
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.services.gateway import channels as ch_svc
from app.services.gateway import keys as key_svc
from app.services.gateway import proxy

logger = logging.getLogger("jianziruyang.gateway.proxy_api")

router = APIRouter(prefix="/v1", tags=["gateway-proxy"])


def _extract_key(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    # 兼容部分客户端用 x-api-key
    return (request.headers.get("x-api-key", "") or "").strip()


def _err(status_code: int, message: str, err_type: str = "gateway_error") -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": err_type, "code": status_code}},
    )


@router.post("/chat/completions")
async def chat_completions(request: Request):
    raw_key = _extract_key(request)
    client_ip = request.client.host if request.client else ""
    try:
        body = await request.json()
    except Exception:
        return _err(400, "请求体不是合法的 JSON", "invalid_request_error")
    if not isinstance(body, dict) or not body.get("model"):
        return _err(400, "缺少必填字段 model", "invalid_request_error")

    # prepare：鉴权 → 配额 → 安全审计 → 选路（终端失败会写日志并抛 GatewayError）
    try:
        ctx = proxy.prepare(body, raw_key, client_ip)
    except proxy.GatewayError as e:
        return _err(e.status_code, e.message, e.code)

    if ctx.stream:
        return StreamingResponse(
            proxy.stream_response(ctx),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "X-Gateway-Risk": ctx.risk_level},
        )
    try:
        status, data = await proxy.json_response(ctx)
        resp = JSONResponse(status_code=status, content=data)
    except proxy.GatewayError as e:
        resp = _err(e.status_code, e.message, e.code)
    resp.headers["X-Gateway-Risk"] = ctx.risk_level
    if ctx.audit_action:
        resp.headers["X-Gateway-Audit"] = ctx.audit_action
    return resp


@router.get("/models")
async def list_models(request: Request):
    raw_key = _extract_key(request)
    try:
        key_row = key_svc.authenticate(raw_key)
    except key_svc.KeyRejected as e:
        return _err(401, e.message, e.code)
    models = ch_svc.all_models()
    # 若密钥限制了可访问模型，只返回其允许的子集
    import json as _json
    try:
        allowed = _json.loads(key_row.get("allowed_models") or "[]")
    except (ValueError, TypeError):
        allowed = []
    if allowed:
        models = [m for m in models if m in allowed]
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "owned_by": "jianziruyang-gateway"} for m in models],
    }
