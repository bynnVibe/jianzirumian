"""
见字如面 - 短期记忆（会话记忆）与上下文四层压缩（对齐 Claude Code，128K 会话窗口）

一个会话 = 一个上下文窗口（CHAT_CONTEXT_WINDOW，名义 128K=128000，K=1000）。
四层压缩按「触发频率越来越低、代价越来越高」排列，只作用于本会话窗口：

  L0 日常约束：Output Cap（每轮 4K）/ Tool-Result Offloading（落盘+2KB预览+路径）/
     Deduplication（重复结果引用替换）——由 context_window 与 llm 层实现，零模型调用。
  L1 微压缩 Micro Compaction：可重现工具结果合计 ≥20K 进入、可释放 <10K 放弃，
     旧结果改占位符、配对不变——context_window.micro_compact，每次 API 调用前，零调用。
  L2 系统压缩：
     2a Session Memory（本模块 maybe_summarize 后台 forked agent → summary.md）：
        首次水位 ≥10K；后续「增长 ≥5K 且 工具调用 ≥3 且 自然断点」三者同时满足；
        只落盘落库，不提前替换历史。
     2b Auto Compaction（本模块 auto_compact，≥95K=128K−20K−13K）：summary.md 命中
        零调用直接替换；未命中 fork 压缩代理生成九段式摘要替换历史，保留 ≥10K 且最近 3 轮。
  L3 兜底压缩 Full Compaction（本模块 full_compact）：Auto 连续失败 ≥3 / ≥93% 窗口 /
     上游超长触发；全部工具结果落盘、仅留最近 1 轮，含零调用确定性兜底。

短期记忆五模块（transcript/Session Memory/Task State/Tool Results/Compaction）中，本模块实现
Session Memory 与 Compaction；transcript.jsonl 只读黑匣子由 memory_store 追加式落盘、
task_state 会话期内存对象在对话暂停时落盘。工具结果账本（原文落盘 tool-results/）/窗口状态/
历史装配/微压缩由 context_window 承担。摘要持久载体 users/<uid>/sessions/<sid>/summary.md（frontmatter
记覆盖点/边界/类型/编辑标记/水位）；DB 缺失时从文件恢复（文件优先）。

再水化：注入「摘要 + continuation 指令 + Task State 要点 + 落盘回执路径」，已知信息不重复询问。
失败时静默降级，不阻断聊天主流程。
"""
import logging
from datetime import datetime
from typing import List, Optional

from app.config import settings
from app.core import db
from app.core import observability as obs
from app.services import memory_store
from app.services import context_window as cw

logger = logging.getLogger("jianziruyang.session_summary")

# Session Memory 首次触发：会话水位第一次达到该值（默认 10K）创建 summary.md
CONTEXT_COMPACT_TRIGGER_TOKENS = int(getattr(settings, "CONTEXT_COMPACT_TRIGGER_TOKENS", 10000))
# Session Memory 后续更新：距上次摘要水位增长 ≥ 该值（默认 5K，与工具数/自然断点同时满足）
SESSION_MEMORY_GROWTH_TOKENS = int(getattr(settings, "SESSION_MEMORY_GROWTH_TOKENS", 5000))
# Session Memory 后续更新：期间工具调用累计 ≥ 该次数
SESSION_MEMORY_TOOL_CALLS = int(getattr(settings, "SESSION_MEMORY_TOOL_CALLS", 3))
# Session Memory 摘要正文 token 上限（九段式）
SESSION_MEMORY_MAX_TOKENS = int(getattr(settings, "SESSION_MEMORY_MAX_TOKENS", 2000))
# 对话暂停落盘 task_state 的空闲阈值（摘要触发语义已退役，落盘行为保留）
SESSION_MEMORY_PAUSE_MINUTES = float(getattr(settings, "SESSION_MEMORY_PAUSE_MINUTES", 10))
# 熔断阈值：Auto 连续失败达到该值升级为 Full（force 手动不受限）
COMPACT_FAIL_STREAK_LIMIT = int(getattr(settings, "COMPACT_FAIL_STREAK_LIMIT", 3))
# 送摘要器的历史总预算（字符）：超预算保留最近部分（摘要器窗口保护）
SUMMARY_TRANSCRIPT_MAX_CHARS = 60000
# 单条消息送摘要器的字符上限
SUMMARY_TRANSCRIPT_PER_MESSAGE = 4000
# 实时类助手答案入历史即衰减的字符上限（位置无关规则，保留）
CLIP_OLD_CHARS = int(getattr(settings, "CLIP_OLD_CHARS", 200))
# 递归守卫：同会话并发压缩互斥（对齐 CC querySource 守卫，防摘要器自触发）
_compacting: set = set()


