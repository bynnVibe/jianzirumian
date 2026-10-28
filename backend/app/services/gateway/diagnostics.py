"""仅由独立 Proxy 执行的渠道诊断；凭据不进入查询串或日志。"""
import asyncio
import secrets

import httpx
from fastapi import APIRouter, Header, HTTPException

from app.config import settings
from app.services.gateway import channels, upstream

router = APIRouter(prefix="/internal/diagnostics", include_in_schema=False)


@router.post("/channels/{channel_id}")
async def diagnose(channel_id: str, x_diagnostic_token: str = Header(default="")):
    token = settings.PROXY_DIAGNOSTIC_TOKEN
    if len(token) < 32:
        raise HTTPException(503, "Proxy 内部诊断凭据未配置（至少32字符）")
    if not secrets.compare_digest(token.encode(), x_diagnostic_token.encode()):
        raise HTTPException(403, "内部诊断鉴权失败")
    row = channels.get_channel(channel_id, with_secret=True)
    if not row:
        raise HTTPException(404, "渠道不存在")
    models, mapping = row.get("models") or [], row.get("model_mapping") or {}
    model = next(iter(mapping), None) or (models[0] if models else "")
    if not model:
        raise HTTPException(400, "请先为渠道配置模型")
    mapped = channels.map_model(row, model)
    body = {"model": model, "messages": [{"role": "user", "content": "ping"}],
            "stream": False, "max_tokens": 5}
    try:
        async with asyncio.timeout(30):
            headers, payload = upstream.build_request(row, body, mapped, stream=False)
            async with httpx.AsyncClient(timeout=httpx.Timeout(25, connect=8), follow_redirects=False) as client:
                response = await client.post(channels.build_upstream_url(row), headers=headers, json=payload)
            ok = 200 <= response.status_code < 300
            if ok:
                normalized = upstream.normalize_response(row, response.status_code, response.json())
                ok = bool(normalized.get("choices")) and not normalized.get("error")
        detail = "Proxy 上游请求成功" if ok else f"Proxy 上游返回 HTTP {response.status_code}"
        if ok:
            channels.mark_success(channel_id)
        else:
            channels.mark_failure(channel_id, detail)
        return {"success": ok, "executor": "llm-proxy", "model": mapped,
                "status_code": response.status_code, "detail": detail}
    except Exception:
        # 不返回原始异常、上游响应或 URL，避免其中携带密钥。
        channels.mark_failure(channel_id, "Proxy 诊断失败（连接、协议或超时）")
        return {"success": False, "executor": "llm-proxy", "model": mapped,
                "status_code": 0, "detail": "Proxy 诊断失败，请核对网络、模型及协议配置"}
