"""
见字如面 - 工具调用前约束钩子（pre-invocation hooks）

在工具真正执行前，按顺序运行一组只读约束钩子；任一钩子拒绝即中止调用并返回
约束结果（绝不执行工具）。钩子链参考「调用前约束」范式（图2）：

  1. status_gate     可用状态闸：disabled / deprecated 工具拒绝调用
  2. schema_validate 参数合法性闸：按工具 JSON Schema 校验必填/类型/枚举
                     （→ 可观测性「参数合法率」指标来源）
  3. permission_gate 权限闸：调用方角色须满足工具 permissions
                     （→ 可观测性「越权暴露率」指标来源）
  4. risk_gate       风险闸：high/critical 风险工具需已获人工确认（confirmed）或显式放行
  5. rate_limit      频率闸：同一 (用户, 工具) 短窗口内调用次数上限，防滥用/失控循环

设计约束：
  - 所有钩子只读「工具元数据 + 参数 + 上下文」，绝不产生副作用（不写库、不发网络），
    保证「调用前约束」的纯粹性与可重入性。
  - 钩子返回 HookVerdict(allow, code, reason, hook)；HookChain 顺序执行，
    遇首个拒绝即短路（fail-fast），但记录已执行钩子的全部判定供调用链可视化。
  - 钩子链可通过 runtime_config 开关/调参（如限流窗口），默认全部启用。
"""
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.services.tools.base import BaseTool, ToolContext

logger = logging.getLogger("jianziruyang.tools.hooks")

# 限流默认参数：同一 (用户, 工具) 在窗口秒数内最多调用次数
_RATE_WINDOW_SECONDS = 60.0
_RATE_MAX_CALLS = 30


@dataclass
class HookVerdict:
    """单个钩子的判定结果。"""

    allow: bool
    hook: str = ""          # 钩子名
    code: str = ""          # 约束码：status_disabled/schema_invalid/permission_denied/risk_blocked/rate_limited
    reason: str = ""        # 人类可读原因（拒绝时回填给调用方/前端）

    def to_dict(self) -> dict:
        return {"hook": self.hook, "allow": self.allow, "code": self.code, "reason": self.reason}


@dataclass
class PreInvokeResult:
    """钩子链汇总结果（供 invoke 决策与可观测性记录调用链）。"""

    allow: bool
    verdicts: List[HookVerdict] = field(default_factory=list)
    blocked_by: Optional[HookVerdict] = None
    # 关键指标位（供 Tool Routing 可观测性聚合）
    schema_valid: bool = True       # 参数是否合法 → 参数合法率
    permission_ok: bool = True      # 权限是否通过 → 越权暴露率（False=越权被拦截）
    risk_blocked: bool = False      # 是否被风险闸拦截
    rate_limited: bool = False      # 是否被限流拦截

    def reason(self) -> str:
        return self.blocked_by.reason if self.blocked_by else ""

    def code(self) -> str:
        return self.blocked_by.code if self.blocked_by else ""


# ---------------------------------------------------------------------------
# 轻量 JSON Schema 校验（必填 / 类型 / 枚举），不引入额外依赖
# ---------------------------------------------------------------------------

