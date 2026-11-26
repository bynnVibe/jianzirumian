"""历史窗口预算整改回归测试（内存 DB，不出网）。

覆盖多轮历史 Token 膨胀整改四抓手与前缀稳定性契约：
  H1  全文区单条帽：最近 KEEP_RECENT_FULL 条内助手长回答投影为头部摘录+原长尾注，
      用户消息不帽（保留完整意图）；
  H2  截断档减半：较早消息按 CLIP_MID_CHARS=400 逐条截断（CJK 1:1 口径减半）；
  H3  实时类衰减：is_realtime 写时打标的助手答案入历史即衰减到 CLIP_OLD_CHARS，
      不等 60 分钟空闲衰减；标签经 SQLite 持久化往返不丢；
  H4  计量去重：history_segment_text 剔除再水化摘要等 system 消息（summary 段已单独计量）；
  H5  前缀稳定：三档投影位置无关——渲染只依赖自身内容/标签与是否位于全文区，
      追加新轮不改变较早消息渲染（仅退出全文区时一次性截断迁移）。
"""
import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.core import db
from app.services import session_summary as ss


@pytest.fixture()
def mem_db(monkeypatch):
    """独立内存数据库：避免污染本地开发库"""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    yield conn
    conn.close()


@pytest.fixture()
def budget_cfg(monkeypatch):
    """固定预算参数，避免环境变量覆盖影响契约断言"""
    monkeypatch.setattr(ss.settings, "CLIP_MID_CHARS", 400)
    monkeypatch.setattr(ss.settings, "FULL_ZONE_MAX_CHARS", 1200)
    monkeypatch.setattr(ss.settings, "KEEP_RECENT_FULL", 4)


def _msg(mid, role, content, realtime=False):
    return SimpleNamespace(message_id=mid, role=role, content=content, is_realtime=realtime)


def _session(messages):
    # updated_at=当前 → 空闲 0 分钟，排除空闲衰减干扰（H3 须证明不等空闲即衰减）
    return SimpleNamespace(session_id="s1", user_id="u1", messages=messages,
                           updated_at=datetime.now().isoformat())


LONG_ANSWER = "深圳今天天气晴，气温 26℃，适合户外活动。" * 100  # ≈3000 字符


# ============================================
# H1 全文区单条帽（助手长回答摘录，用户消息不帽）
# ============================================

def test_full_zone_cap_assistant_only(mem_db, budget_cfg):
    msgs = [_msg("u1", "user", "问一"),
            _msg("a1", "assistant", LONG_ANSWER),
            _msg("u2", "user", "问二"),
            _msg("a2", "assistant", "答" * 2500),
            _msg("u3", "user", "本轮提问")]
    rendered = ss.build_context_messages(_session(msgs), 8)
    # 全文区（最近 4 条）内两条助手长回答均被帽：头部摘录+原长尾注
    assert rendered[1]["content"] == LONG_ANSWER[:1200] + f"\n…（长回答摘录，原长 {len(LONG_ANSWER)} 字符）"
    assert rendered[3]["content"] == ("答" * 2500)[:1200] + "\n…（长回答摘录，原长 2500 字符）"
    # 短消息全文保留
    assert rendered[0]["content"] == "问一" and rendered[4]["content"] == "本轮提问"


def test_full_zone_user_long_message_not_capped(mem_db, budget_cfg):
    long_user = "贴一段长文档原文。" * 300  # >1200 字符的用户消息
    msgs = [_msg("a0", "assistant", "短答"),
            _msg("u0", "user", "短问"),
            _msg("u1", "user", long_user),
            _msg("a1", "assistant", "短答二"),
            _msg("u2", "user", "本轮提问")]
    rendered = ss.build_context_messages(_session(msgs), 8)
    # 用户消息位于全文区不帽：完整意图保留
    assert rendered[2]["content"] == long_user


# ============================================
# H2 截断档减半（400 字符/条）
# ============================================

def test_mid_clip_halved_to_400(mem_db, budget_cfg):
    old_long = "较早的长回答内容。" * 200  # 2000 字符，位于全文区之外
    msgs = [_msg("a0", "assistant", old_long),
            _msg("u1", "user", "问一"),
            _msg("a1", "assistant", "答一"),
            _msg("u2", "user", "问二"),
            _msg("a2", "assistant", "答二"),
            _msg("u3", "user", "本轮提问")]
    rendered = ss.build_context_messages(_session(msgs), 8)
    assert rendered[0]["content"] == old_long[:400] + f"\n…（较早消息已截断，原长 {len(old_long)} 字符）"


# ============================================
# H3 实时类答案衰减（写时打标，入历史即衰减）
# ============================================

def test_realtime_answer_decays_without_idle(mem_db, budget_cfg):
    msgs = [_msg("u1", "user", "香港今天天气怎么样"),
            _msg("a1", "assistant", LONG_ANSWER, realtime=True),
            _msg("u2", "user", "问二"),
            _msg("a2", "assistant", "答" * 2500),  # 非实时长答对照：走全文帽而非衰减
            _msg("u3", "user", "本轮提问")]
    rendered = ss.build_context_messages(_session(msgs), 8)
    # 实时答案虽位于全文区且会话活跃（空闲 0），仍衰减到 200 字符
    assert rendered[1]["content"] == LONG_ANSWER[:200] + f"\n…（实时答案已衰减，原长 {len(LONG_ANSWER)} 字符）"
    assert "长回答摘录" in rendered[3]["content"]


