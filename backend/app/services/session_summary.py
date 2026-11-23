"""
见字如面 - 会话上下文三级压缩链（对齐 Claude Code microCompact/autoCompact/SessionMemoryCompact）

第一级 microCompact（读时投影，不落库）：
- 位置无关两档冻结 clip——最近 KEEP_RECENT_FULL 条全文，其余固定 CLIP_MID_CHARS
  截断（完整原文仍存会话记录）；渲染只依赖消息自身内容，历史前缀 append-only 稳定，
  利于模型前缀缓存命中；
- 空闲衰减变体（对齐 CC 时间衰减 Micro-Compact）：会话空闲超过 MICRO_COMPACT_IDLE_MINUTES
  分钟后，旧消息（最近 MICRO_COMPACT_KEEP_FULL 条之外）进一步压到 CLIP_OLD_CHARS，
  同样只改读时投影，原文不动。

第二级 autoCompact（maybe_summarize，LLM 结构化摘要）：
- 绝对缓冲阈值触发：token headroom（CONTEXT_COMPACT_TRIGGER_TOKENS，语义等价
  CC「有效窗口 − 摘要预留 − 安全缓冲」）优先，过小会话豁免，条数/字符阈值兜底；
- 全量历史送摘要器（总预算保护），九节 checklist（1-5 节沿用既有契约，
  6-9 节对齐 CC：用户消息枚举/知识来源与附件/当前工作最细颗粒度/错误与纠正）；
- 批更新门控（摘要前缀稳定）、递归守卫（同会话并发压缩互斥）、
  熔断（连续失败 ≥ COMPACT_FAIL_STREAK_LIMIT 停自动压缩，force 手动可绕过）；
- 边界标记：compact_kind（auto/manual）与 before_tokens 落库，供观测与再水化区分；
- 手动压缩：force=True + focus 侧重点；自动模式再水化附「抑制追问」指令
  （对齐 CC suppressFollowUpQuestions）。

第三级 SessionMemoryCompact（SESSION.md 持久载体）：
- 每次压缩结果同步写 memory/sessions/<sid>/SESSION.md（frontmatter 记覆盖点/类型/编辑标记）；
- DB 摘要缺失时从文件恢复（文件优先的持久记录语义）；熔断期间复用既有会话记忆降级。

再水化：注入「摘要 + continuation 指令」，已知信息不重复询问。
失败时静默降级为原有的按条数截断策略，不阻断聊天主流程。
"""
import logging
from datetime import datetime
from typing import List, Optional

from app.config import settings
from app.core import db
from app.core import observability as obs
from app.services import memory_store

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
# 摘要批更新门控：距上次摘要覆盖点新增「可摘要消息」（不含最近保留区）不足该值时
# 不重算摘要（摘要前缀稳定，利于前缀缓存）
SUMMARY_BATCH_MIN_MESSAGES = int(getattr(settings, "SUMMARY_BATCH_MIN_MESSAGES", 6))
# 无摘要时的全量冻结窗口上限（固定容量：未达上限前历史前缀完全 append-only）
FREEZE_MAX_MESSAGES = SUMMARY_TRIGGER_MESSAGE_COUNT * 2

# ---- 第一级 microCompact 空闲衰减参数（对齐 CC 60 分钟时间衰减）----
MICRO_COMPACT_IDLE_MINUTES = float(getattr(settings, "MICRO_COMPACT_IDLE_MINUTES", 60))
MICRO_COMPACT_KEEP_FULL = 5
CLIP_OLD_CHARS = int(getattr(settings, "CLIP_OLD_CHARS", 200))

# ---- 第二级 autoCompact 治理参数 ----
# 熔断阈值：连续失败达到该值后停止自动压缩（force 手动不受限），降级复用既有会话记忆
COMPACT_FAIL_STREAK_LIMIT = 3
# 送摘要器的全量历史总预算（字符）：超预算保留最近部分（摘要器窗口保护）
SUMMARY_TRANSCRIPT_MAX_CHARS = 20000
# 单条消息送摘要器的字符上限（全量历史口径，较旧版 400 字大幅放宽）
SUMMARY_TRANSCRIPT_PER_MESSAGE = 2000
# 递归守卫：同会话并发压缩互斥（对齐 CC querySource 守卫，防摘要器自触发）
_compacting: set = set()
# 熔断计数（无摘要行的会话用模块 dict 承载；有摘要行时镜像到 fail_streak 列）
_fail_streak: dict = {}
_FAIL_STREAK_MAX_SESSIONS = 500