def history_segment_text(messages: List[dict]) -> str:
    """可观测 history 段口径：剔除再水化摘要等 system 消息，避免重复计量。"""
    return cw.history_segment_text(messages)


def estimate_context_tokens(session) -> int:
    """会话窗口水位：优先取最近一次主链路调用的真实 prompt_tokens（session_context_state），
    缺失时按「摘要 + 全量历史原文 + 窗口内工具结果」估算（回合结束由真实 usage 校准）。"""
    sid = getattr(session, "session_id", "")
    state = cw.get_state(sid)
    real = int(state.get("ctx_tokens") or 0)
    if real > 0:
        return real
    total = 0
    summary = get_summary(sid)
    if summary:
        total += obs.estimate_tokens(summary.get("summary") or "")
    for m in getattr(session, "messages", []) or []:
        total += obs.estimate_tokens(getattr(m, "content", "") or "")
    total += cw.live_tool_result_tokens(sid, (summary or {}).get("boundary_message_id") or "")
    return total


def compaction_window_threshold() -> int:
    """Auto Compaction 触发阈值（窗口 − 摘要预留 − 输入缓冲，128K→95K）。"""
    return cw.auto_compact_threshold()


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


def _get_fail_streak(session_id: str, existing: Optional[dict]) -> int:
    return cw.get_fail_streak(session_id)


def _note_compact_fail(session, existing: Optional[dict]):
    """Auto 连续失败计数 +1（持久化到 session_context_state，进程重启不丢）。"""
    sid = session.session_id
    n = cw.note_auto_fail(sid, getattr(session, "user_id", ""))
    if existing:
        db.execute("UPDATE chat_session_summaries SET fail_streak = ? WHERE session_id = ?", (n, sid))


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
        "boundary_message_id": rec.get("boundary_message_id") or "",
        "token_estimate": obs.estimate_tokens(rec.get("summary") or ""),
        "edited_by_user": int(rec.get("edited_by_user") or 0),
        "created_at": rec.get("updated_at") or "",
        "updated_at": rec.get("updated_at") or "",
        "compact_kind": rec.get("compact_kind") or "session_memory",
        "before_tokens": int(rec.get("before_tokens") or 0),
        "ctx_tokens_at_update": int(rec.get("ctx_tokens_at_update") or 0),
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
    boundary = (existing or {}).get("boundary_message_id") or ""
    db.execute(
        "INSERT INTO chat_session_summaries"
        " (session_id, user_id, summary, covered_until_message_id, boundary_message_id,"
        " token_estimate, edited_by_user, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(session_id) DO UPDATE SET"
        " summary=excluded.summary, token_estimate=excluded.token_estimate,"
        " edited_by_user=excluded.edited_by_user, updated_at=excluded.updated_at",
        (session_id, user_id, note, covered, boundary, obs.estimate_tokens(note), 1, now, now),
    )
    # Session Memory：用户编辑同步落 summary.md（edited_by_user=1，文件持久记录）
    memory_store.write_session_memory(session_id, user_id, note, {
        "covered_until_message_id": covered, "boundary_message_id": boundary,
        "compact_kind": "note", "edited_by_user": 1, "updated_at": now, "before_tokens": 0,
    })
    return {"session_id": session_id, "note": note, "edited_by_user": 1, "updated_at": now}


