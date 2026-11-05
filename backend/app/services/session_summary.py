"""
见字如面 - 会话历史摘要压缩

借鉴 Claude Code 三层 compaction 设计：
- 自动压缩：按 token headroom 触发（estimate_tokens 估算「摘要+tail 历史」超过
  CONTEXT_COMPACT_TRIGGER_TOKENS 即压缩；过小会话豁免；条数/字符阈值兜底）；
- 微压缩：分级 clip——最近 KEEP_RECENT_FULL 条全文 → 中间档 CLIP_MID_CHARS →
  更旧档 CLIP_OLD_CHARS 深度压缩（完整原文仍存会话记录）；
- 手动压缩：maybe_summarize(force=True, focus=...) 供 compact API 强制触发；
- 结构化摘要契约：五节 checklist（意图/结论/约束/未决/下一步），prior+new 增量更新；
- 再水化：注入「摘要 + continuation 指令」，已知信息不重复询问。
失败时静默降级为原有的按条数截断策略，不阻断聊天主流程。
"""
import logging
from datetime import datetime
from typing import List, Optional

from app.config import settings
from app.core import db
from app.core import observability as obs

logger = logging.getLogger("jianziruyang.session_summary")

# 兜底触发阈值：消息数量或历史总字符数任一超过即触发（token 阈值优先）。
SUMMARY_TRIGGER_MESSAGE_COUNT = int(getattr(settings, "SUMMARY_TRIGGER_MESSAGE_COUNT", 12))
SUMMARY_TRIGGER_CHAR_COUNT = int(getattr(settings, "SUMMARY_TRIGGER_CHAR_COUNT", 6000))
# token headroom 触发：历史 token 估算超过该值即压缩
CONTEXT_COMPACT_TRIGGER_TOKENS = int(getattr(settings, "CONTEXT_COMPACT_TRIGGER_TOKENS", 6000))
# 过小会话豁免：消息数低于该值不压缩（摘要收益不抵信息损耗）
COMPACT_MIN_MESSAGES = 8
# 压缩后仍保留最近 N 条消息不进摘要
KEEP_RECENT_MESSAGES = 6
# 深度压缩尾注（更旧档消息截断时附加）
_OLD_CLIP_NOTE = "（更早消息已深度压缩，完整原文仍存会话记录）"


def _clip_history(messages: List[dict]) -> List[dict]:
    """微压缩分级 clip：最近 keep_full 条全文 → 中间档 mid 截断 → 更旧档 old 深度压缩。

    system 角色消息（如摘要）不截断。避免历史窗口内单条超长回答撑大 prompt token。
    """
    mid = int(getattr(settings, "CLIP_MID_CHARS", 800))
    old = int(getattr(settings, "CLIP_OLD_CHARS", 200))
    keep_full = int(getattr(settings, "KEEP_RECENT_FULL", 4))
    if mid <= 0 or not messages:
        return messages
    n = len(messages)
    # 三档边界：全文区 [n-keep_full, n)；深度压缩区 [0, max(0, n-2*keep_full))；其余为中间档
    old_end = max(0, n - 2 * keep_full)
    result = []
    for i, m in enumerate(messages):
        content = m.get("content", "") or ""
        if m.get("role") == "system" or i >= n - keep_full:
            result.append(m)
            continue
        limit = old if i < old_end else mid
        if len(content) <= limit:
            result.append(m)
        elif i < old_end:
            result.append({
                "role": m.get("role", "user"),
                "content": content[:limit] + f"\n…{_OLD_CLIP_NOTE}",
            })
        else:
            result.append({
                "role": m.get("role", "user"),
                "content": content[:limit] + f"\n…（较早消息已截断，原长 {len(content)} 字符）",
            })
    return result


def estimate_context_tokens(session) -> int:
    """估算会话注入上下文的 token 占用：摘要段 + tail 历史（clip 前原文口径）。"""
    total = 0
    summary = get_summary(session.session_id)
    if summary:
        total += obs.estimate_tokens(summary.get("summary") or "")
    for m in session.messages[-KEEP_RECENT_MESSAGES * 2:]:
        total += obs.estimate_tokens(m.content or "")
    return total


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


