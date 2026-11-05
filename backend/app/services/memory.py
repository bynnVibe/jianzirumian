"""
见字如面 - 用户跨会话记忆服务

借鉴 Claude Code auto memory 的设计原理：
- 类型化记忆（preference/fact/goal/feedback），抽取侧输出 add/update/delete 动作，
  支持去重、纠正与遗忘，而非只增不改的追加日志；
- 两层加载：常驻「用户画像卡」（高重要度/偏好类，每会话必注入，类比 MEMORY.md 索引）
  + 相关检索层（关键词重叠 + 重要度 + 时间衰减 + 类型权重），两层合计受字符预算约束；
- 索引治理：超上限按「低重要度 + 长期未用」先遗忘；写入后对同类型高重叠簇做 LLM 合并。
记忆落库到 SQLite user_memories 表；敏感内容（密码/密钥/证件等）不落库。
"""
import json
import logging
import math
import re
import uuid
from datetime import datetime
from typing import List, Optional

from app.config import settings
from app.core import db

logger = logging.getLogger("jianziruyang.memory")

# 敏感信息关键词：命中时不落库，避免记录密码/密钥/证件号等
_SENSITIVE_PATTERNS = [
    re.compile(r"密码|password|token|api[_\s-]?key|身份证|银行卡|信用卡", re.I),
]

MAX_MEMORIES_PER_USER = 200
MAX_CONTEXT_MEMORIES = 5
# 记忆类型（对应 Claude Code auto memory 四类的本系统映射）
MEMORY_TYPES = ("preference", "fact", "goal", "feedback")
# 记忆类型中文展示名（供可观测性概览面板）
_TYPE_LABELS = {"preference": "偏好", "fact": "事实", "goal": "目标", "feedback": "纠正"}
# 类型权重：偏好与纠正类记忆的跨会话复用价值更高
_TYPE_WEIGHT = {"preference": 1.2, "feedback": 1.2, "fact": 1.0, "goal": 1.0}
# 查重/定位阈值：分词器含单字 token 且套话前缀重叠普遍，需关键词重叠与
# 序列相似度同时达标才视为同一条记忆（避免「高血压控盐/血糖控糖」这类误判）
_DUP_KEYWORD_OVERLAP = 5
_DUP_SIMILARITY = 0.7
# 合并簇阈值：同类型且关键词重叠达标的记忆 ≥3 条时触发 LLM 合并
_MERGE_KEYWORD_OVERLAP = 6
_MERGE_CLUSTER_SIZE = 3
# 画像卡：进入常驻层的重要度阈值与条数上限
_PROFILE_IMPORTANCE = 0.8
_PROFILE_MAX_ITEMS = 6
# 时间衰减参数（天）：decay = exp(-距上次使用天数 / 30)
_DECAY_DAYS = 30.0
# 从未使用过的记忆按该天数计衰减
_UNUSED_DAYS = 60.0


def _is_sensitive(text: str) -> bool:
    return any(p.search(text) for p in _SENSITIVE_PATTERNS)


def _tokenize_keywords(text: str) -> list[str]:
    """简单关键词提取：取中文二元片段与英文/数字词，用于记忆检索匹配。"""
    from app.core.sparse_retriever import tokenize

    return list(dict.fromkeys(tokenize(text)))[:40]


def _normalize_for_dedup(text: str) -> str:
    """去除「用户/该用户」类主语前缀，避免普遍前缀抬高查重相似度。"""
    return re.sub(r"^(这|该)?用户(通常|一般|平时)?", "", (text or "").strip())


