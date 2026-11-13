"""前缀缓存整改阶段一回归测试（内存 DB，不出网）。

覆盖 Prompt 分区装配与前缀稳定性：
  P1  分区顺序：静态头(S) → 画像(U) → 历史(C) → 相关记忆(D) → 提问；
  P2  前缀稳定：D 区（相关记忆/提问）每轮变化不污染 S/U/C 前缀；画像更新只动 U 槽位；
  P3  稳定排序：画像卡按 created_at/id（updated_at 变动不重排）、Skill 按创建序；
  P4  缓存可观测：cached_tokens 采集（OpenAI usage / Anthropic 归一化 / SSE 累计）
      与落库（agent_spans / gateway_logs，prefix_cache_v1 迁移）。
"""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import db
from app.core import observability as obs
from app.services import chat_agent as ca
from app.services.memory import MemoryService
from app.services.skill import skill_service


@pytest.fixture()
def mem_db(monkeypatch):
    """独立内存数据库：避免污染本地开发库"""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    yield conn
    conn.close()


# ============================================
# P1 分区装配顺序（S → U → C → D → 提问）
# ============================================

def test_answer_messages_partition_order():
    static = [{"role": "system", "content": "静态系统提示"}]
    history = [{"role": "user", "content": "历史问"},
               {"role": "assistant", "content": "历史答"}]
    msgs = ca._build_answer_messages(static, "画像内容", "相关记忆内容", history, "本轮提问")
    assert msgs[0] == static[0]
    assert msgs[1]["role"] == "system" and "【用户画像（常驻）" in msgs[1]["content"]
    assert msgs[2:4] == history
    assert msgs[4]["role"] == "system" and "【关于该用户的相关记忆" in msgs[4]["content"]
    assert msgs[5] == {"role": "user", "content": "本轮提问"}


def test_decision_and_plan_partition_order():
    history = [{"role": "user", "content": f"问{i}"} for i in range(8)]
    for builder in (ca._build_decision_messages, ca._build_plan_messages):
        msgs = builder("画像内容", "相关记忆内容", history, "本轮提问", True, False)
        assert msgs[0]["role"] == "system"  # 静态决策/规划提示
        assert "【用户画像（常驻）" in msgs[1]["content"]  # U 区紧跟静态头
        assert msgs[-1] == {"role": "user", "content": "本轮提问"}
        assert "【关于该用户的相关记忆" in msgs[-2]["content"]  # D 区在历史之后、提问之前
        assert all("相关记忆" not in m["content"] for m in msgs[2:-2])


def test_empty_profile_and_relevant_omitted():
    assert ca._profile_messages("") == [] and ca._profile_messages("  ") == []
    assert ca._relevant_messages("") == [] and ca._relevant_messages(None) == []
    msgs = ca._build_answer_messages([{"role": "system", "content": "S"}], "", "", [], "问")
    assert len(msgs) == 2  # 仅静态头 + 提问


# ============================================
# P2 前缀稳定性：动态区变化不污染 S/U/C 前缀
# ============================================

def test_prefix_stable_across_dynamic_changes():
    """相关记忆与提问每轮变化，S/U/C 前缀逐字节不变（前缀缓存命中条件）。"""
    static = [{"role": "system", "content": "静态系统提示" * 50}]
    history = [{"role": "user", "content": f"历史问{i}"} for i in range(6)]
    m1 = ca._build_answer_messages(static, "画像", "相关记忆A", history, "问题1")
    m2 = ca._build_answer_messages(static, "画像", "相关记忆B（本轮检索结果不同）", history, "问题2")
    prefix_len = len(static) + 1 + len(history)  # S + U + C
    assert m1[:prefix_len] == m2[:prefix_len]
    assert m1[prefix_len:] != m2[prefix_len:]  # 差异仅存在于 D 区与提问


def test_profile_update_only_touches_u_slot():
    """画像更新只替换 U 槽位，静态头不受影响（跨会话 S 区缓存仍可命中）。"""
    static = [{"role": "system", "content": "静态系统提示" * 50}]
    m1 = ca._build_answer_messages(static, "旧画像", "", [], "问")
    m2 = ca._build_answer_messages(static, "新画像（记忆更新后）", "", [], "问")
    assert m1[0] == m2[0]
    assert m1[1]["content"] != m2[1]["content"]


# ============================================
# P3 稳定排序：画像卡 / Skill 注入顺序不随 updated_at 抖动
# ============================================

def _insert_memory(conn, mid, content, created_at, updated_at, importance=0.9):
    conn.execute(
        "INSERT INTO user_memories (id, user_id, memory_type, content, keywords,"
        " importance, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (mid, "u1", "fact", content, "[]", importance, created_at, updated_at),
    )


