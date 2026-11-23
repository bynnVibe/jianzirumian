"""检索快照复用回归（内存 DB + mock LLM/检索，不出网）。

场景：用户先只开知识检索提问 → 能力受限卡「开启联网搜索并重新提问」→ 同一问题第二轮。
覆盖：
  - 快照服务：写入/读取 roundtrip、问题归一化、upsert 单行、TTL 过期回落、游客不写、跨会话隔离；
  - Agent（Plan&Execute / ReAct）：命中快照后不再调用检索与重排，上一轮知识步骤以
    「已完成·复用」待办下发，新步骤序号接续，来源编号沿用上一轮；
  - 无快照/未开知识检索时行为不变（仍正常检索）；
  - chat_stream 把 reuse_retrieval 透传到内层流水线。
"""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from app.core import db
from app.services import chat_agent
from app.services import retrieval_reuse as rr


@pytest.fixture()
def mem_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    conn.isolation_level = None  # 自动提交：避免直插数据残留隐式事务
    monkeypatch.setattr(db, "_conn", conn)
    yield conn
    conn.close()


KB = [("失眠调理资料", {"wiki_title": "失眠"}, 0.8)]
SOURCES = [{"index": 1, "wiki_title": "失眠"}]
SNAPSHOT = {
    "kb_results": KB, "sources": SOURCES, "context_text": "失眠调理资料[来源 1]",
    "steps": [{"purpose": "检索失眠调理资料", "query": "失眠", "summary": "检索到 3 条相关片段"}],
}


# ============================================
# 快照服务
# ============================================
def test_save_load_roundtrip_and_normalization(mem_db):
    assert rr.save(session_id="s1", user_id="u1", query="帮我规划3个月的调理方案",
                   mode="plan_execute", kb_results=KB, sources=SOURCES,
                   context_text=SNAPSHOT["context_text"], steps=SNAPSHOT["steps"]) is True
    # 空白/大小写归一化后仍命中同一快照
    got = rr.load("s1", "u1", "  帮我规划3个月的调理方案 \n")
    assert got and got["kb_results"] == KB and got["sources"] == SOURCES
    assert got["context_text"] == SNAPSHOT["context_text"] and got["steps"] == SNAPSHOT["steps"]
    assert got["mode"] == "plan_execute"
    # upsert：同键只保留一行，TTL 顺延
    rr.save(session_id="s1", user_id="u1", query="帮我规划3个月的调理方案", mode="fast",
            kb_results=KB, sources=SOURCES, context_text="新上下文")
    rows = db.query("SELECT * FROM chat_retrieval_snapshots")
    assert len(rows) == 1
    assert rr.load("s1", "u1", "帮我规划3个月的调理方案")["context_text"] == "新上下文"
    expires = datetime.fromisoformat(rows[0]["expires_at"])
    assert timedelta(minutes=14) < expires - datetime.now() <= timedelta(minutes=15)


def test_expired_snapshot_falls_back_and_purge(mem_db):
    rr.save(session_id="s1", user_id="u1", query="q", kb_results=KB)
    db.execute("UPDATE chat_retrieval_snapshots SET expires_at=?",
               ((datetime.now() - timedelta(seconds=1)).isoformat(),))
    assert rr.load("s1", "u1", "q") is None
    assert rr.purge_expired() == 1
    assert db.query("SELECT * FROM chat_retrieval_snapshots") == []


def test_guest_empty_and_no_hits_not_saved(mem_db):
    assert rr.save(session_id="s1", user_id="guest", query="q", kb_results=KB) is False
    assert rr.save(session_id="s1", user_id="u1", query="  ", kb_results=KB) is False
    assert rr.save(session_id="s1", user_id="u1", query="q", kb_results=[]) is False
    assert db.query("SELECT * FROM chat_retrieval_snapshots") == []
    assert rr.load("s1", "guest", "q") is None


def test_snapshot_isolated_by_session_and_user(mem_db):
    rr.save(session_id="s1", user_id="u1", query="q", kb_results=KB)
    assert rr.load("s2", "u1", "q") is None
    assert rr.load("s1", "u2", "q") is None
    assert rr.load("s1", "u1", "另一个问题") is None


