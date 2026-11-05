"""复杂度分类器与意图澄清模块单元测试（mock LLM，不依赖网络/DB/向量库）。

覆盖：
  - query_complexity：启发式预筛直接 fast（不调用 LLM）、LLM 三级分类映射、
    markdown 包裹解析、非法 level 兜底 simple、异常/超时保底 fast。
  - intent_clarifier：should_clarify 解析、缺问题文本降级为不澄清、异常保底不澄清。
"""
import asyncio

from app.core import intent_clarifier as ic
from app.core import query_complexity as qc


class ScriptedLLM:
    """按脚本依次返回响应；chat 为异步生成器（与真实 BaseLLM 流式协议一致）。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def chat(self, messages, stream=True):
        self.calls += 1
        text = self._responses.pop(0) if self._responses else ""
        if text:
            mid = max(1, len(text) // 2)
            yield text[:mid]
            yield text[mid:]


def _patch_llm(monkeypatch, module, responses):
    llm = ScriptedLLM(responses)
    monkeypatch.setattr(module.LLMFactory, "create_for_role", lambda *a, **k: llm)
    return llm


def _forbid_llm(monkeypatch, module):
    monkeypatch.setattr(
        module.LLMFactory, "create_for_role",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("预筛命中时不应调用 LLM")),
    )


# ============================================
# query_complexity：启发式预筛
# ============================================
def test_prescreen_short_query_is_fast_without_llm(monkeypatch):
    _forbid_llm(monkeypatch, qc)
    res = asyncio.run(qc.classify_complexity("红枣的功效"))
    assert res["mode"] == "fast"
    assert res["level"] == "simple"


def test_prescreen_empty_query_is_fast(monkeypatch):
    _forbid_llm(monkeypatch, qc)
    assert asyncio.run(qc.classify_complexity("   "))["mode"] == "fast"


def test_prescreen_multi_step_hint_defers_to_llm(monkeypatch):
    # 含"对比"多步信号 → 预筛不命中，交 LLM 细判
    llm = _patch_llm(monkeypatch, qc, ['{"level":"moderate","reason":"轻度对比"}'])
    res = asyncio.run(qc.classify_complexity("对比人参和黄芪的补气差异"))
    assert res["mode"] == "react"
    assert llm.calls == 1


# ============================================
# query_complexity：LLM 三级分类映射
# ============================================
def test_llm_complex_maps_plan_execute(monkeypatch):
    _patch_llm(monkeypatch, qc, ['{"level":"complex","reason":"需多步规划"}'])
    res = asyncio.run(qc.classify_complexity(
        "先检索穴位知识，再结合联网资料，为我制定一份针对失眠的综合调理方案并说明每步理由"
    ))
    assert res["level"] == "complex"
    assert res["mode"] == "plan_execute"


def test_llm_moderate_maps_react(monkeypatch):
    _patch_llm(monkeypatch, qc, ['{"level":"moderate","reason":"需两次检索"}'])
    assert asyncio.run(qc.classify_complexity("对比人参和黄芪的补气差异"))["mode"] == "react"


def test_llm_simple_maps_fast(monkeypatch):
    _patch_llm(monkeypatch, qc, ['{"level":"simple","reason":"单一意图"}'])
    res = asyncio.run(qc.classify_complexity("请分析一下黄芪这味药材的主要功效到底是什么"))
    assert res["level"] == "simple"
    assert res["mode"] == "fast"


def test_llm_markdown_wrapped_json_parsed(monkeypatch):
    _patch_llm(monkeypatch, qc, ['```json\n{"level":"complex","reason":"多步"}\n```'])
    assert asyncio.run(qc.classify_complexity("对比人参和黄芪的补气差异"))["mode"] == "plan_execute"


def test_llm_invalid_level_defaults_simple(monkeypatch):
    _patch_llm(monkeypatch, qc, ['{"level":"weird","reason":"x"}'])
    res = asyncio.run(qc.classify_complexity("对比人参和黄芪的补气差异"))
    assert res["level"] == "simple"
    assert res["mode"] == "fast"


# ============================================
# query_complexity：保底 fast
# ============================================
def test_llm_exception_falls_back_fast(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("合成模型失败")
    monkeypatch.setattr(qc.LLMFactory, "create_for_role", boom)
    res = asyncio.run(qc.classify_complexity("对比人参和黄芪的补气差异"))
    assert res["mode"] == "fast"


def test_llm_timeout_falls_back_fast(monkeypatch):
    class HangingLLM:
        async def chat(self, messages, stream=True):
            await asyncio.sleep(0.2)
            yield '{"level":"complex"}'
    monkeypatch.setattr(qc.LLMFactory, "create_for_role", lambda *a, **k: HangingLLM())
    monkeypatch.setattr(qc, "CLASSIFY_TIMEOUT", 0.01)
    res = asyncio.run(qc.classify_complexity("对比人参和黄芪的补气差异"))
    assert res["mode"] == "fast"


# ============================================
# intent_clarifier：意图澄清判定
# ============================================
def test_clarify_should_true_parsed(monkeypatch):
    _patch_llm(monkeypatch, ic, ['{"should_clarify":true,"question":"你想了解哪个人群？","options":["老年人","上班族"]}'])
    res = asyncio.run(ic.decide_clarification("怎么调理", "少量证据", "缺少人群限定"))
    assert res["should_clarify"] is True
    assert res["question"] == "你想了解哪个人群？"
    assert res["options"] == ["老年人", "上班族"]


def test_clarify_true_without_question_degrades_false(monkeypatch):
    _patch_llm(monkeypatch, ic, ['{"should_clarify":true,"question":""}'])
    res = asyncio.run(ic.decide_clarification("怎么调理", "", ""))
    assert res["should_clarify"] is False


def test_clarify_should_false(monkeypatch):
    _patch_llm(monkeypatch, ic, ['{"should_clarify":false}'])
    res = asyncio.run(ic.decide_clarification("黄芪功效", "充分证据", ""))
    assert res["should_clarify"] is False


def test_clarify_options_capped_at_four(monkeypatch):
    _patch_llm(monkeypatch, ic, ['{"should_clarify":true,"question":"补充哪方面？","options":["a","b","c","d","e","f"]}'])
    res = asyncio.run(ic.decide_clarification("怎么调理", "", ""))
    assert len(res["options"]) == 4


def test_clarify_exception_falls_back_false(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("合成模型失败")
    monkeypatch.setattr(ic.LLMFactory, "create_for_role", boom)
    res = asyncio.run(ic.decide_clarification("怎么调理", "", ""))
    assert res == {"should_clarify": False, "question": "", "options": []}
