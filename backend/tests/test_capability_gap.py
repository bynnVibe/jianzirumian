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
        cg.record_gap(user_id="alice", request_id=f"request-{i}", query=f"查询今天香港的天气{i}", scenario="realtime_weather")
    out = cg.frequency_suggestions(user_id="alice")
    assert len(out["suggestions"]) == 1
    s = out["suggestions"][0]
    assert s["scenario"] == "realtime_weather"
    assert s["count"] == cg.SEDIMENT_THRESHOLD
    assert s["suggested_name"] == "auto_realtime_weather"
    assert s["suggested_kind"] == "skill"
    assert "独立请求" in s["message"] and "草稿" in s["message"]


def test_frequency_suggestions_below_threshold(isolated_db):
    for owner in ("alice", "bob"):
        for i in range(2):
            cg.record_gap(user_id=owner, request_id=str(i), query="今天天气", scenario="realtime_weather")
    out = cg.frequency_suggestions(user_id="alice")
    assert out["suggestions"] == []
    out2 = cg.frequency_suggestions(threshold=2)
    assert out2["threshold"] == 3 and out2["suggestions"] == []


# ============================================
# SSE 事件（需求5 HITL 前端协议）
# ============================================

def _requests(owner="alice", query="香港今天天气", scenario="realtime_weather", count=3, prefix="r"):
    return [cg.record_gap(user_id=owner, query=query, scenario=scenario, request_id=f"{prefix}-{i}")
            for i in range(count)]


def _suggestion(owner="alice"):
    return cg.frequency_suggestions(user_id=owner, include_cooldown=True)["suggestions"][0]["id"]


def test_governance_migration_preserves_legacy_and_is_independent():
    import sqlite3
    from app.core.skill_schema import migrate
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT)")
        conn.execute("INSERT INTO schema_migrations VALUES('skill_packages_v1','old')")
        conn.execute("CREATE TABLE capability_gaps(id TEXT,user_id TEXT,created_at TEXT)")
        conn.execute("INSERT INTO capability_gaps VALUES('legacy','alice','old')")
        migrate(conn)
        migrate(conn)
        assert conn.execute("SELECT id,request_key FROM capability_gaps").fetchall() == [("legacy", "")]
        # 四个独立迁移各登记一次：预置 skill_packages_v1 保留，治理／依赖／验收各自独立版本；二次 migrate 幂等不重复。
        versions = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        assert versions == {"skill_packages_v1", "capability_suggestions_v2",
                            "skill_tool_dependencies_v1", "capability_acceptance_v1"}
    finally:
        conn.close()


def test_frequency_deduplicates_requests_and_separates_intents(isolated_db):
    _requests(count=2)
    _requests(count=2)
    assert not cg.frequency_suggestions(user_id="alice")["suggestions"]
    _requests(count=1, prefix="new")
    _requests(query="香港明天天气", prefix="forecast")
    _requests(query="香港天气预警", prefix="alert")
    result = cg.frequency_suggestions(user_id="alice")["suggestions"]
    assert {s["count"] for s in result} == {3}
    assert {s["missing_capability"] for s in result} == {"weather_current_api", "weather_forecast_api", "weather_alert_api"}
    assert not cg.frequency_suggestions(user_id="bob")["suggestions"]


@pytest.mark.parametrize("excluded", ["failed", "resolved", "old", "legacy"])
def test_frequency_excludes_failures_resolved_old_and_legacy(isolated_db, excluded):
    gaps = _requests()
    if excluded == "failed":
        cg.record_gap(user_id="alice", query="香港今天天气", scenario="realtime_weather",
                      request_id="r-0", failure_reason="upstream_timeout")
    elif excluded == "resolved":
        db.execute("UPDATE capability_gaps SET status='resolved' WHERE id=?", (gaps[0],))
    elif excluded == "old":
        db.execute("UPDATE capability_gaps SET created_at=? WHERE id=?",
                   ((datetime.now() - timedelta(days=31)).isoformat(), gaps[0]))
    else:
        db.execute("UPDATE capability_gaps SET request_key='' WHERE id=?", (gaps[0],))
    assert not cg.frequency_suggestions(user_id="alice")["suggestions"]


