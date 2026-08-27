"""
OpenRouter 免费模型工具：免费判定、列表拉取、模型不可用时自动降级切换
"""
import logging
import random

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def is_free_model(m: dict) -> bool:
    """判断是否为免费模型：所有 pricing 字段均为 0，或 id 带 :free 后缀"""
    pricing = m.get("pricing") or {}
    # 检查所有计价维度，任一非 0 则不视为免费
    for key, value in pricing.items():
        if key in ("prompt", "completion", "image", "request"):
            try:
                if float(value or 0) != 0:
                    return False
            except (TypeError, ValueError):
                return False
    # 兜底：id 带 :free 后缀也认为是免费
    if str(m.get("id", "")).endswith(":free"):
        return True
    # 如果 pricing 对象为空或没有有效字段，则按 :free 兜底判断；否则认为免费
    return bool(pricing)


async def fetch_free_model_ids() -> list[str]:
    """实时拉取 OpenRouter 模型列表，返回免费模型 id 列表（失败返回空列表）"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(OPENROUTER_MODELS_URL)
            resp.raise_for_status()
            data = resp.json().get("data", [])
    except Exception as e:
        logger.warning("拉取 OpenRouter 模型列表失败: %s", e)
        return []
    return [m.get("id", "") for m in data if m.get("id") and is_free_model(m)]


async def try_openrouter_fallback(llm_provider: str | None = None) -> str | None:
    """OpenRouter 免费模型降级：当前模型不可用时重新拉取免费列表并随机切换。

    仅在当前 LLM 为 custom 且指向 openrouter.ai 时生效。
    切换成功返回新模型 id（已持久化到运行时配置），失败返回 None。
    """
    from app.services.config_manager import effective_provider_config, runtime_config

    provider = llm_provider or runtime_config.llm_provider or settings.LLM_PROVIDER
    if provider != "custom":
        return None
    cfg = effective_provider_config("llm", "custom")
    if "openrouter.ai" not in (cfg.get("base_url") or ""):
        return None

    current_model = cfg.get("model") or ""
    ids = await fetch_free_model_ids()
    candidates = [i for i in ids if i != current_model]
    if not candidates:
        logger.warning("OpenRouter 降级失败：无可用免费模型（当前=%s）", current_model)
        return None
    new_model = random.choice(candidates)
    runtime_config.set_provider_settings("llm", "custom", {"model": new_model})
    logger.info("OpenRouter 免费模型降级切换: %s -> %s", current_model, new_model)
    return new_model