_TYPE_MAP: Dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def validate_schema(schema: Optional[dict], args: Dict[str, Any]) -> Tuple[bool, str]:
    """按工具参数 JSON Schema 校验 args，返回 (是否合法, 失败原因)。

    覆盖 function calling schema 的核心约束：required 必填、type 基本类型、enum 取值。
    schema 为空或非 object 时视为无约束（合法）。
    """
    if not schema or schema.get("type") != "object":
        return True, ""
    props = schema.get("properties", {}) or {}
    required = schema.get("required", []) or []
    args = args or {}

    for req in required:
        val = args.get(req, None)
        if val is None or val == "":
            # 有默认值的必填项允许缺省（执行时由工具兜底）
            if req in props and "default" in props[req]:
                continue
            return False, f"缺少必填参数「{req}」"

    for key, val in args.items():
        spec = props.get(key)
        if not spec or val is None:
            continue
        expected = spec.get("type")
        if expected in _TYPE_MAP:
            if expected == "integer" and isinstance(val, bool):
                return False, f"参数「{key}」应为 integer，实际为 boolean"
            if expected == "number" and isinstance(val, bool):
                return False, f"参数「{key}」应为 number，实际为 boolean"
            if not isinstance(val, _TYPE_MAP[expected]):
                return False, f"参数「{key}」应为 {expected}，实际为 {type(val).__name__}"
        enum = spec.get("enum")
        if enum and val not in enum:
            return False, f"参数「{key}」取值 {val!r} 不在允许范围 {enum}"
    return True, ""


# ---------------------------------------------------------------------------
# 限流：进程内滑动窗口（单实例部署够用；多实例可换 Redis）
# ---------------------------------------------------------------------------

_rate_buckets: Dict[Tuple[str, str], deque] = defaultdict(deque)


def _rate_limit_check(user_id: str, tool_name: str, window: float, max_calls: int) -> Tuple[bool, str]:
    """滑动窗口限流：返回 (是否放行, 拒绝原因)。"""
    if max_calls <= 0:
        return True, ""
    now = time.monotonic()
    key = (user_id or "anon", tool_name)
    bucket = _rate_buckets[key]
    while bucket and now - bucket[0] > window:
        bucket.popleft()
    if len(bucket) >= max_calls:
        return False, f"工具「{tool_name}」调用过于频繁（{int(window)}s 内超过 {max_calls} 次），请稍后再试"
    bucket.append(now)
    return True, ""


def reset_rate_limits() -> None:
    """清空限流计数（供测试与运维重置）。"""
    _rate_buckets.clear()


# ---------------------------------------------------------------------------
# 钩子定义：每个钩子签名 (tool, args, ctx, opts) -> HookVerdict
# ---------------------------------------------------------------------------

def status_gate(tool: BaseTool, args: dict, ctx: ToolContext, opts: dict) -> HookVerdict:
    """可用状态闸：仅 active 工具可调用。"""
    if tool.status != "active":
        return HookVerdict(False, "status_gate", "status_disabled",
                           f"工具「{tool.name}」当前状态为 {tool.status}，不可调用")
    return HookVerdict(True, "status_gate")


def schema_validate(tool: BaseTool, args: dict, ctx: ToolContext, opts: dict) -> HookVerdict:
    """参数合法性闸：按工具 JSON Schema 校验必填/类型/枚举。"""
    valid, reason = validate_schema(tool.parameters, args or {})
    if not valid:
        return HookVerdict(False, "schema_validate", "schema_invalid", reason)
    return HookVerdict(True, "schema_validate")


def permission_gate(tool: BaseTool, args: dict, ctx: ToolContext, opts: dict) -> HookVerdict:
    """权限闸：调用方角色须满足工具 permissions。

    约定：
      - permissions 为空 → 无需特殊权限（登录/游客皆可，由业务层另行约束）。
      - "authenticated" → user_id 不能为空或 guest。
      - 其余角色名（admin/ops/user）→ ctx.role 须匹配其一。
    """
    perms = set(tool.permissions or ())
    if not perms:
        return HookVerdict(True, "permission_gate")
    if "authenticated" in perms:
        if not ctx.user_id or ctx.user_id in ("guest", "system", "-"):
            return HookVerdict(False, "permission_gate", "permission_denied",
                               f"工具「{tool.name}」需要登录用户，当前为游客/匿名")
        perms.discard("authenticated")
        if not perms:
            return HookVerdict(True, "permission_gate")
    role = (ctx.role or "").strip().lower()
    if role and role in perms:
        return HookVerdict(True, "permission_gate")
    # 管理员/运维默认放行（拥有全部权限）
    if role in ("admin", "ops"):
        return HookVerdict(True, "permission_gate")
    return HookVerdict(False, "permission_gate", "permission_denied",
                       f"工具「{tool.name}」需要权限 {sorted(tool.permissions)}，当前角色「{role or '未知'}」不满足")


