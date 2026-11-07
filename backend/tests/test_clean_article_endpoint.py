"""链接正文「内容润色」端点（/knowledge/clean-article）回归测试（内存 DB + 假 LLM，不出网）。

覆盖：
  - 无润色方向且未启用清洗策略：返回原文并提示；
  - 有润色方向：走网关 content_polish 角色做 LLM 改写，提取 <polished> 结果；
  - 有润色方向但 LLM 抛异常：兜底返回清洗/原文，不 500；
  - 无方向且启用清洗策略：仅做确定性清洗（不触发 LLM）。
"""
import asyncio
import sqlite3

import pytest

from app.api import knowledge as knowledge_api
from app.api.knowledge import CleanArticleRequest
from app.core import db
from app.services.skill import SkillService


@pytest.fixture()
def skill_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    yield conn
    conn.close()


class _FakeLLM:
    def __init__(self, chunks):
        self._chunks = chunks

    async def chat(self, messages, stream=True):
        for c in self._chunks:
            yield c


class _RaisingLLM:
    async def chat(self, messages, stream=True):
        raise RuntimeError("upstream down")
        yield  # pragma: no cover


class _FakeFactory:
    role_seen = None
    llm = None

    @staticmethod
    def create_for_role(role, *args, **kwargs):
        _FakeFactory.role_seen = role
        return _FakeFactory.llm


@pytest.fixture()
def fake_llm(monkeypatch):
    monkeypatch.setattr(knowledge_api, "LLMFactory", _FakeFactory)
    _FakeFactory.role_seen = None
    return _FakeFactory


def _run(body, owner="u1"):
    return asyncio.run(knowledge_api.clean_article(body, {"id": owner, "username": owner}))


def test_no_direction_and_policy_off_returns_original(skill_db):
    SkillService().ensure_builtin("u1")
    # 关闭内置清洗策略 → policy 全关
    bid = next(s["id"] for s in SkillService().list_skills("u1") if s["scope"] == "polish")
    SkillService().update_skill("u1", bid, enabled=False)
    res = _run(CleanArticleRequest(text="正文 一句", direction=""))
    assert res["success"] is True
    assert res["cleaned_text"] == "正文 一句"
    assert "润色方向" in res["message"]


def test_direction_uses_content_polish_role_and_extracts_polished(skill_db, fake_llm):
    SkillService().ensure_builtin("u1")
    bid = next(s["id"] for s in SkillService().list_skills("u1") if s["scope"] == "polish")
    SkillService().update_skill("u1", bid, enabled=False)  # 仅验证 LLM 分支
    fake_llm.llm = _FakeLLM(["<polished>", "更通俗的正文一句", "</polished>"])
    res = _run(CleanArticleRequest(text="正文 一句", direction="更通俗"))
    assert fake_llm.role_seen == "content_polish"
    assert res["success"] is True
    assert res["cleaned_text"] == "更通俗的正文一句"
    assert "LLM 润色" in res["message"]


def test_direction_llm_failure_falls_back_to_cleaned(skill_db, fake_llm):
    SkillService().ensure_builtin("u1")
    fake_llm.llm = _RaisingLLM()
    res = _run(CleanArticleRequest(text="扫码关注\n正文 一句[1]", direction="压缩"))
    assert res["success"] is True
    # LLM 失败 → 兜底返回确定性清洗结果（去噪声行/角标，单个空格保留）
    assert res["cleaned_text"] == "正文 一句"
    assert "LLM 润色失败" in res["message"]


def test_no_direction_policy_on_only_deterministic_clean(skill_db, fake_llm):
    SkillService().ensure_builtin("u1")  # 内置清洗策略默认开启
    fake_llm.llm = _FakeLLM(["<polished>不应出现</polished>"])
    res = _run(CleanArticleRequest(text="扫码关注\n正文 一句[1]", direction=""))
    # 未走 LLM
    assert fake_llm.role_seen is None
    assert res["cleaned_text"] == "正文 一句"
