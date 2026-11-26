"""静态/动态双轨记忆改造契约测试（Claude Code 设计融合，不出网）。

覆盖：
  S1 静态记忆：三层种子全量加载、默认触发口径常量（10K/5K/10min/80%）；
  S2 短期 transcript：只读流水账全量镜像，压缩链不改写；
  S3 短期 task_state：plan/steps/status 任务进度投影；
  S4 Session Memory 事件触发：工具调用 / 增长 5K / 对话暂停（绕过批门控）；
  S5 Compaction 80% 硬触发：window80 类型、tail 裁剪、edited_by_user 保护；
  S6 extractMemories：整轮 transcript 入 prompt + 已有记忆清单 + hasMemoryWritesSince 去重；
  S7 AutoDream：固定 Prompt 整理动作执行、LLM 失败回退簇合并、PID 并发锁；
  S8 memory_taxonomy_v3 迁移：Managed 并入静态 Project、SESSION.md 按属主迁移、transcript 补写；
  S9 短期记忆文件 API：本人会话限定（越权 404）。
"""
import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core import db
from app.services import memory_store
from app.services import session_summary as ss
from app.services import memory as memory_module
from app.services.memory import MemoryService


@pytest.fixture()
def mem_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    memory_module._surfaced.clear()
    memory_module._manual_write_at.clear()
    memory_module._auto_write_at.clear()
    yield conn
    conn.close()


