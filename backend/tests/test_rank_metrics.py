"""RAG 检索层排名指标回归测试（纯函数为主；hit_k 配置需隔离 DB）。

覆盖计划第三节 Hit@K/MRR：
  R1 _source_relevant 逐位相关判定（关键词命中片段/chunks、来源标题互含、大小写、无信号）；
  R2 _rank_metrics：Hit@K / MRR=1/首命中排名 / first_hit_rank / K 边界 / 无期望全 0 / K 归一化；
  R3 与 _compute_retrieval_hit 口径一致（委托后命中判定不变）；
  R4 _compute_summary：avg_hit_at_k 命中率 / avg_mrr 非空均值 / 错误用例排除；
  R5 evaluator_hit_k：缺省/自定义/非法/空 id/非正 → settings.RETRIEVAL_HIT_K 默认。
"""
import sqlite3
from datetime import datetime

import pytest

from app.config import settings
from app.core import db
from app.services.evaluation import (
    _compute_retrieval_hit,
    _compute_summary,
    _rank_metrics,
    _source_relevant,
    evaluator_hit_k,
)


@pytest.fixture()
def mem_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    yield conn
    conn.close()


# ============================================
# R1 逐位相关判定
# ============================================
def test_source_relevant_keyword_in_text_and_chunks():
    assert _source_relevant(["黄芪"], "", {"text": "黄芪补气固表"})
    assert _source_relevant(["枸杞"], "", {"chunks": [{"text": "枸杞明目"}]})
    assert _source_relevant(["ashwagandha"], "", {"text": "Ashwagandha helps"})  # 大小写无关
    assert not _source_relevant(["黄芪"], "", {"text": "人参的作用"})
    assert not _source_relevant(["   "], "", {"text": "任意"})  # 空白关键词忽略


def test_source_relevant_title_fields_mutual_containment():
    assert _source_relevant([], "黄芪笔记", {"document_title": "黄芪笔记.pdf"})
    assert _source_relevant([], "养生", {"kb_name": "养生库"})
    assert _source_relevant([], "黄芪笔记", {"source_image": "/data/uploads/黄芪笔记.jpg"})
    assert _source_relevant([], "人参", {"wiki_title": "人参"})
    assert _source_relevant([], "枸杞", {"chunks": [{"wiki_title": "枸杞"}]})
    # 反向互含：标题字段是期望来源的子串
    assert _source_relevant([], "黄芪笔记大全", {"document_title": "黄芪笔记"})
    assert not _source_relevant([], "枸杞", {"document_title": "人参"})


def test_source_relevant_no_signal():
    assert not _source_relevant([], "", {"text": "任意内容"})
    assert not _source_relevant(None, None, {})


# ============================================
# R2 Hit@K / MRR / 首命中排名
# ============================================
def _src(text):
    return {"text": text}


def test_rank_metrics_first_position():
    sources = [_src("黄芪补气"), _src("无关")]
    assert _rank_metrics(["黄芪"], "", sources, 5) == (1, 1.0, 1)


def test_rank_metrics_rank_three_within_and_beyond_k():
    sources = [_src("无关甲"), _src("无关乙"), _src("黄芪补气")]
    # K=5 覆盖第 3 位 → 命中；MRR=1/3
    assert _rank_metrics(["黄芪"], "", sources, 5) == (1, round(1 / 3, 4), 3)
    # K=2 不含第 3 位 → Hit@K=0，但 MRR 仍按首命中排名计 1/3
    assert _rank_metrics(["黄芪"], "", sources, 2) == (0, round(1 / 3, 4), 3)


def test_rank_metrics_no_hit_and_no_signal():
    assert _rank_metrics(["黄芪"], "", [_src("人参"), _src("枸杞")], 5) == (0, 0.0, 0)
    # 无期望信号 → 全 0（即便来源看似相关）
    assert _rank_metrics([], "", [_src("黄芪")], 5) == (0, 0.0, 0)
    assert _rank_metrics(["黄芪"], "", [], 5) == (0, 0.0, 0)


