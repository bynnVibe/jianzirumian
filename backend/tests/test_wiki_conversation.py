"""对话/检索历史知识沉淀（llm-wiki 路径A）离线回归。

覆盖：主题解析（min_hits 闸门/去重/排序/噪声容忍）、topic_hash 归一化与用户隔离、
对话历史读取（chat_messages ⋈ chat_sessions、窗口/长度过滤、游客排除）、沉淀日志
防重复编译，以及 compile_for_user 编排（质量闸门、已沉淀跳过、私人归属 override、
max_topics 上限、综合失败静默跳过）。全部用例仅操作隔离 SQLite，LLM/检索以桩替换，
不触达网络/向量库。
"""
import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from app.core import db
from app.services import wiki_conversation as wc


def _uid():
    return f"u-{uuid.uuid4().hex[:8]}"


def _now_iso(days_ago=0):
    return (datetime.now() - timedelta(days=days_ago)).isoformat()


def _seed_chat(user_id, queries, *, days_ago=0, session_id=None):
    """写入一条会话 + 若干 user 提问（chat_messages 无 user_id，须经 session 关联）。"""
    sid = session_id or f"s-{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT INTO chat_sessions (session_id, user_id, title, created_at, updated_at)"
        " VALUES (?,?,?,?,?)",
        (sid, user_id, "测试会话", _now_iso(days_ago), _now_iso(days_ago)),
    )
    for i, q in enumerate(queries):
        db.execute(
            "INSERT INTO chat_messages (message_id, session_id, seq, role, content,"
            " sources, search_results, use_knowledge, use_search, attachments, created_at)"
            " VALUES (?,?,?,?,?,'[]','[]',0,0,'[]',?)",
            (f"m-{uuid.uuid4().hex[:10]}", sid, i, "user", q, _now_iso(days_ago)),
        )
    return sid


def _cleanup(user_id, sid):
    db.execute("DELETE FROM chat_messages WHERE session_id = ?", (sid,))
    db.execute("DELETE FROM chat_sessions WHERE session_id = ?", (sid,))
    db.execute("DELETE FROM wiki_topic_digest_log WHERE user_id = ?", (user_id,))


# ============================================
# 一、主题解析（纯函数）
# ============================================

def test_parse_topics_filters_dedups_and_sorts():
    raw = ('前置噪声 ```json [{"topic":"高血压饮食","hits":5},'
           '{"topic":"艾灸作用","hits":3},{"topic":"高血压饮食","hits":9},'
           '{"topic":"一次性提问","hits":1},{"topic":"","hits":8}]``` 后缀')
    topics = wc.parse_topics(raw, min_hits=3, max_topics=5)
    # hits<3 被过滤、空主题被丢弃、重复主题去重（保留首次出现的 hits）
    assert [t["topic"] for t in topics] == ["高血压饮食", "艾灸作用"]
    # 按 hits 倒序
    assert topics[0]["hits"] == 5 and topics[1]["hits"] == 3


def test_parse_topics_tolerates_garbage():
    assert wc.parse_topics("", min_hits=1) == []
    assert wc.parse_topics("模型拒绝回答，没有 JSON", min_hits=1) == []
    assert wc.parse_topics('{"topic":"不是数组","hits":3}', min_hits=1) == []
    # max_topics 截断
    many = "[" + ",".join(f'{{"topic":"主题{i}","hits":{10 - i}}}' for i in range(8)) + "]"
    assert len(wc.parse_topics(many, min_hits=1, max_topics=3)) == 3


def test_topic_hash_normalizes_and_isolates_users():
    # 空白/大小写归一化后一致
    assert wc.topic_hash("u1", "高血压") == wc.topic_hash("u1", " 高 血 压 ")
    assert wc.topic_hash("u1", "ABC") == wc.topic_hash("u1", "abc")
    # 不同用户/不同主题哈希不同
    assert wc.topic_hash("u1", "高血压") != wc.topic_hash("u2", "高血压")
    assert wc.topic_hash("u1", "高血压") != wc.topic_hash("u1", "糖尿病")


# ============================================
# 二、对话历史读取
# ============================================

def test_recent_queries_window_and_length_filter():
    uid = _uid()
    sid_recent = _seed_chat(uid, ["高血压吃什么好", "艾灸的作用", "好的"], days_ago=1)
    sid_old = _seed_chat(uid, ["很久以前的问题内容"], days_ago=120)
    try:
        # 窗口内 + 长度达标（「好的」<4 字被过滤）
        qs = wc.recent_user_queries(uid, window_days=30, min_len=4)
        assert "高血压吃什么好" in qs and "艾灸的作用" in qs
        assert "好的" not in qs
        # 120 天前的提问不在 30 天窗口内
        assert "很久以前的问题内容" not in qs
        # 放宽窗口后可见
        assert "很久以前的问题内容" in wc.recent_user_queries(uid, window_days=365, min_len=4)
    finally:
        _cleanup(uid, sid_recent)
        _cleanup(uid, sid_old)


def test_list_active_users_excludes_guest_and_low_volume():
    active = _uid()
    low = _uid()
    sid_a = _seed_chat(active, ["问题一内容", "问题二内容", "问题三内容"], days_ago=1)
    sid_l = _seed_chat(low, ["只问了一次"], days_ago=1)
    sid_g = _seed_chat("guest", ["游客问题一", "游客问题二", "游客问题三"], days_ago=1)
    try:
        users = wc.list_active_users(window_days=30, min_queries=3)
        assert active in users
        assert low not in users       # 提问数不足闸门
        assert "guest" not in users   # 游客排除
        assert "" not in users
    finally:
        _cleanup(active, sid_a)
        _cleanup(low, sid_l)
        db.execute("DELETE FROM chat_messages WHERE session_id = ?", (sid_g,))
        db.execute("DELETE FROM chat_sessions WHERE session_id = ?", (sid_g,))


