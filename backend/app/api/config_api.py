"""
见字如面 - 系统配置 API 路由
运行时 provider 切换、参数在线配置、连接测试
"""
import asyncio
import json
import logging
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.deps import require_admin
from app.config import settings
from app.core.file_cache import effective_file_cache_enabled, effective_redis_url, file_cache
from app.core.llm import LLMFactory
from app.core.vector_store import vector_store
from app.models.schemas import SystemConfig
from app.services.config_manager import effective_provider_config, runtime_config
from app.services.web_search import WebSearchFactory

logger = logging.getLogger("jianziruyang.config")
router = APIRouter(prefix="/api/config", tags=["config"])


# ============================================
# 数据模型
# ============================================
class ProviderSwitch(BaseModel):
    """切换提供商"""
    provider_type: str  # llm | ocr | search
    value: str


class TestConnectionRequest(BaseModel):
    """连接测试请求"""
    provider_type: str  # llm | search
    provider: str       # 具体提供商名称
    model: str = ""     # 模型名称（LLM 可选）


class TestConnectionResult(BaseModel):
    """连接测试结果"""
    success: bool
    message: str
    elapsed: float = 0


class ProviderSettingsBody(BaseModel):
    """保存提供商参数（页面在线配置）"""
    category: str   # llm | ocr | search | embedding
    provider: str
    values: dict


class AssistantLLMBody(BaseModel):
    """保存知识助手专用模型配置"""
    enabled: bool = False
    provider: str = ""      # ollama | openai | custom
    base_url: str = ""
    model: str = ""
    api_key: str = ""       # 留空表示保持现有值不变


# ============================================
# 获取系统配置（完整版）
# ============================================
AVAILABLE_LLM_PROVIDERS = ["ollama", "openai", "custom"]
AVAILABLE_OCR_PROVIDERS = ["local", "aliyun", "custom_api"]
AVAILABLE_SEARCH_PROVIDERS = ["duckduckgo", "bing", "serpapi", "anysearch"]
AVAILABLE_EMBEDDING_PROVIDERS = ["ollama", "openai", "custom"]
AVAILABLE_RERANK_PROVIDERS = ["local", "dashscope"]

# 各类别/提供商允许页面编辑的字段（白名单，防止写入任意键）
EDITABLE_FIELDS = {
    "llm": {
        "ollama": {"base_url", "model"},
        "openai": {"base_url", "api_key", "model"},
        "custom": {"base_url", "api_key", "model"},
    },
    "ocr": {
        "aliyun": {"base_url", "api_key", "model"},
        "custom_api": {"api_url", "api_key"},
    },
    "search": {
        "bing": {"api_key"},
        "serpapi": {"api_key"},
        "anysearch": {"api_key"},
    },
    "embedding": {
        "ollama": {"base_url", "model"},
        "openai": {"base_url", "api_key", "model"},
        "custom": {"base_url", "api_key", "model"},
    },
    "rerank": {
        "local": {"model"},
        "dashscope": {"base_url", "api_key", "model"},
    },
}

# 敏感字段：不回传明文，只回传 has_key 标记
SECRET_FIELDS = {"api_key"}


def _public_config(category: str, provider: str) -> dict:
    """生效配置的对外版本：敏感字段转为 has_key 标记"""
    cfg = effective_provider_config(category, provider)
    out = {}
    for k, v in cfg.items():
        if k in SECRET_FIELDS:
            out["has_key"] = bool(v)
        else:
            out[k] = v
    return out


def _public_assistant_llm() -> dict:
    """知识助手专用模型配置的对外版本：api_key 转为 has_key 标记。

    回传 enabled/provider/base_url/model 及 has_key，并附带一个 effective 标记
    表示当前助手是否真的在使用独立模型（否则回退系统主 LLM）。
    """
    cfg = runtime_config.assistant_llm or {}
    provider = (cfg.get("provider") or "").strip()
    enabled = bool(cfg.get("enabled")) and provider in AVAILABLE_LLM_PROVIDERS
    out = {
        "enabled": bool(cfg.get("enabled")),
        "provider": provider,
        "base_url": (cfg.get("base_url") or "").strip(),
        "model": (cfg.get("model") or "").strip(),
        "has_key": bool((cfg.get("api_key") or "").strip()),
        "effective": enabled and bool((cfg.get("model") or "").strip()),
    }
    return out


