"""
见字如面 - 系统提示词版本库（Prompt「上线」落地）

chat 主流程的系统提示词正文（不含 FINAL_ANSWER_RULES 固定尾规）由本模块托管：
- 无已发布版本时回退到内置默认正文 DEFAULT_BODY；
- 存在 status='published' 版本时，chat 采用该版本正文（Prompt/Skill 评测的「上线」即发布新版本）；
- 同一时刻至多一个 published：发布新版本时把旧 published 归档为 archived，保留完整版本历史。

设计要点：只读回退绝不影响主流程；发布为原子操作（先归档再置新），失败静默返回错误信息。
"""
import logging
import uuid
from datetime import datetime
from typing import List, Optional

from app.core import db

logger = logging.getLogger("jianziruyang.prompt_store")

# 内置默认系统提示词正文（与 chat._build_system_prompt 原始硬编码一致，不含 FINAL_ANSWER_RULES）
DEFAULT_BODY = (
    "你是「见字如面」智能助手，一个专注于健康养生知识库的问答系统。\n\n"
    "你的能力：\n"
    "1. 基于知识库内容回答用户关于健康、养生、中医等方面的问题\n"
    "2. 引用知识库原文和原始图片来源\n"
    "3. 结合联网搜索结果提供更全面的信息\n\n"
    "行为准则：\n"
    "- 回答要准确、专业、有据可查\n"
    "- 不确定的内容不要编造，明确告知用户\n"
    "- 引用来源时，清晰标注原文出处\n"
    "- 以温暖、关怀的语气与用户交流\n"
)


def _now() -> str:
    return datetime.now().isoformat()


def _row_to_dict(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "content": row["content"],
        "status": row["status"],
        "note": row["note"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_active_version() -> Optional[dict]:
    """当前已发布（生效）的系统提示词版本；无则 None。"""
    row = db.query_one(
        "SELECT * FROM prompt_versions WHERE status = 'published'"
        " ORDER BY updated_at DESC LIMIT 1"
    )
    return _row_to_dict(row) if row else None


def get_active_body() -> str:
    """chat 主流程采用的系统提示词正文：已发布版本内容，否则内置默认。"""
    ver = get_active_version()
    return ver["content"] if ver and (ver.get("content") or "").strip() else DEFAULT_BODY


def list_versions(limit: int = 50) -> List[dict]:
    rows = db.query(
        "SELECT * FROM prompt_versions ORDER BY"
        " CASE status WHEN 'published' THEN 0 ELSE 1 END, updated_at DESC LIMIT ?",
        (limit,),
    )
    return [_row_to_dict(r) for r in rows]


def publish(content: str, name: str = "", created_by: str = "", note: str = "") -> dict:
    """发布一个系统提示词新版本：归档旧 published，置新版本为 published。

    返回 {'version': {...}} 或 {'error': '...'}。content 为空时拒绝。
    """
    content = (content or "").strip()
    if not content:
        return {"error": "提示词正文不能为空"}
    now = _now()
    try:
        db.execute("UPDATE prompt_versions SET status = 'archived', updated_at = ?"
                   " WHERE status = 'published'", (now,))
        vid = str(uuid.uuid4())
        db.execute(
            "INSERT INTO prompt_versions (id, name, content, status, note, created_by,"
            " created_at, updated_at) VALUES (?,?,?,'published',?,?,?,?)",
            (vid, (name or "").strip()[:60], content, (note or "").strip()[:200],
             created_by or "", now, now),
        )
        row = db.query_one("SELECT * FROM prompt_versions WHERE id = ?", (vid,))
        logger.info("系统提示词新版本已上线: %s（by=%s）", vid, created_by or "-")
        return {"version": _row_to_dict(row)}
    except Exception as e:  # 发布失败不影响主流程
        logger.warning("系统提示词发布失败: %s", e)
        return {"error": f"发布失败：{str(e)[:200]}"}
