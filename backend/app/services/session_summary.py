"""
见字如面 - 会话历史摘要压缩

当会话消息数量/字符数超过阈值时，用 LLM 将较早的消息压缩为摘要，
后续对话仅携带「摘要 + 最近 N 条消息」，避免上下文无限增长。
失败时静默降级为原有的按条数截断策略，不阻断聊天主流程。
"""
import logging
from datetime import datetime
from typing import List, Optional

from app.core import db

logger = logging.getLogger("jianziruyang.session_summary")

# 触发压缩的阈值：消息数量或历史总字符数任一超过即触发
SUMMARY_TRIGGER_MESSAGE_COUNT = 20
SUMMARY_TRIGGER_CHAR_COUNT = 12000
# 压缩后仍保留最近 N 条消息不进摘要
KEEP_RECENT_MESSAGES = 6


def get_summary(session_id: str) -> Optional[dict]:
    return db.query_one(
        "SELECT * FROM chat_session_summaries WHERE session_id = ?", (session_id,)
    )


def _save_summary(session_id: str, user_id: str, summary: str, covered_until_message_id: str, token_estimate: int):
    now = datetime.now().isoformat()
    db.execute(
        "INSERT INTO chat_session_summaries"
        " (session_id, user_id, summary, covered_until_message_id, token_estimate, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(session_id) DO UPDATE SET"
        " summary=excluded.summary, covered_until_message_id=excluded.covered_until_message_id,"
        " token_estimate=excluded.token_estimate, updated_at=excluded.updated_at",
        (session_id, user_id, summary, covered_until_message_id, token_estimate, now, now),
    )


async def maybe_summarize(session, llm_provider: str = None):
    """检查会话是否需要压缩，需要则调用 LLM 生成/更新摘要（就地更新 session.messages 不变，仅落库摘要）"""
    messages = session.messages
    if len(messages) <= SUMMARY_TRIGGER_MESSAGE_COUNT:
        total_chars = sum(len(m.content) for m in messages)
        if total_chars <= SUMMARY_TRIGGER_CHAR_COUNT:
            return

    existing = get_summary(session.session_id)
    to_summarize = messages[:-KEEP_RECENT_MESSAGES] if len(messages) > KEEP_RECENT_MESSAGES else []
    if not to_summarize:
        return

    try:
        from app.core.llm import LLMFactory

        llm = LLMFactory.create(llm_provider)
        transcript = "\n".join(
            f"{'用户' if m.role == 'user' else '助手'}: {m.content[:400]}" for m in to_summarize
        )
        prior_summary = existing["summary"] if existing else ""
        prompt = (
            "请将以下对话历史压缩为简洁摘要（200字以内），保留关键事实、用户诉求与已给出的结论，"
            "去除寒暄与重复内容。\n\n"
            + (f"【已有摘要】\n{prior_summary}\n\n" if prior_summary else "")
            + f"【新增对话】\n{transcript}"
        )
        collected = ""
        async for chunk in llm.chat([{"role": "user", "content": prompt}], stream=False):
            collected += chunk
        summary = collected.strip()
        if not summary:
            return
        _save_summary(
            session.session_id, session.user_id, summary,
            to_summarize[-1].message_id, len(summary),
        )
        logger.info("会话 %s 摘要已更新，覆盖 %d 条历史消息", session.session_id, len(to_summarize))
    except Exception as e:
        logger.warning("会话摘要生成失败（降级为按条数截断，忽略）: %s", e)


def build_context_messages(session, max_recent: int) -> List[dict]:
    """构建带摘要压缩的历史消息：若有摘要，前置摘要 system 消息 + 最近消息；否则按条数截断"""
    summary = get_summary(session.session_id)
    recent = session.messages[-max_recent * 2:]
    if not summary:
        return [{"role": m.role, "content": m.content} for m in recent]

    covered_id = summary.get("covered_until_message_id")
    covered_idx = -1
    for i, m in enumerate(session.messages):
        if m.message_id == covered_id:
            covered_idx = i
            break
    tail = session.messages[covered_idx + 1:] if covered_idx >= 0 else recent

    result = [{
        "role": "system",
        "content": f"【此前对话摘要】{summary['summary']}",
    }]
    result.extend({"role": m.role, "content": m.content} for m in tail)
    return result
