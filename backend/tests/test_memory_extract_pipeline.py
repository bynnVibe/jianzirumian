"""记忆抽取管线重构回归（确定性信号层 + LLM 层 + 健壮解析 + 运行事件）。

针对「所有对话都看不到记忆」的根因锁定新契约：
  RC1 抽取零产出：用户显式「记住/忘记…」与自述事实走确定性信号层，
      LLM 不可用/判空时仍必定落库；疑问句守卫防止把提问当自述。
  RC2 失败不可见：每轮必落 action=extract 运行事件（含无产出原因），
      观测/个人面板把「运行」与「写入」拆口径，空态可解释。
  RC4 纠正/遗忘静默失败：update/delete 定位在严格查重未命中时按放宽阈值回退。
  附带：LLM 输出多形态健壮解析、file_path 存量回填（memory_markdown_v2）。
"""
import asyncio
import json
import sqlite3
import time

import pytest

from app.core import db, observability as obs
from app.api import me as api_me
from app.api import observability as api_obs
from app.services import memory as memory_module
from app.services import memory_store
from app.services.memory import (
    MemoryService,
    _parse_extraction_items,
    extract_explicit_signals,
)


@pytest.fixture()
def mem_db(monkeypatch):
    """独立内存数据库（markdown 层不落盘），避免污染本地开发库。"""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    memory_module._surfaced.clear()
    memory_module._manual_write_at.clear()
    yield conn
    conn.close()