def _save_summary(session_id: str, user_id: str, summary: str, covered_until_message_id: str,
                  token_estimate: int, compact_kind: str = "session_memory", before_tokens: int = 0,
                  ctx_tokens_at_update: int = 0, boundary_message_id: Optional[str] = None):
    """写摘要行 + summary.md。

    boundary_message_id=None 表示沿用既有压缩边界（Session Memory 只记不替换历史）；
    显式传值（含空串）表示推进/清空边界（Auto/Full 才替换历史）。
    """
    now = datetime.now().isoformat()
    if boundary_message_id is None:
        boundary_message_id = (get_summary(session_id) or {}).get("boundary_message_id") or ""
    db.execute(
        "INSERT INTO chat_session_summaries"
        " (session_id, user_id, summary, covered_until_message_id, boundary_message_id,"
        "  token_estimate, created_at, updated_at, compact_kind, before_tokens, fail_streak,"
        "  ctx_tokens_at_update)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,0,?)"
        " ON CONFLICT(session_id) DO UPDATE SET"
        " summary=excluded.summary, covered_until_message_id=excluded.covered_until_message_id,"
        " boundary_message_id=excluded.boundary_message_id,"
        " token_estimate=excluded.token_estimate, updated_at=excluded.updated_at,"
        " compact_kind=excluded.compact_kind, before_tokens=excluded.before_tokens, fail_streak=0,"
        " ctx_tokens_at_update=excluded.ctx_tokens_at_update",
        (session_id, user_id, summary, covered_until_message_id, boundary_message_id,
         token_estimate, now, now, compact_kind, int(before_tokens), int(ctx_tokens_at_update)),
    )
    # 压缩成功：清熔断计数（session_context_state 与 DB 列同步归零）
    cw.reset_fail_streak(session_id)
    cw.reset_tool_calls(session_id)
    cw.set_last_compact(session_id, compact_kind)
    # Session Memory：摘要笔记同步落 summary.md（短期记忆持久载体，按用户隔离）
    memory_store.write_session_memory(session_id, user_id, summary, {
        "covered_until_message_id": covered_until_message_id,
        "boundary_message_id": boundary_message_id, "compact_kind": compact_kind,
        "edited_by_user": 0, "updated_at": now, "before_tokens": int(before_tokens),
        "ctx_tokens_at_update": int(ctx_tokens_at_update),
    })


# 九段式结构化摘要契约（1-5 节沿用既有契约，6-9 节对齐 CC compaction，防关键信息遗漏）
NINE_SECTION_SPEC = (
    "按九节输出，每节一行、以序号开头，无内容的小节写「无」，去除寒暄与重复内容：\n"
    "1.用户意图与当前任务 2.已给出的关键结论/建议 3.用户约束与偏好（本会话提及） "
    "4.未决问题 5.下一步\n"
    "6.所有用户消息枚举（逐条列出用户的关键诉求，不得遗漏） "
    "7.知识来源与附件状态（引用过的文档/链接/附件及处理状态） "
    "8.当前工作（最细颗粒度：正在做什么、卡点、立即要做的动作） "
    "9.错误与纠正（出现过的错误、用户纠正及修正结论）"
)


def _transcript_text(messages) -> str:
    """送摘要器的历史文本：单条/总量双预算保护摘要器窗口（不做粗截断丢信息）。"""
    transcript = "\n".join(
        f"{'用户' if getattr(m, 'role', '') == 'user' else '助手'}: "
        f"{(getattr(m, 'content', '') or '')[:SUMMARY_TRANSCRIPT_PER_MESSAGE]}"
        for m in messages
    )
    if len(transcript) > SUMMARY_TRANSCRIPT_MAX_CHARS:
        transcript = "…（较早历史超出摘要器预算，已省略）\n" + transcript[-SUMMARY_TRANSCRIPT_MAX_CHARS:]
    return transcript


