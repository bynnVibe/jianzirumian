"""可观测性重构 + 能力缺口治理回归测试（本轮需求1-6）

覆盖：
  - 需求1 today 时间范围：_range_since/_range_days + metrics(range_mode='today') + golden_signals
  - 需求2/3 统一会话可观测：session_observability 的 traces 按天筛选 / 段构成 / 调用链 / 记忆
  - 需求5 能力缺口：场景检测 / 上下文抽取 / 方案生成 / 留痕 / 沙箱执行 / 高风险确认
  - 需求6 频次沉淀：同场景达阈值 → 「沉淀为 skill/工具」建议
"""
import asyncio
import json
from datetime import datetime, timedelta

import pytest

from app.core import db
from app.core import observability as obs
from app.services import capability_gap as cg


# ============================================
# 共享 fixture
# ============================================

@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """把落库指向临时 SQLite，避免污染开发库。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(db, "_conn", None)
    db.get_conn()
    yield db
    if db._conn is not None:
        db._conn.close()
        monkeypatch.setattr(db, "_conn", None)


def _seed_session(db_mod, session_id="s1", user_id="u1", title="测试对话"):
    now = datetime.now().isoformat()
    db_mod.execute(
        "INSERT INTO chat_sessions (session_id, user_id, title, created_at, updated_at)"
        " VALUES (?,?,?,?,?)",
        (session_id, user_id, title, now, now),
    )


def _seed_trace(db_mod, trace_id, session_id, *, day_offset=0, status="ok",
                prompt_tokens=100, input_text="问题"):
    started = (datetime.now() - timedelta(days=day_offset)).replace(
        hour=10, minute=0, second=0, microsecond=0
    ).isoformat()
    db_mod.execute(
        "INSERT INTO agent_traces (id, agent, user_id, session_id, input, status,"
        " llm_calls, tool_calls, prompt_tokens, completion_tokens, latency_ms,"
        " started_at, ended_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trace_id, "chat", "u1", session_id, input_text, status,
         1, 0, prompt_tokens, 50, 800, started, started),
    )


def _seed_segment(db_mod, seg_id, trace_id, segment, tokens):
    db_mod.execute(
        "INSERT INTO agent_token_segments (id, trace_id, agent, segment, tokens, chars,"
        " created_at) VALUES (?,?,?,?,?,?,?)",
        (seg_id, trace_id, "chat", segment, tokens, tokens * 2,
         datetime.now().isoformat()),
    )


# ============================================
# 需求1：today 时间范围 + 需求4：黄金信号
# ============================================

def test_range_since_today_returns_midnight():
    since = obs._range_since(7, "today")
    today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    assert since == today0.isoformat()


def test_range_since_days_uses_window():
    since = obs._range_since(7, "days")
    # _since 语义：含今天在内共 7 天 → 起点 = now-6d 的 00:00
    expect = (datetime.now() - timedelta(days=6)).date()
    got = datetime.fromisoformat(since)
    assert got.date() == expect, "days 模式应为含今天共 N 天的起点"
    assert got.hour == 0 and got.minute == 0


def test_range_days_today_is_one():
    assert obs._range_days(30, "today") == 1
    assert obs._range_days(30, "days") == 30


def test_metrics_today_mode_and_golden_signals(isolated_db):
    _seed_trace(isolated_db, "t1", "s1", status="ok", prompt_tokens=200)
    m = obs.metrics(days=7, range_mode="today")
    assert m["range"] == "today"
    golden = m["golden_signals"]
    # 今日 1 条请求 → rate_per_day=1，error_rate=0
    assert golden["rate_per_day"] == 1.0
    assert golden["error_rate"] == 0.0
    assert golden["duration"]["avg_ms"] >= 0
    # 饱和度口径 = prompt+completion 总量 / 请求数（seed: 200+50）
    assert golden["saturation"]["avg_tokens_per_request"] == 250
    assert "overuse" in golden["saturation"]


def test_metrics_today_excludes_old_records(isolated_db):
    _seed_trace(isolated_db, "t-old", "s1", day_offset=3, status="ok")
    m = obs.metrics(days=7, range_mode="today")
    assert m["overview"]["total"] == 0
    m7 = obs.metrics(days=7, range_mode="days")
    assert m7["overview"]["total"] == 1


# ============================================
# 需求2/3：统一会话可观测视图
# ============================================

def test_session_observability_unifies_context_traces_spans(isolated_db):
    _seed_session(isolated_db)
    _seed_trace(isolated_db, "t1", "s1", prompt_tokens=300)
    _seed_segment(isolated_db, "g1", "t1", "knowledge", 200)
    _seed_segment(isolated_db, "g2", "t1", "memory", 60)
    view = obs.session_observability("s1")
    assert view["title"] == "测试对话"
    assert [t["trace_id"] for t in view["traces"]] == ["t1"]
    assert view["selected_trace_id"] == "t1"
    # 上下文段构成（与聊天页缩略图同口径）
    segs = {s["segment"]: s for s in view["segments"]}
    assert segs["knowledge"]["tokens"] == 200
    assert segs["memory"]["tokens"] == 60
    assert abs(sum(s["share"] for s in view["segments"]) - 1.0) < 0.01
    # 会话级记忆累计（按对话查看记忆）
    assert view["memory"]["total_tokens"] == 60


def test_session_observability_day_filter_and_trace_select(isolated_db):
    _seed_session(isolated_db)
    _seed_trace(isolated_db, "t-today", "s1", day_offset=0, prompt_tokens=100)
    _seed_trace(isolated_db, "t-old", "s1", day_offset=2, prompt_tokens=200)
    today = datetime.now().date().isoformat()
    view = obs.session_observability("s1", day=today)
    assert [t["trace_id"] for t in view["traces"]] == ["t-today"]
    assert len(view["days"]) == 1
    # 全量 + 指定 trace
    full = obs.session_observability("s1")
    assert len(full["traces"]) == 2
    assert full["selected_trace_id"] == "t-today"  # 默认最近一次
    picked = obs.session_observability("s1", trace_id="t-old")
    assert picked["selected_trace_id"] == "t-old"
    assert picked["total_tokens"] == 200


def test_session_observability_empty_session(isolated_db):
    view = obs.session_observability("not-exist")
    assert view["traces"] == [] and view["segments"] == [] and view["spans"] == []


# ============================================
# 需求5：能力缺口检测 / 方案 / 沙箱执行 / 风险确认
# ============================================

def test_detect_scenario_realtime_weather():
    assert cg.detect_scenario(
        "查询今天香港的天气", is_realtime=True, has_suitable_tool=True,
        search_sufficient=False,
    ) == "realtime_weather"


def test_detect_scenario_realtime_general():
    assert cg.detect_scenario(
        "现在黄金价格多少", is_realtime=True, has_suitable_tool=True,
        search_sufficient=False,
    ) == "realtime_general"


def test_detect_scenario_no_tool_requires_external_signal():
    # 带实时/外部信号 + 无工具 + 无任何内容 → no_tool
    assert cg.detect_scenario(
        "帮我查最新汇率", is_realtime=False, has_suitable_tool=False,
        search_sufficient=False,
    ) == "no_tool"
    # 纯知识库问题（无外部信号）不误报
    assert cg.detect_scenario(
        "晚上吃什么养生", is_realtime=False, has_suitable_tool=False,
        search_sufficient=False,
    ) is None


def test_detect_scenario_no_gap_when_content_available():
    # 搜索充分 / KB 有内容 → 无缺口
    assert cg.detect_scenario(
        "查询今天香港的天气", is_realtime=True, has_suitable_tool=True,
        search_sufficient=True,
    ) is None
    assert cg.detect_scenario(
        "查询今天香港的天气", is_realtime=True, has_suitable_tool=True,
        search_sufficient=False, kb_has_content=True,
    ) is None


def test_extract_context_city_and_url():
    ctx = cg.extract_context("查询今天香港的天气")
    assert ctx.get("city") == "香港"
    ctx2 = cg.extract_context("访问 https://example.com/api 获取数据")
    assert ctx2.get("url") == "https://example.com/api"


def test_propose_solutions_fills_placeholders():
    sols = cg.propose_solutions("realtime_weather", {"city": "香港"})
    assert sols, "天气场景应有候选方案"
    wttr = next(s for s in sols if s["id"] == "cli_wttr")
    assert any("香港" in part for part in wttr["argv"])
    assert all("{city}" not in part for part in wttr["argv"])


def test_record_and_get_gap(isolated_db):
    gap_id = cg.record_gap(session_id="s1", user_id="u1", trace_id="t1",
                           query="查询今天香港的天气", scenario="realtime_weather",
                           context={"city": "香港"})
    assert gap_id
    gap = cg.get_gap(gap_id)
    assert gap["scenario"] == "realtime_weather"
    assert gap["status"] == "pending"
    assert gap["context"]["city"] == "香港"
    assert any(s["id"] == "cli_wttr" for s in gap["proposed"])


def test_execute_solution_low_risk_runs_in_sandbox(isolated_db):
    """低风险方案（date 命令）直接沙箱执行，离线可验证。"""
    gap_id = cg.record_gap(query="今天几号", scenario="realtime_general")
    result = asyncio.run(cg.execute_solution(gap_id, "cli_today_date"))
    assert result["success"] is True
    assert result["sandbox"] is True
    assert result["needs_confirmation"] is False
    assert datetime.now().strftime("%Y") in result["stdout"]
    # 执行回写：chosen/status/executions
    gap = cg.get_gap(gap_id)
    assert gap["chosen"] == "cli_today_date"
    assert gap["status"] == "executed"
    assert gap["executions"] == 1


def test_execute_solution_high_risk_needs_confirmation(isolated_db, monkeypatch):
    """high/critical 风险方案未确认时暂停（HITL），确认后才在沙箱执行。"""
    gap_id = cg.record_gap(query="测试", scenario="no_tool")
    # 把 curl 方案风险提升为 high 以验证确认闸门
    row = db.query_one("SELECT proposed_json FROM capability_gaps WHERE id=?", (gap_id,))
    sols = json.loads(row["proposed_json"])
    sols[0]["risk_level"] = "high"
    db.execute("UPDATE capability_gaps SET proposed_json=? WHERE id=?",
               (json.dumps(sols, ensure_ascii=False), gap_id))

    blocked = asyncio.run(cg.execute_solution(gap_id, "cli_curl_url"))
    assert blocked["success"] is False
    assert blocked["needs_confirmation"] is True
    assert blocked["risk_level"] == "high"

    calls = []

    async def fake_run(argv, timeout=None, **kw):
        calls.append(argv)

        class R:
            success = True
            stdout = "ok"
            stderr = ""
            exit_code = 0
            timed_out = False
            duration_ms = 1
            error = ""
        return R()

    monkeypatch.setattr(cg.default_runner, "run", fake_run)
    # 经可信 Sandbox Controller：联网 host 必须在审核白名单内（否则 egress_denied）
    confirmed = asyncio.run(cg.execute_solution(gap_id, "cli_curl_url",
                                                confirmed=True, url="https://wttr.in/test"))
    assert confirmed["success"] is True
    assert calls and "https://wttr.in/test" in calls[0]


def test_execute_solution_missing_gap(isolated_db):
    result = asyncio.run(cg.execute_solution("not-exist", "cli_wttr"))
    assert result["success"] is False
    assert "不存在" in result["error"]


# ============================================
# 需求6：频次沉淀建议
# ============================================

def test_frequency_suggestions_reaches_threshold(isolated_db):
    for i in range(cg.SEDIMENT_THRESHOLD):
        cg.record_gap(query=f"查询今天香港的天气{i}", scenario="realtime_weather")
    out = cg.frequency_suggestions()
    assert len(out["suggestions"]) == 1
    s = out["suggestions"][0]
    assert s["scenario"] == "realtime_weather"
    assert s["count"] == cg.SEDIMENT_THRESHOLD
    assert s["suggested_name"] == "auto_realtime_weather"
    assert s["suggested_kind"] == "skill"
    assert "沉淀" in s["message"]


def test_frequency_suggestions_below_threshold(isolated_db):
    cg.record_gap(query="今天天气", scenario="realtime_weather")
    cg.record_gap(query="今天天气2", scenario="realtime_weather")
    out = cg.frequency_suggestions()
    assert out["suggestions"] == []
    # 降低阈值即可命中
    out2 = cg.frequency_suggestions(threshold=2)
    assert len(out2["suggestions"]) == 1


# ============================================
# SSE 事件（需求5 HITL 前端协议）
# ============================================

def test_capability_gap_sse_event_shape():
    from app.services import chat_agent

    raw = chat_agent.capability_gap_event(
        "gap-1", "realtime_weather", "实时天气查询",
        [{"id": "cli_wttr", "label": "x", "kind": "cli", "argv": ["curl"],
          "risk_level": "low", "note": ""}],
        "查询今天香港的天气",
    )
    assert raw.startswith("data: ") and raw.endswith("\n\n")
    payload = json.loads(raw[len("data: "):])
    assert payload["event"] == "capability_gap"
    assert payload["gap_id"] == "gap-1"
    assert payload["solutions"][0]["id"] == "cli_wttr"
