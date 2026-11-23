"""五层记忆架构回归测试（内存 DB，不出网）。

覆盖 memory_five_layer_v1 改造（对应 docs/记忆模块五层架构设计.md）：
  L1  指令层：get/set/敏感词拦截/字符预算截断/分层注入顺序（wiki _memory_system_messages）/API；
  L2  自动记忆：置信度递增与上限、召回评分因子、新鲜度文案、What-NOT-to-save prompt、
      巩固门控（time_gate/session_gate/ok）、主副互斥；
  L3  会话笔记：get/update + 归属校验、用户编辑摘要不被自动压缩覆盖（force 仍可）；
  L4  助手记忆：行级隔离（跨用户/跨域）、越权拒绝、API CRUD、context_text 预算；
  L5  相关记忆：会话级去重（surfaced/mark/reset）+ compact 重置；
  概览：usage_overview 平均置信度。
"""
import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core import db
from app.services import memory as memory_module
from app.services import session_summary as ss
from app.services import user_instructions
from app.services.agent_memory import AgentMemoryService, agent_memory_service
from app.services.memory import MemoryService, memory_line


@pytest.fixture()
def mem_db(monkeypatch):
    """独立内存数据库：避免污染本地开发库。"""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    # 清理模块级会话去重/主副互斥状态，避免跨用例串味
    memory_module._surfaced.clear()
    memory_module._manual_write_at.clear()
    yield conn
    conn.close()


class FakeLLM:
    """返回固定文本的 LLM 桩（async 生成器协议与真实 chat 一致）。"""

    last_prompt = ""

    def __init__(self, reply: str = "[]"):
        self.reply = reply

    async def chat(self, messages, stream=False, **kwargs):
        FakeLLM.last_prompt = messages[-1]["content"]
        yield self.reply


def _patch_llm(monkeypatch, reply="[]"):
    llm = FakeLLM(reply)
    monkeypatch.setattr("app.core.llm.LLMFactory.create_for_role", lambda *a, **k: llm)
    return llm


def _set_fields(conn, memory_id, table="user_memories", **fields):
    for col, val in fields.items():
        conn.execute(f"UPDATE {table} SET {col} = ? WHERE id = ?", (val, memory_id))
    conn.commit()


def _make_session(n_messages, content_len=60, user_id="u1", session_id="s1"):
    msgs = [
        SimpleNamespace(
            message_id=f"m{i}",
            role="user" if i % 2 == 0 else "assistant",
            content=(f"消息{i}：" + "养" * max(0, content_len - 4)),
        )
        for i in range(n_messages)
    ]
    return SimpleNamespace(session_id=session_id, user_id=user_id, messages=msgs)


def _insert_session(conn, session_id="s1", user_id="u1"):
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO chat_sessions (session_id, user_id, title, created_at, updated_at)"
        " VALUES (?,?,?,?,?)",
        (session_id, user_id, "测试会话", now, now),
    )
    conn.commit()


class FakeRuntimeConfig:
    """dict 背书的 runtime_config 桩（巩固门控状态隔离）。"""

    def __init__(self):
        self.store = {}

    def get(self, key, default=None):
        return self.store.get(key, default)

    def set(self, key, value):
        self.store[key] = value


# ============================================
# L1 指令层
# ============================================

def test_instruction_set_get_and_messages(mem_db):
    res = user_instructions.set_instruction("u1", "回答先给结论再给依据")
    assert res and res["content"] == "回答先给结论再给依据"
    assert user_instructions.get_instruction("u1") == "回答先给结论再给依据"
    msgs = user_instructions.instruction_messages("u1")
    assert len(msgs) == 1 and msgs[0]["role"] == "system"
    assert "回答先给结论再给依据" in msgs[0]["content"]
    assert "优先级高于系统默认约定" in msgs[0]["content"]


def test_instruction_sensitive_rejected(mem_db):
    assert user_instructions.set_instruction("u1", "我的密码是 abc123") is None
    assert user_instructions.get_instruction("u1") == ""


def test_instruction_budget_truncation(mem_db):
    user_instructions.set_instruction("u1", "养" * 800)
    assert len(user_instructions.get_instruction("u1")) == user_instructions.MAX_INSTRUCTION_CHARS == 500


def test_instruction_guest_and_empty(mem_db):
    assert user_instructions.get_instruction("guest") == ""
    assert user_instructions.get_instruction("") == ""
    assert user_instructions.instruction_messages("u1") == []  # 无指令不注入
    assert user_instructions.format_instruction_messages("  ") == []


