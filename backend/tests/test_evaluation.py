"""Agent 回归评测模块单元测试（纯函数，不依赖 DB / LLM / 向量库）"""
from app.services.evaluation import (
    _compute_retrieval_hit,
    _compute_summary,
    _parse_judge,
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
def _row(status="ok", hit=1, faith=1, score=4.0, latency=1000):
    return {
        "status": status,
        "retrieval_hit": hit,
        "faithfulness_pass": faith,
        "judge_score": score,
        "latency_ms": latency,
    }


def test_summary_basic():
    results = [
        _row(hit=1, faith=1, score=5.0, latency=1000),
        _row(hit=1, faith=0, score=3.0, latency=2000),
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
