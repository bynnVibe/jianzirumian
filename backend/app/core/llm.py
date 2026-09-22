"""
见字如面 - LLM 抽象层
支持：
  - Ollama 本地模型
  - OpenAI 兼容 API（通义千问、DeepSeek、ModelScope 等）
"""
import json
import logging
from abc import ABC, abstractmethod
from typing import AsyncGenerator, List, Optional

import httpx
from openai import AsyncOpenAI

from app.config import settings
from app.core import observability as agent_obs
from app.services.config_manager import effective_provider_config, runtime_config

logger = logging.getLogger("jianziruyang.llm")

# 流式生成可能持续较久：连接超时短、读超时放宽
_LLM_TIMEOUT = httpx.Timeout(connect=10, read=300, write=30, pool=10)

# Ollama 共享客户端：复用连接池，避免每次请求重复 TCP 握手
_OLLAMA_CLIENT: Optional[httpx.AsyncClient] = None


def _get_ollama_client() -> httpx.AsyncClient:
    global _OLLAMA_CLIENT
    if _OLLAMA_CLIENT is None:
        _OLLAMA_CLIENT = httpx.AsyncClient(
            timeout=_LLM_TIMEOUT,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
    return _OLLAMA_CLIENT


# OpenAI 兼容客户端缓存：按 (base_url, api_key, model) 复用 AsyncOpenAI 实例
# （其内部持有 httpx 连接池），避免每次对话重建客户端与 TLS 握手
_OPENAI_CLIENT_CACHE: dict = {}


def _get_openai_client(base_url: str, api_key: str, model: str) -> AsyncOpenAI:
    key = (base_url, api_key, model)
    client = _OPENAI_CLIENT_CACHE.get(key)
    if client is None:
        if len(_OPENAI_CLIENT_CACHE) >= 16:
            _OPENAI_CLIENT_CACHE.clear()
        # max_retries=1：限流场景下 SDK 内置重试只会放大请求量，
        # 业务层已有降级（非流式兜底 / OpenRouter 免费模型切换）
        client = AsyncOpenAI(
            base_url=base_url, api_key=api_key, timeout=_LLM_TIMEOUT, max_retries=1,
        )
        _OPENAI_CLIENT_CACHE[key] = client
    return client


def _report_openai_usage(response) -> None:
    """可观测性：非流式响应携带 usage 时上报真实 token 用量（流式接口无 usage 由业务层估算）。"""
    usage = getattr(response, "usage", None)
    if usage is not None:
        agent_obs.note_usage(
            getattr(usage, "prompt_tokens", 0), getattr(usage, "completion_tokens", 0)
        )


def _log_llm_failure(stage: str, e: Exception):
    """LLM 调用失败分级日志：限流/模型不可用属预期降级（WARNING），其余 ERROR"""
    msg = str(e)
    low = msg.lower()
    if "429" in msg or "rate limit" in low or "rate-limited" in low or "no provider supported" in low:
        logger.warning("LLM %s失败（限流或模型不可用，走降级处理）: %.200s", stage, msg)
    else:
        logger.error("LLM %s失败: %s", stage, msg)


class BaseLLM(ABC):
    """LLM 基类"""

    @abstractmethod
    async def chat(
        self,
        messages: List[dict],
        stream: bool = False,
    ) -> AsyncGenerator[str, None]:
        """聊天接口，支持流式输出"""
        ...


class OllamaLLM(BaseLLM):
    """Ollama 本地模型"""

    def __init__(self, base_url: str = None, model: str = None):
        self.base_url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/")
        self.model = model or settings.OLLAMA_MODEL

    async def chat(
        self,
        messages: List[dict],
        stream: bool = False,
    ) -> AsyncGenerator[str, None]:
        url = f"{self.base_url}/api/chat"
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
        }

        client = _get_ollama_client()
        if stream:
            async with client.stream("POST", url, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                        if content := chunk.get("message", {}).get("content", ""):
                            yield content
                        if chunk.get("done"):
                            # 可观测性：Ollama 结束块携带真实 token 用量
                            agent_obs.note_usage(
                                chunk.get("prompt_eval_count"), chunk.get("eval_count")
                            )
                            break
                    except json.JSONDecodeError:
                        continue
        else:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            agent_obs.note_usage(data.get("prompt_eval_count"), data.get("eval_count"))
            yield data.get("message", {}).get("content", "")


class OpenAICompatibleLLM(BaseLLM):
    """OpenAI 兼容 API（DeepSeek / 通义千问 / OpenAI 等）"""

    def __init__(self, base_url: str = None, api_key: str = None, model: str = None):
        self.base_url = (base_url or settings.API_BASE_URL).rstrip("/")
        self.api_key = api_key or settings.API_KEY
        self.model = model or settings.API_MODEL

    async def chat(
        self,
        messages: List[dict],
        stream: bool = False,
    ) -> AsyncGenerator[str, None]:
        client = _get_openai_client(self.base_url, self.api_key, self.model)

        if stream:
            # 先尝试非流式作为保底方案
            try:
                response = await client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    stream=True,
                )
                yielded_any = False
                async for chunk in response:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    # DeepSeek 模型将部分内容放在 reasoning_content
                    # （思考过程），需要同时检查 reasoning_content
                    content = delta.content
                    if not content:
                        content = getattr(delta, 'reasoning_content', None)
                    if content:
                        yielded_any = True
                        yield content

                # 流式未返回任何内容 → 降级为非流式重试
                if not yielded_any:
                    logger.warning("LLM 流式返回为空，降级为非流式重试")
                    response2 = await client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        stream=False,
                    )
                    # ModelScope 接口可能返回 choices 为 null
                    if response2.choices and len(response2.choices) > 0:
                        text = response2.choices[0].message.content or ""
                        _report_openai_usage(response2)
                        if text:
                            yield text
                        else:
                            raise ValueError("LLM 非流式返回内容为空")
                    else:
                        raise ValueError(f"LLM 非流式返回无 choices: {response2}")

            except Exception as e:
                _log_llm_failure("stream", e)
                # 异常时也降级为非流式
                try:
                    response2 = await client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        stream=False,
                    )
                    if response2.choices and len(response2.choices) > 0:
                        text = response2.choices[0].message.content or ""
                        _report_openai_usage(response2)
                        if text:
                            yield text
                        else:
                            raise ValueError("LLM 异常降级返回内容为空")
                    else:
                        raise ValueError(f"LLM 异常降级返回无 choices: {response2}")
                except Exception as e2:
                    _log_llm_failure("非流式降级", e2)
                    raise
        else:
            response = await client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=False,
            )
            _report_openai_usage(response)
            yield response.choices[0].message.content or ""


class LLMFactory:
    """LLM 工厂 - 根据配置创建对应的 LLM 实例"""

    @staticmethod
    def create(provider: str = None) -> BaseLLM:
        provider = provider or runtime_config.llm_provider or settings.LLM_PROVIDER

        if provider == "ollama":
            cfg = effective_provider_config("llm", "ollama")
            return OllamaLLM(base_url=cfg.get("base_url"), model=cfg.get("model"))
        elif provider in ("openai", "custom"):
            cfg = effective_provider_config("llm", provider)
            return OpenAICompatibleLLM(
                base_url=cfg.get("base_url"),
                api_key=cfg.get("api_key"),
                model=cfg.get("model"),
            )
        else:
            raise ValueError(f"不支持的 LLM 提供商: {provider}")