@router.get("/public")
async def get_public_config():
    """公开配置（无需登录）：仅返回注册开关与游客免费次数，供登录页/游客展示"""
    return {
        "success": True,
        "registration_enabled": runtime_config.registration_enabled,
        "guest_query_limit": runtime_config.guest_query_limit,
    }


@router.get("")
@router.get("/")
async def get_full_config():
    """获取完整系统配置（参数均为生效值：页面保存的覆盖 > .env 默认）"""
    current_llm = runtime_config.llm_provider or settings.LLM_PROVIDER
    current_ocr = runtime_config.ocr_provider or settings.OCR_PROVIDER
    current_search = runtime_config.search_provider or settings.SEARCH_PROVIDER
    current_embedding = runtime_config.embedding_provider or settings.EMBEDDING_PROVIDER
    current_rerank = runtime_config.rerank_provider or settings.RERANKER_PROVIDER

    # 检查向量库状态
    embedding_ok = vector_store.is_available()
    doc_count = vector_store.get_document_count() if embedding_ok else 0

    return {
        "llm_provider": current_llm,
        "ocr_provider": current_ocr,
        "search_provider": current_search,
        "embedding_provider": current_embedding,
        "rerank_provider": current_rerank,
        "available_llm_providers": AVAILABLE_LLM_PROVIDERS,
        "available_ocr_providers": AVAILABLE_OCR_PROVIDERS,
        "available_search_providers": AVAILABLE_SEARCH_PROVIDERS,
        "available_embedding_providers": AVAILABLE_EMBEDDING_PROVIDERS,
        "available_rerank_providers": AVAILABLE_RERANK_PROVIDERS,
        "registration_enabled": runtime_config.registration_enabled,
        "guest_query_limit": runtime_config.guest_query_limit,
        "llm_config": {p: _public_config("llm", p) for p in AVAILABLE_LLM_PROVIDERS},
        "ocr_config": {
            "local": {"note": "本地识别，无需配置"},
            "aliyun": _public_config("ocr", "aliyun"),
            "custom_api": _public_config("ocr", "custom_api"),
        },
        "search_config": {
            "duckduckgo": {"note": "免费，无需 API Key"},
            "bing": _public_config("search", "bing"),
            "serpapi": _public_config("search", "serpapi"),
            "anysearch": _public_config("search", "anysearch"),
        },
        "embedding": {
            "provider": current_embedding,
            "available": embedding_ok,
            "knowledge_count": doc_count,
            **_public_config("embedding", current_embedding),
        },
        "embedding_config": {p: _public_config("embedding", p) for p in AVAILABLE_EMBEDDING_PROVIDERS},
        "rerank_config": {p: _public_config("rerank", p) for p in AVAILABLE_RERANK_PROVIDERS},
        "assistant_llm": _public_assistant_llm(),
        "redis_config": {
            "url": effective_redis_url(),
            "enabled": effective_file_cache_enabled(),
            "connected": file_cache.status()["connected"],
            "ttl": settings.FILE_CACHE_TTL,
            "max_mb": settings.FILE_CACHE_MAX_MB,
        },
    }