# ============================================
# 待授权计划（计划后、执行前授权 HITL 的续跑载体）
# ============================================
PENDING_STEPS = [{"tool": "search_knowledge", "args": {"query": "失眠"}, "purpose": "检索"},
                 {"tool": "web_search", "args": {"query": "失眠 运动"}, "purpose": "联网"}]


def test_pending_plan_roundtrip_take_once(mem_db):
    assert rr.save_pending_plan(session_id="s1", user_id="u1", query="规划方案",
                                steps=PENDING_STEPS) is True
    # 归一化命中；读后即删（一次性，避免重复续跑旧计划）
    assert rr.take_pending_plan("s1", "u1", " 规划方案 ") == PENDING_STEPS
    assert rr.take_pending_plan("s1", "u1", "规划方案") is None
    assert db.query("SELECT * FROM chat_pending_plans") == []


def test_pending_plan_upsert_expired_and_purge(mem_db):
    rr.save_pending_plan(session_id="s1", user_id="u1", query="q", steps=PENDING_STEPS)
    rr.save_pending_plan(session_id="s1", user_id="u1", query="q",
                         steps=[{"tool": "web_search", "args": {"query": "新"}}])
    rows = db.query("SELECT * FROM chat_pending_plans")
    assert len(rows) == 1  # 同键 upsert 单行
    db.execute("UPDATE chat_pending_plans SET expires_at=?",
               ((datetime.now() - timedelta(seconds=1)).isoformat(),))
    assert rr.take_pending_plan("s1", "u1", "q") is None  # 过期回落重新规划
    # purge 同时清理快照与待授权计划两张表
    rr.save(session_id="s1", user_id="u1", query="q", kb_results=KB)
    rr.save_pending_plan(session_id="s1", user_id="u1", query="q2", steps=PENDING_STEPS)
    db.execute("UPDATE chat_retrieval_snapshots SET expires_at=?",
               ((datetime.now() - timedelta(seconds=1)).isoformat(),))
    db.execute("UPDATE chat_pending_plans SET expires_at=?",
               ((datetime.now() - timedelta(seconds=1)).isoformat(),))
    assert rr.purge_expired() == 2
    assert db.query("SELECT * FROM chat_pending_plans") == []


def test_pending_plan_guest_and_empty_not_saved(mem_db):
    assert rr.save_pending_plan(session_id="s1", user_id="guest", query="q",
                                steps=PENDING_STEPS) is False
    assert rr.save_pending_plan(session_id="s1", user_id="u1", query="q", steps=[]) is False
    assert rr.take_pending_plan("s1", "guest", "q") is None
    assert db.query("SELECT * FROM chat_pending_plans") == []


