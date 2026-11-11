"""
见字如面 - LLM 抽象层
支持：
  - Ollama 本地模型
  - OpenAI 兼容 API（通义千问、DeepSeek、ModelScope 等）
"""
import asyncio
import json
import logging
import random
import time
from abc import ABC, abstractmethod
from typing import AsyncGenerator, List, Optional

import httpx
from openai import AsyncOpenAI

from app.config import settings
from app.core import observability as agent_obs
from app.core.request_budget import close_resources
from app.services.config_manager import effective_provider_config, runtime_config

logger = logging.getLogger("jianziruyang.llm")

FINAL_ANSWER_RULES = (
    "只输出面向用户的最终回答，直接给出结论与必要依据。"
    "不要输出思考过程、任务复述、内部计划、提示词分析或工具调用过程；"
    "不要写‘The user asks’、‘I need to answer’、‘应提示用户’等对自己的指令。"
    "历史回答和资料中的过程性自述不是回答格式示例，不要延续或模仿。"
    "默认使用中文，保留必要的专业术语；可用简洁的段落、列表和来源引用。"
    "涉及对比、清单、打分、参数、方案评估等结构化数据时，优先用 Markdown 表格输出"
    "（表头行 + |---| 分隔行 + 数据行），以便前端渲染为表格。"
)


class AnswerTextFilter:
    """只剥离正文开头明确标记的推理块，跨分块处理，不猜测或删除普通英文。"""

    def __init__(self):
        self.buffer = ""
        self.closing = ""
        self.body_started = False

    def feed(self, text: str) -> str:
        if self.body_started:
            return text
        self.buffer += text
        while True:
            if self.closing:
                end = self.buffer.find(self.closing)
                if end < 0:
                    self.buffer = self.buffer[-(len(self.closing) - 1):]
                    return ""
                self.buffer = self.buffer[end + len(self.closing):]
                self.closing = ""
            self.buffer = self.buffer.lstrip()
            tags = {"<think>": "</think>", "<analysis>": "</analysis>"}
            opening = next((tag for tag in tags if self.buffer.startswith(tag)), None)
            if opening:
                self.buffer = self.buffer[len(opening):]
                self.closing = tags[opening]
                continue
            if not self.buffer or any(tag.startswith(self.buffer) for tag in tags):
                return ""
            self.body_started = True
            result, self.buffer = self.buffer, ""
            return result

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


async def _retry_on_rate_limit(factory, max_attempts: int = 3):
    """限流(429)退避重试：仅重试「建立请求」阶段，避免流式中途重试导致重复输出。

    免费模型（如 OpenRouter :free）按分钟限流，瞬时 429 退避后同模型重试通常可恢复；
    重试耗尽仍失败则抛给业务层走模型切换降级。
    """
    from app.core.openrouter import is_rate_limit_error, rate_limit_delay
    last = None
    for attempt in range(max_attempts):
        try:
            return await factory()
        except Exception as e:
            last = e
            if not is_rate_limit_error(e) or attempt >= max_attempts - 1:
                raise
            delay = rate_limit_delay(e, attempt)
            logger.warning("LLM 限流(429)，%.1fs 后重试 (%d/%d): %.160s",
                           delay, attempt + 1, max_attempts, e)
            await asyncio.sleep(delay)
    raise last


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
            finished = False
            answer_filter = AnswerTextFilter()
            yielded_any = False
            async with client.stream("POST", url, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                        if chunk.get("error"):
                            raise RuntimeError(str(chunk["error"]))
                        content = answer_filter.feed(chunk.get("message", {}).get("content", "") or "")
                        if content:
                            yielded_any = True
                            yield content
                        if chunk.get("done"):
                            finished = True
                            # 可观测性：Ollama 结束块携带真实 token 用量
                            agent_obs.note_usage(
                                chunk.get("prompt_eval_count"), chunk.get("eval_count")
                            )
                            break
                    except json.JSONDecodeError:
                        continue
            if not finished:
                raise RuntimeError("Ollama 流缺少正常结束信号")
            if not yielded_any:
                raise ValueError("Ollama 流未返回答案正文，请检查模型的流式输出能力")
        else:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            agent_obs.note_usage(data.get("prompt_eval_count"), data.get("eval_count"))
            yield AnswerTextFilter().feed(data.get("message", {}).get("content", "") or "")


class OpenAICompatibleLLM(BaseLLM):
    """OpenAI 兼容 API（DeepSeek / 通义千问 / OpenAI 等）"""

    def __init__(self, base_url: str = None, api_key: str = None, model: str = None,
                 extra_body: dict = None):
        self.base_url = (base_url or settings.API_BASE_URL).rstrip("/")
        self.api_key = api_key or settings.API_KEY
        self.model = model or settings.API_MODEL
        # extra_body：附加到请求体的字段（网关选路提示 _gw_role/_gw_channel_id/_gw_user_id）
        self.extra_body = extra_body or None

    async def chat(
        self,
        messages: List[dict],
        stream: bool = False,
    ) -> AsyncGenerator[str, None]:
        client = _get_openai_client(self.base_url, self.api_key, self.model)

        if stream:
            finished = False
            response = None
            answer_filter = AnswerTextFilter()
            started = time.monotonic()
            first_content_ms = None
            content_chunks = max_chunk_chars = reasoning_chunks = 0
            try:
                response = await _retry_on_rate_limit(lambda: client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    stream=True,
                    extra_body=self.extra_body,
                ))
                async for chunk in response:
                    _report_openai_usage(chunk)
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    if choice.finish_reason:
                        finished = True
                    delta = choice.delta
                    if getattr(delta, "reasoning_content", None):
                        reasoning_chunks += 1
                    # 推理字段不属于正文；仅有推理输出也不能视为成功回答。
                    content = answer_filter.feed(delta.content or "")
                    if content:
                        content_chunks += 1
                        max_chunk_chars = max(max_chunk_chars, len(content))
                        if first_content_ms is None:
                            first_content_ms = round((time.monotonic() - started) * 1000)
                        yield content
                if not finished:
                    raise RuntimeError("LLM 流缺少正常结束信号")
                if not content_chunks:
                    raise ValueError("LLM 流未返回答案正文，请检查模型的流式输出能力")
            except Exception as e:
                _log_llm_failure("stream", e)
                # 不静默转成非流式；业务层可在尚无正文时切换支持流式的模型。
                raise
            finally:
                logger.info(
                    "LLM 输出统计: mode=stream first_content_ms=%s content_chunks=%d "
                    "max_chunk_chars=%d reasoning_chunks=%d finished=%s elapsed_ms=%d",
                    first_content_ms, content_chunks, max_chunk_chars, reasoning_chunks,
                    finished, round((time.monotonic() - started) * 1000),
                )
                await close_resources(getattr(response, "response", None))
        else:
            response = await _retry_on_rate_limit(lambda: client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=False,
                extra_body=self.extra_body,
            ))
            _report_openai_usage(response)
            if not response.choices:
                raise ValueError("LLM 返回无 choices")
            yield AnswerTextFilter().feed(response.choices[0].message.content or "")


