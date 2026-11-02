"""
见字如面 - Embedding 抽象层
支持:
  - Ollama 本地嵌入模型 (如 bge-m3, nomic-embed-text)
  - OpenAI 兼容 API (text-embedding-3-small 等)
"""
import logging
import math
import time
from datetime import datetime, timezone
from numbers import Real
from typing import List
from urllib.parse import urlsplit, urlunsplit

from langchain_core.embeddings import Embeddings
from langchain_community.embeddings import OllamaEmbeddings
from langchain_openai import OpenAIEmbeddings

from app.config import settings
from app.services.config_manager import effective_provider_config, runtime_config

# 实例缓存：同一配置复用同一 Embedding 实例（内部持有 HTTP 连接池），
# 避免每次检索/上传重复构建客户端与 TLS 握手
_instance_cache: dict = {}
logger = logging.getLogger(__name__)


def embedding_error_message(error: Exception) -> str:
    """只返回诊断类别，不透出上游响应中的密钥、请求内容或带凭据的 URL。"""
    if isinstance(error, EmbeddingValidationError):
        return str(error)
    status = getattr(error, "status_code", None)
    text = str(error).lower()
    if any(word in text for word in ("insufficient_quota", "quota exhausted", "free quota", "arrearage")):
        return "Embedding 额度不足或账户欠费，请检查阿里云模型额度与计费状态"
    if status == 401:
        return "Embedding 鉴权失败（401），请检查 API Key 及其所属地域/工作空间"
    if status == 403:
        return "Embedding 访问被拒绝（403），请检查模型权限、工作空间及额度"
    if status == 429:
        return "Embedding 请求限流（429），请稍后重试或检查配额"
    if status in (400, 404):
        return f"Embedding 接口或模型配置不正确（{status}），请检查地址与模型名称"
    if "timeout" in type(error).__name__.lower() or "timed out" in text:
        return "Embedding 请求超时，请检查远程服务及网络连接"
    return f"Embedding 请求失败（{type(error).__name__}），请检查服务地址、网络及模型配置"


class EmbeddingValidationError(ValueError):
    """仅用于本地生成、可安全展示的向量校验错误。"""


class RuntimeEmbeddings(Embeddings):
    """业务与探测共用的客户端，记录真实请求结果；配置重载使用新的状态实例。"""

    def __init__(self, provider: str, config: dict, index_dimension):
        self.provider = provider
        self.config = dict(config)
        self.index_dimension = index_dimension
        self._client = None
        self._status = {
            "status": "unknown", "available": None, "connected": None,
            "dimension": None, "index_compatible": None, "checked_at": None,
            "message": "待验证：尚未向当前模型发送请求", "elapsed": 0,
        }

    def status(self) -> dict:
        url = urlsplit(self.config.get("base_url") or "")
        endpoint = urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1], url.path, "", ""))
        return {
            **self._status, "provider": self.provider,
            "model": self.config.get("model"), "base_url": endpoint,
            "mode": "local" if self.provider == "ollama" else "remote",
        }

    def _embed(self, texts: list[str], operation: str) -> list[list[float]]:
        started = time.monotonic()
        connected, dimension, compatible = False, None, None
        state, message = "error", "Embedding 请求已中断"
        try:
            expected = self.index_dimension()
            if self._client is None:
                self._client = EmbeddingFactory.create(self.provider, config=self.config)
            vectors = (
                [self._client.embed_query(texts[0])] if operation == "query"
                else self._client.embed_documents(texts)
            )
            if len(vectors) != len(texts) or not vectors:
                raise EmbeddingValidationError("Embedding 返回的向量数量与输入不符")
            dimension = len(vectors[0])
            for vector in vectors:
                if (not dimension or len(vector) != dimension
                        or any(not isinstance(v, Real) or not math.isfinite(v) for v in vector)
                        or not any(v != 0 for v in vector)):
                    raise EmbeddingValidationError("Embedding 返回空向量、非有限数值或无效维度")
            connected = True
            compatible = dimension == expected if expected is not None else None
            if compatible is False:
                raise EmbeddingValidationError(
                    f"模型连接成功，但向量为 {dimension} 维、已有索引为 {expected} 维；"
                    "请恢复原模型或另行重建索引，现有数据未修改"
                )
            message = f"{self.config.get('model')} 连接成功，返回 {dimension} 维向量"
            message += "；与现有索引维度一致" if compatible else "；尚无索引"
            state = "ok"
        except Exception as error:
            message = embedding_error_message(error)
            state = "error"
            raise RuntimeError(message) from None
        finally:
            self._status = {
                "status": state, "available": state == "ok", "connected": connected,
                "dimension": dimension, "index_compatible": compatible,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "message": message, "elapsed": round(time.monotonic() - started, 2),
            }
            logger.info(
                "Embedding 实际请求: provider=%s model=%s operation=%s status=%s dimension=%s elapsed=%ss",
                self.provider, self.config.get("model"), operation, state, dimension, self._status["elapsed"],
            )
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "query")[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "documents") if texts else []


class EmbeddingFactory:
    """Embedding 工厂"""

    @staticmethod
    def create(provider: str = None, config: dict = None):
        provider = provider or runtime_config.embedding_provider or settings.EMBEDDING_PROVIDER
        cfg = config if config is not None else effective_provider_config("embedding", provider)

        key = (provider, cfg.get("base_url"), cfg.get("api_key"), cfg.get("model"), settings.EMBEDDING_REQUEST_TIMEOUT)
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
                request_timeout=settings.EMBEDDING_REQUEST_TIMEOUT,
                max_retries=0,
                chunk_size=10,
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
