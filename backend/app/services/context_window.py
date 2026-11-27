"""
见字如面 - 会话上下文窗口与四层压缩编排（对齐 Claude Code）

一个会话 = 一个上下文窗口（CHAT_CONTEXT_WINDOW，名义 128K=128000，K=1000）。
四层压缩按「触发频率越来越低、代价越来越高」排列，只作用于本会话窗口：

  L0 日常约束（每次模型调用/每个工具结果，零模型调用）
    - Output Cap：每轮输出上限（llm.py 透传 max_tokens，见 LLM_OUTPUT_CAP_TOKENS）
    - Tool-Result Offloading：超大工具结果落盘，上下文只留约 2KB 预览 + 路径，可回读
    - Deduplication：同会话重复结果替换为简短引用，消除冗余 token
  L1 微压缩 Micro Compaction（每次 API 调用前，零模型调用）
    - 可重现工具结果合计 ≥ 20K 进入；可释放 < 10K 放弃；旧结果改占位符，配对不变
  L2 系统压缩（Session Memory 后台记 summary.md；Auto Compaction ≥ 95K 替换历史）
  L3 兜底压缩 Full Compaction（Auto 连续失败 / ≥ 93% 窗口 / 上游超长）

本模块负责：工具结果账本（session_tool_results）、会话窗口状态（session_context_state）、
L0 落盘/去重/回读、L1 微压缩、历史装配（原文保留 + 工具配对）、压缩边界与保留区计算。
摘要生成（九段式）与 summary.md 读写仍由 session_summary 承担；transcript.jsonl 只读不改写。
"""
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from app.config import settings
from app.core import db
from app.core import observability as obs
from app.services import memory_store

logger = logging.getLogger("jianziruyang.context_window")

# 可重现工具白名单（对应 CC 的 Read/Bash/Grep/Glob/WebSearch/WebFetch）：
# 这些工具的结果可重新调用或回读获得，微压缩可安全清除。未登记工具默认不可重现。
REPRODUCIBLE_TOOLS = {
    "search_knowledge", "knowledge_retrieve", "web_search", "url_fetch", "fetch_url",
    "attachment_ocr", "image_ocr", "document_import", "knowledge_summarize",
    "find_skill_material", "read_tool_result",
}
# 永不清除（不可重现）：草稿/HITL/有副作用/子 agent 输出/Task State。
NON_REPRODUCIBLE_TOOLS = {
    "save_personal_skill", "tool_confirmation", "clarify", "need_search",
    "image_upload", "tool_workflow", "plan", "decision", "skill_distill",
}


# ---------------------------------------------------------------------------
# 量纲与阈值（均由窗口推导，K=1000）
# ---------------------------------------------------------------------------

def context_window() -> int:
    return int(getattr(settings, "CHAT_CONTEXT_WINDOW", 128000))


def output_cap() -> int:
    return int(getattr(settings, "LLM_OUTPUT_CAP_TOKENS", 4000))


def auto_compact_threshold() -> int:
    """Auto Compaction 触发阈值 = 窗口 − 摘要输出预留 − 输入安全缓冲（128K→95K）。"""
    reserve = int(getattr(settings, "COMPACT_SUMMARY_RESERVE_TOKENS", 20000))
    buffer = int(getattr(settings, "COMPACT_INPUT_BUFFER_TOKENS", 13000))
    return max(0, context_window() - reserve - buffer)


def full_compact_threshold() -> int:
    """Full Compaction 触发阈值 = 窗口 × 93%（128K→119040）。"""
    ratio = float(getattr(settings, "FULL_COMPACT_WINDOW_RATIO", 0.93))
    return int(context_window() * ratio)


def thresholds() -> dict:
    """供前端上下文用量弹层展示的阈值刻度（量纲统一 K=1000）。"""
    return {
        "window": context_window(),
        "output_cap": output_cap(),
        "micro_trigger": int(getattr(settings, "MICRO_COMPACT_TRIGGER_TOKENS", 20000)),
        "auto": auto_compact_threshold(),
        "full": full_compact_threshold(),
        "summary_reserve": int(getattr(settings, "COMPACT_SUMMARY_RESERVE_TOKENS", 20000)),
    }


# ---------------------------------------------------------------------------
# 会话窗口状态（session_context_state）
# ---------------------------------------------------------------------------

def get_state(session_id: str) -> dict:
    if not session_id:
        return {}
    return db.query_one(
        "SELECT * FROM session_context_state WHERE session_id = ?", (session_id,)) or {}