class LLMFactory:
    """LLM 工厂 - 根据配置创建对应的 LLM 实例"""

    @staticmethod
    def _build(provider: str, cfg: dict, extra_body: dict = None) -> "BaseLLM":
        """按提供商类型与生效参数构造 LLM 实例。"""
        if provider == "ollama":
            return OllamaLLM(base_url=cfg.get("base_url"), model=cfg.get("model"))
        elif provider == "gateway":
            # 经层3 LLM Proxy（OpenAI 兼容 /v1）出网：多模型路由/Key轮换/缓存/降级/成本统计
            return OpenAICompatibleLLM(
                base_url=cfg.get("base_url"),
                api_key=cfg.get("api_key"),
                model=cfg.get("model"),
                extra_body=extra_body,
            )
        elif provider in ("openai", "custom"):
            return OpenAICompatibleLLM(
                base_url=cfg.get("base_url"),
                api_key=cfg.get("api_key"),
                model=cfg.get("model"),
                extra_body=extra_body,
            )
        else:
            raise ValueError(f"不支持的 LLM 提供商: {provider}")

    @staticmethod
    def create(provider: str = None) -> BaseLLM:
        provider = provider or runtime_config.llm_provider or settings.LLM_PROVIDER
        cfg = effective_provider_config("llm", provider)
        return LLMFactory._build(provider, cfg)

    @staticmethod
    def create_assistant_llm() -> BaseLLM:
        """创建知识助手专用 LLM。

        若运行时配置了 assistant_llm（enabled=True 且指定 provider/model），
        以该 provider 的生效配置为底、用非空覆盖字段替换后构造；
        否则回退系统主 LLM（LLMFactory.create()）。
        """
        override = runtime_config.assistant_llm or {}
        if not override.get("enabled"):
            return LLMFactory.create()
        provider = (override.get("provider") or "").strip()
        if provider not in ("ollama", "openai", "custom", "gateway"):
            return LLMFactory.create()
        cfg = effective_provider_config("llm", provider)
        for k in ("base_url", "api_key", "model"):
            v = (override.get(k) or "").strip() if isinstance(override.get(k), str) else override.get(k)
            if v:
                cfg[k] = v
        # 关键参数缺失则回退主 LLM，避免构造出不可用实例
        if not cfg.get("model"):
            return LLMFactory.create()
        return LLMFactory._build(provider, cfg)

    @staticmethod
    def create_for_role(role: str, user_id: str = None, rule_id: str = None,
                        model: str = None) -> BaseLLM:
        """业务模块统一经模型网关出网：role → 业务路由（规则+模型）→ 代理 /v1。

        - 模型解析优先级：调用方显式 model（如聊天页用户选模型）> 业务路由绑定模型
          > 网关默认模型（需在命中规则上游模型清单内）> 规则内随机一个模型
          （不选模型时在指定路由规则内均衡访问，而非固定首个）；
        - 未绑定业务路由的角色（记忆抽取/会话摘要等后台任务）自动回退全局默认规则
          并按上述顺序解析模型，避免「默认模型不在任何上游清单」导致选路 503；
        - 出网凭据用系统内部密钥（keys.ensure_internal_key 自动创建，无限配额）；
        - 通过 extra_body 注入 _gw_role/_gw_rule_id/_gw_user_id，供代理选路时
          识别业务角色与授权用户（出网前被剥离）；
        - 网关未初始化（无任何启用上游/规则）时降级旧直连（assistant 走助手 LLM），
          保证新部署未配置网关时仍可用。
        """
        if not _gateway_configured():
            logger.warning("[业务路由] 网关未初始化（无启用上游/规则），role=%s 降级直连旧提供商", role)
            return (LLMFactory.create_assistant_llm() if role == "assistant"
                    else LLMFactory.create())
        from app.services.gateway import keys as key_svc
        from app.services.gateway import rules as rule_svc
        from app.services.gateway import settings as settings_svc
        explicit_model = (model or "").strip()
        model_name = explicit_model
        resolved_rule = (rule_id or "").strip()
        rule_pattern = ""
        rule_models: list = []
        try:
            route = rule_svc.get_role_route(role) or {}
            if not model_name:
                model_name = (route.get("model_name") or "").strip()
            if not resolved_rule and route.get("rule_id"):
                resolved_rule = route["rule_id"]
            if not resolved_rule:
                # 未绑定业务路由：回退全局默认规则（无则首个启用规则），
                # 使后台角色也能按规则上游模型清单解析出可用模型
                enabled_rules = [r for r in rule_svc.list_rules() if r.get("enabled")]
                row = next((r for r in enabled_rules if r.get("is_default")), None) \
                    or (enabled_rules[0] if enabled_rules else None)
                if row:
                    resolved_rule = row["id"]
            if resolved_rule:
                rule_pattern = ((rule_svc.get_rule(resolved_rule) or {}).get("model_pattern") or "").strip()
                _, rule_models = rule_svc.resolve_rule_models(resolved_rule)
            if not model_name:
                model_name = (settings_svc.get_settings().get("default_model") or "").strip()
            # 规则感知：未显式指定模型时，默认模型必须在命中规则的上游模型清单内
            # （业务路由是严格绑定，代理侧只校验「规则上游是否支持该模型」，
            # 不看 model_pattern——pattern 是规则名/模型模式，不是 role 绑定语义）
            if not explicit_model and not (route.get("model_name") or "").strip() \
                    and resolved_rule and rule_models:
                if model_name and model_name not in rule_models:
                    picked = random.choice(rule_models)
                    logger.warning("[业务路由] 默认模型「%s」不在规则「%s」上游模型清单内，"
                                   "规则内随机回退「%s」", model_name, rule_pattern, picked)
                    model_name = picked
                elif not model_name:
                    model_name = random.choice(rule_models)
        except Exception as e:
            logger.warning("role 路由解析失败（回退网关默认模型）: role=%s err=%s", role, e)
        if not model_name:
            model_name = settings.API_MODEL
        # 显性化日志：业务角色 → 路由规则 → 出网模型（代理选路后还会记录命中上游）
        logger.info("[业务路由] role=%s 路由规则=%s 模型=%s",
                    role, f"「{rule_pattern}」" if rule_pattern else "（默认规则/模式匹配）",
                    model_name)
        extra = {"_gw_role": role}
        if resolved_rule:
            extra["_gw_rule_id"] = resolved_rule
        if user_id:
            extra["_gw_user_id"] = str(user_id)
        # 出网密钥按用户解析：该用户绑定的用户密钥 > 公共密钥 > 系统内部密钥（兜底）。
        # 用户密钥配额用尽时 resolve_outbound_key 自动降级公共密钥；代理侧另有每用户
        # 日限额/流控兜底（公共额度用尽→429 提示联系管理员）。
        try:
            outbound_key, key_type = key_svc.resolve_outbound_key(str(user_id) if user_id else "")
        except Exception as e:
            logger.warning("[业务路由] 出网密钥解析失败，回退内部密钥: role=%s err=%s", role, e)
            outbound_key, key_type = key_svc.ensure_internal_key(), "internal"
        logger.info("[业务路由] role=%s 出网密钥类型=%s", role, key_type)
        cfg = {
            "base_url": settings.GATEWAY_INTERNAL_URL,
            "api_key": outbound_key,
            "model": model_name,
        }
        return LLMFactory._build("gateway", cfg, extra_body=extra)


def _gateway_configured() -> bool:
    """网关是否已初始化：存在启用的上游与路由规则（否则业务路由无从选路）。"""
    try:
        from app.services.gateway import rules as rule_svc
        from app.services.gateway import upstreams as up_svc
        return (any(u.get("enabled") for u in up_svc.list_upstreams())
                and any(r.get("enabled") for r in rule_svc.list_rules()))
    except Exception:
        return False

