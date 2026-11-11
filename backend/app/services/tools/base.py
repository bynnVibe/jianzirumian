"""
见字如面 - 工具调用框架基础定义（标准化注册表）

参考 function calling 的 schema 设计，为每个工具补齐标准化注册元数据：
  - 命名空间 namespace：按能力域分组（retrieval / attachment / knowledge / system / cli）
  - 标签 tags：细粒度能力标记，供路由与检索
  - 适用场景 scenarios：自然语言描述「何时应选用该工具」，供路由决策与 LLM 选择
  - 参数 Schema parameters：JSON Schema（draft-07 子集），与 function calling 的 parameters 对应
  - 权限 permissions：调用所需角色/权限（空=登录即可）
  - 风险等级 risk_level：low/medium/high/critical，供调用前钩子做风险闸
  - 可用状态 status：active/disabled/deprecated

ToolContext: 单次工具执行所需的上下文（附件路径、用户、目标知识库等）。
ToolResult: 工具执行结果的统一结构，summary 字段用于拼接进 LLM 对话上下文。
BaseTool: 所有工具的基类；register_tool/get_tool 提供进程内注册表。

注册表在原有 name→tool 映射之上，补充 function calling schema 导出与
按命名空间/标签/场景的查询能力，作为 Tool Routing、调用前约束钩子、
CLI 封装与可观测性的统一元数据来源。
"""
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# 风险等级：low(只读/无副作用) < medium(写用户数据) < high(写共享/外部副作用) < critical(系统命令/不可逆)
RISK_LEVELS: Tuple[str, ...] = ("low", "medium", "high", "critical")
# 可用状态：active(可用) / disabled(停用) / deprecated(弃用)
TOOL_STATUS: Tuple[str, ...] = ("active", "disabled", "deprecated")
# high/critical 风险默认需要人工确认（HITL）
_CONFIRM_RISK = ("high", "critical")


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
    action_id: str = ""
    # 调用方角色（供权限钩子校验；空=未知，按最严格处理）
    role: str = ""
    # 本次调用的追踪标识（供 Tool Routing 可观测性串联调用链）
    trace_id: str = ""


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
    """工具基类（携带标准化注册元数据，参考 function calling schema 设计）"""

    # ---- 基本标识 ----
    name: str = "base_tool"
    description: str = ""
    # ---- 命名空间：按能力域分组 ----
    namespace: str = "general"
    # ---- 标签：细粒度能力标记 ----
    tags: Tuple[str, ...] = ()
    # ---- 适用场景：何时应选用该工具（供路由决策与 LLM 选择）----
    scenarios: Tuple[str, ...] = ()
    # ---- 参数 Schema：JSON Schema，与 function calling 的 parameters 对应 ----
    parameters: Dict[str, Any] = {}
    # ---- 权限：调用所需角色/权限（空=登录即可）----
    permissions: Tuple[str, ...] = ()
    # ---- 风险等级：low/medium/high/critical ----
    risk_level: str = "low"
    # ---- 可用状态：active/disabled/deprecated ----
    status: str = "active"
    # ---- 是否需要人工确认（HITL）：未显式设置时 high/critical 默认需要 ----
    requires_confirmation: Optional[bool] = None

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        raise NotImplementedError

    # ---- function calling schema 导出 ----
    def to_function_schema(self) -> dict:
        """导出 OpenAI function-calling 兼容的 schema（type/function/name/description/parameters）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }

    def needs_confirmation(self) -> bool:
        """该工具调用是否需要人工确认（HITL）。"""
        if self.requires_confirmation is not None:
            return bool(self.requires_confirmation)
        return self.risk_level in _CONFIRM_RISK

    def to_spec_dict(self) -> dict:
        """导出完整注册元数据（供注册表 API / Tool Routing 可视化）。"""
        return {
            "name": self.name,
            "description": self.description,
            "namespace": self.namespace,
            "tags": list(self.tags),
            "scenarios": list(self.scenarios),
            "parameters": self.parameters or {"type": "object", "properties": {}},
            "permissions": list(self.permissions),
            "risk_level": self.risk_level,
            "status": self.status,
            "requires_confirmation": self.needs_confirmation(),
        }


_REGISTRY: Dict[str, BaseTool] = {}


def register_tool(tool: BaseTool) -> BaseTool:
    """注册工具到进程内注册表（同名覆盖，返回工具本身便于链式赋值）。"""
    _REGISTRY[tool.name] = tool
    return tool


def get_tool(name: str) -> Optional[BaseTool]:
    return _REGISTRY.get(name)


def list_tools() -> List[str]:
    return list(_REGISTRY.keys())


def all_tools() -> List[BaseTool]:
    """返回全部已注册工具（含 disabled/deprecated）。"""
    return list(_REGISTRY.values())


def active_tools(namespaces: Optional[Tuple[str, ...]] = None) -> List[BaseTool]:
    """返回可用状态为 active 的工具（可按命名空间过滤）。"""
    return [
        t for t in _REGISTRY.values()
        if t.status == "active" and (not namespaces or t.namespace in namespaces)
    ]


def find_by_tag(tag: str) -> List[BaseTool]:
    """按标签精确匹配工具。"""
    return [t for t in _REGISTRY.values() if tag in (t.tags or ())]


def find_by_scenario(keyword: str) -> List[BaseTool]:
    """按场景/标签关键词模糊匹配工具（供场景感知路由）。"""
    kw = (keyword or "").strip().lower()
    if not kw:
        return []
    hits = []
    for t in _REGISTRY.values():
        blob = " ".join(list(t.scenarios or ()) + list(t.tags or ())).lower()
        if kw in blob:
            hits.append(t)
    return hits


def function_schemas(names: Optional[List[str]] = None) -> List[dict]:
    """导出 function calling schema 列表（可指定工具名子集，仅含 active 工具）。"""
    if names is None:
        tools = list(_REGISTRY.values())
    else:
        tools = [_REGISTRY[n] for n in names if n in _REGISTRY]
    return [t.to_function_schema() for t in tools if t.status == "active"]


def registry_snapshot() -> List[dict]:
    """完整注册表快照（按命名空间+名称排序，供 API / 可视化）。"""
    return sorted(
        (t.to_spec_dict() for t in _REGISTRY.values()),
        key=lambda d: (d["namespace"], d["name"]),
    )


def schema_token_estimate(names: Optional[List[str]] = None) -> int:
    """估算工具 schema 注入 LLM 的 token 占用（供可观测性「工具 Schema Token」指标）。

    采用与 observability.estimate_tokens 同口径的轻量估算（CJK 1:1，其余 4 字符/token），
    在此内联实现以避免 base ↔ observability 的循环依赖。
    """
    import math

    text = json.dumps(function_schemas(names), ensure_ascii=False)
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" or "\uff00" <= ch <= "\uffef")
    return cjk + math.ceil((len(text) - cjk) / 4)