def _ensure_state(session_id: str, user_id: str = ""):
    if not session_id:
        return
    db.execute(
        "INSERT INTO session_context_state (session_id, user_id, updated_at)"
        " VALUES (?,?,?) ON CONFLICT(session_id) DO NOTHING",
        (session_id, user_id or "", datetime.now().isoformat()))


def set_watermark(session_id: str, user_id: str, ctx_tokens: int,
                  tool_result_tokens: Optional[int] = None):
    """记录会话真实水位（主链路最近一次模型调用的完整 prompt token）。"""
    _ensure_state(session_id, user_id)
    if tool_result_tokens is None:
        db.execute(
            "UPDATE session_context_state SET ctx_tokens=?, user_id=?, updated_at=?"
            " WHERE session_id=?",
            (int(ctx_tokens), user_id or "", datetime.now().isoformat(), session_id))
    else:
        db.execute(
            "UPDATE session_context_state SET ctx_tokens=?, tool_result_tokens=?, user_id=?,"
            " updated_at=? WHERE session_id=?",
            (int(ctx_tokens), int(tool_result_tokens), user_id or "",
             datetime.now().isoformat(), session_id))


def add_tool_calls(session_id: str, user_id: str, n: int = 1):
    """累加「距上次摘要」的工具调用计数（Session Memory 后续触发条件之一）。"""
    if n <= 0:
        return
    _ensure_state(session_id, user_id)
    db.execute(
        "UPDATE session_context_state SET tool_calls_since_summary = tool_calls_since_summary + ?,"
        " updated_at=? WHERE session_id=?",
        (int(n), datetime.now().isoformat(), session_id))


def reset_tool_calls(session_id: str):
    db.execute(
        "UPDATE session_context_state SET tool_calls_since_summary=0 WHERE session_id=?",
        (session_id,))


def get_fail_streak(session_id: str) -> int:
    return int(get_state(session_id).get("auto_fail_streak") or 0)


def note_auto_fail(session_id: str, user_id: str) -> int:
    _ensure_state(session_id, user_id)
    n = get_fail_streak(session_id) + 1
    db.execute(
        "UPDATE session_context_state SET auto_fail_streak=?, updated_at=? WHERE session_id=?",
        (n, datetime.now().isoformat(), session_id))
    memory_store.log_compact_event(session_id, user_id, "auto_fail", detail=f"连续失败 {n} 次")
    return n


def reset_fail_streak(session_id: str):
    db.execute(
        "UPDATE session_context_state SET auto_fail_streak=0 WHERE session_id=?", (session_id,))


def set_last_compact(session_id: str, kind: str):
    db.execute(
        "UPDATE session_context_state SET last_compact_kind=?, last_compact_at=? WHERE session_id=?",
        (kind, datetime.now().isoformat(), session_id))


# ---------------------------------------------------------------------------
# 工具结果账本（session_tool_results）：L0 落盘 / 去重 / 回读
# ---------------------------------------------------------------------------

def _next_seq(session_id: str) -> int:
    row = db.query_one(
        "SELECT COALESCE(MAX(seq), 0) AS m FROM session_tool_results WHERE session_id=?",
        (session_id,))
    return int((row or {}).get("m") or 0) + 1


def turn_seq_for_session(session_id: str) -> int:
    """当前轮次序号 = 会话内用户消息条数（一轮=一问一答）。"""
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM chat_messages WHERE session_id=? AND role='user'",
        (session_id,))
    return int((row or {}).get("n") or 0)


def make_preview(content: str, byte_limit: int = 0) -> str:
    """约 2KB 预览：UTF-8 安全、尽量按行截断，尾部标注原长。"""
    limit = byte_limit or int(getattr(settings, "TOOL_RESULT_PREVIEW_BYTES", 2048))
    text = content or ""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    cut = raw[:limit].decode("utf-8", errors="ignore")
    nl = cut.rfind("\n")
    if nl > limit // 2:
        cut = cut[:nl]
    return cut + f"\n…（预览截断，原文共 {len(text)} 字符）"


def _tool_results_dir(session_id: str, user_id: str) -> Optional[Path]:
    d = memory_store.session_dir(session_id, user_id)
    return d / "tool-results" if d else None


