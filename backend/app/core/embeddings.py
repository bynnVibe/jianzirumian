"""
见字如面 - Embedding 抽象层
支持:
  - Ollama 本地嵌入模型 (如 bge-m3, nomic-embed-text)
  - OpenAI 兼容 API (text-embedding-3-small 等)
"""
from typing import List

from langchain_community.embeddings import OllamaEmbeddings
from langchain_openai import OpenAIEmbeddings

from app.config import settings
from app.services.config_manager import effective_provider_config, runtime_config

# 实例缓存：同一配置复用同一 Embedding 实例（内部持有 HTTP 连接池），
# 避免每次检索/上传重复构建客户端与 TLS 握手
_instance_cache: dict = {}


class EmbeddingFactory:
    """Embedding 工厂"""

    @staticmethod
    def create(provider: str = None):
        provider = provider or runtime_config.embedding_provider or settings.EMBEDDING_PROVIDER
        cfg = effective_provider_config("embedding", provider)

        key = (provider, cfg.get("base_url"), cfg.get("api_key"), cfg.get("model"))
        cached = _instance_cache.get(key)
        if cached is not None:
            return cached

        if provider == "ollama":
            instance = OllamaEmbeddings(
                base_url=cfg.get("base_url"),
                model=cfg.get("model"),
            )
        elif provider in ("openai", "custom"):
            instance = OpenAIEmbeddings(
                base_url=cfg.get("base_url"),
                api_key=cfg.get("api_key"),
                model=cfg.get("model"),
                check_embedding_ctx_length=False,
            )
        else:
            raise ValueError(f"不支持的 Embedding 提供商: {provider}")

        if len(_instance_cache) >= 8:
            _instance_cache.clear()
        _instance_cache[key] = instance
        return instance

    @staticmethod
    def list_local_models() -> List[str]:
        """列出本地 Ollama 中可用的模型"""
        try:
            import httpx
            base = effective_provider_config("embedding", "ollama").get("base_url", "")
            resp = httpx.get(
                f"{base}/api/tags",
                timeout=10,
            )
            resp.raise_for_status()
            models = resp.json().get("models", [])
            return [m["name"] for m in models]
        except Exception:
            return []