def test_instruction_api(mem_db):
    from fastapi import HTTPException

    from app.api.instructions import (
        InstructionUpdateRequest, get_my_instructions, update_my_instructions,
    )

    user = {"id": "u1"}
    got = asyncio.run(get_my_instructions(user))
    assert got["content"] == "" and got["max_chars"] == 500
    res = asyncio.run(update_my_instructions(InstructionUpdateRequest(content="涉及剂量务必标注来源"), user))
    assert res["success"] and res["content"] == "涉及剂量务必标注来源"
    with pytest.raises(HTTPException) as ei:
        asyncio.run(update_my_instructions(InstructionUpdateRequest(content="身份证 110101"), user))
    assert ei.value.status_code == 400


def test_wiki_memory_injection_order_l1_l2_l4(mem_db):
    """wiki 助手记忆注入顺序：L1 指令 → L2 画像 → L4 助手域（真实函数验证）。"""
    from app.services.wiki_assistant import AssistantContext, _memory_system_messages

    user_instructions.set_instruction("u1", "分析文档先给结论")
    MemoryService.add_memory("u1", "用户长期关注睡眠质量改善", importance=0.9)
    agent_memory_service.add_memory("u1", "wiki_assistant", "常分析睡眠与穴位主题文档", importance=0.9)
    msgs = _memory_system_messages(AssistantContext(user_id="u1"), "睡眠文档分析")
    assert len(msgs) == 3
    assert "用户个人指令" in msgs[0]["content"] and "分析文档先给结论" in msgs[0]["content"]
    assert "用户画像" in msgs[1]["content"]
    assert "助手记忆" in msgs[2]["content"] and "睡眠与穴位" in msgs[2]["content"]
    # 游客不注入
    assert _memory_system_messages(AssistantContext(user_id="guest"), "q") == []


def test_segment_label_registered():
    """成对登记不变量：note_segment('user_instructions') 必有 SEGMENT_LABELS 对应。"""
    from app.core.observability import SEGMENT_LABELS

    assert SEGMENT_LABELS.get("user_instructions") == "用户指令"


# ============================================
# L2 自动记忆增强
# ============================================

def test_confidence_increments_on_dedup_and_caps(mem_db):
    base = "用户对花生过敏，饮食需避免花生"
    first = MemoryService.add_memory("u1", base, memory_type="preference")
    assert first and not first.get("updated")
    row = MemoryService.list_memories("u1")[0]
    assert row["confidence"] == pytest.approx(1.0)
    # 反复确认：每次查重命中 +1，直到上限 5 后不再增长
    for expected in (2.0, 3.0, 4.0, 5.0, 5.0):
        MemoryService.add_memory("u1", base, memory_type="preference")
        row = MemoryService.list_memories("u1")[0]
        assert row["confidence"] == pytest.approx(expected)
    assert len(MemoryService.list_memories("u1")) == 1  # 始终一条，不堆积


def test_confidence_score_factor_ranks_higher(mem_db):
    """关键词/重要度/类型/新鲜度相同：置信度更高者排序更靠前（0.1×confidence 因子）。"""
    low = MemoryService.add_memory("u1", "用户喜欢喝绿茶养生", memory_type="fact", importance=0.5)
    high = MemoryService.add_memory("u1", "用户喜欢喝红茶养胃", memory_type="fact", importance=0.5)
    now = datetime.now().isoformat()
    _set_fields(mem_db, low["id"], last_used_at=now, confidence=1.0)
    _set_fields(mem_db, high["id"], last_used_at=now, confidence=5.0)
    ranked = MemoryService.retrieve_relevant("u1", "用户喜欢喝什么茶")
    assert ranked[0]["id"] == high["id"]


def test_freshness_warning_on_stale_memory(mem_db):
    m = MemoryService.add_memory("u1", "用户偏好清淡饮食")
    # 新鲜记忆（updated_at=now）：无时点观察提醒（取完整行，add_memory 返回不含时间戳）
    fresh_row = MemoryService.list_memories("u1")[0]
    assert "时点观察" not in memory_line(fresh_row)
    stale = (datetime.now() - timedelta(days=45)).isoformat()
    _set_fields(mem_db, m["id"], updated_at=stale, created_at=stale)
    row = MemoryService.list_memories("u1")[0]
    line = memory_line(row)
    assert "时点观察" in line and "45 天前" in line and "以用户最新表述为准" in line