def _content_similarity(a: str, b: str) -> float:
    """序列相似度（主语前缀归一化后的 difflib ratio），用于查重与 update 定位。

    不用字符集交集比：套话式中文（「…重要度…记忆…」）字符集重叠虚高，误判率高。
    """
    from difflib import SequenceMatcher

    a, b = _normalize_for_dedup(a), _normalize_for_dedup(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _days_since(iso: str) -> float:
    if not iso:
        return _UNUSED_DAYS
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return _UNUSED_DAYS
    return max(0.0, (datetime.now() - dt).total_seconds() / 86400.0)


def _clamp_importance(value) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


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
    def update_memory(
        user_id: str,
        memory_id: str,
        content: str = None,
        importance: float = None,
        memory_type: str = None,
    ) -> Optional[dict]:
        """更新一条记忆（用户编辑或抽取侧 update 动作）；content 命中敏感词时拒绝。"""
        row = db.query_one(
            "SELECT * FROM user_memories WHERE id = ? AND user_id = ?",
            (memory_id, user_id),
        )
        if not row:
            return None
        content = (content or "").strip()
        if content and _is_sensitive(content):
            return None
        mtype = memory_type if memory_type in MEMORY_TYPES else None
        now = datetime.now().isoformat()
        db.execute(
            "UPDATE user_memories SET content = ?, keywords = ?, memory_type = ?,"
            " importance = ?, updated_at = ? WHERE id = ?",
            (
                content or row["content"],
                json.dumps(_tokenize_keywords(content), ensure_ascii=False) if content else row["keywords"],
                mtype or row["memory_type"],
                _clamp_importance(importance) if importance is not None else row["importance"],
                now, memory_id,
            ),
        )
        return {"id": memory_id, "content": content or row["content"], "updated": True}

    @staticmethod
    def find_similar(user_id: str, content: str, memory_type: str = None) -> Optional[dict]:
        """查找与 content 视为同一条的已有记忆（关键词重叠 ≥5 且序列相似度 >0.7）。"""
        keywords = set(_tokenize_keywords(content))
        best, best_key = None, (0, 0.0)
        for m in MemoryService.list_memories(user_id):
            if memory_type and (m.get("memory_type") or "fact") != memory_type:
                continue
            overlap = len(keywords & set(m.get("keywords") or []))
            sim = _content_similarity(content, m.get("content") or "")
            if overlap >= _DUP_KEYWORD_OVERLAP and sim > _DUP_SIMILARITY:
                key = (overlap, sim)
                if key > best_key:
                    best, best_key = m, key
        return best

    @staticmethod
    def add_memory(
        user_id: str,
        content: str,
        memory_type: str = "fact",
        importance: float = 0.5,
        source_session_id: str = "",
        source_message_id: str = "",
        dedup: bool = True,
    ) -> Optional[dict]:
        """新增一条用户记忆（敏感内容会被拒绝）。

        dedup=True 时先查重：命中已有同条记忆则转为覆盖更新（借鉴 Claude 记忆
        「编辑而非追加」的治理方式），避免同一偏好重复堆积多条。
        """
        content = (content or "").strip()
        if not content or _is_sensitive(content):
            return None
        if memory_type not in MEMORY_TYPES:
            memory_type = "fact"
        if dedup:
            hit = MemoryService.find_similar(user_id, content)
            if hit:
                return MemoryService.update_memory(
                    user_id, hit["id"], content=content,
                    importance=_clamp_importance(importance), memory_type=memory_type,
                )

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
                source_session_id, source_message_id,
                _clamp_importance(importance), now, now,
            ),
        )
        # 超出上限时清理最旧、重要度最低、长期未用的记忆
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
        # 遗忘顺序：重要度升序 → 长期未用优先（last_used_at 空/旧者先忘）→ updated_at 旧者先删
        stale = db.query(
            "SELECT id FROM user_memories WHERE user_id = ?"
            " ORDER BY importance ASC, COALESCE(last_used_at, '') ASC, updated_at ASC LIMIT ?",
            (user_id, excess),
        )
        for row in stale:
            db.execute("DELETE FROM user_memories WHERE id = ?", (row["id"],))

    @staticmethod
    def profile_memories(user_id: str) -> List[dict]:
        """常驻画像卡：高重要度或偏好类记忆，每会话必注入（类比 Claude MEMORY.md 索引层）。

        按重要度降序取 ≤6 条，合计字符受 MEMORY_PROFILE_CHARS 预算约束。
        """
        if not user_id:
            return []
        rows = db.query(
            "SELECT * FROM user_memories WHERE user_id = ?"
            " AND (importance >= ? OR memory_type = 'preference')"
            " ORDER BY importance DESC, updated_at DESC LIMIT ?",
            (user_id, _PROFILE_IMPORTANCE, _PROFILE_MAX_ITEMS),
        )
        budget = int(getattr(settings, "MEMORY_PROFILE_CHARS", 300))
        chosen, used = [], 0
        for r in rows:
            content = r.get("content") or ""
            if used + len(content) > budget:
                continue
            r["keywords"] = json.loads(r.get("keywords") or "[]")
            chosen.append(r)
            used += len(content)
        return chosen

    @staticmethod
    def retrieve_relevant(
        user_id: str,
        query: str,
        limit: int = MAX_CONTEXT_MEMORIES,
        exclude_ids: Optional[list] = None,
    ) -> List[dict]:
        """相关检索层：关键词重叠 + 重要度 + 时间衰减 + 类型权重评分。

        exclude_ids 用于与画像卡去重（已在常驻层注入的不再重复注入）。
        """
        if not user_id:
            return []
        excluded = set(exclude_ids or [])
        memories = [m for m in MemoryService.list_memories(user_id) if m["id"] not in excluded]
        if not memories:
            return []
        query_tokens = set(_tokenize_keywords(query))

        def score(m: dict) -> float:
            overlap = len(query_tokens & set(m.get("keywords") or []))
            decay = math.exp(-_days_since(m.get("last_used_at") or m.get("updated_at")) / _DECAY_DAYS)
            weight = _TYPE_WEIGHT.get(m.get("memory_type") or "fact", 1.0)
            return overlap * 2 + (float(m.get("importance", 0.5)) + decay) * weight

        ranked = sorted(memories, key=score, reverse=True)
        # 关键词完全不重叠时，仅保留评分最高的少量记忆兜底，避免无关注入
        top = [m for m in ranked if query_tokens & set(m.get("keywords") or [])]
        chosen = (top or ranked)[:limit]
        if chosen:
            now = datetime.now().isoformat()
            db.executemany(
                "UPDATE user_memories SET last_used_at = ? WHERE id = ?",
                [(now, m["id"]) for m in chosen],
            )
        return chosen

    @staticmethod
    def usage_overview(user_id: str) -> dict:
        """记忆模块使用概览（供可观测性「当前用户」面板展示）。

        只读聚合，不改任何状态：条数/分类型/画像卡候选/检索命中/字符占用与预算，
        以及存储水位（相对 MAX_MEMORIES_PER_USER）。用户无记忆时返回全零结构。
        """
        empty = {
            "total": 0,
            "by_type": [{"type": t, "label": _TYPE_LABELS[t], "count": 0} for t in MEMORY_TYPES],
            "profile_count": 0,
            "retrieved_count": 0,
            "never_used_count": 0,
            "avg_importance": 0.0,
            "total_chars": 0,
            "profile_chars_budget": int(getattr(settings, "MEMORY_PROFILE_CHARS", 300)),
            "ctx_chars_budget": int(getattr(settings, "MEMORY_CTX_CHARS", 600)),
            "storage_limit": MAX_MEMORIES_PER_USER,
            "storage_usage": 0.0,
            "last_updated_at": None,
        }
        if not user_id or user_id in {"guest", "system", "-"}:
            return empty
        rows = db.query(
            "SELECT memory_type, content, importance, last_used_at, updated_at"
            " FROM user_memories WHERE user_id = ? LIMIT ?",
            (user_id, MAX_MEMORIES_PER_USER * 2),
        )
        if not rows:
            return empty
        by_type = {t: 0 for t in MEMORY_TYPES}
        profile_count = retrieved_count = never_used = total_chars = 0
        importance_sum = 0.0
        last_updated = ""
        for r in rows:
            mtype = r.get("memory_type") or "fact"
            by_type[mtype if mtype in by_type else "fact"] += 1
            content = r.get("content") or ""
            total_chars += len(content)
            importance_sum += float(r.get("importance") or 0.5)
            if float(r.get("importance") or 0) >= _PROFILE_IMPORTANCE or mtype == "preference":
                profile_count += 1
            if r.get("last_used_at"):
                retrieved_count += 1
            else:
                never_used += 1
            if (r.get("updated_at") or "") > last_updated:
                last_updated = r.get("updated_at") or ""
        total = len(rows)
        return {
            "total": total,
            "by_type": [{"type": t, "label": _TYPE_LABELS[t], "count": by_type[t]} for t in MEMORY_TYPES],
            "profile_count": profile_count,
            "retrieved_count": retrieved_count,
            "never_used_count": never_used,
            "avg_importance": round(importance_sum / total, 3) if total else 0.0,
            "total_chars": total_chars,
            "profile_chars_budget": int(getattr(settings, "MEMORY_PROFILE_CHARS", 300)),
            "ctx_chars_budget": int(getattr(settings, "MEMORY_CTX_CHARS", 600)),
            "storage_limit": MAX_MEMORIES_PER_USER,
            "storage_usage": round(total / MAX_MEMORIES_PER_USER, 4) if MAX_MEMORIES_PER_USER else 0.0,
            "last_updated_at": last_updated or None,
        }

    @staticmethod
    def apply_extraction(user_id: str, session_id: str, items: list) -> dict:
        """执行抽取侧输出的动作清单（add/update/delete），单轮上限 3 条。"""
        stats = {"add": 0, "update": 0, "delete": 0}
        for item in items[:3]:
            if not isinstance(item, dict):
                continue
            content = (item.get("content") or "").strip()
            action = (item.get("action") or "add").strip().lower()
            mtype = item.get("type") or "fact"
            if mtype not in MEMORY_TYPES:
                mtype = "fact"
            importance = _clamp_importance(item.get("importance", 0.5))
            hint = (item.get("target_hint") or content).strip()
            if action == "delete":
                hit = MemoryService.find_similar(user_id, hint)
                if hit:
                    MemoryService.delete_memory(user_id, hit["id"])
                    stats["delete"] += 1
                continue
            if action == "update":
                hit = MemoryService.find_similar(user_id, hint)
                if hit:
                    MemoryService.update_memory(
                        user_id, hit["id"], content=content or None,
                        importance=importance, memory_type=mtype,
                    )
                    stats["update"] += 1
                    continue
                # 定位不到目标时降级为新增（仍走查重）
            if not content:
                continue
            res = MemoryService.add_memory(
                user_id, content, memory_type=mtype, importance=importance,
                source_session_id=session_id,
            )
            if res:
                stats["update" if res.get("updated") else "add"] += 1
        return stats

    @staticmethod
    async def _merge_cluster(user_id: str, memory_type: str):
        """同类型高重叠簇合并：≥3 条且关键词重叠 ≥5 时 LLM 合并为一条，失败静默。"""
        rows = [m for m in MemoryService.list_memories(user_id)
                if (m.get("memory_type") or "fact") == memory_type]
        if len(rows) < _MERGE_CLUSTER_SIZE:
            return
        clusters, used = [], set()
        for m in rows:
            if m["id"] in used:
                continue
            group, kw = [m], set(m.get("keywords") or [])
            used.add(m["id"])
            for other in rows:
                if other["id"] in used:
                    continue
                if len(kw & set(other.get("keywords") or [])) >= _MERGE_KEYWORD_OVERLAP:
                    group.append(other)
                    used.add(other["id"])
            if len(group) >= _MERGE_CLUSTER_SIZE:
                clusters.append(group)
        if not clusters:
            return
        try:
            from app.core.llm import LLMFactory

            llm = LLMFactory.create_for_role("memory_extract")
        except Exception as e:
            logger.warning("记忆合并初始化失败（忽略）: %s", e)
            return
        for group in clusters:
            try:
                listing = "\n".join(f"{i + 1}. {m['content']}" for i, m in enumerate(group))
                prompt = (
                    f"以下是同一用户的多条同类型（{memory_type}）记忆，内容高度重叠。"
                    "请合并为一条记忆：保留全部不重复信息点、去除冗余表述，不要新增信息。"
                    "仅输出 JSON 对象 {\"content\": str, \"importance\": 0~1}。\n\n" + listing
                )
                collected = ""
                async for chunk in llm.chat([{"role": "user", "content": prompt}], stream=False):
                    collected += chunk
                match = re.search(r"\{.*\}", collected, re.S)
                if not match:
                    continue
                merged = json.loads(match.group(0))
                content = (merged.get("content") or "").strip()
                if not content or _is_sensitive(content):
                    continue
                target, rest = group[0], group[1:]
                MemoryService.update_memory(
                    user_id, target["id"], content=content,
                    importance=_clamp_importance(merged.get("importance", target.get("importance"))),
                )
                for m in rest:
                    MemoryService.delete_memory(user_id, m["id"])
                logger.info("用户 %s 记忆合并：%d 条 → 1 条（type=%s）", user_id, len(group), memory_type)
            except Exception as e:
                logger.warning("记忆合并失败（忽略）: %s", e)

    @staticmethod
    async def extract_from_turn(user_id: str, session_id: str, user_message, assistant_message):
        """对话结束后，调用 LLM 抽取候选记忆并执行动作（失败静默跳过，不影响主流程）"""
        if not user_id or user_id == "guest":
            return
        try:
            from app.core.llm import LLMFactory

            # 记忆抽取为后台辅助生成，走网关默认规则（role 仅用于日志显性化）
            llm = LLMFactory.create_for_role("memory_extract")
            prompt = (
                "从下面这一轮对话中，提取用户值得长期记住的个人信息。写入门禁："
                "只记「未来对话有用」的内容（偏好/禁忌、健康或个人事实、目标/计划、对回答方式的纠正）；"
                "不记一次性提问、本轮上下文即可推导的内容与密码/密钥/身份证等敏感信息。"
                "若用户纠正或覆盖了此前已记住的内容，输出 action=update 并用 target_hint 描述原记忆；"
                "若用户明确要求遗忘，输出 action=delete 并用 target_hint 描述目标。"
                "type 取值：preference/fact/goal/feedback。仅输出 JSON 数组，每项 "
                "{\"content\": str, \"importance\": 0~1, \"type\": str, "
                "\"action\": \"add|update|delete\", \"target_hint\": str}；没有则输出 []。\n\n"
                f"用户: {user_message[:500]}\n助手: {assistant_message[:500]}"
            )
            collected = ""
            async for chunk in llm.chat([{"role": "user", "content": prompt}], stream=False):
                collected += chunk
            match = re.search(r"\[.*\]", collected, re.S)
            if not match:
                return
            items = json.loads(match.group(0))
            if not isinstance(items, list):
                return
            stats = MemoryService.apply_extraction(user_id, session_id, items)
            if sum(stats.values()):
                logger.info("用户 %s 记忆抽取动作: %s", user_id, stats)
            # 写入后对涉及类型做同类型高重叠簇合并（索引治理）
            touched = {it.get("type") or "fact" for it in items[:3] if isinstance(it, dict)}
            for mtype in touched:
                if mtype in MEMORY_TYPES:
                    await MemoryService._merge_cluster(user_id, mtype)
        except Exception as e:
            logger.warning("记忆抽取失败（忽略，不影响对话）: %s", e)


memory_service = MemoryService()