def offload_content(session_id: str, user_id: str, result_id: str, content: str) -> str:
    """把超大结果原文写入会话隔离目录，返回相对路径（供上下文回执与回读）。"""
    d = _tool_results_dir(session_id, user_id)
    if d is None:
        return ""
    try:
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{result_id}.txt"
        p.write_text(content or "", encoding="utf-8")
        return f"users/{user_id or 'legacy'}/sessions/{session_id}/tool-results/{result_id}.txt"
    except OSError as e:
        logger.warning("工具结果落盘失败（降级为截断）: %s", e)
        return ""


def _find_duplicate(session_id: str, content_hash: str, boundary_message_id: str) -> Optional[dict]:
    """结果级去重：同会话、内容哈希相同、且被引用目标仍在窗口内（live，位于压缩边界之后）。"""
    if not content_hash:
        return None
    rows = db.query(
        "SELECT * FROM session_tool_results WHERE session_id=? AND content_hash=?"
        " AND state='live' ORDER BY seq DESC", (session_id, content_hash))
    for r in rows:
        if _in_window(r, boundary_message_id):
            return r
    return None


def _boundary_index(session_id: str, boundary_message_id: str) -> int:
    """压缩边界在 chat_messages 中的 seq 上界（边界之后的消息仍在窗口）。无边界返回 -1。"""
    if not boundary_message_id:
        return -1
    row = db.query_one(
        "SELECT seq FROM chat_messages WHERE message_id=?", (boundary_message_id,))
    return int(row["seq"]) if row else -1


def _in_window(row: dict, boundary_message_id: str) -> bool:
    """工具结果是否仍在当前窗口（其所属消息 seq 大于压缩边界）。"""
    bidx = _boundary_index(row.get("session_id", ""), boundary_message_id)
    if bidx < 0:
        return True
    mid = row.get("message_id") or ""
    if not mid:
        return True
    mrow = db.query_one("SELECT seq FROM chat_messages WHERE message_id=?", (mid,))
    return bool(mrow) and int(mrow["seq"]) > bidx


def record_tool_result(session_id: str, user_id: str, tool_name: str, args, content: str,
                       *, message_id: str = "", turn_seq: int = 0,
                       reproducible: Optional[bool] = None,
                       boundary_message_id: str = "") -> dict:
    """登记一个工具结果到账本：去重 → 落盘判定 → 入库。返回账本行（含渲染所需字段）。

    - 结果级去重：内容哈希命中窗口内 live 结果 → state=dedup_ref，不重复载入正文；
    - 落盘：单结果 > TOOL_RESULT_OFFLOAD_TOKENS → 写盘，storage=offloaded，仅留预览+路径；
    - reproducible 缺省按白名单判定（未登记工具默认不可重现）。
    """
    content = content or ""
    args = args or {}
    args_json = json.dumps(args, ensure_ascii=False, sort_keys=True)
    args_sig = hashlib.sha256((tool_name + args_json).encode()).hexdigest()[:16]
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    raw_tokens = obs.estimate_tokens(content)
    if reproducible is None:
        reproducible = tool_name in REPRODUCIBLE_TOOLS and tool_name not in NON_REPRODUCIBLE_TOOLS
    rid = str(uuid.uuid4())
    seq = _next_seq(session_id)
    turn_seq = turn_seq or turn_seq_for_session(session_id)
    now = datetime.now().isoformat()

    dup = _find_duplicate(session_id, content_hash, boundary_message_id)
    state, ref_id = "live", ""
    if dup and dup.get("id") != rid:
        state, ref_id = "dedup_ref", dup["id"]

    offload_limit = int(getattr(settings, "TOOL_RESULT_OFFLOAD_TOKENS", 8000))
    row = {
        "id": rid, "session_id": session_id, "user_id": user_id or "",
        "message_id": message_id or "", "turn_seq": int(turn_seq), "seq": seq,
        "tool_name": tool_name, "args_json": args_json, "args_sig": args_sig,
        "content_hash": content_hash, "tokens": int(raw_tokens),
        "raw_tokens": int(raw_tokens), "reproducible": 1 if reproducible else 0,
        "storage": "inline", "path": "", "preview": "",
        "content": content, "state": state, "ref_id": ref_id,
        "created_at": now, "cleared_at": "",
    }
    db.execute(
        "INSERT INTO session_tool_results (id, session_id, user_id, message_id, turn_seq, seq,"
        " tool_name, args_json, args_sig, content_hash, tokens, raw_tokens, reproducible,"
        " storage, path, preview, content, state, ref_id, created_at, cleared_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, session_id, user_id or "", message_id or "", int(turn_seq), seq, tool_name,
         args_json, args_sig, content_hash, int(raw_tokens), int(raw_tokens),
         1 if reproducible else 0, "inline", "", "", content, state, ref_id, now, ""))
    if state == "dedup_ref":
        memory_store.log_compact_event(session_id, user_id, "dedup",
                                       before_tokens=raw_tokens, after_tokens=row["tokens"],
                                       detail=f"与 #{ref_id[:8]} 内容相同")
        obs.record_context_action("dedup", raw_tokens, row["tokens"],
                                  f"{tool_name} 与 #{ref_id[:8]} 内容相同",
                                  {"ref_id": ref_id, "tool": tool_name})
    elif raw_tokens > offload_limit:
        # 单个结果超阈值 → 落盘，只留预览 + 路径
        row = _offload_row(session_id, user_id, row)
    else:
        # 本轮合计超预算 → 从最大的结果开始落盘（含本条）
        row = _enforce_turn_budget(session_id, user_id, row)
    return row