def test_extract_prompt_has_what_not_to_save(mem_db, monkeypatch):
    llm = _patch_llm(monkeypatch, "[]")
    asyncio.run(MemoryService.extract_from_turn("u1", "s1", "今天天气如何", "晴天"))
    assert "What-NOT-to-save" in FakeLLM.last_prompt
    assert "一次性提问" in FakeLLM.last_prompt
    assert "不显而易见" in FakeLLM.last_prompt


def test_manual_write_mutex_skips_extraction(mem_db, monkeypatch):
    """主副互斥（新口径）：近期手动写入后跳过 LLM 抽取层（LLM 不被调用）。

    互斥范围已从「整轮抽取」收窄为「仅 LLM 层」：用户显式「记住/忘记…」走确定性
    信号层，不受互斥拦截（否则显式指令会被静默丢弃）；该行为由
    test_memory_extract_pipeline 覆盖。本例用无显式信号的一次性查询验证互斥门控。
    """
    _patch_llm(monkeypatch, json.dumps([
        {"content": "用户喜欢徒步", "importance": 0.6, "type": "fact", "action": "add", "target_hint": ""},
    ], ensure_ascii=False))
    memory_module.note_manual_write("u1")
    prompt_before = FakeLLM.last_prompt
    asyncio.run(MemoryService.extract_from_turn("u1", "s1", "今天天气如何", "晴天"))
    assert MemoryService.list_memories("u1") == []  # LLM 层被互斥跳过
    assert FakeLLM.last_prompt == prompt_before  # LLM 未被调用
    # 窗口过期后恢复抽取
    memory_module._manual_write_at["u1"] = time.time() - (memory_module._MANUAL_WRITE_MUTEX_SECONDS + 10)
    asyncio.run(MemoryService.extract_from_turn("u1", "s1", "今天天气如何", "晴天"))
    assert len(MemoryService.list_memories("u1")) == 1


def test_consolidate_time_gate(mem_db, monkeypatch):
    cfg = FakeRuntimeConfig()
    cfg.set("memory_consolidation_state", {"u1": {"last_at": time.time(), "sessions_at": 0}})
    monkeypatch.setattr("app.services.config_manager.runtime_config", cfg)
    res = asyncio.run(MemoryService.consolidate_if_due("u1"))
    assert res["ran"] is False and res["reason"] == "time_gate"


def test_consolidate_session_gate(mem_db, monkeypatch):
    cfg = FakeRuntimeConfig()
    # 距上次 25h（超时间门控），但无新增会话 → 会话门控拦截
    cfg.set("memory_consolidation_state", {"u1": {"last_at": time.time() - 25 * 3600, "sessions_at": 0}})
    monkeypatch.setattr("app.services.config_manager.runtime_config", cfg)
    res = asyncio.run(MemoryService.consolidate_if_due("u1"))
    assert res["ran"] is False and res["reason"] == "session_gate"


def test_consolidate_runs_when_both_gates_pass(mem_db, monkeypatch):
    cfg = FakeRuntimeConfig()
    last = time.time() - 25 * 3600
    cfg.set("memory_consolidation_state", {"u1": {"last_at": last, "sessions_at": 0}})
    monkeypatch.setattr("app.services.config_manager.runtime_config", cfg)
    _patch_llm(monkeypatch, "[]")  # 无记忆簇时 _merge_cluster 早退，不调用 LLM
    # 新增 5 个会话（created_at 晚于上次巩固）
    after = datetime.fromtimestamp(last + 3600).isoformat()
    for i in range(5):
        mem_db.execute(
            "INSERT INTO chat_sessions (session_id, user_id, title, created_at, updated_at)"
            " VALUES (?,?,?,?,?)",
            (f"sn{i}", "u1", "新会话", after, after),
        )
    mem_db.commit()
    res = asyncio.run(MemoryService.consolidate_if_due("u1"))
    assert res["ran"] is True and res["reason"] == "ok" and res["new_sessions"] == 5
    # 状态推进：last_at 更新
    assert cfg.store["memory_consolidation_state"]["u1"]["last_at"] > last


def test_consolidate_guest_noop(mem_db):
    res = asyncio.run(MemoryService.consolidate_if_due("guest"))
    assert res["ran"] is False and res["reason"] == "no_user"