# ============================================
# 切换提供商
# ============================================
@router.post("/provider")
async def switch_provider(body: ProviderSwitch, _admin: dict = Depends(require_admin)):
    """切换运行时提供商（仅管理员，影响全局配置）"""
    valid_types = {"llm", "ocr", "search", "embedding", "rerank"}

    if body.provider_type not in valid_types:
        return {"success": False, "message": f"不支持的类型: {body.provider_type}"}

    # 验证值是否合法
    valid_values = {
        "llm": AVAILABLE_LLM_PROVIDERS,
        "ocr": AVAILABLE_OCR_PROVIDERS,
        "search": AVAILABLE_SEARCH_PROVIDERS,
        "embedding": AVAILABLE_EMBEDDING_PROVIDERS,
        "rerank": AVAILABLE_RERANK_PROVIDERS,
    }
    if body.value not in valid_values[body.provider_type]:
        return {
            "success": False,
            "message": f"不支持的 {body.provider_type} 提供商: {body.value}，可选: {valid_values[body.provider_type]}",
        }

    # 保存运行时配置
    key_map = {
        "llm": "llm_provider",
        "ocr": "ocr_provider",
        "search": "search_provider",
        "embedding": "embedding_provider",
        "rerank": "rerank_provider",
    }
    setattr(runtime_config, key_map[body.provider_type], body.value)
    logger.info("切换 %s 提供商为: %s", body.provider_type, body.value)

    # Embedding 切换后需重建实例（失败不阻断切换，后续可通过测试重试）
    if body.provider_type == "embedding":
        try:
            vector_store.retry_init(force=True)
        except Exception as e:
            logger.warning("Embedding 切换后初始化失败: %s", e)

    return {"success": True, "message": f"{body.provider_type} 提供商已切换为 {body.value}"}


@router.post("/provider-settings")
async def save_provider_settings(body: ProviderSettingsBody, _admin: dict = Depends(require_admin)):
    """保存提供商参数（仅管理员），持久化为系统默认配置

    敏感字段（api_key）传空表示保持现有值不变。
    """
    allowed = EDITABLE_FIELDS.get(body.category, {}).get(body.provider)
    if allowed is None:
        return {"success": False, "message": f"不支持的配置项: {body.category}/{body.provider}"}

    values = {}
    for k, v in body.values.items():
        if k not in allowed or not isinstance(v, str):
            continue
        v = v.strip()
        # 敏感字段留空 = 不修改；其他字段允许置空（回退到 .env 默认值）
        if k in SECRET_FIELDS and not v:
            continue
        values[k] = v

    if not values:
        return {"success": False, "message": "没有可保存的配置项"}

    runtime_config.set_provider_settings(body.category, body.provider, values)
    logger.info("保存 %s/%s 参数: %s", body.category, body.provider, sorted(values.keys()))

    # Embedding 参数变更后重建实例，使新配置立即生效
    if body.category == "embedding":
        try:
            vector_store.retry_init(force=True)
        except Exception as e:
            logger.warning("Embedding 配置保存后初始化失败: %s", e)

    return {"success": True, "message": "配置已保存，并已同步为系统默认配置"}


# ============================================
# 知识助手专用模型（与问答界面模型分离）
# ============================================
@router.post("/assistant-llm")
async def save_assistant_llm(body: AssistantLLMBody, _admin: dict = Depends(require_admin)):
    """保存知识助手专用模型配置（仅管理员）。

    enabled=False 时助手回退使用系统主 LLM；api_key 留空表示保持已保存的值不变。
    未填写的 base_url 会在构造时回退到所选 provider 的生效默认值。
    """
    provider = (body.provider or "").strip()
    if body.enabled and provider not in AVAILABLE_LLM_PROVIDERS:
        return {"success": False, "message": f"启用独立模型时需选择有效提供商，可选: {AVAILABLE_LLM_PROVIDERS}"}
    if body.enabled and provider in ("openai", "custom") and not (body.model or "").strip():
        return {"success": False, "message": "启用独立模型时需填写模型名称"}

    current = runtime_config.assistant_llm or {}
    api_key = (body.api_key or "").strip()
    # api_key 留空 = 保持现有值；启用关闭时也保留旧值以便再次开启
    if not api_key:
        api_key = (current.get("api_key") or "").strip()

    runtime_config.assistant_llm = {
        "enabled": bool(body.enabled),
        "provider": provider,
        "base_url": (body.base_url or "").strip(),
        "model": (body.model or "").strip(),
        "api_key": api_key,
    }
    logger.info("保存知识助手模型配置: enabled=%s provider=%s model=%s",
                body.enabled, provider or "-", (body.model or "-"))
    return {
        "success": True,
        "message": "知识助手模型配置已保存并立即生效" if body.enabled else "已保存：知识助手将使用系统主模型",
        "assistant_llm": _public_assistant_llm(),
    }


