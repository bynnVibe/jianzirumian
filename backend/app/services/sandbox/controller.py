"""
见字如面 - 可信 Sandbox Controller（受控执行控制面）

对应《整改方案》§6/§7/§10 与图5「可信 Sandbox Controller → 一次性执行环境」。

Controller 位于可信控制面，职责是：创建任务环境、验证执行合同、启动与取消、清理；
它 **不接受模型提供的任意镜像、挂载或宿主路径**，只接受审核后的运行配置。

核心不变量（§3.1 第 8 条）：
    隔离环境不可用时拒绝动态执行，返回 sandbox_unavailable，**绝不退回主机子进程**。

执行类别分流：
  - fixed_tool：专用工具/API，不经沙箱（由统一调用入口 invoke_tool 治理）；
  - cli_spec：声明式命令。仅当 spec_id 属于「预审核固定命令白名单」且低风险时，
    以 fixed_adapter 运行配置经受治理 CliRunner（无 shell/显式 argv/env 白名单/超时熔断/
    输出限额）执行——这是 §7.2 模式 B 的过渡落地；其余一律要求 isolated 运行配置；
  - python_script / 任意二进制 / 动态参数：要求 isolated 运行配置；未配置隔离运行时时
    返回 sandbox_unavailable（第一版动态代码「待沙箱与审批验收后开放」，§3.2）。

网络出口（§11）：fixed_adapter 的联网命令只允许访问审核过的公网 host 白名单，
禁止回环/内网/链路本地/云元数据地址，凭据不随命令透传。
"""
from __future__ import annotations

import ipaddress
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import urlparse

from app.services.tools import cli_runner

logger = logging.getLogger("jianziruyang.sandbox")

# 预审核固定命令白名单：只有这些二进制可作为 fixed_adapter 在受治理 Runner 执行。
# 不把「PATH 中有该程序」当执行白名单（§7.2）；此处是维护者显式审核的最小子集。
FIXED_CLI_ALLOWLIST = frozenset({"date", "curl"})

# 联网命令的公网 host 白名单（§11.1 Operation Broker 的最小落地：限定连接器）。
EGRESS_HOST_ALLOWLIST = frozenset({
    "wttr.in",
    "api.open-meteo.com",
    "geocoding-api.open-meteo.com",
})

# 运行配置（runtime profile）：isolated 需真实隔离运行时；fixed_adapter 为受治理 Runner。
_ISOLATED_READY_FLAG = "SANDBOX_ISOLATED_RUNTIME_READY"


@dataclass
class ExecutionSpec:
    """服务端生成的执行合同子集（§9.2）；模型不能设置镜像/挂载/宿主路径/审批结论。"""

    executor_kind: str                    # fixed_tool | cli_spec | python_script
    argv: List[str] = field(default_factory=list)
    code: str = ""                        # 动态代码（python_script）
    spec_id: str = ""                     # cli_spec 的已审核规格 id
    spec_version: str = ""
    runtime_profile_id: str = ""          # 缺省由 Controller 按 executor_kind 解析
    owner_id: str = ""
    session_id: str = ""
    run_id: str = ""
    timeout: float = 20.0
    max_output_chars: int = 8000
    approval_id: str = ""
    plan_hash: str = ""


@dataclass
class SandboxResult:
    """执行层结果（仅 execution_status；validation/resolution 由上层判定）。"""

    execution_id: str
    execution_status: str                 # completed | failed | timeout | blocked | sandbox_unavailable
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    duration_ms: int = 0
    runtime_profile_id: str = ""
    error_code: str = ""
    error: str = ""
    argv: List[str] = field(default_factory=list)
    isolated: bool = False

    def to_dict(self) -> dict:
        return {
            "execution_id": self.execution_id,
            "execution_status": self.execution_status,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "runtime_profile_id": self.runtime_profile_id,
            "error_code": self.error_code,
            "error": self.error,
            "isolated": self.isolated,
            "stdout_chars": len(self.stdout),
            "stderr_chars": len(self.stderr),
        }


def _isolated_runtime_ready() -> bool:
    """隔离运行时是否就绪：由服务端配置决定，绝不由模型或请求体控制。

    第一版默认未配置真实隔离运行时（Docker/gVisor 等）→ 返回 False，
    动态执行据此返回 sandbox_unavailable，不回退主机子进程。
    """
    from app.config import settings
    return bool(getattr(settings, _ISOLATED_READY_FLAG, False))


def _is_public_host(host: str) -> bool:
    """SSRF 约束（§11.3）：拒绝回环/内网/链路本地/云元数据等非公网目标。"""
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified)
    except ValueError:
        # 非 IP：按 host 白名单判断（域名规范化后仍需 Broker 侧解析校验）
        return host.lower() in EGRESS_HOST_ALLOWLIST


def _check_egress(argv: List[str]) -> Optional[str]:
    """校验联网命令的目标 host 是否在审核白名单内；返回拒绝原因或 None。"""
    joined = " ".join(argv)
    if "curl" not in joined and "http" not in joined:
        return None
    for tok in argv:
        if "://" in tok:
            host = (urlparse(tok).hostname or "").lower()
            if not _is_public_host(host):
                return f"目标 host「{host or tok[:40]}」不在审核的公网白名单内（禁止内网/回环/元数据）"
    return None


