"""
见字如面 - 工具调用框架基础定义

ToolContext: 单次工具执行所需的上下文（附件路径、用户、目标知识库等）。
ToolResult: 工具执行结果的统一结构，summary 字段用于拼接进 LLM 对话上下文。
BaseTool: 所有工具的基类；register_tool/get_tool 提供简单的进程内注册表。
"""
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ToolContext:
    """工具执行上下文"""

    user_id: str
    file_path: str
    filename: str
    kind: str  # "image" | "word" | "pdf"
    kb_id: str = ""
    visibility: str = "private"
    ocr_provider: Optional[str] = None
    session_id: str = ""
    message_id: str = ""


@dataclass
class ToolResult:
    """工具执行结果"""

    tool_name: str
    success: bool
    summary: str = ""  # 供拼接进对话上下文的自然语言描述
    text: str = ""  # 识别/提取出的原始文本或摘要
    chunk_count: int = 0
    doc_ids: List[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    error: str = ""


class BaseTool:
    """工具基类"""

    name: str = "base_tool"
    description: str = ""

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        raise NotImplementedError


_REGISTRY: dict[str, BaseTool] = {}


def register_tool(tool: BaseTool) -> BaseTool:
    _REGISTRY[tool.name] = tool
    return tool


def get_tool(name: str) -> Optional[BaseTool]:
    return _REGISTRY.get(name)


def list_tools() -> List[str]:
    return list(_REGISTRY.keys())