async def _fork_summary_agent(prompt: str, max_tokens: int) -> str:
    """fork 压缩代理：走网关 memory_agent 业务路由，输出上限按摘要预留显式指定。

    压缩自身的模型调用落 LLM span（name=compact_summary），失败不计入主 trace 失败
    （压缩失败按现有上下文继续，属可降级路径）。
    """
    import time

    from app.core.llm import LLMFactory

    llm = LLMFactory.create_for_role("memory_agent")
    t0 = time.monotonic()
    collected = ""
    try:
        async for chunk in llm.chat([{"role": "user", "content": prompt}], stream=False,
                                    max_tokens=max_tokens):
            collected += chunk
    except Exception as e:
        obs.record_llm("compact_summary", prompt, collected, t0=t0,
                       status="error", error=str(e), fail_trace=False)
        raise
    obs.record_llm("compact_summary", prompt, collected, t0=t0, fail_trace=False)
    return collected.strip()


def _summary_prompt(transcript: str, prior_summary: str = "", focus: str = "",
                    max_tokens: int = 0, keep_note: bool = False) -> str:
    limit = max_tokens or SESSION_MEMORY_MAX_TOKENS
    return (
        f"请将以下对话历史压缩为结构化摘要（{limit} Token 以内），{NINE_SECTION_SPEC}\n\n"
        + (f"【压缩侧重点】{focus.strip()}\n\n" if focus and focus.strip() else "")
        + ("【用户编辑过的笔记】以下内容为用户手工确认，必须原样保留其要点，不得丢弃：\n"
           f"{prior_summary}\n\n" if keep_note and prior_summary else
           (f"【已有摘要】\n{prior_summary}\n\n" if prior_summary else ""))
        + f"【新增对话】\n{transcript}"
    )


def _covered_index(messages, covered_id: str) -> int:
    for i, m in enumerate(messages):
        if getattr(m, "message_id", "") == covered_id:
            return i
    return -1


async def maybe_summarize(session, llm_provider: str = None, force: bool = False, focus: str = "",
                          turn_has_tools: bool = False, natural_break: bool = True) -> Optional[dict]:
    """L2a Session Memory：后台 forked agent 生成/更新 summary.md，**不替换上下文历史**。

    触发口径（对齐 Claude Code）：
    - 首次：会话水位第一次 ≥ CONTEXT_COMPACT_TRIGGER_TOKENS（10K）；
    - 后续：距上次摘要水位增长 ≥5K **且** 期间工具调用累计 ≥3 次 **且** 处于自然断点
      （本轮正常结束：非中断、非错误、无待处理 HITL），三者同时满足才更新。
    force=True 为手动压缩（/compact），走 Auto Compaction 同一流程（事件 manual）。
    退役：条数/字符兜底、过小会话豁免、批门控、暂停触发、「本轮有工具即触发」
    （turn_has_tools 参数保留兼容，不再参与触发判定）。
    返回统计 {covered_messages, before_chars, after_chars, compact_kind}，未触发/失败返回 None。
    """
    # 对话暂停（空闲 ≥ SESSION_MEMORY_PAUSE_MINUTES）视为会话过程结束：
    # 把在途 Task State 内存对象落盘 task_state.md（恢复对话后重新建立内存对象）
    if not force and _session_idle_minutes(session) >= SESSION_MEMORY_PAUSE_MINUTES:
        memory_store.flush_task_state(session.session_id, getattr(session, "user_id", ""))
    if force:
        return await auto_compact(session, focus=focus, manual=True)

    messages = getattr(session, "messages", []) or []
    existing = get_summary(session.session_id)
    ctx_now = estimate_context_tokens(session)
    if existing is None:
        if ctx_now < CONTEXT_COMPACT_TRIGGER_TOKENS:
            return None
    else:
        growth = ctx_now - int(existing.get("ctx_tokens_at_update") or 0)
        tool_calls = int(cw.get_state(session.session_id).get("tool_calls_since_summary") or 0)
        if not (growth >= SESSION_MEMORY_GROWTH_TOKENS
                and tool_calls >= SESSION_MEMORY_TOOL_CALLS and natural_break):
            return None
        # 用户编辑过的笔记不被后台覆盖（edited_by_user=1）
        if int(existing.get("edited_by_user") or 0):
            return None

    # 递归守卫（对齐 CC querySource）：同会话已有压缩在途时直接跳过，防并发重入
    if session.session_id in _compacting:
        return None
    covered_idx = _covered_index(messages, (existing or {}).get("covered_until_message_id") or "")
    to_summarize = messages[covered_idx + 1:] if covered_idx >= 0 else messages
    if not to_summarize:
        return None
    before_chars = sum(len(getattr(m, "content", "") or "") for m in to_summarize)

    _compacting.add(session.session_id)
    try:
        prompt = _summary_prompt(
            _transcript_text(to_summarize),
            prior_summary=(existing or {}).get("summary") or "", focus=focus,
            max_tokens=SESSION_MEMORY_MAX_TOKENS)
        summary = await _fork_summary_agent(prompt, SESSION_MEMORY_MAX_TOKENS)
        if not summary:
            _note_compact_fail(session, existing)
            return None
        before_tokens_est = obs.estimate_tokens("\n".join(
            getattr(m, "content", "") or "" for m in to_summarize))
        # Session Memory 只记不替换：boundary_message_id=None 沿用既有压缩边界
        _save_summary(
            session.session_id, session.user_id, summary,
            to_summarize[-1].message_id, obs.estimate_tokens(summary),
            compact_kind="session_memory", before_tokens=before_tokens_est,
            ctx_tokens_at_update=ctx_now,
        )
        memory_store.log_compact_event(session.session_id, session.user_id, "session_memory",
                                       before_tokens=before_tokens_est,
                                       after_tokens=obs.estimate_tokens(summary),
                                       detail=f"覆盖 {len(to_summarize)} 条")
        _reset_surfaced(session.session_id)
        stats = {
            "covered_messages": len(to_summarize),
            "before_chars": before_chars,
            "after_chars": len(summary),
            "compact_kind": "session_memory",
        }
        logger.info("会话 %s Session Memory 已更新：覆盖 %d 条，%d 字 → %d 字（水位 %d）",
                    session.session_id, len(to_summarize), before_chars, len(summary), ctx_now)
        return stats
    except Exception as e:
        logger.warning("Session Memory 生成失败（降级为不压缩，忽略）: %s", e)
        _note_compact_fail(session, existing)
        return None
    finally:
        _compacting.discard(session.session_id)


