"""
见字如面 - 知识提取总结工具

对 OCR/文档提取的文本调用 LLM 生成结构化摘要，默认不入库；
仅当明确要求（save_to_kb=True）时才将摘要写入知识库。
"""
import logging

from app.services.knowledge import knowledge_service
from app.services.records import upload_records
from app.services.tools.base import BaseTool, ToolContext, ToolResult, register_tool

logger = logging.getLogger("jianziruyang.tools.knowledge_summarize")


class KnowledgeSummarizeTool(BaseTool):
    name = "knowledge_summarize"
    description = "识别/解析附件内容后，用 LLM 总结要点（默认不入库）"

    async def _extract_text(self, ctx: ToolContext) -> str:
        if ctx.kind == "image":
            return await knowledge_service.preview_ocr(ctx.file_path, ctx.ocr_provider)
        preview = await knowledge_service.preview_document(ctx.file_path, ctx.kind)
        return "\n\n".join(p.get("text", "") for p in preview.get("pages", []))

    async def run(self, ctx: ToolContext, save_to_kb: bool = False, **kwargs) -> ToolResult:
        try:
            raw_text = await self._extract_text(ctx)
        except Exception as e:
            return ToolResult(
                tool_name=self.name, success=False,
                summary=f"提取「{ctx.filename}」内容失败：{e}", error=str(e),
            )

        if not raw_text.strip():
            return ToolResult(
                tool_name=self.name, success=False,
                summary=f"未能从「{ctx.filename}」中提取出有效内容。", error="empty_text",
            )

        try:
            from app.core.llm import LLMFactory

            llm = LLMFactory.create()
            prompt = (
                "请对以下笔记内容提炼知识点总结，突出重点、结构清晰，200~400字：\n\n"
                f"{raw_text[:4000]}"
            )
            collected = ""
            async for chunk in llm.chat([{"role": "user", "content": prompt}], stream=False):
                collected += chunk
            summary_text = collected.strip() or raw_text[:400]
        except Exception as e:
            logger.warning("总结生成失败，降级为原文截取: %s", e)
            summary_text = raw_text[:400]

        chunk_count = 0
        doc_ids: list[str] = []
        if save_to_kb:
            try:
                import asyncio

                from app.core.vector_store import vector_store

                # 分割 + Embedding 向量化为同步重计算，移入线程池避免阻塞事件循环
                def _vectorize():
                    chunks = vector_store.split_text(
                        summary_text, ctx.file_path, ctx.visibility, ctx.user_id, kb_id=ctx.kb_id,
                    )
                    if not chunks:
                        return 0, []
                    texts = [c[0] for c in chunks]
                    metadatas = [c[1] for c in chunks]
                    return len(chunks), vector_store.add_texts(texts, metadatas)

                chunk_count, doc_ids = await asyncio.to_thread(_vectorize)
                if chunk_count:
                    upload_records.add_record(
                        filename=f"{ctx.filename}（总结）",
                        image_path=ctx.file_path,
                        ocr_text=summary_text,
                        ocr_provider="llm_summary",
                        chunk_count=chunk_count,
                        doc_ids=doc_ids,
                        visibility=ctx.visibility,
                        owner_id=ctx.user_id,
                        source_type=ctx.kind,
                    )
            except Exception as e:
                logger.warning("总结入库失败（忽略，摘要仍返回给用户）: %s", e)

        summary_line = (
            f"已为「{ctx.filename}」生成知识总结"
            + ("并存入知识库" if chunk_count else "（未存入知识库）")
            + f"：\n{summary_text}"
        )
        return ToolResult(
            tool_name=self.name, success=True, text=summary_text,
            chunk_count=chunk_count, doc_ids=doc_ids, summary=summary_line,
        )


knowledge_summarize_tool = register_tool(KnowledgeSummarizeTool())
