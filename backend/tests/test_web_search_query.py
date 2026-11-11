"""联网搜索查询精炼与相关性护栏回归测试（需求：问句搜索返回无关裁判文书的根因修复）。

根因：搜索引擎（AnySearch）对完整口语问句排序失真，返回法院裁判文书；
对空格分隔关键词则返回相关健康科普。修复在 web_search() 统一入口做查询精炼 +
裁判文书主导时重试。本测试不触网，mock 提供商验证行为。
"""
import asyncio

import pytest

from app.services import web_search as ws
from app.services.web_search import (
    SearchResult,
    refine_web_query,
    _legal_dominated,
    _is_legal_query,
)


# ---- 查询精炼 ----
def test_refine_sentence_to_keywords():
    #  reported case：口语问句 → 空格分隔关键词 + 方法意图词
    assert refine_web_query("失眠可以怎么缓解") == "失眠 缓解 方法"


def test_refine_idempotent_for_keywords():
    assert refine_web_query("失眠 缓解 方法") == "失眠 缓解 方法"


def test_refine_removes_possessive():
    assert refine_web_query("红枣枸杞的功效") == "红枣枸杞 功效"


def test_refine_empty_and_stopword_only():
    assert refine_web_query("") == ""
    assert refine_web_query("   ") == ""
    # 全停用词无实词时回退原句，不返回空
    assert refine_web_query("可以吗") == "可以吗"


def test_refine_howto_appends_method():
    out = refine_web_query("如何改善睡眠")
    assert "方法" in out
    assert "改善睡眠" in out


# ---- 相关性护栏判定 ----
def _legal_results(n=5):
    return [SearchResult(f"判决书{i}", f"https://wenshu.court.gov.cn/x{i}", "失眠") for i in range(n)]


def _health_results(n=5):
    return [SearchResult(f"失眠科普{i}", f"https://www.mayoclinic.org/insomnia{i}", "失眠") for i in range(n)]


def test_legal_dominated_true():
    assert _legal_dominated(_legal_results()) is True


def test_legal_dominated_false_for_health():
    assert _legal_dominated(_health_results()) is False


def test_legal_dominated_empty():
    assert _legal_dominated([]) is False


def test_is_legal_query():
    assert _is_legal_query("医疗纠纷怎么起诉") is True
    assert _is_legal_query("失眠可以怎么缓解") is False


# ---- web_search 统一入口：精炼 + 重试 ----
class _FakeSearcher:
    def __init__(self, responses):
        # responses: list of result-lists, consumed per search call
        self.responses = list(responses)
        self.queries = []

    async def search(self, query, max_results=5):
        self.queries.append(query)
        return self.responses.pop(0) if self.responses else []


def test_web_search_refines_query(monkeypatch):
    fake = _FakeSearcher([_health_results()])
    monkeypatch.setattr(ws.WebSearchFactory, "create", staticmethod(lambda provider=None: fake))
    rs = asyncio.run(ws.web_search("失眠可以怎么缓解", 5))
    assert fake.queries == ["失眠 缓解 方法"]  # 发送的是精炼关键词而非原问句
    assert len(rs) == 5


def test_web_search_retries_on_legal_domination(monkeypatch):
    # 第一次返回裁判文书主导 → 触发护栏重试；第二次返回健康结果 → 采用重试结果
    fake = _FakeSearcher([_legal_results(), _health_results()])
    monkeypatch.setattr(ws.WebSearchFactory, "create", staticmethod(lambda provider=None: fake))
    rs = asyncio.run(ws.web_search("失眠可以怎么缓解", 5))
    assert len(fake.queries) == 2
    assert "科普" in fake.queries[1]
    assert all("mayoclinic" in r.url for r in rs)


def test_web_search_no_retry_for_legal_query(monkeypatch):
    # 法律类问题即使返回裁判文书也不重试（属相关结果）
    fake = _FakeSearcher([_legal_results()])
    monkeypatch.setattr(ws.WebSearchFactory, "create", staticmethod(lambda provider=None: fake))
    rs = asyncio.run(ws.web_search("医疗纠纷判决书", 5))
    assert len(fake.queries) == 1
    assert len(rs) == 5


def test_web_search_keeps_original_when_retry_still_legal(monkeypatch):
    fake = _FakeSearcher([_legal_results(), _legal_results()])
    monkeypatch.setattr(ws.WebSearchFactory, "create", staticmethod(lambda provider=None: fake))
    rs = asyncio.run(ws.web_search("失眠可以怎么缓解", 5))
    assert len(fake.queries) == 2
    assert len(rs) == 5  # 重试仍无关时保留原结果，不丢空