"""
见字如面 - 长期记忆服务（跨会话沉淀，参考 Claude Code auto memory 设计）

长期记忆四类型（user/feedback/project/reference）+ MEMORY.md 索引：
- user：用户个人信息/偏好/事实（原 preference/fact 归并）；
- feedback：用户对回答方式的纠正与要求；
- project：用户进行中的目标/计划/任务（原 goal 归并）；
- reference：外部资源/位置指引（材料在哪、看哪份文档）。
抽取侧输出 add/update/delete 动作，支持去重、纠正与遗忘，而非只增不改的追加日志。

两个提取动作（均为后台 forked agent，每轮 query loop 完整结束后触发）：
- extractMemories：扫描该轮对话的整轮 transcript（用户反馈/纠正/信息），
  主动检测 hasMemoryWritesSince 过滤刚写过的内容，按四类型写新 topic 文件；
- AutoDream：独立后台子代理按固定 Prompt 整理记忆（合并/去重/晋升/裁剪），
  三重门控任一不满足立即退出：时间门（≥24h）/会话门（≥5 个新会话产生转录）/
  并发锁（.consolidate-lock PID 文件锁）。

两层加载：常驻「用户画像卡」（高重要度/user 类，每会话必注入，类比 MEMORY.md 索引）
+ 相关检索层（关键词重叠 + 重要度 + 时间衰减 + 类型权重），两层合计受字符预算约束；
索引治理：超上限按「低重要度 + 长期未用」先遗忘；写入后对同类型高重叠簇做 LLM 合并。
记忆以 markdown 文件为持久记录载体（memory_store：users/<uid>/memories/ 每记忆独立
topic .md + users/<uid>/MEMORY.md 索引），SQLite user_memories 表为查询镜像
（评分/检索/UI/观测），写路径双写；敏感内容（密码/密钥/证件等）不落库。
"""
import json
import logging
import math
import re
import time
import uuid
from datetime import datetime
from typing import List, Optional

from app.config import settings
from app.core import db
from app.services import memory_store

logger = logging.getLogger("jianziruyang.memory")

# 敏感信息关键词：命中时不落库，避免记录密码/密钥/证件号等
_SENSITIVE_PATTERNS = [
    re.compile(r"密码|password|token|api[_\s-]?key|身份证|银行卡|信用卡", re.I),
]

MAX_MEMORIES_PER_USER = 200
MAX_CONTEXT_MEMORIES = 5
# 长期记忆四类型（对齐 Claude Code auto memory：user/feedback/project/reference）
MEMORY_TYPES = ("user", "feedback", "project", "reference")
# 记忆类型中文展示名（供可观测性概览面板）
_TYPE_LABELS = {"user": "用户信息", "feedback": "反馈纠正", "project": "项目目标", "reference": "参考索引"}
# 类型权重：用户信息与反馈纠正类记忆的跨会话复用价值更高
_TYPE_WEIGHT = {"user": 1.2, "feedback": 1.2, "project": 1.0, "reference": 0.9}
# 旧类型兼容映射（存量 preference/fact/goal 在写入口归一化为新四类型）
_LEGACY_TYPE_MAP = {"preference": "user", "fact": "user", "goal": "project"}


def _normalize_type(memory_type: str) -> str:
    """类型归一化：旧类型映射到新四类型，未知类型兜底 user。"""
    t = (memory_type or "").strip()
    t = _LEGACY_TYPE_MAP.get(t, t)
    return t if t in MEMORY_TYPES else "user"
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
# 新鲜度警告阈值（天）：超过该天数未更新的记忆注入时附「时点观察」提醒，
# 防止陈旧记忆被当作当前事实（对齐 Claude Code memoryFreshnessText）。
_FRESHNESS_WARN_DAYS = 30.0
# 置信度上限：查重命中/用户确认后递增，召回评分因子（0.1*confidence）
_MAX_CONFIDENCE = 5.0
# AutoDream 三重门控：距上次 >=24h 且新增会话（产生转录）>=5 且 .consolidate-lock 空闲
_CONSOLIDATE_HOURS = 24.0
_CONSOLIDATE_SESSIONS = 5
# 主副互斥窗口（秒）：用户刚手动写入记忆后，后台抽取的 LLM 层在该窗口内跳过，
# 避免竞态重复写；确定性信号层（用户显式「记住/忘记」）不受互斥影响，必须落库。
# 等价 Claude Code hasMemoryWritesSince：后台抽取写入同样登记，过滤刚写过的内容。
_MANUAL_WRITE_MUTEX_SECONDS = 120.0
# 动作定位放宽阈值：find_similar 的查重门槛（重叠≥5 且 sim>0.7）用于「防重复新增」，
# 但 update/delete 是「找到用户指的那一条」，门槛过高会让纠正/遗忘静默失败。
_LOCATE_RELAX_SIMILARITY = 0.55
_LOCATE_RELAX_OVERLAP = 3
_LOCATE_MIN_SIMILARITY = 0.35
# 删除定位更保守（误删不可逆）：相似度门槛高于 update
_LOCATE_DELETE_SIMILARITY = 0.6
_LOCATE_DELETE_OVERLAP = 4
_LOCATE_DELETE_MIN_SIMILARITY = 0.5