def test_usage_overview_avg_confidence(mem_db):
    MemoryService.add_memory("u1", "用户喜欢晨跑锻炼", importance=0.6)
    MemoryService.add_memory("u1", "用户正在备考营养师", importance=0.6)
    ov = MemoryService.usage_overview("u1")
    assert ov["total"] == 2 and "avg_confidence" in ov
    assert ov["avg_confidence"] == pytest.approx(1.0)
    assert MemoryService.usage_overview("guest")["avg_confidence"] == 0.0


# ============================================
# L3 会话笔记
# ============================================

def test_session_note_get_empty_and_update(mem_db):
    empty = ss.get_session_note("s1")
    assert empty["note"] == "" and empty["edited_by_user"] == 0
    _insert_session(mem_db, "s1", "u1")
    res = ss.update_session_note("s1", "u1", "1.意图：改善睡眠\n2.结论：无")
    assert res and res["edited_by_user"] == 1
    got = ss.get_session_note("s1")
    assert got["note"].startswith("1.意图") and got["edited_by_user"] == 1


def test_session_note_ownership_check(mem_db):
    _insert_session(mem_db, "s1", "u1")
    assert ss.update_session_note("s1", "u2", "篡改") is None  # 非归属
    assert ss.update_session_note("missing", "u1", "x") is None  # 会话不存在
    assert ss.update_session_note("s1", "u1", "我的笔记") is not None


def test_session_note_budget_truncation(mem_db):
    _insert_session(mem_db, "s1", "u1")
    res = ss.update_session_note("s1", "u1", "养" * 3000)
    assert len(res["note"]) == ss.MAX_SESSION_NOTE_CHARS == 2000


def test_edited_note_not_overwritten_by_auto_compact(mem_db, monkeypatch):
    """用户编辑过的摘要不被自动压缩覆盖；手动 force 仍可重新生成。"""
    _insert_session(mem_db, "s1", "u1")
    ss.update_session_note("s1", "u1", "用户手写笔记")
    _patch_llm(monkeypatch, "1.意图：自动摘要\n2.结论：无\n3.约束：无\n4.未决：无\n5.下一步：无")
    monkeypatch.setattr(ss, "CONTEXT_COMPACT_TRIGGER_TOKENS", 100)
    session = _make_session(10, content_len=60)
    # 自动压缩：edited_by_user=1 → 跳过，笔记不变
    assert asyncio.run(ss.maybe_summarize(session)) is None
    assert ss.get_session_note("s1")["note"] == "用户手写笔记"
    # 手动 force：用户显式动作，允许重新生成
    stats = asyncio.run(ss.maybe_summarize(session, force=True))
    assert stats and stats["covered_messages"] == 10 - ss.KEEP_RECENT_MESSAGES
    assert "自动摘要" in ss.get_summary("s1")["summary"]


# ============================================
# L4 助手分域记忆
# ============================================

def test_agent_memory_row_level_isolation(mem_db):
    m = agent_memory_service.add_memory("u1", "wiki_assistant", "常分析睡眠主题文档")
    assert m
    assert len(agent_memory_service.list_memories("u1", "wiki_assistant")) == 1
    assert agent_memory_service.list_memories("u1", "chat") == []  # 跨域隔离
    assert agent_memory_service.list_memories("u2", "wiki_assistant") == []  # 跨用户隔离
    # 越权/跨域删除与更新均失败
    assert agent_memory_service.delete_memory("u2", "wiki_assistant", m["id"]) is False
    assert agent_memory_service.delete_memory("u1", "chat", m["id"]) is False
    assert agent_memory_service.update_memory("u2", "wiki_assistant", m["id"], content="x") is None
    assert agent_memory_service.update_memory("u1", "chat", m["id"], content="x") is None
    # 本域本用户可正常更新/删除
    assert agent_memory_service.update_memory("u1", "wiki_assistant", m["id"], content="常分析穴位主题")
    assert agent_memory_service.delete_memory("u1", "wiki_assistant", m["id"]) is True


def test_agent_memory_invalid_agent_and_sensitive(mem_db):
    assert agent_memory_service.add_memory("u1", "unknown_agent", "内容") is None
    assert agent_memory_service.list_memories("u1", "unknown_agent") == []
    assert agent_memory_service.add_memory("u1", "wiki_assistant", "密码是 abc123") is None