def _reset_surfaced(session_id: str):
    """摘要覆盖点推进后重置本会话记忆去重集合（对齐 CC compact 后自然重置语义）。"""
    try:
        from app.services.memory import memory_service
        memory_service.reset_surfaced(session_id)
    except Exception:
        pass


def build_context_messages(session, max_recent: int = 0) -> List[dict]:
    """构建会话历史区（CC 式连续窗口）：原文保留 + 工具配对入账 + 压缩边界。

    - 未压缩：全量历史原文（追加式，不做条数窗口/字符 clip），max_recent 参数保留兼容但不再裁剪；
    - 已压缩（boundary_message_id 非空）：前置「摘要 + continuation 指令 + Task State 要点
      + 最近落盘回执」system 消息，其后为边界之后的原文；
    - 每轮工具交互以配对消息进入历史（助手 [工具调用 #id] + 用户 [工具结果 #id]），
      渲染形态由账本 state/storage 决定（正文 / 预览+路径 / 引用 / 占位符）。
    无压缩事件时上一轮历史区是下一轮的严格前缀（前缀缓存不变量）。
    """
    summary = get_summary(session.session_id)
    history = cw.build_history_messages(session, summary)
    if not summary or not (summary.get("summary") or "").strip() \
            or not (summary.get("boundary_message_id") or ""):
        # Session Memory 只记不替换：有摘要但无压缩边界时仍走全量原文
        return history

    # 再水化（借鉴 Claude Code）：摘要 + continuation 指令，避免重复询问已知信息；
    # 自动压缩附「抑制追问」指令（对齐 CC suppressFollowUpQuestions），手动压缩不附加。
    summary_text = (
        f"【此前对话摘要】\n{summary['summary']}\n"
        "请基于摘要与最近消息继续对话，已知信息不要重复询问。"
    )
    if (summary.get("compact_kind") or "") in ("auto_compact", "auto_compact_hit", "full", "full_fallback"):
        summary_text += "\n（自动压缩后请直接推进当前任务，避免不必要的追问确认。）"
    # Task State 要点：压缩后保留任务进度，避免重复推进已完成步骤
    try:
        state = memory_store.get_task_state(session.session_id) or {}
        bits = [f"目标：{state.get('goal')}"] if state.get("goal") else []
        if state.get("status"):
            bits.append(f"状态：{state['status']}")
        if state.get("steps"):
            bits.append("步骤：" + "；".join(str(s)[:60] for s in state["steps"][:5]))
        if bits:
            summary_text += "\n【任务进度】" + " | ".join(bits)
    except Exception:
        pass
    # 落盘回执：压缩后提示模型可回读被清除/落盘的工具结果原文
    receipts = cw.recent_offload_receipts(session.session_id, 5)
    if receipts:
        summary_text += "\n【可回读的工具结果】\n" + "\n".join(receipts)
    obs.note_segment("summary", summary_text)
    return [{"role": "system", "content": summary_text}] + history


