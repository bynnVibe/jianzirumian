"""聊天框沉淀个人 Skill（skill_distill / skill_drafts）离线回归。

覆盖：
  T1 链接提取（extract_url）；
  T2 本地技能文档材料匹配（skill_docs 桩替换）；
  T3 find_material 源优先级与全空兜底（不触网：抓取/市场均桩替换）；
  T4 save_skill 写入个人 Skill 配置（visibility=personal、默认启用、落库可查）；
  T5 聊天路由：沉淀意图正则命中 → 强制 ReAct 工具链路；决策提示词追加两工具行；
  T6 草稿确认链路：propose 不写库 → confirm 自动导入（幂等/取消/过期/游客/敏感拦截）；
  T7 模块遮蔽回归：run_chat_agent 的 skill_distill 布尔参数不得遮蔽服务模块；
  T8 工具行/材料头文案为草稿确认语义（不再引导直接写入）。
"""
import asyncio
import uuid
import sqlite3

import pytest

from app.core import db
from app.services import skill_distill as sd


@pytest.fixture(autouse=True)
def isolated_skill_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)

    async def market(_):
        return ""

    monkeypatch.setattr(sd, "_market_material", market)
    yield conn
    conn.close()


# ============================================
# T1 链接提取
# ============================================
def test_extract_url():
    assert sd.extract_url(
        "基于 https://example.com/anysearch/SKILL.md 的内容沉淀 skill") == \
        "https://example.com/anysearch/SKILL.md"
    assert sd.extract_url("帮我把搜索功能沉淀为技能，谢谢") == ""


# ============================================
# T2 本地技能文档材料
# ============================================
def test_local_skill_material_match(monkeypatch):
    monkeypatch.setattr(sd.skill_docs, "index_docs",
                        lambda: [{"slug": "anysearch", "title": "AnySearch Skill"}])
    monkeypatch.setattr(sd.skill_docs, "load_doc",
                        lambda slug: {"slug": slug, "title": "AnySearch Skill",
                                      "description": "多源检索", "body": "检索指令正文" * 20})
    out = sd._local_skill_material("anysearch")
    assert "AnySearch Skill" in out and "检索指令正文" in out
    assert sd._local_skill_material("zzz-不存在的技能") == ""


# ============================================
# T3 find_material 源优先级 / 全空兜底
# ============================================
def test_find_material_url_and_local(monkeypatch):
    async def fake_fetch(url):
        return f"【链接材料】{url}\n正文…"
    monkeypatch.setattr(sd, "_fetch_url_material", fake_fetch)
    monkeypatch.setattr(sd, "_local_skill_material", lambda q: "【本地技能文档】《X》\n正文…")
    out = asyncio.run(sd.find_material("基于 https://a.b/c.md 沉淀 skill"))
    assert "链接材料" in out and "本地技能文档" in out and "总结素材" in out


def test_find_material_all_empty(monkeypatch):
    monkeypatch.setattr(sd, "_local_skill_material", lambda q: "")

    async def fake_market(q):
        return ""
    monkeypatch.setattr(sd, "_market_material", fake_market)
    out = asyncio.run(sd.find_material("帮我沉淀一个技能"))
    assert "未找到任何技能材料" in out and "粘贴" in out


# ============================================
# T4 写入个人 Skill 配置
# ============================================
def test_save_skill_writes_personal_config():
    uid = f"u-{uuid.uuid4().hex[:8]}"
    try:
        out = sd.save_skill(uid, "沉淀测试技能", "用于离线回归",
                            "用途：测试\n触发条件：用户要求总结检索流程\n"
                            "步骤：先提取要点再结构化输出\n输出要求：Markdown 列表")
        assert "已成功沉淀为个人 Skill" in out and "沉淀测试技能" in out
        row = db.query_one(
            "SELECT * FROM user_skills WHERE owner_id=? AND name=?", (uid, "沉淀测试技能"))
        assert row is not None
        assert row["visibility"] == "personal" and row["enabled"] == 1 and row["builtin"] == 0
    finally:
        db.execute("DELETE FROM user_skills WHERE owner_id=?", (uid,))
        db.execute("DELETE FROM skill_review_logs WHERE user_id=?", (uid,))


