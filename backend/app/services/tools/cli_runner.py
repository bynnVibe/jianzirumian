"""
见字如面 - CLI 执行内核（受治理的命令行封装）

把系统命令行工具（soffice / tesseract / pandoc / ffmpeg 等）封装为可被工具注册表
统一治理的受控执行内核，遵循「最小权限 + 显式 argv + 无 shell」原则（图3）：

  - 只允许 asyncio.create_subprocess_exec(argv)：禁止 shell=True，从根本上杜绝命令注入
  - shutil.which 预检：二进制不存在时降级返回（success=False），不抛异常、不阻断主流程
  - 超时熔断：asyncio.wait_for + 超时后 kill 整个进程组（start_new_session）
  - cwd 隔离：默认在会话级临时目录执行，避免污染工作区/读到无关文件
  - env 白名单：仅透传 PATH/HOME/LANG/LC_ALL/TMPDIR，剥离 API Key 等敏感环境变量
  - 输出截断：stdout/stderr 头尾截断，防止超长输出撑爆 LLM 上下文
  - 退出码语义：非 0 退出码转 success=False，stderr 摘要回填 error

当系统内置能力满足不了用户需求（如需要转格式、压缩、抽取多媒体信息）时，
路由层可选用 CLI 工具兜底；CLI 工具风险等级默认 high，须经调用前约束钩子
（risk_gate 需 confirmed）与网关安全闸治理后方可执行。
"""
import asyncio
import logging
import os
import shutil
import signal
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

logger = logging.getLogger("jianziruyang.tools.cli_runner")

# 默认执行超时（秒）
DEFAULT_TIMEOUT = 60.0
# 默认 stdout/stderr 头尾截断上限（字符）
DEFAULT_MAX_OUTPUT = 8000
# env 白名单：仅透传这些环境变量给子进程（剥离敏感信息）
DEFAULT_ENV_WHITELIST = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP")


