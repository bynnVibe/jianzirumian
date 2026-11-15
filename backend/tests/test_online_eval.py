"""在线采样评测回归测试（内存 DB / mock LLM，不出网）。

覆盖计划第五节在线评测：
  O1 上下文提取 _source_texts（chunks/截断/上限/坏 JSON）；
  O2 候选选取与问题回溯（游标过滤、排除空回答）；
  O3 run_round：采样率/单轮上限/游标推进/开关关闭/LLM 失败静默；
  O4 有检索上下文跑 faithfulness+GEval，无上下文仅 GEval；
  O5 aggregate 聚合口径（样本/均分/低分数）；
  O6 list_results 返回前脱敏，原始落库不改写。
"""
import asyncio
import json
import sqlite3
from datetime import datetime

import pytest

from app.core import db
from app.services import online_eval as oe


@pytest.fixture()
def mem_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    yield conn
    conn.close()


@pytest.fixture()
def rc_store(monkeypatch):
    """runtime_config 单例替换为内存字典（不落盘、不读真实配置）。"""
    from app.services import config_manager
    store = {}
    monkeypatch.setattr(config_manager.runtime_config, "get",
                        lambda key, default=None: store.get(key, default))
    monkeypatch.setattr(config_manager.runtime_config, "set",
                        lambda key, value: store.__setitem__(key, value))
    return store


def _msg(i, content="答案内容", sources="[]", ts=None):
    ts = ts or datetime.now().isoformat()
    db.execute(
        "INSERT INTO chat_messages (message_id, session_id, seq, role, content, sources,"
        " created_at) VALUES (?,?,?,?,?,?,?)",
        (f"m{i}", "s1", i * 2, "assistant", content, sources, ts))
    db.execute(
        "INSERT INTO chat_messages (message_id, session_id, seq, role, content,"
        " created_at) VALUES (?,?,?,?,?,?)",
        (f"u{i}", "s1", i * 2 - 1, "user", f"问题{i}", ts))


def _patch_eval(monkeypatch, measured=None, raise_exc=False):
    """替换指标执行入口，记录调用参数。"""
    from app.services import deepeval_metrics as dm
    calls = []

    async def fake(tc, metrics, criteria="", llm_provider=None):
        calls.append({"metrics": list(metrics), "input": tc.input,
                      "contexts": tc.retrieval_context})
        if raise_exc:
            raise RuntimeError("模型不可用")
        return measured or {"faithfulness": (0.9, {}), "geval": (0.8, {"reason": "良好"})}
    monkeypatch.setattr(dm, "evaluate_case", fake)
    return calls


# ============================================
# O1 上下文提取
# ============================================
def test_source_texts_chunks_and_limits():
    sources = json.dumps([{"chunks": [{"text": "片段A"}, {"text": "  "}]},
                          {"text": "片段B"}])
    assert oe._source_texts(sources) == ["片段A", "片段B"]
    assert oe._source_texts("坏JSON") == []
    assert oe._source_texts("") == []
    long_sources = json.dumps([{"text": "字" * 2000}])
    assert len(oe._source_texts(long_sources)[0]) == 800  # 单片段截断
    many = json.dumps([{"text": f"片段{i}"} for i in range(12)])
    assert len(oe._source_texts(many)) == 8  # 上限 8 片段


# ============================================
# O2 候选选取与问题回溯
# ============================================
def test_pick_candidates_cursor_and_question(mem_db):
    _msg(1, ts="2026-01-01T10:00:00")
    _msg(2, ts="2026-01-02T10:00:00")
    db.execute("INSERT INTO chat_messages (message_id, session_id, seq, role, content,"
               " created_at) VALUES ('empty','s1',9,'assistant','','2026-01-03T10:00:00')")
    rows = oe._pick_candidates("")
    assert [r["message_id"] for r in rows] == ["m1", "m2"]  # 排除空回答、时间升序
    rows = oe._pick_candidates("2026-01-01T10:00:00")
    assert [r["message_id"] for r in rows] == ["m2"]  # 游标之后
    assert oe._question_for(rows[0]) == "问题2"


# ============================================
# O3 run_round：采样 / 上限 / 游标 / 开关 / 静默
# ============================================
def test_run_round_cap_and_cursor_advance(mem_db, rc_store, monkeypatch):
    from app.config import settings
    for i in range(1, 7):
        _msg(i)
    monkeypatch.setattr(settings, "ONLINE_EVAL_SAMPLE_RATE", 1.0)
    monkeypatch.setattr(settings, "ONLINE_EVAL_MAX_PER_ROUND", 2)
    calls = _patch_eval(monkeypatch)
    out = asyncio.run(oe.run_round())
    assert out["enabled"] is True and out["candidates"] == 6
    assert out["evaluated"] == 2 and len(calls) == 2  # 单轮上限
    assert rc_store[oe._CURSOR_KEY]  # 游标推进到本批最大 created_at
    out2 = asyncio.run(oe.run_round())
    assert out2["evaluated"] == 0 and out2.get("candidates", 0) == 0  # 不重复评测
    assert db.query_one("SELECT COUNT(*) AS n FROM online_eval_results")["n"] == 2