# ============================================
# 三、沉淀日志：防重复编译
# ============================================

def test_digest_log_dedup_and_forget():
    uid = _uid()
    try:
        assert wc.already_digested(uid, "高血压饮食") is False
        wc.log_digest(uid, "高血压饮食", "page-1", 5)
        assert wc.already_digested(uid, "高血压饮食") is True
        assert wc.already_digested(uid, "高血压饮食 ") is True  # 归一化命中
        # 幂等：同主题再登记覆盖而非新增第二行
        wc.log_digest(uid, "高血压饮食", "page-2", 8)
        log = wc.digest_log(uid)
        assert len(log) == 1 and log[0]["page_id"] == "page-2" and log[0]["hits"] == 8
        # 词条删除 → 清理日志 → 允许重新沉淀
        wc.forget_topic("page-2")
        assert wc.already_digested(uid, "高血压饮食") is False
    finally:
        db.execute("DELETE FROM wiki_topic_digest_log WHERE user_id = ?", (uid,))


# ============================================
# 四、编排：compile_for_user
# ============================================

def test_compile_for_user_insufficient_queries(monkeypatch):
    monkeypatch.setattr(wc, "conversation_compile_enabled", lambda: True)
    uid = _uid()
    sid = _seed_chat(uid, ["只问了一次"], days_ago=0)
    try:
        res = asyncio.run(wc.compile_for_user(uid, min_hits=3))
        assert res["generated"] == 0 and res["skipped"] == "insufficient_queries"
    finally:
        _cleanup(uid, sid)


def test_compile_for_user_private_override_dedup_and_cap(monkeypatch):
    monkeypatch.setattr(wc, "conversation_compile_enabled", lambda: True)
    uid = _uid()
    sid = _seed_chat(uid, ["高血压吃什么", "高血压调理", "高血压饮食注意"], days_ago=0)

    calls = []

    async def fake_distill(queries, *, min_hits, max_topics):
        return [{"topic": "高血压饮食", "hits": 4}, {"topic": "艾灸作用", "hits": 3},
                {"topic": "失眠调理", "hits": 3}]

    async def fake_synthesize(query, user_id, *, visibility=None, owner_id=None):
        calls.append({"query": query, "user_id": user_id,
                      "visibility": visibility, "owner_id": owner_id})
        return {"id": f"page-{uuid.uuid4().hex[:6]}", "title": f"{query}综合词条"}

    monkeypatch.setattr(wc, "distill_topics", fake_distill)
    import app.services.wiki as wiki_mod
    monkeypatch.setattr(wiki_mod.wiki_service, "synthesize", fake_synthesize)

    try:
        # max_topics=2：只沉淀前两个高频主题
        res = asyncio.run(wc.compile_for_user(uid, max_topics=2, min_hits=3))
        assert res["generated"] == 2
        assert [t["topic"] for t in res["topics"]] == ["高血压饮食", "艾灸作用"]
        # 私人归属 override 正确透传
        assert all(c["visibility"] == "private" and c["owner_id"] == uid for c in calls)
        # 已沉淀主题被登记
        assert wc.already_digested(uid, "高血压饮食") is True
        assert wc.already_digested(uid, "失眠调理") is False  # 超出上限未沉淀

        # 再次运行：已沉淀主题跳过，仅补「失眠调理」
        calls.clear()
        res2 = asyncio.run(wc.compile_for_user(uid, max_topics=3, min_hits=3))
        assert res2["generated"] == 1
        assert res2["topics"][0]["topic"] == "失眠调理"
        assert [c["query"] for c in calls] == ["失眠调理"]
    finally:
        _cleanup(uid, sid)


def test_compile_for_user_silent_skip_on_synthesize_failure(monkeypatch):
    monkeypatch.setattr(wc, "conversation_compile_enabled", lambda: True)
    uid = _uid()
    sid = _seed_chat(uid, ["问题一内容", "问题二内容", "问题三内容"], days_ago=0)

    async def fake_distill(queries, *, min_hits, max_topics):
        return [{"topic": "无素材主题", "hits": 5}, {"topic": "有素材主题", "hits": 4}]

    async def fake_synthesize(query, user_id, *, visibility=None, owner_id=None):
        if query == "无素材主题":
            raise ValueError("知识库中未检索到与该主题相关的素材")
        return {"id": "page-ok", "title": f"{query}综合词条"}

    monkeypatch.setattr(wc, "distill_topics", fake_distill)
    import app.services.wiki as wiki_mod
    monkeypatch.setattr(wiki_mod.wiki_service, "synthesize", fake_synthesize)

    try:
        res = asyncio.run(wc.compile_for_user(uid, max_topics=3, min_hits=3))
        # 无素材主题静默跳过、不登记；有素材主题正常沉淀
        assert res["generated"] == 1
        assert res["topics"][0]["topic"] == "有素材主题"
        assert wc.already_digested(uid, "无素材主题") is False
        assert wc.already_digested(uid, "有素材主题") is True
    finally:
        _cleanup(uid, sid)
