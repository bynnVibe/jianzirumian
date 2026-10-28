"""
见字如面 - LLM Proxy 独立服务入口（层3）

把模型网关的 OpenAI 兼容代理运行时（/v1）从 Agent 单体中拆出，作为独立可部署服务：
- 复用 app.services.gateway/*（proxy/upstream/channels/keys/security/logs/settings），
  读取与 Agent 共享的 SQLite 网关配置（channels/keys/settings），并写网关请求日志。
- 仅挂载 /v1 代理路由 + /health + /metrics；不挂载业务路由与站点鉴权中间件
  （/v1 凭网关本地密钥 sk-jzrm-... 自鉴权）。
- 与 Agent Service（层2）通过 HTTP 互通：Agent 可配置 LLM_PROVIDER=gateway 把
  LLM 调用导向本服务的 /v1，从而获得多模型路由 / Key 轮换 / 缓存 / 降级 / 成本统计。

部署：compose 服务 llm-proxy，命令 uvicorn app.llm_proxy:app --port 8001，
与 agent 共享 ./data 卷（同宿主机文件系统，SQLite WAL 多进程可接受）。
"""
import logging
import os
import sys
from pathlib import Path

# 确保项目根目录在路径中（与 app.main 一致）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

from app.api import gateway_proxy as gateway_proxy_api
from app.config import settings
from app.core import metrics_export
from app.core.log_setup import setup_logging
from app.core.trusted_proxy import TrustedProxyMiddleware
from app.services.gateway.diagnostics import router as diagnostic_router

setup_logging(debug=settings.DEBUG)
logger = logging.getLogger("jianziruyang.llm_proxy")

app = FastAPI(
    title="见字如面 LLM Proxy",
    description="层3 自建 LLM 网关：多模型路由 / Key 轮换 / 缓存 / 降级 / 成本统计（OpenAI 兼容 /v1）",
    version="1.0.0",
)

# OpenAI 兼容代理入口（/v1/chat/completions、/v1/models），凭网关本地密钥自鉴权
app.include_router(gateway_proxy_api.router)
app.include_router(diagnostic_router)
app.add_middleware(TrustedProxyMiddleware, trusted_hosts=settings.TRUSTED_PROXY_CIDRS)


@app.get("/health")
async def health():
    """健康检查（供 compose / 监控层探活）"""
    return JSONResponse(content={"status": "ok", "service": "llm-proxy"})


@app.get("/ready")
async def ready():
    from app.core.agent_runtime import readiness
    result = readiness("llm-proxy")
    return JSONResponse(result, status_code=200 if result["status"] == "ready" else 503)


@app.get("/metrics")
async def metrics():
    """Prometheus 指标（监控层抓取）"""
    return Response(
        content=metrics_export.render_prometheus("llm-proxy"),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.llm_proxy:app",
        host=settings.HOST,
        port=int(os.getenv("LLM_PROXY_PORT", "8001")),
        reload=settings.DEBUG,
        proxy_headers=False,
    )
