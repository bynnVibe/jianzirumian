"""
见字如面 - 聊天框工具调用意图路由

根据用户文本与附件类型，决定调用哪个工具，并将执行结果落库到 tool_calls 表：
  - 附件 + "上传/保存/入库" 等关键词 → image_upload / document_import（写入知识库）
  - 附件 + "总结/提炼/提取重点" 等关键词 → knowledge_summarize（默认不入库）
  - 其余情况（含无法判断意图时的默认动作）→ 仅识别/提取内容，不自动写入知识库，
    避免在用户未明确表达入库意图时误写公共/个人知识库。
"""
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from app.core import db
from app.services.tools import base as tools_base
from app.services.tools.document_import import document_import_tool
from app.services.tools.image_ocr import image_ocr_tool
from app.services.tools.image_upload import image_upload_tool
from app.services.tools.knowledge_summarize import knowledge_summarize_tool

logger = logging.getLogger("jianziruyang.tool_orchestrator")

_UPLOAD_KEYWORDS = ("上传", "保存", "入库", "加入知识库", "存进知识库", "存到知识库", "收藏一下", "收藏")
_SUMMARIZE_KEYWORDS = ("总结", "提炼", "概括", "提取重点", "归纳", "摘要")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}
DOC_EXTS = {".docx": "word", ".pdf": "pdf"}


def detect_kind(filename: str) -> Optional[str]:
    """按扩展名判断附件类型：image | word | pdf | None（不支持）"""
    ext = Path(filename or "").suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    return DOC_EXTS.get(ext)


def _decide_action(query: str) -> str:
    """根据用户文本判断动作：upload | summarize | ocr（安全默认，不入库）"""
    text = query or ""
    if any(k in text for k in _UPLOAD_KEYWORDS):
        return "upload"
    if any(k in text for k in _SUMMARIZE_KEYWORDS):
        return "summarize"
    return "ocr"


def _record_tool_call(session_id: str, message_id: str, user_id: str, tool_name: str,
                       input_data: dict, output_data: dict, status: str) -> None:
    try:
        db.execute(
            "INSERT INTO tool_calls (id, session_id, message_id, user_id, tool_name,"
            " input_json, output_json, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()), session_id, message_id, user_id, tool_name,
                json.dumps(input_data, ensure_ascii=False),
                json.dumps(output_data, ensure_ascii=False),
                status, datetime.now().isoformat(),
            ),
        )
    except Exception as e:
        logger.warning("记录工具调用失败（忽略，不影响对话）: %s", e)


async def handle_attachments(
    query: str,
    attachments: List[dict],
    user_id: str,
    kb_id: str,
    session_id: str,
    message_id: str,
) -> Tuple[str, List[dict]]:
    """处理聊天框附件，返回 (拼接进 LLM 对话上下文的工具结果文本, 工具调用记录列表)

    attachments: [{"path": str, "filename": str, "kind": "image"|"word"|"pdf"|None}, ...]
    """
    if not attachments:
        return "", []

    action = _decide_action(query)
    if user_id == "guest" or not user_id:
        # 游客不允许写入知识库，与 /api/knowledge/upload 需登录保持一致，降级为仅识别/提取
        action = "ocr"
    summaries: List[str] = []
    records: List[dict] = []

    for att in attachments:
        filename = att.get("filename", "")
        file_path = att.get("path", "")
        kind = att.get("kind") or detect_kind(filename)
        if not kind or not file_path:
            summaries.append(f"附件「{filename}」的类型暂不支持处理。")
            continue

        ctx = tools_base.ToolContext(
            user_id=user_id, file_path=file_path, filename=filename, kind=kind,
            kb_id=kb_id, visibility="private", session_id=session_id, message_id=message_id,
        )

        if action == "upload":
            tool = image_upload_tool if kind == "image" else document_import_tool
            kwargs = {}
        elif action == "summarize":
            tool = knowledge_summarize_tool
            kwargs = {"save_to_kb": False}
        else:
            # 默认动作：图片仅 OCR；文档没有独立的"仅解析不入库"工具，
            # 复用总结工具但不写入知识库，避免长文档原文直接灌入对话。
            tool = image_ocr_tool if kind == "image" else knowledge_summarize_tool
            kwargs = {} if kind == "image" else {"save_to_kb": False}

        try:
            result = await tool.run(ctx, **kwargs)
            status = "success" if result.success else "failed"
        except Exception as e:
            logger.error("工具 %s 执行异常: %s", tool.name, e)
            result = tools_base.ToolResult(
                tool_name=tool.name, success=False,
                summary=f"处理「{filename}」时出错：{e}", error=str(e),
            )
            status = "error"

        summaries.append(result.summary)
        record = {
            "tool_name": tool.name,
            "filename": filename,
            "success": result.success,
            "chunk_count": result.chunk_count,
            "doc_ids": result.doc_ids,
        }
        records.append(record)
        _record_tool_call(
            session_id, message_id, user_id, tool.name,
            {"filename": filename, "kind": kind, "action": action},
            record, status,
        )

    context_text = "【聊天框附件处理结果】\n" + "\n\n".join(summaries) if summaries else ""
    return context_text, records