def test_agent_memory_dedup_confidence(mem_db):
    base = "用户偏好分析文档时先给结论再给依据"
    agent_memory_service.add_memory("u1", "wiki_assistant", base, memory_type="preference")
    agent_memory_service.add_memory("u1", "wiki_assistant", base, memory_type="preference")
    rows = agent_memory_service.list_memories("u1", "wiki_assistant")
    assert len(rows) == 1 and rows[0]["confidence"] == pytest.approx(2.0)


def test_agent_context_text_budget(mem_db):
    for i in range(6):
        agent_memory_service.add_memory("u1", "wiki_assistant", f"常分析主题文档领域编号{i}睡眠", importance=0.9)
    text = agent_memory_service.context_text("u1", "wiki_assistant", "睡眠主题文档", budget_chars=50)
    assert len(text) <= 50
    assert agent_memory_service.context_text("u1", "chat", "睡眠") == ""


def test_agent_memory_api_crud_and_isolation(mem_db):
    from fastapi import HTTPException

    from app.api.me import (
        AgentMemoryCreateRequest, AgentMemoryUpdateRequest,
        create_my_agent_memory, delete_my_agent_memory,
        list_my_agent_memories, update_my_agent_memory,
    )

    u1, u2 = {"id": "u1"}, {"id": "u2"}
    res = asyncio.run(create_my_agent_memory(
        AgentMemoryCreateRequest(agent="wiki_assistant", content="分析文档先给结论再给依据"), u1))
    assert res["success"] and res["agent"] == "wiki_assistant"
    mid = res["id"]
    listing = asyncio.run(list_my_agent_memories("wiki_assistant", u1))
    assert len(listing["memories"]) == 1 and listing["storage_limit"] == 50
    # 跨用户编辑/删除 → 404
    with pytest.raises(HTTPException) as ei:
        asyncio.run(update_my_agent_memory(mid, AgentMemoryUpdateRequest(agent="wiki_assistant", content="篡改"), u2))
    assert ei.value.status_code == 404
    with pytest.raises(HTTPException) as ei:
        asyncio.run(delete_my_agent_memory(mid, "wiki_assistant", u2))
    assert ei.value.status_code == 404
    # 未知助手域 → 400
    with pytest.raises(HTTPException) as ei:
        asyncio.run(list_my_agent_memories("unknown", u1))
    assert ei.value.status_code == 400
    # 本人删除成功
    assert asyncio.run(delete_my_agent_memory(mid, "wiki_assistant", u1))["success"]


# ============================================
# L5 会话级去重
# ============================================

def test_surfaced_dedup_and_exclude(mem_db):
    m1 = MemoryService.add_memory("u1", "用户有高血压病史需要控盐")
    m2 = MemoryService.add_memory("u1", "用户血糖偏高需要控制糖分摄入")
    assert memory_module.surfaced_ids("s1") == set()
    memory_module.mark_surfaced("s1", [m1["id"]])
    assert memory_module.surfaced_ids("s1") == {m1["id"]}
    chosen = MemoryService.retrieve_relevant("u1", "高血压控盐怎么做", exclude_ids=list(memory_module.surfaced_ids("s1")))
    assert m1["id"] not in [m["id"] for m in chosen]
    memory_module.reset_surfaced("s1")
    assert memory_module.surfaced_ids("s1") == set()


def test_memory_service_instance_exposes_dedup_methods(mem_db):
    """chat.py/session_summary.py 经 memory_service 实例调用去重方法（锁定接线契约）。"""
    from app.services.memory import memory_service

    memory_service.mark_surfaced("s1", ["a", "b"])
    assert memory_service.surfaced_ids("s1") == {"a", "b"}
    memory_service.reset_surfaced("s1")
    assert memory_service.surfaced_ids("s1") == set()
    memory_service.note_manual_write("u1")
    assert memory_module._manual_write_recent("u1") is True


def test_compact_resets_surfaced(mem_db, monkeypatch):
    """compact 推进摘要覆盖点后重置会话去重集合（允许压缩后合理重注入）。"""
    _patch_llm(monkeypatch, "1.意图：测试\n2.结论：无\n3.约束：无\n4.未决：无\n5.下一步：继续")
    memory_module.mark_surfaced("s1", ["mid-1", "mid-2"])
    assert memory_module.surfaced_ids("s1")
    session = _make_session(10, content_len=60)
    stats = asyncio.run(ss.maybe_summarize(session, force=True))
    assert stats  # 压缩成功
    assert memory_module.surfaced_ids("s1") == set()  # 已重置