"""
见字如面 - Rerank 重排序服务
对检索结果按与查询的相关度从高到低重排序。
支持两种提供商（系统设置页可在线切换与配置参数）:
  - local: 本地交叉编码器 (sentence-transformers CrossEncoder)
  - dashscope: 阿里云 DashScope OpenAI 兼容 /reranks 接口 (qwen3-rerank 等)
"""
import logging
import math
from typing import List, Optional, Tuple

import httpx

from app.config import settings
from app.services.config_manager import effective_provider_config, runtime_config

logger = logging.getLogger("jianziruyang.reranker")

_RERANKER_AVAILABLE = False
_reranker_model = None
_reranker_model_name = None

# DashScope 共享同步客户端：复用连接池，避免每次 rerank 重复 TCP+TLS 握手
_http_client: Optional[httpx.Client] = None


def _get_http_client() -> httpx.Client:
    global _http_client
    if _http_client is None:
        _http_client = httpx.Client(
            timeout=30,
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )
    return _http_client

# 尝试加载 CrossEncoder 模型
try:
    from sentence_transformers import CrossEncoder

    _RERANKER_AVAILABLE = True
except ImportError:
    logger.warning("sentence-transformers 未安装，本地 rerank 不可用。执行: pip install sentence-transformers")


def current_provider() -> str:
    """当前生效的 rerank 提供商（运行时配置 > .env 默认）"""
    return runtime_config.rerank_provider or settings.RERANKER_PROVIDER


def _get_model():
    """懒加载本地 reranker 模型（模型名变更后自动重载）"""
    global _reranker_model, _reranker_model_name
    if not _RERANKER_AVAILABLE:
        return None

    model_name = effective_provider_config("rerank", "local").get("model") or settings.RERANKER_MODEL
    if _reranker_model is not None and _reranker_model_name == model_name:
        return _reranker_model

    try:
        logger.info("加载 reranker 模型: %s", model_name)
        _reranker_model = CrossEncoder(model_name)
        _reranker_model_name = model_name
        logger.info("Reranker 模型加载成功")
        return _reranker_model
    except Exception as e:
        logger.error("加载 reranker 模型失败: %s", e)
        _reranker_model = None
        return None


def _score_local(query: str, texts: List[str]) -> List[float]:
    """本地交叉编码器打分（原始 logit，越大越相关），失败抛异常"""
    if not _RERANKER_AVAILABLE:
        raise RuntimeError("sentence-transformers 未安装，无法使用本地 rerank")
    model = _get_model()
    if model is None:
        raise RuntimeError("本地 reranker 模型加载失败，请检查模型名称")
    scores = model.predict([[query, t] for t in texts])
    return [float(s) for s in scores]


def _score_dashscope(query: str, texts: List[str]) -> List[float]:
    """调用阿里云 DashScope OpenAI 兼容 /reranks 接口打分（相关度 0~1），失败抛异常"""
    cfg = effective_provider_config("rerank", "dashscope")
    api_key = cfg.get("api_key", "")
    if not api_key:
        raise RuntimeError("DashScope rerank 未配置 API Key")

    base_url = (cfg.get("base_url") or settings.DASHSCOPE_RERANK_BASE_URL).rstrip("/")
    if not base_url:
        raise RuntimeError("DashScope rerank 未配置 base_url")
    # base_url 为 OpenAI 兼容地址（.../compatible-api/v1），rerank 路径为 /reranks
    url = base_url if base_url.endswith("/reranks") else f"{base_url}/reranks"

    payload = {
        "model": cfg.get("model") or settings.DASHSCOPE_RERANK_MODEL,
        "query": query,
        "documents": texts,
        "top_n": len(texts),
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    resp = _get_http_client().post(url, json=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"DashScope rerank HTTP {resp.status_code}: {resp.text[:150]}")
    data = resp.json()
    # 兼容两种返回结构：顶层 results（OpenAI 兼容）/ output.results（原生 API）
    items = data.get("results") or data.get("output", {}).get("results", [])
    if not items:
        raise RuntimeError(f"DashScope rerank 返回异常: {str(data)[:150]}")

    # 按 index 回填分数（接口按相关度降序返回，且可能只返回 top_n 条）
    items = data.get("results") or data.get("output", {}).get("results", [])
    if not items:
        raise RuntimeError(f"DashScope rerank 返回异常: {str(data)[:150]}")

    # 兼容文本为空的条目：服务可能跳过后返回数量不一致，按实际条目文本数回填
    scored_count = sum(1 for t in texts if isinstance(t, str) and t.strip())
    scores = [0.0] * max(len(texts), 1)
    for item in items:
        idx = item.get("index")
        if isinstance(idx, int) and 0 <= idx < len(texts):
            scores[idx] = float(item.get("relevance_score", 0.0))
    if scored_count == 0:
        raise RuntimeError("DashScope rerank 无有效文本可打分")
    return scores


def score_texts(provider: str, query: str, texts: List[str]) -> List[float]:
    """按指定提供商对 (query, texts) 打分，供 rerank 与连接测试使用，失败抛异常"""
    if provider == "dashscope":
        return _score_dashscope(query, texts)
    elif provider == "local":
        return _score_local(query, texts)
    raise ValueError(f"不支持的 rerank 提供商: {provider}")


def rerank(
    query: str,
    documents: List[Tuple[str, dict, float]],
    top_k: Optional[int] = None,
) -> List[Tuple[str, dict, float]]:
    """对检索结果进行重排序

    Args:
        query: 用户查询
        documents: [(text, metadata, original_score), ...] 原始检索结果
        top_k: 返回前 k 条（默认全部）

    Returns:
        重排序后的 [(text, metadata, rerank_score), ...]
    """
    if not documents:
        return []

    top_k = top_k or len(documents)
    provider = current_provider()

    try:
        texts = [doc[0] for doc in documents]
        scores = score_texts(provider, query, texts)

        # 合并分数并排序
        scored = [(documents[i][0], documents[i][1], scores[i]) for i in range(len(documents))]
        scored.sort(key=lambda x: x[2], reverse=True)

        logger.info("Rerank (%s) 完成: %d 条 → 重新排序", provider, len(scored))
        return scored[:top_k]

    except Exception as e:
        # 回退：用原始分数排序（高分在前）
        logger.warning("Rerank (%s) 不可用: %s，使用原始相似度分数排序", provider, e)
        sorted_docs = sorted(documents, key=lambda x: x[2], reverse=True)
        return sorted_docs[:top_k]


def normalize_score(score: float) -> int:
    """将分数映射到 0-100 的百分比

    - 0~1 区间分数（DashScope 相关度 / FAISS 相似度）直接按百分比
    - 其他（本地 cross-encoder 原始 logit）用 sigmoid 归一化
    """
    try:
        if 0.0 <= score <= 1.0:
            return round(score * 100)
        normalized = 1.0 / (1.0 + math.exp(-score))
        return min(100, max(0, round(normalized * 100)))
    except (OverflowError, ValueError, TypeError):
        return 50