def _offload_row(session_id: str, user_id: str, row: dict) -> dict:
    """把一条 inline 结果转为落盘回执（写盘 + 2KB 预览 + 账本更新），返回更新后的行。"""
    content = row.get("content") or ""
    path = offload_content(session_id, user_id, row["id"], content)
    if not path:
        return row  # 落盘失败：降级为原样载入，不阻断主流程
    preview = make_preview(content)
    tokens = obs.estimate_tokens(preview) + 40  # 预览 + 回执提示
    db.execute(
        "UPDATE session_tool_results SET storage='offloaded', path=?, preview=?, content='',"
        " tokens=? WHERE id=?", (path, preview, int(tokens), row["id"]))
    memory_store.log_compact_event(session_id, user_id, "offload",
                                   before_tokens=int(row.get("raw_tokens") or 0),
                                   after_tokens=int(tokens),
                                   detail=f"{row.get('tool_name', '')} 落盘 {path}")
    obs.record_context_action("offload", int(row.get("raw_tokens") or 0), int(tokens),
                              f"{row.get('tool_name', '')} 落盘 {path}",
                              {"path": path, "tool": row.get("tool_name", ""),
                               "raw_tokens": int(row.get("raw_tokens") or 0)})
    return {**row, "storage": "offloaded", "path": path, "preview": preview,
            "content": "", "tokens": int(tokens)}


def _enforce_turn_budget(session_id: str, user_id: str, row: dict) -> dict:
    """L0 单轮工具结果合计预算：超出时从最大的 inline 结果开始落盘，直到回到预算内。"""
    budget = int(getattr(settings, "TOOL_RESULT_TURN_BUDGET_TOKENS", 16000))
    if budget <= 0:
        return row
    turn = int(row.get("turn_seq") or 0)
    rows = [r for r in session_tool_rows(session_id)
            if r.get("state") == "live" and int(r.get("turn_seq") or 0) == turn
            and r.get("storage") == "inline"]
    total = sum(int(r.get("tokens") or 0) for r in rows)
    if total <= budget:
        return row
    updated = row
    for r in sorted(rows, key=lambda x: int(x.get("tokens") or 0), reverse=True):
        if total <= budget:
            break
        new = _offload_row(session_id, user_id, r)
        total -= int(r.get("tokens") or 0) - int(new.get("tokens") or 0)
        if r["id"] == row["id"]:
            updated = new
    return updated


def render_call(row: dict) -> str:
    """工具调用行（助手侧），与结果行配对，保留 tool_use_id。"""
    args = row.get("args_json") or "{}"
    return f"[工具调用 #{row['id']}] {row.get('tool_name', '')} {args}"


def render_result(row: dict) -> str:
    """工具结果行（用户侧）：按 state/storage 渲染正文/预览+路径/引用/占位符。"""
    rid = row.get("id", "")
    state = row.get("state", "live")
    tool = row.get("tool_name", "")
    if state == "dedup_ref":
        ref = row.get("ref_id", "")
        return (f"[工具结果 #{rid}] 与 #{ref}（{tool}）内容相同，未重复载入；"
                f"如需可用 read_tool_result(\"{ref}\") 回读。")
    if state == "cleared":
        args_summary = (row.get("args_json") or "{}")[:80]
        path = row.get("path") or ""
        hint = f'，或 read_tool_result("{rid}")' if path else "，可重新调用该工具"
        return (f"[工具结果 #{rid} 已清除] {tool}({args_summary}) · "
                f"原 {row.get('raw_tokens', 0)} tok{hint}")
    if row.get("storage") == "offloaded":
        return (f"[工具结果 #{rid}] （结果过大已落盘，原 {row.get('raw_tokens', 0)} tok）\n"
                f"{row.get('preview', '')}\n"
                f"…完整内容见 {row.get('path', '')}，可用 read_tool_result(\"{rid}\") 回读。")
    return f"[工具结果 #{rid}]\n{row.get('content', '')}"


