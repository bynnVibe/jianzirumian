"""
见字如面 - 文档（Word/PDF）导入工具（解析全文 + 分割 + 向量入库）

对应聊天框场景：用户拖入 Word/PDF 并要求"上传/保存/加入知识库"，
复用 knowledge_service.process_document()，与 /api/knowledge/upload/document 一致的入库逻辑。
"""
from app.services.knowledge import knowledge_service
from app.services.records import upload_records
from app.services.tools.base import BaseTool, ToolContext, ToolResult, register_tool


class DocumentImportTool(BaseTool):
    name = "document_import"
    description = "解析 Word/PDF 文档内容并存入知识库"

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        doc_type = ctx.kind
        if doc_type not in ("word", "pdf"):
            return ToolResult(
                tool_name=self.name, success=False,
                summary=f"不支持的文档类型: {doc_type}", error="unsupported_type",
            )

        try:
            result = await knowledge_service.process_document(
                file_path=ctx.file_path,
                doc_type=doc_type,
                visibility=ctx.visibility,
                owner_id=ctx.user_id,
                kb_id=ctx.kb_id,
            )
        except Exception as e:
            return ToolResult(
                tool_name=self.name, success=False,
                summary=f"文档「{ctx.filename}」入库失败：{e}", error=str(e),
            )

        if result["chunk_count"] <= 0:
            return ToolResult(
                tool_name=self.name, success=False, text=result["text"],
                summary=f"文档「{ctx.filename}」未解析出有效文字，未存入知识库。", error="empty_text",
            )

        upload_records.add_record(
            filename=ctx.filename,
            image_path=ctx.file_path,
            ocr_text=result["text"],
            ocr_provider=f"document:{doc_type}",
            chunk_count=result["chunk_count"],
            doc_ids=result["doc_ids"],
            visibility=ctx.visibility,
            owner_id=ctx.user_id,
            source_type=doc_type,
        )
        from app.services.usage import record_upload
        record_upload(ctx.user_id)

        preview = result["text"][:800]
        return ToolResult(
            tool_name=self.name, success=True, text=result["text"],
            chunk_count=result["chunk_count"], doc_ids=result["doc_ids"],
            summary=(
                f"已解析文档「{ctx.filename}」并存入知识库（{result['chunk_count']} 个知识片段）。"
                f"内容摘录：\n{preview}"
            ),
        )


document_import_tool = register_tool(DocumentImportTool())
