"""
见字如面 - 工具统一调用入口（Tool Routing 执行内核）

把「查注册表 → 调用前约束钩子 → 可观测性记录 → 执行工具」收敛为单一入口
invoke_tool()，使所有工具调用（检索 / 附件 / CLI）都经过同一套治理：

  1. 注册表查找：未知工具直接拒绝并记录（status=error）。
  2. 调用前约束（req3）：run_pre_invoke_hooks 顺序运行状态/参数/权限/风险/限流闸，
     fail-fast 短路；被拦截则不执行工具，返回 blocked 结果并记录调用链。
  3. 可观测性（req2）：record_tool_routing 落库一条 Tool Routing 记录，含
     参数合法率 / 越权暴露 / 风险拦截 / 限流 / 任务成功 / Schema Token / 端到端延迟。
  4. 执行：tool.run(ctx, **args)，异常转 ToolResult（不抛出，保证主流程不崩）。

invoke_tool 绝不抛出业务异常（除取消/生成器退出），所有失败都收敛为 ToolResult，
与既有 chat_agent「异常转观测，静默降级」的约定一致。
"""
import logging
import time
from typing import Any, Dict, Optional

from app.services.tools import base as tools_base
from app.services.tools.base import ToolContext, ToolResult, get_tool, schema_token_estimate
from app.services.tools.hooks import PreInvokeResult, run_pre_invoke_hooks

logger = logging.getLogger("jianziruyang.tools.invoke")


def build_args_from_ctx(ctx: ToolContext, extra: Optional[dict] = None) -> Dict[str, Any]:
    """从 ToolContext 派生附件类工具的逻辑参数（供 Schema 校验与调用链记录）。

    附件工具（image_ocr/image_upload/document_import/knowledge_summarize）的真实输入
    来自 ctx（file_path/filename/kind/...），而非显式 args；此处把 ctx 关键字段映射为
    参数 dict，使统一的 Schema 校验/可观测性对附件工具同样生效。
    """
    args: Dict[str, Any] = {
        "file_path": ctx.file_path,
        "filename": ctx.filename,
        "kind": ctx.kind,
    }
    if ctx.kb_id:
        args["kb_id"] = ctx.kb_id
    if ctx.visibility:
        args["visibility"] = ctx.visibility
    if extra:
        args.update(extra)
    return args


async def invoke_tool(
    name: str,
    ctx: ToolContext,
    args: Optional[dict] = None,
    *,
    confirmed: bool = False,
    allow_high_risk: bool = False,
    routed_by: str = "manual",
    record_obs: bool = True,
    rate_window: Optional[float] = None,
    rate_max_calls: Optional[int] = None,
) -> ToolResult:
    """统一工具调用入口：约束钩子 → 可观测性记录 → 执行工具。

    Args:
        name: 工具名（注册表键）
        ctx:  调用上下文（user_id/role/session_id/...）
        args: 调用参数（检索类={"query":..}；附件类可由 build_args_from_ctx 派生）
        confirmed: 是否已获人工确认（HITL），放行 high/critical 风险工具
        allow_high_risk: 显式放行高风险（运维/测试用）
        routed_by: 路由来源标记（router/agent/orchestrator/manual），供调用链分析
        record_obs: 是否记录 Tool Routing 可观测性（默认 True）
        rate_window/rate_max_calls: 覆盖默认限流参数

    Returns:
        ToolResult：成功/失败/被拦截都收敛为 ToolResult，绝不抛业务异常。
    """
    args = dict(args or {})
    t0 = time.monotonic()
    tool = get_tool(name)

    # 1. 未知工具：直接拒绝并记录
    if tool is None:
        result = ToolResult(
            tool_name=name, success=False,
            summary=f"未知工具：{name}。", error="unknown_tool",
        )
        if record_obs:
            _record(name, "", ctx, args, None, result, routed_by, t0, 0, status="error")
        return result

    schema_tokens = schema_token_estimate([name]) if record_obs else 0

    # 2. 调用前约束钩子链（req3）
    opts: Dict[str, Any] = {"confirmed": confirmed, "allow_high_risk": allow_high_risk}
    if rate_window is not None:
        opts["rate_window"] = rate_window
    if rate_max_calls is not None:
        opts["rate_max_calls"] = rate_max_calls
    pre = run_pre_invoke_hooks(tool, args, ctx, opts)

    if not pre.allow:
        result = ToolResult(
            tool_name=name, success=False,
            summary=f"工具调用被约束拦截：{pre.reason()}", error=pre.code(),
            extra={"blocked_code": pre.code(), "blocked_reason": pre.reason()},
        )
        if record_obs:
            _record(name, tool.namespace, ctx, args, pre, result, routed_by, t0,
                    schema_tokens, status="blocked")
        return result

    # 3. 执行工具（异常转 ToolResult，不抛出）
    try:
        result = await tool.run(ctx, **args)
        status = "ok" if result.success else "error"
    except Exception as e:
        logger.warning("[INVOKE] 工具 %s 执行异常: %s", name, e)
        result = ToolResult(
            tool_name=name, success=False,
            summary=f"工具 {name} 执行出错：{e}", error=str(e),
        )
        status = "error"

    # 4. 可观测性记录（req2）
    if record_obs:
        _record(name, tool.namespace, ctx, args, pre, result, routed_by, t0,
                schema_tokens, status=status)
    return result


def _record(name: str, namespace: str, ctx: ToolContext, args: dict,
            pre: Optional[PreInvokeResult], result: ToolResult, routed_by: str,
            t0: float, schema_tokens: int, status: str) -> None:
    """落库一条 Tool Routing 记录（可观测性绝不影响主流程，失败静默）。"""
    try:
        from app.core import observability as obs

        obs.record_tool_routing(
            tool_name=name,
            namespace=namespace,
            user_id=ctx.user_id,
            session_id=ctx.session_id,
            trace_id=ctx.trace_id,
            routed_by=routed_by,
            args=args,
            risk_level=(get_tool(name).risk_level if get_tool(name) else "low"),
            schema_valid=(pre.schema_valid if pre else True),
            permission_ok=(pre.permission_ok if pre else True),
            risk_blocked=(pre.risk_blocked if pre else False),
            rate_limited=(pre.rate_limited if pre else False),
            allowed=(pre.allow if pre else True),
            blocked_code=(pre.code() if pre else ""),
            status=status,
            success=bool(result.success),
            schema_tokens=schema_tokens,
            t0=t0,
            error=(result.error or "")[:500],
        )
    except Exception as e:
        logger.warning("Tool Routing 可观测性记录失败（忽略）: %s", e)