def _clip_history(messages: List[dict]) -> List[dict]:
    """微压缩两档冻结 clip：最近 keep_full 条全文，其余固定 mid 截断并附原长尾注。

    位置无关设计（前缀缓存友好）：单条消息的渲染结果只取决于自身内容与「是否位于
    最近 keep_full 条」，不随窗口滑动/总条数变化而重排；历史消息一旦渲染即冻结，
    仅最近 keep_full 条在退出全文区时发生一次性截断迁移。system 角色消息（如摘要）
    与短消息不截断。完整原文仍存会话记录。
    """
    mid = int(getattr(settings, "CLIP_MID_CHARS", 800))
    keep_full = int(getattr(settings, "KEEP_RECENT_FULL", 4))
    if mid <= 0 or not messages:
        return messages
    n = len(messages)
    result = []
    for i, m in enumerate(messages):
        content = m.get("content", "") or ""
        if m.get("role") == "system" or i >= n - keep_full or len(content) <= mid:
            result.append(m)
            continue
        result.append({
            "role": m.get("role", "user"),
            "content": content[:mid] + f"\n…（较早消息已截断，原长 {len(content)} 字符）",
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


def _session_idle_minutes(session) -> float:
    """会话空闲时长（分钟）：取会话 updated_at，缺失时退到最后一条消息时间戳。"""
    ts = getattr(session, "updated_at", "") or ""
    if not ts:
        msgs = getattr(session, "messages", None) or []
        ts = getattr(msgs[-1], "created_at", "") if msgs else ""
    try:
        dt = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (datetime.now() - dt).total_seconds() / 60.0)


def _micro_decay(messages: List[dict]) -> tuple:
    """microCompact 空闲衰减：最近 MICRO_COMPACT_KEEP_FULL 条之外压到 CLIP_OLD_CHARS。

    读时投影（不落库），system 消息与短消息不动。返回 (渲染结果, 被衰减条数)。
    """
    n = len(messages)
    result, clipped = [], 0
    for i, m in enumerate(messages):
        content = m.get("content", "") or ""
        if m.get("role") == "system" or i >= n - MICRO_COMPACT_KEEP_FULL or len(content) <= CLIP_OLD_CHARS:
            result.append(m)
            continue
        result.append({
            "role": m.get("role", "user"),
            "content": content[:CLIP_OLD_CHARS] + f"\n…（空闲衰减压缩，原长 {len(content)} 字符）",
        })
        clipped += 1
    return result, clipped


def _get_fail_streak(session_id: str, existing: Optional[dict]) -> int:
    if session_id in _fail_streak:
        return _fail_streak[session_id]
    return int((existing or {}).get("fail_streak") or 0)


def _note_compact_fail(session, existing: Optional[dict]):
    """熔断计数 +1（无摘要行时仅记模块 dict，不创建空摘要行）。"""
    sid = session.session_id
    n = _get_fail_streak(sid, existing) + 1
    _fail_streak[sid] = n
    if len(_fail_streak) > _FAIL_STREAK_MAX_SESSIONS:
        for key in list(_fail_streak.keys())[:len(_fail_streak) - _FAIL_STREAK_MAX_SESSIONS]:
            _fail_streak.pop(key, None)
    if existing:
        db.execute("UPDATE chat_session_summaries SET fail_streak = ? WHERE session_id = ?", (n, sid))
    memory_store.log_compact_event(sid, getattr(session, "user_id", ""), "auto_fail",
                                   detail=f"连续失败 {n} 次")


def get_summary(session_id: str) -> Optional[dict]:
    """读取会话摘要；DB 缺失时从 SESSION.md 恢复（SessionMemoryCompact 文件优先语义）。"""
    row = db.query_one(
        "SELECT * FROM chat_session_summaries WHERE session_id = ?", (session_id,)
    )
    if row:
        return row
    rec = memory_store.read_session_memory(session_id)
    if not rec:
        return None
    return {
        "session_id": session_id,
        "user_id": rec.get("user_id") or "",
        "summary": rec.get("summary") or "",
        "covered_until_message_id": rec.get("covered_until_message_id") or "",
        "token_estimate": obs.estimate_tokens(rec.get("summary") or ""),
        "edited_by_user": int(rec.get("edited_by_user") or 0),
        "created_at": rec.get("updated_at") or "",
        "updated_at": rec.get("updated_at") or "",
        "compact_kind": rec.get("compact_kind") or "auto",
        "before_tokens": int(rec.get("before_tokens") or 0),
        "fail_streak": 0,
        "restored_from_file": True,
    }


# 会话笔记（L3 Session Memory 显性化）：用户可编辑的摘要正文上限
MAX_SESSION_NOTE_CHARS = 2000


def get_session_note(session_id: str) -> dict:
    """读取会话笔记（L3 显性化的结构化摘要），供前端查看/编辑。

    返回 {session_id, note, edited_by_user, updated_at}；无摘要时 note 为空串、
    edited_by_user=0（前端展示空态并允许用户首次填写）。只读，不改任何状态。
    """
    row = get_summary(session_id)
    if not row:
        return {"session_id": session_id, "note": "", "edited_by_user": 0, "updated_at": None}
    return {
        "session_id": session_id,
        "note": row.get("summary") or "",
        "edited_by_user": int(row.get("edited_by_user") or 0),
        "updated_at": row.get("updated_at"),
    }


def update_session_note(session_id: str, user_id: str, text: str) -> Optional[dict]:
    """用户编辑会话笔记：校验会话归属后置 edited_by_user=1（自动压缩不再覆盖）。

    归属不符/会话不存在返回 None（API 转 403/404）。保留既有覆盖点，token 估算按新正文重算。
    """
    if not session_id or not user_id:
        return None
    owner = db.query_one(
        "SELECT user_id FROM chat_sessions WHERE session_id = ?", (session_id,)
    )
    if not owner or (owner.get("user_id") or "") != user_id:
        return None
    note = (text or "").strip()[:MAX_SESSION_NOTE_CHARS]
    existing = get_summary(session_id)
    now = datetime.now().isoformat()
    covered = (existing or {}).get("covered_until_message_id") or ""
    db.execute(
        "INSERT INTO chat_session_summaries"
        " (session_id, user_id, summary, covered_until_message_id, token_estimate,"
        " edited_by_user, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(session_id) DO UPDATE SET"
        " summary=excluded.summary, token_estimate=excluded.token_estimate,"
        " edited_by_user=excluded.edited_by_user, updated_at=excluded.updated_at",
        (session_id, user_id, note, covered, obs.estimate_tokens(note), 1, now, now),
    )
    # SessionMemoryCompact：用户编辑同步落 SESSION.md（edited_by_user=1，文件持久记录）
    memory_store.write_session_memory(session_id, user_id, note, {
        "covered_until_message_id": covered, "compact_kind": "note",
        "edited_by_user": 1, "updated_at": now, "before_tokens": 0,
    })
    return {"session_id": session_id, "note": note, "edited_by_user": 1, "updated_at": now}


def _save_summary(session_id: str, user_id: str, summary: str, covered_until_message_id: str,
                  token_estimate: int, compact_kind: str = "auto", before_tokens: int = 0):
    now = datetime.now().isoformat()
    db.execute(
        "INSERT INTO chat_session_summaries"
        " (session_id, user_id, summary, covered_until_message_id, token_estimate,"
        "  created_at, updated_at, compact_kind, before_tokens, fail_streak)"
        " VALUES (?,?,?,?,?,?,?,?,?,0)"
        " ON CONFLICT(session_id) DO UPDATE SET"
        " summary=excluded.summary, covered_until_message_id=excluded.covered_until_message_id,"
        " token_estimate=excluded.token_estimate, updated_at=excluded.updated_at,"
        " compact_kind=excluded.compact_kind, before_tokens=excluded.before_tokens, fail_streak=0",
        (session_id, user_id, summary, covered_until_message_id, token_estimate, now, now,
         compact_kind, int(before_tokens)),
    )
    # 压缩成功：清熔断计数（模块 dict + DB 列已在 upsert 中归零）
    _fail_streak.pop(session_id, None)
    # SessionMemoryCompact：压缩结果同步落 SESSION.md（第三级持久载体）
    memory_store.write_session_memory(session_id, user_id, summary, {
        "covered_until_message_id": covered_until_message_id, "compact_kind": compact_kind,
        "edited_by_user": 0, "updated_at": now, "before_tokens": int(before_tokens),
    })


async def maybe_summarize(session, llm_provider: str = None, force: bool = False, focus: str = "") -> Optional[dict]:
    """autoCompact：检查会话是否需要压缩，需要则调用 LLM 生成/更新摘要（仅落库摘要，不改 session.messages）。

    force=True 时无视阈值强制触发（手动 compact），focus 作为压缩侧重点拼入 prompt；
    返回压缩统计 {covered_messages, before_chars, after_chars}，未触发/失败返回 None。
    治理：递归守卫（同会话互斥）、熔断（连续失败停自动）、边界标记（compact_kind/before_tokens）。
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

    # 递归守卫（对齐 CC querySource）：同会话已有压缩在途时直接跳过，防并发重入
    if session.session_id in _compacting:
        return None
    existing = get_summary(session.session_id)
    # L3 用户笔记优先：用户编辑过的摘要不被「自动」压缩覆盖（edited_by_user=1）。
    # 手动 compact（force=True）是用户显式动作，仍允许重新生成；再水化复用已落库摘要。
    if not force and existing and int(existing.get("edited_by_user") or 0):
        return None
    # 熔断（对齐 CC 连续失败 3 次停自动压缩）：降级复用既有会话记忆，force 手动可绕过
    if not force and _get_fail_streak(session.session_id, existing) >= COMPACT_FAIL_STREAK_LIMIT:
        memory_store.log_compact_event(session.session_id, getattr(session, "user_id", ""),
                                       "auto_blocked", detail="熔断：复用既有会话记忆")
        logger.info("会话 %s 自动压缩熔断中（连续失败 ≥%d），复用既有会话记忆",
                    session.session_id, COMPACT_FAIL_STREAK_LIMIT)
        return None
    # 批更新门控：已有摘要且新增「可摘要消息」（总条数 - 最近保留区 - 已覆盖数）不足 N 条
    # 时不重算（摘要前缀稳定，利于前缀缓存）；force=True（手动 compact）绕过门控。
    if not force and existing:
        covered_id = existing.get("covered_until_message_id")
        covered_idx = next(
            (i for i, m in enumerate(messages) if m.message_id == covered_id), -1)
        if covered_idx >= 0:
            new_coverable = len(messages) - KEEP_RECENT_MESSAGES - (covered_idx + 1)
            if new_coverable < SUMMARY_BATCH_MIN_MESSAGES:
                return None
    to_summarize = messages[:-KEEP_RECENT_MESSAGES] if len(messages) > KEEP_RECENT_MESSAGES else []
    if not to_summarize:
        return None
    before_chars = sum(len(m.content) for m in to_summarize)

    _compacting.add(session.session_id)
    try:
        from app.core.llm import LLMFactory

        # 会话摘要为后台辅助生成，走网关默认规则（role 仅用于日志显性化）
        llm = LLMFactory.create_for_role("session_summary")
        # 全量历史送摘要器（对齐 CC：不再 400 字粗截断），单条/总量双预算保护摘要器窗口
        transcript = "\n".join(
            f"{'用户' if m.role == 'user' else '助手'}: {m.content[:SUMMARY_TRANSCRIPT_PER_MESSAGE]}"
            for m in to_summarize
        )
        if len(transcript) > SUMMARY_TRANSCRIPT_MAX_CHARS:
            transcript = "…（较早历史超出摘要器预算，已省略）\n" + transcript[-SUMMARY_TRANSCRIPT_MAX_CHARS:]
        prior_summary = existing["summary"] if existing else ""
        max_chars = int(getattr(settings, "SUMMARY_MAX_CHARS", 400))
        # 结构化摘要契约（九节 checklist：1-5 节沿用既有契约，6-9 节对齐 CC compaction，防关键信息遗漏）
        prompt = (
            f"请将以下对话历史压缩为结构化摘要（{max_chars}字以内），按九节输出，每节一行、以序号开头，"
            "无内容的小节写「无」，去除寒暄与重复内容：\n"
            "1.用户意图与当前任务 2.已给出的关键结论/建议 3.用户约束与偏好（本会话提及） "
            "4.未决问题 5.下一步\n"
            "6.所有用户消息枚举（逐条列出用户的关键诉求，不得遗漏） "
            "7.知识来源与附件状态（引用过的文档/链接/附件及处理状态） "
            "8.当前工作（最细颗粒度：正在做什么、卡点、立即要做的动作） "
            "9.错误与纠正（出现过的错误、用户纠正及修正结论）\n\n"
            + (f"【压缩侧重点】{focus.strip()}\n\n" if focus and focus.strip() else "")
            + (f"【已有摘要】\n{prior_summary}\n\n" if prior_summary else "")
            + f"【新增对话】\n{transcript}"
        )
        collected = ""
        async for chunk in llm.chat([{"role": "user", "content": prompt}], stream=False):
            collected += chunk
        summary = collected.strip()
        if not summary:
            _note_compact_fail(session, existing)
            return None
        before_tokens_est = obs.estimate_tokens("\n".join(m.content for m in to_summarize))
        compact_kind = "manual" if force else "auto"
        _save_summary(
            session.session_id, session.user_id, summary,
            to_summarize[-1].message_id, obs.estimate_tokens(summary),
            compact_kind=compact_kind, before_tokens=before_tokens_est,
        )
        # 压缩链观测事件：边界标记（类型/压缩前后 token）供可观测性钻取
        memory_store.log_compact_event(session.session_id, session.user_id, compact_kind,
                                       before_tokens=before_tokens_est,
                                       after_tokens=obs.estimate_tokens(summary),
                                       detail=f"覆盖 {len(to_summarize)} 条")
        # L5：摘要覆盖点推进（compact）后重置本会话记忆去重集合，允许压缩后合理重注入
        # （对齐 Claude Code collectSurfacedMemories「compact 后自然重置」语义）。
        try:
            from app.services.memory import memory_service
            memory_service.reset_surfaced(session.session_id)
        except Exception:
            pass
        stats = {
            "covered_messages": len(to_summarize),
            "before_chars": before_chars,
            "after_chars": len(summary),
        }
        logger.info(
            "会话 %s 摘要已更新%s：覆盖 %d 条历史消息，%d 字 → %d 字（token 估算 %d → %d）",
            session.session_id, "（手动压缩）" if force else "", len(to_summarize),
            before_chars, len(summary), before_tokens_est, obs.estimate_tokens(summary),
        )
        return stats
    except Exception as e:
        logger.warning("会话摘要生成失败（降级为按条数截断，忽略）: %s", e)
        _note_compact_fail(session, existing)
        return None
    finally:
        _compacting.discard(session.session_id)


def build_context_messages(session, max_recent: int) -> List[dict]:
    """构建带摘要压缩的历史消息：若有摘要，前置「摘要 + continuation 指令」system 消息
    + 摘要覆盖点之后的 tail；否则取全量冻结窗口（固定容量，未达上限前 append-only）。

    无论是否有摘要，较早消息均施加两档冻结 clip（_clip_history）：渲染位置无关，
    历史前缀稳定，利于模型前缀缓存命中，同时控制历史窗口 Token。
    microCompact 空闲衰减：会话空闲超阈值时旧消息进一步压缩（读时投影，不落库）。
    """
    summary = get_summary(session.session_id)
    if not summary or not (summary.get("summary") or "").strip():
        msgs = session.messages
        window = msgs if len(msgs) <= FREEZE_MAX_MESSAGES else msgs[-FREEZE_MAX_MESSAGES:]
        result = _clip_history([{"role": m.role, "content": m.content} for m in window])
    else:
        recent = session.messages[-max_recent * 2:]

        covered_id = summary.get("covered_until_message_id")
        covered_idx = -1
        for i, m in enumerate(session.messages):
            if m.message_id == covered_id:
                covered_idx = i
                break
        tail = session.messages[covered_idx + 1:] if covered_idx >= 0 else recent

        # 再水化（借鉴 Claude Code）：摘要 + continuation 指令，避免重复询问已知信息；
        # 自动压缩附「抑制追问」指令（对齐 CC suppressFollowUpQuestions），手动压缩不附加。
        summary_text = (
            f"【此前对话摘要】\n{summary['summary']}\n"
            "请基于摘要与最近消息继续对话，已知信息不要重复询问。"
        )
        if (summary.get("compact_kind") or "auto") == "auto":
            summary_text += "\n（自动压缩后请直接推进当前任务，避免不必要的追问确认。）"
        obs.note_segment("summary", summary_text)
        result = [{"role": "system", "content": summary_text}]
        result.extend(_clip_history([{"role": m.role, "content": m.content} for m in tail]))

    # microCompact 空闲衰减（第一级时间衰减变体）：仅读时投影，完整原文仍存会话记录
    idle = _session_idle_minutes(session)
    if idle >= MICRO_COMPACT_IDLE_MINUTES:
        result, clipped = _micro_decay(result)
        if clipped:
            memory_store.log_compact_event(
                session.session_id, getattr(session, "user_id", ""), "micro",
                detail=f"空闲 {int(idle)} 分钟，衰减 {clipped} 条旧消息")
    return result