# ---------------------------------------------------------------------------
# L2b Auto Compaction（≥95K）：summary.md 命中零调用，未命中 fork 压缩代理
# ---------------------------------------------------------------------------

def _messages_tokens(messages) -> int:
    return sum(obs.estimate_tokens(getattr(m, "content", "") or "") for m in messages)


async def auto_compact(session, focus: str = "", manual: bool = False) -> Optional[dict]:
    """Auto Compaction：水位 ≥95K（窗口−摘要预留20K−输入缓冲13K）时替换历史。

    - 命中（零调用）：summary.md 非空且覆盖点之后的全部消息可放进保留区上限 → 边界设到覆盖点；
    - 未命中：fork 压缩代理（memory_agent，输出上限=摘要预留）生成九段式摘要，
      丢弃覆盖点到保留区起点之间的历史，保留 ≥10K 且最近 3 轮（上限 40K，3 轮优先）。
    失败：auto_fail_streak+1（事件 auto_fail），连续 ≥3 次升级 Full Compaction。
    manual=True 为手动 /compact：无视阈值与熔断，事件 manual。
    """
    sid = getattr(session, "session_id", "")
    uid = getattr(session, "user_id", "") or ""
    messages = getattr(session, "messages", []) or []
    if not sid or not messages:
        return None
    if sid in _compacting:
        return None
    existing = get_summary(sid)
    ctx_now = estimate_context_tokens(session)
    if not manual:
        if ctx_now < cw.auto_compact_threshold():
            return None
        # 熔断由「停止自动压缩」改为「升级兜底压缩」
        if cw.get_fail_streak(sid) >= COMPACT_FAIL_STREAK_LIMIT:
            return await full_compact(session, reason="auto_fail_streak")

    boundary, keep_start = cw.compute_keep_boundary(session, existing)
    keep_max = int(getattr(settings, "AUTO_COMPACT_KEEP_MAX_TOKENS", 40000))
    covered_id = (existing or {}).get("covered_until_message_id") or ""
    covered_idx = _covered_index(messages, covered_id)
    prior_summary = (existing or {}).get("summary") or ""
    edited = int((existing or {}).get("edited_by_user") or 0)
    reserve = int(getattr(settings, "COMPACT_SUMMARY_RESERVE_TOKENS", 20000))

    _compacting.add(sid)
    try:
        after_covered = messages[covered_idx + 1:] if covered_idx >= 0 else messages
        hit = bool(prior_summary.strip()) and _messages_tokens(after_covered) <= keep_max
        if hit:
            # 命中：零模型调用，直接用摘要替换覆盖点之前的历史
            new_boundary, new_covered = covered_id, covered_id
            summary, kind = prior_summary, ("manual" if manual else "auto_compact_hit")
            dropped = messages[:covered_idx + 1] if covered_idx >= 0 else []
        else:
            to_summarize = messages[covered_idx + 1:keep_start] if covered_idx >= 0 else messages[:keep_start]
            if not to_summarize:
                return None
            prompt = _summary_prompt(
                _transcript_text(to_summarize), prior_summary=prior_summary, focus=focus,
                max_tokens=reserve, keep_note=bool(edited))
            summary = await _fork_summary_agent(prompt, reserve)
            if not summary:
                _note_compact_fail(session, existing)
                return None
            new_boundary = boundary
            new_covered = getattr(messages[keep_start - 1], "message_id", "") if keep_start > 0 else ""
            kind = "manual" if manual else "auto_compact"
            dropped = to_summarize
        before_tokens = _messages_tokens(dropped)
        after_tokens = obs.estimate_tokens(summary)
        _save_summary(sid, uid, summary, new_covered, after_tokens,
                      compact_kind=kind, before_tokens=before_tokens,
                      ctx_tokens_at_update=ctx_now, boundary_message_id=new_boundary)
        memory_store.log_compact_event(sid, uid, kind, before_tokens=before_tokens,
                                       after_tokens=after_tokens,
                                       detail=f"丢弃 {len(dropped)} 条，边界 {new_boundary[:8]}")
        _reset_surfaced(sid)
        obs.record_context_action(
            "auto_compact", before_tokens, after_tokens,
            detail=f"{kind}：丢弃 {len(dropped)} 条，边界 {new_boundary[:8]}",
            meta={"hit": bool(hit), "manual": bool(manual), "compact_kind": kind,
                  "dropped_messages": len(dropped), "boundary_message_id": new_boundary})
        logger.info("会话 %s Auto Compaction(%s)：水位 %d，丢弃 %d 条（%d→%d tok）",
                    sid, kind, ctx_now, len(dropped), before_tokens, after_tokens)
        return {
            "covered_messages": len(dropped),
            "before_chars": sum(len(getattr(m, "content", "") or "") for m in dropped),
            "after_chars": len(summary),
            "compact_kind": kind,
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "boundary_message_id": new_boundary,
        }
    except Exception as e:
        logger.warning("Auto Compaction 失败（本次按现有上下文继续）: %s", e)
        _note_compact_fail(session, existing)
        return None
    finally:
        _compacting.discard(sid)


