"""原生 Function Calling 增量拼装；未完整结束的调用绝不交给执行器。"""
import re


class ToolProtocolError(ValueError):
    pass


class ToolModelUnavailable(RuntimeError):
    pass


class ToolCallAccumulator:
    def __init__(self):
        self.calls = {}
        self.finish_reason = None
        self.content_size = 0

    def add(self, delta):
        if not isinstance(delta, dict):
            raise ToolProtocolError("工具流片段必须为对象")
        if self.finish_reason and (delta.get("content") or delta.get("tool_calls")):
            raise ToolProtocolError("模型结束后仍返回增量内容")
        content = delta.get("content")
        if content is not None and not isinstance(content, str):
            raise ToolProtocolError("正文增量必须是字符串")
        parts = delta.get("tool_calls")
        if parts is not None and not isinstance(parts, list):
            raise ToolProtocolError("工具增量必须是列表")
        self.content_size += len((content or "").encode())
        if self.content_size > 262144:
            raise ToolProtocolError("模型正文超过本轮预算")
        for part in parts or []:
            if not isinstance(part, dict):
                raise ToolProtocolError("工具增量项必须是对象")
            index = part.get("index")
            if type(index) is not int or not 0 <= index < 6 or part.get("type", "function") not in (None, "function"):
                raise ToolProtocolError("工具流索引或类型无效")
            call = self.calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
            identifier = part.get("id")
            if identifier is not None and (not isinstance(identifier, str) or len(identifier) > 200
                                           or any(ord(c) < 32 or ord(c) == 127 for c in identifier)):
                raise ToolProtocolError("工具调用 ID 无效")
            if identifier:
                if call["id"] and call["id"] != identifier:
                    raise ToolProtocolError("同一调用的标识发生变化")
                call["id"] = identifier
            fn = part.get("function")
            if fn is not None and not isinstance(fn, dict):
                raise ToolProtocolError("工具函数增量必须是对象")
            fn = fn or {}
            for key, limit in (("name", 64), ("arguments", 65536)):
                value = fn.get(key)
                if value is not None:
                    if not isinstance(value, str):
                        raise ToolProtocolError("工具增量必须是字符串")
                    call["function"][key] += value
                    if len(call["function"][key].encode()) > limit:
                        raise ToolProtocolError("工具名称或参数超过预算")

    def finish(self, reason):
        if self.finish_reason is not None and self.finish_reason != reason:
            raise ToolProtocolError("模型返回矛盾的结束状态")
        self.finish_reason = reason

    def completed(self):
        if self.finish_reason not in ("stop", "tool_calls"):
            raise ToolProtocolError("模型未完整结束，或因长度／内容过滤中止")
        if bool(self.calls) != (self.finish_reason == "tool_calls"):
            raise ToolProtocolError("工具调用与结束状态不一致")
        if sorted(self.calls) != list(range(len(self.calls))):
            raise ToolProtocolError("工具流缺少调用索引")
        calls, seen = [], set()
        for index in sorted(self.calls):
            call = self.calls[index]
            identifier, fn = call["id"], call["function"]
            if (not isinstance(identifier, str) or not 1 <= len(identifier) <= 200
                    or identifier in seen or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", fn["name"])):
                raise ToolProtocolError("工具调用缺少合法且唯一的 ID／函数名")
            seen.add(identifier)
            calls.append(call)
        return calls
