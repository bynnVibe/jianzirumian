"""
见字如面 - Agent 编排器（图5 主干）

对应《整改方案》§6 目标组件与图5：

    请求上下文与需求分析 → Agent 编排器
      ├─ 能力目录与候选筛选
      ├─ 缺口识别与执行规划
      ├─ 统一工具调用入口 ─ 策略引擎 / 审批记录 / 预算账本
      │     ├─ 固定工具适配器
      │     └─ 可信 Sandbox Controller → 一次性执行环境 → 网络出口 Broker
      └─ 证据验证 → 回答生成

编排器只负责「决定下一项信息需求、选择能力、有限重规划」，不直接执行系统进程、
不批准权限（§6）。执行统一收敛到：
  - 固定工具 → app.services.tools.invoke.invoke_tool（调用前钩子=策略/权限/风险/限流+可观测性）；
  - 声明式命令/动态代码 → SandboxController（隔离不可用则拒绝，不回退主机）。

预算账本（§3.1 第 7 条）：所有尝试共享总时间/动作次数/HTTP 请求预算，不能拆步骤绕过。
证据验证（§13）：执行级→结构级→证据级→需求级；判定异常保留 unknown/failed，绝不假成功。
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional

from app.core.requirement_contract import RequirementContract, build_contract
from app.services import capability_catalog as catalog
from app.services.capability_catalog import Candidate, ExecutionResult
from app.services.sandbox import ExecutionSpec, sandbox_controller
from app.services.tools import base as tools_base
from app.services.tools.invoke import invoke_tool

logger = logging.getLogger("jianziruyang.orchestrator")

# §15.3 SSE 事件名
EVENTS = (
    "capability_gap_detected", "capability_plan_ready", "approval_required",
    "execution_queued", "execution_started", "execution_progress",
    "execution_finished", "evidence_validated", "capability_resolved",
    "capability_blocked", "capability_completed",
)


@dataclass
class OrchestratorEvent:
    event: str
    run_id: str
    seq: int = 0
    execution_id: str = ""
    at: str = ""
    status: str = ""
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"event": self.event, "run_id": self.run_id, "event_seq": self.seq,
                "execution_id": self.execution_id, "at": self.at,
                "status": self.status, **self.payload}


@dataclass
class BudgetLedger:
    """预算账本：跨所有尝试共享，超限即中止，不自动提额（§3.1 第 7 条 / §10.3）。"""

    total_seconds: float = 120.0
    max_actions: int = 6
    max_http_requests: int = 8
    _deadline: float = field(default=0.0, init=False)
    actions_used: int = 0
    http_used: int = 0

    def start(self):
        self._deadline = time.monotonic() + self.total_seconds

    def remaining(self) -> float:
        return max(0.0, self._deadline - time.monotonic()) if self._deadline else self.total_seconds

    def exhausted(self) -> Optional[str]:
        if self._deadline and time.monotonic() >= self._deadline:
            return "total_time_budget"
        if self.actions_used >= self.max_actions:
            return "action_budget"
        if self.http_used >= self.max_http_requests:
            return "http_budget"
        return None

    def spend_action(self, http: bool = False) -> Optional[str]:
        """消费一次动作预算；返回超限原因或 None。"""
        over = self.exhausted()
        if over:
            return over
        self.actions_used += 1
        if http:
            self.http_used += 1
        return None


@dataclass
class PolicyDecision:
    decision: str                         # allow | require_approval | deny
    reason: str = ""
    approval_policy: str = "auto"


class PolicyEngine:
    """策略引擎：用户权限/作用域/输入/网络/预算/审批校验；不依赖模型自报 low/safe（§6）。"""

    def evaluate(self, cand: Candidate, contract: RequirementContract,
                 permitted: set, *, approved: bool = False) -> PolicyDecision:
        # 系统禁止事项优先：外部副作用未授权 → deny（不因用户确认而扩大目录，§12.1）
        if cand.effect_type == "external_side_effect" \
                and "external" not in permitted \
                and not contract.source_policy.get("allow_external"):
            return PolicyDecision("deny", "本轮未授权外部查询", cand.approval_policy)
        if cand.executor_kind == "python_script" and "dynamic_execute" not in permitted:
            return PolicyDecision("deny", "本轮未授权动态执行", cand.approval_policy)
        # 审批矩阵（§12.2）：require_approval 且未获批 → 等待确认
        if cand.approval_policy == "require_approval" and not approved:
            return PolicyDecision("require_approval",
                                  f"能力「{cand.name}」风险等级 {cand.risk_level}，需针对冻结合同确认",
                                  cand.approval_policy)
        if cand.approval_policy == "deny":
            return PolicyDecision("deny", "系统策略禁止该操作", cand.approval_policy)
        return PolicyDecision("allow", "", cand.approval_policy)


def _validate(result: ExecutionResult, contract: RequirementContract) -> ExecutionResult:
    """四级验证的结构级/证据级/需求级落地（§13.1）；判定异常保留 unknown，不假成功。"""
    # 执行级未通过 → 直接返回，不进入证据/需求级
    if result.execution_status != "completed":
        result.validation_status = "not_run"
        result.resolution_status = "unknown" if result.execution_status in ("timeout", "blocked") else "unresolved"
        return result
    # 结构级：无业务数据视为结构失败
    if not result.data:
        result.validation_status = "failed"
        result.resolution_status = "unresolved"
        result.warnings.append("执行完成但无结构化业务数据，不判定为满足需求")
        return result
    result.validation_status = "passed"
    # 需求级：必需字段覆盖检查
    missing = [f for f in contract.required_fields if f and f not in result.data]
    if missing:
        result.missing_fields.extend(missing)
    # 证据级：实体/时间口径（天气类必须匹配实体，防止梅州问题返回深圳）
    if contract.domain == "weather" and contract.entity and result.entity \
            and contract.entity not in result.entity and result.entity not in contract.entity:
        result.warnings.append(f"实体口径不匹配：需求「{contract.entity}」，证据「{result.entity}」")
        result.resolution_status = "unresolved"
        return result
    if result.missing_fields:
        result.resolution_status = "partial"
    else:
        result.resolution_status = "fulfilled"
    return result


class AgentOrchestrator:
    """图5 主干编排器。"""

    def __init__(self, policy: Optional[PolicyEngine] = None):
        self.policy = policy or PolicyEngine()

    async def run(
        self,
        query: str,
        *,
        user: dict,
        permitted_capabilities: Optional[List[str]] = None,
        allow_external: bool = False,
        timezone: str = "",
        approved_capability: str = "",
        emit: Optional[Callable[[OrchestratorEvent], None]] = None,
    ) -> dict:
        """执行一轮编排，返回 {run_id, contract, resolution_status, result, events, ...}。"""
        run_id = str(uuid.uuid4())
        events: List[OrchestratorEvent] = []
        seq = 0

        def _emit(name: str, **payload):
            nonlocal seq
            seq += 1
            ev = OrchestratorEvent(event=name, run_id=run_id, seq=seq,
                                   at=datetime.now().isoformat(), payload=payload)
            events.append(ev)
            if emit:
                try:
                    emit(ev)
                except Exception as e:  # 可观测性/回调失败不阻断编排
                    logger.warning("事件回调失败（忽略）: %s", e)

        user_id = user.get("id", "") if isinstance(user, dict) else str(user)
        role = user.get("role", "") if isinstance(user, dict) else ""

        # 1) 请求上下文与需求分析
        contract = build_contract(query, timezone=timezone,
                                  permitted_capabilities=permitted_capabilities or [],
                                  allow_external=allow_external)

        # 2) 授权求交：系统允许 ∩ 用户有权 ∩ 本轮已授权（§12.1）
        permitted = set(contract.permitted_capabilities)
        if allow_external:
            permitted.add("external")
        if role in ("admin", "ops"):
            permitted.add("dynamic_execute")

        # 3) 缺口识别：缺参数不猜测（§4.2 missing_argument）
        if contract.missing_arguments:
            _emit("capability_gap_detected", gap_type="missing_argument",
                  missing=contract.missing_arguments, domain=contract.domain,
                  hint=f"缺少必要参数：{'、'.join(contract.missing_arguments)}，请补充后重试")
            _emit("capability_completed", status="blocked", resolution_status="unresolved",
                  reason="missing_argument")
            return {"run_id": run_id, "contract": contract.to_dict(),
                    "resolution_status": "unresolved", "result": None,
                    "gap_type": "missing_argument", "events": [e.to_dict() for e in events]}

        # 4) 能力目录与候选筛选
        candidates = catalog.select_candidates(contract, permitted=sorted(permitted))
        if not candidates:
            _emit("capability_gap_detected", gap_type="no_matching_tool", domain=contract.domain,
                  hint="能力目录中无匹配的可用能力")
            _emit("capability_completed", status="blocked", resolution_status="unresolved",
                  reason="no_matching_tool")
            return {"run_id": run_id, "contract": contract.to_dict(),
                    "resolution_status": "unresolved", "result": None,
                    "gap_type": "no_matching_tool", "events": [e.to_dict() for e in events]}

        # 5) 执行规划
        plan = [{"capability_id": c.capability_id, "executor_kind": c.executor_kind,
                 "approval_policy": c.approval_policy} for c in candidates]
        _emit("capability_plan_ready", plan=plan, chosen=candidates[0].capability_id)

        # 6) 预算账本
        b = contract.budgets
        ledger = BudgetLedger(
            total_seconds=float(b.get("total_seconds", 120)),
            max_actions=int(b.get("max_actions", 6)),
            max_http_requests=int(b.get("max_http_requests", 8)),
        )
        ledger.start()

        last_result: Optional[ExecutionResult] = None
        for cand in candidates:
            over = ledger.exhausted()
            if over:
                _emit("capability_blocked", reason=f"budget_exhausted:{over}")
                break
            # 7) 策略引擎
            decision = self.policy.evaluate(cand, contract, permitted,
                                            approved=(cand.capability_id == approved_capability))
            if decision.decision == "deny":
                _emit("capability_blocked", capability_id=cand.capability_id,
                      reason=decision.reason)
                continue
            if decision.decision == "require_approval":
                _emit("approval_required", capability_id=cand.capability_id,
                      approval_policy=cand.approval_policy, reason=decision.reason,
                      plan_hash=cand.capability_id)
                # 等待针对具体合同的确认；不保留运行中的沙箱（§8.1 第 8 步）
                continue

            # 8) 统一执行入口
            over = ledger.spend_action(http=(cand.effect_type == "external_side_effect"))
            if over:
                _emit("capability_blocked", reason=f"budget_exhausted:{over}")
                break
            _emit("execution_started", capability_id=cand.capability_id,
                  executor_kind=cand.executor_kind)
            result = await self._execute(cand, contract, user_id, ledger)
            _emit("execution_finished", capability_id=cand.capability_id,
                  execution_status=result.execution_status, execution_id=result.capability_id)

            # 9) 证据验证（四级）
            result = _validate(result, contract)
            _emit("evidence_validated", capability_id=cand.capability_id,
                  validation_status=result.validation_status,
                  resolution_status=result.resolution_status,
                  missing_fields=result.missing_fields, warnings=result.warnings)
            last_result = result
            if result.resolution_status == "fulfilled":
                _emit("capability_resolved", capability_id=cand.capability_id)
                break
            # partial/unresolved → 有限重规划：尝试下一个候选

        resolution = last_result.resolution_status if last_result else "unresolved"
        _emit("capability_completed", status="resolved" if resolution == "fulfilled" else
              ("partial" if resolution == "partial" else "blocked"),
              resolution_status=resolution)
        return {"run_id": run_id, "contract": contract.to_dict(),
                "resolution_status": resolution,
                "result": last_result.to_dict() if last_result else None,
                "budget": {"actions_used": ledger.actions_used, "http_used": ledger.http_used},
                "events": [e.to_dict() for e in events]}

    async def _execute(self, cand: Candidate, contract: RequirementContract,
                       user_id: str, ledger: BudgetLedger) -> ExecutionResult:
        """按执行类别分流到固定工具适配器或 Sandbox Controller。"""
        if cand.executor_kind == "fixed_tool" and cand.tool_name:
            ctx = tools_base.ToolContext(user_id=user_id, file_path="", filename="",
                                         kind="", role="")
            args = {"query": contract.raw_query}
            tr = await invoke_tool(cand.tool_name, ctx, args, routed_by="orchestrator")
            res = ExecutionResult(capability_id=cand.capability_id, executor_kind="fixed_tool",
                                  execution_status="completed" if tr.success else "failed",
                                  summary=tr.summary,
                                  data=(tr.extra or {}) if tr.success else {},
                                  error_code=(tr.error or "") if not tr.success else "")
            return res

        if cand.executor_kind == "cli_spec":
            from app.services import capability_gap as cg
            ctx_args = cg.extract_context(contract.raw_query)
            sols = cg.propose_solutions(contract.domain if contract.domain in cg.CAPABILITY_RECIPES
                                        else _scenario_for(contract), ctx_args)
            sol = next((s for s in sols if s["id"] == cand.spec_id), None)
            if not sol or sol.get("needs_argument"):
                return ExecutionResult(capability_id=cand.capability_id, executor_kind="cli_spec",
                                       execution_status="blocked", error_code="missing_argument",
                                       summary="方案缺少必要参数（不猜测默认值）")
            spec = ExecutionSpec(executor_kind="cli_spec", argv=list(sol.get("argv", [])),
                                 spec_id=sol["id"], owner_id=user_id,
                                 timeout=float(contract.budgets.get("attempt_seconds", 20)))
            sres = await sandbox_controller.submit_execution(spec)
            res = ExecutionResult(capability_id=cand.capability_id, executor_kind="cli_spec",
                                  summary=(sres.stdout or sres.error or "")[:500])
            res.execution_status = sres.execution_status if sres.execution_status in (
                "completed", "failed", "timeout", "blocked") else "failed"
            if sres.execution_status == "sandbox_unavailable":
                res.execution_status = "blocked"
                res.error_code = "sandbox_unavailable"
                res.summary = sres.error
            if sres.stdout:
                res.data = {"raw": sres.stdout}
            return res

        # python_script / 其他：交 Sandbox Controller（隔离不可用 → sandbox_unavailable）
        spec = ExecutionSpec(executor_kind=cand.executor_kind, owner_id=user_id,
                             timeout=float(contract.budgets.get("attempt_seconds", 20)))
        sres = await sandbox_controller.submit_execution(spec)
        return ExecutionResult(capability_id=cand.capability_id, executor_kind=cand.executor_kind,
                               execution_status="blocked" if sres.execution_status == "sandbox_unavailable" else sres.execution_status,
                               error_code=sres.error_code, summary=sres.error or sres.stdout)


def _scenario_for(contract: RequirementContract) -> str:
    """把需求契约的域映射回能力配方 scenario（weather→realtime_weather 等）。"""
    if contract.domain == "weather":
        return "realtime_weather"
    if contract.domain in ("time", "news"):
        return "realtime_general"
    return "no_tool"


# 全局单例
orchestrator = AgentOrchestrator()