def test_realtime_flag_persist_roundtrip(mem_db):
    """is_realtime 经 SQLite 落库/加载往返不丢（写时打标→读时投影的持久化链路）"""
    from app.services.chat import ChatService, Message
    now = datetime.now().isoformat()
    db.execute("INSERT INTO chat_sessions (session_id, user_id, title, created_at, updated_at)"
               " VALUES ('s1','u1','t',?,?)", (now, now))
    svc = ChatService()
    session = svc.create_session(user_id="u1", session_id="s1")
    session.add_message(Message(role="user", content="深圳今天的天气怎么样"))
    session.add_message(Message(role="assistant", content=LONG_ANSWER, is_realtime=True))
    svc._persist_session(session)
    row = db.query_one("SELECT is_realtime FROM chat_messages WHERE role='assistant'")
    assert row["is_realtime"] == 1
    loaded = svc._load_session("s1")
    assert loaded.messages[-1].is_realtime is True
    assert loaded.messages[0].is_realtime is False


# ============================================
# H4 history 段计量去重（剔除摘要 system 消息）
# ============================================

def test_history_segment_text_excludes_system():
    msgs = [{"role": "system", "content": "【此前对话摘要】摘要正文"},
            {"role": "user", "content": "问"},
            {"role": "assistant", "content": "答"}]
    assert ss.history_segment_text(msgs) == "问\n答"


def test_summary_branch_decay_and_dedup(mem_db, budget_cfg):
    """有摘要分支：tail 实时答案同样衰减；history 段口径不含摘要正文"""
    now = datetime.now().isoformat()
    msgs = [_msg("u0", "user", "问零"), _msg("a0", "assistant", "答零"),
            _msg("u1", "user", "问一"),
            _msg("a1", "assistant", LONG_ANSWER, realtime=True),
            _msg("u2", "user", "问二"), _msg("a2", "assistant", "答二"),
            _msg("u3", "user", "本轮提问")]
    db.execute(
        "INSERT INTO chat_session_summaries"
        " (session_id, user_id, summary, covered_until_message_id, token_estimate,"
        "  created_at, updated_at, compact_kind, before_tokens, fail_streak)"
        " VALUES ('s1','u1','九节摘要正文','u1',100,?,?, 'auto', 5000, 0)", (now, now))
    rendered = ss.build_context_messages(_session(msgs), 8)
    assert rendered[0]["role"] == "system" and "【此前对话摘要】" in rendered[0]["content"]
    # 覆盖点之后的 tail 首条即实时长答 → 衰减投影
    assert rendered[1]["content"] == LONG_ANSWER[:200] + f"\n…（实时答案已衰减，原长 {len(LONG_ANSWER)} 字符）"
    # 计量去重：history 段（不含本次提问）剔除摘要 system 消息
    seg = ss.history_segment_text(rendered[:-1])
    assert "摘要正文" not in seg and "实时答案已衰减" in seg


# ============================================
# H5 前缀稳定性：三档投影位置无关、追加轮次不重排
# ============================================

def test_cap_render_position_independent(mem_db, budget_cfg):
    """同一条长回答在不同窗口位置（仍居全文区）渲染逐字节一致"""
    long_a = _msg("a1", "assistant", LONG_ANSWER)
    w1 = [_msg("u0", "user", "旧问"), long_a,
          _msg("u1", "user", "问一"), _msg("a2", "assistant", "答二"),
          _msg("u2", "user", "本轮提问")]
    w2 = [_msg("x0", "user", "更早问"), _msg("x1", "assistant", "更早答"),
          _msg("u0", "user", "旧问"), long_a,
          _msg("u1", "user", "问一"), _msg("a2", "assistant", "答二"),
          _msg("u2", "user", "本轮提问")]
    r1 = ss.build_context_messages(_session(w1), 8)
    r2 = ss.build_context_messages(_session(w2), 8)
    assert r1[1] == r2[3]  # 渲染只依赖自身内容与全文区归属，与窗口总条数无关


def test_prefix_frozen_across_rounds(mem_db, budget_cfg):
    """追加新轮：较早消息渲染冻结；仅退出全文区的长回答发生一次性截断迁移"""
    old_long = "更早的长回答。" * 200
    rt = _msg("a1", "assistant", LONG_ANSWER, realtime=True)
    capped = _msg("a2", "assistant", "答" * 2500)
    w1 = [_msg("a0", "assistant", old_long), rt,
          _msg("u1", "user", "问一"), capped, _msg("u2", "user", "问二")]
    w2 = w1 + [_msg("a3", "assistant", "答三"), _msg("u3", "user", "问三"),
               _msg("a4", "assistant", "答四"), _msg("u4", "user", "本轮提问")]
    r1 = ss.build_context_messages(_session(w1), 8)
    r2 = ss.build_context_messages(_session(w2), 8)
    # 迁移点之前的前缀逐字节稳定（mid 截断/实时衰减渲染冻结）
    assert r2[:3] == r1[:3]
    # 长回答退出全文区：一次性迁移到 mid 截断（此后渲染再冻结）
    assert r1[3]["content"].endswith("（长回答摘录，原长 2500 字符）")
    assert r2[3]["content"] == ("答" * 2500)[:400] + "\n…（较早消息已截断，原长 2500 字符）"
    # 实时衰减渲染跨轮冻结
    assert r2[1] == r1[1]