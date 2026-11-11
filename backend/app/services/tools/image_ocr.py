"""
见字如面 - 图片 OCR 识别工具（仅识别，不入库）

对应聊天框场景：用户拖入图片并问"识别一下/看看写了什么"，
只返回识别文本作为对话上下文，不写入知识库。
"""
from app.services.knowledge import knowledge_service
from app.services.tools.base import BaseTool, ToolContext, ToolResult, register_tool


class ImageOCRTool(BaseTool):
    name = "image_ocr"
    description = "识别图片中的文字内容，不写入知识库"
    namespace = "attachment"
    tags = ("ocr", "image", "readonly", "extract")
    scenarios = (
        "用户拖入图片要求「识别一下/看看写了什么」",
        "粘贴截图即问（图中文字并入检索）",
        "仅提取图片文字而不入库",
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "待识别的图片文件路径"},
            "filename": {"type": "string", "description": "图片文件名"},
        },
        "required": ["file_path"],
    }
    permissions = ()
    risk_level = "low"
    status = "active"

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        try:
            text = await knowledge_service.preview_ocr(ctx.file_path, ctx.ocr_provider)
        except Exception as e:
            return ToolResult(
                tool_name=self.name, success=False,
                summary=f"识别图片「{ctx.filename}」失败：{e}", error=str(e),
            )
        if not text.strip():
            return ToolResult(
                tool_name=self.name, success=False,
                summary=f"未能从「{ctx.filename}」中识别出文字内容。", error="empty_text",
            )
        return ToolResult(
            tool_name=self.name, success=True, text=text,
            summary=f"已识别图片「{ctx.filename}」中的文字内容（未存入知识库）：\n{text}",
        )


image_ocr_tool = register_tool(ImageOCRTool())
