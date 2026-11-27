"""
见字如面 - 上下文回读工具（L0 Tool-Result Offloading 的配套能力）

超大工具结果落盘后，上下文只保留约 2KB 预览 + 相对路径；模型需要完整正文时
通过本工具按 id 分页回读。对齐 Claude Code 的「offload + 按需重读」设计：

  - 只能读当前会话且属于本人的落盘结果（会话隔离 + 属主校验 + 路径穿越校验）；
  - 分页读取，单次不超过 TOOL_RESULT_READ_PAGE_TOKENS（默认 4K）；
  - 只读工具（risk_level=low），自身结果标记为可重现，可被 L1 微压缩安全清除。

注册为工具注册表一等公民，使 ReAct / Plan&Execute 之外的原生 Function Calling
链路也能在工具目录中看到并调用它。
"""
import logging

from app.services.tools.base import BaseTool, ToolContext, ToolResult, register_tool

logger = logging.getLogger("jianziruyang.tools.context_read")


class ReadToolResultTool(BaseTool):
    name = "read_tool_result"
    description = (
        "回读本会话已落盘或已被微压缩清除的工具结果原文。当上下文中的工具结果只显示"
        "预览/引用/占位符，而回答需要完整正文（全文、详细数据、逐条内容）时使用。"
        "id 取上下文中 [工具结果 #id] 的编号或落盘回执路径中的文件名；支持 offset 分页续读。"
    )
    namespace = "context"
    tags = ("readonly", "context", "offload", "reproducible")
    scenarios = (
        "上下文中的工具结果被落盘/清除，只留预览或占位符",
        "用户要求给出附件/检索结果的完整正文或逐条明细",
        "需要续读上一次未读完的落盘结果（offset 分页）",
    )
    parameters = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "工具结果编号（[工具结果 #id]）或落盘文件名"},
            "offset": {"type": "integer", "description": "起始字符偏移（续读用，默认 0）", "default": 0},
            "limit": {"type": "integer", "description": "本次读取字符数上限（默认按页上限）", "default": 0},
        },
        "required": ["id"],
    }
    permissions = ()
    risk_level = "low"
    status = "active"
    requires_confirmation = False

    async def run(self, ctx: ToolContext, id: str = "", offset: int = 0, limit: int = 0, **kwargs) -> ToolResult:
        from app.services import context_window as cw

        rid = (id or "").strip()
        # 兼容模型直接传落盘路径：取末段文件名并去掉 .txt 后缀
        if "/" in rid:
            rid = rid.rsplit("/", 1)[-1]
        if rid.endswith(".txt"):
            rid = rid[:-4]
        if not rid:
            return ToolResult(tool_name=self.name, success=False,
                              summary="缺少工具结果编号 id。", error="empty_id")
        if not ctx.session_id:
            return ToolResult(tool_name=self.name, success=False,
                              summary="当前调用没有会话上下文，无法回读。", error="no_session")
        text = cw.read_tool_result(ctx.session_id, ctx.user_id, rid,
                                   offset=int(offset or 0), limit=int(limit or 0))
        ok = not text.startswith(("未找到", "无权", "落盘文件不可用"))
        return ToolResult(
            tool_name=self.name, success=ok,
            summary="已回读工具结果原文" if ok else text,
            text=text,
            extra={"result_id": rid, "offset": int(offset or 0),
                   "execution_status": "completed" if ok else "failed",
                   "validation_status": "passed" if ok else "failed",
                   "requirement_status": "satisfied" if ok else "unmet"},
            error="" if ok else "read_unavailable",
        )


register_tool(ReadToolResultTool())