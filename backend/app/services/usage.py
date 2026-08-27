"""
见字如面 - 用量统计服务
按用户按天聚合查询次数与字符消耗（Token 估算 = 字符数 / 1.5）
"""
from datetime import datetime, timedelta

from app.core import db

# 中文场景经验值：约 1.5 个字符对应 1 个 Token
CHARS_PER_TOKEN = 1.5


def estimate_tokens(chars: int) -> int:
    """字符数估算为 Token 数"""
    return int(round(chars / CHARS_PER_TOKEN))


def record_usage(
    user_id: str,
    prompt_chars: int,
    completion_chars: int,
    use_knowledge: bool = False,
    use_search: bool = False,
):
    """记录一次查询的用量（usage_daily 按天 upsert 累加）

    use_knowledge / use_search 分别累加知识库检索、联网搜索次数。
    """
    uid = user_id or "guest"
    day = datetime.now().strftime("%Y-%m-%d")
    db.execute(
        "INSERT INTO usage_daily"
        " (user_id, day, query_count, prompt_chars, completion_chars,"
        "  knowledge_count, web_search_count)"
        " VALUES (?, ?, 1, ?, ?, ?, ?)"
        " ON CONFLICT(user_id, day) DO UPDATE SET"
        " query_count = query_count + 1,"
        " prompt_chars = prompt_chars + excluded.prompt_chars,"
        " completion_chars = completion_chars + excluded.completion_chars,"
        " knowledge_count = knowledge_count + excluded.knowledge_count,"
        " web_search_count = web_search_count + excluded.web_search_count",
        (
            uid, day, prompt_chars, completion_chars,
            1 if use_knowledge else 0,
            1 if use_search else 0,
        ),
    )


def record_upload(user_id: str, count: int = 1):
    """记录一次笔记上传（仅累加 upload_count，不计为查询）"""
    uid = user_id or "guest"
    day = datetime.now().strftime("%Y-%m-%d")
    db.execute(
        "INSERT INTO usage_daily (user_id, day, query_count, upload_count)"
        " VALUES (?, ?, 0, ?)"
        " ON CONFLICT(user_id, day) DO UPDATE SET"
        " upload_count = upload_count + excluded.upload_count",
        (uid, day, count),
    )


def get_user_usage(user_id: str, days: int = 30) -> dict:
    """获取用户近 N 天用量：每日明细 + 总量（含知识库/联网/上传分类）"""
    uid = user_id or "guest"
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = db.query(
        "SELECT day, query_count, prompt_chars, completion_chars,"
        " knowledge_count, web_search_count, upload_count"
        " FROM usage_daily WHERE user_id = ? AND day >= ? ORDER BY day",
        (uid, since),
    )
    daily = [
        {
            "day": r["day"],
            "query_count": r["query_count"],
            "tokens": estimate_tokens(r["prompt_chars"] + r["completion_chars"]),
            "knowledge_count": r["knowledge_count"] or 0,
            "web_search_count": r["web_search_count"] or 0,
            "upload_count": r["upload_count"] or 0,
        }
        for r in rows
    ]
    total_queries = sum(r["query_count"] for r in rows)
    total_chars = sum(r["prompt_chars"] + r["completion_chars"] for r in rows)
    return {
        "daily": daily,
        "total_query_count": total_queries,
        "total_tokens": estimate_tokens(total_chars),
        "model_calls": total_queries,  # 每次查询对应一次 LLM 调用
        "total_knowledge_count": sum(r["knowledge_count"] or 0 for r in rows),
        "total_web_search_count": sum(r["web_search_count"] or 0 for r in rows),
        "total_upload_count": sum(r["upload_count"] or 0 for r in rows),
        "active_days": len(rows),
    }


def get_all_usage_summary(days: int = 30) -> list[dict]:
    """全部用户近 N 天用量汇总（看板用）"""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = db.query(
        "SELECT user_id, SUM(query_count) AS queries,"
        " SUM(prompt_chars + completion_chars) AS chars"
        " FROM usage_daily WHERE day >= ? GROUP BY user_id",
        (since,),
    )
    return [
        {
            "user_id": r["user_id"],
            "query_count": r["queries"],
            "tokens": estimate_tokens(r["chars"] or 0),
        }
        for r in rows
    ]