# ============================================
# Agent 复用（mock LLM / 检索 / 重排）
# ============================================
class ScriptedLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.messages = []

    async def chat(self, messages, stream=True):
        self.messages.append(list(messages))
        text = self._responses.pop(0) if self._responses else ""
        if text:
            mid = max(1, len(text) // 2)
            yield text[:mid]
            yield text[mid:]


def _setup(monkeypatch, responses, *, forbid_retrieval=False):
    llm = ScriptedLLM(responses)
    monkeypatch.setattr(chat_agent.LLMFactory, "create_for_role", lambda *a, **k: llm)
    monkeypatch.setattr(chat_agent.vector_store, "result_key", lambda t, m: t)
    calls = {"retrieve": 0, "format": 0, "save": [], "skip": []}

    def _retrieve(q, u):
        calls["retrieve"] += 1
        if forbid_retrieval:
            raise AssertionError("命中快照后不应再次知识检索")
        return list(KB)

    def _format(q, r):
        calls["format"] += 1
        if forbid_retrieval:
            raise AssertionError("命中快照后不应重跑重排序")
        return SNAPSHOT["context_text"], list(SOURCES)

    monkeypatch.setattr(chat_agent.knowledge_service, "retrieve_for_user", _retrieve)
    monkeypatch.setattr(chat_agent.knowledge_service, "format_knowledge_context", _format)
    monkeypatch.setattr(chat_agent.knowledge_service, "build_knowledge_prompt",
                        lambda q, c, f: f"Q:{q}|CTX:{c}")
    monkeypatch.setattr(chat_agent.knowledge_service, "build_search_context", lambda r: "联网资料")
    monkeypatch.setattr(chat_agent.retrieval_reuse, "save", lambda **kw: calls["save"].append(kw) or True)
    monkeypatch.setattr(chat_agent.obs, "record_skip", lambda name, reason="": calls["skip"].append(name))

    async def _web(q):
        return []
    monkeypatch.setattr(chat_agent, "web_search", _web)
    return llm, calls


def _run(**kwargs):
    result = {"status": "ok", "answer": "", "sources": [], "search_results": [], "hitl": None, "error": ""}

    async def go():
        events = [ev async for ev in chat_agent.run_chat_agent(result=result, **kwargs)]
        return [json.loads(ev[len("data: "):].strip()) for ev in events], result

    return asyncio.run(go())


def _base(**overrides):
    kwargs = dict(mode="plan_execute", query="失眠怎么调理", user_id="u1", rule_id="",
                  session_id="s1", use_knowledge=True, use_search=True, skip_search_prompt=False,
                  history_ctx=[], memory_profile="", memory_relevant="",
                  system_prompt=[{"role": "system", "content": "sys"}])
    kwargs.update(overrides)
    return kwargs


def test_plan_execute_reuse_skips_retrieval_and_replays_steps(monkeypatch):
    plan = json.dumps({"steps": [{"tool": "web_search", "args": {"query": "失眠 运动"},
                                  "purpose": "补充联网运动建议"}]})
    llm, calls = _setup(monkeypatch, [plan, "结合知识库与联网资料给出方案[来源 1]"], forbid_retrieval=True)
    events, res = _run(**_base(retrieval_reuse_snapshot=dict(SNAPSHOT)))

    assert calls["retrieve"] == 0 and calls["format"] == 0
    assert "knowledge_retrieve" in calls["skip"] and "rerank" in calls["skip"]
    # 复用提示：先下发上一轮知识步骤（已完成·复用），再下发含新步骤的完整清单
    plans = [e for e in events if e["event"] == "plan"]
    assert len(plans) == 2
    assert all(s["reused"] for s in plans[0]["steps"])
    assert plans[1]["steps"][0]["reused"] is True
    assert plans[1]["steps"][1]["tool"] == "web_search"
    # 工具事件：复用步骤只有 end（index 1），新步骤 start/end 序号接续（index 2）
    tools = [e for e in events if e["event"] == "tool"]
    assert [(t["tool"], t["state"], t["index"]) for t in tools] == [
        ("search_knowledge", "end", 1), ("web_search", "start", 2), ("web_search", "end", 2)]
    assert tools[0]["reused"] is True and tools[0]["purpose"] == "检索失眠调理资料"
    # 规划提示词注入复用约束，且不再列出 search_knowledge
    plan_prompt = json.dumps(llm.messages[0], ensure_ascii=False)
    assert "禁止重复知识检索" in plan_prompt and "search_knowledge(query)" not in plan_prompt
    # 来源沿用上一轮编号；过程快照含复用步骤；本轮结束刷新快照
    assert res["status"] == "ok" and res["sources"] == SOURCES
    assert res["agent_process"]["tools"][0]["reused"] is True
    assert calls["save"] and calls["save"][0]["kb_results"] == KB
    assert calls["save"][0]["steps"][0]["purpose"] == "检索失眠调理资料"
    reused_status = [e for e in events if e.get("phase") == "retrieval_reused"]
    assert reused_status and reused_status[0]["sources"] == SOURCES


def test_react_reuse_prefills_context_without_search_tool(monkeypatch):
    llm, calls = _setup(monkeypatch, ['{"action":"final"}', "已结合上一轮知识作答[来源 1]"],
                        forbid_retrieval=True)
    events, res = _run(**_base(mode="react", retrieval_reuse_snapshot=dict(SNAPSHOT)))
    assert calls["retrieve"] == 0 and calls["format"] == 0
    decision_prompt = json.dumps(llm.messages[0], ensure_ascii=False)
    assert "禁止重复知识检索" in decision_prompt and "search_knowledge(query)" not in decision_prompt
    assert res["status"] == "ok" and res["sources"] == SOURCES
    assert "retrieval_reused" in [e.get("phase") for e in events if e["event"] == "status"]


def test_no_snapshot_keeps_normal_retrieval(monkeypatch):
    plan = json.dumps({"steps": [{"tool": "search_knowledge", "args": {"query": "失眠"},
                                  "purpose": "检索失眠资料"}]})
    llm, calls = _setup(monkeypatch, [plan, "失眠可调理[来源 1]"])
    events, res = _run(**_base(use_search=False))
    assert calls["retrieve"] == 1 and calls["format"] == 1
    tools = [e for e in events if e["event"] == "tool"]
    assert [(t["state"], t["index"]) for t in tools] == [("start", 1), ("end", 1)]
    assert not any(t.get("reused") for t in tools)
    assert res["sources"] == SOURCES
    assert calls["save"]  # 本轮检索产物写入快照，供下一轮复用


def test_snapshot_ignored_when_knowledge_disabled(monkeypatch):
    llm, calls = _setup(monkeypatch, ['{"steps":[]}', "直接作答"])
    events, res = _run(**_base(use_knowledge=False, retrieval_reuse_snapshot=dict(SNAPSHOT)))
    assert "retrieval_reused" not in [e.get("phase") for e in events if e["event"] == "status"]
    assert not any(e.get("reused") for e in events if e["event"] == "plan")
    assert res["status"] == "ok"


def test_plan_authorization_roundtrip_resume_without_replan(monkeypatch, mem_db):
    """端到端：首轮计划含联网步骤 → 执行前暂停并暂存计划；
    授权重发后取走待授权计划，免重规划直接执行知识+联网步骤。"""
    plan = json.dumps({"steps": [
        {"tool": "search_knowledge", "args": {"query": "失眠"}, "purpose": "检索失眠资料"},
        {"tool": "web_search", "args": {"query": "失眠 运动"}, "purpose": "补充运动建议"}]})
    _setup(monkeypatch, [plan])
    events, res = _run(**_base(use_search=False))
    assert res["status"] == "hitl" and res["hitl"] == "need_search"
    assert "tool" not in [e["event"] for e in events]  # 步骤未执行
    assert db.query("SELECT * FROM chat_pending_plans")  # 计划已暂存
    # 授权续跑：take 读后即删，免重规划（LLM 仅被回答阶段调用一次）
    pending = rr.take_pending_plan("s1", "u1", "失眠怎么调理")
    assert pending and [s["tool"] for s in pending] == ["search_knowledge", "web_search"]
    llm2, calls2 = _setup(monkeypatch, ["结合知识库与联网资料的方案[来源 1]"])
    events2, res2 = _run(**_base(use_search=True, pending_plan_steps=pending))
    assert res2["status"] == "ok" and res2["answer"]
    tools = [e for e in events2 if e["event"] == "tool"]
    assert [t["tool"] for t in tools if t["state"] == "start"] == ["search_knowledge", "web_search"]
    assert "plan" in calls2["skip"]
    assert len(llm2.messages) == 1  # 未重新规划
    assert db.query("SELECT * FROM chat_pending_plans") == []


# ============================================
# 请求标记透传
# ============================================
def test_chat_stream_forwards_reuse_flag(monkeypatch):
    from app.services.chat import chat_service

    seen = {}

    async def _inner(*args, **kwargs):
        seen["args"] = args
        yield 'data: {"event": "done", "session_id": "s1"}\n\n'

    monkeypatch.setattr(chat_service, "_chat_stream_inner", _inner)

    async def go():
        return [ev async for ev in chat_service.chat_stream(
            session_id="s1", query="q", user_id="u1", reuse_retrieval=True)]

    events = asyncio.run(go())
    assert events and seen["args"][-1] is True