def _truncate(text: str, limit: int) -> str:
    """头尾截断：保留首尾各一半，中间标注省略，避免超长输出撑爆上下文。"""
    text = text or ""
    if limit <= 0 or len(text) <= limit:
        return text
    half = max(0, (limit - 40) // 2)
    return text[:half] + f"\n...(输出过长，已截断，原长 {len(text)} 字符)...\n" + text[-half:]


@dataclass
class CliResult:
    """CLI 执行结果（统一结构，供上层转 ToolResult）。"""

    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False
    duration_ms: int = 0
    binary: str = ""
    argv: List[str] = field(default_factory=list)
    error: str = ""        # 预检失败/异常的可读原因

    def to_dict(self) -> dict:
        return {
            "success": self.success, "exit_code": self.exit_code,
            "timed_out": self.timed_out, "duration_ms": self.duration_ms,
            "binary": self.binary, "error": self.error,
            "stdout_chars": len(self.stdout), "stderr_chars": len(self.stderr),
        }


class CliRunner:
    """受治理的 CLI 执行内核（无 shell、显式 argv、超时熔断、env 白名单、输出截断）。"""

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
        max_output_chars: int = DEFAULT_MAX_OUTPUT,
        env_whitelist: Sequence[str] = DEFAULT_ENV_WHITELIST,
        base_cwd: Optional[str] = None,
    ):
        self.timeout = float(timeout)
        self.max_output_chars = int(max_output_chars)
        self.env_whitelist = tuple(env_whitelist)
        # base_cwd：CLI 默认工作根目录（缺省用系统临时目录下的专用子目录）
        self.base_cwd = base_cwd or str(Path(tempfile.gettempdir()) / "jianziruyang_cli")

    def _build_env(self) -> dict:
        """按白名单构造子进程环境变量（剥离敏感信息）。"""
        env = {}
        for key in self.env_whitelist:
            val = os.environ.get(key)
            if val is not None:
                env[key] = val
        # 确保 PATH 至少存在，否则多数二进制无法定位
        env.setdefault("PATH", os.defpath)
        return env

    def which(self, binary: str) -> Optional[str]:
        """预检二进制是否存在（返回绝对路径或 None）。"""
        return shutil.which(binary)

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout: Optional[float] = None,
        cwd: Optional[str] = None,
        input_text: Optional[str] = None,
        env_extra: Optional[dict] = None,
    ) -> CliResult:
        """执行一条 CLI 命令（显式 argv，无 shell）。

        Args:
            argv: 命令与参数列表（argv[0] 为二进制名/路径）
            timeout: 覆盖默认超时（秒）
            cwd: 覆盖默认工作目录（不存在则自动创建）
            input_text: 写入子进程 stdin 的文本（可选）
            env_extra: 额外环境变量（仍受白名单约束之外的显式追加，调用方自负其责）

        Returns:
            CliResult：预检失败/超时/非 0 退出都收敛为 success=False，绝不抛异常。
        """
        argv = [str(a) for a in (argv or [])]
        if not argv:
            return CliResult(success=False, error="空命令（argv 为空）", argv=[])
        binary = argv[0]
        eff_timeout = float(timeout if timeout is not None else self.timeout)

        # 1. 预检二进制存在性（不存在直接降级，不抛异常）
        resolved = self.which(binary)
        if not resolved:
            logger.warning("[CLI] 二进制不存在，降级返回: %s", binary)
            return CliResult(success=False, binary=binary, argv=argv,
                             error=f"命令「{binary}」未安装或不在 PATH 中")

        # 2. 准备工作目录（隔离）
        work_dir = cwd or self.base_cwd
        try:
            Path(work_dir).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return CliResult(success=False, binary=binary, argv=argv,
                             error=f"创建工作目录失败：{e}")

        # 3. 构造 env（白名单 + 显式追加）
        env = self._build_env()
        if env_extra:
            env.update({str(k): str(v) for k, v in env_extra.items()})

        t0 = time.monotonic()
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=work_dir,
                env=env,
                stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # 独立进程组，超时可整组 kill
            )
        except Exception as e:
            logger.warning("[CLI] 启动子进程失败: %s (%s)", binary, e)
            return CliResult(success=False, binary=binary, argv=argv,
                             duration_ms=int((time.monotonic() - t0) * 1000),
                             error=f"启动命令失败：{e}")

        # 4. 等待完成（超时熔断 → kill 进程组）
        timed_out = False
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=input_text.encode() if input_text else None),
                timeout=eff_timeout,
            )
        except asyncio.TimeoutError:
            timed_out = True
            stdout_b, stderr_b = b"", b""
            logger.warning("[CLI] 命令超时（%.1fs），kill 进程组: %s", eff_timeout, binary)
            await self._kill_group(proc)
        except (asyncio.CancelledError, GeneratorExit):
            await self._kill_group(proc)
            raise
        except Exception as e:
            await self._kill_group(proc)
            return CliResult(success=False, binary=binary, argv=argv,
                             duration_ms=int((time.monotonic() - t0) * 1000),
                             error=f"执行命令异常：{e}")

        duration_ms = int((time.monotonic() - t0) * 1000)
        stdout = _truncate(stdout_b.decode("utf-8", errors="replace"), self.max_output_chars)
        stderr = _truncate(stderr_b.decode("utf-8", errors="replace"), self.max_output_chars)
        exit_code = proc.returncode if proc.returncode is not None else -1

        if timed_out:
            return CliResult(success=False, stdout=stdout, stderr=stderr, exit_code=exit_code,
                             timed_out=True, duration_ms=duration_ms, binary=binary, argv=argv,
                             error=f"命令超时（超过 {eff_timeout:.0f} 秒），已中止")
        success = exit_code == 0
        return CliResult(
            success=success, stdout=stdout, stderr=stderr, exit_code=exit_code,
            timed_out=False, duration_ms=duration_ms, binary=binary, argv=argv,
            error="" if success else (stderr.strip()[:300] or f"命令以退出码 {exit_code} 结束"),
        )

    @staticmethod
    async def _kill_group(proc) -> None:
        """kill 子进程所在整个进程组（start_new_session 使其成为组长）。"""
        if proc is None or proc.returncode is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except Exception:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass


# 全局默认执行内核（可被 cli_spec 覆盖参数）
default_runner = CliRunner()