async def maybe_summarize(session, llm_provider: str = None, force: bool = False, focus: str = "") -> Optional[dict]:
    """检查会话是否需要压缩，需要则调用 LLM 生成/更新摘要（仅落库摘要，不改 session.messages）。

    force=True 时无视阈值强制触发（手动 compact），focus 作为压缩侧重点拼入 prompt；
    返回压缩统计 {covered_messages, before_chars, after_chars}，未触发/失败返回 None。
    """
    messages = session.messages
    if not force:
        # 过小会话豁免（借鉴 Claude Code：压缩收益不抵信息损耗）
        if len(messages) < COMPACT_MIN_MESSAGES:
            return None
        before_tokens = estimate_context_tokens(session)
        if before_tokens <= CONTEXT_COMPACT_TRIGGER_TOKENS:
            # token headroom 未超：条数/字符阈值兜底（保留既有触发语义）
            total_chars = sum(len(m.content) for m in messages)
            if len(messages) <= SUMMARY_TRIGGER_MESSAGE_COUNT and total_chars <= SUMMARY_TRIGGER_CHAR_COUNT:
                return None

    existing = get_summary(session.session_id)
    to_summarize = messages[:-KEEP_RECENT_MESSAGES] if len(messages) > KEEP_RECENT_MESSAGES else []
    if not to_summarize:
        return None
    before_chars = sum(len(m.content) for m in to_summarize)

    try:
        from app.core.llm import LLMFactory

        # 会话摘要为后台辅助生成，走网关默认规则（role 仅用于日志显性化）
        llm = LLMFactory.create_for_role("session_summary")
        transcript = "\n".join(
            f"{'用户' if m.role == 'user' else '助手'}: {m.content[:400]}" for m in to_summarize
        )
        prior_summary = existing["summary"] if existing else ""
        max_chars = int(getattr(settings, "SUMMARY_MAX_CHARS", 400))
        # 结构化摘要契约（借鉴 Claude Code compaction checklist，防关键信息遗漏）
        prompt = (
            f"请将以下对话历史压缩为结构化摘要（{max_chars}字以内），按五节输出，每节一行、以序号开头，"
            "无内容的小节写「无」，去除寒暄与重复内容：\n"
            "1.用户意图与当前任务 2.已给出的关键结论/建议 3.用户约束与偏好（本会话提及） "
            "4.未决问题 5.下一步\n\n"
            + (f"【压缩侧重点】{focus.strip()}\n\n" if focus and focus.strip() else "")
            + (f"【已有摘要】\n{prior_summary}\n\n" if prior_summary else "")
            + f"【新增对话】\n{transcript}"
        )
        collected = ""
        async for chunk in llm.chat([{"role": "user", "content": prompt}], stream=False):
            collected += chunk
        summary = collected.strip()
        if not summary:
            return None
        _save_summary(
            session.session_id, session.user_id, summary,
            to_summarize[-1].message_id, obs.estimate_tokens(summary),
        )
        stats = {
            "covered_messages": len(to_summarize),
            "before_chars": before_chars,
            "after_chars": len(summary),
        }
        logger.info(
            "会话 %s 摘要已更新%s：覆盖 %d 条历史消息，%d 字 → %d 字（token 估算 %d → %d）",
            session.session_id, "（手动压缩）" if force else "", len(to_summarize),
            before_chars, len(summary),
            obs.estimate_tokens("\n".join(m.content for m in to_summarize)),
            obs.estimate_tokens(summary),
        )
        return stats
    except Exception as e:
        logger.warning("会话摘要生成失败（降级为按条数截断，忽略）: %s", e)
        return None


def build_context_messages(session, max_recent: int) -> List[dict]:
    """构建带摘要压缩的历史消息：若有摘要，前置「摘要 + continuation 指令」system 消息
    + 摘要覆盖点之后的 tail；否则按条数截断。

    无论是否有摘要，较早消息均施加微压缩分级 clip（_clip_history），控制历史窗口 Token。
    """
    summary = get_summary(session.session_id)
    recent = session.messages[-max_recent * 2:]
    if not summary:
        return _clip_history([{"role": m.role, "content": m.content} for m in recent])

    covered_id = summary.get("covered_until_message_id")
    covered_idx = -1
    for i, m in enumerate(session.messages):
        if m.message_id == covered_id:
            covered_idx = i
            break
    tail = session.messages[covered_idx + 1:] if covered_idx >= 0 else recent

    # 再水化（借鉴 Claude Code）：摘要 + continuation 指令，避免重复询问已知信息
    summary_text = (
        f"【此前对话摘要】\n{summary['summary']}\n"
        "请基于摘要与最近消息继续对话，已知信息不要重复询问。"
    )
    obs.note_segment("summary", summary_text)
    result = [{"role": "system", "content": summary_text}]
    result.extend(_clip_history([{"role": m.role, "content": m.content} for m in tail]))
    return result
