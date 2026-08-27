"""
见字如面 - 用户跨会话记忆服务

从对话中抽取用户的长期偏好/事实/目标，落库到 SQLite user_memories 表；
聊天前按关键词检索相关记忆注入 LLM 上下文，实现跨会话记忆能力。
"""
import json
import logging
import re
import uuid
from datetime import datetime
from typing import List, Optional

from app.core import db

logger = logging.getLogger("jianziruyang.memory")

# 敏感信息关键词：命中时不落库，避免记录密码/密钥/证件号等
_SENSITIVE_PATTERNS = [
    re.compile(r"密码|password|token|api[_\s-]?key|身份证|银行卡|信用卡", re.I),
]

MAX_MEMORIES_PER_USER = 200
MAX_CONTEXT_MEMORIES = 5


def _is_sensitive(text: str) -> bool:
    return any(p.search(text) for p in _SENSITIVE_PATTERNS)


def _tokenize_keywords(text: str) -> list[str]:
    """简单关键词提取：取中文二元片段与英文/数字词，用于记忆检索匹配。"""
    from app.core.sparse_retriever import tokenize

    return list(dict.fromkeys(tokenize(text)))[:20]


class MemoryService:
    @staticmethod
    def list_memories(user_id: str) -> List[dict]:
        rows = db.query(
            "SELECT * FROM user_memories WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
            (user_id, MAX_MEMORIES_PER_USER),
        )
        for r in rows:
            r["keywords"] = json.loads(r.get("keywords") or "[]")
        return rows

    @staticmethod
    def delete_memory(user_id: str, memory_id: str) -> bool:
        return db.execute(
            "DELETE FROM user_memories WHERE id = ? AND user_id = ?",
            (memory_id, user_id),
        ) > 0

    @staticmethod
    def add_memory(
        user_id: str,
        content: str,
        memory_type: str = "fact",
        importance: float = 0.5,
        source_session_id: str = "",
        source_message_id: str = "",
    ) -> Optional[dict]:
        """新增一条用户记忆（敏感内容会被拒绝）"""
        content = (content or "").strip()
        if not content or _is_sensitive(content):
            return None

        now = datetime.now().isoformat()
        keywords = _tokenize_keywords(content)
        memory_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO user_memories (id, user_id, memory_type, content, keywords,"
            " source_session_id, source_message_id, importance, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                memory_id, user_id, memory_type, content,
                json.dumps(keywords, ensure_ascii=False),
                source_session_id, source_message_id, importance, now, now,
            ),
        )
        # 超出上限时清理最旧、重要度最低的记忆
        MemoryService._trim(user_id)
        return {"id": memory_id, "content": content, "memory_type": memory_type}

    @staticmethod
    def _trim(user_id: str):
        count_row = db.query_one(
            "SELECT COUNT(*) AS c FROM user_memories WHERE user_id = ?", (user_id,)
        )
        if not count_row or count_row["c"] <= MAX_MEMORIES_PER_USER:
            return
        excess = count_row["c"] - MAX_MEMORIES_PER_USER
        stale = db.query(
            "SELECT id FROM user_memories WHERE user_id = ?"
            " ORDER BY importance ASC, updated_at ASC LIMIT ?",
            (user_id, excess),
        )
        for row in stale:
            db.execute("DELETE FROM user_memories WHERE id = ?", (row["id"],))

    @staticmethod
    def retrieve_relevant(user_id: str, query: str, limit: int = MAX_CONTEXT_MEMORIES) -> List[dict]:
        """按关键词重叠度 + 重要度 + 时间新鲜度检索相关记忆"""
        if not user_id:
            return []
        memories = MemoryService.list_memories(user_id)
        if not memories:
            return []
        query_tokens = set(_tokenize_keywords(query))

        def score(m: dict) -> float:
            overlap = len(query_tokens & set(m.get("keywords") or []))
            return overlap * 2 + float(m.get("importance", 0.5))

        ranked = sorted(memories, key=score, reverse=True)
        # 关键词完全不重叠时，仅保留重要度最高的少量记忆兜底，避免无关注入
        top = [m for m in ranked if score(m) > float(m.get("importance", 0.5))]
        chosen = (top or ranked)[:limit]
        if chosen:
            now = datetime.now().isoformat()
            db.executemany(
                "UPDATE user_memories SET last_used_at = ? WHERE id = ?",
                [(now, m["id"]) for m in chosen],
            )
        return chosen

    @staticmethod
    async def extract_from_turn(user_id: str, session_id: str, user_message, assistant_message):
        """对话结束后，调用 LLM 抽取候选记忆（失败静默跳过，不影响主流程）"""
        if not user_id or user_id == "guest":
            return
        try:
            from app.core.llm import LLMFactory

            llm = LLMFactory.create()
            prompt = (
                "从下面这一轮对话中，提取用户值得长期记住的、明确的个人事实/偏好/目标（如饮食禁忌、"
                "健康状况、学习目标等）。仅输出 JSON 数组，每项 {\"content\": str, \"importance\": 0~1}；"
                "没有则输出 []。不要提取密码、密钥、身份证等敏感信息。\n\n"
                f"用户: {user_message[:500]}\n助手: {assistant_message[:500]}"
            )
            collected = ""
            async for chunk in llm.chat([{"role": "user", "content": prompt}], stream=False):
                collected += chunk
            match = re.search(r"\[.*\]", collected, re.S)
            if not match:
                return
            items = json.loads(match.group(0))
            for item in items[:3]:
                content = (item.get("content") or "").strip()
                if content:
                    MemoryService.add_memory(
                        user_id, content,
                        importance=float(item.get("importance", 0.5)),
                        source_session_id=session_id,
                    )
        except Exception as e:
            logger.warning("记忆抽取失败（忽略，不影响对话）: %s", e)


memory_service = MemoryService()