class SandboxController:
    """可信控制面：提交/查询/取消类型化执行任务，仅供可信后端调用。"""

    def __init__(self):
        # execution_id → SandboxResult（本期进程内记录；生产可换持久化 + 租约）
        self._executions: Dict[str, SandboxResult] = {}

    def list_profiles(self) -> List[dict]:
        return [
            {"id": "fixed_adapter", "kind": "fixed_adapter",
             "available": True,
             "description": "受治理 CliRunner：无 shell/显式 argv/env 白名单/超时熔断/输出限额；"
                            "仅限预审核固定命令白名单。"},
            {"id": "isolated", "kind": "isolated",
             "available": _isolated_runtime_ready(),
             "image_digest": "",
             "description": "一次性隔离执行环境（只读根/非 root/断网/资源限额）；"
                            "未配置隔离运行时时不可用，动态执行将被拒绝而非回退主机。"},
        ]

    def _resolve_profile(self, spec: ExecutionSpec) -> tuple[str, Optional[str]]:
        """按 executor_kind 解析运行配置；返回 (profile_id, 拒绝原因)。"""
        if spec.runtime_profile_id:
            return spec.runtime_profile_id, None
        if spec.executor_kind == "cli_spec":
            binary = (spec.argv[0] if spec.argv else "").split("/")[-1]
            if binary in FIXED_CLI_ALLOWLIST and not _isolated_runtime_ready():
                # 过渡：预审核固定命令走 fixed_adapter；其余要求 isolated
                return "fixed_adapter", None
            return "isolated", None
        if spec.executor_kind == "python_script":
            return "isolated", None
        return "fixed_adapter", None

    async def submit_execution(self, spec: ExecutionSpec) -> SandboxResult:
        """提交一次执行；强制执行合同边界与「隔离不可用不回退主机」不变量。"""
        execution_id = str(uuid.uuid4())

        if spec.executor_kind == "fixed_tool":
            # 专用工具不经沙箱；应由统一调用入口处理，这里明确拒绝误用
            res = SandboxResult(execution_id=execution_id, execution_status="blocked",
                                error_code="wrong_executor",
                                error="fixed_tool 应经统一调用入口，不通过 Sandbox Controller")
            self._executions[execution_id] = res
            return res

        profile_id, _ = self._resolve_profile(spec)

        # cli_spec：只允许已审核 spec_id + 白名单二进制；禁止模型指定任意 binary
        if spec.executor_kind == "cli_spec":
            binary = (spec.argv[0] if spec.argv else "").split("/")[-1]
            if profile_id == "fixed_adapter":
                if binary not in FIXED_CLI_ALLOWLIST:
                    return self._record(execution_id, "blocked", "unapproved_binary",
                                        f"二进制「{binary}」不在预审核固定命令白名单内", spec, profile_id)
                if not spec.spec_id:
                    return self._record(execution_id, "blocked", "missing_spec_id",
                                        "cli_spec 执行必须绑定已审核 spec_id", spec, profile_id)
                egress = _check_egress(spec.argv)
                if egress:
                    return self._record(execution_id, "blocked", "egress_denied", egress, spec, profile_id)
            else:
                # 需要隔离但隔离不可用 → 拒绝，不回退主机（§3.1 第 8 条）
                if not _isolated_runtime_ready():
                    return self._record(execution_id, "sandbox_unavailable", "sandbox_unavailable",
                                        "隔离执行环境不可用；按不变量拒绝动态执行，不回退主机子进程",
                                        spec, profile_id)

        # python_script / 动态：必须隔离运行时
        if spec.executor_kind == "python_script" and not _isolated_runtime_ready():
            return self._record(execution_id, "sandbox_unavailable", "sandbox_unavailable",
                                "动态 Python 执行需要隔离运行时；当前未配置，拒绝执行（不回退主机）",
                                spec, "isolated")

        # fixed_adapter：经受治理 CliRunner 执行（无 shell/超时/输出限额）
        if profile_id == "fixed_adapter":
            runner = cli_runner.default_runner
            cli = await runner.run(spec.argv, timeout=spec.timeout)
            status = "completed" if cli.success else ("timeout" if cli.timed_out else "failed")
            res = SandboxResult(
                execution_id=execution_id, execution_status=status,
                stdout=cli.stdout, stderr=cli.stderr, exit_code=cli.exit_code,
                duration_ms=cli.duration_ms, runtime_profile_id=profile_id,
                error=cli.error, argv=list(spec.argv), isolated=False,
            )
            self._executions[execution_id] = res
            return res

        # isolated 且就绪：真实隔离执行由部署侧 Runner 实现（本期未接入 → 明确不可用）
        return self._record(execution_id, "sandbox_unavailable", "isolated_runner_not_wired",
                            "隔离 Runner 尚未在本部署接入；拒绝执行，不回退主机", spec, "isolated")

    def _record(self, execution_id: str, status: str, error_code: str, error: str,
                spec: ExecutionSpec, profile_id: str) -> SandboxResult:
        res = SandboxResult(execution_id=execution_id, execution_status=status,
                            error_code=error_code, error=error,
                            runtime_profile_id=profile_id, argv=list(spec.argv),
                            isolated=(profile_id == "isolated"))
        self._executions[execution_id] = res
        logger.warning("[SANDBOX] %s: %s (profile=%s, spec=%s)", error_code, error,
                       profile_id, spec.spec_id or spec.executor_kind)
        return res

    def get_execution(self, execution_id: str, owner_id: str = "") -> Optional[SandboxResult]:
        res = self._executions.get(execution_id)
        return res

    def cancel_execution(self, execution_id: str) -> bool:
        # 本期执行均为短任务、同步等待完成；取消语义在接入隔离 Runner 后扩展。
        return execution_id in self._executions


# 全局单例（可信控制面）
sandbox_controller = SandboxController()


def _now() -> str:
    return datetime.now().isoformat()
