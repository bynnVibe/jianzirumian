"""DeepEval 风格评测指标回归测试（内存 DB / mock LLM，不出网）。

覆盖计划第五节：
  D1 LLMTestCase 用例抽象（input/actual_output/expected_output/retrieval_context）；
  D2 指标配置 normalize_metrics：空/非法 → 默认 Ragas 四指标（存量兼容）；
  D3 HallucinationMetric：原子陈述矛盾判定、矛盾率口径、无上下文/坏 JSON 降级；
  D4 GEval：1-10 分归一化 0~1、坏 JSON/越界降级、准则缺省；
  D5 evaluate_case 并发 + Ragas 委托既有实现（单一事实源）+ 单点异常降级；
  D6 evaluator_metric_config：评估器 metrics 列解析（对象/数组/空值兼容）。
"""
import asyncio
import sqlite3

import pytest

from app.core import db
from app.services import deepeval_metrics as dm


@pytest.fixture()
def mem_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    yield conn
    conn.close()


def _case(answer="黄芪补气。", contexts=None):
    return dm.LLMTestCase(input="黄芪有什么功效？", actual_output=answer,
                          expected_output="补气固表", retrieval_context=["黄芪补气固表"] if contexts is None else contexts)


# ============================================
# D1 用例抽象
# ============================================
def test_llm_test_case_defaults_and_context_text():
    tc = dm.LLMTestCase(input="q", actual_output="a")
    assert tc.expected_output == "" and tc.retrieval_context == []
    assert tc.context_text == ""
    tc = _case(contexts=["片段一", "  ", "", "片段二"])
    assert tc.context_text == "片段一\n\n片段二"  # 过滤空白片段


# ============================================
# D2 指标配置收敛（存量兼容）
# ============================================
def test_normalize_metrics_empty_and_invalid_fallback_default():
    default = list(dm.RAGAS_METRICS)
    for raw in (None, "", "   ", "not-json", "{bad", 123, [], ["unknown"]):
        assert dm.normalize_metrics(raw) == default


def test_normalize_metrics_valid_forms():
    assert dm.normalize_metrics(["faithfulness", "geval"]) == ["faithfulness", "geval"]
    assert dm.normalize_metrics('["hallucination"]') == ["hallucination"]
    assert dm.normalize_metrics({"metrics": ["geval"], "geval_criteria": "x"}) == ["geval"]
    # 非法名被过滤，保留合法子集
    assert dm.normalize_metrics(["geval", "bogus"]) == ["geval"]
    assert dm.ALL_METRICS == dm.RAGAS_METRICS + ("hallucination", "geval")


# ============================================
# D3 HallucinationMetric
# ============================================
def test_hallucination_contradiction_rate(monkeypatch):
    from app.services import evaluation as ev

    async def fake_extract(text, provider=None):
        return ["黄芪补气", "黄芪有毒"]
    monkeypatch.setattr(ev, "_extract_statements", fake_extract)

    async def fake_generate(messages, provider=None):
        return '{"verdicts": [0, 1]}'
    monkeypatch.setattr(ev, "_generate", fake_generate)
    score, detail = asyncio.run(dm.measure_hallucination(_case()))
    assert score == 0.5  # 1/2 陈述与上下文矛盾
    assert detail["verdicts"] == [0, 1]


def test_hallucination_verdicts_coerced(monkeypatch):
    from app.services import evaluation as ev

    async def fake_extract(text, provider=None):
        return ["s1", "s2", "s3"]
    monkeypatch.setattr(ev, "_extract_statements", fake_extract)

    async def fake_generate(messages, provider=None):
        return '{"verdicts": [1]}'  # 长度不足 → 补 0
    monkeypatch.setattr(ev, "_generate", fake_generate)
    score, detail = asyncio.run(dm.measure_hallucination(_case()))
    assert detail["verdicts"] == [1, 0, 0] and score == round(1 / 3, 4)


def test_hallucination_no_answer_or_context_returns_none():
    score, _ = asyncio.run(dm.measure_hallucination(_case(answer="")))
    assert score is None
    score, _ = asyncio.run(dm.measure_hallucination(_case(contexts=[])))
    assert score is None


def test_hallucination_bad_json_degrades_none(monkeypatch):
    from app.services import evaluation as ev

    async def fake_extract(text, provider=None):
        return ["s1"]
    monkeypatch.setattr(ev, "_extract_statements", fake_extract)

    async def fake_generate(messages, provider=None):
        return "无法判定"
    monkeypatch.setattr(ev, "_generate", fake_generate)
    score, _ = asyncio.run(dm.measure_hallucination(_case()))
    assert score is None


# ============================================
# D4 GEval
# ============================================
def test_parse_geval_normalization():
    assert dm._parse_geval('{"score": 10, "reason": "满分"}') == (1.0, "满分")
    assert dm._parse_geval('{"score": 1, "reason": ""}') == (0.0, "")
    assert dm._parse_geval('{"score": 8, "reason": "良好"}')[0] == round(7 / 9, 4)
    assert dm._parse_geval('```json\n{"score": 5, "reason": "中"}\n```')[0] == round(4 / 9, 4)


def test_parse_geval_invalid_inputs():
    for raw in ("", "不是 JSON", '{"score": "abc"}', '{"score": 0}', '{"score": 11}', "{}"):
        assert dm._parse_geval(raw) == (None, "")