def test_run_round_zero_rate_still_advances_cursor(mem_db, rc_store, monkeypatch):
    from app.config import settings
    _msg(1)
    monkeypatch.setattr(settings, "ONLINE_EVAL_SAMPLE_RATE", 0.0)
    _patch_eval(monkeypatch)
    out = asyncio.run(oe.run_round())
    assert out["evaluated"] == 0 and rc_store[oe._CURSOR_KEY]


def test_run_round_disabled(mem_db, rc_store, monkeypatch):
    _msg(1)
    rc_store["online_eval_enabled"] = False
    _patch_eval(monkeypatch)
    out = asyncio.run(oe.run_round())
    assert out == {"enabled": False, "evaluated": 0}
    assert db.query_one("SELECT COUNT(*) AS n FROM online_eval_results")["n"] == 0


def test_run_round_llm_failure_silent(mem_db, rc_store, monkeypatch):
    from app.config import settings
    _msg(1)
    monkeypatch.setattr(settings, "ONLINE_EVAL_SAMPLE_RATE", 1.0)
    _patch_eval(monkeypatch, raise_exc=True)
    out = asyncio.run(oe.run_round())  # 不抛异常
    assert out["evaluated"] == 0
    assert db.query_one("SELECT COUNT(*) AS n FROM online_eval_results")["n"] == 0


# ============================================
# O4 指标选择：有上下文 vs 无上下文
# ============================================
def test_metrics_selection_by_context(mem_db, rc_store, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "ONLINE_EVAL_SAMPLE_RATE", 1.0)
    _msg(1, sources=json.dumps([{"text": "检索片段"}]))
    _msg(2, sources="[]")
    calls = _patch_eval(monkeypatch)
    asyncio.run(oe.run_round())
    by_input = {c["input"]: c for c in calls}
    assert by_input["问题1"]["metrics"] == ["faithfulness", "geval"]
    assert by_input["问题2"]["metrics"] == ["geval"]  # 无检索仅 GEval
    row = db.query_one("SELECT context_chars FROM online_eval_results WHERE question = '问题1'")
    assert row["context_chars"] == len("检索片段")


# ============================================
# O5 聚合口径
# ============================================
def test_aggregate_samples_avg_low(mem_db):
    now = datetime.now().isoformat()
    for i, (faith, geval) in enumerate([(0.9, 0.8), (0.7, 0.5), (None, 0.9)]):
        db.execute(
            "INSERT INTO online_eval_results (id, created_at, question, faithfulness,"
            " geval_score, status) VALUES (?,?,?,?,?,'ok')",
            (f"r{i}", now, f"q{i}", faith, geval))
    db.execute("INSERT INTO online_eval_results (id, created_at, status)"
               " VALUES ('skip', ?, 'skipped')", (now,))
    agg = oe.aggregate(7)
    assert agg["samples"] == 3  # 仅 ok
    assert agg["avg_faithfulness"] == 0.8  # (0.9+0.7)/2，None 不计
    assert agg["avg_geval"] == round((0.8 + 0.5 + 0.9) / 3, 4)
    assert agg["low_count"] == 1  # 0.5 < 0.6


def test_aggregate_empty_window(mem_db):
    agg = oe.aggregate(7)
    assert agg == {"samples": 0, "avg_faithfulness": None, "avg_geval": None, "low_count": 0}


# ============================================
# O6 明细脱敏（原始落库不改写）
# ============================================
def test_list_results_redacts_but_keeps_raw(mem_db):
    now = datetime.now().isoformat()
    db.execute(
        "INSERT INTO online_eval_results (id, created_at, question, answer, geval_reason,"
        " status) VALUES ('r1', ?, ?, ?, ?, 'ok')",
        (now, "联系电话13812345678是多少", "答案含手机号13812345678", "理由13812345678"))
    listing = oe.list_results()
    item = listing["items"][0]
    assert listing["total"] == 1
    for field in ("question", "answer", "geval_reason"):
        assert "13812345678" not in item[field]
        assert "[手机号已脱敏]" in item[field]
    raw = db.query_one("SELECT question FROM online_eval_results WHERE id = 'r1'")
    assert "13812345678" in raw["question"]  # 原始落库不改写