@pytest.fixture()
def file_db(tmp_path, monkeypatch):
    conn = sqlite3.connect(str(tmp_path / "app.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    memory_module._surfaced.clear()
    memory_module._manual_write_at.clear()
    memory_module._auto_write_at.clear()
    yield conn
    conn.close()


class FakeLLM:
    last_prompt = ""

    def __init__(self, reply="[]"):
        self.reply = reply
        self.calls = 0

    async def chat(self, messages, stream=False, **kwargs):
        FakeLLM.last_prompt = messages[-1]["content"]
        self.calls += 1
        yield self.reply


def _patch_llm(monkeypatch, reply="[]"):
    llm = FakeLLM(reply)
    monkeypatch.setattr("app.core.llm.LLMFactory.create_for_role", lambda *a, **k: llm)
    return llm


def _make_session(n=10, content_len=60, user_id="u1", sid="s1"):
    msgs = [
        SimpleNamespace(message_id=f"m{i}", role="user" if i % 2 == 0 else "assistant",
                        content=(f"消息{i}：" + "养" * max(0, content_len - 4)),
                        created_at=datetime.now().isoformat())
        for i in range(n)
    ]
    return SimpleNamespace(session_id=sid, user_id=user_id, messages=msgs,
                           title="测试会话", created_at=datetime.now().isoformat(),
                           updated_at=datetime.now().isoformat())


# ============================================
# S1 静态记忆与默认触发口径
# ============================================

def test_default_trigger_constants():
    """新触发口径：首次 10K / 增长 5K / 暂停 10min / Compaction 80%。"""
    assert ss.CONTEXT_COMPACT_TRIGGER_TOKENS == 10000
    assert ss.SESSION_MEMORY_GROWTH_TOKENS == 5000
    assert ss.SESSION_MEMORY_PAUSE_MINUTES == 10
    assert ss.COMPACT_WINDOW_RATIO == 0.8
    assert ss.compaction_window_threshold() == int(ss.settings.CHAT_CONTEXT_WINDOW * 0.8)


def test_static_full_load_for_guest(file_db):
    """静态三层全量加载：游客也注入全局规矩（叠加层才按用户/KB 隔离）。"""
    msgs = memory_store.static_layer_messages("guest", "")
    assert [m["segment"] for m in msgs] == [
        "memory_static_user", "memory_static_project", "memory_static_local"]
    assert all(m["content"] for m in msgs)


# ============================================
# S2 transcript 只读流水账
# ============================================

def test_transcript_full_mirror_and_compaction_immutable(file_db, monkeypatch):
    """transcript 全量不丢细节；压缩（摘要替换历史）不改写流水账。"""
    _patch_llm(monkeypatch, "1.意图：测试\n2.结论：无\n3.约束：无\n4.未决：无\n5.下一步：无")
    session = _make_session(n=10)
    assert memory_store.write_transcript(session)
    p = memory_store.transcript_path("s1", "u1")
    before = p.read_text(encoding="utf-8")
    for i in range(10):
        assert f"消息{i}" in before
    assert "message_count: 10" in before
    # 强制压缩后 transcript 原文不变（压缩只改读时投影与 summary.md）
    asyncio.run(ss.maybe_summarize(session, force=True))
    assert p.read_text(encoding="utf-8") == before
    assert (p.parent / "summary.md").is_file()


# ============================================
# S3 task_state 任务进度投影
# ============================================

def test_task_state_projects_plan_and_steps(file_db):
    session = _make_session(n=4)
    assistant = SimpleNamespace(agent_process={
        "status": "ok", "intent": "知识库问答",
        "plan": [{"title": "检索知识", "status": "done"}, {"title": "生成回答", "status": "done"}],
        "steps": [{"tool": "kb_retrieve", "status": "ok"}],
    })
    assert memory_store.write_task_state(session, assistant)
    text = memory_store.task_state_path("s1", "u1").read_text(encoding="utf-8")
    assert "会话目标" in text and "计划步骤" in text and "检索知识" in text
    assert "执行轨迹" in text and "kb_retrieve" in text


# ============================================
# S4 Session Memory 事件触发
# ============================================

def _seed_summary(monkeypatch, session):
    _patch_llm(monkeypatch, "1.意图：测试\n2.结论：无\n3.约束：无\n4.未决：无\n5.下一步：无")
    asyncio.run(ss.maybe_summarize(session, force=True))
    assert ss.get_summary(session.session_id) is not None


def test_tool_call_triggers_summary_update(mem_db, monkeypatch):
    """有工具调用 → 即使无增长/未暂停也更新 summary.md（事件触发绕过批门控）。"""
    session = _make_session(n=10)
    _seed_summary(monkeypatch, session)
    monkeypatch.setattr(ss, "SESSION_MEMORY_GROWTH_TOKENS", 10 ** 9)
    monkeypatch.setattr(ss, "SESSION_MEMORY_PAUSE_MINUTES", 10 ** 9)
    monkeypatch.setattr(ss, "CONTEXT_COMPACT_TRIGGER_TOKENS", 10 ** 9)
    stats = asyncio.run(ss.maybe_summarize(session, turn_has_tools=True))
    assert stats is not None and stats["compact_kind"] == "auto"


def test_growth_triggers_summary_update(mem_db, monkeypatch):
    """上下文增长 ≥5K（此处缩为 1 token）→ 触发更新。"""
    session = _make_session(n=10)
    _seed_summary(monkeypatch, session)
    monkeypatch.setattr(ss, "SESSION_MEMORY_GROWTH_TOKENS", 1)
    monkeypatch.setattr(ss, "SESSION_MEMORY_PAUSE_MINUTES", 10 ** 9)
    monkeypatch.setattr(ss, "CONTEXT_COMPACT_TRIGGER_TOKENS", 10 ** 9)
    session.messages.extend(_make_session(n=2, sid="s1").messages)
    stats = asyncio.run(ss.maybe_summarize(session))
    assert stats is not None


def test_pause_triggers_summary_update(mem_db, monkeypatch):
    """对话暂停 ≥10min（此处置 0 立即达标）→ 触发更新。"""
    session = _make_session(n=10)
    _seed_summary(monkeypatch, session)
    monkeypatch.setattr(ss, "SESSION_MEMORY_GROWTH_TOKENS", 10 ** 9)
    monkeypatch.setattr(ss, "SESSION_MEMORY_PAUSE_MINUTES", 0)
    monkeypatch.setattr(ss, "CONTEXT_COMPACT_TRIGGER_TOKENS", 10 ** 9)
    stats = asyncio.run(ss.maybe_summarize(session))
    assert stats is not None


def test_no_event_no_growth_no_update(mem_db, monkeypatch):
    """无事件/无增长/未达阈值 → 不更新（批门控语义保留）。"""
    session = _make_session(n=10)
    _seed_summary(monkeypatch, session)
    monkeypatch.setattr(ss, "SESSION_MEMORY_GROWTH_TOKENS", 10 ** 9)
    monkeypatch.setattr(ss, "SESSION_MEMORY_PAUSE_MINUTES", 10 ** 9)
    monkeypatch.setattr(ss, "CONTEXT_COMPACT_TRIGGER_TOKENS", 10 ** 9)
    assert asyncio.run(ss.maybe_summarize(session)) is None


# ============================================
# S5 Compaction 80% 硬触发
# ============================================

def test_window80_hard_trigger_and_tail_trim(mem_db, monkeypatch):
    """上下文 ≥80% 窗口 → window80 硬触发；读时投影 tail 仅留最近 6 条。"""
    _patch_llm(monkeypatch, "1.意图：测试\n2.结论：无\n3.约束：无\n4.未决：无\n5.下一步：无")
    monkeypatch.setattr(ss, "COMPACT_WINDOW_RATIO", 0.0001)  # 阈值≈13 token，必达硬触发
    session = _make_session(n=12)
    stats = asyncio.run(ss.maybe_summarize(session))
    assert stats is not None and stats["compact_kind"] == "window80"
    ctx = ss.build_context_messages(session, max_recent=12)
    assert "【此前对话摘要】" in ctx[0]["content"]
    assert len(ctx) == 1 + ss.KEEP_RECENT_MESSAGES


def test_window80_respects_user_edited_summary(mem_db, monkeypatch):
    """用户手编摘要受保护：80% 硬触发也不覆盖（force 除外）。"""
    _patch_llm(monkeypatch, "1.意图：测试\n2.结论：无\n3.约束：无\n4.未决：无\n5.下一步：无")
    session = _make_session(n=10)
    asyncio.run(ss.maybe_summarize(session, force=True))
    db.execute("UPDATE chat_session_summaries SET edited_by_user=1 WHERE session_id='s1'")
    monkeypatch.setattr(ss, "COMPACT_WINDOW_RATIO", 0.0001)
    assert asyncio.run(ss.maybe_summarize(session)) is None


# ============================================
# S6 extractMemories 整轮扫描与去重
# ============================================

def test_turn_transcript_and_existing_memories_in_prompt(mem_db, monkeypatch):
    llm = _patch_llm(monkeypatch, "[]")
    MemoryService.add_memory("u1", "用户对花生过敏", memory_type="user")
    memory_module._auto_write_at.clear()  # 隔离写入互斥，聚焦 prompt 契约
    asyncio.run(MemoryService.extract_from_turn(
        "u1", "s1", "今天天气如何", "晴天", turn_transcript="用户：今天天气如何\n助手：晴天"))
    assert llm.calls == 1
    assert "本轮 transcript" in FakeLLM.last_prompt
    assert "今天天气如何" in FakeLLM.last_prompt
    assert "已有记忆" in FakeLLM.last_prompt and "花生过敏" in FakeLLM.last_prompt


def test_has_memory_writes_since_filters_duplicate_add(mem_db, monkeypatch):
    """刚写入过（hasMemoryWritesSince）且内容高度重复的 add 候选被过滤。"""
    MemoryService.add_memory("u1", "用户对海鲜过敏，饮食需避开海鲜", memory_type="user")
    memory_module.note_auto_write("u1")
    reply = json.dumps([{"content": "用户对海鲜过敏，饮食需避开海鲜", "importance": 0.9,
                         "type": "user", "action": "add"}], ensure_ascii=False)
    _patch_llm(monkeypatch, reply)
    # 非显式指令轮：仅 LLM 层产出候选，验证去重过滤（显式「记住」另走必落库信号层）
    res = asyncio.run(MemoryService.extract_from_turn("u1", "s1", "今天天气如何", "晴天"))
    assert len(MemoryService.list_memories("u1")) == 1  # 未重复新增
    assert res["add"] == 0


# ============================================
# S7 AutoDream 固定 Prompt 与并发锁
# ============================================

class _FakeRuntimeConfig:
    """dict 背书的 runtime_config 桩（AutoDream 门控状态隔离）。"""

    def __init__(self):
        self.store = {}

    def get(self, key, default=None):
        return self.store.get(key, default)

    def set(self, key, value):
        self.store[key] = value


def _arm_consolidation(mem_db, monkeypatch, n_sessions=5):
    cfg = _FakeRuntimeConfig()
    last = time.time() - 25 * 3600
    cfg.set("memory_consolidation_state", {"u1": {"last_at": last, "sessions_at": 0}})
    monkeypatch.setattr("app.services.config_manager.runtime_config", cfg)
    after = datetime.fromtimestamp(last + 3600).isoformat()
    for i in range(n_sessions):
        mem_db.execute(
            "INSERT INTO chat_sessions (session_id, user_id, title, created_at, updated_at)"
            " VALUES (?,?,?,?,?)", (f"sd{i}", "u1", "会话", after, after))
        mem_db.execute(
            "INSERT INTO chat_messages (message_id, session_id, seq, role, content, created_at)"
            " VALUES (?,?,?,?,?,?)", (f"md{i}", f"sd{i}", 0, "user", "你好", after))
    mem_db.commit()
    return cfg


def test_auto_dream_executes_fixed_prompt_actions(mem_db, monkeypatch):
    """门控达标 → 固定 Prompt 整理代理输出 merge 动作被执行，落 dream 运行事件。"""
    a = MemoryService.add_memory("u1", "用户喜欢清淡饮食少油", memory_type="user", dedup=False)
    b = MemoryService.add_memory("u1", "用户喜欢清淡饮食少盐", memory_type="user", dedup=False)
    reply = json.dumps([{"action": "merge", "ids": [a["id"], b["id"]],
                         "content": "用户长期偏好清淡饮食（少油少盐）", "importance": 0.8}],
                       ensure_ascii=False)
    llm = _patch_llm(monkeypatch, reply)
    _arm_consolidation(mem_db, monkeypatch)
    res = asyncio.run(MemoryService.consolidate_if_due("u1"))
    assert res["ran"] and res["reason"] == "ok" and res["actions"] == 1
    assert "AutoDream" in FakeLLM.last_prompt and "现有记忆" in FakeLLM.last_prompt
    rows = MemoryService.list_memories("u1")
    assert len(rows) == 1 and "少油少盐" in rows[0]["content"]
    dreams = db.query("SELECT detail FROM memory_write_events WHERE action='dream'")
    assert len(dreams) == 1


def test_auto_dream_fallback_to_cluster_merge(mem_db, monkeypatch):
    """LLM 不可用 → 回退逐类型簇合并，门控状态仍推进。"""
    MemoryService.add_memory("u1", "用户喜欢晨跑锻炼", memory_type="user")

    def _raise(*a, **k):
        raise RuntimeError("memory_extract 角色无可用上游")

    monkeypatch.setattr("app.core.llm.LLMFactory.create_for_role", _raise)
    cfg = _arm_consolidation(mem_db, monkeypatch)
    res = asyncio.run(MemoryService.consolidate_if_due("u1"))
    assert res["ran"] and res["fallback"] is True
    assert cfg.store["memory_consolidation_state"]["u1"]["last_at"] > 0


def test_consolidate_lock_pid_file(file_db):
    """.consolidate-lock PID 文件锁：他进程存活 PID 持锁 → 拒绝；释放后恢复。"""
    p = memory_store.consolidate_lock_path("u1")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("1", encoding="utf-8")  # PID 1 存活且非本进程
    assert memory_store.acquire_consolidate_lock("u1") is False
    p.write_text(str(os.getpid()), encoding="utf-8")
    assert memory_store.acquire_consolidate_lock("u1") is True  # 本进程重入放行
    memory_store.release_consolidate_lock("u1")
    assert not p.exists()


# ============================================
# S8 memory_taxonomy_v3 迁移
# ============================================

def test_v3_migration_managed_session_and_transcript(file_db, tmp_path):
    base = tmp_path / "memory"
    now = datetime.now().isoformat()
    # 旧 Managed 自定义内容
    (base / "managed").mkdir(parents=True, exist_ok=True)
    (base / "managed" / "MANAGED.md").write_text("# 平台记忆策略（Managed）\n\n自定义平台策略XYZ\n",
                                                 encoding="utf-8")
    # 旧会话摘要文件 + 会话/消息/摘要行
    (base / "sessions" / "sx").mkdir(parents=True, exist_ok=True)
    (base / "sessions" / "sx" / "SESSION.md").write_text(
        "---\ncovered_until_message_id: m1\n---\n旧摘要内容ABC\n", encoding="utf-8")
    file_db.execute("INSERT INTO chat_sessions (session_id, user_id, title, created_at, updated_at)"
                    " VALUES ('sx','u1','旧会话',?,?)", (now, now))
    file_db.execute("INSERT INTO chat_messages (message_id, session_id, seq, role, content, created_at)"
                    " VALUES ('mx0','sx',0,'user','旧会话提问内容',?)", (now,))
    file_db.execute("INSERT INTO chat_session_summaries (session_id, user_id, summary, created_at, updated_at)"
                    " VALUES ('sx','u1','旧摘要内容ABC',?,?)", (now, now))
    file_db.execute("DELETE FROM schema_migrations WHERE version='memory_taxonomy_v3'")
    file_db.commit()
    db.init_db(file_db)
    # Managed 并入静态 Project 后删除
    assert not (base / "managed" / "MANAGED.md").exists()
    assert "自定义平台策略XYZ" in (base / "static" / "PROJECT.md").read_text(encoding="utf-8")
    # SESSION.md 按属主迁移 + transcript 补写
    assert "旧摘要内容ABC" in (base / "users" / "u1" / "sessions" / "sx" / "summary.md").read_text(encoding="utf-8")
    assert not (base / "sessions" / "sx" / "SESSION.md").exists()
    tp = base / "users" / "u1" / "sessions" / "sx" / "transcript.md"
    assert tp.is_file() and "旧会话提问内容" in tp.read_text(encoding="utf-8")


# ============================================
# S9 短期记忆文件 API（本人会话限定）
# ============================================

def test_session_files_api_owner_only(file_db):
    from fastapi import HTTPException

    from app.api.me import my_memory_session_files

    now = datetime.now().isoformat()
    file_db.execute("INSERT INTO chat_sessions (session_id, user_id, title, created_at, updated_at)"
                    " VALUES ('s1','u1','测试',?,?)", (now, now))
    file_db.commit()
    got = asyncio.run(my_memory_session_files("s1", {"id": "u1", "role": "user"}))
    assert got["overview"]["session_id"] == "s1"
    assert {m["file"] for m in got["overview"]["modules"]} == {
        "transcript.md", "summary.md", "task_state.md"}
    with pytest.raises(HTTPException) as ei:
        asyncio.run(my_memory_session_files("s1", {"id": "u2", "role": "user"}))
    assert ei.value.status_code == 404