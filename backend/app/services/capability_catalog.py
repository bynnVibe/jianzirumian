"""
见字如面 - 能力目录与候选筛选（Capability Catalog）

对应《整改方案》§5 与图5「能力目录与候选筛选」。在现有工具注册表
（app.services.tools.base）之上扩展，不重新发明工具框架：

  - 统一目录：把注册表工具与能力配方（capability_gap.CAPABILITY_RECIPES）
    归一为「候选能力」，附带域、可用性、安全范围、执行类别等元数据；
  - 候选筛选：按「权限 ∩ 开关 ∩ 运行可用 ∩ 域匹配」过滤，"active" 仅表示注册状态，
    不等于运行可用（§5.1）；提交执行时由策略引擎/Runner 重新检查；
  - 三层结果：ExecutionResult 拆分 execution_status / validation_status /
    resolution_status（§9.3），杜绝「进程成功=问题解决」的假成功；
  - unknown_capability：模型选中不存在的能力时返回该状态并允许一次纠正，
    绝不自动安装工具（§5.2）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from app.core.requirement_contract import RequirementContract
from app.services.tools import base as tools_base

logger = logging.getLogger("jianziruyang.capability_catalog")

# 执行类别（§7）：fixed_tool 专用工具/API | approved_skill 已审核技能组合
# | cli_spec 声明式命令 | python_script 动态代码 | web_search 受控联网检索
EXECUTOR_KINDS = ("fixed_tool", "approved_skill", "cli_spec", "python_script", "web_search")

# 三层状态取值
EXECUTION_STATUS = ("completed", "failed", "timeout", "blocked", "not_run")
VALIDATION_STATUS = ("passed", "failed", "unknown", "not_run")
RESOLUTION_STATUS = ("fulfilled", "partial", "unresolved", "unknown")

# 域 → 注册表命名空间/标签的粗匹配（规模增大后再引入语义召回）
_DOMAIN_TAGS = {
    "weather": ("weather", "realtime", "external"),
    "time": ("time", "clock", "system"),
    "news": ("news", "realtime", "external"),
    "knowledge": ("retrieval", "knowledge"),
    "realtime_general": ("realtime", "external", "web_search"),
}

# 能力配方 scenario → 需求契约域（使配方能被域匹配评分命中）
_SCENARIO_DOMAIN = {
    "realtime_weather": "weather",
    "realtime_general": "realtime_general",
    "no_tool": "realtime_general",
}


def _ensure_registry() -> None:
    """工具注册表懒引导：应用启动时已 bootstrap，此处为独立调用/测试兜底。

    注册表为空（如未走 lifespan 的脚本/单测）时补一次 bootstrap_registry()，
    使能力目录不依赖调用时序即可枚举到固定工具。
    """
    if not tools_base.all_tools():
        try:
            from app.services.tools import bootstrap_registry
            bootstrap_registry()
        except Exception as e:  # 引导失败不阻断目录构建（仍可含配方候选）
            logger.warning("工具注册表引导失败（忽略）: %s", e)


@dataclass
class ExecutionResult:
    """统一 Tool/Execution 结果（§9.3）：三层状态 + 证据与来源元数据。"""

    capability_id: str = ""
    executor_kind: str = "fixed_tool"
    execution_status: str = "not_run"     # 程序/接口调用是否完成
    validation_status: str = "not_run"    # 结构/证据校验是否通过
    resolution_status: str = "unknown"    # 需求是否被满足
    data: dict = field(default_factory=dict)
    evidence_refs: List[str] = field(default_factory=list)
    source_type: str = ""
    provider_id: str = ""
    entity: str = ""
    timezone: str = ""
    units: str = ""
    observed_at: str = ""                 # 源数据观测时间；不存在则空且标记未知
    published_at: str = ""
    fetched_at: str = ""                  # 可信出口实际抓取时间，不代替源数据时间
    expires_at: str = ""
    warnings: List[str] = field(default_factory=list)
    missing_fields: List[str] = field(default_factory=list)
    error_code: str = ""
    retryable: bool = False
    usage: dict = field(default_factory=dict)
    summary: str = ""

    @property
    def is_false_success_guarded(self) -> bool:
        """防假成功（§13.3）：只有三层都达标才算 fulfilled。"""
        return (self.execution_status == "completed"
                and self.validation_status == "passed"
                and self.resolution_status == "fulfilled")

    def to_dict(self) -> dict:
        return {
            "capability_id": self.capability_id,
            "executor_kind": self.executor_kind,
            "execution_status": self.execution_status,
            "validation_status": self.validation_status,
            "resolution_status": self.resolution_status,
            "data": self.data,
            "evidence_refs": list(self.evidence_refs),
            "source_type": self.source_type,
            "provider_id": self.provider_id,
            "entity": self.entity,
            "timezone": self.timezone,
            "units": self.units,
            "observed_at": self.observed_at,
            "published_at": self.published_at,
            "fetched_at": self.fetched_at,
            "expires_at": self.expires_at,
            "warnings": list(self.warnings),
            "missing_fields": list(self.missing_fields),
            "error_code": self.error_code,
            "retryable": self.retryable,
            "usage": self.usage,
            "summary": self.summary,
        }


@dataclass
class Candidate:
    """目录中的一个候选能力（归一化元数据，§5.1）。"""

    capability_id: str
    name: str
    description: str = ""
    domain: str = ""
    executor_kind: str = "fixed_tool"
    tags: List[str] = field(default_factory=list)
    scenarios: List[str] = field(default_factory=list)
    # 可用性
    configured: bool = True
    credential_ready: bool = True
    runtime_ready: bool = True
    status: str = "active"
    # 安全范围
    risk_level: str = "low"
    effect_type: str = "read"            # read | write | external_side_effect
    approval_policy: str = "auto"        # auto | require_approval | deny
    # 执行属性
    tool_name: str = ""                  # 注册表工具名（fixed_tool 时有效）
    spec_id: str = ""                    # cli_spec 时的规格 id

    def runtime_available(self) -> bool:
        """"active" 仅表示注册状态；运行可用需综合开关/凭据/运行时（§5.1）。"""
        return (self.status == "active" and self.configured
                and self.credential_ready and self.runtime_ready)

    def to_dict(self) -> dict:
        return {
            "capability_id": self.capability_id, "name": self.name,
            "description": self.description, "domain": self.domain,
            "executor_kind": self.executor_kind, "tags": list(self.tags),
            "scenarios": list(self.scenarios), "configured": self.configured,
            "credential_ready": self.credential_ready, "runtime_ready": self.runtime_ready,
            "status": self.status, "risk_level": self.risk_level,
            "effect_type": self.effect_type, "approval_policy": self.approval_policy,
            "tool_name": self.tool_name, "spec_id": self.spec_id,
            "runtime_available": self.runtime_available(),
        }


def _domain_of_tool(tool: tools_base.BaseTool) -> str:
    tags = set(tool.tags or ())
    for domain, dtags in _DOMAIN_TAGS.items():
        if tags & set(dtags):
            return domain
    ns = tool.namespace or ""
    if ns in ("retrieval", "knowledge"):
        return "knowledge"
    if ns == "cli":
        return "realtime_general"
    return ns or "general"


def build_catalog() -> List[Candidate]:
    """从真实注册表 + 能力配方生成统一目录（不重新实现工具框架）。"""
    from app.services import capability_gap as cg

    _ensure_registry()
    candidates: List[Candidate] = []
    for t in tools_base.all_tools():
        candidates.append(Candidate(
            capability_id=f"tool:{t.name}",
            name=t.name,
            description=t.description,
            domain=_domain_of_tool(t),
            executor_kind="fixed_tool",
            tags=list(t.tags or ()),
            scenarios=list(t.scenarios or ()),
            status=t.status,
            risk_level=t.risk_level,
            effect_type="read" if t.risk_level == "low" else "write",
            approval_policy="require_approval" if t.needs_confirmation() else "auto",
            tool_name=t.name,
        ))
    # 能力配方（CLI/URL 兜底方案）→ cli_spec 候选
    for scenario, recipe in (cg.CAPABILITY_RECIPES or {}).items():
        sdomain = _SCENARIO_DOMAIN.get(scenario, scenario)
        for sol in recipe.get("solutions", []):
            risk = sol.get("risk_level", "low")
            is_external = "curl" in " ".join(sol.get("argv", []))
            tags = [scenario, sdomain, "realtime"]
            if is_external:
                tags.append("external")
            candidates.append(Candidate(
                capability_id=f"recipe:{scenario}:{sol['id']}",
                name=sol.get("label", sol["id"]),
                description=sol.get("note", ""),
                domain=sdomain,
                executor_kind="cli_spec",
                tags=tags,
                scenarios=[recipe.get("label", scenario)],
                status="active",
                risk_level=risk,
                effect_type="external_side_effect" if is_external else "read",
                approval_policy="require_approval" if risk in ("high", "critical") else "auto",
                spec_id=sol["id"],
            ))
    return candidates


def select_candidates(
    contract: RequirementContract,
    *,
    permitted: Optional[List[str]] = None,
    limit: int = 8,
) -> List[Candidate]:
    """按需求契约筛选候选能力（§5.2 选择顺序的目录侧实现）。

    过滤：无权限 / 停用 / 未配置 / 运行不可用；再按域相关性排序；
    每次只给模型少量相关能力 Schema（初始上限 8，§5.2）。
    """
    permitted_set = set(permitted if permitted is not None else contract.permitted_capabilities)
    catalog = build_catalog()
    domain_tags = set(_DOMAIN_TAGS.get(contract.domain, ()))
    scored: List[tuple[int, Candidate]] = []
    for c in catalog:
        # 运行可用性（active≠可用）
        if not c.runtime_available():
            continue
        # 外部副作用能力需本轮已授权 external
        if c.effect_type == "external_side_effect" and "external" not in permitted_set \
                and not contract.source_policy.get("allow_external"):
            continue
        # 动态执行能力需本轮已授权 dynamic_execute
        if c.executor_kind == "python_script" and "dynamic_execute" not in permitted_set:
            continue
        score = 0
        if c.domain == contract.domain:
            score += 3
        if domain_tags & set(c.tags):
            score += 2
        if c.executor_kind == "fixed_tool":
            score += 1  # 精确匹配专用工具优先于配方/动态
        if score > 0:
            scored.append((score, c))
    scored.sort(key=lambda x: (-x[0], x[1].capability_id))
    return [c for _, c in scored[:max(1, limit)]]


def resolve(capability_id: str) -> Optional[Candidate]:
    """按 capability_id 解析目录项；不存在返回 None（上层转 unknown_capability）。"""
    for c in build_catalog():
        if c.capability_id == capability_id:
            return c
    return None


def unknown_capability_result(capability_id: str) -> ExecutionResult:
    """模型选中不存在的能力：返回 unknown_capability，允许一次纠正，不自动安装（§5.2）。"""
    return ExecutionResult(
        capability_id=capability_id,
        execution_status="blocked",
        validation_status="not_run",
        resolution_status="unknown",
        error_code="unknown_capability",
        summary=f"未知能力：{capability_id}。目录中不存在该能力，不会自动安装工具。",
    )