@router.post("/test-assistant-llm", response_model=TestConnectionResult)
async def test_assistant_llm_connection(_admin: dict = Depends(require_admin)):
    """测试知识助手专用模型连接（使用当前已保存的助手配置）。"""
    if not (runtime_config.assistant_llm or {}).get("enabled"):
        return TestConnectionResult(success=False, message="尚未启用知识助手独立模型，将使用系统主模型")
    start = time.time()
    try:
        from app.core.llm import LLMFactory
        llm = LLMFactory.create_assistant_llm()
        collected = ""
        async for chunk in llm.chat([{"role": "user", "content": "回复'连接成功'四个字"}], stream=True):
            collected += chunk
            if len(collected) > 50:
                break
        elapsed = time.time() - start
        if collected.strip():
            return TestConnectionResult(
                success=True,
                message=f"连接成功！模型回复: {collected.strip()[:50]}",
                elapsed=round(elapsed, 2),
            )
        return TestConnectionResult(success=False, message="模型返回空响应", elapsed=round(elapsed, 2))
    except Exception as e:
        return TestConnectionResult(
            success=False, message=str(e)[:120], elapsed=round(time.time() - start, 2)
        )


@router.post("/registration")
async def toggle_registration(body: dict, _admin: dict = Depends(require_admin)):
    """开启/关闭用户注册功能（仅管理员）"""
    enabled = body.get("enabled", True)
    runtime_config.registration_enabled = enabled
    logger.info("注册功能已%s", "开启" if enabled else "关闭")
    return {"success": True, "registration_enabled": enabled}


@router.post("/guest-limit")
async def set_guest_limit(body: dict, _admin: dict = Depends(require_admin)):
    """设定游客免费访问次数（仅管理员，按 IP 计数）"""
    try:
        limit = int(body.get("limit", 3))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="次数必须为整数")
    if limit < 0:
        raise HTTPException(status_code=400, detail="次数不能为负数")
    runtime_config.guest_query_limit = limit
    logger.info("游客免费访问次数已设为: %d", limit)
    return {"success": True, "guest_query_limit": limit}


# ============================================
# Redis 文件缓存配置
# ============================================
@router.get("/redis")
async def get_redis_config(_admin: dict = Depends(require_admin)):
    """获取 Redis 文件缓存配置（仅管理员）"""
    return {
        "success": True,
        "url": effective_redis_url(),
        "enabled": effective_file_cache_enabled(),
        "connected": file_cache.status()["connected"],
        "ttl": settings.FILE_CACHE_TTL,
        "max_mb": settings.FILE_CACHE_MAX_MB,
    }


@router.post("/redis")
async def save_redis_config(body: dict, _admin: dict = Depends(require_admin)):
    """保存 Redis 文件缓存配置（仅管理员）：url 留空 = 回退 .env 默认；保存后立即重连生效"""
    if "url" in body:
        url = (body.get("url") or "").strip()
        if url and not url.startswith(("redis://", "rediss://")):
            return {"success": False, "message": "地址需以 redis:// 或 rediss:// 开头"}
        runtime_config.redis_url = url
    if "enabled" in body:
        runtime_config.file_cache_enabled = bool(body.get("enabled"))
    # 配置变更后重置连接，下次访问时用新配置重连
    file_cache.reset()
    logger.info("Redis 文件缓存配置已保存: url=%s enabled=%s",
                runtime_config.redis_url or "(.env 默认)", runtime_config.file_cache_enabled)
    return {
        "success": True,
        "message": "Redis 配置已保存",
        "url": effective_redis_url(),
        "enabled": effective_file_cache_enabled(),
    }


