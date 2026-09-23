"""Agent 回归评测模块单元测试（纯函数，不依赖 DB / LLM / 向量库）"""
from app.services.evaluation import (
    _compute_retrieval_hit,
    _compute_summary,
    _coerce_verdicts,
    _cosine,
    _parse_judge,
    _split_sentences,
    _weighted_precision,
)


# ============================================
# 检索命中判定
# ============================================
def test_retrieval_hit_by_keyword():
    ctx = "[来源 1]:\n黄芪具有补气固表的功效\n"
    assert _compute_retrieval_hit(["黄芪", "补气"], "", ctx, []) == 1


def test_retrieval_hit_keyword_case_insensitive():
    ctx = "Ashwagandha helps with stress"
    assert _compute_retrieval_hit(["ashwagandha"], "", ctx, []) == 1


def test_retrieval_hit_by_source_title():
    sources = [{"wiki_title": "", "kb_name": "养生库", "source_image": "/data/uploads/黄芪笔记.jpg", "text": "无关内容"}]
    assert _compute_retrieval_hit([], "黄芪笔记", "", sources) == 1


def test_retrieval_hit_miss():
    ctx = "[来源 1]:\n人参的作用\n"
    sources = [{"wiki_title": "人参", "kb_name": "库", "source_image": "a.jpg", "text": "人参"}]
    assert _compute_retrieval_hit(["黄芪"], "枸杞", ctx, sources) == 0


def test_retrieval_hit_no_signal():
    assert _compute_retrieval_hit([], "", "任意上下文", []) == 0


# ============================================
# judge 输出解析
# ============================================
def test_parse_judge_plain():
    assert _parse_judge('{"score": 4, "reason": "覆盖主要要点"}') == {"score": 4, "reason": "覆盖主要要点"}


def test_parse_judge_markdown_wrapped():
    raw = '```json\n{"score": 5, "reason": "完全一致"}\n```'
    parsed = _parse_judge(raw)
    assert parsed["score"] == 5


def test_parse_judge_with_noise():
    parsed = _parse_judge('好的，评分如下：{"score": 3, "reason": "部分遗漏"} 以上。')
    assert parsed["score"] == 3


def test_parse_judge_invalid():
    assert _parse_judge("这不是 JSON") is None
    assert _parse_judge("") is None


# ============================================
# 聚合 summary
# ============================================
def _row(status="ok", hit=1, faith=1, score=4.0, latency=1000,
         f=None, rel=None, prec=None, rec=None):
    return {
        "status": status,
        "retrieval_hit": hit,
        "faithfulness_pass": faith,
        "faithfulness": f,
        "answer_relevance": rel,
        "context_precision": prec,
        "context_recall": rec,
        "judge_score": score,
        "latency_ms": latency,
    }


def test_summary_basic():
    results = [
        _row(hit=1, faith=1, score=5.0, latency=1000, f=1.0),
        _row(hit=1, faith=0, score=3.0, latency=2000, f=0.5),
        _row(hit=0, faith=0, score=None, latency=3000),
    ]
    s = _compute_summary(results)
    assert s["total"] == 3
    assert s["ok"] == 3
    assert s["error"] == 0
    assert s["retrieval_hit_rate"] == round(2 / 3, 4)
    assert s["faithfulness_pass_rate"] == round(1 / 3, 4)
    assert s["avg_judge_score"] == 4.0  # (5+3)/2，None 不计入
    assert s["avg_latency_ms"] == 2000.0
    assert s["avg_faithfulness"] == 0.75  # (1.0+0.5)/2，None 不计入
    assert s["avg_answer_relevance"] is None


def test_summary_with_error_rows_excluded_from_rates():
    results = [
        _row(hit=1, faith=1, score=4.0, latency=1000),
        _row(status="error", hit=0, faith=0, score=None, latency=0),
    ]
    s = _compute_summary(results)
    assert s["total"] == 2
    assert s["ok"] == 1
    assert s["error"] == 1
    # 错误用例不计入率的分母
    assert s["retrieval_hit_rate"] == 1.0
    assert s["faithfulness_pass_rate"] == 1.0


def test_summary_all_error():
    results = [_row(status="error"), _row(status="error")]
    s = _compute_summary(results)
    assert s["ok"] == 0
    assert s["retrieval_hit_rate"] == 0.0
    assert s["avg_judge_score"] is None
    assert s["avg_latency_ms"] == 0.0
    assert s["avg_faithfulness"] is None
    assert s["avg_context_recall"] is None


def test_summary_ragas_averages_ignore_none():
    results = [
        _row(f=1.0, rel=0.8, prec=0.5, rec=1.0),
        _row(f=0.5, rel=None, prec=0.0, rec=0.0),
        _row(status="error"),
    ]
    s = _compute_summary(results)
    assert s["avg_faithfulness"] == 0.75
    assert s["avg_answer_relevance"] == 0.8  # 仅一条非空
    assert s["avg_context_precision"] == 0.25
    assert s["avg_context_recall"] == 0.5


# ============================================
# Ragas 上下文精确率（加权 precision@k）
# ============================================
def test_weighted_precision_all_relevant():
    assert _weighted_precision([1, 1, 1]) == 1.0


def test_weighted_precision_rank_aware():
    # 相关在第 1、3 位：precision@1=1/1，precision@3=2/3 → (1 + 2/3)/2
    assert _weighted_precision([1, 0, 1]) == round((1 + 2 / 3) / 2, 4)


def test_weighted_precision_no_relevant():
    assert _weighted_precision([0, 0]) == 0.0
    assert _weighted_precision([]) == 0.0


# ============================================
# Ragas 辅助纯函数
# ============================================
def test_coerce_verdicts_pad_truncate_and_bad_values():
    assert _coerce_verdicts([1, "0"], 3) == [1, 0, 0]   # 不足补 0
    assert _coerce_verdicts([1, 1, 1], 2) == [1, 1]     # 超出截断
    assert _coerce_verdicts([None, 2], 2) == [0, 0]     # 非法值记 0


def test_cosine():
    assert _cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert _cosine([], []) == 0.0


def test_split_sentences():
    assert _split_sentences("黄芪补气。固表！\n止汗") == ["黄芪补气", "固表", "止汗"]
    assert _split_sentences("") == []