def test_profile_order_stable_when_updated_at_changes(mem_db):
    """updated_at 变动（记忆被更新）不改变画像卡顺序：按 created_at/id 稳定排序。"""
    _insert_memory(mem_db, "ma", "用户喜欢晨跑", "2026-01-01T00:00:00", "2026-01-01T00:00:00")
    _insert_memory(mem_db, "mb", "用户备考营养师", "2026-01-02T00:00:00", "2026-01-02T00:00:00")
    before = [m["id"] for m in MemoryService.profile_memories("u1")]
    assert before == ["ma", "mb"]
    # ma 被更新（updated_at 变最新）：旧排序会把它提到最前，稳定排序保持原序
    mem_db.execute("UPDATE user_memories SET updated_at='2026-11-01T00:00:00' WHERE id='ma'")
    after = [m["id"] for m in MemoryService.profile_memories("u1")]
    assert after == before


def _insert_skill(conn, sid, name, created_at, updated_at, builtin=0):
    conn.execute(
        "INSERT INTO user_skills (id, owner_id, name, description, kind, scope, content,"
        " enabled, builtin, visibility, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, "u1", name, "", "prompt", "chat", f"{name}的内容", 1, builtin,
         "personal", created_at, updated_at),
    )


def test_skill_order_stable_when_updated_at_changes(mem_db):
    """Skill 注入按创建序：编辑（updated_at 变动）不重排 U 区前缀。"""
    _insert_skill(mem_db, "sa", "技能A", "2026-01-01T00:00:00", "2026-01-01T00:00:00")
    _insert_skill(mem_db, "sb", "技能B", "2026-01-02T00:00:00", "2026-01-02T00:00:00")
    names_before = [s["name"] for s in skill_service.active_prompt_skills("u1")]
    mem_db.execute("UPDATE user_skills SET updated_at='2026-11-01T00:00:00' WHERE id='sa'")
    names_after = [s["name"] for s in skill_service.active_prompt_skills("u1")]
    assert names_after == names_before
    assert names_after.index("技能A") < names_after.index("技能B")


# ============================================
# P4 缓存可观测：cached_tokens 采集与落库
# ============================================

def test_note_usage_and_span_persist_cached_tokens(mem_db):
    """note_usage 3-tuple → record_llm 落 agent_spans.cached_tokens（prefix_cache_v1 迁移列）。"""
    obs.start_trace("test-agent", user_id="u1")
    try:
        obs.note_usage(100, 20, 64)
        pt, ct = obs.record_llm("test-llm", "prompt", "response")
        assert (pt, ct) == (100, 20)
        row = db.query_one(
            "SELECT cached_tokens FROM agent_spans WHERE kind='llm' ORDER BY seq DESC LIMIT 1")
        assert row and row["cached_tokens"] == 64
    finally:
        obs.end_trace("ok")


def test_report_openai_usage_extracts_cached():
    """OpenAI 形态 usage（dict/对象）提取 prompt_tokens_details.cached_tokens。"""
    from app.core.llm import _report_openai_usage

    resp = SimpleNamespace(usage={
        "prompt_tokens": 100, "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 64},
    })
    _report_openai_usage(resp)
    assert obs._pending_usage.get() == (100, 20, 64)
    obs._pending_usage.set(None)
    # 无 details 的旧上游：cached 缺省 0，不报错
    _report_openai_usage(SimpleNamespace(usage={"prompt_tokens": 10, "completion_tokens": 2}))
    assert obs._pending_usage.get() == (10, 2, 0)
    obs._pending_usage.set(None)


def test_anthropic_normalization_maps_cache_read():
    """Anthropic cache_read_input_tokens → OpenAI 形态 prompt_tokens_details.cached_tokens。"""
    from app.services.gateway import upstream

    raw = {"content": [{"type": "text", "text": "你好"}],
           "usage": {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 80}}
    normalized = upstream._anthropic_to_openai({"name": "claude"}, raw)
    assert normalized["usage"]["prompt_tokens_details"]["cached_tokens"] == 80


def test_account_sse_accumulates_cached():
    """流式 SSE usage 块累计 cached_tokens。"""
    from app.services.gateway.proxy import _account_sse

    usage = {"pt": 0, "ct": 0, "cached": 0}
    sse = "data: " + json.dumps({"usage": {
        "prompt_tokens": 10, "completion_tokens": 5,
        "prompt_tokens_details": {"cached_tokens": 8}}}) + "\n\n"
    _account_sse(sse, [], usage, [])
    assert usage == {"pt": 10, "ct": 5, "cached": 8}


def test_gateway_log_persists_cached_tokens(mem_db):
    """gateway_logs.cached_tokens 落库（prefix_cache_v1 迁移列）。"""
    from app.services.gateway.logs import write_log

    log_id = write_log({"model_requested": "m", "prompt_tokens": 100,
                        "completion_tokens": 20, "cached_tokens": 64, "total_tokens": 120})
    row = db.query_one("SELECT cached_tokens FROM gateway_logs WHERE id=?", (log_id,))
    assert row and row["cached_tokens"] == 64