"""工具治理体系回归测试（req1-req5）

覆盖：
  - req1 标准化注册表：元数据齐全 + function calling schema 导出 + 查询函数
  - req2 Tool Routing 可观测性：record_tool_routing 落库 + tool_routing_metrics 五项指标
  - req3 调用前约束钩子链：schema/权限/风险/限流/状态闸 + fail-fast 短路
  - req4 CLI 封装：声明式规格 build_argv + 执行内核预检降级 + 高风险治理
  - req5 场景感知路由：天气实时查询主来源指向联网搜索 + 提示词不再自相矛盾
"""
import asyncio

import pytest

from app.core import db
from app.services.tools import base as tb
from app.services.tools import hooks as th
from app.services.tools import invoke as ti
from app.services.tools import router as tr
from app.services.tools import bootstrap_registry
from app.services.tools.cli_runner import CliRunner
from app.services.tools.cli_spec import DOC_TO_PDF_SPEC, CliToolSpec


# ============================================
# 共享 fixture
# ============================================

@pytest.fixture(scope="module", autouse=True)
def registry():
    """模块级引导一次注册表（register_tool 同名覆盖，幂等）。"""
    names = bootstrap_registry()
    th.reset_rate_limits()
    return names


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """把 Tool Routing 落库指向临时 SQLite，避免污染开发库。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(db, "_conn", None)
    db.get_conn()
    yield db
    if db._conn is not None:
        db._conn.close()
        monkeypatch.setattr(db, "_conn", None)


def _ctx(user_id="u1", role="user", **kw):
    return tb.ToolContext(user_id=user_id, file_path="", filename="", kind="", role=role, **kw)


# ============================================
# req1 标准化注册表
# ============================================

def test_bootstrap_registers_expected_tools(registry):
    for name in ("image_ocr", "image_upload", "document_import",
                 "knowledge_summarize", "web_search", "search_knowledge", "doc_to_pdf"):
        assert name in registry, f"工具 {name} 未注册"


def test_every_tool_has_standard_metadata(registry):
    """每个工具都补齐命名空间/标签/场景/参数 Schema/权限/风险等级/可用状态。"""
    for tool in tb.all_tools():
        assert tool.namespace, f"{tool.name} 缺命名空间"
        assert tool.status in tb.TOOL_STATUS, f"{tool.name} 状态非法"
        assert tool.risk_level in tb.RISK_LEVELS, f"{tool.name} 风险等级非法"
        assert isinstance(tool.tags, tuple)
        assert isinstance(tool.parameters, dict)


def test_function_schema_is_openai_compatible(registry):
    schema = tb.get_tool("web_search").to_function_schema()
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == "web_search"
    assert fn["parameters"]["type"] == "object"
    assert "properties" in fn["parameters"]


def test_registry_query_functions(registry):
    assert tb.get_tool("web_search") in tb.find_by_tag("realtime")
    assert tb.get_tool("web_search") in tb.find_by_scenario("天气")
    retrieval = tb.active_tools(namespaces=("retrieval",))
    assert {t.name for t in retrieval} >= {"web_search", "search_knowledge"}
    schemas = tb.function_schemas()
    assert len(schemas) == len([t for t in tb.all_tools() if t.status == "active"])
    assert tb.schema_token_estimate() > 0
    snap = tb.registry_snapshot()
    assert snap and set(snap[0].keys()) >= {
        "name", "namespace", "tags", "scenarios", "parameters",
        "permissions", "risk_level", "status",
    }


def test_high_risk_tool_needs_confirmation(registry):
    assert tb.get_tool("doc_to_pdf").needs_confirmation() is True
    assert tb.get_tool("web_search").needs_confirmation() is False


# ============================================
# req3 调用前约束钩子链
# ============================================

def test_validate_schema_required_and_type():
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}},
        "required": ["query"],
    }
    assert th.validate_schema(schema, {})[0] is False           # 缺必填
    assert th.validate_schema(schema, {"query": ""})[0] is False  # 空串视为缺失
    assert th.validate_schema(schema, {"query": "天气"})[0] is True
    assert th.validate_schema(schema, {"query": "天气", "max_results": "5"})[0] is False  # 类型错
    assert th.validate_schema(None, {"anything": 1})[0] is True  # 无 schema=无约束


def test_validate_schema_enum():
    schema = {"type": "object", "properties": {"mode": {"enum": ["a", "b"]}}, "required": ["mode"]}
    assert th.validate_schema(schema, {"mode": "a"})[0] is True
    assert th.validate_schema(schema, {"mode": "c"})[0] is False


def test_hook_chain_blocks_invalid_schema(registry):
    pre = th.run_pre_invoke_hooks(tb.get_tool("web_search"), {}, _ctx())
    assert pre.allow is False
    assert pre.code() == "schema_invalid"
    assert pre.schema_valid is False


def test_hook_chain_allows_valid_args(registry):
    pre = th.run_pre_invoke_hooks(tb.get_tool("web_search"), {"query": "深圳天气"}, _ctx())
    assert pre.allow is True


def test_permission_gate_blocks_guest(registry):
    """需要登录的工具（permissions=('authenticated',)）拒绝游客。"""
    tool = tb.get_tool("doc_to_pdf")
    pre = th.run_pre_invoke_hooks(
        tool, {"input_path": "/tmp/x.docx"}, _ctx(user_id="guest", role=""),
        opts={"confirmed": True},
    )
    assert pre.allow is False
    assert pre.code() == "permission_denied"
    assert pre.permission_ok is False


def test_risk_gate_requires_confirmation(registry):
    tool = tb.get_tool("doc_to_pdf")
    blocked = th.run_pre_invoke_hooks(tool, {"input_path": "/tmp/x.docx"}, _ctx())
    assert blocked.allow is False and blocked.code() == "risk_blocked"
    assert blocked.risk_blocked is True
    allowed = th.run_pre_invoke_hooks(
        tool, {"input_path": "/tmp/x.docx"}, _ctx(), opts={"confirmed": True}
    )
    assert allowed.allow is True


def test_rate_limit_sliding_window(registry):
    th.reset_rate_limits()
    tool = tb.get_tool("web_search")
    last = None
    for _ in range(5):
        last = th.run_pre_invoke_hooks(
            tool, {"query": "q"}, _ctx(), opts={"rate_window": 60, "rate_max_calls": 3}
        )
    assert last.allow is False and last.code() == "rate_limited"
    assert last.rate_limited is True
    th.reset_rate_limits()


def test_status_gate_blocks_disabled(registry):
    tool = tb.get_tool("web_search")
    original = tool.status
    tool.status = "disabled"
    try:
        pre = th.run_pre_invoke_hooks(tool, {"query": "q"}, _ctx())
        assert pre.allow is False and pre.code() == "status_disabled"
    finally:
        tool.status = original


# ============================================
# req1+3 统一调用入口 invoke_tool
# ============================================

def test_invoke_unknown_tool(isolated_db, registry):
    res = asyncio.run(ti.invoke_tool("no_such_tool", _ctx(), {}))
    assert res.success is False
    assert res.error == "unknown_tool"


def test_invoke_blocked_by_schema(isolated_db, registry):
    res = asyncio.run(ti.invoke_tool("web_search", _ctx(), {}))
    assert res.success is False
    assert res.error == "schema_invalid"


def test_invoke_blocked_by_risk(isolated_db, registry):
    res = asyncio.run(ti.invoke_tool("doc_to_pdf", _ctx(), {"input_path": "/tmp/x.docx"}))
    assert res.success is False
    assert res.error == "risk_blocked"


# ============================================
# req2 Tool Routing 可观测性
# ============================================

def test_record_and_metrics(isolated_db, registry):
    from app.core import observability as obs

    # 通过 invoke_tool 产生真实调用链记录（含拦截事件）
    asyncio.run(ti.invoke_tool("web_search", _ctx(), {}))            # schema_invalid → blocked
    asyncio.run(ti.invoke_tool("doc_to_pdf", _ctx(), {"input_path": "/tmp/x.docx"}))  # risk_blocked
    asyncio.run(ti.invoke_tool("no_such_tool", _ctx(), {}))          # unknown → error

    m = obs.tool_routing_metrics(days=1)
    ov = m["overview"]
    assert ov["total_calls"] == 3
    assert ov["blocked"] >= 2
    # 五项核心指标键齐全
    for key in ("param_validity_rate", "task_success_rate", "privilege_exposure_rate",
                "schema_tokens_total", "avg_latency_ms", "p95_latency_ms"):
        assert key in ov
    assert 0.0 <= ov["param_validity_rate"] <= 1.0
    assert m["by_tool"] and m["blocked_events"]


# ============================================
# req4 CLI 封装
# ============================================

def test_cli_spec_build_argv():
    argv = DOC_TO_PDF_SPEC.build_argv({"input_path": "/tmp/a.docx", "outdir": "/tmp/out"})
    assert argv[0] == "soffice"
    assert "--headless" in argv and "--convert-to" in argv and "pdf" in argv
    assert "--outdir" in argv and "/tmp/out" in argv
    assert argv[-1] == "/tmp/a.docx"  # 位置参数在末尾


def test_cli_spec_defaults_and_optional():
    spec = CliToolSpec(
        name="t", description="", binary="echo",
        option_flags={"n": "--n"}, positional=("msg",), defaults={"n": 1},
    )
    argv = spec.build_argv({"msg": "hi"})
    assert "--n" in argv and "1" in argv and "hi" in argv


def test_cli_runner_missing_binary_degrades():
    """二进制不存在时预检降级（success=False），绝不抛异常。"""
    runner = CliRunner()
    assert runner.which("definitely_not_a_real_binary_xyz") is None
    res = asyncio.run(runner.run(["definitely_not_a_real_binary_xyz", "--flag"]))
    assert res.success is False
    assert "未安装" in res.error or "不在 PATH" in res.error


def test_cli_runner_empty_argv():
    res = asyncio.run(CliRunner().run([]))
    assert res.success is False


def test_doc_to_pdf_registered_as_high_risk(registry):
    tool = tb.get_tool("doc_to_pdf")
    assert tool.namespace == "cli"
    assert tool.risk_level == "high"
    assert "cli" in tool.tags


# ============================================
# req5 场景感知路由 + 天气答非所问修复
# ============================================

def test_detect_realtime_and_health():
    assert tr.detect_realtime("查询今天深圳的天气") is True
    assert tr.detect_realtime("最新股价") is True
    assert tr.detect_realtime("红枣有什么功效") is False
    assert tr.detect_health("红枣枸杞有什么功效") is True
    assert tr.detect_health("今天天气如何") is False


def test_route_weather_points_to_web_search(registry):
    """天气实时查询：主来源必须指向 web_search，且 search_primary=True。"""
    r = tr.route_tools("查询今天深圳的天气", use_knowledge=True, use_search=True)
    assert r.primary_tool == "web_search"
    assert r.is_realtime is True
    assert r.search_primary is True
    assert r.scenario == "realtime"


def test_route_health_points_to_knowledge(registry):
    r = tr.route_tools("红枣枸杞有什么功效", use_knowledge=True, use_search=False)
    assert r.primary_tool == "search_knowledge"
    assert r.search_primary is False
    assert r.scenario == "health"


def test_route_no_tools_available(registry):
    r = tr.route_tools("你好", use_knowledge=False, use_search=False)
    assert r.primary_tool == ""


def test_augmented_query_weather_uses_search_as_primary():
    """天气场景：提示词以联网搜索结果为核心依据，不再注入「知识库未收录、请勿编造」矛盾指令。"""
    from app.services.chat import ChatService

    svc = ChatService()
    route = tr.RouteDecision(
        primary_tool="web_search", is_realtime=True, search_primary=True, scenario="realtime"
    )
    prompt = svc._build_augmented_query(
        "查询今天深圳的天气", context_text="", kb_has_content=False,
        search_context="深圳今天多云，气温 25-31℃，东南风 2 级。",
        route=route, use_knowledge=True, skip_search_prompt=False,
    )
    assert "联网搜索结果" in prompt
    assert "请基于联网搜索结果回答" in prompt
    assert "深圳今天多云" in prompt
    # 矛盾指令必须消失（旧逻辑会注入这两句导致答非所问）
    assert "知识库中暂未收录" not in prompt
    assert "不要自己编造" not in prompt
    assert "优先以知识库内容为主要回答依据" not in prompt


def test_augmented_query_health_uses_knowledge_as_primary():
    """健康场景且知识库有命中：以知识库为主要依据，搜索结果仅作补充参考。"""
    from app.services.chat import ChatService

    svc = ChatService()
    route = tr.RouteDecision(
        primary_tool="search_knowledge", is_realtime=False, search_primary=False, scenario="health"
    )
    prompt = svc._build_augmented_query(
        "红枣有什么功效", context_text="红枣具有补中益气、养血安神的功效。",
        kb_has_content=True, search_context="",
        route=route, use_knowledge=True, skip_search_prompt=False,
    )
    assert "知识库内容" in prompt
    assert "优先使用知识库内容" in prompt
    assert "补中益气" in prompt


def test_augmented_query_kb_empty_but_search_has_result():
    """知识库无命中但联网有结果（非实时）：搜索结果升级为主依据，不报「未收录」。"""
    from app.services.chat import ChatService

    svc = ChatService()
    route = tr.RouteDecision(primary_tool="web_search", search_primary=True, scenario="general")
    prompt = svc._build_augmented_query(
        "某个冷门问题", context_text="", kb_has_content=False,
        search_context="搜索结果内容 XYZ", route=route,
        use_knowledge=True, skip_search_prompt=False,
    )
    assert "搜索结果内容 XYZ" in prompt
    assert "请基于联网搜索结果回答" in prompt
    assert "知识库中暂未收录" not in prompt