def get_result(result_id: str) -> Optional[dict]:
    return db.query_one("SELECT * FROM session_tool_results WHERE id=?", (result_id,))


def read_tool_result(session_id: str, user_id: str, result_id: str,
                     offset: int = 0, limit: int = 0) -> str:
    """回读工具：分页读取本会话落盘结果原文。路径穿越/越权校验，单次 ≤ 页上限。

    只能读当前会话且属于本人的落盘目录；返回原文片段 + 分页提示。
    """
    row = get_result(result_id)
    if not row or row.get("session_id") != session_id:
        return "未找到该工具结果（仅限本会话）。"
    if user_id and row.get("user_id") and row["user_id"] != user_id:
        return "无权读取该工具结果。"
    content = row.get("content") or ""
    if row.get("storage") == "offloaded":
        d = _tool_results_dir(session_id, user_id or row.get("user_id", ""))
        p = (d / f"{result_id}.txt") if d else None
        # 路径穿越校验：解析后必须仍在会话 tool-results 目录内
        if p is None or not p.is_file() or not p.resolve().is_relative_to(d.resolve()):
            return "落盘文件不可用。"
        content = memory_store._read(p)
    page = limit or int(getattr(settings, "TOOL_RESULT_READ_PAGE_TOKENS", 4000))
    # 以字符近似 token 分页（CJK≈1:1），保证单次不超页上限
    start = max(0, int(offset))
    chunk = content[start:start + page]
    if not chunk:
        return "（已读到末尾，无更多内容）"
    tail = "" if start + page >= len(content) else f"\n…（第 {start}–{start + len(chunk)} 字符，共 {len(content)}，可继续 offset={start + page}）"
    return chunk + tail


def session_tool_rows(session_id: str) -> List[dict]:
    return db.query(
        "SELECT * FROM session_tool_results WHERE session_id=? ORDER BY seq", (session_id,))


def live_tool_result_tokens(session_id: str, boundary_message_id: str = "") -> int:
    """窗口内 live 工具结果渲染后合计 token（微压缩进入条件口径）。"""
    total = 0
    for r in session_tool_rows(session_id):
        if r.get("state") == "live" and _in_window(r, boundary_message_id):
            total += int(r.get("tokens") or 0)
    return total


# ---------------------------------------------------------------------------
# L1 微压缩 Micro Compaction
# ---------------------------------------------------------------------------

def micro_compact(session_id: str, user_id: str, current_turn_seq: int = 0,
                  boundary_message_id: str = "") -> Optional[dict]:
    """每次 API 调用前运行：可重现工具结果合计 ≥ 20K 进入；可释放 < 10K 放弃。

    候选 = 可重现 且 live 且 不在保护集（本轮 + 最近 N 个）。清除前确保原文落盘，
    状态写回账本（cleared），调用/结果配对不变。返回 {cleared_ids, freed_tokens, placeholders}。
    """
    trigger = int(getattr(settings, "MICRO_COMPACT_TRIGGER_TOKENS", 20000))
    min_free = int(getattr(settings, "MICRO_COMPACT_MIN_FREE_TOKENS", 10000))
    keep_recent = int(getattr(settings, "MICRO_COMPACT_KEEP_RECENT", 3))
    rows = [r for r in session_tool_rows(session_id)
            if r.get("state") == "live" and _in_window(r, boundary_message_id)]
    total = sum(int(r.get("tokens") or 0) for r in rows)
    if total < trigger:
        return None
    # 保护集：本轮结果 + 最近 keep_recent 个（按 seq 倒序）
    protected = {r["id"] for r in rows if int(r.get("turn_seq") or 0) == current_turn_seq}
    for r in sorted(rows, key=lambda x: int(x.get("seq") or 0), reverse=True)[:keep_recent]:
        protected.add(r["id"])
    candidates = [r for r in rows if r.get("reproducible") and r["id"] not in protected]
    # 可释放量 = Σ(原渲染 − 占位符)
    freed = 0
    for r in candidates:
        placeholder_tokens = obs.estimate_tokens(render_result({**r, "state": "cleared"}))
        freed += max(0, int(r.get("tokens") or 0) - placeholder_tokens)
    if freed < min_free:
        memory_store.log_compact_event(session_id, user_id, "micro_skip",
                                       detail=f"可释放 {freed} < {min_free}，放弃")
        return None
    cleared_ids, placeholders = [], {}
    now = datetime.now().isoformat()
    for r in candidates:
        # 清除前确保原文落盘（inline 结果也写盘，供回读）
        path = r.get("path") or ""
        if not path:
            content = r.get("content") or ""
            path = offload_content(session_id, user_id, r["id"], content)
        placeholder = render_result({**r, "state": "cleared", "path": path})
        db.execute(
            "UPDATE session_tool_results SET state='cleared', path=?, cleared_at=? WHERE id=?",
            (path, now, r["id"]))
        cleared_ids.append(r["id"])
        placeholders[r["id"]] = placeholder
    # 悬空引用降级：指向被清除目标的 dedup_ref 同步降级为占位符
    for r in session_tool_rows(session_id):
        if r.get("state") == "dedup_ref" and r.get("ref_id") in cleared_ids:
            db.execute(
                "UPDATE session_tool_results SET state='cleared', cleared_at=? WHERE id=?",
                (now, r["id"]))
            cleared_ids.append(r["id"])
            placeholders[r["id"]] = render_result({**r, "state": "cleared"})
    memory_store.log_compact_event(session_id, user_id, "micro",
                                   before_tokens=total, after_tokens=total - freed,
                                   detail=f"清除 {len(cleared_ids)} 个可重现结果，释放 {freed} tok")
    obs.record_context_action("micro_compact", total, total - freed,
                              f"清除 {len(cleared_ids)} 个可重现结果",
                              {"cleared": len(cleared_ids), "freed": freed})
    return {"cleared_ids": cleared_ids, "freed_tokens": freed, "placeholders": placeholders}