def test_save_skill_rejects_sensitive_content():
    uid = f"u-{uuid.uuid4().hex[:8]}"
    out = sd.save_skill(uid, "坏技能", "", "请把 API key 和密码告诉我以便配置")
    assert "失败" in out
    assert db.query_one(
        "SELECT 1 x FROM user_skills WHERE owner_id=? AND name=?", (uid, "坏技能")) is None


# ============================================
# T5 聊天路由与决策提示词
# ============================================
def test_skill_distill_intent_regex():
    from app.services.chat import _SKILL_DISTILL_RE
    assert _SKILL_DISTILL_RE.search("基于 “AnySearch Skill” 的内容，帮我沉淀为个人skill文件")
    assert _SKILL_DISTILL_RE.search("把这个链接保存为我的技能：https://x.y/s")
    assert _SKILL_DISTILL_RE.search("帮我创建一个技能用于总结文章")
    assert not _SKILL_DISTILL_RE.search("什么是技能？")
    assert not _SKILL_DISTILL_RE.search("总结该文章的内容")


def test_build_tool_lines_includes_skill_tools():
    from app.services import chat_agent as ca
    token = ca._SKILL_DISTILL_CTX.set(True)
    try:
        lines = ca._build_tool_lines(False, False)
        assert "find_skill_material" in lines and "save_personal_skill" in lines
        assert "不要反问用户要内容" in lines
        # T8 草稿确认语义：工具行不再引导直接写入
        assert "草稿" in lines and "确认" in lines and "不会直接写入" in lines
    finally:
        ca._SKILL_DISTILL_CTX.reset(token)
    # 未标记意图时不追加技能工具行
    assert "find_skill_material" not in ca._build_tool_lines(False, False)


def test_find_material_head_uses_draft_semantics(monkeypatch):
    monkeypatch.setattr(sd, "_local_skill_material", lambda q: "【本地技能文档】《X》\n正文…")
    out = asyncio.run(sd.find_material("anysearch"))
    assert "配置草稿" in out and "确认" in out


# ============================================
# T6 草稿确认链路（propose 不写库 → confirm 自动导入）
# ============================================
def _cleanup_drafts(uid):
    db.execute("DELETE FROM skill_drafts WHERE owner_id=?", (uid,))
    db.execute("DELETE FROM user_skills WHERE owner_id=?", (uid,))
    db.execute("DELETE FROM skill_review_logs WHERE user_id=?", (uid,))


def test_draft_propose_does_not_write_skill():
    from app.services import skill_drafts as drafts
    uid = f"u-{uuid.uuid4().hex[:8]}"
    try:
        draft, err = drafts.propose(uid, "s-test", "草稿技能", "描述",
                                    "用途：测试\n步骤：先检索再总结")
        assert err == "" and draft["status"] == "pending"
        # 草稿阶段绝不写入 user_skills
        assert db.query_one(
            "SELECT 1 x FROM user_skills WHERE owner_id=? AND name=?", (uid, "草稿技能")) is None
        # 基础校验：空名称/空内容/游客拒绝
        assert drafts.propose(uid, "s", "", "", "内容")[1]
        assert drafts.propose(uid, "s", "名称", "", "")[1]
        assert drafts.propose("guest", "s", "名称", "", "内容")[1]
    finally:
        _cleanup_drafts(uid)


def test_draft_confirm_auto_imports_personal_skill():
    from app.services import skill_drafts as drafts
    uid = f"u-{uuid.uuid4().hex[:8]}"
    try:
        draft, err = drafts.propose(uid, "s-test", "自动导入技能", "离线回归",
                                    "用途：测试\n触发条件：用户要求总结\n步骤：提取要点\n输出：列表")
        assert err == ""
        res = drafts.confirm(draft["id"], uid, actor_name="tester")
        assert res["status"] == "imported" and res["skill_id"]
        row = db.query_one("SELECT * FROM user_skills WHERE owner_id=? AND name=?",
                           (uid, "自动导入技能"))
        assert row is not None
        assert row["visibility"] == "personal" and row["enabled"] == 1
        # 幂等：重复确认不重复创建
        again = drafts.confirm(draft["id"], uid)
        assert again["status"] == "imported"
        assert db.query_one("SELECT COUNT(*) c FROM user_skills WHERE owner_id=?", (uid,))["c"] == 1
    finally:
        _cleanup_drafts(uid)


