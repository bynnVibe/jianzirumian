"""
见字如面 - 运行时配置管理
持久化到 data/runtime_config.json，覆盖 .env 中的默认值
"""
import json
import logging
from pathlib import Path

from app.config import DATA_DIR

logger = logging.getLogger("jianziruyang.config")


class RuntimeConfig:
    """运行时配置，覆盖 settings 中的默认 provider 选择"""

    def __init__(self):
        self._file = DATA_DIR / "runtime_config.json"
        self._data: dict = {}
        self._load()

    def _load(self):
        if self._file.exists():
            try:
                self._data = json.loads(self._file.read_text(encoding="utf-8"))
                logger.info("加载运行时配置: %s", self._file)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning("运行时配置加载失败: %s", e)
                self._data = {}
        else:
            self._data = {}

    def _save(self):
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._file.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value):
        self._data[key] = value
        self._save()

    @property
    def llm_provider(self):
        return self._data.get("llm_provider")

    @llm_provider.setter
    def llm_provider(self, value: str):
        self.set("llm_provider", value)

    @property
    def ocr_provider(self):
        return self._data.get("ocr_provider")

    @ocr_provider.setter
    def ocr_provider(self, value: str):
        self.set("ocr_provider", value)

    @property
    def search_provider(self):
        return self._data.get("search_provider")

    @search_provider.setter
    def search_provider(self, value: str):
        self.set("search_provider", value)

    @property
    def embedding_provider(self):
        return self._data.get("embedding_provider")

    @embedding_provider.setter
    def embedding_provider(self, value: str):
        self.set("embedding_provider", value)

    @property
    def rerank_provider(self):
        return self._data.get("rerank_provider")

    @rerank_provider.setter
    def rerank_provider(self, value: str):
        self.set("rerank_provider", value)

    # ---- Redis 文件缓存 ----
    @property
    def redis_url(self) -> str:
        """系统设置页保存的 Redis 地址覆盖（空/未保存时回退 .env）"""
        return self._data.get("redis_url") or ""

    @redis_url.setter
    def redis_url(self, value: str):
        self.set("redis_url", (value or "").strip())

    @property
    def file_cache_enabled(self):
        """文件缓存开关（None = 未配置，回退 .env 默认）"""
        return self._data.get("file_cache_enabled")

    @file_cache_enabled.setter
    def file_cache_enabled(self, value: bool):
        self.set("file_cache_enabled", bool(value))

    # ---- llm-wiki 知识编译 ----
    @property
    def wiki_compile_enabled(self):
        """知识编译开关（None = 未配置，回退 .env 默认）"""
        return self._data.get("wiki_compile_enabled")

    @wiki_compile_enabled.setter
    def wiki_compile_enabled(self, value: bool):
        self.set("wiki_compile_enabled", bool(value))

    @property
    def wiki_priority_enabled(self) -> bool:
        """检索是否优先命中百科卡片，默认开启"""
        return self._data.get("wiki_priority_enabled", True)

    @wiki_priority_enabled.setter
    def wiki_priority_enabled(self, value: bool):
        self.set("wiki_priority_enabled", bool(value))

    # ---- 提供商参数覆盖（页面在线配置，覆盖 .env 默认值） ----
    def get_provider_settings(self, category: str, provider: str) -> dict:
        """获取某类别/提供商的参数覆盖（仅页面保存过的字段）"""
        return self._data.get("provider_settings", {}).get(category, {}).get(provider, {})

    def set_provider_settings(self, category: str, provider: str, values: dict):
        """保存参数覆盖（增量更新，不影响未提交的字段）"""
        ps = self._data.setdefault("provider_settings", {})
        ps.setdefault(category, {}).setdefault(provider, {}).update(values)
        self._save()

    @property
    def registration_enabled(self) -> bool:
        """注册功能是否开启，默认为 True（开启）"""
        return self._data.get("registration_enabled", True)

    @registration_enabled.setter
    def registration_enabled(self, value: bool):
        self.set("registration_enabled", value)

    @property
    def guest_query_limit(self) -> int:
        """游客免费访问次数（按 IP 计数），默认 3 次"""
        try:
            return max(0, int(self._data.get("guest_query_limit", 3)))
        except (TypeError, ValueError):
            return 3

    @guest_query_limit.setter
    def guest_query_limit(self, value: int):
        self.set("guest_query_limit", max(0, int(value)))

    # ---- 混合检索参数 ----
    @property
    def retrieval_mode(self) -> str:
        """检索模式：dense（仅稠密） | hybrid（稠密+BM25稀疏 RRF 融合，默认）"""
        value = self._data.get("retrieval_mode", "hybrid")
        return value if value in ("dense", "hybrid") else "hybrid"

    @retrieval_mode.setter
    def retrieval_mode(self, value: str):
        self.set("retrieval_mode", value if value in ("dense", "hybrid") else "hybrid")

    @property
    def dense_top_k(self) -> int:
        try:
            return max(1, int(self._data.get("dense_top_k", 0) or 0)) or None
        except (TypeError, ValueError):
            return None

    @dense_top_k.setter
    def dense_top_k(self, value: int):
        self.set("dense_top_k", max(1, int(value)))

    @property
    def sparse_top_k(self) -> int:
        try:
            return max(1, int(self._data.get("sparse_top_k", 0) or 0)) or None
        except (TypeError, ValueError):
            return None

    @sparse_top_k.setter
    def sparse_top_k(self, value: int):
        self.set("sparse_top_k", max(1, int(value)))

    @property
    def rrf_k(self) -> int:
        try:
            return max(1, int(self._data.get("rrf_k", 60) or 60))
        except (TypeError, ValueError):
            return 60

    @rrf_k.setter
    def rrf_k(self, value: int):
        self.set("rrf_k", max(1, int(value)))

    @property
    def rerank_top_k(self) -> int:
        try:
            return max(1, int(self._data.get("rerank_top_k", 0) or 0)) or None
        except (TypeError, ValueError):
            return None

    @rerank_top_k.setter
    def rerank_top_k(self, value: int):
        self.set("rerank_top_k", max(1, int(value)))


