"""
见字如面 - 聊天框工具调用意图路由

根据用户文本与附件类型，决定调用哪个工具，并将执行结果落库到 tool_calls 表：
  - 附件 + "上传/保存/入库" 等关键词 → image_upload / document_import（写入知识库）
  - 附件 + "总结/提炼/提取重点" 等关键词 → knowledge_summarize（默认不入库）
  - 其余情况（含无法判断意图时的默认动作）→ 仅识别/提取内容，不自动写入知识库，
    避免在用户未明确表达入库意图时误写公共/个人知识库。

图片识别文本除注入对话上下文外，还通过 build_ocr_retrieval_query 并入知识检索查询，
支持「粘贴截图即检索」：用户只贴图不打字时，以图中文字作为检索问题。
"""
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from app.config import settings
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
DOC_EXTS = {".docx": "word", ".pdf": "pdf", ".md": "markdown", ".markdown": "markdown"}


def detect_kind(filename: str) -> Optional[str]:
    """按扩展名判断附件类型：image | word | pdf | markdown | None（不支持）"""
    ext = Path(filename or "").suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    return DOC_EXTS.get(ext)


def _decide_action(query: str) -> str:
    """根据用户文本判断动作：upload | summarize | ocr（安全默认，不入库）"""
    text = query or ""
    conservative = bool(re.search(r"不要|不用|不必|别|禁止|无需|不想|不需要|不保存|不上传|不入库|是否|能否|可以吗|怎么|如何|假如|如果|或者|还是|[\"'“”‘’「」『』`？?]", text))
    if not conservative and any(k in text for k in _UPLOAD_KEYWORDS):
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


def _clip_budget(text: str, limit: int) -> str:
    """按字符预算截断单段文本，超限时保留首部并标注被截断。"""
    text = text or ""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"\n…（内容过长，已截断，原长 {len(text)} 字符，仅保留前 {limit} 字符）"


def _apply_context_budget(summaries: List[str]) -> Tuple[List[str], bool]:
    """对附件工具结果施加两级字符预算：先逐条截断，再控制总量。

    附件（OCR/文档解析原文）是单次请求 Token 尖峰的最大来源。返回 (截断后 summaries, 是否发生截断)。
    """
    per_item = getattr(settings, "ATTACHMENT_CTX_PER_ITEM_CHARS", 1500)
    total = getattr(settings, "ATTACHMENT_CTX_TOTAL_CHARS", 4000)
    clipped = [s for s in summaries]
    truncated = False
    for i, s in enumerate(clipped):
        new = _clip_budget(s, per_item)
        if new != s:
            truncated = True
            clipped[i] = new
    # 总量预算：从前往后累加，超出的附件以占位提示替代，避免总量失控。
    if total > 0:
        acc = 0
        for i, s in enumerate(clipped):
            if acc + len(s) > total:
                remain = max(0, total - acc)
                clipped[i] = _clip_budget(s, remain) if remain > 80 else "（因附件上下文总量预算已满，本附件内容已省略）"
                truncated = True
                # 其后附件全部折叠为占位，避免总量失控
                for j in range(i + 1, len(clipped)):
                    clipped[j] = "（因附件上下文总量预算已满，本附件内容已省略）"
                break
            acc += len(s)
    return clipped, truncated


def _ocr_excerpt(records: List[dict], limit: int) -> str:
    """从附件工具记录中提取图片 OCR 文本，压缩为检索用摘录。

    只取「图片 + 执行成功 + 非入库提案」的记录；多张图片按附件顺序拼接去重。
    检索查询过长会稀释向量/BM25 信号，故按字符预算截断（完整原文仍在对话上下文中）。
    """
    if limit <= 0:
        limit = 300
    texts: List[str] = []
    for rec in records:
        if rec.get("confirmation") or not rec.get("success"):
            continue
        if rec.get("kind") != "image":
            continue
        text = (rec.get("text") or "").strip()
        if text and text not in texts:
            texts.append(text)
    return "\n".join(texts)[:limit]


def build_ocr_retrieval_query(query: str, records: List[dict]) -> Tuple[str, str]:
    """截图检索：把图片附件的 OCR 文本并入知识检索查询。

    返回 (检索查询, OCR 摘录)；摘录为空表示本次无可用识别文本，检索查询原样返回
    用户输入（行为与未贴图时完全一致）。
      - 用户有文本：文本在前、识别内容在后，检索兼顾「用户想问什么」与「图里写了什么」；
      - 用户仅粘贴截图（文本为空）：直接以识别内容作为检索查询，实现「截图即问」。
    """
    excerpt = _ocr_excerpt(records, getattr(settings, "OCR_RETRIEVAL_CHARS", 300))
    text_query = (query or "").strip()
    if not excerpt:
        return text_query or (query or ""), ""
    if not text_query:
        return excerpt, excerpt
    return f"{text_query}\n{excerpt}", excerpt


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
            from app.services.chat_actions import propose
            proposal = propose({**att, "kind": kind}, user_id, session_id, message_id)
            records.append({"confirmation": proposal})
            summaries.append(f"附件「{filename}」仅生成入库提案，尚未执行。请用户在确认卡片核对目标并点击确认；不得声称已入库。")
            continue
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
            # 附件类型 + 识别/解析原文：供上层做「截图检索」（OCR 文本并入检索查询）
            "kind": kind,
            "text": result.text or "",
        }
        records.append(record)
        # 落库只保留原文摘要，避免 tool_calls 表随附件原文膨胀
        _record_tool_call(
            session_id, message_id, user_id, tool.name,
            {"filename": filename, "kind": kind, "action": action},
            {**record, "text": _clip_budget(record["text"], 500)}, status,
        )

    clipped, truncated = _apply_context_budget(summaries)
    header = "【聊天框附件处理结果】"
    if truncated:
        header += "（部分内容因上下文预算被截断，如需完整内容请单独查看附件）"
    context_text = header + "\n" + "\n\n".join(clipped) if clipped else ""
    return context_text, records