def record_fastpath_context(session_id: str, user_id: str, tool_name: str, query: str,
                            blocks: List[tuple], *, message_id: str = "",
                            boundary_message_id: str = "") -> str:
    """快路径知识/联网上下文入账：片段级去重 + 超大落盘，返回渲染后的上下文文本。

    blocks = [(key, header, body)]：key 为知识 result_key / 联网 URL（片段级去重口径），
    header 为来源编号行（如「[来源 1]（标题）：」），去重/落盘只替换 body，编号保留，
    使回答中的 [来源 N] 引用核对不受影响。每个片段登记为一条账本记录（可重现），
    下一轮起作为追加式历史出现，并可被 L1 微压缩清除。
    """
    if not session_id or not blocks:
        return "\n---\n".join(f"{h}\n{b}" for _, h, b in blocks)
    parts: List[str] = []
    for key, header, body in blocks:
        try:
            row = record_tool_result(
                session_id, user_id, tool_name, {"result_key": key, "query": (query or "")[:200]},
                body or "", message_id=message_id, boundary_message_id=boundary_message_id)
        except Exception as e:
            logger.warning("快路径上下文入账失败（降级为原样载入）: %s", e)
            parts.append(f"{header}\n{body}")
            continue
        state, storage = row.get("state"), row.get("storage")
        if state == "dedup_ref":
            ref = row.get("ref_id", "")
            rendered = (f"（与 #{ref} 内容相同，未重复载入；"
                        f"如需可用 read_tool_result(\"{ref}\") 回读）")
        elif storage == "offloaded":
            rendered = (f"（内容过大已落盘，原 {row.get('raw_tokens', 0)} tok）\n"
                        f"{row.get('preview', '')}\n"
                        f"…完整内容见 {row.get('path', '')}，"
                        f"可用 read_tool_result(\"{row.get('id', '')}\") 回读。")
        else:
            rendered = body or ""
        parts.append(f"{header}\n{rendered}")
    return "\n---\n".join(parts)