# 全局单例
runtime_config = RuntimeConfig()


def _provider_defaults(category: str, provider: str) -> dict:
    """各类别/提供商的 .env 默认参数"""
    from app.config import settings

    defaults = {
        "llm": {
            "ollama": {"base_url": settings.OLLAMA_BASE_URL, "model": settings.OLLAMA_MODEL},
            "openai": {"base_url": settings.API_BASE_URL, "api_key": settings.API_KEY, "model": settings.API_MODEL},
            "custom": {"base_url": settings.API_BASE_URL, "api_key": settings.API_KEY, "model": settings.API_MODEL},
        },
        "ocr": {
            "local": {},
            "aliyun": {
                "base_url": settings.BAILIAN_OCR_BASE_URL,
                "api_key": settings.BAILIAN_OCR_API_KEY,
                "model": settings.BAILIAN_OCR_MODEL,
            },
            "custom_api": {"api_url": settings.CUSTOM_OCR_API_URL, "api_key": settings.CUSTOM_OCR_API_KEY},
        },
        "search": {
            "duckduckgo": {},
            "bing": {"api_key": settings.BING_API_KEY},
            "serpapi": {"api_key": settings.SERPAPI_API_KEY},
            "anysearch": {"api_key": settings.ANYSEARCH_API_KEY},
        },
        "embedding": {
            "ollama": {"base_url": settings.EMBEDDING_API_BASE, "model": settings.EMBEDDING_MODEL},
            "openai": {
                "base_url": settings.EMBEDDING_API_BASE or settings.API_BASE_URL,
                "api_key": settings.EMBEDDING_API_KEY or settings.API_KEY,
                "model": settings.EMBEDDING_API_MODEL or settings.API_MODEL,
            },
            "custom": {
                "base_url": settings.EMBEDDING_API_BASE or settings.API_BASE_URL,
                "api_key": settings.EMBEDDING_API_KEY or settings.API_KEY,
                "model": settings.EMBEDDING_API_MODEL or settings.API_MODEL,
            },
        },
        "rerank": {
            "local": {"model": settings.RERANKER_MODEL},
            "dashscope": {
                "base_url": settings.DASHSCOPE_RERANK_BASE_URL,
                "api_key": settings.DASHSCOPE_RERANK_API_KEY,
                "model": settings.DASHSCOPE_RERANK_MODEL,
            },
        },
    }
    return dict(defaults.get(category, {}).get(provider, {}))


def effective_provider_config(category: str, provider: str) -> dict:
    """生效配置 = .env 默认值 + 页面保存的运行时覆盖（空值不覆盖）"""
    cfg = _provider_defaults(category, provider)
    for k, v in runtime_config.get_provider_settings(category, provider).items():
        if v not in (None, ""):
            cfg[k] = v
    return cfg