# ---- 确定性信号层：用户显式指令与自述事实不依赖 LLM 判断 ----
# 背景：纯 LLM 抽取在「一次性查询」门禁下极易整轮判空（实测大量 [] 产出），
# 用户明确说「记住…」也会被静默丢弃。此层用规则兜底，保证显式意图必定落库。
_EXPLICIT_REMEMBER_RE = re.compile(
    r"记住|记一下|记下|记录一下|帮我记|别忘了|别忘记|以后记得|记到记忆|存到记忆|写入记忆"
)
_EXPLICIT_FORGET_RE = re.compile(
    r"忘记|忘掉|删掉记忆|删除记忆|清除记忆|别记了|不要再记|不要记住|从记忆里删"
)
# 自述事实：主语为「我」的健康/身份/生活事实（非疑问、非通用知识）
_SELF_DISCLOSURE_RE = re.compile(
    r"我对|我有|我患|我得|我确诊|我做过|我今年|我住在|我来自|我从事|我叫|我的名字|"
    r"我老公|我老婆|我孩子|我妈|我爸|我家|我平时|我最近|我刚|我血压|我血糖|我体重|我身高"
)
_SELF_PREF_RE = re.compile(
    r"喜欢|爱吃|爱喝|讨厌|不爱|不吃|不喝|忌口|过敏|偏好|习惯|受不了|不喜欢"
)
_SELF_REF_RE = re.compile(r"我|咱")
_GOAL_RE = re.compile(r"我想|我打算|我计划|我的目标|目标是|打算在|准备在|希望能在|想减肥|想控")
# 对回答方式的纠正（feedback 类，跨会话复用价值高）
_REPLY_FEEDBACK_RE = re.compile(
    r"以后请|以后回答|下次请|回答太|太啰嗦|太简短|简洁一点|详细一点|先给结论|先说结论|"
    r"不要长篇|别废话|直接给|说人话|分点回答"
)
# 疑问句守卫：命中则舍弃该分句，避免把「我有高血压吗？」这类提问当自述事实入库
_QUESTION_GUARD_RE = re.compile(
    r"[？?]|吗|呢|什么|怎么|如何|是否|能否|有没有|能不能|请问|不知道|不了解|不清楚|多少"
)
# 单轮确定性信号上限（与 apply_extraction 的单轮 3 条预算一致）
_EXPLICIT_MAX_ITEMS = 3

# ---- L5 会话级记忆去重（类比 Claude Code collectSurfacedMemories）----
# 扫描会话已注入记忆 id 而非持久状态：compact 时重置，允许压缩后合理重注入。
# 模块级 dict 带容量上限裁剪，防长会话/多会话无限增长。
_surfaced: dict = {}
_SURFACED_MAX_SESSIONS = 500
_SURFACED_MAX_IDS = 200
# 主副互斥：user_id -> 最近一次手动写入时间戳（time.time()）
_manual_write_at: dict = {}
# 后台抽取写入登记：user_id -> 最近一次自动写入时间戳（hasMemoryWritesSince 同源）
_auto_write_at: dict = {}


def surfaced_ids(session_id: str) -> set:
    """返回本会话已注入过的相关记忆 id 集合（只读）。"""
    if not session_id:
        return set()
    return set(_surfaced.get(session_id) or ())