def test_rank_metrics_k_normalization_and_source_signal():
    sources = [_src("x")] * 4 + [_src("黄芪补气")]
    # k=0 归一化为默认 5 → 第 5 位命中
    assert _rank_metrics(["黄芪"], "", sources, 0) == (1, 0.2, 5)
    # 期望来源驱动排名（标题互含）
    titled = [{"document_title": "无关"}, {"document_title": "黄芪笔记.pdf"}]
    assert _rank_metrics([], "黄芪笔记", titled, 5) == (1, 0.5, 2)


# ============================================
# R3 与 retrieval_hit 口径一致
# ============================================
def test_rank_metrics_consistent_with_retrieval_hit():
    sources = [_src("无关"), _src("黄芪补气固表")]
    ctx = " ".join(s["text"] for s in sources)
    hit = _compute_retrieval_hit(["黄芪"], "", ctx, sources)
    hit_at_k, mrr, first = _rank_metrics(["黄芪"], "", sources, 5)
    assert hit == 1 and hit_at_k == 1 and first == 2
    # 无信号两者皆 0
    assert _compute_retrieval_hit([], "", ctx, sources) == 0
    assert _rank_metrics([], "", sources, 5) == (0, 0.0, 0)


# ============================================
# R4 summary 口径
# ============================================
def _row(status="ok", hit=1, hit_k=1, mrr=1.0, latency=1000):
    return {
        "status": status, "retrieval_hit": hit, "hit_at_k": hit_k, "mrr": mrr,
        "faithfulness_pass": 1, "faithfulness": None, "answer_relevance": None,
        "context_precision": None, "context_recall": None,
        "judge_score": None, "latency_ms": latency,
    }


def test_summary_rank_metrics():
    results = [_row(hit_k=1, mrr=1.0), _row(hit_k=0, mrr=0.5), _row(hit_k=1, mrr=0.3333)]
    s = _compute_summary(results)
    assert s["avg_hit_at_k"] == round(2 / 3, 4)
    assert s["avg_mrr"] == round((1.0 + 0.5 + 0.3333) / 3, 4)


def test_summary_rank_metrics_error_excluded_and_none_ignored():
    results = [_row(hit_k=1, mrr=1.0), _row(status="error", hit_k=0, mrr=0.0),
               _row(hit_k=0, mrr=None)]
    s = _compute_summary(results)
    assert s["ok"] == 2 and s["error"] == 1
    assert s["avg_hit_at_k"] == 0.5             # 错误用例不入分母
    assert s["avg_mrr"] == 1.0                  # None 不计均值


def test_summary_rank_metrics_all_error():
    s = _compute_summary([_row(status="error"), _row(status="error")])
    assert s["avg_hit_at_k"] == 0.0
    assert s["avg_mrr"] is None


# ============================================
# R5 hit_k 配置
# ============================================
def _evaluator(eid, hit_k):
    if hit_k is None:
        db.execute("INSERT INTO eval_evaluators (id, name, dataset_id, created_at)"
                   " VALUES (?,?,?,?)", (eid, "评估器", "ds", datetime.now().isoformat()))
    else:
        db.execute("INSERT INTO eval_evaluators (id, name, dataset_id, hit_k, created_at)"
                   " VALUES (?,?,?,?,?)", (eid, "评估器", "ds", hit_k, datetime.now().isoformat()))


def test_evaluator_hit_k_default_and_custom(mem_db, monkeypatch):
    monkeypatch.setattr(settings, "RETRIEVAL_HIT_K", 5, raising=False)
    assert evaluator_hit_k("") == 5             # 空 id → 默认
    assert evaluator_hit_k("不存在") == 5        # 无记录 → 默认
    _evaluator("e3", 3)
    assert evaluator_hit_k("e3") == 3
    _evaluator("eNULL", None)
    assert evaluator_hit_k("eNULL") == 5         # 列缺省 → 默认
    _evaluator("e0", 0)
    assert evaluator_hit_k("e0") == 5            # 非正 → 默认


def test_evaluator_hit_k_respects_settings_default(mem_db, monkeypatch):
    monkeypatch.setattr(settings, "RETRIEVAL_HIT_K", 7, raising=False)
    assert evaluator_hit_k("") == 7
    _evaluator("e2", 2)
    assert evaluator_hit_k("e2") == 2            # 显式配置优先于默认