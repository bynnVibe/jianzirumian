"""
见字如面 - CLI 工具声明式规格（CliToolSpec → 自动注册为受治理工具）

把「命令行封装」声明为与内置工具同构的注册表一等公民：一份规格 = 一个工具，
经 register_cli_tool 包装为 CliTool(BaseTool) 后进入统一注册表，从而复用
function calling schema 导出、调用前约束钩子（风险闸/权限闸）、Tool Routing
可观测性与场景感知路由——CLI 能力与内置能力治理口径完全一致（图3）。

规格字段（声明式，不含执行逻辑）：
  - binary：可执行文件名（经 shutil.which 预检）
  - argv_prefix：固定参数前缀（如 --headless --convert-to pdf）
  - option_flags：{参数名: 选项旗标}，构造 `旗标 值`
  - positional：作为位置参数按序追加的参数名
  - defaults：参数默认值（如 outdir 缺省到隔离临时目录）
  - output_kind：stdout(返回标准输出文本) | file(产出文件，返回输出目录)
  - risk_level：默认 high（执行外部进程有副作用，经风险闸需 confirmed）

规格来源：内置 Python 规格（BUILTIN_CLI_SPECS）+ 可选 JSON 规格目录扫描
（cli_specs/*.json），便于运维零代码扩展受治理的命令行能力。

安全边界（与 cli_runner 一致）：只构造显式 argv、无 shell、二进制白名单预检、
env 白名单、超时熔断、输出截断；绝不接受裸 shell 字符串或管道/重定向元字符。
"""
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.services.tools.base import BaseTool, ToolContext, ToolResult, register_tool
from app.services.tools.cli_runner import CliRunner, CliResult, default_runner

logger = logging.getLogger("jianziruyang.tools.cli_spec")

# 可选 JSON 规格目录（运维零代码扩展；不存在则跳过）
_CLI_SPEC_DIR = Path(__file__).parent / "cli_specs"


@dataclass
class CliToolSpec:
    """一条 CLI 工具的声明式规格。"""

    name: str
    description: str
    binary: str
    namespace: str = "cli"
    tags: Tuple[str, ...] = ("cli",)
    scenarios: Tuple[str, ...] = ()
    parameters: Dict[str, Any] = field(default_factory=dict)
    permissions: Tuple[str, ...] = ("authenticated",)
    risk_level: str = "high"
    status: str = "active"
    requires_confirmation: Optional[bool] = None
    timeout: float = 120.0
    cwd: Optional[str] = None
    argv_prefix: Tuple[str, ...] = ()
    option_flags: Dict[str, str] = field(default_factory=dict)
    positional: Tuple[str, ...] = ()
    defaults: Dict[str, Any] = field(default_factory=dict)
    output_kind: str = "stdout"   # stdout | file

    def build_argv(self, args: Dict[str, Any]) -> List[str]:
        """按规格把参数构造为显式 argv（无 shell，杜绝注入）。"""
        merged = {**self.defaults, **(args or {})}
        argv: List[str] = [self.binary, *[str(p) for p in self.argv_prefix]]
        for arg_name, flag in self.option_flags.items():
            val = merged.get(arg_name)
            if val not in (None, ""):
                argv.extend([str(flag), str(val)])
        for arg_name in self.positional:
            val = merged.get(arg_name)
            if val not in (None, ""):
                argv.append(str(val))
        return argv


class CliTool(BaseTool):
    """把 CliToolSpec 包装为注册表工具：run() 构造 argv → CliRunner 执行 → ToolResult。"""

    def __init__(self, spec: CliToolSpec, runner: Optional[CliRunner] = None):
        self.spec = spec
        self.runner = runner or default_runner
        # 把规格元数据映射到 BaseTool 标准字段（注册表/function schema/钩子共用）
        self.name = spec.name
        self.description = spec.description
        self.namespace = spec.namespace
        self.tags = tuple(spec.tags)
        self.scenarios = tuple(spec.scenarios)
        self.parameters = spec.parameters or {"type": "object", "properties": {}}
        self.permissions = tuple(spec.permissions)
        self.risk_level = spec.risk_level
        self.status = spec.status
        self.requires_confirmation = spec.requires_confirmation

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        spec = self.spec
        args = dict(kwargs or {})

        # file 类输出：outdir 缺省到隔离临时目录（避免污染工作区）
        if spec.output_kind == "file" and not args.get("outdir"):
            outdir = Path(self.runner.base_cwd) / ctx.session_id / spec.name
            outdir.mkdir(parents=True, exist_ok=True)
            args["outdir"] = str(outdir)

        argv = spec.build_argv(args)
        cli: CliResult = await self.runner.run(
            argv, timeout=spec.timeout, cwd=args.get("cwd") or spec.cwd,
        )

        extra = {"cli": cli.to_dict(), "argv": argv, "output_kind": spec.output_kind}
        if spec.output_kind == "file":
            extra["outdir"] = args.get("outdir", "")

        if not cli.success:
            return ToolResult(
                tool_name=self.name, success=False,
                summary=f"命令行工具「{spec.name}」执行失败：{cli.error}",
                error=cli.error or f"exit_code={cli.exit_code}", extra=extra,
            )

        if spec.output_kind == "file":
            summary = f"已通过命令行「{spec.binary}」完成处理，输出目录：{args.get('outdir', '')}"
            text = cli.stdout.strip()[:1000]
        else:
            text = cli.stdout
            summary = (cli.stdout.strip()[:300] or f"命令「{spec.binary}」执行成功（无标准输出）")
        return ToolResult(
            tool_name=self.name, success=True, text=text, summary=summary, extra=extra,
        )


