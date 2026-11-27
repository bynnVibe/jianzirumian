"""
见字如面 - Agent 分域记忆服务（五层记忆架构 L4，类比 Claude Code Agent Memory）

与用户记忆层（L2 user_memories）正交：按 (user_id, agent_key) 双条件行级隔离，
每个助手（chat / wiki_assistant）拥有独立记忆域，互不串味。
- wiki_assistant 域：助手专属记忆（常分析文档主题、分析风格偏好），不记用户个人健康/身份
  事实——那归用户记忆层 L2；
- chat 域：本轮仅建表与 API，抽取仍走用户层（避免双写漂移），为演进预留位（见设计文档）。

复用 memory.py 的同源函数（分词/查重/敏感词/截断/新鲜度渲染），不复制检索逻辑；
每用户每 agent 上限 50 条，超限按「低重要度 + 长期未用」裁剪。失败静默降级，不阻断主链路。
"""
import json
import logging
import math
import uuid
from datetime import datetime
from typing import List, Optional

from app.config import settings
from app.core import db
# 同源复用用户记忆层的工具函数与常量（单一事实源，避免逻辑漂移）
from app.services.memory import (
    MEMORY_TYPES,
    _TYPE_WEIGHT,
    _DECAY_DAYS,
    _DUP_KEYWORD_OVERLAP,
    _DUP_SIMILARITY,
    _LOCATE_RELAX_SIMILARITY,
    _LOCATE_RELAX_OVERLAP,
    _LOCATE_MIN_SIMILARITY,
    _LOCATE_DELETE_SIMILARITY,
    _LOCATE_DELETE_OVERLAP,
    _LOCATE_DELETE_MIN_SIMILARITY,
    _tokenize_keywords,
    _content_similarity,
    _is_sensitive,
    _normalize_type,
    _clamp_importance,
    _clamp_confidence,
    _days_since,
    _parse_extraction_items,
    memory_line,
)

logger = logging.getLogger("jianziruyang.agent_memory")

# 每用户每 agent 域的记忆条数上限
MAX_AGENT_MEMORIES = 50
# 合法 agent 域（与前端下拉、API 校验同源）
AGENT_KEYS = ("chat", "wiki_assistant")
# 相关召回条数上限与注入字符预算（wiki 问答前注入）
MAX_CONTEXT_MEMORIES = 5
AGENT_CTX_CHARS = 200
# 画像/常驻层重要度阈值（助手域偏好类优先）
_PROFILE_IMPORTANCE = 0.8


def _valid_agent(agent_key: str) -> bool:
    return agent_key in AGENT_KEYS


def _sync_mirror(user_id: str, agent_key: str, action: str):
    """双写：助手分域 markdown 镜像重生成 + 写路径观测事件（文件为持久记录）。"""
    from app.services import memory_store

    memory_store.sync_agent_memory(user_id, agent_key)
    memory_store.log_memory_event(user_id, action, "agent", detail=agent_key)