@router.post("/test-redis", response_model=TestConnectionResult)
async def test_redis_connection(body: dict = None, _admin: dict = Depends(require_admin)):
    """测试 Redis 连接（仅管理员）；可传 url 测试任意地址，不传则测当前生效配置"""
    url = ((body or {}).get("url") or "").strip() or effective_redis_url()
    start = time.time()
    try:
        import redis
        client = redis.Redis.from_url(url, socket_connect_timeout=3, socket_timeout=3)
        client.ping()
        elapsed = time.time() - start
        # 若测的是当前生效地址，顺便重置连接池（可能之前处于失败冷却期）
        if url == effective_redis_url():
            file_cache.reset()
        return TestConnectionResult(
            success=True,
            message=f"Redis 连接正常 ({url})",
            elapsed=round(elapsed, 2),
        )
    except Exception as e:
        return TestConnectionResult(
            success=False,
            message=f"连接失败: {str(e)[:120]}",
            elapsed=round(time.time() - start, 2),
        )


@router.get("/retrieval")
async def get_retrieval_config(_admin: dict = Depends(require_admin)):
    """获取混合检索参数（仅管理员）"""
    return {
        "success": True,
        "retrieval_mode": runtime_config.retrieval_mode,
        "dense_top_k": runtime_config.dense_top_k,
        "sparse_top_k": runtime_config.sparse_top_k,
        "rrf_k": runtime_config.rrf_k,
        "rerank_top_k": runtime_config.rerank_top_k,
    }


@router.post("/retrieval")
async def set_retrieval_config(body: dict, _admin: dict = Depends(require_admin)):
    """保存混合检索参数（仅管理员）：retrieval_mode/dense_top_k/sparse_top_k/rrf_k/rerank_top_k"""
    if "retrieval_mode" in body:
        runtime_config.retrieval_mode = body["retrieval_mode"]
    for key in ("dense_top_k", "sparse_top_k", "rrf_k", "rerank_top_k"):
        if body.get(key):
            setattr(runtime_config, key, body[key])
    logger.info("混合检索参数已保存: %s", body)
    return {"success": True, "message": "检索参数已保存"}


# ============================================
# 连接测试（仅管理员，测试使用当前生效配置）
# ============================================


@router.post("/test-llm", response_model=TestConnectionResult)
async def test_llm_connection(body: TestConnectionRequest, _admin: dict = Depends(require_admin)):
    """测试 LLM 连接"""
    start = time.time()
    try:
        llm = LLMFactory.create(body.provider)
        test_messages = [{"role": "user", "content": "回复'连接成功'四个字"}]

        collected = ""
        async for chunk in llm.chat(test_messages, stream=True):
            collected += chunk
            if len(collected) > 50:
                break

        elapsed = time.time() - start
        if collected.strip():
            return TestConnectionResult(
                success=True,
                message=f"连接成功！模型回复: {collected.strip()[:50]}",
                elapsed=round(elapsed, 2),
            )
        else:
            return TestConnectionResult(
                success=False,
                message="模型返回空响应",
                elapsed=round(elapsed, 2),
            )
    except Exception as e:
        elapsed = time.time() - start
        err_msg = str(e)
        if "connect" in err_msg.lower() or "Connection refused" in err_msg:
            hint = f"无法连接到 {body.provider} 服务，请检查服务是否已启动"
        elif "401" in err_msg or "unauthorized" in err_msg.lower() or "auth" in err_msg.lower():
            hint = "API Key 无效或已过期"
        elif "429" in err_msg or "quota" in err_msg.lower() or "insufficient" in err_msg.lower():
            hint = "API 配额不足"
        else:
            hint = err_msg[:100]
        return TestConnectionResult(
            success=False,
            message=hint,
            elapsed=round(elapsed, 2),
        )


