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


def resolve_embedding_upstream_config() -> dict:
    """解析用于「直连探测」的 embedding 配置。

    优先网关 embedding 上游（与网关「连通测试」同源：list_upstreams 返回的是脱敏 dict，
    须按 id 重取 with_secret=True 才能拿到真实 api_key），回退 provider 生效配置。
    启动期探测不能经 GATEWAY_INTERNAL_URL（同进程 /v1）自调用——lifespan 未结束前事件循环
    不受理入站请求，自调用会挂到超时并误报「请求超时」。
    返回 {source, protocol, base_url, api_key, model}。
    """
    try:
        from app.services.gateway import upstreams as up_svc
        ups = [u for u in up_svc.list_upstreams(category="embedding") if u.get("enabled")]
        if ups:
            # list_upstreams 返回脱敏 dict（无真实 api_key），须按 id 重取 with_secret
            up = up_svc.get_upstream(ups[0]["id"], with_secret=True) or ups[0]
            eff = up_svc.effective(up)
            models = up.get("models") or []
            return {
                "source": "gateway",
                "protocol": eff.get("protocol") or "openai",
                "base_url": (eff.get("base_url") or "").rstrip("/"),
                "api_key": eff.get("api_key") or "",
                "model": (models[0] if models else ""),
            }
    except Exception as e:  # 网关不可用时回退直连 provider 配置
        logger.debug("解析网关 embedding 上游失败，回退 provider 配置: %s", e)
    provider = runtime_config.embedding_provider or settings.EMBEDDING_PROVIDER
    cfg = effective_provider_config("embedding", provider)
    return {
        "source": "provider",
        "protocol": "openai",
        "base_url": (cfg.get("base_url") or "").rstrip("/"),
        "api_key": cfg.get("api_key") or "",
        "model": cfg.get("model") or "",
    }