def risk_gate(tool: BaseTool, args: dict, ctx: ToolContext, opts: dict) -> HookVerdict:
    """风险闸：high/critical 风险工具需已获人工确认（opts['confirmed']）或显式放行。"""
    if not tool.needs_confirmation():
        return HookVerdict(True, "risk_gate")
    if opts.get("confirmed") or opts.get("allow_high_risk"):
        return HookVerdict(True, "risk_gate")
    return HookVerdict(False, "risk_gate", "risk_blocked",
                       f"工具「{tool.name}」风险等级为 {tool.risk_level}，需人工确认后方可执行")


def rate_limit(tool: BaseTool, args: dict, ctx: ToolContext, opts: dict) -> HookVerdict:
    """频率闸：同一 (用户, 工具) 短窗口内调用次数上限。"""
    window = float(opts.get("rate_window", _RATE_WINDOW_SECONDS))
    max_calls = int(opts.get("rate_max_calls", _RATE_MAX_CALLS))
    ok, reason = _rate_limit_check(ctx.user_id, tool.name, window, max_calls)
    if not ok:
        return HookVerdict(False, "rate_limit", "rate_limited", reason)
    return HookVerdict(True, "rate_limit")


# 默认钩子链（顺序即执行顺序，fail-fast 短路）
DEFAULT_HOOKS: List[Callable[[BaseTool, dict, ToolContext, dict], HookVerdict]] = [
    status_gate,
    schema_validate,
    permission_gate,
    risk_gate,
    rate_limit,
]


def run_pre_invoke_hooks(
    tool: BaseTool,
    args: Optional[dict],
    ctx: ToolContext,
    opts: Optional[dict] = None,
    hooks: Optional[List[Callable]] = None,
) -> PreInvokeResult:
    """顺序运行调用前约束钩子链，返回汇总结果（fail-fast：遇首个拒绝即短路）。

    Args:
        tool: 待调用的工具（携带标准化元数据）
        args: 调用参数
        ctx:  调用上下文（user_id/role/...）
        opts: 钩子选项（confirmed/allow_high_risk/rate_window/rate_max_calls）
        hooks: 自定义钩子链（默认 DEFAULT_HOOKS）

    Returns:
        PreInvokeResult：allow=是否放行；verdicts=已执行钩子判定；blocked_by=首个拒绝。
    """
    args = args or {}
    opts = opts or {}
    chain = hooks if hooks is not None else DEFAULT_HOOKS
    result = PreInvokeResult(allow=True)
    for hook in chain:
        try:
            verdict = hook(tool, args, ctx, opts)
        except Exception as e:  # 钩子自身异常不应放行，按拒绝处理（保守安全）
            logger.warning("约束钩子 %s 执行异常，按拒绝处理: %s", getattr(hook, "__name__", hook), e)
            verdict = HookVerdict(False, getattr(hook, "__name__", "hook"), "hook_error", f"约束钩子异常：{e}")
        result.verdicts.append(verdict)
        # 回填关键指标位
        if verdict.hook == "schema_validate":
            result.schema_valid = verdict.allow
        elif verdict.hook == "permission_gate":
            result.permission_ok = verdict.allow
        elif verdict.hook == "risk_gate" and not verdict.allow:
            result.risk_blocked = True
        elif verdict.hook == "rate_limit" and not verdict.allow:
            result.rate_limited = True
        if not verdict.allow:
            result.allow = False
            result.blocked_by = verdict
            logger.info("[HOOK] 工具 %s 调用被拦截: code=%s reason=%s",
                        tool.name, verdict.code, verdict.reason)
            break  # fail-fast 短路
    return result
