"""截图检索（对话框粘贴图片 → OCR 识别 → 并入知识检索）的隔离回归。"""
import asyncio
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.core import db
from app.services import chat as chat_module
from app.services import tool_orchestrator
from app.services.chat import ChatService
from app.services.tools.base import ToolResult


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离库 + 一个用户 + 一个会话 + 一份合成图片附件（OCR 被 mock，不读图）。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "uploads"))
    db.execute(
        "INSERT INTO users(id,username,password_hash,salt,role,created_at,updated_at)"
        " VALUES (?,?,?,?,?,0,0)",
        ("alice", "alice", "synthetic", "synthetic", "user"),
    )
    service = ChatService()
    session = service.create_session(user_id="alice")
    file_id = str(uuid.uuid4())
    path = Path(settings.UPLOAD_DIR) / "chat_attachments" / f"{file_id}.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic screenshot")
    attachment = {"file_id": file_id, "path": str(path),
                  "filename": "截图_101530.png", "kind": "image"}
    yield service, session, attachment
    db._conn.close()


def collect(stream):
    """跑完 SSE 流并解析出事件对象列表。"""
    async def run():
        out = []
        async for raw in stream:
            if raw.startswith("data: "):
                out.append(json.loads(raw[6:].strip()))
        return out
    return asyncio.run(run())


def _rec(text="当归 红枣 补血", kind="image", success=True, **kw):
    return {"tool_name": "image_ocr", "filename": "截图.png",
            "success": success, "kind": kind, "text": text, **kw}


# ---------------------------------------------------------------------------
# 检索查询构建
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query,expected_query", [
    ("这个方子有什么功效", "这个方子有什么功效\n当归 红枣 补血"),
    ("", "当归 红枣 补血"),
    ("   ", "当归 红枣 补血"),
])
def test_ocr_text_is_merged_into_retrieval_query(query, expected_query):
    got, excerpt = tool_orchestrator.build_ocr_retrieval_query(query, [_rec()])
    assert got == expected_query
    assert excerpt == "当归 红枣 补血"


@pytest.mark.parametrize("records", [
    [],                                       # 无附件工具结果
    [_rec(success=False)],                    # OCR 失败
    [_rec(kind="word")],                      # 文档解析原文不参与截图检索
    [_rec(text="  ")],                        # 识别为空
    [{"confirmation": {"id": "c1"}, "kind": "image", "success": True, "text": "当归"}],
])
def test_retrieval_query_unchanged_without_usable_ocr(records):
    """无可用的图片识别文本时，检索查询保持用户原输入（行为与未贴图一致）。"""
    got, excerpt = tool_orchestrator.build_ocr_retrieval_query("失眠怎么调理", records)
    assert got == "失眠怎么调理" and excerpt == ""


def test_multiple_images_are_joined_in_order():
    got, excerpt = tool_orchestrator.build_ocr_retrieval_query(
        "", [_rec(text="当归"), _rec(text="红枣"), _rec(text="当归")])
    assert excerpt == "当归\n红枣" and got == "当归\n红枣"


def test_long_ocr_is_clipped_by_budget(monkeypatch):
    """检索查询过长会稀释向量/BM25 信号，按 OCR_RETRIEVAL_CHARS 预算截断。"""
    monkeypatch.setattr(settings, "OCR_RETRIEVAL_CHARS", 40)
    _, excerpt = tool_orchestrator.build_ocr_retrieval_query("", [_rec(text="当" * 200)])
    assert len(excerpt) == 40


# ---------------------------------------------------------------------------
# 附件工具结果回传识别原文
# ---------------------------------------------------------------------------

def test_handle_attachments_records_carry_ocr_text(env, monkeypatch):
    _, session, attachment = env
    monkeypatch.setattr(
        tool_orchestrator.image_ocr_tool, "run",
        AsyncMock(return_value=ToolResult(tool_name="image_ocr", success=True,
                                          text="当归 红枣 补血", summary="已识别图片")),
    )
    _, records = asyncio.run(tool_orchestrator.handle_attachments(
        "这个方子有什么功效", [attachment], "alice", "", session.session_id, "m1"))
    assert records[0]["kind"] == "image"
    assert records[0]["text"] == "当归 红枣 补血"
    # 落库保留原文摘要（裁剪后），便于审计追溯
    assert "当归" in db.query_one("SELECT output_json FROM tool_calls")["output_json"]


# ---------------------------------------------------------------------------
# 聊天流水线：OCR 文本真正进入检索
# ---------------------------------------------------------------------------