class AgentMemoryService:
    @staticmethod
    def list_memories(user_id: str, agent_key: str) -> List[dict]:
        if not user_id or not _valid_agent(agent_key):
            return []
        rows = db.query(
            "SELECT * FROM agent_memories WHERE user_id = ? AND agent_key = ?"
            " ORDER BY updated_at DESC LIMIT ?",
            (user_id, agent_key, MAX_AGENT_MEMORIES),
        )
        for r in rows:
            r["keywords"] = json.loads(r.get("keywords") or "[]")
        return rows

    @staticmethod
    def delete_memory(user_id: str, agent_key: str, memory_id: str) -> bool:
        # 行级隔离：user_id + agent_key 双条件，杜绝跨域/越权删除
        ok = db.execute(
            "DELETE FROM agent_memories WHERE id = ? AND user_id = ? AND agent_key = ?",
            (memory_id, user_id, agent_key),
        ) > 0
        if ok:
            _sync_mirror(user_id, agent_key, "delete")
        return ok

    @staticmethod
    def update_memory(
        user_id: str,
        agent_key: str,
        memory_id: str,
        content: str = None,
        importance: float = None,
        memory_type: str = None,
        confidence: float = None,
    ) -> Optional[dict]:
        """更新一条助手域记忆（敏感内容拒绝）；行级隔离 user_id + agent_key。"""
        if not _valid_agent(agent_key):
            return None
        row = db.query_one(
            "SELECT * FROM agent_memories WHERE id = ? AND user_id = ? AND agent_key = ?",
            (memory_id, user_id, agent_key),
        )
        if not row:
            return None
        content = (content or "").strip()
        if content and _is_sensitive(content):
            return None
        mtype = _normalize_type(memory_type) if memory_type else None
        now = datetime.now().isoformat()
        db.execute(
            "UPDATE agent_memories SET content = ?, keywords = ?, memory_type = ?,"
            " importance = ?, confidence = ?, updated_at = ?"
            " WHERE id = ? AND user_id = ? AND agent_key = ?",
            (
                content or row["content"],
                json.dumps(_tokenize_keywords(content), ensure_ascii=False) if content else row["keywords"],
                mtype or row["memory_type"],
                _clamp_importance(importance) if importance is not None else row["importance"],
                _clamp_confidence(confidence) if confidence is not None else _clamp_confidence(row.get("confidence")),
                now, memory_id, user_id, agent_key,
            ),
        )
        _sync_mirror(user_id, agent_key, "update")
        return {"id": memory_id, "content": content or row["content"], "updated": True}

    @staticmethod
    def find_similar(user_id: str, agent_key: str, content: str, memory_type: str = None) -> Optional[dict]:
        """域内查重（关键词重叠 ≥5 且序列相似度 >0.7，与用户层同源阈值）。"""
        keywords = set(_tokenize_keywords(content))
        best, best_key = None, (0, 0.0)
        for m in AgentMemoryService.list_memories(user_id, agent_key):
            if memory_type and (m.get("memory_type") or "user") != memory_type:
                continue
            overlap = len(keywords & set(m.get("keywords") or []))
            sim = _content_similarity(content, m.get("content") or "")
            if overlap >= _DUP_KEYWORD_OVERLAP and sim > _DUP_SIMILARITY:
                key = (overlap, sim)
                if key > best_key:
                    best, best_key = m, key
        return best

    @staticmethod
    def _locate_for_action(user_id: str, agent_key: str, hint: str,
                           memory_type: str = None, for_delete: bool = False) -> Optional[dict]:
        """update/delete 目标定位：严格查重未命中时按放宽阈值回退（与用户层同源口径）。"""
        hit = AgentMemoryService.find_similar(user_id, agent_key, hint, memory_type)
        if hit:
            return hit
        hint = (hint or "").strip()
        if not hint:
            return None
        keywords = set(_tokenize_keywords(hint))
        best, best_key = None, (0, 0.0)
        for m in AgentMemoryService.list_memories(user_id, agent_key):
            if memory_type and (m.get("memory_type") or "user") != memory_type:
                continue
            overlap = len(keywords & set(m.get("keywords") or []))
            sim = _content_similarity(hint, m.get("content") or "")
            if for_delete:
                ok = (sim > _LOCATE_DELETE_SIMILARITY
                      or (overlap >= _LOCATE_DELETE_OVERLAP and sim > _LOCATE_DELETE_MIN_SIMILARITY))
            else:
                ok = (sim > _LOCATE_RELAX_SIMILARITY
                      or (overlap >= _LOCATE_RELAX_OVERLAP and sim > _LOCATE_MIN_SIMILARITY))
            if ok and (overlap, sim) > best_key:
                best, best_key = m, (overlap, sim)
        return best

    @staticmethod
    def add_memory(
        user_id: str,
        agent_key: str,
        content: str,
        memory_type: str = "user",
        importance: float = 0.5,
        source_session_id: str = "",
        dedup: bool = True,
    ) -> Optional[dict]:
        """新增一条助手域记忆（敏感内容拒绝；dedup 命中转更新并置信度 +1）。"""
        if not user_id or not _valid_agent(agent_key):
            return None
        content = (content or "").strip()
        if not content or _is_sensitive(content):
            return None
        memory_type = _normalize_type(memory_type)
        if dedup:
            hit = AgentMemoryService.find_similar(user_id, agent_key, content)
            if hit:
                return AgentMemoryService.update_memory(
                    user_id, agent_key, hit["id"], content=content,
                    importance=_clamp_importance(importance), memory_type=memory_type,
                    confidence=_clamp_confidence(float(hit.get("confidence") or 1.0) + 1),
                )
        now = datetime.now().isoformat()
        keywords = _tokenize_keywords(content)
        memory_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO agent_memories (id, user_id, agent_key, memory_type, content, keywords,"
            " importance, confidence, source_session_id, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                memory_id, user_id, agent_key, memory_type, content,
                json.dumps(keywords, ensure_ascii=False),
                _clamp_importance(importance), 1.0, source_session_id, now, now,
            ),
        )
        _sync_mirror(user_id, agent_key, "add")
        AgentMemoryService._trim(user_id, agent_key)
        return {"id": memory_id, "content": content, "memory_type": memory_type}

    @staticmethod
    def _trim(user_id: str, agent_key: str):
        count_row = db.query_one(
            "SELECT COUNT(*) AS c FROM agent_memories WHERE user_id = ? AND agent_key = ?",
            (user_id, agent_key),
        )
        if not count_row or count_row["c"] <= MAX_AGENT_MEMORIES:
            return
        excess = count_row["c"] - MAX_AGENT_MEMORIES
        stale = db.query(
            "SELECT id FROM agent_memories WHERE user_id = ? AND agent_key = ?"
            " ORDER BY importance ASC, COALESCE(last_used_at, '') ASC, updated_at ASC LIMIT ?",
            (user_id, agent_key, excess),
        )
        for row in stale:
            db.execute("DELETE FROM agent_memories WHERE id = ?", (row["id"],))
        if stale:
            _sync_mirror(user_id, agent_key, "forget")

    @staticmethod
    def retrieve_relevant(
        user_id: str,
        agent_key: str,
        query: str,
        limit: int = MAX_CONTEXT_MEMORIES,
        exclude_ids: Optional[list] = None,
    ) -> List[dict]:
        """助手域相关召回：关键词重叠 + 重要度 + 时间衰减 + 类型权重 + 置信度（与用户层同构）。"""
        if not user_id or not _valid_agent(agent_key):
            return []
        excluded = set(exclude_ids or [])
        memories = [m for m in AgentMemoryService.list_memories(user_id, agent_key) if m["id"] not in excluded]
        if not memories:
            return []
        query_tokens = set(_tokenize_keywords(query))

        def score(m: dict) -> float:
            overlap = len(query_tokens & set(m.get("keywords") or []))
            decay = math.exp(-_days_since(m.get("last_used_at") or m.get("updated_at")) / _DECAY_DAYS)
            weight = _TYPE_WEIGHT.get(m.get("memory_type") or "user", 1.0)
            conf = 0.1 * _clamp_confidence(m.get("confidence"))
            return overlap * 2 + (float(m.get("importance", 0.5)) + decay) * weight + conf

        ranked = sorted(memories, key=score, reverse=True)
        top = [m for m in ranked if query_tokens & set(m.get("keywords") or [])]
        chosen = (top or ranked)[:limit]
        if chosen:
            now = datetime.now().isoformat()
            db.executemany(
                "UPDATE agent_memories SET last_used_at = ? WHERE id = ?",
                [(now, m["id"]) for m in chosen],
            )
        return chosen

    @staticmethod
    def context_text(user_id: str, agent_key: str, query: str, budget_chars: int = AGENT_CTX_CHARS) -> str:
        """构建助手域注入文本（受字符预算约束，新鲜度渲染复用 memory_line）。"""
        if not user_id or not _valid_agent(agent_key):
            return ""
        lines, used = [], 0
        for m in AgentMemoryService.retrieve_relevant(user_id, agent_key, query):
            line = memory_line(m)
            if used + len(line) > budget_chars:
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines)

    @staticmethod
    def apply_extraction(user_id: str, agent_key: str, session_id: str, items: list) -> dict:
        """执行助手域抽取动作清单（add/update/delete），单轮上限 3 条。"""
        stats = {"add": 0, "update": 0, "delete": 0}
        if not _valid_agent(agent_key):
            return stats
        for item in items[:3]:
            if not isinstance(item, dict):
                continue
            content = (item.get("content") or "").strip()
            action = (item.get("action") or "add").strip().lower()
            mtype = _normalize_type(item.get("type") or "user")
            importance = _clamp_importance(item.get("importance", 0.5))
            hint = (item.get("target_hint") or content).strip()
            if action == "delete":
                hit = AgentMemoryService._locate_for_action(user_id, agent_key, hint, for_delete=True)
                if hit:
                    AgentMemoryService.delete_memory(user_id, agent_key, hit["id"])
                    stats["delete"] += 1
                continue
            if action == "update":
                hit = AgentMemoryService._locate_for_action(user_id, agent_key, hint)
                if hit:
                    AgentMemoryService.update_memory(
                        user_id, agent_key, hit["id"], content=content or None,
                        importance=importance, memory_type=mtype,
                        confidence=_clamp_confidence(float(hit.get("confidence") or 1.0) + 1),
                    )
                    stats["update"] += 1
                    continue
            if not content:
                continue
            res = AgentMemoryService.add_memory(
                user_id, agent_key, content, memory_type=mtype, importance=importance,
                source_session_id=session_id,
            )
            if res:
                stats["update" if res.get("updated") else "add"] += 1
        return stats

    @staticmethod
    async def _llm_extract(user_message: str, assistant_message: str):
        """助手域 LLM 抽取：返回 (items, reason)，解析与用户层同源（避免逻辑漂移）。"""
        try:
            from app.core.llm import LLMFactory

            llm = LLMFactory.create_for_role("memory_agent")
        except Exception as e:
            logger.warning("助手域记忆抽取初始化失败（忽略）: %s", e)
            return [], "llm_unavailable"
        prompt = (
            "从下面这一轮「文档助手」对话中，提取该助手值得长期记住的专属信息（仅助手域）。"
            "只记：用户常分析的文档主题/领域、对分析风格与输出格式的偏好、反复出现的分析诉求。"
            "明确不记：用户个人健康/身份/疾病等事实（那归用户记忆层，不归助手域）、"
            "一次性提问、可从文档或上下文直接推导的内容、密码/密钥/证件等敏感信息。"
            "若用户纠正了此前的分析偏好，输出 action=update 并用 target_hint 描述原记忆；"
            "若要求遗忘，输出 action=delete。type 取值：user/feedback/project/reference。"
            "content 用第三人称（「用户…」）陈述，不要写成问句。"
            "仅输出 JSON 数组，每项 {\"content\": str, \"importance\": 0~1, \"type\": str, "
            "\"action\": \"add|update|delete\", \"target_hint\": str}；确实无可保存内容才输出 []。\n\n"
            f"用户: {user_message[:500]}\n助手: {assistant_message[:500]}"
        )
        try:
            collected = ""
            async for chunk in llm.chat([{"role": "user", "content": prompt}], stream=False):
                collected += chunk
        except Exception as e:
            logger.warning("助手域记忆抽取调用失败（忽略，不影响问答）: %s", e)
            return [], "llm_error"
        items, reason = _parse_extraction_items(collected)
        if reason == "parse_error":
            logger.warning("助手域记忆抽取输出无法解析（已忽略）: %s", (collected or "")[:200])
        return items[:3], reason

    @staticmethod
    async def extract_from_turn(user_id: str, agent_key: str, session_id: str, user_message, assistant_message):
        """助手域后台抽取：健壮解析 + 每轮落「抽取运行」事件，返回统计 dict。

        wiki_assistant 专属：只记助手相关偏好/主题，明确不记用户个人健康/身份事实
        （那归用户记忆层 L2，避免双写漂移）。模型判空/不可用/解析失败不再静默——
        reason 写进运行事件 detail，使「助手域无记忆」可被解释。异常不影响问答主链路。
        """
        from app.services import memory_store

        result = {"ran": False, "llm": 0, "reason": "", "add": 0, "update": 0, "delete": 0}
        if not user_id or user_id in {"guest", "system", "-"} or not _valid_agent(agent_key):
            result["reason"] = "no_user"
            return result
        user_text = (user_message or "").strip()
        if not user_text:
            result["reason"] = "empty_turn"
            return result
        result["ran"] = True
        items, reason = await AgentMemoryService._llm_extract(user_text, (assistant_message or "").strip())
        result["llm"] = len(items)
        result["reason"] = reason
        stats = AgentMemoryService.apply_extraction(user_id, agent_key, session_id, items)
        result.update(stats)
        detail = (f"{agent_key}:模型{len(items)}条→"
                  f"新增{stats['add']}更新{stats['update']}删除{stats['delete']}")
        if reason:
            detail += f"（模型层:{reason}）"
        memory_store.log_memory_event(user_id, "extract", "agent", detail=detail)
        if sum(stats.values()):
            logger.info("用户 %s 助手 %s 域记忆抽取动作: %s（%s）", user_id, agent_key, stats, detail)
        return result


agent_memory_service = AgentMemoryService()