def test_measure_geval_success_and_criteria_default(monkeypatch):
    from app.services import evaluation as ev
    from app.config import settings
    captured = {}

    async def fake_generate(messages, provider=None):
        captured["messages"] = messages
        return '{"score": 9, "reason": "覆盖准则"}'
    monkeypatch.setattr(ev, "_generate", fake_generate)
    score, detail = asyncio.run(dm.measure_geval(_case(), criteria=""))
    assert score == round(8 / 9, 4)
    assert detail["criteria"] == settings.ONLINE_EVAL_GEVAL_CRITERIA  # 空准则取默认
    score, detail = asyncio.run(dm.measure_geval(_case(), criteria="自定义准则"))
    assert detail["criteria"] == "自定义准则"
    assert "自定义准则" in captured["messages"][1]["content"]


def test_measure_geval_empty_answer_and_failure(monkeypatch):
    from app.services import evaluation as ev
    score, _ = asyncio.run(dm.measure_geval(_case(answer="")))
    assert score is None

    async def boom(messages, provider=None):
        raise RuntimeError("模型不可用")
    monkeypatch.setattr(ev, "_generate", boom)
    score, detail = asyncio.run(dm.measure_geval(_case()))
    assert score is None and detail["reason"] == ""


# ============================================
# D5 evaluate_case：委托 + 并发 + 单点降级
# ============================================
def test_evaluate_case_delegates_ragas_and_runs_deepeval(monkeypatch):
    from app.services import evaluation as ev
    calls = []

    async def fake_statement(text, context, provider=None):
        calls.append(("statement", text))
        return 0.9, {}

    async def fake_relevance(q, a, provider=None):
        calls.append(("relevance", q))
        return 0.8, {}

    async def fake_precision(q, ref, contexts, provider=None):
        calls.append(("precision", tuple(contexts)))
        return 0.7, {}

    async def fake_generate(messages, provider=None):
        if "幻觉检测器" in messages[0]["content"]:
            return '{"verdicts": [0]}'
        return '{"score": 10, "reason": "ok"}'
    monkeypatch.setattr(ev, "_statement_metric", fake_statement)
    monkeypatch.setattr(ev, "_compute_answer_relevance", fake_relevance)
    monkeypatch.setattr(ev, "_compute_context_precision", fake_precision)
    monkeypatch.setattr(ev, "_extract_statements",
                        lambda text, provider=None: _async_list(["s1"]))
    monkeypatch.setattr(ev, "_generate", fake_generate)
    tc = _case()
    out = asyncio.run(dm.evaluate_case(tc, list(dm.ALL_METRICS)))
    assert set(out) == set(dm.ALL_METRICS)
    assert out["faithfulness"][0] == 0.9
    assert out["answer_relevancy"][0] == 0.8
    assert out["contextual_precision"][0] == 0.7
    assert out["contextual_recall"][0] == 0.9  # 委托同一 _statement_metric
    assert out["hallucination"][0] == 0.0
    assert out["geval"][0] == 1.0
    assert ("precision", ("黄芪补气固表",)) in calls


def _async_list(items):
    async def _coro():
        return items
    return _coro()


def test_evaluate_case_single_metric_exception_degrades(monkeypatch):
    from app.services import evaluation as ev

    async def boom(*a, **k):
        raise RuntimeError("单点失败")
    monkeypatch.setattr(ev, "_statement_metric", boom)

    async def fake_generate(messages, provider=None):
        return '{"score": 6, "reason": "ok"}'
    monkeypatch.setattr(ev, "_generate", fake_generate)
    out = asyncio.run(dm.evaluate_case(_case(), ["faithfulness", "geval"]))
    assert out["faithfulness"] == (None, {})  # 异常降级不中断
    assert out["geval"][0] == round(5 / 9, 4)


def test_evaluate_case_empty_metrics_uses_default(monkeypatch):
    from app.services import evaluation as ev

    async def fake_statement(text, context, provider=None):
        return 1.0, {}
    monkeypatch.setattr(ev, "_statement_metric", fake_statement)
    monkeypatch.setattr(ev, "_compute_answer_relevance",
                        lambda *a, **k: _async_tuple((1.0, {})))
    monkeypatch.setattr(ev, "_compute_context_precision",
                        lambda *a, **k: _async_tuple((1.0, {})))
    out = asyncio.run(dm.evaluate_case(_case(), None))
    assert set(out) == set(dm.RAGAS_METRICS)  # 空配置=默认四指标


def _async_tuple(value):
    async def _coro():
        return value
    return _coro()


# ============================================
# D6 评估器指标配置解析（DB）
# ============================================
def _insert_evaluator(metrics_raw):
    db.execute(
        "INSERT INTO eval_evaluators (id, name, dataset_id, description, created_by,"
        " created_at, metrics) VALUES ('ev1','测试','ds1','','admin','2026-01-01',?)",
        (metrics_raw,))


def test_evaluator_metric_config_forms(mem_db):
    from app.services import evaluation as ev
    assert ev.evaluator_metric_config("") == (None, "")
    assert ev.evaluator_metric_config("missing") == (None, "")
    _insert_evaluator("")
    assert ev.evaluator_metric_config("ev1") == (None, "")  # 存量空配置
    db.execute("UPDATE eval_evaluators SET metrics = ? WHERE id = 'ev1'",
               ('{"metrics": ["hallucination", "geval"], "geval_criteria": "准确有用"}',))
    metrics, criteria = ev.evaluator_metric_config("ev1")
    assert metrics == ["hallucination", "geval"] and criteria == "准确有用"
    db.execute("UPDATE eval_evaluators SET metrics = ? WHERE id = 'ev1'",
               ('["faithfulness"]',))  # 兼容纯数组形态
    assert ev.evaluator_metric_config("ev1")[0] == ["faithfulness"]
    db.execute("UPDATE eval_evaluators SET metrics = '坏JSON' WHERE id = 'ev1'")
    assert ev.evaluator_metric_config("ev1") == (None, "")