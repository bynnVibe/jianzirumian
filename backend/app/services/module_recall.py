"""
见字如面 - 记忆 / Skill 召回使用与效果度量

回答两个问题：
  1) 记忆与 Skill 配置后，日常对话中是否真的被「召回并注入」了？
  2) 召回之后，回答效果是否真的改善了？

做法：
  - 召回度量：chat 主流程在注入记忆/技能时调用 record_turn_usage，把本轮注入的
    画像记忆条数、相关记忆条数、技能字符数随助手消息落库（turn_module_usage）。
  - 效果度量：以 message_feedbacks（like/dislike）为效果信号，按「该轮是否召回记忆 /
    是否注入技能」分组统计点赞率，用「召回组点赞率 - 未召回组点赞率」的差值近似
    衡量模块对回答满意度的边际贡献（观察性指标，非因果结论）。
"""
import logging
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Optional

from app.core import db

logger = logging.getLogger("jianziruyang.module_recall")

# 本轮对话累积的模块召回量（注入时 note、助手消息落库时 flush），请求级隔离
_pending: ContextVar[Optional[dict]] = ContextVar("module_recall_pending", default=None)


def note_turn_modules(
    user_id: str,
    session_id: str,
    memory_profile_n: int = 0,
    memory_relevant_n: int = 0,
    skill_chars: int = 0,
    memory_chars: int = 0,
) -> None:
    """在记忆/技能注入点记录本轮召回量，待助手消息落库时随 message_id 一起写库。"""
    _pending.set({
        "user_id": user_id,
        "session_id": session_id,
        "memory_profile_n": int(memory_profile_n or 0),
        "memory_relevant_n": int(memory_relevant_n or 0),
        "skill_chars": int(skill_chars or 0),
        "memory_chars": int(memory_chars or 0),
    })


def flush_turn_modules(message_id: str) -> None:
    """助手消息落库后调用：把本轮召回量绑定到 message_id 写库并清空上下文。"""
    pending = _pending.get()
    _pending.set(None)
    if not pending or not message_id:
        return
    record_turn_usage(
        message_id,
        pending.get("session_id") or "",
        pending.get("user_id") or "",
        memory_profile_n=pending.get("memory_profile_n", 0),
        memory_relevant_n=pending.get("memory_relevant_n", 0),
        skill_chars=pending.get("skill_chars", 0),
        memory_chars=pending.get("memory_chars", 0),
    )


def _now() -> str:
    return datetime.now().isoformat()


def record_turn_usage(
    message_id: str,
    session_id: str,
    user_id: str,
    memory_profile_n: int = 0,
    memory_relevant_n: int = 0,
    skill_chars: int = 0,
    memory_chars: int = 0,
) -> None:
    """记录一轮对话中记忆/技能的召回注入情况（失败静默，不影响主流程）。"""
    try:
        db.execute(
            "INSERT INTO turn_module_usage (id, message_id, session_id, user_id,"
            " memory_profile_n, memory_relevant_n, skill_chars, memory_chars, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()), message_id, session_id, user_id,
                int(memory_profile_n or 0), int(memory_relevant_n or 0),
                int(skill_chars or 0), int(memory_chars or 0), _now(),
            ),
        )
    except Exception as e:  # 度量埋点不允许影响对话主流程
        logger.warning("记录模块召回用量失败: %s", e)


def _like_rate(rows) -> Optional[float]:
    """由 (rating, count) 行计算点赞率；无反馈返回 None。"""
    like = sum(c for r, c in rows if r == "like")
    dislike = sum(c for r, c in rows if r == "dislike")
    total = like + dislike
    return round(like / total, 4) if total else None


def recall_overview(user_id: str, days: int = 30) -> dict:
    """聚合指定用户近 N 天的记忆/技能召回率与效果对照。"""
    if not user_id or user_id in {"guest", "system", "-"}:
        return {"enabled": False, "total_turns": 0}
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    rows = db.query(
        "SELECT memory_profile_n, memory_relevant_n, skill_chars, memory_chars"
        " FROM turn_module_usage WHERE user_id = ? AND created_at >= ?",
        (user_id, cutoff),
    )
    total = len(rows)
    mem_turns = sum(1 for r in rows if (r["memory_profile_n"] + r["memory_relevant_n"]) > 0)
    skill_turns = sum(1 for r in rows if r["skill_chars"] > 0)

    def _group(flag_expr: str):
        grouped = db.query(
            f"SELECT CASE WHEN {flag_expr} THEN 1 ELSE 0 END AS recalled, f.rating AS rating,"
            " COUNT(*) AS c FROM turn_module_usage t"
            " JOIN message_feedbacks f ON f.message_id = t.message_id"
            " WHERE t.user_id = ? AND t.created_at >= ?"
            " GROUP BY recalled, f.rating",
            (user_id, cutoff),
        )
        with_fb = [(g["rating"], g["c"]) for g in grouped if g["recalled"] == 1]
        without_fb = [(g["rating"], g["c"]) for g in grouped if g["recalled"] == 0]
        return _like_rate(with_fb), _like_rate(without_fb)

    mem_like_with, mem_like_without = _group("(memory_profile_n + memory_relevant_n) > 0")
    skill_like_with, skill_like_without = _group("skill_chars > 0")

    def _delta(a: Optional[float], b: Optional[float]) -> Optional[float]:
        if a is None or b is None:
            return None
        return round(a - b, 4)

    return {
        "enabled": True,
        "days": days,
        "total_turns": total,
        "memory": {
            "recall_turns": mem_turns,
            "recall_rate": round(mem_turns / total, 4) if total else 0.0,
            "like_rate_with": mem_like_with,
            "like_rate_without": mem_like_without,
            "effect_delta": _delta(mem_like_with, mem_like_without),
        },
        "skill": {
            "apply_turns": skill_turns,
            "apply_rate": round(skill_turns / total, 4) if total else 0.0,
            "like_rate_with": skill_like_with,
            "like_rate_without": skill_like_without,
            "effect_delta": _delta(skill_like_with, skill_like_without),
        },
    }