@pytest.mark.parametrize("query", ["@skills 天气", "@tool 新闻", "@tools 海拔", "请添加天气工具"])
def test_addition_requests_do_not_record_gaps(isolated_db, query):
    assert cg.record_gap(user_id="alice", request_id="x", query=query, scenario="no_tool") == ""
    assert db.query("SELECT * FROM capability_gaps") == []


def test_aggregate_is_anonymous_and_counts_unique_owners(isolated_db):
    _requests(query="香港金价", scenario="realtime_general", prefix="a")
    _requests(query="北京金价", scenario="realtime_general", prefix="b")
    _requests(owner="bob", query="上海金价", scenario="realtime_general")
    assert len(cg.frequency_suggestions(user_id="alice")["suggestions"]) == 2
    aggregate = cg.frequency_suggestions()
    assert aggregate["scope"] == "aggregate"
    item = aggregate["suggestions"][0]
    assert item["owners"] == 2 and item["count"] == 9
    assert item["sample_query"] == item["intent"] == item["id"] == item["draft_id"] == ""
    assert all(value not in json.dumps(aggregate, ensure_ascii=False) for value in ("alice", "bob", "香港", "北京", "上海"))


def test_seen_and_dismiss_cooldown_and_owner_isolation(isolated_db):
    from fastapi import HTTPException
    _requests()
    key = _suggestion()
    assert cg.mark_suggestions_seen("bob", [key])["ids"] == []
    assert cg.mark_suggestions_seen("alice", [key, key])["ids"] == [key]
    assert cg.mark_suggestions_seen("alice", [key])["ids"] == []
    assert not cg.frequency_suggestions(user_id="alice")["suggestions"]
    with pytest.raises(HTTPException) as error:
        cg.dismiss_suggestion("bob", key)
    assert error.value.status_code == 404
    cg.dismiss_suggestion("alice", key)
    assert db.query_one("SELECT status FROM capability_suggestions")["status"] == "dismissed"
    db.execute("UPDATE capability_suggestions SET last_prompted_at=?", ((datetime.now() - timedelta(days=8)).isoformat(),))
    assert cg.frequency_suggestions(user_id="alice")["suggestions"]


@pytest.fixture
def suggestion_flow(isolated_db, monkeypatch):
    from app.services import chat, skill_workflow, skill_drafts
    service = chat.ChatService()
    monkeypatch.setattr(chat, "chat_service", service)
    async def build(owner, session, message, query):
        draft, error = skill_drafts.propose(owner, session, "测试能力", "测试草稿", "请引用资料并注明时间。", message_id=message)
        assert not error
        return draft
    monkeypatch.setattr(skill_workflow, "build_draft", build)
    _requests()
    return service, _suggestion(), build


def test_accept_creates_only_draft_is_idempotent_and_preserves_round(suggestion_flow):
    from app.services import skill_drafts
    service, key, _ = suggestion_flow
    cg.mark_suggestions_seen("alice", [key])
    first = asyncio.run(cg.accept_suggestion("alice", key))
    second = asyncio.run(cg.accept_suggestion("alice", key))
    assert first == second
    assert first["draft"]["status"] == "pending"
    assert db.query("SELECT * FROM user_skills WHERE owner_id='alice'") == []
    session = service.get_session(first["session_id"])
    assert first["draft"]["message_id"] == session.messages[-1].message_id
    assert skill_drafts.history(session.session_id, "alice")[0]["id"] == first["draft"]["id"]
    assert service._active_tasks == {}
    skill_drafts.confirm(first["draft"]["id"], "alice")
    assert db.query_one("SELECT status FROM capability_suggestions")["status"] != "resolved"