@pytest.fixture()
def file_db(tmp_path, monkeypatch):
    """临时文件数据库：markdown 持久层真实落盘（隔离目录内）。"""
    conn = sqlite3.connect(str(tmp_path / "app.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    memory_module._surfaced.clear()
    memory_module._manual_write_at.clear()
    yield conn
    conn.close()


class FakeLLM:
    """返回固定文本的 LLM 桩（async 生成器协议与真实 chat 一致）。"""

    last_prompt = ""

    def __init__(self, reply: str = "[]"):
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


def _patch_llm_unavailable(monkeypatch):
    """角色未绑定上游/初始化失败：create_for_role 抛异常（生产 503 的等价情形）。"""
    def _raise(*a, **k):
        raise RuntimeError("memory_extract 角色无可用上游")

    monkeypatch.setattr("app.core.llm.LLMFactory.create_for_role", _raise)


def _events(action=None):
    sql = "SELECT action, detail, created_at FROM memory_write_events"
    params = ()
    if action:
        sql += " WHERE action = ?"
        params = (action,)
    return db.query(sql + " ORDER BY created_at DESC", params)


# ============================================
# 确定性信号层：显式指令必定落库
# ============================================

def test_explicit_remember_writes_without_llm(mem_db, monkeypatch):
    """LLM 完全不可用时，用户显式「记住：…」仍落库（旧版会被静默丢弃）。"""
    _patch_llm_unavailable(monkeypatch)
    res = asyncio.run(MemoryService.extract_from_turn(
        "u1", "s1", "记住：我对海鲜过敏，以后推荐菜谱要避开", "好的"))
    rows = MemoryService.list_memories("u1")
    assert len(rows) == 1
    assert "海鲜过敏" in rows[0]["content"]
    assert rows[0]["content"].startswith("用户")
    assert rows[0]["importance"] == pytest.approx(0.85)
    assert res["ran"] and res["explicit"] == 1 and res["add"] == 1
    assert res["reason"] == "llm_unavailable"


def test_explicit_remember_bypasses_manual_mutex(mem_db, monkeypatch):
    """主副互斥只拦 LLM 层：刚手动写过记忆，显式「记住」仍必须落库。"""
    llm = _patch_llm(monkeypatch, "[]")
    memory_module.note_manual_write("u1")
    res = asyncio.run(MemoryService.extract_from_turn(
        "u1", "s1", "帮我记一下，我每周游泳三次", "好的"))
    assert llm.calls == 0 and res["reason"] == "manual_mutex"
    assert len(MemoryService.list_memories("u1")) == 1
    assert res["add"] == 1


def test_explicit_forget_deletes_existing(mem_db, monkeypatch):
    """显式遗忘：按触发词之后的描述定位并删除（放宽阈值后不再静默失败）。"""
    _patch_llm(monkeypatch, "[]")
    MemoryService.add_memory("u1", "用户计划三个月减重五公斤", memory_type="goal")
    res = asyncio.run(MemoryService.extract_from_turn(
        "u1", "s1", "忘记我三个月减重五公斤的计划", "好的"))
    assert res["delete"] == 1
    assert MemoryService.list_memories("u1") == []


def test_self_disclosure_saved_but_question_guarded(mem_db, monkeypatch):
    """自述事实落库；纯疑问句不落库；疑问句夹带的自述事实仍落库。"""
    _patch_llm(monkeypatch, "[]")
    asyncio.run(MemoryService.extract_from_turn("u1", "s1", "我今年38岁，住在杭州。", "了解"))
    rows = MemoryService.list_memories("u1")
    assert len(rows) == 1 and rows[0]["memory_type"] == "fact"
    assert "38岁" in rows[0]["content"] and "杭州" in rows[0]["content"]

    asyncio.run(MemoryService.extract_from_turn("u1", "s2", "我有高血压吗？", "需要测量"))
    assert len(MemoryService.list_memories("u1")) == 1  # 提问未被当作自述

    asyncio.run(MemoryService.extract_from_turn("u1", "s3", "我有高血压，能吃什么药？", "建议…"))
    contents = [m["content"] for m in MemoryService.list_memories("u1")]
    assert any("高血压" in c for c in contents)
    assert not any("什么药" in c for c in contents)


def test_signal_types_preference_feedback_goal(mem_db):
    """偏好/纠正/目标按措辞分类，重要度进入画像卡候选区间。"""
    items = extract_explicit_signals("我喜欢吃清淡的。以后请先给结论再给依据。我打算三个月减重五公斤。")
    types = {i["type"] for i in items}
    assert {"preference", "feedback", "goal"} <= types
    assert all(i["action"] == "add" for i in items)
    assert len(items) <= memory_module._EXPLICIT_MAX_ITEMS


def test_sensitive_explicit_rejected(mem_db, monkeypatch):
    """显式指令也不能绕过敏感信息门禁。"""
    _patch_llm(monkeypatch, "[]")
    asyncio.run(MemoryService.extract_from_turn("u1", "s1", "记住我的密码是 abc123", "好的"))
    assert MemoryService.list_memories("u1") == []


def test_one_off_query_yields_nothing(mem_db, monkeypatch):
    """一次性查询（无自述、无显式指令）不入库，但运行事件仍落库。"""
    _patch_llm(monkeypatch, "[]")
    res = asyncio.run(MemoryService.extract_from_turn("u1", "s1", "今天金价多少一克？", "约…"))
    assert MemoryService.list_memories("u1") == []
    assert res["ran"] and res["explicit"] == 0 and res["add"] == 0
    assert res["reason"] == "empty"


# ============================================
# LLM 输出健壮解析
# ============================================

@pytest.mark.parametrize("raw,expected_len,reason", [
    ('[{"content": "用户爱吃辣", "type": "preference"}]', 1, ""),
    ('```json\n[{"content": "用户爱吃辣"}]\n```', 1, ""),
    ('好的，提取结果如下：\n[{"content": "用户爱吃辣"}]\n以上。', 1, ""),
    ('{"content": "用户爱吃辣", "type": "preference"}', 1, ""),
    ('[{"content": "甲"}]\n补充：[{"content": "乙"}]', 1, ""),
    ('[]', 0, "empty"),
    ('这不是JSON', 0, "parse_error"),
    ('', 0, "empty"),
])
def test_parse_extraction_items(raw, expected_len, reason):
    items, got = _parse_extraction_items(raw)
    assert len(items) == expected_len
    assert got == reason


def test_llm_fenced_output_lands_in_db(mem_db, monkeypatch):
    """模型带 fence/前后缀说明时不再整轮丢弃（旧版贪婪正则解析失败即静默跳过）。"""
    _patch_llm(monkeypatch, '提取如下：\n```json\n[{"content": "用户对青霉素过敏", '
                            '"importance": 0.9, "type": "fact", "action": "add"}]\n```')
    res = asyncio.run(MemoryService.extract_from_turn("u1", "s1", "今天天气如何", "晴天"))
    assert res["llm"] == 1 and res["add"] == 1
    assert "青霉素" in MemoryService.list_memories("u1")[0]["content"]


def test_recent_user_messages_enter_prompt(mem_db, monkeypatch):
    """抽取窗口含近期用户消息（跨轮纠正判断），单轮窗口不再是唯一依据。"""
    llm = _patch_llm(monkeypatch, "[]")
    asyncio.run(MemoryService.extract_from_turn(
        "u1", "s1", "今天天气如何", "晴天",
        recent_user_messages=["我之前说过我爱吃辣", "今天天气如何"]))
    assert llm.calls == 1
    assert "近期用户消息" in FakeLLM.last_prompt
    assert "我之前说过我爱吃辣" in FakeLLM.last_prompt


def test_guest_and_empty_turn_not_run(mem_db, monkeypatch):
    _patch_llm(monkeypatch, "[]")
    assert asyncio.run(MemoryService.extract_from_turn("guest", "s1", "记住我爱吃辣", "好"))["reason"] == "no_user"
    assert asyncio.run(MemoryService.extract_from_turn("u1", "s1", "  ", "好"))["reason"] == "empty_turn"
    assert _events() == []


# ============================================
# update/delete 定位放宽
# ============================================

def test_update_locates_with_relaxed_threshold(mem_db):
    """短 hint 与长旧记忆相似度不足查重门槛，但放宽后仍能定位并覆盖更新。"""
    old = MemoryService.add_memory("u1", "用户对花生过敏需避免花生制品", memory_type="preference")
    assert MemoryService.find_similar("u1", "用户花生过敏") is None  # 严格查重不命中
    stats = MemoryService.apply_extraction("u1", "s1", [
        {"content": "用户对花生过敏，已确认需严格避免", "importance": 0.9,
         "type": "preference", "action": "update", "target_hint": "用户花生过敏"},
    ])
    assert stats["update"] == 1 and stats["add"] == 0
    rows = MemoryService.list_memories("u1")
    assert len(rows) == 1 and rows[0]["id"] == old["id"]
    assert "严格避免" in rows[0]["content"]


def test_delete_keeps_higher_bar(mem_db):
    """删除不可逆：弱相关 hint 不误删（放宽门槛高于 update）。"""
    MemoryService.add_memory("u1", "用户喜欢晨跑锻炼")
    stats = MemoryService.apply_extraction("u1", "s1", [
        {"content": "", "action": "delete", "type": "fact", "target_hint": "用户的血压数值"},
    ])
    assert stats["delete"] == 0
    assert len(MemoryService.list_memories("u1")) == 1


# ============================================
# 运行事件 + 观测拆口径 + 空态可解释
# ============================================

def test_extract_run_event_logged_with_reason(file_db, monkeypatch):
    """每轮必落 extract 运行事件，detail 含显式/模型产出与无产出原因。"""
    _patch_llm(monkeypatch, "[]")
    asyncio.run(MemoryService.extract_from_turn("u1", "s1", "今天天气如何", "晴天"))
    runs = _events("extract")
    assert len(runs) == 1
    assert "显式0条/模型0条" in runs[0]["detail"]
    assert "模型层:empty" in runs[0]["detail"]


def test_write_events_exclude_extract_runs(file_db, monkeypatch):
    """写路径指标不含运行事件；extract_runs 单列（运行>0 而写入=0 可被识别）。"""
    _patch_llm(monkeypatch, "[]")
    asyncio.run(MemoryService.extract_from_turn("u1", "s1", "今天天气如何", "晴天"))
    ov = obs.personal_context_overview("u1")
    assert ov["memory"]["write_events"]["total"] == 0
    assert ov["memory"]["extract_runs"]["total"] == 1
    assert "模型层:empty" in ov["memory"]["extract_runs"]["last_detail"]
    mc = obs.monitor_metrics(days=7)["memory_context"]
    assert mc["write_events_total"] == 0 and mc["extract_runs"]["total"] == 1
    # 真实写入后：写路径 +1，运行数不变
    MemoryService.add_memory("u1", "用户对海鲜过敏", memory_type="preference")
    ov = obs.personal_context_overview("u1")
    assert ov["memory"]["write_events"]["total"] == 1
    assert ov["memory"]["write_events"]["by_action"] == {"add": 1}
    assert ov["memory"]["extract_runs"]["total"] == 1


def test_extract_status_api_explains_empty_panel(file_db, monkeypatch):
    """个人抽取状态接口：运行数/写入数/最近原因，供记忆弹窗空态解释。"""
    _patch_llm(monkeypatch, "[]")
    asyncio.run(MemoryService.extract_from_turn("u1", "s1", "今天天气如何", "晴天"))
    st = asyncio.run(api_me.my_memory_extract_status(days=7, user={"id": "u1", "role": "user"}))
    assert st["enabled"] and st["runs"] == 1 and st["writes"] == 0
    assert st["last_reason"] == "empty" and st["by_reason"] == {"empty": 1}
    assert st["last_run_at"]
    # 显式记住后：写入数上升，原因转为正常产出
    asyncio.run(MemoryService.extract_from_turn("u1", "s2", "记住我对海鲜过敏", "好的"))
    st = asyncio.run(api_me.my_memory_extract_status(days=7, user={"id": "u1", "role": "user"}))
    assert st["runs"] == 2 and st["writes"] == 1 and st["last_reason"] == "produced"
    guest = asyncio.run(api_me.my_memory_extract_status(days=7, user={"id": "guest", "role": "guest"}))
    assert guest["enabled"] is False and guest["runs"] == 0


def test_drill_memory_extract_branch(file_db, monkeypatch):
    """管理端钻取：memory_extract 只含运行事件，memory_events 只含写入事件。"""
    _patch_llm(monkeypatch, "[]")
    asyncio.run(MemoryService.extract_from_turn("u1", "s1", "今天天气如何", "晴天"))
    MemoryService.add_memory("u1", "用户对海鲜过敏", memory_type="preference")
    cols, rows, total = api_obs._drill_query("memory_extract", 50, {"u1": "张三"})
    assert total == 1 and rows[0]["username"] == "张三"
    assert {c["key"] for c in cols} == {"created_at", "username", "detail"}
    assert "模型层:empty" in rows[0]["detail"]
    _, wrows, wtotal = api_obs._drill_query("memory_events", 50, {"u1": "张三"})
    assert wtotal == 1 and wrows[0]["action_label"] == "新增"
    assert api_obs._MEMORY_ACTION_LABELS["extract"] == "抽取运行"


def test_backfill_file_paths_migration(file_db, tmp_path):
    """memory_markdown_v2：存量 file_path 为空的镜像行回填为可读 topic 文件路径。"""
    now = db.query_one("SELECT 1 AS x")  # 连接可用
    assert now
    db.execute(
        "INSERT INTO user_memories (id, user_id, memory_type, content, keywords,"
        " importance, confidence, created_at, updated_at, file_path)"
        " VALUES (?,?,?,?,?,?,?,?,?, '')",
        ("mid-0001", "u1", "fact", "用户对花生过敏", "[]", 0.7, 1.0,
         time.strftime("%Y-%m-%dT%H:%M:%S"), time.strftime("%Y-%m-%dT%H:%M:%S")))
    n = memory_store.backfill_file_paths(file_db)
    assert n == 1
    row = db.query_one("SELECT file_path FROM user_memories WHERE id = 'mid-0001'")
    assert row["file_path"] == "users/u1/auto/fact_mid-0001.md"
    assert (tmp_path / "memory" / row["file_path"]).is_file()
    # 幂等：已回填后不再重复处理
    assert memory_store.backfill_file_paths(file_db) == 0


# ============================================
# 接线集成：chat 后台任务 → 抽取 → 落库 → 面板可见
# ============================================

def test_post_turn_background_end_to_end(mem_db, monkeypatch):
    """真实 chat._post_turn_background 路径：近几条用户消息传入抽取，
    显式记住落库 + 运行事件落库，个人面板与抽取状态接口均可见。"""
    from types import SimpleNamespace

    from app.services.chat import chat_service

    llm = _patch_llm(monkeypatch, "[]")
    msgs = [
        SimpleNamespace(message_id="m1", role="user", content="我之前说过我爱吃辣"),
        SimpleNamespace(message_id="m2", role="assistant", content="好的"),
        SimpleNamespace(message_id="m3", role="user", content="记住我对海鲜过敏"),
        SimpleNamespace(message_id="m4", role="assistant", content="已为你记录"),
    ]
    session = SimpleNamespace(session_id="s1", user_id="u1", messages=msgs)
    asyncio.run(chat_service._post_turn_background(session, msgs[2], msgs[3], ""))
    # 落库：显式指令不依赖模型（模型返回 []）
    rows = MemoryService.list_memories("u1")
    assert len(rows) == 1 and "海鲜过敏" in rows[0]["content"]
    # 窗口接线：近期用户消息进入抽取 prompt
    assert llm.calls == 1 and "我之前说过我爱吃辣" in FakeLLM.last_prompt
    # 观测：运行事件与写入事件各一条，且口径不混
    assert len(_events("extract")) == 1 and len(_events("add")) == 1
    ov = obs.personal_context_overview("u1")
    assert ov["memory"]["total"] == 1
    assert ov["memory"]["write_events"]["total"] == 1
    assert ov["memory"]["extract_runs"]["total"] == 1
    st = asyncio.run(api_me.my_memory_extract_status(days=7, user={"id": "u1", "role": "user"}))
    assert st["runs"] == 1 and st["writes"] == 1 and st["last_reason"] == "produced"


# ============================================
# L4 助手分域：同源解析 + 运行事件 + 定位放宽
# ============================================

def test_agent_domain_fenced_output_and_run_event(file_db, monkeypatch, tmp_path):
    """助手域模型带 fence 输出不再整轮丢弃，且落 layer=agent 的运行事件与镜像文件。"""
    from app.services.agent_memory import AgentMemoryService

    _patch_llm(monkeypatch, '```json\n[{"content": "用户常分析睡眠与穴位主题文档", '
                            '"importance": 0.8, "type": "preference", "action": "add"}]\n```')
    res = asyncio.run(AgentMemoryService.extract_from_turn(
        "u1", "wiki_assistant", "s1", "帮我分析这份睡眠文档", "好的"))
    assert res["ran"] and res["llm"] == 1 and res["add"] == 1
    rows = AgentMemoryService.list_memories("u1", "wiki_assistant")
    assert len(rows) == 1 and "睡眠与穴位" in rows[0]["content"]
    assert AgentMemoryService.list_memories("u1", "chat") == []  # 不串域
    runs = db.query("SELECT layer, detail FROM memory_write_events WHERE action = 'extract'")
    assert len(runs) == 1 and runs[0]["layer"] == "agent"
    assert "wiki_assistant:模型1条" in runs[0]["detail"] and "新增1" in runs[0]["detail"]
    mirror = tmp_path / "memory" / "users" / "u1" / "agents" / "wiki_assistant.md"
    assert mirror.is_file() and "睡眠与穴位" in mirror.read_text(encoding="utf-8")


def test_agent_domain_llm_unavailable_is_explainable(file_db, monkeypatch):
    """助手域模型不可用：不静默，运行事件带原因（旧版直接 return 无痕迹）。"""
    from app.services.agent_memory import AgentMemoryService

    _patch_llm_unavailable(monkeypatch)
    res = asyncio.run(AgentMemoryService.extract_from_turn(
        "u1", "wiki_assistant", "s1", "以后分析先给结论", "好的"))
    assert res["ran"] and res["reason"] == "llm_unavailable" and res["add"] == 0
    assert AgentMemoryService.list_memories("u1", "wiki_assistant") == []
    runs = _events("extract")
    assert len(runs) == 1 and "模型层:llm_unavailable" in runs[0]["detail"]


def test_agent_domain_guards(mem_db, monkeypatch):
    """guest / 非法域 / 空消息不运行也不落事件。"""
    from app.services.agent_memory import AgentMemoryService

    _patch_llm(monkeypatch, "[]")
    assert asyncio.run(AgentMemoryService.extract_from_turn(
        "guest", "wiki_assistant", "s1", "记住我爱吃辣", "好"))["reason"] == "no_user"
    assert asyncio.run(AgentMemoryService.extract_from_turn(
        "u1", "unknown_agent", "s1", "记住我爱吃辣", "好"))["reason"] == "no_user"
    assert asyncio.run(AgentMemoryService.extract_from_turn(
        "u1", "wiki_assistant", "s1", "  ", "好"))["reason"] == "empty_turn"
    assert _events() == []


def test_agent_domain_update_relaxed_and_delete_conservative(mem_db):
    """助手域纠正可定位（放宽），弱相关 hint 不误删（删除门槛更高）。"""
    from app.services.agent_memory import AgentMemoryService

    old = AgentMemoryService.add_memory(
        "u1", "wiki_assistant", "用户偏好先给结论再给依据的文档分析风格", memory_type="preference")
    assert AgentMemoryService.find_similar("u1", "wiki_assistant", "用户偏好结论先行的分析风格") is None
    stats = AgentMemoryService.apply_extraction("u1", "wiki_assistant", "s1", [
        {"content": "用户偏好结论先行、依据在后的分析风格", "importance": 0.9,
         "type": "preference", "action": "update", "target_hint": "用户偏好结论先行的分析风格"},
    ])
    assert stats["update"] == 1 and stats["add"] == 0
    rows = AgentMemoryService.list_memories("u1", "wiki_assistant")
    assert len(rows) == 1 and rows[0]["id"] == old["id"] and "依据在后" in rows[0]["content"]
    stats = AgentMemoryService.apply_extraction("u1", "wiki_assistant", "s1", [
        {"content": "", "action": "delete", "type": "fact", "target_hint": "用户的血压数值"},
    ])
    assert stats["delete"] == 0
    assert len(AgentMemoryService.list_memories("u1", "wiki_assistant")) == 1