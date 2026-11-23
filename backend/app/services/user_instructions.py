"""
见字如面 - 用户级指令服务（五层记忆架构 L1，类比 Claude Code CLAUDE.md 指令层）

分层指令与优先级（对齐 Claude Code Managed < User 的 recency bias 设计）：
- Managed 层：prompt_store 托管的系统提示词（管理员发布版本），最先注入、优先级最低；
- User 层：本服务维护的用户个人指令（每用户一条），注入位置靠后、优先级更高。

指令是「人类预写的静态规则」，与 L2 Auto Memory（AI 自动抽取）正交：
用户可随时编辑，敏感内容（密码/密钥/证件等）复用记忆层同源拦截不落库。
"""
import logging
import uuid
from datetime import datetime
from typing import Optional

from app.core import db

logger = logging.getLogger("jianziruyang.user_instructions")

# 用户指令字符上限（注入预算：与画像卡合计增量受控，计入 segment 度量）
MAX_INSTRUCTION_CHARS = 500


def get_instruction(user_id: str) -> str:
    """读取当前用户的个人指令正文（无则空串）。游客/系统不注入。"""
    if not user_id or user_id in {"guest", "system", "-"}:
        return ""
    row = db.query_one(
        "SELECT content FROM user_instructions WHERE user_id = ?", (user_id,)
    )
    return (row.get("content") or "") if row else ""


def set_instruction(user_id: str, content: str) -> Optional[dict]:
    """写入/更新用户指令（upsert）。敏感内容拒绝；超长按上限截断。

    返回 {content, updated_at}；敏感命中返回 None（由 API 层转 400）。
    """
    from app.services.memory import _is_sensitive

    if not user_id or user_id in {"guest", "system", "-"}:
        return None
    content = (content or "").strip()
    if content and _is_sensitive(content):
        return None
    content = content[:MAX_INSTRUCTION_CHARS]
    now = datetime.now().isoformat()
    db.execute(
        "INSERT INTO user_instructions (id, user_id, content, updated_at)"
        " VALUES (?,?,?,?)"
        " ON CONFLICT(user_id) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at",
        (str(uuid.uuid4()), user_id, content, now),
    )
    # 双写：User 层 markdown 镜像（USER.md 为持久记录，DB 为查询镜像）
    from app.services import memory_store

    memory_store.sync_instruction(user_id, content)
    memory_store.log_memory_event(user_id, "update" if content else "delete", "user")
    return {"content": content, "updated_at": now}


def format_instruction_messages(text: str) -> list:
    """把指令正文渲染为 system 消息（纯格式化，单一事实源；chat 与 chat_agent 共用）。"""
    if not (text or "").strip():
        return []
    return [{"role": "system",
             "content": f"【用户个人指令（优先级高于系统默认约定，回答时须遵循）】\n{text.strip()}"}]


def instruction_messages(user_id: str) -> list:
    """构建 L1 用户指令 system 消息（U 区半静态：同用户跨会话稳定，前缀缓存友好）。

    注入位置约定：静态系统头（Managed）之后、画像卡之前——靠后注入优先级更高。
    """
    return format_instruction_messages(get_instruction(user_id))