def probe_upstream_direct(timeout: float = None) -> dict:
    """直连上游 /embeddings 发最小请求（启动期连通检测用，不经同进程网关）。

    与网关「连通测试」同路径：gateway 源复用 build_upstream_url + transport.build_request
    按上游协议构造 URL 与鉴权头（避免脱敏密钥/协议不匹配导致 401）；provider 源按 OpenAI
    兼容协议。返回 {ok, dimension, status_code, message, elapsed}；不抛异常。
    """
    import httpx
    cfg = resolve_embedding_upstream_config()
    if not cfg["base_url"] or not cfg["model"]:
        return {"ok": False, "dimension": None, "status_code": None, "elapsed": 0,
                "message": "未配置 Embedding 上游（缺少 Base URL 或模型），请在模型网关/系统设置中补全"}
    body = {"model": cfg["model"], "input": ["ping"]}
    if cfg["source"] == "gateway":
        from app.services.gateway import upstreams as up_svc
        from app.services.gateway import upstream as transport
        eff = {"protocol": cfg["protocol"], "base_url": cfg["base_url"],
               "api_key": cfg["api_key"], "name": ""}
        url = up_svc.build_upstream_url(eff, "embeddings")
        headers, payload = transport.build_request(eff, body, cfg["model"], stream=False, kind="embeddings")
    else:
        url = cfg["base_url"] + ("" if cfg["base_url"].endswith("/embeddings") else "/embeddings")
        headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}
        payload = body
    started = time.monotonic()
    try:
        with httpx.Client(timeout=timeout or min(settings.EMBEDDING_REQUEST_TIMEOUT, 10)) as client:
            resp = client.post(url, json=payload, headers=headers)
        elapsed = round(time.monotonic() - started, 2)
        if resp.status_code < 400:
            dimension = None
            try:
                data = resp.json()
                # openai 兼容: {"data":[{"embedding":[...]}]}；ollama: {"embeddings":[[...]]}
                if isinstance(data.get("data"), list) and data["data"]:
                    dimension = len(data["data"][0]["embedding"])
                elif isinstance(data.get("embeddings"), list) and data["embeddings"]:
                    dimension = len(data["embeddings"][0])
            except Exception:
                dimension = None
            msg = f"{cfg['model']} 连接成功" + (f"，返回 {dimension} 维向量" if dimension else "")
            return {"ok": True, "dimension": dimension, "status_code": resp.status_code,
                    "message": msg, "elapsed": elapsed}
        sc = resp.status_code
        if sc in (401, 403):
            msg = f"Embedding 鉴权/访问被拒绝（{sc}），请检查 API Key 及模型权限"
        elif sc == 404:
            msg = "Embedding 接口或模型配置不正确（404），请检查地址与模型名称"
        elif sc == 429:
            msg = "Embedding 请求限流（429），请稍后重试或检查配额"
        else:
            msg = f"Embedding 请求失败（HTTP {sc}），请检查服务地址、网络及模型配置"
        return {"ok": False, "dimension": None, "status_code": sc, "message": msg, "elapsed": elapsed}
    except Exception as e:
        return {"ok": False, "dimension": None, "status_code": None,
                "message": embedding_error_message(e), "elapsed": round(time.monotonic() - started, 2)}


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
                self._client = EmbeddingFactory.create_for_role(
                    "embed", fallback_provider=self.provider, fallback_config=self.config)
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
        """构建 Embedding 客户端。

        - 显式传入 config：按该配置直连（探测/旧调用兼容）；
        - 未传 config：优先经模型网关（role=embed），网关未就绪时回退直连当前 provider。
        """
        if config is not None:
            return EmbeddingFactory._create_direct(provider, config)
        provider = provider or runtime_config.embedding_provider or settings.EMBEDDING_PROVIDER
        gw = EmbeddingFactory._gateway_embeddings_client("embed")
        if gw is not None:
            return gw
        return EmbeddingFactory._create_direct(provider, None)

    @staticmethod
    def create_for_role(role: str = "embed", fallback_provider: str = None,
                        fallback_config: dict = None):
        """业务经模型网关出网 embedding（role=embed）：解析模型 → 网关 /v1/embeddings；

        网关未就绪（无 embedding 上游/无可用规则）时回退直连 fallback provider/config。
        """
        gw = EmbeddingFactory._gateway_embeddings_client(role)
        if gw is not None:
            return gw
        # 回退直连：走公开 create（config 非空时不再重复网关判定），保留缓存与可测试性
        return EmbeddingFactory.create(fallback_provider, config=fallback_config)

    @staticmethod
    def _gateway_embeddings_client(role: str = "embed"):
        """构建经网关的 OpenAIEmbeddings（base_url=网关内部地址，api_key=内部密钥）。

        仅当存在启用的 embedding 上游、可解析出模型且选路有候选时返回实例，否则 None。
        """
        try:
            from app.services.gateway import keys as key_svc
            from app.services.gateway import rules as rule_svc
            from app.services.gateway import upstreams as up_svc
            ups = [u for u in up_svc.list_upstreams(category="embedding") if u.get("enabled")]
            if not ups:
                return None
            # 解析出网模型：业务路由绑定模型 > embedding 上游模型清单首个
            model = ((rule_svc.get_role_route(role) or {}).get("model_name") or "").strip()
            if not model:
                names = up_svc.all_model_names(category="embedding")
                model = names[0] if names else ""
            if not model:
                return None
            # 校验选路可用（role→category 过滤 + 规则上游支持该模型）
            _, candidates = rule_svc.select_upstreams(model, role=role)
            if not candidates:
                return None
            key = ("gateway", settings.GATEWAY_INTERNAL_URL, model, role,
                   settings.EMBEDDING_REQUEST_TIMEOUT)
            cached = _instance_cache.get(key)
            if cached is not None:
                return cached
            instance = OpenAIEmbeddings(
                base_url=settings.GATEWAY_INTERNAL_URL,
                api_key=key_svc.ensure_internal_key(),
                model=model,
                check_embedding_ctx_length=False,
                request_timeout=settings.EMBEDDING_REQUEST_TIMEOUT,
                max_retries=0,
                chunk_size=10,
            )
            if len(_instance_cache) >= 8:
                _instance_cache.clear()
            _instance_cache[key] = instance
            logger.info("[业务路由] embedding 经网关出网: role=%s model=%s", role, model)
            return instance
        except Exception as e:
            logger.warning("[业务路由] embedding 网关客户端构建失败，回退直连: role=%s err=%s", role, e)
            return None

    @staticmethod
    def _create_direct(provider: str = None, config: dict = None):
        """直连构建（不经网关）：按 provider 生效配置构造并缓存实例。"""
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