@router.post("/test-search", response_model=TestConnectionResult)
async def test_search_connection(body: TestConnectionRequest, _admin: dict = Depends(require_admin)):
    """测试搜索连接"""
    start = time.time()
    try:
        searcher = WebSearchFactory.create(body.provider)
        results = await searcher.search("测试", max_results=1)
        elapsed = time.time() - start
        if results:
            return TestConnectionResult(
                success=True,
                message=f"搜索成功，返回 {len(results)} 条结果",
                elapsed=round(elapsed, 2),
            )
        else:
            return TestConnectionResult(
                success=False,
                message="搜索返回空结果",
                elapsed=round(elapsed, 2),
            )
    except Exception as e:
        elapsed = time.time() - start
        return TestConnectionResult(
            success=False,
            message=str(e)[:100],
            elapsed=round(elapsed, 2),
        )


@router.post("/test-ocr", response_model=TestConnectionResult)
async def test_ocr_connection(body: TestConnectionRequest, _admin: dict = Depends(require_admin)):
    """测试 OCR 配置（不消耗图片识别，仅验证服务可达与凭证）"""
    start = time.time()
    provider = body.provider
    try:
        if provider == "local":
            return TestConnectionResult(
                success=True,
                message="本地 OCR 首次使用时自动初始化，无需测试",
                elapsed=0,
            )
        elif provider == "aliyun":
            cfg = effective_provider_config("ocr", "aliyun")
            if not cfg.get("api_key"):
                return TestConnectionResult(success=False, message="未配置 API Key")
            url = f"{cfg['base_url'].rstrip('/')}/chat/completions"
            payload = {
                "model": cfg.get("model"),
                "messages": [{"role": "user", "content": [{"type": "text", "text": "ping"}]}],
                "max_tokens": 8,
            }
            headers = {"Authorization": f"Bearer {cfg['api_key']}"}
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json=payload, headers=headers)
            elapsed = time.time() - start
            if resp.status_code == 200:
                return TestConnectionResult(success=True, message="百炼 OCR 连接正常", elapsed=round(elapsed, 2))
            return TestConnectionResult(
                success=False,
                message=f"HTTP {resp.status_code}: {resp.text[:100]}",
                elapsed=round(elapsed, 2),
            )
        elif provider == "custom_api":
            cfg = effective_provider_config("ocr", "custom_api")
            if not cfg.get("api_url"):
                return TestConnectionResult(success=False, message="未配置 OCR API URL")
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(cfg["api_url"])
            elapsed = time.time() - start
            # 能连通即可（部分接口不支持 GET 会返回 405，属于可达）
            return TestConnectionResult(
                success=resp.status_code < 500,
                message=f"服务可达 (HTTP {resp.status_code})",
                elapsed=round(elapsed, 2),
            )
        else:
            return TestConnectionResult(success=False, message=f"不支持的 OCR 提供商: {provider}")
    except Exception as e:
        return TestConnectionResult(
            success=False,
            message=str(e)[:100],
            elapsed=round(time.time() - start, 2),
        )


@router.post("/test-embedding", response_model=TestConnectionResult)
async def test_embedding_connection(_admin: dict = Depends(require_admin)):
    """测试 Embedding 连接（使用当前生效配置重建并初始化）"""
    start = time.time()
    try:
        ok = vector_store.retry_init(force=True)
        elapsed = time.time() - start
        if not ok:
            return TestConnectionResult(
                success=False,
                message="Embedding 服务不可用，请检查配置",
                elapsed=round(elapsed, 2),
            )
        return TestConnectionResult(
            success=True,
            message="Embedding 服务连接正常",
            elapsed=round(elapsed, 2),
        )
    except Exception as e:
        elapsed = time.time() - start
        return TestConnectionResult(
            success=False,
            message=str(e)[:100],
            elapsed=round(elapsed, 2),
        )


# ============================================
# OpenRouter 免费模型搜索
# ============================================
from app.core.openrouter import OPENROUTER_BASE_URL, OPENROUTER_MODELS_URL, is_free_model