def mark_surfaced(session_id: str, memory_ids: list):
    """登记本会话本轮实际注入的相关记忆 id（供后续轮次去重）。"""
    if not session_id or not memory_ids:
        return
    bucket = _surfaced.setdefault(session_id, set())
    bucket.update(memory_ids)
    if len(bucket) > _SURFACED_MAX_IDS:
        # 超容量时保留最近登记的一半（set 无序，裁剪仅为防膨胀，不影响正确性）
        _surfaced[session_id] = set(list(bucket)[-(_SURFACED_MAX_IDS // 2):])
    if len(_surfaced) > _SURFACED_MAX_SESSIONS:
        for key in list(_surfaced.keys())[:len(_surfaced) - _SURFACED_MAX_SESSIONS]:
            _surfaced.pop(key, None)


def reset_surfaced(session_id: str):
    """compact（摘要覆盖点推进）时重置本会话去重集合，允许压缩后重新注入相关记忆。"""
    if session_id:
        _surfaced.pop(session_id, None)


def note_manual_write(user_id: str):
    """用户手动写入记忆时登记时间戳（主副互斥：后台抽取近期跳过）。"""
    if user_id:
        _manual_write_at[user_id] = time.time()


def note_auto_write(user_id: str):
    """后台抽取写入记忆时登记时间戳（hasMemoryWritesSince：过滤刚写过的内容）。"""
    if user_id:
        _auto_write_at[user_id] = time.time()


def _manual_write_recent(user_id: str) -> bool:
    ts = _manual_write_at.get(user_id)
    return bool(ts and (time.time() - ts) < _MANUAL_WRITE_MUTEX_SECONDS)


def has_memory_writes_since(user_id: str, seconds: float = _MANUAL_WRITE_MUTEX_SECONDS) -> bool:
    """对齐 Claude Code hasMemoryWritesSince：窗口内是否有手动/自动记忆写入。

    抽取代理据此跳过 LLM 层或过滤与刚写入内容重复的候选，避免对同一件事反复写记忆。
    """
    now = time.time()
    for store in (_manual_write_at, _auto_write_at):
        ts = store.get(user_id)
        if ts and (now - ts) < seconds:
            return True
    return False


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


def _clamp_confidence(value) -> float:
    try:
        return min(_MAX_CONFIDENCE, max(1.0, float(value)))
    except (TypeError, ValueError):
        return 1.0


def _split_clauses(text: str) -> list:
    """按句末标点切分分句（保留逗号内部语义），供逐句判定自述/疑问。"""
    return [c.strip() for c in re.split(r"[。！？!?；;\n]+", text or "") if c.strip()]


def _to_third_person(text: str) -> str:
    """自述句转第三人称（「我对花生过敏」→「用户对花生过敏」），与存量记忆口径一致。"""
    body = (text or "").strip().strip("，,。.：:、 ")
    body = re.sub(r"^我", "用户", body)
    return body.replace("我的", "用户的")


def extract_explicit_signals(user_message: str) -> list:
    """确定性信号层：从用户本轮消息提取「必定该落库」的动作项（不依赖 LLM）。

    优先级：显式记住/忘记 > 回答方式纠正 > 自述偏好/事实/目标。
    疑问句守卫逐分句生效，避免把提问当自述；敏感信息在 add/update 写点再拦一道。
    返回与 apply_extraction 同构的 items（action/type/content/importance/target_hint）。
    """
    text = (user_message or "").strip()
    if len(text) < 4:
        return []
    primary, secondary = [], []
    # 1) 显式遗忘：取触发词之后的描述作为定位 hint
    if _EXPLICIT_FORGET_RE.search(text):
        tail = _EXPLICIT_FORGET_RE.split(text, maxsplit=1)[-1].strip(" ，,。.：:、")
        if tail:
            primary.append({"action": "delete", "type": "user", "content": "",
                            "target_hint": tail[:120], "importance": 0.9})
    # 2) 显式记住：触发词之后的内容为记忆正文（为空则用整句）
    if _EXPLICIT_REMEMBER_RE.search(text):
        tail = _EXPLICIT_REMEMBER_RE.split(text, maxsplit=1)[-1].strip(" ，,。.：:、")
        body = tail or text
        if body and not _is_sensitive(body):
            if _GOAL_RE.search(body):
                mtype, imp = "project", 0.8
            elif _SELF_PREF_RE.search(body):
                mtype, imp = "user", 0.85
            else:
                mtype, imp = "user", 0.85
            primary.append({"action": "add", "type": mtype,
                            "content": _to_third_person(body)[:200], "importance": imp})
    # 3) 逐分句自述信号（疑问句舍弃，但保留疑问句里夹带的自述事实）
    def _classify(part: str):
        part = part.strip()
        if not 5 <= len(part) <= 120 or _is_sensitive(part):
            return None
        if _REPLY_FEEDBACK_RE.search(part):
            mtype, imp = "feedback", 0.75
        elif _SELF_PREF_RE.search(part) and _SELF_REF_RE.search(part):
            mtype, imp = "user", 0.8
        elif _GOAL_RE.search(part):
            mtype, imp = "project", 0.7
        elif _SELF_DISCLOSURE_RE.search(part):
            mtype, imp = "user", 0.7
        else:
            return None
        return {"action": "add", "type": mtype, "_rule": True,
                "content": _to_third_person(part)[:200], "importance": imp}

    for clause in _split_clauses(text):
        # 显式指令分句已由上面采集（且已去掉触发词前缀），此处不重复采集，
        # 否则「记住：我对海鲜过敏」会多出一条带「记住：」前缀的劣质正文并查重覆盖。
        if _EXPLICIT_REMEMBER_RE.search(clause) or _EXPLICIT_FORGET_RE.search(clause):
            continue
        if _QUESTION_GUARD_RE.search(clause):
            # 「我有高血压，能吃什么药」：整句是疑问，但前半句是自述事实，
            # 按逗号退一步切分，仅保留自身非疑问且含自述主语的分句。
            for part in re.split(r"[，,、]+", clause):
                if _QUESTION_GUARD_RE.search(part) or not _SELF_REF_RE.search(part):
                    continue
                if _EXPLICIT_REMEMBER_RE.search(part) or _EXPLICIT_FORGET_RE.search(part):
                    continue
                item = _classify(part)
                if item:
                    secondary.append(item)
            continue
        item = _classify(clause)
        if item:
            secondary.append(item)
    # 显式指令优先，同内容去重，单轮上限 3 条
    out, seen = [], set()
    for item in primary + secondary:
        key = (item["action"], item.get("content") or item.get("target_hint") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= _EXPLICIT_MAX_ITEMS:
            break
    return out


def _parse_extraction_items(text: str):
    """健壮解析 LLM 抽取输出：返回 (items, reason)，reason ∈ {"", "empty", "parse_error"}。

    旧版单一贪婪 `\\[.*\\]` 正则在模型输出 fence/前后缀说明/单对象/多段数组时
    直接解析失败并静默丢弃整轮产出。此处按可信度依次尝试多候选并逐个 json 校验。
    """
    raw = (text or "").strip()
    if not raw:
        return [], "empty"
    candidates = []
    fence = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(raw)
    candidates.extend(m.group(0) for m in re.finditer(r"\[[\s\S]*?\]", raw))
    candidates.extend(m.group(0) for m in re.finditer(r"\{[\s\S]*?\}", raw))
    candidates.extend(m.group(0) for m in re.finditer(r"\[[\s\S]*\]", raw))
    candidates.extend(m.group(0) for m in re.finditer(r"\{[\s\S]*\}", raw))
    for cand in candidates:
        try:
            data = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            data = [data]
        if isinstance(data, list):
            items = [x for x in data if isinstance(x, dict)]
            return items, ("" if items else "empty")
    return [], "parse_error"


def memory_line(m: dict) -> str:
    """把一条记忆渲染为注入行（单一事实源，chat 画像卡/相关层共用）。

    新鲜度警告（对齐 Claude Code memoryFreshnessText）：超过 _FRESHNESS_WARN_DAYS
    未更新的记忆行尾附「时点观察」提醒，防止陈旧记忆被当作当前事实。天数在注入时
    预计算，同会话内字节稳定（前缀缓存友好）；跨天自然变化可接受。
    """
    content = (m.get("content") or "").strip()
    line = f"- {content}"
    age_days = _days_since(m.get("updated_at") or m.get("created_at"))
    if age_days > _FRESHNESS_WARN_DAYS:
        line += f"（记忆沉淀于 {int(age_days)} 天前，属时点观察，如与现状冲突以用户最新表述为准）"
    return line


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
        ok = db.execute(
            "DELETE FROM user_memories WHERE id = ? AND user_id = ?",
            (memory_id, user_id),
        ) > 0
        if ok:
            # 双写：topic 文件删除 + MEMORY.md 索引重建 + 写路径观测事件
            memory_store.remove_memory(user_id, memory_id)
            memory_store.log_memory_event(user_id, "delete", "auto")
        return ok

    @staticmethod
    def update_memory(
        user_id: str,
        memory_id: str,
        content: str = None,
        importance: float = None,
        memory_type: str = None,
        confidence: float = None,
    ) -> Optional[dict]:
        """更新一条记忆（用户编辑或抽取侧 update 动作）；content 命中敏感词时拒绝。

        confidence 传入时按上限截断写入（查重命中/用户确认会 +1 递增），None 保持原值。
        """
        row = db.query_one(
            "SELECT * FROM user_memories WHERE id = ? AND user_id = ?",
            (memory_id, user_id),
        )
        if not row:
            return None
        content = (content or "").strip()
        if content and _is_sensitive(content):
            return None
        mtype = _normalize_type(memory_type) if memory_type else None
        now = datetime.now().isoformat()
        db.execute(
            "UPDATE user_memories SET content = ?, keywords = ?, memory_type = ?,"
            " importance = ?, confidence = ?, updated_at = ? WHERE id = ?",
            (
                content or row["content"],
                json.dumps(_tokenize_keywords(content), ensure_ascii=False) if content else row["keywords"],
                mtype or row["memory_type"],
                _clamp_importance(importance) if importance is not None else row["importance"],
                _clamp_confidence(confidence) if confidence is not None else _clamp_confidence(row.get("confidence")),
                now, memory_id,
            ),
        )
        # 双写：topic 文件按最新镜像行重生成（编辑而非追加，文件持续演进）
        memory_store.sync_memory_by_id(user_id, memory_id)
        memory_store.log_memory_event(user_id, "update", "auto", mtype or row["memory_type"])
        return {"id": memory_id, "content": content or row["content"], "updated": True}

    @staticmethod
    def find_similar(user_id: str, content: str, memory_type: str = None) -> Optional[dict]:
        """查找与 content 视为同一条的已有记忆（关键词重叠 ≥5 且序列相似度 >0.7）。"""
        keywords = set(_tokenize_keywords(content))
        best, best_key = None, (0, 0.0)
        for m in MemoryService.list_memories(user_id):
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
    def _locate_for_action(user_id: str, hint: str, memory_type: str = None,
                           for_delete: bool = False) -> Optional[dict]:
        """update/delete 的目标定位：先走严格查重，未命中再按放宽阈值找最相似一条。

        查重门槛（防重复新增）与定位门槛（找用户指的那条）语义不同：用户说「我改吃素了」
        与旧记忆「用户爱吃红烧肉」字面重叠很低，严格门槛下永远定位不到，纠正会静默失败。
        删除不可逆，因此 for_delete 用更高的相似度/重叠门槛。
        """
        hit = MemoryService.find_similar(user_id, hint, memory_type)
        if hit:
            return hit
        hint = (hint or "").strip()
        if not hint:
            return None
        keywords = set(_tokenize_keywords(hint))
        best, best_key = None, (0, 0.0)
        for m in MemoryService.list_memories(user_id):
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
        content: str,
        memory_type: str = "user",
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
        memory_type = _normalize_type(memory_type)
        if dedup:
            hit = MemoryService.find_similar(user_id, content)
            if hit:
                # 查重命中视为再次确认：置信度 +1（上限 _MAX_CONFIDENCE），召回评分随之提升
                return MemoryService.update_memory(
                    user_id, hit["id"], content=content,
                    importance=_clamp_importance(importance), memory_type=memory_type,
                    confidence=_clamp_confidence(float(hit.get("confidence") or 1.0) + 1),
                )

        now = datetime.now().isoformat()
        keywords = _tokenize_keywords(content)
        memory_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO user_memories (id, user_id, memory_type, content, keywords,"
            " source_session_id, source_message_id, importance, confidence, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                memory_id, user_id, memory_type, content,
                json.dumps(keywords, ensure_ascii=False),
                source_session_id, source_message_id,
                _clamp_importance(importance), 1.0, now, now,
            ),
        )
        # 双写：新增记忆落 topic markdown + 重建索引，回填 file_path 镜像列
        rel = memory_store.sync_memory_by_id(user_id, memory_id)
        if rel:
            db.execute("UPDATE user_memories SET file_path = ? WHERE id = ?", (rel, memory_id))
        memory_store.log_memory_event(user_id, "add", "auto", memory_type, content[:60])
        note_auto_write(user_id)
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
            # 遗忘同步到 markdown 层（topic 文件删除 + 索引重建）
            memory_store.remove_memory(user_id, row["id"])
            memory_store.log_memory_event(user_id, "forget", "auto", detail="超上限遗忘")

    @staticmethod
    def profile_memories(user_id: str) -> List[dict]:
        """常驻画像卡：高重要度或 user 类记忆，每会话必注入（类比 Claude MEMORY.md 索引层）。

        按重要度降序取 ≤6 条，合计字符受 MEMORY_PROFILE_CHARS 预算约束。
        """
        if not user_id:
            return []
        rows = db.query(
            "SELECT * FROM user_memories WHERE user_id = ?"
            " AND (importance >= ? OR memory_type = 'user')"
            # 稳定排序（前缀缓存友好）：重要度降序后按创建时间/id 升序，
            # 避免 updated_at 变动导致画像卡顺序抖动、U 区前缀失效。
            " ORDER BY importance DESC, created_at ASC, id ASC LIMIT ?",
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

    # ---- L5 会话级去重 / 主副互斥：委托模块级实现（单一事实源，状态存模块 dict）----
    # 方法体内的裸名解析到模块级函数（类属性不在方法作用域链内），故非递归。
    @staticmethod
    def surfaced_ids(session_id: str) -> set:
        return surfaced_ids(session_id)

    @staticmethod
    def mark_surfaced(session_id: str, memory_ids: list):
        return mark_surfaced(session_id, memory_ids)

    @staticmethod
    def reset_surfaced(session_id: str):
        return reset_surfaced(session_id)

    @staticmethod
    def note_manual_write(user_id: str):
        return note_manual_write(user_id)

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
        # 整改：单字关键词（的/系/和…）几乎与任何记忆重叠，会造成无关注入；
        # 相关性门禁仅计二字及以上关键词，无有效重叠时不注入任何记忆。
        meaningful = {t for t in query_tokens if len(t) >= 2}

        def score(m: dict) -> float:
            overlap = len(meaningful & set(m.get("keywords") or []))
            decay = math.exp(-_days_since(m.get("last_used_at") or m.get("updated_at")) / _DECAY_DAYS)
            weight = _TYPE_WEIGHT.get(m.get("memory_type") or "user", 1.0)
            # 置信度因子（0.1*confidence）：反复确认的记忆排序更靠前，但权重低于关键词/重要度
            conf = 0.1 * _clamp_confidence(m.get("confidence"))
            return overlap * 2 + (float(m.get("importance", 0.5)) + decay) * weight + conf

        ranked = sorted(memories, key=score, reverse=True)
        # 无有效关键词重叠 → 不注入（旧版「无重叠也兜底注入 top」会让每个问题
        # 都携带个性化上下文，语义缓存探针与写入被一刀切旁路）。
        chosen = [m for m in ranked if meaningful & set(m.get("keywords") or [])][:limit]
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
            "avg_confidence": 0.0,
            "total_chars": 0,
            "profile_chars_budget": int(getattr(settings, "MEMORY_PROFILE_CHARS", 300)),
            "ctx_chars_budget": int(getattr(settings, "MEMORY_CTX_CHARS", 600)),
            "storage_limit": MAX_MEMORIES_PER_USER,
            "storage_usage": 0.0,
            "last_updated_at": None,
            "layers": memory_store.layers_overview(user_id),
        }
        if not user_id or user_id in {"guest", "system", "-"}:
            return empty
        rows = db.query(
            "SELECT memory_type, content, importance, confidence, last_used_at, updated_at"
            " FROM user_memories WHERE user_id = ? LIMIT ?",
            (user_id, MAX_MEMORIES_PER_USER * 2),
        )
        if not rows:
            return empty
        by_type = {t: 0 for t in MEMORY_TYPES}
        profile_count = retrieved_count = never_used = total_chars = 0
        importance_sum = 0.0
        confidence_sum = 0.0
        last_updated = ""
        for r in rows:
            mtype = _normalize_type(r.get("memory_type"))
            by_type[mtype] += 1
            content = r.get("content") or ""
            total_chars += len(content)
            importance_sum += float(r.get("importance") or 0.5)
            confidence_sum += _clamp_confidence(r.get("confidence"))
            if float(r.get("importance") or 0) >= _PROFILE_IMPORTANCE or mtype == "user":
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
            "avg_confidence": round(confidence_sum / total, 3) if total else 0.0,
            "total_chars": total_chars,
            "profile_chars_budget": int(getattr(settings, "MEMORY_PROFILE_CHARS", 300)),
            "ctx_chars_budget": int(getattr(settings, "MEMORY_CTX_CHARS", 600)),
            "storage_limit": MAX_MEMORIES_PER_USER,
            "storage_usage": round(total / MAX_MEMORIES_PER_USER, 4) if MAX_MEMORIES_PER_USER else 0.0,
            "last_updated_at": last_updated or None,
            "layers": memory_store.layers_overview(user_id),
        }

    @staticmethod
    def drill_memories(user_id: str, kind: str = "all") -> dict:
        """记忆概览钻取：按指标/类型返回记忆明细 + 口径说明（只读）。

        kind: all/profile/retrieved/never_used/importance 或 type:<user|feedback|project|reference>（旧类型自动归一）。
        口径与 usage_overview 同源，explanation 代入真实数值推导。
        """
        rows = MemoryService.list_memories(user_id) if user_id else []
        for r in rows:
            r["is_profile"] = (
                float(r.get("importance") or 0) >= _PROFILE_IMPORTANCE
                or (r.get("memory_type") or "user") == "user"
            )
            r["type_label"] = _TYPE_LABELS.get(r.get("memory_type") or "user", "用户信息")
        total = len(rows)
        imp_sum = sum(float(r.get("importance") or 0.5) for r in rows)
        avg = round(imp_sum / total, 3) if total else 0.0
        budget = int(getattr(settings, "MEMORY_PROFILE_CHARS", 300))

        if kind == "profile":
            items = [r for r in rows if r["is_profile"]]
            label = "画像卡候选"
            explanation = (
                f"画像卡候选 = 重要度 ≥ {_PROFILE_IMPORTANCE} 或类型为「用户信息」的记忆，共 {len(items)} 条；"
                f"常驻画像卡按重要度降序最多取 {_PROFILE_MAX_ITEMS} 条、字符预算 {budget}。"
            )
        elif kind == "retrieved":
            items = [r for r in rows if r.get("last_used_at")]
            label = "被检索命中"
            explanation = f"被检索命中 = last_used_at 非空（曾被相关检索层注入上下文）的记忆，共 {len(items)} 条。"
        elif kind == "never_used":
            items = [r for r in rows if not r.get("last_used_at")]
            label = "从未使用"
            explanation = (
                f"从未使用 = last_used_at 为空的记忆，共 {len(items)} 条；"
                "超上限时「低重要度 + 长期未用」者会被优先遗忘。"
            )
        elif kind == "importance":
            items = sorted(rows, key=lambda r: float(r.get("importance") or 0), reverse=True)
            label = "平均重要度"
            explanation = f"平均重要度 = 全部 {total} 条记忆重要度之和 {imp_sum:.2f} ÷ {total} = {avg}。按重要度降序展示。"
        elif kind.startswith("type:"):
            t = _normalize_type(kind.split(":", 1)[1])
            items = [r for r in rows if _normalize_type(r.get("memory_type")) == t]
            label = f"{_TYPE_LABELS.get(t, t)}类记忆"
            explanation = f"「{_TYPE_LABELS.get(t, t)}」类记忆 = memory_type='{t}' 的记录，共 {len(items)} 条。"
        else:
            kind = "all"
            items = rows
            label = "记忆条数"
            usage = round(total / MAX_MEMORIES_PER_USER * 100, 1) if MAX_MEMORIES_PER_USER else 0.0
            explanation = (
                f"记忆条数 = 当前用户 user_memories 表记录数 {total}"
                f"（存储上限 {MAX_MEMORIES_PER_USER}，水位 {usage}%）。按更新时间倒序展示。"
            )
        if kind not in ("importance",):
            items = sorted(items, key=lambda r: r.get("updated_at") or "", reverse=True)
        return {"kind": kind, "label": label, "explanation": explanation, "total": len(items), "items": items}

    @staticmethod
    def apply_extraction(user_id: str, session_id: str, items: list) -> dict:
        """执行抽取侧输出的动作清单（add/update/delete），单轮上限 3 条。"""
        stats = {"add": 0, "update": 0, "delete": 0}
        for item in items[:3]:
            if not isinstance(item, dict):
                continue
            content = (item.get("content") or "").strip()
            action = (item.get("action") or "add").strip().lower()
            mtype = _normalize_type(item.get("type") or "user")
            importance = _clamp_importance(item.get("importance", 0.5))
            hint = (item.get("target_hint") or content).strip()
            if action == "delete":
                hit = MemoryService._locate_for_action(user_id, hint, for_delete=True)
                if hit:
                    MemoryService.delete_memory(user_id, hit["id"])
                    stats["delete"] += 1
                continue
            if action == "update":
                hit = MemoryService._locate_for_action(user_id, hint)
                if hit:
                    MemoryService.update_memory(
                        user_id, hit["id"], content=content or None,
                        importance=importance, memory_type=mtype,
                        confidence=_clamp_confidence(float(hit.get("confidence") or 1.0) + 1),
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
                if (m.get("memory_type") or "user") == memory_type]
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
                memory_store.log_memory_event(user_id, "merge", "auto", memory_type,
                                              f"{len(group)} 条合并为 1 条")
                logger.info("用户 %s 记忆合并：%d 条 → 1 条（type=%s）", user_id, len(group), memory_type)
            except Exception as e:
                logger.warning("记忆合并失败（忽略）: %s", e)

    @staticmethod
    async def _llm_extract(user_message: str, assistant_message, recent_user_messages=None,
                           turn_transcript: str = "", existing_memories=None):
        """extractMemories 的 LLM 层：扫描整轮 transcript，返回 (items, reason)。

        reason 供运行事件解释「为何无产出」。existing_memories 为现有记忆清单
        （hasMemoryWritesSince 配套：提示代理勿对刚写过/已存在的内容重复新增）。
        """
        try:
            from app.core.llm import LLMFactory

            # 记忆抽取为后台辅助生成（forked agent），走网关默认规则（role 仅用于日志显性化）
            llm = LLMFactory.create_for_role("memory_extract")
        except Exception as e:
            logger.warning("记忆抽取初始化失败（忽略）: %s", e)
            return [], "llm_unavailable"
        user_text = (user_message or "").strip()
        # 窗口从单轮扩到近几条用户消息：偏好/纠正往往跨轮表达（「上次说的那个…」），
        # 单轮 500 字窗口看不到上下文，模型只能保守判空。
        history = [(m or "").strip()[:200] for m in (recent_user_messages or []) if (m or "").strip()]
        history = [h for h in history if h != user_text[:200]][-2:]
        context_block = ""
        if history:
            context_block = (
                "近期用户消息（仅供判断是否纠正/覆盖旧记忆，不单独据此新增）：\n"
                + "\n".join(f"- {h}" for h in history) + "\n\n"
            )
        # 现有记忆清单：代理主动比对去重（hasMemoryWritesSince 语义的 prompt 侧配套）
        existing_block = ""
        if existing_memories:
            existing_block = (
                "【已有记忆（勿重复新增；仅当用户纠正/覆盖时输出 update）】\n"
                + "\n".join(f"- [{m.get('memory_type')}] {(m.get('content') or '')[:100]}"
                            for m in existing_memories[:20]) + "\n\n"
            )
        # 整轮 transcript（含工具调用轨迹）优先；无则退回单轮用户/助手消息
        turn_block = turn_transcript.strip() if turn_transcript else (
            f"用户: {user_text[:500]}\n助手: {(assistant_message or '')[:500]}"
        )
        prompt = (
            "你是记忆抽取代理（extractMemories）。扫描下面这一轮对话的完整 transcript，"
            "提取用户值得长期记住的内容。只记「未来对话有用、且不显而易见」的内容。\n"
            "【必须保存】①用户明确说「记住/记一下/别忘了…」的内容（不得判空）；"
            "②用户自述的健康/身份/生活事实与饮食/口味/作息偏好禁忌（type=user）；"
            "③用户对回答方式的纠正与要求（type=feedback）；"
            "④用户进行中的目标/计划/任务（type=project）；"
            "⑤外部资源/位置指引，如材料在哪份文档/哪个位置（type=reference）。\n"
            "写入门禁（What-NOT-to-save），以下一律不记：①可从当前上下文或知识库直接推导的内容；"
            "②一次性提问/临时查询本身（但提问里夹带的自述事实仍要保存，"
            "如「我有高血压，能吃什么药」→ 保存「用户有高血压」）；"
            "③对用户的负面评价或臆测；④活动流水、寒暄等低价值内容；⑤密码/密钥/身份证等敏感信息；"
            "⑥已有记忆清单中已存在或刚写入过的内容（避免对同一件事反复写记忆）。\n"
            "若用户纠正或覆盖了此前已记住的内容，输出 action=update 并用 target_hint 描述原记忆；"
            "若用户明确要求遗忘，输出 action=delete 并用 target_hint 描述目标。\n"
            "content 用第三人称（「用户…」）陈述，保留具体信息点，不要写成问句。\n"
            "type 取值：user/feedback/project/reference。仅输出 JSON 数组，每项 "
            "{\"content\": str, \"importance\": 0~1, \"type\": str, "
            "\"action\": \"add|update|delete\", \"target_hint\": str}；确实无可保存内容才输出 []。\n\n"
            + existing_block
            + context_block
            + "【本轮 transcript】\n" + turn_block[:6000]
        )
        try:
            collected = ""
            async for chunk in llm.chat([{"role": "user", "content": prompt}], stream=False):
                collected += chunk
        except Exception as e:
            logger.warning("记忆抽取调用失败（忽略，不影响对话）: %s", e)
            return [], "llm_error"
        items, reason = _parse_extraction_items(collected)
        if reason == "parse_error":
            logger.warning("记忆抽取输出无法解析（已忽略）: %s", (collected or "")[:200])
        return items[:3], reason

    @staticmethod
    async def extract_from_turn(user_id: str, session_id: str, user_message, assistant_message,
                                recent_user_messages=None, turn_transcript: str = ""):
        """extractMemories：每轮 query loop 完整结束后的后台记忆抽取代理。

        扫描该轮对话的所有 transcript（用户反馈/纠正/信息），确定性信号层 + LLM 层
        双通道，并落「抽取运行」观测事件。结构要点：
        - 显式层始终执行：用户说「记住/忘记…」不依赖模型判断，也不受主副互斥拦截；
        - hasMemoryWritesSince：近期手动/自动写入后 LLM 层跳过（主副互斥），
          且与刚写入内容高度重复的候选被过滤，避免对同一件事反复写记忆；
        - LLM 层失败/判空不再静默：reason 写进运行事件 detail，面板可区分
          「未运行 / 跑了无产出 / 模型不可用 / 解析失败」；
        - 每轮必写一条 action=extract 事件（与 add/update/delete 写事件拆口径）。
        返回统计 dict 供观测/测试；任何异常不影响主对话流程。
        """
        result = {"ran": False, "explicit": 0, "llm": 0, "reason": "",
                  "add": 0, "update": 0, "delete": 0}
        if not user_id or user_id in {"guest", "system", "-"}:
            result["reason"] = "no_user"
            return result
        user_text = (user_message or "").strip()
        if not user_text:
            result["reason"] = "empty_turn"
            return result
        result["ran"] = True
        # 1) 确定性信号层（始终执行，不受手动写入互斥影响）
        explicit = extract_explicit_signals(user_text)
        result["explicit"] = len(explicit)
        # 2) LLM 层：hasMemoryWritesSince（对齐 Claude Code）仅拦此层，
        #    避免用户刚手动写过记忆后后台抽取竞态重复写。
        llm_items, llm_reason = [], ""
        if _manual_write_recent(user_id):
            llm_reason = "manual_mutex"
            logger.info("用户 %s 近期手动写入记忆，跳过本轮 LLM 抽取（主副互斥）", user_id)
        else:
            llm_items, llm_reason = await MemoryService._llm_extract(
                user_text, assistant_message, recent_user_messages,
                turn_transcript=turn_transcript,
                existing_memories=MemoryService.list_memories(user_id))
        result["llm"] = len(llm_items)
        result["reason"] = llm_reason
        # 3) 合并执行：显式项优先（单轮 3 条预算内），同内容去重；
        #    规则自述项与模型项同内容时采纳模型更细的分类与更高重要度，
        #    用户显式「记住」项不被模型降级。
        merged, index = [], {}
        for item in explicit + llm_items:
            key = ((item.get("action") or "add").strip().lower(),
                   (item.get("content") or item.get("target_hint") or "").strip())
            if key in index:
                kept = merged[index[key]]
                if kept.get("_rule") and not item.get("_rule"):
                    if item.get("type") in MEMORY_TYPES:
                        kept["type"] = item["type"]
                    kept["importance"] = max(_clamp_importance(kept.get("importance", 0.5)),
                                             _clamp_importance(item.get("importance", 0.5)))
                continue
            index[key] = len(merged)
            merged.append(dict(item))
        for it in merged:
            it.pop("_rule", None)
        # 4) hasMemoryWritesSince 过滤：窗口内刚写入过且内容高度重复的 add 候选直接丢弃
        #    （查重转 update 仍会让同一件事每轮置信度虚增，此处提前拦截）。
        if has_memory_writes_since(user_id):
            kept_items = []
            for it in merged:
                if (it.get("action") or "add") == "add" and it.get("content"):
                    hit = MemoryService.find_similar(user_id, it["content"])
                    if hit and _content_similarity(it["content"], hit.get("content") or "") > 0.9:
                        continue
                kept_items.append(it)
            merged = kept_items
        stats = MemoryService.apply_extraction(user_id, session_id, merged)
        result.update(stats)
        detail = (f"显式{len(explicit)}条/模型{len(llm_items)}条→"
                  f"新增{stats['add']}更新{stats['update']}删除{stats['delete']}")
        if llm_reason:
            detail += f"（模型层:{llm_reason}）"
        memory_store.log_memory_event(user_id, "extract", "auto", detail=detail)
        if sum(stats.values()):
            logger.info("用户 %s 记忆抽取动作: %s（%s）", user_id, stats, detail)
        # AutoDream（索引治理）由 consolidate_if_due 按三重门控调度，不在此无条件合并。
        return result

    @staticmethod
    async def _auto_dream(user_id: str) -> dict:
        """AutoDream：独立后台整理代理，按固定 Prompt 审视全部长期记忆。

        输入=现有记忆清单 + 近期会话摘要（短期沉淀线索）；输出=类型化整理动作
        （merge/update/delete/add），逐条经写入治理执行。LLM 不可用或输出无法
        解析时回退逐类型 _merge_cluster（确定性簇合并），保证门控达标必有整理。
        """
        rows = MemoryService.list_memories(user_id)
        stats = {"actions": 0, "fallback": False}
        if not rows:
            return stats
        listing = "\n".join(
            f"- id={m['id']} type={m.get('memory_type') or 'user'} "
            f"importance={m.get('importance')} | {m['content']}"
            for m in rows[:60]
        )
        summaries = db.query(
            "SELECT session_id, summary FROM chat_session_summaries"
            " WHERE user_id = ? ORDER BY updated_at DESC LIMIT 5", (user_id,))
        sess_block = "\n".join(
            f"- 会话 {r['session_id']}: {(r.get('summary') or '')[:300]}" for r in summaries
        ) or "（无近期会话摘要）"
        prompt = (
            "你是记忆整理代理（AutoDream），在后台定期整理一位用户的长期记忆。"
            "请审视下方全部记忆与近期会话摘要，按固定规则整理：\n"
            "1. 合并：同一件事的多条记忆合并为一条（保留全部不重复信息点，不新增信息）；\n"
            "2. 更新：被近期会话证实已过时/被纠正的记忆，改写为最新事实；\n"
            "3. 删除：相互矛盾且可判定失效、或明显一次性无跨会话价值的记忆；\n"
            "4. 沉淀：近期会话摘要中反复出现、值得跨会话保留的新信息可新增。\n"
            "禁止：新增敏感信息（密码/证件号等）、凭空编造、改动静态规矩。\n"
            "仅输出 JSON 数组，元素为以下动作之一（无动作输出 []）：\n"
            '{"action":"merge","ids":["id1","id2"],"content":"合并后内容","importance":0.8}\n'
            '{"action":"update","id":"记忆id","content":"更新后内容","importance":0.7}\n'
            '{"action":"delete","id":"记忆id"}\n'
            '{"action":"add","content":"新记忆","type":"user|feedback|project|reference","importance":0.6}\n\n'
            f"【现有记忆】\n{listing}\n\n【近期会话摘要】\n{sess_block}"
        )
        try:
            from app.core.llm import LLMFactory

            llm = LLMFactory.create_for_role("memory_extract")
            collected = ""
            async for chunk in llm.chat([{"role": "user", "content": prompt}], stream=False):
                collected += chunk
            items, reason = _parse_extraction_items(collected)
            if reason == "parse_error":
                raise ValueError("AutoDream 输出无法解析")
        except Exception as e:
            logger.warning("AutoDream 代理不可用，回退簇合并（忽略）: %s", e)
            stats["fallback"] = True
            for mtype in MEMORY_TYPES:
                await MemoryService._merge_cluster(user_id, mtype)
            return stats
        valid_ids = {m["id"] for m in rows}
        for it in items[:10]:
            try:
                action = (it.get("action") or "").strip().lower()
                if action == "merge":
                    ids = [i for i in (it.get("ids") or []) if i in valid_ids]
                    content = (it.get("content") or "").strip()
                    if len(ids) < 2 or not content or _is_sensitive(content):
                        continue
                    target = next(m for m in rows if m["id"] == ids[0])
                    MemoryService.update_memory(
                        user_id, ids[0], content=content,
                        importance=_clamp_importance(it.get("importance", target.get("importance"))))
                    for mid in ids[1:]:
                        MemoryService.delete_memory(user_id, mid)
                        valid_ids.discard(mid)
                    memory_store.log_memory_event(user_id, "merge", "auto",
                                                  target.get("memory_type") or "user",
                                                  f"AutoDream {len(ids)} 条合并为 1 条")
                    stats["actions"] += 1
                elif action == "update" and it.get("id") in valid_ids:
                    content = (it.get("content") or "").strip()
                    if not content or _is_sensitive(content):
                        continue
                    MemoryService.update_memory(
                        user_id, it["id"], content=content,
                        importance=_clamp_importance(it.get("importance")))
                    stats["actions"] += 1
                elif action == "delete" and it.get("id") in valid_ids:
                    MemoryService.delete_memory(user_id, it["id"])
                    valid_ids.discard(it["id"])
                    stats["actions"] += 1
                elif action == "add":
                    content = (it.get("content") or "").strip()
                    if not content or _is_sensitive(content):
                        continue
                    if MemoryService.add_memory(
                            user_id, content,
                            memory_type=_normalize_type(it.get("type") or "user"),
                            importance=_clamp_importance(it.get("importance", 0.6))):
                        stats["actions"] += 1
            except Exception as e:
                logger.warning("AutoDream 动作执行失败（忽略）: %s", e)
        memory_store.log_memory_event(user_id, "dream", "auto",
                                      detail=f"AutoDream 整理 {stats['actions']} 个动作")
        return stats

    @staticmethod
    async def consolidate_if_due(user_id: str) -> dict:
        """AutoDream 调度：三重门控（任一不满足立即退出）+ 固定 Prompt 整理代理。

        1. 时间门：距上次整合 >= _CONSOLIDATE_HOURS（24h）；
        2. 会话门：期间新增 >= _CONSOLIDATE_SESSIONS（5）个产生转录的会话；
        3. 并发锁：.consolidate-lock（PID 文件锁）空闲，防止多进程重复整合。
        状态存 runtime_config（键 memory_consolidation_state，per-user）。
        由 chat 后台任务尾部调用，失败静默，不阻断主链路。返回 {"ran": bool, ...} 供观测/测试。
        """
        result = {"ran": False, "reason": ""}
        if not user_id or user_id in {"guest", "system", "-"}:
            result["reason"] = "no_user"
            return result
        try:
            from app.services.config_manager import runtime_config

            key = "memory_consolidation_state"
            state = runtime_config.get(key, {}) or {}
            if not isinstance(state, dict):
                state = {}
            mine = state.get(user_id) or {}
            now = time.time()
            last_ts = float(mine.get("last_at") or 0.0)
            hours_elapsed = (now - last_ts) / 3600.0 if last_ts else _CONSOLIDATE_HOURS
            if hours_elapsed < _CONSOLIDATE_HOURS:
                result["reason"] = "time_gate"
                return result
            # 会话门控：上次巩固之后新建、且产生了转录（有对话消息）的会话数
            since_iso = datetime.fromtimestamp(last_ts).isoformat() if last_ts else ""
            if since_iso:
                sess_row = db.query_one(
                    "SELECT COUNT(*) AS c FROM chat_sessions s WHERE s.user_id = ? AND s.created_at > ?"
                    " AND EXISTS (SELECT 1 FROM chat_messages m WHERE m.session_id = s.session_id)",
                    (user_id, since_iso),
                )
            else:
                sess_row = db.query_one(
                    "SELECT COUNT(*) AS c FROM chat_sessions s WHERE s.user_id = ?"
                    " AND EXISTS (SELECT 1 FROM chat_messages m WHERE m.session_id = s.session_id)",
                    (user_id,),
                )
            new_sessions = int((sess_row or {}).get("c") or 0)
            if new_sessions < _CONSOLIDATE_SESSIONS:
                result["reason"] = "session_gate"
                return result
            # 并发锁：其他进程正在整合 → 立即退出
            if not memory_store.acquire_consolidate_lock(user_id):
                result["reason"] = "lock_gate"
                return result
            try:
                dream = await MemoryService._auto_dream(user_id)
                state[user_id] = {"last_at": now, "sessions_at": new_sessions}
                runtime_config.set(key, state)
                result.update({"ran": True, "reason": "ok", "new_sessions": new_sessions,
                               "actions": dream.get("actions", 0), "fallback": dream.get("fallback", False)})
                logger.info("用户 %s AutoDream 完成（新增会话 %d，距上次 %.1fh，动作 %d）",
                            user_id, new_sessions, hours_elapsed, dream.get("actions", 0))
            finally:
                memory_store.release_consolidate_lock(user_id)
        except Exception as e:
            logger.warning("记忆巩固失败（忽略，不影响对话）: %s", e)
            result["reason"] = "error"
        return result


memory_service = MemoryService()
