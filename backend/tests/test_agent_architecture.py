"""需求4：Agent 能力缺口补救与动态命令执行整改 —— 新架构回归测试

对应《整改方案》§3.1 系统不变量 / §4 需求契约与缺口 / §5 能力目录 /
§7 执行模式 / §10 沙箱隔离 / §13 防假成功。覆盖：

  - 需求契约：缺参数不猜测（weather 无城市 → missing_argument）、服务端时钟锚定；
  - 能力目录：注册表 + 配方归一、外部/动态能力按本轮授权过滤、unknown_capability；
  - Sandbox Controller：隔离不可用绝不回退主机、非白名单二进制拒绝、
    SSRF 内网/回环出口拒绝、fixed_tool 误用拒绝、预审核 date 命令经受治理 Runner 执行；
  - 编排器：缺参数缺口即中止、四级验证防假成功（无数据不判 fulfilled）。
"""
import asyncio
from datetime import datetime

import pytest

from app.core.requirement_contract import build_contract
from app.services import capability_catalog as catalog
from app.services.capability_catalog import ExecutionResult
from app.services.sandbox import ExecutionSpec, sandbox_controller
from app.services import agent_orchestrator as ao


# ============================================
# 需求契约（§4.1 / §4.2）
# ============================================

def test_contract_weather_without_city_is_missing_argument():
    """缺参数不猜测：天气问题未给出城市 → missing_argument，绝不默认某城市。"""
    c = build_contract("查询今天的天气")
    assert c.domain == "weather"
    assert "city" in c.missing_arguments
    assert c.entity_resolved is False


def test_contract_weather_with_city_resolves_entity():
    c = build_contract("查询今天梅州的天气")
    assert c.domain == "weather"
    assert c.entity_resolved is True
    assert "梅州" in c.entity
    assert c.missing_arguments == []


def test_contract_today_anchored_to_server_clock():
    """「今天」由服务端时钟锚定，不由模型自行推算。"""
    fixed = datetime(2026, 11, 11, 9, 0, 0)
    c = build_contract("今天梅州天气如何", now=fixed)
    assert c.requested_time == "today"
    assert c.date_from == "2026-11-11"
    assert c.date_to == "2026-11-11"
    assert c.server_now == fixed.isoformat()


def test_contract_time_needs_timezone_when_absent():
    c = build_contract("现在几点")
    assert c.domain == "time"
    assert "timezone" in c.missing_arguments


# ============================================
# 能力目录（§5）
# ============================================

def test_build_catalog_includes_tools_and_recipes():
    cands = catalog.build_catalog()
    ids = {c.capability_id for c in cands}
    assert any(i.startswith("tool:") for i in ids)
    assert any(i.startswith("recipe:") for i in ids)


def test_select_candidates_filters_external_without_permission():
    """未授权 external 时，外部副作用（curl）配方不进候选。"""
    c = build_contract("查询今天梅州的天气")
    cands = catalog.select_candidates(c, permitted=[])
    assert all(cc.effect_type != "external_side_effect" for cc in cands)


def test_select_candidates_allows_external_when_permitted():
    c = build_contract("查询今天梅州的天气", allow_external=True)
    cands = catalog.select_candidates(c, permitted=["external"])
    assert any(cc.effect_type == "external_side_effect" for cc in cands)


def test_unknown_capability_result_does_not_install():
    r = catalog.unknown_capability_result("tool:not_exist")
    assert r.execution_status == "blocked"
    assert r.error_code == "unknown_capability"
    assert r.resolution_status == "unknown"


# ============================================
# Sandbox Controller（§7 / §10 / §11 / §3.1 第 8 条）
# ============================================

def test_sandbox_python_script_unavailable_no_host_fallback():
    """隔离运行时未配置 → 动态代码 sandbox_unavailable，绝不回退主机子进程。"""
    spec = ExecutionSpec(executor_kind="python_script", code="print(1)")
    res = asyncio.run(sandbox_controller.submit_execution(spec))
    assert res.execution_status == "sandbox_unavailable"
    assert res.error_code == "sandbox_unavailable"
    assert res.isolated is True