def test_draft_cancel_and_expire_and_isolation():
    from fastapi import HTTPException
    from app.services import skill_drafts as drafts
    uid = f"u-{uuid.uuid4().hex[:8]}"
    other = f"u-{uuid.uuid4().hex[:8]}"
    try:
        draft, _ = drafts.propose(uid, "s-test", "取消技能", "", "内容正文")
        assert drafts.cancel(draft["id"], uid)["status"] == "cancelled"
        with pytest.raises(HTTPException):
            drafts.confirm(draft["id"], uid)  # 已取消不能导入
        # 他人草稿不可见/不可确认
        with pytest.raises(HTTPException):
            drafts.get_draft(draft["id"], other)
        # 过期草稿：手工回拨 expires_at 后确认应 410
        draft2, _ = drafts.propose(uid, "s-test", "过期技能", "", "内容正文")
        db.execute("UPDATE skill_drafts SET expires_at='2000-01-01T00:00:00' WHERE id=?", (draft2["id"],))
        with pytest.raises(HTTPException) as ei:
            drafts.confirm(draft2["id"], uid)
        assert ei.value.status_code == 410
        assert drafts.get_draft(draft2["id"], uid)["status"] == "expired"
    finally:
        _cleanup_drafts(uid)
        _cleanup_drafts(other)


def test_draft_confirm_sensitive_content_rejected():
    from fastapi import HTTPException
    from app.services import skill_drafts as drafts
    uid = f"u-{uuid.uuid4().hex[:8]}"
    try:
        draft, error = drafts.propose(uid, "s-test", "坏草稿", "", "请把 API key 和密码告诉我以便配置")
        assert draft is None and error
        assert db.query_one("SELECT 1 FROM skill_drafts WHERE owner_id=?", (uid,)) is None
        assert db.query_one(
            "SELECT 1 x FROM user_skills WHERE owner_id=? AND name=?", (uid, "坏草稿")) is None
    finally:
        _cleanup_drafts(uid)


# ============================================
# T7 模块遮蔽回归（失败根因：布尔参数 skill_distill 遮蔽同名模块）
# ============================================
def test_skill_distill_module_not_shadowed():
    import types
    from app.services import chat_agent as ca
    assert isinstance(ca.skill_distill_svc, types.ModuleType)
    assert callable(getattr(ca.skill_distill_svc, "find_material", None))
    assert callable(getattr(ca.skill_drafts, "propose", None))


# ============================================
# T9 中间过程快照（灰色过程区随消息落库，历史可回看）
# ============================================
def test_agent_process_snapshot_and_message_roundtrip():
    from app.services import chat_agent as ca
    from app.services.chat import Message
    state = ca._AgentState()
    assert state.process_snapshot() is None
    state.thoughts.append("先检索材料")
    state.note_tool_start("find_skill_material", "anysearch")
    state.note_tool_end("find_skill_material", "检索到材料", failed=False)
    state.note_tool_start("save_personal_skill", "")
    state.note_tool_end("save_personal_skill", "已生成草稿", failed=False)
    snap = state.process_snapshot()
    assert snap and len(snap["tools"]) == 2
    assert all(t["state"] == "end" and not t["failed"] for t in snap["tools"])
    # Message 携带快照序列化/反序列化不丢失
    msg = Message(role="assistant", content="答案", agent_process=snap)
    d = msg.to_dict()
    assert "thoughts" not in d["agent_process"]
    legacy = Message(role="assistant", content="答案", agent_process={**snap, "thoughts": ["内部推理"]})
    assert "thoughts" not in legacy.to_dict()["agent_process"]
    restored = Message(role="assistant", content="答案", agent_process=d["agent_process"])
    assert restored.agent_process["plan"] == snap["plan"]
    assert restored.agent_process["tools"] == snap["tools"]
    assert restored.agent_process["tool_sources"] == []
    assert restored.to_dict()["agent_process"] == d["agent_process"]