# 内存缓存：模型列表较大，10 分钟内复用，避免频繁请求外部 API
_openrouter_cache = {"ts": 0.0, "models": []}


@router.get("/openrouter-models")
async def list_openrouter_models(
    search: str = Query("", description="搜索关键词（匹配模型 id/name）"),
    refresh: bool = Query(False, description="强制重新拉取 OpenRouter 列表"),
    _admin: dict = Depends(require_admin),
):
    """实时搜索 OpenRouter 免费模型列表（仅管理员）"""
    now = time.time()
    models = _openrouter_cache["models"]

    # 缓存过期、为空，或用户点击刷新时重新拉取
    if refresh or now - _openrouter_cache["ts"] > 600 or not models:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(OPENROUTER_MODELS_URL)
                resp.raise_for_status()
                data = resp.json().get("data", [])
        except Exception as e:
            logger.warning("拉取 OpenRouter 模型列表失败: %s", e)
            if not models:
                return {"success": False, "message": f"无法访问 OpenRouter: {str(e)[:100]}", "models": [], "total": 0}
            data = None  # 拉取失败但有缓存，继续使用缓存

        if data is not None:
            free = []
            for m in data:
                if not is_free_model(m):
                    continue
                free.append({
                    "id": m.get("id", ""),
                    "name": m.get("name", ""),
                    "context_length": m.get("context_length") or 0,
                    "description": (m.get("description") or "")[:160],
                })
            # 上下文长度降序，长上下文模型优先展示
            free.sort(key=lambda x: x["context_length"], reverse=True)
            _openrouter_cache["ts"] = now
            _openrouter_cache["models"] = free
            models = free

    kw = search.strip().lower()
    if kw:
        filtered = [
            m for m in models
            if kw in (m["id"] or "").lower() or kw in (m["name"] or "").lower()
        ]
    else:
        filtered = models

    return {
        "success": True,
        "models": filtered[:100],
        "total": len(filtered),
    }


@router.post("/openrouter-apply")
async def apply_openrouter_model(body: dict, _admin: dict = Depends(require_admin)):
    """将 OpenRouter 免费模型应用为系统 LLM（仅管理员）

    写入 custom 提供商配置（base_url + model，可选 api_key）并切换 LLM 提供商为 custom。
    """
    model_id = (body.get("model") or "").strip()
    api_key = (body.get("api_key") or "").strip()
    if not model_id:
        return {"success": False, "message": "未指定模型"}

    values = {"base_url": OPENROUTER_BASE_URL, "model": model_id}
    if api_key:
        values["api_key"] = api_key
    runtime_config.set_provider_settings("llm", "custom", values)
    runtime_config.llm_provider = "custom"
    logger.info("已将 LLM 切换为 OpenRouter 免费模型: %s", model_id)
    return {"success": True, "message": f"LLM 已切换为 OpenRouter 模型 {model_id}"}


@router.post("/test-rerank", response_model=TestConnectionResult)
async def test_rerank_connection(body: TestConnectionRequest, _admin: dict = Depends(require_admin)):
    """测试 Rerank 配置（用样例文本实际打分验证）

    本地模型首次测试需加载模型，可能耗时较长；打分运行在线程池中，不阻塞服务。
    """
    from app.core.reranker import current_provider, score_texts

    provider = body.provider or current_provider()
    if provider not in AVAILABLE_RERANK_PROVIDERS:
        return TestConnectionResult(success=False, message=f"不支持的 rerank 提供商: {provider}")

    start = time.time()
    try:
        scores = await asyncio.to_thread(
            score_texts, provider, "淘米水的作用", ["淘米水可用于洗脸护肤和浇花"]
        )
        elapsed = time.time() - start
        return TestConnectionResult(
            success=True,
            message=f"Rerank 打分正常（样例相关度 {scores[0]:.3f}）",
            elapsed=round(elapsed, 2),
        )
    except Exception as e:
        return TestConnectionResult(
            success=False,
            message=str(e)[:150],
            elapsed=round(time.time() - start, 2),
        )