def test_sandbox_cli_spec_rejects_non_allowlist_binary():
    """非预审核白名单二进制 → blocked/unapproved_binary（PATH 有≠可执行）。"""
    spec = ExecutionSpec(executor_kind="cli_spec", argv=["python3", "-c", "print(1)"],
                         spec_id="x")
    res = asyncio.run(sandbox_controller.submit_execution(spec))
    # python3 不在白名单：需隔离，而隔离不可用 → sandbox_unavailable；绝不主机执行
    assert res.execution_status in ("blocked", "sandbox_unavailable")
    assert res.stdout == ""


def test_sandbox_cli_spec_requires_spec_id():
    """白名单二进制但缺 spec_id → blocked/missing_spec_id。"""
    spec = ExecutionSpec(executor_kind="cli_spec", argv=["date", "+%Y"])
    res = asyncio.run(sandbox_controller.submit_execution(spec))
    assert res.execution_status == "blocked"
    assert res.error_code == "missing_spec_id"


def test_sandbox_egress_denies_internal_host():
    """SSRF 约束：curl 指向内网/回环 → egress_denied。"""
    spec = ExecutionSpec(executor_kind="cli_spec",
                         argv=["curl", "-s", "http://127.0.0.1:8000/admin"],
                         spec_id="cli_curl_url")
    res = asyncio.run(sandbox_controller.submit_execution(spec))
    assert res.execution_status == "blocked"
    assert res.error_code == "egress_denied"


def test_sandbox_fixed_tool_misuse_rejected():
    """fixed_tool 应经统一调用入口，不通过 Controller → blocked/wrong_executor。"""
    spec = ExecutionSpec(executor_kind="fixed_tool", argv=["date"])
    res = asyncio.run(sandbox_controller.submit_execution(spec))
    assert res.execution_status == "blocked"
    assert res.error_code == "wrong_executor"


def test_sandbox_allowlisted_date_runs_via_governed_runner():
    """预审核 date 命令 + spec_id → 经受治理 fixed_adapter Runner 真实执行。"""
    spec = ExecutionSpec(executor_kind="cli_spec", argv=["date", "+%Y-%m-%d"],
                         spec_id="cli_today_date", timeout=10.0)
    res = asyncio.run(sandbox_controller.submit_execution(spec))
    assert res.execution_status == "completed"
    assert res.exit_code == 0
    assert datetime.now().strftime("%Y") in res.stdout
    assert res.runtime_profile_id == "fixed_adapter"


def test_sandbox_profiles_isolated_unavailable_by_default():
    profiles = {p["id"]: p for p in sandbox_controller.list_profiles()}
    assert profiles["fixed_adapter"]["available"] is True
    assert profiles["isolated"]["available"] is False


# ============================================
# 编排器（§6 / §13 防假成功）
# ============================================

def test_orchestrator_blocks_on_missing_argument():
    """缺参数缺口：编排器不猜测，直接 blocked/unresolved。"""
    out = asyncio.run(ao.orchestrator.run("查询今天的天气", user={"id": "u1", "role": "user"}))
    assert out["resolution_status"] == "unresolved"
    assert out["gap_type"] == "missing_argument"
    assert out["result"] is None


def test_validate_false_success_guard_no_data():
    """四级验证防假成功：执行完成但无结构化数据 → 不判 fulfilled。"""
    c = build_contract("查询今天梅州的天气")
    r = ExecutionResult(capability_id="tool:x", execution_status="completed", data={})
    validated = ao._validate(r, c)
    assert validated.validation_status == "failed"
    assert validated.resolution_status == "unresolved"
    assert validated.is_false_success_guarded is False


def test_validate_entity_mismatch_not_fulfilled():
    """证据级：需求梅州但证据深圳 → unresolved（防张冠李戴）。"""
    c = build_contract("查询今天梅州的天气")
    r = ExecutionResult(capability_id="tool:x", execution_status="completed",
                        data={"当前温度": "20"}, entity="深圳")
    validated = ao._validate(r, c)
    assert validated.resolution_status == "unresolved"


def test_validate_fulfilled_when_data_and_fields_present():
    c = build_contract("查询今天梅州的天气")
    c.required_fields = ["当前温度"]
    r = ExecutionResult(capability_id="tool:x", execution_status="completed",
                        data={"当前温度": "20"}, entity="梅州")
    validated = ao._validate(r, c)
    assert validated.resolution_status == "fulfilled"
    assert validated.is_false_success_guarded is True
