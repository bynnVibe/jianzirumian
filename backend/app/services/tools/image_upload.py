"""
见字如面 - 图片入库工具（OCR 识别 + 文本分割 + 向量入库）

对应聊天框场景：用户拖入图片并要求"上传/保存/加入知识库"，
复用 knowledge_service.process_image()，与 /api/knowledge/upload 一致的入库逻辑。
"""
from app.config import settings
from app.services.knowledge import knowledge_service
from app.services.records import upload_records
from app.services.tools.base import BaseTool, ToolContext, ToolResult, register_tool


class ImageUploadTool(BaseTool):
    name = "image_upload"
    description = "识别图片中的文字并存入知识库"

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        try:
            result = await knowledge_service.process_image(
                image_path=ctx.file_path,
                ocr_provider=ctx.ocr_provider,
                visibility=ctx.visibility,
                owner_id=ctx.user_id,
                kb_id=ctx.kb_id,
            )
        except Exception as e:
            return ToolResult(
                tool_name=self.name, success=False,
                summary=f"图片「{ctx.filename}」入库失败：{e}", error=str(e),
            )

        if result["chunk_count"] <= 0:
            return ToolResult(
                tool_name=self.name, success=False, text=result["ocr_text"],
                summary=f"图片「{ctx.filename}」未识别出有效文字，未存入知识库。", error="empty_text",
            )

        upload_records.add_record(
            filename=ctx.filename,
            image_path=ctx.file_path,
            ocr_text=result["ocr_text"],
            ocr_provider=ctx.ocr_provider or settings.OCR_PROVIDER,
            chunk_count=result["chunk_count"],
            doc_ids=result["doc_ids"],
            visibility=ctx.visibility,
            owner_id=ctx.user_id,
            source_type="image",
        )
        from app.services.usage import record_upload
        record_upload(ctx.user_id)

        return ToolResult(
            tool_name=self.name, success=True, text=result["ocr_text"],
            chunk_count=result["chunk_count"], doc_ids=result["doc_ids"],
            summary=(
                f"已识别图片「{ctx.filename}」并存入知识库（{result['chunk_count']} 个知识片段）。"
                f"识别文字：\n{result['ocr_text']}"
            ),
        )


image_upload_tool = register_tool(ImageUploadTool())