# ---------------------------------------------------------------------------
# L3 兜底压缩 Full Compaction：比 Auto 更彻底，保证有结果
# ---------------------------------------------------------------------------

def _deterministic_summary(session, existing: Optional[dict]) -> str:
    """零调用确定性兜底：既有摘要 + 用户消息逐条枚举 + Task State + 来源/附件清单。"""
    lines = []
    prior = (existing or {}).get("summary") or ""
    if prior.strip():
        lines.append("【既有摘要】\n" + prior.strip())
    users = [getattr(m, "content", "") or "" for m in getattr(session, "messages", []) or []
             if getattr(m, "role", "") == "user"]
    if users:
        lines.append("6.所有用户消息枚举：\n" + "\n".join(
            f"- {u[:200]}" for u in users[-30:]))
    try:
        state = memory_store.get_task_state(session.session_id) or {}
        if state:
            lines.append("8.当前工作：" + memory_store.render_task_state(state)[:600])
    except Exception:
        pass
    receipts = cw.recent_offload_receipts(session.session_id, 10)
    if receipts:
        lines.append("7.知识来源与附件状态（可回读）：\n" + "\n".join(receipts))
    return "\n\n".join(lines)[:SESSION_MEMORY_MAX_TOKENS * 4]


async def full_compact(session, reason: str = "") -> Optional[dict]:
    """Full Compaction：Auto 连续失败 ≥3 / 水位 ≥93% 窗口 / 上游上下文超长时兜底。

    1. 窗口内全部工具结果转落盘回执（不可重现也落盘，不丢信息）；
    2. 原文只保留最近 1 轮（取消 10K 下限）；
    3. 摘要按 transcript 分块重新生成（分块→合并）；模型再失败走零调用确定性兜底；
    4. 成功后 auto_fail_streak 归零，事件 full / full_fallback。
    """
    sid = getattr(session, "session_id", "")
    uid = getattr(session, "user_id", "") or ""
    messages = getattr(session, "messages", []) or []
    if not sid or not messages:
        return None
    if sid in _compacting:
        return None
    existing = get_summary(sid)
    ctx_now = estimate_context_tokens(session)
    reserve = int(getattr(settings, "COMPACT_SUMMARY_RESERVE_TOKENS", 20000))
    _compacting.add(sid)
    try:
        cw.clear_all_tool_results(sid, uid)
        starts = [i for i, m in enumerate(messages) if getattr(m, "role", "") == "user"]
        keep_start = starts[-1] if starts else max(0, len(messages) - 1)
        to_summarize = messages[:keep_start]
        boundary = getattr(messages[keep_start - 1], "message_id", "") if keep_start > 0 else ""
        summary, kind = "", "full"
        if to_summarize:
            try:
                summary = await _chunked_summary(to_summarize, existing, reserve)
            except Exception as e:
                logger.warning("Full Compaction 摘要生成失败，走确定性兜底: %s", e)
                summary = ""
        if not summary:
            summary = _deterministic_summary(session, existing)
            kind = "full_fallback"
        before_tokens = _messages_tokens(to_summarize)
        after_tokens = obs.estimate_tokens(summary)
        covered = getattr(messages[keep_start - 1], "message_id", "") if keep_start > 0 else ""
        _save_summary(sid, uid, summary, covered, after_tokens, compact_kind=kind,
                      before_tokens=before_tokens, ctx_tokens_at_update=ctx_now,
                      boundary_message_id=boundary)
        memory_store.log_compact_event(sid, uid, kind, before_tokens=before_tokens,
                                       after_tokens=after_tokens,
                                       detail=f"{reason or '兜底'}；仅留最近 1 轮")
        _reset_surfaced(sid)
        obs.record_context_action(
            "full_compact", before_tokens, after_tokens,
            detail=f"{kind}：{reason or '兜底'}，仅留最近 1 轮",
            meta={"reason": reason, "compact_kind": kind,
                  "dropped_messages": len(to_summarize), "boundary_message_id": boundary})
        logger.info("会话 %s Full Compaction(%s)：水位 %d，%d→%d tok", sid, kind,
                    ctx_now, before_tokens, after_tokens)
        return {
            "covered_messages": len(to_summarize),
            "before_chars": sum(len(getattr(m, "content", "") or "") for m in to_summarize),
            "after_chars": len(summary),
            "compact_kind": kind,
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "boundary_message_id": boundary,
        }
    finally:
        _compacting.discard(sid)