def test_accept_concurrency_and_other_owner_are_blocked(suggestion_flow, monkeypatch):
    from fastapi import HTTPException
    from app.services import skill_workflow
    service, key, build = suggestion_flow
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def waiting(*args):
            entered.set()
            await release.wait()
            return await build(*args)
        monkeypatch.setattr(skill_workflow, "build_draft", waiting)
        task = asyncio.create_task(cg.accept_suggestion("alice", key))
        await entered.wait()
        for owner, code in (("alice", 409), ("bob", 404)):
            with pytest.raises(HTTPException) as error:
                await cg.accept_suggestion(owner, key)
            assert error.value.status_code == code
        with pytest.raises(HTTPException) as error:
            cg.dismiss_suggestion("alice", key)
        assert error.value.status_code == 409
        release.set()
        await task
    asyncio.run(run())
    assert len(db.query("SELECT * FROM skill_drafts")) == 1 and not service._active_tasks


@pytest.mark.parametrize("delete,swallow", [(False, False), (True, False), (True, True)])
def test_accept_cancel_and_deleted_session_never_resurrect(suggestion_flow, monkeypatch, delete, swallow):
    from app.services import skill_workflow
    service, key, build = suggestion_flow
    async def run():
        entered = asyncio.Event()
        async def waiting(*args):
            entered.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                if not swallow:
                    raise
            return await build(*args)
        monkeypatch.setattr(skill_workflow, "build_draft", waiting)
        task = asyncio.create_task(cg.accept_suggestion("alice", key))
        await entered.wait()
        session_id = db.query_one("SELECT session_id FROM capability_suggestions")["session_id"]
        if delete:
            service.delete_session(session_id, user_id="alice")
        else:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert not service._active_tasks
    assert not db.query("SELECT * FROM skill_drafts WHERE status='pending'")
    assert db.query_one("SELECT status FROM capability_suggestions")["status"] == "open"
    if delete:
        assert not db.query("SELECT * FROM chat_sessions") and not db.query("SELECT * FROM chat_messages")
    else:
        assert db.query_one("SELECT interrupted FROM chat_messages WHERE role='assistant'")["interrupted"]


@pytest.mark.parametrize("stage", ["create", "discover", "response_lost"])
def test_accept_failure_releases_lease_and_recovers_saved_draft(suggestion_flow, monkeypatch, stage):
    from app.services import skill_workflow
    service, key, build = suggestion_flow
    original_create = service.create_session
    async def failing(*args):
        if stage == "response_lost":
            await build(*args)
        raise RuntimeError("isolated failure")
    if stage == "create":
        monkeypatch.setattr(service, "create_session", lambda **kw: (_ for _ in ()).throw(RuntimeError("isolated failure")))
    else:
        monkeypatch.setattr(skill_workflow, "build_draft", failing)
    with pytest.raises(RuntimeError):
        asyncio.run(cg.accept_suggestion("alice", key))
    assert db.query_one("SELECT status FROM capability_suggestions")["status"] == "open"
    monkeypatch.setattr(service, "create_session", original_create)
    monkeypatch.setattr(skill_workflow, "build_draft", build)
    result = asyncio.run(cg.accept_suggestion("alice", key))
    assert result["draft"]["status"] == "pending"
    assert len(db.query("SELECT * FROM skill_drafts")) == 1
    assert db.query_one("SELECT status FROM capability_suggestions")["status"] == "drafted"


def test_suggestion_api_restores_only_owned_unexpired_drafts(suggestion_flow):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.skills import router
    from app.api.deps import get_current_user
    _, key, _ = suggestion_flow
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"id": "alice"}
    with TestClient(app) as client:
        result = client.post(f"/api/skills/suggestions/{key}/accept").json()
        assert result["draft"]["status"] == "pending"
        assert client.get("/api/skills/suggestions").json()["drafts"][0]["id"] == result["draft"]["id"]
        app.dependency_overrides[get_current_user] = lambda: {"id": "bob"}
        assert client.get("/api/skills/suggestions").json()["drafts"] == []
        assert client.post(f"/api/skills/suggestions/{key}/accept").status_code == 404
        app.dependency_overrides[get_current_user] = lambda: {"id": "alice"}
        db.execute("UPDATE skill_drafts SET expires_at='2000-01-01'")
        assert client.get("/api/skills/suggestions").json()["drafts"] == []


