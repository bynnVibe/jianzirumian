"""
见字如面 - 知识收藏服务
按用户隔离，持久化到 SQLite (backend/data/app.db)
"""
import json
import logging
import secrets
import time
from typing import Optional

from app.core import db

logger = logging.getLogger("jianziruyang.bookmark")


def _row_to_bookmark(row: dict) -> dict:
    """数据库行 → 业务 dict（JSON 字段反序列化）"""
    from app.services.records import upload_records

    source_info = json.loads(row["source_info"] or "{}")
    # 历史收藏的来源信息回填预览 PDF 路径，保证收藏页同样显示原文跳转链接
    if source_info and source_info.get("source_image"):
        upload_records.enrich_sources([source_info])
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "title": row["title"],
        "tags": json.loads(row["tags"] or "[]"),
        "content": row["content"],
        "source_type": row["source_type"],
        "source_info": source_info,
        "created_at": row["created_at"],
    }


def create_bookmark(
    user_id: str,
    title: str,
    tags: list[str],
    content: str,
    source_type: str = "knowledge",  # "answer" | "knowledge"
    source_info: Optional[dict] = None,
) -> dict:
    """
    创建收藏。
    user_id: 所属用户
    title: 收藏标题
    tags: 标签列表
    content: 收藏的文本内容
    source_type: 来源类型 answer=AI回答 knowledge=知识片段
    source_info: 来源附加信息（如知识库来源图片等）
    """
    now = time.time()
    bookmark = {
        "id": str(secrets.token_hex(16)),
        "user_id": user_id,
        "title": title.strip() or "未命名收藏",
        "tags": [t.strip() for t in tags if t.strip()],
        "content": content,
        "source_type": source_type,
        "source_info": source_info or {},
        "created_at": now,
    }
    db.execute(
        "INSERT INTO bookmarks (id, user_id, title, tags, content, source_type, source_info, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (
            bookmark["id"], user_id, bookmark["title"],
            json.dumps(bookmark["tags"], ensure_ascii=False),
            content, source_type,
            json.dumps(source_info or {}, ensure_ascii=False),
            now,
        ),
    )
    logger.info("用户 %s 创建收藏: %s", user_id[:8], bookmark["title"])
    return bookmark


def list_bookmarks(user_id: str) -> list[dict]:
    """获取指定用户的所有收藏，按时间降序"""
    rows = db.query(
        "SELECT * FROM bookmarks WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    )
    return [_row_to_bookmark(r) for r in rows]


def get_bookmark(bookmark_id: str) -> Optional[dict]:
    """获取单条收藏（调用方需校验 user_id）"""
    row = db.query_one("SELECT * FROM bookmarks WHERE id = ?", (bookmark_id,))
    return _row_to_bookmark(row) if row else None


def delete_bookmark(user_id: str, bookmark_id: str) -> bool:
    """删除收藏（仅限本人）"""
    affected = db.execute(
        "DELETE FROM bookmarks WHERE id = ? AND user_id = ?",
        (bookmark_id, user_id),
    )
    if affected:
        logger.info("用户 %s 删除收藏: %s", user_id[:8], bookmark_id[:8])
    return affected > 0