async def _chunked_summary(messages, existing: Optional[dict], reserve: int) -> str:
    """分块摘要 → 合并：超摘要器预算的历史按块压缩，再合并为九段式总摘要。"""
    text = _transcript_text(messages)
    prior = (existing or {}).get("summary") or ""
    if len(text) <= SUMMARY_TRANSCRIPT_MAX_CHARS:
        return await _fork_summary_agent(
            _summary_prompt(text, prior_summary=prior, max_tokens=reserve), reserve)
    chunks, buf = [], ""
    for m in messages:
        piece = f"{'用户' if getattr(m, 'role', '') == 'user' else '助手'}: {(getattr(m, 'content', '') or '')[:SUMMARY_TRANSCRIPT_PER_MESSAGE]}\n"
        if len(buf) + len(piece) > SUMMARY_TRANSCRIPT_MAX_CHARS and buf:
            chunks.append(buf)
            buf = ""
        buf += piece
    if buf:
        chunks.append(buf)
    partials = []
    for i, chunk in enumerate(chunks, 1):
        part = await _fork_summary_agent(
            _summary_prompt(chunk, max_tokens=reserve // 2), reserve // 2)
        if part:
            partials.append(f"【第 {i}/{len(chunks)} 段】\n{part}")
    if not partials:
        return ""
    merged = "\n\n".join(partials)
    return await _fork_summary_agent(
        _summary_prompt(merged, prior_summary=prior, max_tokens=reserve), reserve)


def should_full_compact(session) -> bool:
    """L3 触发判定：水位 ≥93% 窗口，或 Auto 连续失败 ≥3 且水位仍 ≥95K。"""
    ctx = estimate_context_tokens(session)
    if ctx >= cw.full_compact_threshold():
        return True
    return (ctx >= cw.auto_compact_threshold()
            and cw.get_fail_streak(getattr(session, "session_id", "")) >= COMPACT_FAIL_STREAK_LIMIT)