def _stub_pipeline(monkeypatch, ocr_text, rewrite_result="改写后检索式"):
    """把附件处理与检索链路替换为合成实现，返回记录入参的 dict。

    检索固定返回空结果 → 走 HITL need_search 分支结束本轮，无需真实 LLM 出网。
    """
    seen = {}
    monkeypatch.setattr(chat_module.LLMFactory, "create_for_role", lambda *a, **k: object())
    monkeypatch.setattr(chat_module.memory_service, "retrieve_relevant", lambda *a, **k: [])
    # 固定走 fast 流水线：Agent 分流会发起额外 LLM 调用，与本测试关注的检索链路无关
    monkeypatch.setattr(chat_module, "classify_complexity",
                        AsyncMock(return_value={"mode": "fast"}))
    monkeypatch.setattr(
        tool_orchestrator, "handle_attachments",
        AsyncMock(return_value=(f"【聊天框附件处理结果】\n{ocr_text}", [_rec(text=ocr_text)])),
    )

    async def fake_rewrite(q, provider=None):
        seen["rewrite_input"] = q
        # chat.py 意图识别改用 analyze_intent（返回 {query, scope}）
        return {"query": rewrite_result, "scope": "personal"}

    def fake_retrieve(q, user_id):
        seen["retrieve_input"] = q
        return []

    monkeypatch.setattr(chat_module, "analyze_intent", fake_rewrite)
    monkeypatch.setattr(chat_module.knowledge_service, "retrieve_for_user", fake_retrieve)
    return seen


def test_pasted_screenshot_without_text_retrieves_by_ocr(env, monkeypatch):
    """纯截图提问（不打字）：以图中文字作为检索查询与 HITL 卡片问题。"""
    service, session, attachment = env
    seen = _stub_pipeline(monkeypatch, "当归 红枣 枸杞 补血")
    events = collect(service.chat_stream(session.session_id, "", use_knowledge=True,
                                        user_id="alice", attachments=[attachment]))
    phases = [e.get("phase") for e in events if e.get("event") == "status"]
    assert "ocr_retrieval" in phases
    assert seen["rewrite_input"] == "当归 红枣 枸杞 补血"
    assert seen["retrieve_input"] == "改写后检索式"
    card = next(e for e in events if e.get("event") == "need_search")
    assert card["query"] == "当归 红枣 枸杞 补血"
    # 识别原文不外泄到前端事件（完整内容只进对话上下文与 trace segment）
    tool_done = next(e for e in events if e.get("phase") == "tool_done")
    assert all("text" not in c for c in tool_done["tool_calls"])


def test_screenshot_with_question_keeps_user_text(env, monkeypatch):
    """截图 + 文本：检索兼顾两者，卡片仍展示用户原问题。"""
    service, session, attachment = env
    seen = _stub_pipeline(monkeypatch, "当归 红枣 枸杞 补血")
    events = collect(service.chat_stream(session.session_id, "这个方子有什么功效",
                                        use_knowledge=True, user_id="alice",
                                        attachments=[attachment]))
    assert seen["rewrite_input"] == "这个方子有什么功效\n当归 红枣 枸杞 补血"
    card = next(e for e in events if e.get("event") == "need_search")
    assert card["query"] == "这个方子有什么功效"


def test_long_screenshot_card_query_is_clipped(env, monkeypatch):
    """长截图：卡片问题被截断，避免整段识别文字占满卡片。"""
    service, session, attachment = env
    _stub_pipeline(monkeypatch, "当" * 200)
    events = collect(service.chat_stream(session.session_id, "", use_knowledge=True,
                                        user_id="alice", attachments=[attachment]))
    card = next(e for e in events if e.get("event") == "need_search")["query"]
    assert card.endswith("…") and len(card) <= chat_module._CARD_QUERY_CHARS + 1


def test_plain_text_question_retrieval_unchanged(env, monkeypatch):
    """无附件的普通提问：检索链路行为与改造前完全一致。"""
    service, session, _ = env
    seen = _stub_pipeline(monkeypatch, "不会被用到")
    events = collect(service.chat_stream(session.session_id, "失眠怎么调理",
                                        use_knowledge=True, user_id="alice"))
    assert seen["rewrite_input"] == "失眠怎么调理"
    assert "ocr_retrieval" not in [e.get("phase") for e in events if e.get("event") == "status"]
    assert next(e for e in events if e.get("event") == "need_search")["query"] == "失眠怎么调理"