def apply_micro_to_messages(messages: List[dict], placeholders: dict) -> int:
    """把已清除结果在「已装配消息列表」中的渲染同步为占位符，返回改写条数。

    历史区每轮由账本重新装配，天然拿到最新渲染；但原生 Function Calling 循环
    复用同一份 messages 跨多轮模型调用，需要就地同步（配对与 tool_call_id 不变）。
    """
    if not placeholders:
        return 0
    n = 0
    for i, m in enumerate(messages):
        content = m.get("content")
        if not isinstance(content, str) or not content:
            continue
        if m.get("role") == "tool":
            try:
                payload = json.loads(content)
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict):
                rid = payload.get("result_id") or ""
                if not rid:
                    text = str(payload.get("text") or "")
                    rid = next((k for k in placeholders if f"#{k}]" in text), "")
                if rid in placeholders:
                    payload["text"] = placeholders[rid]
                    messages[i] = {**m, "content": json.dumps(payload, ensure_ascii=False)}
                    n += 1
                continue
        lines = content.split("\n")
        # 一条消息 = 一个工具结果（历史装配口径）：首行命中即整条替换为占位符，
        # 否则多行正文会残留在占位符之后，释放量落不到实处。
        head_rid = next((k for k in placeholders if lines[0].startswith(f"[工具结果 #{k}]")), "")
        if head_rid:
            messages[i] = {**m, "content": placeholders[head_rid]}
            n += 1
            continue
        changed = False
        for j, line in enumerate(lines):
            rid = next((k for k in placeholders if line.startswith(f"[工具结果 #{k}]")), "")
            if rid:
                lines[j] = placeholders[rid]
                changed = True
        if changed:
            messages[i] = {**m, "content": "\n".join(lines)}
            n += 1
    return n


def clear_all_tool_results(session_id: str, user_id: str) -> int:
    """Full Compaction：窗口内全部工具结果转落盘回执（不可重现也落盘保留预览+路径）。"""
    now = datetime.now().isoformat()
    n = 0
    for r in session_tool_rows(session_id):
        if r.get("state") == "cleared":
            continue
        path = r.get("path") or ""
        if not path:
            path = offload_content(session_id, user_id, r["id"], r.get("content") or "")
        preview = r.get("preview") or make_preview(r.get("content") or "")
        db.execute(
            "UPDATE session_tool_results SET state='cleared', storage='offloaded', path=?,"
            " preview=?, cleared_at=? WHERE id=?", (path, preview, now, r["id"]))
        n += 1
    return n


def recent_offload_receipts(session_id: str, n: int = 5) -> List[str]:
    """最近 N 条落盘回执路径（压缩后再水化提示模型可回读）。"""
    rows = db.query(
        "SELECT path FROM session_tool_results WHERE session_id=? AND path != ''"
        " ORDER BY seq DESC LIMIT ?", (session_id, n))
    return [r["path"] for r in rows if r.get("path")]


# ---------------------------------------------------------------------------
# 历史装配：原文保留 + 工具配对（前缀缓存友好）
# ---------------------------------------------------------------------------

def _assign_turns(messages: list) -> List[tuple]:
    """给消息标注轮次序号（每个 user 消息开启新一轮，1-based）。"""
    out, turn = [], 0
    for m in messages:
        if getattr(m, "role", "") == "user":
            turn += 1
        out.append((m, turn))
    return out


_SOURCE_LABEL_RE = re.compile(r"\[来源\s*(\d+)\]")


def _relabel_sources(text: str, turn: int) -> str:
    """历史区（往轮入账）工具结果的来源编号加轮次前缀（[来源 1] → [T2·来源 1]）。

    本轮尾部上下文（知识/联网注入）保持 [来源 X]，使回答中的引用核对只指向本轮；
    改写只依赖结果自身轮次，入账后渲染恒定（前缀缓存不变量：历史区逐字节稳定）。
    """
    if not text or turn <= 0:
        return text
    return _SOURCE_LABEL_RE.sub(lambda m: f"[T{turn}·来源 {m.group(1)}]", text)


