"""主聊天 Agent 单元测试（mock LLM / 检索 / 搜索，不依赖网络/DB/向量库）。

覆盖：
  - ReAct：final 直接作答、search_knowledge 工具后作答、clarify 暂停、
    知识库空结果触发 need_search 暂停、联网开启/跳过询问时不触发 need_search、
    未知工具回喂不产生 tool 事件。
  - Plan&Execute：发 plan 事件并顺序执行工具、过滤未启用工具。
  - 契约：Agent 不发 done（done 由 chat.py 单一所有）；SSE 事件构造器格式。
"""
import asyncio
import json

import pytest

from app.services import chat_agent


class ScriptedLLM:
    """按脚本依次返回响应；chat 为异步生成器（决策/规划经 _llm_complete 收集，回答经流式转发）。"""

    def __init__(self, responses):
        self._responses = list(responses)

    async def chat(self, messages, stream=True):
        text = self._responses.pop(0) if self._responses else ""
        if text:
            mid = max(1, len(text) // 2)
            yield text[:mid]
            yield text[mid:]


def _setup(monkeypatch, responses, kb_results=None, format_ret=None):
    """替换 LLM 与检索/搜索/格式化依赖为可控合成实现。"""
    llm = ScriptedLLM(responses)
    monkeypatch.setattr(chat_agent.LLMFactory, "create_for_role", lambda *a, **k: llm)
    monkeypatch.setattr(chat_agent.vector_store, "result_key", lambda t, m: t)
    monkeypatch.setattr(chat_agent.knowledge_service, "retrieve_for_user",
                        lambda q, u: list(kb_results or []))
    monkeypatch.setattr(chat_agent.knowledge_service, "format_knowledge_context",
                        lambda q, r: tuple(format_ret or ("", [])))
    monkeypatch.setattr(chat_agent.knowledge_service, "build_knowledge_prompt",
                        lambda q, c, f: f"Q:{q}|hasCtx:{bool(c)}|kb:{f}")
    monkeypatch.setattr(chat_agent.knowledge_service, "build_search_context", lambda r: "")

    async def _no_web(q):
        return []
    monkeypatch.setattr(chat_agent, "web_search", _no_web)
    return llm


def _run(**kwargs):
    """驱动 run_chat_agent，返回 (解析后的事件列表, result dict)。"""
    result = {"status": "ok", "answer": "", "sources": [], "search_results": [], "hitl": None, "error": ""}

    async def go():
        events = [ev async for ev in chat_agent.run_chat_agent(result=result, **kwargs)]
        parsed = []
        for ev in events:
            assert ev.startswith("data: ") and ev.endswith("\n\n")
            parsed.append(json.loads(ev[len("data: "):].strip()))
        return parsed, result

    return asyncio.run(go())


def _base(**overrides):
    kwargs = dict(
        mode="react", query="黄芪的功效", user_id="u1", rule_id="",
        use_knowledge=True, use_search=False, skip_search_prompt=False,
        history_ctx=[], memory_profile="", memory_relevant="",
        system_prompt=[{"role": "system", "content": "sys"}],
    )
    kwargs.update(overrides)
    return kwargs


def _kinds(events):
    return [e["event"] for e in events]


# ============================================
# SSE 事件构造器契约（chat.py 快路径亦复用）
# ============================================
def test_need_search_and_clarify_event_shape():
    ns = json.loads(chat_agent.need_search_event("冷门问题")[len("data: "):].strip())
    assert ns["event"] == "need_search" and ns["query"] == "冷门问题" and ns["message"]
    cl = json.loads(chat_agent.clarify_event("哪个人群？", ["老年人", "上班族"])[len("data: "):].strip())
    assert cl["event"] == "clarify" and cl["question"] == "哪个人群？" and cl["options"] == ["老年人", "上班族"]


# ============================================
# ReAct
# ============================================
@pytest.mark.parametrize("mode", ["react", "plan_execute"])
@pytest.mark.parametrize("refresh,failed", [(False, False), (True, False), (False, True)])
def test_gap_request_identity_refresh_and_failure_filter(monkeypatch, mode, refresh, failed):
    from app.services import capability_gap
    responses = ([json.dumps({"steps": [{"tool": "web_search", "args": {"query": "天气"}}]})]
                 if mode == "plan_execute" else [json.dumps({"action": "tool", "tool": "web_search", "args": {"query": "天气"}}), '{"action":"final"}'])
    _setup(monkeypatch, responses + ["暂无天气数据。"])
    calls = []
    monkeypatch.setattr(capability_gap, "detect_scenario", lambda *a, **k: "realtime_weather")
    monkeypatch.setattr(capability_gap, "record_gap", lambda **kw: calls.append(kw) or "")
    if failed:
        async def unavailable(query):
            raise RuntimeError("temporary search failure")
        monkeypatch.setattr(chat_agent, "web_search", unavailable)
    events, result = _run(**_base(mode=mode, query="香港天气", use_knowledge=False, use_search=True,
                                request_id="user-message-id", force_refresh=refresh))
    assert result["status"] == "ok"
    if refresh or failed:
        assert calls == []
    else:
        assert calls[0]["request_id"] == "user-message-id" and calls[0]["user_id"] == "u1"
    assert "error" not in _kinds(events)


def test_react_final_answer_streams_and_emits_no_done(monkeypatch):
    _setup(monkeypatch, ['{"thought":"寒暄直接回答","action":"final"}', "你好，很高兴为你服务"])
    events, res = _run(**_base(query="你好"))
    kinds = _kinds(events)
    assert "thought" not in kinds
    assert "寒暄直接回答" not in json.dumps(events, ensure_ascii=False)
    assert "token" in kinds
    assert "done" not in kinds  # Agent 不发 done
    assert res["status"] == "ok"
    assert "很高兴" in res["answer"]


def test_react_search_knowledge_tool_then_answer(monkeypatch):
    kb = [("黄芪补气固表", {"wiki_title": "黄芪"}, 0.9)]
    _setup(monkeypatch,
           ['{"thought":"检索黄芪","action":"tool","tool":"search_knowledge","args":{"query":"黄芪"}}',
            '{"action":"final"}',
            "黄芪具有补气固表功效[来源 1]"],
           kb_results=kb,
           format_ret=("黄芪补气固表[来源 1]", [{"index": 1, "wiki_title": "黄芪"}]))
    events, res = _run(**_base())
    tool_events = [e for e in events if e["event"] == "tool"]
    assert [t["state"] for t in tool_events] == ["start", "end"]
    assert tool_events[0]["tool"] == "search_knowledge"
    assert res["status"] == "ok"
    assert res["sources"]  # 回答阶段统一格式化回填来源
    assert "[来源 1]" in res["answer"]


def test_react_clarify_pauses_without_answer(monkeypatch):
    _setup(monkeypatch, ['{"thought":"意图模糊","action":"clarify","question":"你想了解哪方面？","options":["食疗","穴位"]}'])
    events, res = _run(**_base(query="怎么调理"))
    kinds = _kinds(events)
    assert "clarify" in kinds
    assert "token" not in kinds  # 暂停，不生成回答
    assert "generating" not in [e.get("phase") for e in events if e["event"] == "status"]  # 未进入回答阶段
    assert res["status"] == "hitl"
    assert res["hitl"] == "clarify"


def test_react_empty_kb_triggers_need_search(monkeypatch):
    _setup(monkeypatch, ['{"action":"tool","tool":"search_knowledge","args":{"query":"冷门主题"}}'], kb_results=[])
    events, res = _run(**_base(query="冷门主题"))
    assert "need_search" in _kinds(events)
    assert "generating" not in [e.get("phase") for e in events if e["event"] == "status"]  # 未进入回答阶段
    assert res["status"] == "hitl"
    assert res["hitl"] == "need_search"


def test_react_empty_kb_with_search_enabled_no_need_search(monkeypatch):
    _setup(monkeypatch,
           ['{"action":"tool","tool":"search_knowledge","args":{"query":"x"}}', '{"action":"final"}', "回答"],
           kb_results=[])
    events, res = _run(**_base(use_search=True))
    assert "need_search" not in _kinds(events)
    assert res["status"] == "ok"


def test_react_empty_kb_with_skip_prompt_no_need_search(monkeypatch):
    _setup(monkeypatch,
           ['{"action":"tool","tool":"search_knowledge","args":{"query":"x"}}', '{"action":"final"}', "回答"],
           kb_results=[])
    events, res = _run(**_base(skip_search_prompt=True))
    assert "need_search" not in _kinds(events)
    assert res["status"] == "ok"


def test_react_unknown_tool_refed_without_tool_event(monkeypatch):
    _setup(monkeypatch, ['{"action":"tool","tool":"bogus","args":{}}', '{"action":"final"}', "回答"], kb_results=[])
    events, res = _run(**_base())
    assert "tool" not in _kinds(events)  # 不可用工具不产生 tool 事件，仅回喂错误观测
    assert res["status"] == "ok"


# ============================================
# Plan&Execute
# ============================================
def test_plan_execute_emits_plan_and_executes_tools(monkeypatch):
    kb = [("失眠调理", {"wiki_title": "失眠"}, 0.8)]
    _setup(monkeypatch,
           ['{"steps":[{"tool":"search_knowledge","args":{"query":"失眠"},"purpose":"检索失眠资料"}]}',
            "根据知识库，失眠可通过穴位按摩调理[来源 1]"],
           kb_results=kb,
           format_ret=("失眠调理[来源 1]", [{"index": 1, "wiki_title": "失眠"}]))
    events, res = _run(**_base(mode="plan_execute", query="失眠怎么办"))
    kinds = _kinds(events)
    assert "plan" in kinds
    assert "tool" in kinds
    phases = [e.get("phase") for e in events if e["event"] == "status"]
    assert "planning" in phases and "executing" in phases
    plan_ev = next(e for e in events if e["event"] == "plan")
    assert plan_ev["steps"][0]["tool"] == "search_knowledge"
    assert res["status"] == "ok"
    assert res["answer"]


def test_plan_execute_filters_disallowed_tools(monkeypatch):
    # 用户已拒绝联网授权（skip_search_prompt）：web_search 完全下线，计划中被过滤
    _setup(monkeypatch,
           ['{"steps":[{"tool":"web_search","args":{"query":"x"},"purpose":"联网"}]}', "回答"],
           kb_results=[])
    events, res = _run(**_base(mode="plan_execute", use_knowledge=True, use_search=False,
                               skip_search_prompt=True))
    plan_ev = next(e for e in events if e["event"] == "plan")
    assert plan_ev["steps"] == []  # web_search 未启用被过滤
    assert "tool" not in _kinds(events)  # 无可执行步骤
    assert "need_search" not in _kinds(events)  # 拒绝后不再询问
    assert res["status"] == "ok"


# ============================================
# 联网授权门：计划后、执行前 HITL（需求：不再等整轮答完才判断）
# ============================================
def test_plan_web_step_pauses_for_authorization_before_execution(monkeypatch):
    plan = json.dumps({"steps": [
        {"tool": "search_knowledge", "args": {"query": "失眠 调理"}, "purpose": "检索知识库调理资料"},
        {"tool": "web_search", "args": {"query": "失眠 运动方案"}, "purpose": "补充运动方案"}]})
    _setup(monkeypatch, [plan, "不应生成的回答"],
           kb_results=[("失眠调理", {"wiki_title": "失眠"}, 0.8)])
    saved = []
    monkeypatch.setattr(chat_agent.retrieval_reuse, "save_pending_plan",
                        lambda **kw: saved.append(kw) or True)
    events, res = _run(**_base(mode="plan_execute", query="帮我规划3个月的调理方案", session_id="s1"))
    kinds = _kinds(events)
    assert "plan" in kinds and "need_search" in kinds
    assert "tool" not in kinds  # 计划已呈现但步骤一个都未执行
    assert "token" not in kinds  # 未生成回答（不白跑一轮）
    plan_ev = next(e for e in events if e["event"] == "plan")
    web_step = next(s for s in plan_ev["steps"] if s["tool"] == "web_search")
    assert web_step["needs_auth"] is True
    assert res["status"] == "hitl" and res["hitl"] == "need_search"
    # 待授权计划已暂存，供授权后免重规划续跑
    assert saved and [s["tool"] for s in saved[0]["steps"]] == ["search_knowledge", "web_search"]


def test_plan_resume_authorized_skips_replanning(monkeypatch):
    pending = [{"tool": "search_knowledge", "args": {"query": "失眠"}, "purpose": "检索知识库"},
               {"tool": "web_search", "args": {"query": "失眠 运动"}, "purpose": "补充联网运动"}]
    _setup(monkeypatch, ["结合知识库与联网资料的方案[来源 1]"],
           kb_results=[("失眠调理", {"wiki_title": "失眠"}, 0.8)],
           format_ret=("失眠调理[来源 1]", [{"index": 1, "wiki_title": "失眠"}]))
    skips = []
    monkeypatch.setattr(chat_agent.obs, "record_skip", lambda name, reason="": skips.append(name))
    events, res = _run(**_base(mode="plan_execute", use_search=True, pending_plan_steps=pending))
    assert "plan" in _kinds(events)
    assert "沿用已确认的计划" in json.dumps(events, ensure_ascii=False)
    assert "正在规划解题步骤" not in json.dumps(events, ensure_ascii=False)
    assert "plan" in skips  # 免重规划
    tools = [e for e in events if e["event"] == "tool"]
    assert [(t["tool"], t["state"]) for t in tools] == [
        ("search_knowledge", "start"), ("search_knowledge", "end"),
        ("web_search", "start"), ("web_search", "end")]
    assert res["status"] == "ok" and res["answer"]


def test_plan_resume_denied_drops_web_steps(monkeypatch):
    pending = [{"tool": "search_knowledge", "args": {"query": "失眠"}, "purpose": "检索知识库"},
               {"tool": "web_search", "args": {"query": "失眠 运动"}, "purpose": "补充联网运动"}]
    _setup(monkeypatch, ["仅基于知识库的回答[来源 1]"],
           kb_results=[("失眠调理", {"wiki_title": "失眠"}, 0.8)],
           format_ret=("失眠调理[来源 1]", [{"index": 1, "wiki_title": "失眠"}]))
    events, res = _run(**_base(mode="plan_execute", skip_search_prompt=True, pending_plan_steps=pending))
    plan_ev = next(e for e in events if e["event"] == "plan")
    assert [s["tool"] for s in plan_ev["steps"]] == ["search_knowledge"]  # 拒绝后剔除联网步骤
    tools = [e for e in events if e["event"] == "tool"]
    assert {t["tool"] for t in tools} == {"search_knowledge"}
    assert "need_search" not in _kinds(events)
    assert res["status"] == "ok" and res["answer"]


def test_react_web_proposal_pauses_for_authorization(monkeypatch):
    _setup(monkeypatch,
           ['{"action":"tool","tool":"web_search","args":{"query":"运动方案"}}', "不应生成"],
           kb_results=[("失眠调理", {"wiki_title": "失眠"}, 0.8)])
    events, res = _run(**_base(query="帮我规划运动方案"))
    assert "need_search" in _kinds(events)
    assert "tool" not in _kinds(events)  # 工具执行前拦截
    assert "token" not in _kinds(events)
    assert res["status"] == "hitl" and res["hitl"] == "need_search"


# ============================================
# JSON 解析健壮性（免费模型回显多次拼接）
# ============================================
def test_parse_first_json_handles_duplicated_object():
    doubled = ('{"steps":[{"tool":"search_knowledge","args":{"query":"失眠"},"purpose":"检索"}]} '
               '{"steps": [{"tool": "search_knowledge"}]}')
    parsed = chat_agent._parse_first_json(doubled)
    assert parsed["steps"][0]["tool"] == "search_knowledge"
    assert parsed["steps"][0]["args"]["query"] == "失眠"


def test_parse_first_json_plain_and_noise():
    assert chat_agent._parse_first_json('{"action":"final"}') == {"action": "final"}
    assert chat_agent._parse_first_json('好的：{"action":"final"} 以上') == {"action": "final"}
    assert chat_agent._parse_first_json("不是 JSON") == {}


def test_plan_execute_duplicated_plan_json_still_executes(monkeypatch):
    # 规划响应被回显两次，仍应解析出首个计划并执行工具
    kb = [("失眠调理", {"wiki_title": "失眠"}, 0.8)]
    dup_plan = ('{"steps":[{"tool":"search_knowledge","args":{"query":"失眠"},"purpose":"检索"}]} '
                '{"steps":[{"tool":"search_knowledge","args":{"query":"失眠"}}]}')
    _setup(monkeypatch, [dup_plan, "根据知识库，失眠可调理[来源 1]"],
           kb_results=kb, format_ret=("失眠调理[来源 1]", [{"index": 1, "wiki_title": "失眠"}]))
    events, res = _run(**_base(mode="plan_execute", query="失眠怎么办"))
    assert "tool" in _kinds(events)  # 步骤已执行
    assert res["status"] == "ok"
    assert res["sources"]
