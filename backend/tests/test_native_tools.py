"""原生工具协议、网关能力隔离与调用预算；全部使用内存库和本地桩。"""
import asyncio
import copy
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from app.core import db
from app.core.json_schema import SchemaError, parse_object, validate
from app.core.llm import LLMFactory, OpenAICompatibleLLM
from app.core.tool_stream import ToolCallAccumulator, ToolModelUnavailable, ToolProtocolError
from app.services.gateway import upstream, upstreams, rules, keys, proxy
from app.services.tools import native, base, hooks

SCHEMA = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 5}},
          "required": ["n"], "additionalProperties": False}
TOOLS = [{"type": "function", "function": {"name": "test_native_read", "parameters": SCHEMA}}]


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    hooks.reset_rate_limits()
    yield conn
    conn.close()


def call(identifier="call_1", name="test_native_read", arguments='{"n":1}'):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": arguments}}


def delta(call_value, index=0):
    return {"tool_calls": [{"index": index, **call_value}]}


def test_accumulator_parallel_fragments_and_finish():
    acc = ToolCallAccumulator()
    acc.add(delta(call(arguments='{"n":')))
    acc.add(delta(call("call_2", arguments='{"n":2}'), 1))
    acc.add({"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]})
    acc.finish("tool_calls")
    assert acc.completed() == [call(), call("call_2", arguments='{"n":2}')]


@pytest.mark.parametrize("kind", ["missing_finish", "length", "missing_id", "duplicate_id", "index_gap", "changed_id", "oversize"])
def test_accumulator_rejects_partial_or_ambiguous(kind):
    acc = ToolCallAccumulator()
    with pytest.raises(ToolProtocolError):
        value = call(identifier="" if kind == "missing_id" else "call_1")
        acc.add(delta(value, 1 if kind == "index_gap" else 0))
        if kind == "duplicate_id":
            acc.add(delta(value, 1))
        if kind == "changed_id":
            acc.add(delta(call("changed")))
        if kind == "oversize":
            acc.add(delta(call(arguments="x" * 65537)))
        if kind != "missing_finish":
            acc.finish("length" if kind == "length" else "tool_calls")
        acc.completed()


@pytest.mark.parametrize("text", ['[]', '{"n":1,"n":2}', '{"n":NaN}', '{"n":1}{}'])
def test_strict_json(text):
    with pytest.raises(SchemaError):
        parse_object(text)


@pytest.mark.parametrize("args", [{"n": True}, {"n": 0}, {"n": 6}, {"n": 1, "url": "x"}, {"n": None}])
def test_strict_schema(args):
    with pytest.raises(SchemaError):
        validate(SCHEMA, args)


def test_nested_schema_and_no_remote_refs():
    schema = {"type": "object", "properties": {"a": {"type": "array", "maxItems": 1, "items": SCHEMA}}}
    validate(schema, {"a": [{"n": 1}]})
    with pytest.raises(SchemaError):
        validate(schema, {"a": [{"n": "1"}]})
    with pytest.raises(SchemaError):
        validate({"$ref": "http://127.0.0.1/private"}, {})


class Stream:
    def __init__(self, chunks):
        self.chunks, self.closed = chunks, False
        self.response = self
    async def __aiter__(self):
        for chunk in self.chunks:
            yield ChatCompletionChunk.model_validate(chunk)
    async def aclose(self):
        self.closed = True


def chunk(value=None, finish=None):
    return {"id": "completion", "object": "chat.completion.chunk", "created": 1, "model": "m1",
            "choices": [{"index": 0, "delta": value or {}, "finish_reason": finish}]}


def test_llm_native_stream_and_nonstream_forwarding(monkeypatch):
    from app.core import llm as module
    captured = []
    response_stream = Stream([chunk(delta(call(arguments='{"n":'))),
                              chunk({"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}),
                              chunk(finish="tool_calls")])
    async def create(**kwargs):
        captured.append(kwargs)
        if kwargs["stream"]:
            return response_stream
        return ChatCompletion.model_validate({"id": "x", "object": "chat.completion", "created": 1, "model": "m1",
            "choices": [{"index": 0, "message": {"role": "assistant", "tool_calls": [call()]}, "finish_reason": "tool_calls"}]})
    monkeypatch.setattr(module, "_get_openai_client", lambda *a: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    llm = OpenAICompatibleLLM(model="m1", supports_tools=True,
                             extra_body={"_gw_role": "tool_decision", "_gw_cache_contract": "secret"})
    async def check():
        for streaming in (True, False):
            events = [event async for event in llm.chat_tools([{"role": "user", "content": "读取"}], TOOLS, stream=streaming)]
            assert events[-1]["tool_calls"] == [call()]
        unsupported = OpenAICompatibleLLM(model="m1")
        with pytest.raises(ToolModelUnavailable):
            await anext(unsupported.chat_tools([], TOOLS))
    asyncio.run(check())
    assert response_stream.closed
    assert all(v["tools"] == TOOLS and v["tool_choice"] == "auto" for v in captured)
    assert all("_gw_cache_contract" not in v["extra_body"] for v in captured)


def environment():
    ordinary = upstreams.create_upstream({"name": "ordinary", "tier": "free", "models": ["m1"]})
    capable = upstreams.create_upstream({"name": "capable", "tier": "free", "models": ["m1"], "tool_models": ["m1"]})
    rule = rules.create_rule({"model_pattern": "*", "is_default": True, "upstreams": [ordinary["id"], capable["id"]]})
    return ordinary, capable, rule


def test_gateway_declared_capability_isolation_and_migration(isolated):
    ordinary, capable, rule = environment()
    assert not upstreams.supports_tools(ordinary, "m1")
    assert upstreams.clone_upstream(capable["id"])["tool_models"] == ["m1"]
    with pytest.raises(ValueError):
        upstreams.update_upstream(ordinary["id"], {"tool_models": ["other"]})
    model = LLMFactory.create_for_role("tool_decision", "u1", rule["id"], "m1", require_tools=True)
    assert model.supports_tools
    body = {"model": "m1", "tools": TOOLS, "messages": [], "_gw_role": "tool_decision", "_gw_rule_id": rule["id"]}
    ctx = proxy.prepare(body, keys.ensure_internal_key())
    assert [c["id"] for c in ctx.candidates] == [capable["id"]]
    upstreams.update_upstream(capable["id"], {"tool_models": []})
    with pytest.raises(proxy.GatewayError) as exc:
        proxy.prepare(body, keys.ensure_internal_key())
    assert exc.value.code == "tool_model_unavailable"
    with pytest.raises(ToolModelUnavailable):
        LLMFactory.create_for_role("tool_decision", "u1", rule["id"], "m1", require_tools=True)
    db.init_db(isolated)
    assert db.query_one("SELECT COUNT(*) n FROM schema_migrations WHERE version='gateway_native_tools_v1'")["n"] == 1


def test_protocol_roundtrip_tool_ids_and_choices():
    messages = [{"role": "system", "content": "说明"}, {"role": "user", "content": "读取"},
                {"role": "assistant", "content": None, "tool_calls": [call()]},
                {"role": "tool", "tool_call_id": "call_1", "content": '{"ok":true}'}]
    body = {"messages": messages, "tools": TOOLS, "tool_choice": "required"}
    _, payload = upstream.build_request({"protocol": "anthropic"}, body, "m1", True)
    assert payload["messages"][1]["content"][0]["input"] == {"n": 1}
    assert payload["messages"][2]["content"][0]["tool_use_id"] == "call_1"
    assert payload["tool_choice"] == {"type": "any"}
    with pytest.raises(ValueError):
        upstream.build_request({"protocol": "ollama"}, body, "m1", True)
    _, payload = upstream.build_request({"protocol": "ollama"}, {**body, "tool_choice": "auto"}, "m1", True)
    assert payload["messages"][-1]["tool_name"] == "test_native_read"
    assert payload["messages"][-2]["tool_calls"][0]["function"]["arguments"] == {"n": 1}
    assert messages[-1]["tool_call_id"] == "call_1"


def test_anthropic_stream_arguments_not_content():
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "call_1", "name": "test_native_read", "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"n":'}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '1}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 8}},
        {"type": "message_stop"}]
    async def lines():
        for event in events:
            yield "data: " + json.dumps(event)
    async def check():
        acc = ToolCallAccumulator()
        async for line in upstream.iter_anthropic_sse({}, lines()):
            if "[DONE]" in line:
                continue
            value = json.loads(line[5:])
            choice = value["choices"][0]
            assert "content" not in choice["delta"]
            acc.add(choice["delta"])
            if choice.get("finish_reason"):
                acc.finish(choice["finish_reason"])
                assert value["usage"]["total_tokens"] == 18
        assert acc.completed() == [call()]
    asyncio.run(check())


class FakeLLM:
    supports_tools = True
    def __init__(self, turns):
        self.turns, self.messages = iter(turns), []
    async def chat_tools(self, messages, tools, **kwargs):
        self.messages.append(copy.deepcopy(messages))
        for event in next(self.turns):
            yield event


def turn(calls=None, text="完成"):
    return [{"type": "text", "text": text}, {"type": "done", "tool_calls": calls or [], "finish_reason": "tool_calls" if calls else "stop"}]


@pytest.fixture
def test_tool(monkeypatch):
    calls = []
    class ReadTool(base.BaseTool):
        name = "test_native_read"
        parameters = SCHEMA
        permissions = ("authenticated",)
        strict_schema = True
        async def run(self, ctx, n):
            calls.append(n)
            return base.ToolResult(self.name, True, text=str(n), extra={"validation_status": "passed", "requirement_status": "satisfied"})
    monkeypatch.setitem(base._REGISTRY, ReadTool.name, ReadTool())
    return calls


def run(llm, result, **kwargs):
    async def collect():
        return [e async for e in native.run_tool_loop(llm, [{"role": "user", "content": "读取"}],
            base.ToolContext("u1", "", "", ""), TOOLS, result=result, **kwargs)]
    return asyncio.run(collect())


def test_native_loop_sequential_result_messages(test_tool):
    llm = FakeLLM([turn([call(), call("call_2", arguments='{"n":2}')], "内部说明不公开"), turn(text="结果是 1 和 2")])
    result = {}
    events = run(llm, result)
    assert test_tool == [1, 2] and result["status"] == "ok"
    assert [m["tool_call_id"] for m in llm.messages[1] if m["role"] == "tool"] == ["call_1", "call_2"]
    assert all("内部说明" not in str(e) for e in events)
    assert events[-1]["content"] == "结果是 1 和 2"


@pytest.mark.parametrize("bad", [call(name="web_search"), call(arguments='{"n":9}'), call(arguments='{"n":1,"url":"x"}')])
def test_native_loop_rejects_unapproved_and_invalid(test_tool, bad):
    result = {}
    events = run(FakeLLM([turn([bad]), turn(text="全部成功")]), result)
    assert test_tool == [] and result["status"] == "partial"
    assert "全部成功" not in str(events)


def test_native_budget_and_repeat_failure(monkeypatch, test_tool):
    result = {}
    run(FakeLLM([turn([call(), call("call_2")])]), result, max_calls=1)
    assert test_tool == [] and result["error"] == "tool_budget_exhausted"
    invoked = []
    async def fail(*a, **k):
        invoked.append(1)
        return base.ToolResult("test_native_read", False, error="private-secret")
    monkeypatch.setattr(native, "invoke_tool", fail)
    result = {}
    events = run(FakeLLM([turn([call(f"c{i}")]) for i in range(7)]), result)
    assert len(invoked) == 2 and result["calls"] == 6 and "private-secret" not in str(events)


def test_native_cancellation_and_timeout(monkeypatch, test_tool):
    cancelled = []
    async def wait(*a, **k):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)
    monkeypatch.setattr(native, "invoke_tool", wait)
    result = {}
    run(FakeLLM([turn([call(), call("call_2")])]), result, deadline=time.monotonic() + .05)
    assert cancelled == [True] and result["error"] == "tool_timeout" and test_tool == []
    assert result["records"][-1]["execution_status"] == "timeout"
    async def cancel():
        result = {}
        async def consume():
            async for _ in native.run_tool_loop(FakeLLM([turn([call()])]), [], base.ToolContext("u1", "", "", ""), TOOLS, result=result):
                pass
        task = asyncio.create_task(consume())
        await asyncio.sleep(.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert result["status"] == "cancelled"
        assert result["records"][-1]["execution_status"] == "cancelled"
    asyncio.run(cancel())


@pytest.mark.parametrize("value", [{"content": []}, {"tool_calls": {}}, {"tool_calls": [None]},
                                    {"tool_calls": [{"index": 0, "function": []}]},
                                    {"tool_calls": [{"index": 0, "id": "bad\nvalue"}]}])
def test_malformed_delta_rejected(value):
    with pytest.raises(ToolProtocolError):
        ToolCallAccumulator().add(value)


def test_tool_factory_auto_selection_stays_in_compatible_rule():
    from app.services.gateway import settings as gateway_settings
    ordinary, capable, rule = environment()
    upstreams.update_upstream(ordinary["id"], {"models": ["plain"]})
    gateway_settings.save_settings({"default_model": "plain"})
    model = LLMFactory.create_for_role("tool_decision", "u1", rule["id"], require_tools=True)
    assert model.model == "m1"
    with pytest.raises(ToolModelUnavailable):
        LLMFactory.create_for_role("tool_decision", "u1", rule["id"], "plain", require_tools=True)
    with pytest.raises(ToolModelUnavailable):
        LLMFactory.create_for_role("tool_decision", "u1", "missing-rule", require_tools=True)


@pytest.mark.parametrize("messages", [None, "invalid", {}])
def test_gateway_messages_shape_does_not_crash(messages):
    environment()
    assert proxy.prepare({"model": "m1", "messages": messages}, keys.ensure_internal_key()).candidates


def test_ollama_truncated_tool_call_cannot_be_completed():
    result = upstream.normalize_response({"protocol": "ollama"}, 200,
        {"done_reason": "length", "message": {"tool_calls": [{"function": {"name": "x", "arguments": {}}}]}})
    assert result["choices"][0]["finish_reason"] == "length"


def test_repeat_failure_uses_canonical_arguments(monkeypatch):
    invoked = []
    async def fail(*a, **k):
        invoked.append(1)
        return base.ToolResult("test_native_read", False)
    monkeypatch.setattr(native, "invoke_tool", fail)
    result = {}
    turns = [turn([call("c1", arguments='{"n":1}')]), turn([call("c2", arguments='{ "n": 1 }')]),
             turn([call("c3", arguments='{ "n":1}')]), turn(text="完成")]
    run(FakeLLM(turns), result)
    assert len(invoked) == 2 and result["status"] == "partial"


def test_final_answer_streams_only_after_decision(test_tool):
    final = [{"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"},
             {"type": "done", "tool_calls": [], "finish_reason": "stop"}]
    llm = FakeLLM([turn([call()], "内部决策"), turn(text="非最终稿"), final])
    result = {}
    events = run(llm, result, stream_final=True)
    assert [e["content"] for e in events if e["event"] == "token"] == ["第一段", "第二段"]
    assert result["answer"] == "第一段第二段" and result["status"] == "ok"
    assert "非最终稿" not in str(events) and test_tool == [1]


def test_final_stream_truncation_is_not_success(test_tool):
    final = [{"type": "text", "text": "部分答案"}]
    result = {}
    events = run(FakeLLM([turn([call()]), turn(), final]), result, stream_final=True)
    assert result["status"] == "error" and result["answer"] == "部分答案"
    assert events[-1]["event"] == "error"


def test_final_phase_never_executes_new_tool(test_tool):
    result = {}
    run(FakeLLM([turn([call()]), turn(), turn([call("unexpected")])]), result, stream_final=True)
    assert result["status"] == "error" and test_tool == [1]


def test_ollama_none_omits_tool_catalog():
    _, payload = upstream.build_request({"protocol": "ollama"}, {"messages": [], "tools": TOOLS, "tool_choice": "none"}, "m1", True)
    assert "tools" not in payload