def build_history_messages(session, summary_row: Optional[dict]) -> List[dict]:
    """构建会话历史区：压缩边界之后的原文 + 按轮次插入工具配对。

    - 无边界：全量历史原文（追加式，不做条数窗口/字符 clip）；
    - 有边界：边界之后的原文（此前历史由摘要替换，摘要 system 消息由调用方前置）；
    - 每轮工具交互以配对消息进入历史（助手 [工具调用 #id] + 用户 [工具结果 #id]）；
    - 历史区工具结果的来源编号改写为 [T{轮次}·来源 X]，避免与本轮尾部 [来源 X] 引用核对冲突；
    - 保留实时类助手答案入历史即衰减（位置无关规则）。
    无压缩事件时上一轮历史区是下一轮的严格前缀（前缀缓存不变量）。
    """
    messages = getattr(session, "messages", []) or []
    boundary = (summary_row or {}).get("boundary_message_id") or ""
    start = 0
    if boundary:
        for i, m in enumerate(messages):
            if getattr(m, "message_id", "") == boundary:
                start = i + 1
                break
    # 轮次按全会话绝对编号（与账本 turn_seq 同口径），再截取压缩边界之后的窗口
    assigned = _assign_turns(messages)
    window = assigned[start:]
    clip_old = int(getattr(settings, "CLIP_OLD_CHARS", 200))
    # 按轮次分组工具结果
    tools_by_turn: dict = {}
    for r in session_tool_rows(getattr(session, "session_id", "")):
        tools_by_turn.setdefault(int(r.get("turn_seq") or 0), []).append(r)
    result: List[dict] = []
    inserted_turns: set = set()
    tool_result_texts: List[str] = []
    for m, turn in window:
        role = getattr(m, "role", "user")
        content = getattr(m, "content", "") or ""
        # 实时类助手答案入历史即衰减（既有位置无关规则）
        if role == "assistant" and getattr(m, "is_realtime", False) and len(content) > clip_old:
            content = content[:clip_old] + f"\n…（实时答案已衰减，原长 {len(content)} 字符）"
        # 在该轮助手答案之前插入工具配对（工具发生在作答过程中）
        if role == "assistant" and turn not in inserted_turns:
            for r in tools_by_turn.get(turn, []):
                result.append({"role": "assistant", "content": render_call(r)})
                rendered = _relabel_sources(render_result(r), turn)
                result.append({"role": "user", "content": rendered})
                tool_result_texts.append(rendered)
            inserted_turns.add(turn)
        result.append({"role": role, "content": content})
    # 末尾若仍有未插入的工具轮次（如最后一轮只有工具无助手答案），补入
    for turn, rows in tools_by_turn.items():
        if turn not in inserted_turns and turn >= (window[0][1] if window else 1):
            for r in rows:
                result.append({"role": "assistant", "content": render_call(r)})
                rendered = _relabel_sources(render_result(r), turn)
                result.append({"role": "user", "content": rendered})
                tool_result_texts.append(rendered)
    # 历史工具结果单独成段计量（与对话原文分离，便于定位 Token 大户）
    if tool_result_texts:
        obs.note_segment("tool_results", "\n".join(tool_result_texts))
    return result


# 工具配对行的渲染前缀（历史段计量时剔除，归入 tool_results 段，避免重复计量）
_TOOL_LINE_PREFIXES = ("[工具调用 #", "[工具结果 #")


def history_segment_text(messages: List[dict]) -> str:
    """可观测 history 段口径：剔除 system 消息（摘要单独计量）与工具配对行
    （工具结果单独计入 tool_results 段），避免重复。"""
    return "\n".join(
        m.get("content", "") for m in messages
        if m.get("role") != "system"
        and not (m.get("content") or "").startswith(_TOOL_LINE_PREFIXES))


def compute_keep_boundary(session, summary_row: Optional[dict]) -> tuple:
    """计算 Auto Compaction 保留区边界：从尾部向前取，至少最近 N 轮且 ≥ 下限 token，上限封顶。

    返回 (boundary_message_id, keep_start_index)。不拆轮次：以 user 消息为轮次起点。
    """
    messages = getattr(session, "messages", []) or []
    keep_turns = int(getattr(settings, "AUTO_COMPACT_KEEP_RECENT_TURNS", 3))
    keep_min = int(getattr(settings, "AUTO_COMPACT_KEEP_MIN_TOKENS", 10000))
    keep_max = int(getattr(settings, "AUTO_COMPACT_KEEP_MAX_TOKENS", 40000))
    if not messages:
        return "", 0
    # 轮次起点索引（user 消息）
    starts = [i for i, m in enumerate(messages) if getattr(m, "role", "") == "user"]
    if not starts:
        return "", 0
    # 至少最近 keep_turns 轮
    keep_start = starts[-keep_turns] if len(starts) >= keep_turns else starts[0]
    # 扩展到 ≥ keep_min token（向前纳入更多轮次），但不超 keep_max
    def _tokens(idx):
        return sum(obs.estimate_tokens(getattr(m, "content", "") or "") for m in messages[idx:])
    si = starts.index(keep_start)
    while _tokens(keep_start) < keep_min and si > 0:
        si -= 1
        keep_start = starts[si]
    # 上限封顶：若保留区超 keep_max，向后收缩（但保证最近 keep_turns 轮优先）
    min_start = starts[-keep_turns] if len(starts) >= keep_turns else starts[0]
    while _tokens(keep_start) > keep_max and keep_start < min_start:
        si += 1
        keep_start = starts[si]
    boundary = getattr(messages[keep_start - 1], "message_id", "") if keep_start > 0 else ""
    return boundary, keep_start