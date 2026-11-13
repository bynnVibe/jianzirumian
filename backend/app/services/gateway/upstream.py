"""
见字如面 - 模型网关上游协议适配层

把不同供应商协议统一转换成 OpenAI 兼容协议（下游只看到一个 OpenAI 形态）：
- openai    ：直接透传（请求体已是 OpenAI 形态，仅替换映射后的 model）
- ollama    ：/api/chat，请求/响应双向翻译为 OpenAI 形态（含流式 NDJSON → SSE）
- anthropic ：/v1/messages，请求/响应双向翻译为 OpenAI 形态（含流式事件 → SSE）

非流式返回统一 OpenAI dict；流式统一 yield OpenAI 形态的 SSE 文本块（`data: {...}\\n\\n`），
调用方（proxy）负责透传给下游并顺带做 Token/内容/工具调用的计量。
"""
import json
import logging
import time
import uuid
from typing import AsyncGenerator, Optional

logger = logging.getLogger("jianziruyang.gateway.upstream")

_ANTHROPIC_VERSION = "2023-06-01"


# ---------------------------------------------------------------------------
# 请求构造：返回 (headers, payload)
# ---------------------------------------------------------------------------

def build_request(channel: dict, body: dict, mapped_model: str, stream: bool,
                  kind: str = "chat") -> tuple[dict, dict]:
    """按渠道协议把 OpenAI 形态请求体转换为上游请求（headers + payload）。

    kind: chat（默认）/ embeddings / rerank。后两者为透传型 JSON 端点，仅按协议
    做最小适配（ollama embeddings 走 /api/embeddings 且无鉴权头），与 chat 通道隔离。
    """
    if kind == "embeddings":
        return _build_embeddings_request(channel, body, mapped_model)
    if kind == "rerank":
        return _build_rerank_request(channel, body, mapped_model)
    protocol = channel.get("protocol") or "openai"
    api_key = channel.get("api_key") or ""

    if protocol == "ollama":
        payload = {"model": mapped_model, "messages": body.get("messages") or [], "stream": stream}
        options = {}
        if body.get("temperature") is not None:
            options["temperature"] = body["temperature"]
        if body.get("top_p") is not None:
            options["top_p"] = body["top_p"]
        if body.get("tools"):
            payload["tools"] = body["tools"]
        if options:
            payload["options"] = options
        return {}, payload

    if protocol == "anthropic":
        messages, system_text = _split_anthropic_messages(body.get("messages") or [])
        payload = {
            "model": mapped_model,
            "messages": messages,
            "max_tokens": int(body.get("max_tokens") or 2048),
            "stream": stream,
        }
        if system_text:
            payload["system"] = system_text
        if body.get("temperature") is not None:
            payload["temperature"] = body["temperature"]
        if body.get("tools"):
            payload["tools"] = [_openai_tool_to_anthropic(t) for t in body["tools"]]
        headers = {"x-api-key": api_key, "anthropic-version": _ANTHROPIC_VERSION,
                   "content-type": "application/json"}
        return headers, payload

    # openai 兼容：透传，替换 model，去掉本网关注入的内部字段
    payload = {k: v for k, v in body.items() if not k.startswith("_gw_")}
    payload["model"] = mapped_model
    payload["stream"] = stream
    headers = {"content-type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers, payload


def _build_embeddings_request(channel: dict, body: dict, mapped_model: str) -> tuple[dict, dict]:
    """embeddings 请求构造：openai 兼容透传 {model,input,...}；ollama 走 /api/embeddings。"""
    protocol = channel.get("protocol") or "openai"
    api_key = channel.get("api_key") or ""
    # 透传非内部字段，替换 model，剥离 stream（embeddings 无流式）
    payload = {k: v for k, v in body.items() if not k.startswith("_gw_")}
    payload["model"] = mapped_model
    payload.pop("stream", None)
    payload.setdefault("input", body.get("input") or [])
    if protocol == "ollama":
        # ollama /api/embeddings 仅需 model + input，无鉴权头
        return {}, {"model": mapped_model, "input": payload["input"]}
    headers = {"content-type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers, payload


def _build_rerank_request(channel: dict, body: dict, mapped_model: str) -> tuple[dict, dict]:
    """rerank 请求构造：{model,query,documents,top_n?} + Bearer（镜像原生 reranker）。"""
    api_key = channel.get("api_key") or ""
    payload = {
        "model": mapped_model,
        "query": body.get("query") or "",
        "documents": body.get("documents") or [],
    }
    if body.get("top_n") is not None:
        payload["top_n"] = body["top_n"]
    headers = {"content-type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers, payload


def _split_anthropic_messages(messages: list) -> tuple[list, str]:
    """Anthropic 的 system 独立于 messages：抽取 system 消息，其余转为 {role,content} 文本。"""
    system_parts = []
    out = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        if role == "system":
            system_parts.append(text)
        else:
            out.append({"role": "user" if role not in ("user", "assistant") else role, "content": text})
    return out, "\n\n".join(system_parts)


def _openai_tool_to_anthropic(tool: dict) -> dict:
    fn = tool.get("function") or {}
    return {
        "name": fn.get("name") or tool.get("name") or "",
        "description": fn.get("description") or "",
        "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
    }


# ---------------------------------------------------------------------------
# 非流式响应归一化：统一为 OpenAI dict
# ---------------------------------------------------------------------------

def normalize_response(channel: dict, status_code: int, raw: dict, kind: str = "chat") -> dict:
    if kind == "embeddings":
        return _normalize_embeddings(channel, raw)
    if kind == "rerank":
        return _normalize_rerank(channel, raw)
    protocol = channel.get("protocol") or "openai"
    if protocol == "openai":
        return raw
    if protocol == "ollama":
        return _ollama_to_openai(channel, raw)
    if protocol == "anthropic":
        return _anthropic_to_openai(channel, raw)
    return raw


def _normalize_embeddings(channel: dict, raw: dict) -> dict:
    """embeddings 响应归一为 OpenAI {object:list,data:[{object:embedding,index,embedding}],model,usage}。

    openai 兼容上游已为该形态则透传；ollama {embeddings:[[..]]}（/api/embed）或
    {embedding:[..]}（/api/embeddings 单条）翻译为 OpenAI 形态。
    """
    protocol = channel.get("protocol") or "openai"
    if protocol != "ollama" and isinstance(raw.get("data"), list):
        return raw
    model = raw.get("model") or channel.get("name", "")
    vectors: list = []
    if isinstance(raw.get("embeddings"), list):
        vectors = raw["embeddings"]
    elif isinstance(raw.get("embedding"), list):
        vectors = [raw["embedding"]]
    elif isinstance(raw.get("data"), list):
        vectors = [d.get("embedding") for d in raw["data"]]
    data = [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)]
    usage = raw.get("usage") or {}
    pt = int(usage.get("prompt_tokens") or 0)
    total = int(usage.get("total_tokens") or pt)
    return {"object": "list", "model": model, "data": data,
            "usage": {"prompt_tokens": pt, "total_tokens": total}}


def _normalize_rerank(channel: dict, raw: dict) -> dict:
    """rerank 响应归一为 {model,results:[{index,relevance_score}]}。

    兼容顶层 results（OpenAI 兼容）/ output.results（DashScope 原生）。
    """
    items = raw.get("results") or (raw.get("output") or {}).get("results") or []
    results = []
    for it in items:
        if not isinstance(it, dict):
            continue
        score = it.get("relevance_score", it.get("score", 0.0))
        results.append({"index": it.get("index"), "relevance_score": float(score or 0.0)})
    return {"model": raw.get("model") or channel.get("name", ""), "results": results,
            "usage": raw.get("usage") or {}}


def _ollama_to_openai(channel: dict, raw: dict) -> dict:
    msg = raw.get("message") or {}
    pt = int(raw.get("prompt_eval_count") or 0)
    ct = int(raw.get("eval_count") or 0)
    choice = {"index": 0, "message": {"role": "assistant", "content": msg.get("content") or ""},
              "finish_reason": "stop"}
    if msg.get("tool_calls"):
        choice["message"]["tool_calls"] = [
            {"id": f"call_{i}", "type": "function",
             "function": {"name": tc.get("function", {}).get("name", ""),
                          "arguments": json.dumps(tc.get("function", {}).get("arguments", {}),
                                                  ensure_ascii=False)}}
            for i, tc in enumerate(msg["tool_calls"])
        ]
        choice["finish_reason"] = "tool_calls"
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion",
        "created": int(time.time()), "model": raw.get("model") or channel.get("name", ""),
        "choices": [choice],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
    }


def _anthropic_to_openai(channel: dict, raw: dict) -> dict:
    text_parts, tool_calls = [], []
    for block in raw.get("content") or []:
        if block.get("type") == "text":
            text_parts.append(block.get("text") or "")
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block.get("id") or f"call_{len(tool_calls)}", "type": "function",
                "function": {"name": block.get("name", ""),
                             "arguments": json.dumps(block.get("input", {}), ensure_ascii=False)},
            })
    message = {"role": "assistant", "content": "".join(text_parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    usage = raw.get("usage") or {}
    pt = int(usage.get("input_tokens") or 0)
    ct = int(usage.get("output_tokens") or 0)
    # Anthropic 前缀缓存命中量 → OpenAI 形态 prompt_tokens_details.cached_tokens（可观测）
    cached = int(usage.get("cache_read_input_tokens") or 0)
    return {
        "id": raw.get("id") or f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion",
        "created": int(time.time()), "model": raw.get("model") or channel.get("name", ""),
        "choices": [{"index": 0, "message": message,
                     "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct,
                  "prompt_tokens_details": {"cached_tokens": cached}},
    }


# ---------------------------------------------------------------------------
# 流式响应归一化：统一 yield OpenAI 形态 SSE 文本块
# ---------------------------------------------------------------------------

async def iter_openai_sse(channel: dict, aiter_lines) -> AsyncGenerator[str, None]:
    """openai 协议：上游 SSE 已是 OpenAI 形态，原样透传 data 行。"""
    async for line in aiter_lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("data:"):
            yield line if line.endswith("\n\n") else line + "\n\n"
            if line[5:].strip() == "[DONE]":
                return


async def iter_ollama_sse(channel: dict, aiter_lines) -> AsyncGenerator[str, None]:
    """ollama 协议：NDJSON 行 → OpenAI SSE 块。"""
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    async for line in aiter_lines:
        line = line.strip()
        if not line:
            continue
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            continue
        if chunk.get("error"):
            raise RuntimeError(str(chunk["error"]))
        msg = chunk.get("message") or {}
        delta = {"role": "assistant"}
        if msg.get("content"):
            delta["content"] = msg["content"]
        if msg.get("tool_calls"):
            delta["tool_calls"] = [
                {"index": i, "id": f"call_{i}", "type": "function",
                 "function": {"name": tc.get("function", {}).get("name", ""),
                              "arguments": json.dumps(tc.get("function", {}).get("arguments", {}),
                                                      ensure_ascii=False)}}
                for i, tc in enumerate(msg["tool_calls"])
            ]
        obj = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
               "model": chunk.get("model") or channel.get("name", ""), "choices": [{"index": 0, "delta": delta}]}
        if chunk.get("done"):
            obj["choices"][0]["finish_reason"] = "tool_calls" if msg.get("tool_calls") else "stop"
            pt = int(chunk.get("prompt_eval_count") or 0)
            ct = int(chunk.get("eval_count") or 0)
            obj["usage"] = {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}
        yield "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
        if chunk.get("done"):
            yield "data: [DONE]\n\n"
            return


async def iter_anthropic_sse(channel: dict, aiter_lines) -> AsyncGenerator[str, None]:
    """anthropic 协议：SSE 事件 → OpenAI SSE 块（提取 content_block_delta / message_delta）。"""
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    async for line in aiter_lines:
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        delta: dict = {}
        obj: Optional[dict] = None
        if etype == "content_block_delta":
            d = ev.get("delta") or {}
            if d.get("type") == "text_delta" and d.get("text"):
                delta["content"] = d["text"]
                obj = _chunk(cid, channel, delta)
            elif d.get("type") == "input_json_delta" and d.get("partial_json"):
                delta["content"] = d["partial_json"]
                obj = _chunk(cid, channel, delta)
        elif etype == "message_start":
            obj = _chunk(cid, channel, {"role": "assistant"})
            usage = (ev.get("message") or {}).get("usage") or {}
            obj["usage"] = {"prompt_tokens": int(usage.get("input_tokens") or 0),
                            "completion_tokens": int(usage.get("output_tokens") or 0),
                            "prompt_tokens_details": {
                                "cached_tokens": int(usage.get("cache_read_input_tokens") or 0)}}
        elif etype == "message_delta":
            usage = (ev.get("usage") or {})
            obj = _chunk(cid, channel, {}, finish_reason="stop")
            if usage.get("output_tokens") is not None:
                obj["usage"] = {"prompt_tokens": 0, "completion_tokens": int(usage["output_tokens"]),
                                "total_tokens": int(usage["output_tokens"])}
        elif etype == "message_stop":
            yield "data: [DONE]\n\n"
            return
        elif etype == "error":
            raise RuntimeError(str(ev.get("error") or "上游流错误"))
        if obj is not None:
            yield "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _chunk(cid: str, channel: dict, delta: dict, finish_reason: Optional[str] = None) -> dict:
    choice = {"index": 0, "delta": delta}
    if finish_reason:
        choice["finish_reason"] = finish_reason
    return {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
            "model": channel.get("name", ""), "choices": [choice]}


def stream_iterator(channel: dict, aiter_lines) -> AsyncGenerator[str, None]:
    """按协议选择流式归一化迭代器。"""
    protocol = channel.get("protocol") or "openai"
    if protocol == "ollama":
        return iter_ollama_sse(channel, aiter_lines)
    if protocol == "anthropic":
        return iter_anthropic_sse(channel, aiter_lines)
    return iter_openai_sse(channel, aiter_lines)