def register_cli_tool(spec: CliToolSpec, runner: Optional[CliRunner] = None) -> CliTool:
    """把一条 CLI 规格包装为 CliTool 并注册到统一注册表。"""
    tool = CliTool(spec, runner)
    register_tool(tool)
    logger.info("[CLI] 注册命令行工具: %s (binary=%s risk=%s)", spec.name, spec.binary, spec.risk_level)
    return tool


# ---------------------------------------------------------------------------
# 内置 CLI 规格：把既有 ad-hoc 子进程能力收敛为受治理工具
# ---------------------------------------------------------------------------

# LibreOffice 文档转 PDF：迁移 knowledge.py 中 `soffice --headless --convert-to pdf` 的
# ad-hoc subprocess.run 调用，纳入注册表治理（风险闸/权限闸/可观测性/路由）。
DOC_TO_PDF_SPEC = CliToolSpec(
    name="doc_to_pdf",
    description="用 LibreOffice（soffice）把 Word/ODT 等文档转换为 PDF（受治理的命令行封装）",
    binary="soffice",
    namespace="cli",
    tags=("cli", "convert", "document", "libreoffice", "pdf"),
    scenarios=(
        "需要把 Word/ODT 文档转换为 PDF 时",
        "内置解析器不支持的文档格式转换需求",
        "系统能力不足需借助命令行工具兜底完成格式转换",
    ),
    parameters={
        "type": "object",
        "properties": {
            "input_path": {"type": "string", "description": "待转换文档的绝对路径"},
            "outdir": {"type": "string", "description": "PDF 输出目录（缺省到隔离临时目录）"},
        },
        "required": ["input_path"],
    },
    permissions=("authenticated",),
    risk_level="high",
    timeout=120.0,
    argv_prefix=("--headless", "--convert-to", "pdf"),
    option_flags={"outdir": "--outdir"},
    positional=("input_path",),
    output_kind="file",
)

BUILTIN_CLI_SPECS: Tuple[CliToolSpec, ...] = (DOC_TO_PDF_SPEC,)


def _load_json_specs() -> List[CliToolSpec]:
    """扫描 cli_specs/*.json，把声明式规格加载为 CliToolSpec（运维零代码扩展）。"""
    specs: List[CliToolSpec] = []
    if not _CLI_SPEC_DIR.is_dir():
        return specs
    for path in sorted(_CLI_SPEC_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not data.get("name") or not data.get("binary"):
                logger.warning("[CLI] 跳过无效规格文件: %s", path.name)
                continue
            specs.append(CliToolSpec(
                name=data["name"],
                description=data.get("description", ""),
                binary=data["binary"],
                namespace=data.get("namespace", "cli"),
                tags=tuple(data.get("tags", ("cli",))),
                scenarios=tuple(data.get("scenarios", ())),
                parameters=data.get("parameters", {}),
                permissions=tuple(data.get("permissions", ("authenticated",))),
                risk_level=data.get("risk_level", "high"),
                status=data.get("status", "active"),
                requires_confirmation=data.get("requires_confirmation"),
                timeout=float(data.get("timeout", 120.0)),
                cwd=data.get("cwd"),
                argv_prefix=tuple(data.get("argv_prefix", ())),
                option_flags=dict(data.get("option_flags", {})),
                positional=tuple(data.get("positional", ())),
                defaults=dict(data.get("defaults", {})),
                output_kind=data.get("output_kind", "stdout"),
            ))
        except Exception as e:
            logger.warning("[CLI] 加载规格文件 %s 失败: %s", path.name, e)
    return specs


def register_all_cli_tools() -> List[str]:
    """注册全部内置 + JSON 规格 CLI 工具，返回已注册工具名列表。"""
    registered: List[str] = []
    for spec in list(BUILTIN_CLI_SPECS) + _load_json_specs():
        try:
            tool = register_cli_tool(spec)
            registered.append(tool.name)
        except Exception as e:
            logger.warning("[CLI] 注册工具 %s 失败: %s", spec.name, e)
    return registered