def test_capability_gap_sse_event_shape():
    """能力受限降级（DeepSeek 式）：SSE 只下发限制说明与应用内建议，不含 CLI 方案。"""
    from app.services import chat_agent

    raw = chat_agent.capability_gap_event(
        "gap-1", "realtime_weather", "实时天气查询", [],
        "深圳今天天气怎么样",
    )
    assert raw.startswith("data: ") and raw.endswith("\n\n")
    payload = json.loads(raw[len("data: "):])
    assert payload["event"] == "capability_gap"
    assert payload["gap_id"] == "gap-1"
    assert payload["kind"] == "limitation"
    assert payload["query"] == "深圳今天天气怎么样"
    assert "联网搜索" in payload["message"]
    assert payload["suggestions"] == [{"id": "enable_search", "label": "开启联网搜索并重新提问"}]
    assert "solutions" not in payload and "argv" not in raw


def test_limitation_payload_with_search_enabled():
    payload = cg.limitation_payload("g", "realtime_weather", "实时天气查询", "q", use_search=True)
    assert payload["kind"] == "limitation"
    assert payload["suggestions"] == [{"id": "retry", "label": "重新提问"}]
    assert "联网搜索" in payload["message"]


def test_fill_placeholders_percent_encodes_non_ascii_in_urls():
    """中文城市名进 URL 查询串必须百分号编码（open-meteo 前置 nginx 对原始 UTF-8 返回 403）。"""
    url = "https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1"
    filled = cg._fill_placeholders(url, {"city": "深圳"})
    assert "%E6%B7%B1%E5%9C%B3" in filled and "深圳" not in filled
    # 非 URL 文本（方案标题）保留可读原文
    assert cg._fill_placeholders("查询 {city} 天气", {"city": "深圳"}) == "查询 深圳 天气"
    # 缺失占位符保留原样
    assert "{lat}" in cg._fill_placeholders("latitude={lat}", {})


def test_weather_recipe_has_server_side_geocode_chain():
    sols = cg.propose_solutions("realtime_weather", {"city": "深圳"})
    ids = [s["id"] for s in sols]
    assert "cli_geocode" in ids and "cli_open_meteo" in ids
    geocode = next(s for s in sols if s["id"] == "cli_geocode")
    assert not geocode.get("needs_argument")
    assert "%E6%B7%B1%E5%9C%B3" in " ".join(geocode["argv"])
    meteo = next(s for s in sols if s["id"] == "cli_open_meteo")
    assert meteo.get("needs_argument") and "lat" in meteo["missing_arguments"]


def test_absorb_geocode_refills_meteo_coordinates(isolated_db):
    gap_id = cg.record_gap(user_id="u1", query="深圳今天天气怎么样",
                           scenario="realtime_weather", context={"city": "深圳"})
    stdout = json.dumps({"results": [{"latitude": 22.54554, "longitude": 114.0683,
                                      "timezone": "Asia/Shanghai"}]})
    cg._absorb_geocode(gap_id, cg.get_gap(gap_id), stdout)
    gap = cg.get_gap(gap_id)
    assert gap["context"]["lat"] == "22.54554" and gap["context"]["lon"] == "114.0683"
    meteo = next(s for s in gap["proposed"] if s["id"] == "cli_open_meteo")
    assert not meteo.get("needs_argument")
    assert "22.54554" in " ".join(meteo["argv"]) and "114.0683" in " ".join(meteo["argv"])


def test_absorb_geocode_bad_output_keeps_missing_argument(isolated_db):
    gap_id = cg.record_gap(user_id="u1", query="深圳今天天气怎么样",
                           scenario="realtime_weather", context={"city": "深圳"})
    cg._absorb_geocode(gap_id, cg.get_gap(gap_id), "not json")
    gap = cg.get_gap(gap_id)
    assert "lat" not in gap["context"]
    meteo = next(s for s in gap["proposed"] if s["id"] == "cli_open_meteo")
    assert meteo.get("needs_argument")
