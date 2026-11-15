"""
见字如面 - RAG 测试集构建流水线（候选池 + 人工审核）

两条候选来源，产出统一落 eval_case_candidates(status=pending)，人工审核通过后才
进入 eval_cases（不直入，保证测试集质量）：
- generate_from_kb：LLM 基于知识库内容生成用例（扩展输出 expected_source）；
- mine_from_logs：游标后线上问答日志配对（use_knowledge=1、答非空），新增配对
  达量（LOG_MINING_MIN_PAIRS）才触发 LLM 提取期望信号，reference_answer 取真实回答。

设计要点：
- 后台周期任务（lifespan 启停），游标存 runtime_config，重启不重复挖掘；
- LLM 辅助路径静默降级：日志挖掘的关键词/来源提取失败仍保留问答对候选；
- 原始文本落库不改写，API 返回前经 redact_log_text 脱敏（单一事实源）。
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import List, Optional

from app.config import settings
from app.core import db

logger = logging.getLogger("jianziruyang.testset_builder")

_PERIODIC_TASK: Optional[asyncio.Task] = None
_CURSOR_KEY = "testset_mining_cursor"
_MINING_DATASET_NAME = "日志挖掘候选集"
# 单轮挖掘配对上限（控制 LLM 成本，剩余配对下轮继续）
_MINING_BATCH = 30

_KB_GEN_PROMPT = (
    "你是「RAG 评估出题专家」。请基于给定的知识库内容，生成用于检索增强问答评估的用例，"
    "只输出一个 JSON 对象，不要输出任何解释、不要用代码块包裹。格式：\n"
    '{"cases": [{"question": "用户问题", "reference_answer": "基于内容的参考答案",'
    ' "expected_keywords": ["答案应命中的关键词，2-4 个"],'
    ' "expected_source": "答案出处的文档/词条标题（取自内容中的标题）"}]}\n'
    "要求：问题覆盖内容中的不同主题，答案严格忠于内容，关键词取自参考答案，"
    "expected_source 必须是内容中真实出现的标题。"
)

_LOG_EXTRACT_PROMPT = (
    "你是「RAG 评估标注专家」。给定一条线上真实问答（问题 + 助手回答 + 检索来源标题），"
    "请提取用于离线回归评测的期望信号，只输出一个 JSON 对象，不要输出任何解释、"
    "不要用代码块包裹。格式：\n"
    '{"expected_keywords": ["回答中应命中的关键词，2-4 个"],'
    ' "expected_source": "最相关的来源标题（没有则空字符串）"}\n'
    "要求：关键词取自回答本身；expected_source 只能从给定来源标题中选择。"
)


def _now() -> str:
    return datetime.now().isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _clean_keywords(value) -> str:
    """关键词列表规范化为 JSON 串（去空白/去重/限量，与 eval_cases 口径一致）。"""
    items = []
    for k in value if isinstance(value, list) else []:
        k = str(k).strip()
        if k and k not in items:
            items.append(k)
    return json.dumps(items[:6], ensure_ascii=False)


def _insert_candidate(dataset_id: str, kb_id: str, origin: str, question: str,
                      reference_answer: str, expected_keywords, expected_source: str,
                      source_ref: dict, user_id: str) -> dict:
    cid = _new_id()
    db.execute(
        "INSERT INTO eval_case_candidates (id, dataset_id, kb_id, origin, question,"
        " reference_answer, expected_keywords, expected_source, source_ref, status,"
        " created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?, 'pending', ?, ?)",
        (cid, dataset_id, kb_id, origin, question[:500],
         (reference_answer or "").strip()[:2000],
         _clean_keywords(expected_keywords), (expected_source or "").strip()[:200],
         json.dumps(source_ref or {}, ensure_ascii=False), user_id, _now()),
    )
    return {"id": cid, "dataset_id": dataset_id, "origin": origin, "question": question[:500]}


# ---------------------------------------------------------------------------
# 来源一：LLM 基于知识库生成（产出候选，不直入评测集）
# ---------------------------------------------------------------------------
async def generate_from_kb(dataset_id: str, kb_id: str, count: int, user: dict) -> dict:
    """基于知识库内容 LLM 生成候选用例（pending，待人工审核入集）。"""
    from app.services import wiki
    from app.services.kb_content import build_kb_context

    if not db.query_one("SELECT id FROM eval_datasets WHERE id = ?", (dataset_id,)):
        raise ValueError("评测集不存在")
    kb = db.query_one("SELECT id, name FROM knowledge_bases WHERE id = ?", (kb_id,))
    if not kb:
        raise ValueError("知识库不存在")
    count = max(1, min(20, int(count or 5)))
    context = build_kb_context(kb_id, 8000)
    if not context.strip():
        raise ValueError("知识库暂无内容，无法自动生成评估用例")
    messages = [
        {"role": "system", "content": _KB_GEN_PROMPT},
        {"role": "user",
         "content": f"【知识库】{kb['name']}\n\n【内容】\n{context}\n\n请生成 {count} 个用例。"},
    ]
    raw = await wiki._llm_complete(messages)
    data = wiki._extract_json(raw)
    items = data.get("cases") or []
    created = []
    user_id = (user or {}).get("id") or (user or {}).get("username") or ""
    for item in items[:count]:
        if not isinstance(item, dict) or not (item.get("question") or "").strip():
            continue
        created.append(_insert_candidate(
            dataset_id, kb_id, "llm_gen", item["question"].strip(),
            item.get("reference_answer") or "", item.get("expected_keywords") or [],
            item.get("expected_source") or "",
            {"kb_id": kb_id, "kb_name": kb["name"]}, user_id,
        ))
    if not created:
        raise ValueError("LLM 未返回有效用例，请重试")
    logger.info("[测试集构建] KB=%s 生成候选 %d 条（dataset=%s）", kb_id, len(created), dataset_id)
    return {"created": len(created), "candidates": created}


# ---------------------------------------------------------------------------
# 来源二：线上日志挖掘（达量触发，游标推进）
# ---------------------------------------------------------------------------
def _source_titles(sources_json: str) -> List[str]:
    """从消息 sources JSON 提取来源标题（expected_source 候选池）。"""
    try:
        sources = json.loads(sources_json or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    titles = []
    for s in sources if isinstance(sources, list) else []:
        if not isinstance(s, dict):
            continue
        for t in (s.get("document_title"), s.get("wiki_title"), s.get("kb_name")):
            t = (t or "").strip()
            if t and t not in titles:
                titles.append(t)
        for c in s.get("chunks") or []:
            if isinstance(c, dict):
                t = (c.get("wiki_title") or "").strip()
                if t and t not in titles:
                    titles.append(t)
    return titles[:6]


def _pick_log_pairs(cursor: str, batch: int = _MINING_BATCH) -> List[dict]:
    """游标后的知识问答配对：助手消息(use_knowledge=1、答非空) + 紧邻的上一条用户问。"""
    where = "role = 'assistant' AND use_knowledge = 1 AND content != ''"
    params: list = []
    if cursor:
        where += " AND created_at > ?"
        params.append(cursor)
    msgs = db.query(
        f"SELECT message_id, session_id, content, sources, created_at FROM chat_messages"
        f" WHERE {where} ORDER BY created_at ASC LIMIT ?", (*params, batch))
    pairs = []
    for m in msgs:
        q = db.query_one(
            "SELECT content FROM chat_messages WHERE session_id = ? AND role = 'user'"
            " AND seq < (SELECT seq FROM chat_messages WHERE message_id = ?)"
            " ORDER BY seq DESC LIMIT 1",
            (m["session_id"], m["message_id"]))
        question = ((q or {}).get("content") or "").strip()
        if not question:
            continue
        pairs.append({
            "message_id": m["message_id"], "session_id": m["session_id"],
            "question": question, "answer": m["content"],
            "titles": _source_titles(m.get("sources") or "[]"),
            "created_at": m["created_at"],
        })
    return pairs


def _ensure_mining_dataset(dataset_id: Optional[str]) -> str:
    """指定数据集则校验存在；缺省创建/复用「日志挖掘候选集」(kind=rag)。"""
    if dataset_id:
        if not db.query_one("SELECT id FROM eval_datasets WHERE id = ?", (dataset_id,)):
            raise ValueError("评测集不存在")
        return dataset_id
    row = db.query_one(
        "SELECT id FROM eval_datasets WHERE name = ? AND kind = 'rag'",
        (_MINING_DATASET_NAME,))
    if row:
        return row["id"]
    ds_id = _new_id()
    db.execute(
        "INSERT INTO eval_datasets (id, name, description, kind, created_at)"
        " VALUES (?,?,?,?,?)",
        (ds_id, _MINING_DATASET_NAME, "线上问答日志自动挖掘的候选测试集（人工审核后入集）",
         "rag", _now()))
    return ds_id


async def _extract_log_signals(pair: dict) -> dict:
    """LLM 从真实问答提取期望关键词/来源；失败静默降级为空信号。"""
    from app.services import wiki
    titles = pair.get("titles") or []
    messages = [
        {"role": "system", "content": _LOG_EXTRACT_PROMPT},
        {"role": "user", "content": (
            f"【问题】{pair['question'][:500]}\n\n【回答】{pair['answer'][:1500]}\n\n"
            f"【来源标题】{json.dumps(titles, ensure_ascii=False) if titles else '（无）'}")},
    ]
    try:
        raw = await asyncio.wait_for(wiki._llm_complete(messages), timeout=30)
        data = wiki._extract_json(raw)
        source = str(data.get("expected_source") or "").strip()
        # 来源只能取自真实标题（防 LLM 编造）
        if source and titles and source not in titles:
            source = next((t for t in titles if source in t or t in source), "")
        return {"expected_keywords": data.get("expected_keywords") or [],
                "expected_source": source}
    except Exception as e:
        logger.warning("[测试集构建] 日志期望信号提取失败（降级空信号）msg=%s: %s",
                       pair.get("message_id"), e)
        return {"expected_keywords": [], "expected_source": ""}


async def mine_from_logs(dataset_id: Optional[str] = None) -> dict:
    """挖掘一轮线上日志候选（周期任务与手动触发共用入口）。

    新增配对不足 LOG_MINING_MIN_PAIRS 时不触发（游标不推进，等待积累）；
    达量后逐对提取期望信号写候选，游标推进到本批最大 created_at。
    """
    from app.services.config_manager import runtime_config

    if dataset_id and not db.query_one(
            "SELECT id FROM eval_datasets WHERE id = ?", (dataset_id,)):
        raise ValueError("评测集不存在")
    cursor = str(runtime_config.get(_CURSOR_KEY) or "")
    pairs = await asyncio.to_thread(_pick_log_pairs, cursor)
    min_pairs = max(1, int(getattr(settings, "LOG_MINING_MIN_PAIRS", 20)))
    if len(pairs) < min_pairs:
        return {"triggered": False, "pairs": len(pairs), "min_pairs": min_pairs, "created": 0}
    ds_id = _ensure_mining_dataset(dataset_id)
    created = 0
    for pair in pairs:
        signals = await _extract_log_signals(pair)
        _insert_candidate(
            ds_id, "", "log_mining", pair["question"], pair["answer"],
            signals["expected_keywords"], signals["expected_source"],
            {"message_id": pair["message_id"], "session_id": pair["session_id"],
             "titles": pair.get("titles") or []}, "system",
        )
        created += 1
    new_cursor = max(p.get("created_at") or "" for p in pairs)
    if new_cursor > cursor:
        runtime_config.set(_CURSOR_KEY, new_cursor)
    logger.info("[测试集构建] 日志挖掘 %d 对 → 候选 %d 条（dataset=%s）", len(pairs), created, ds_id)
    return {"triggered": True, "pairs": len(pairs), "created": created, "dataset_id": ds_id}


# ---------------------------------------------------------------------------
# 候选审核（通过后入 eval_cases，origin 继承）
# ---------------------------------------------------------------------------
def _candidate_to_dict(row: dict, redact: bool = True) -> dict:
    from app.core.log_setup import redact_log_text
    d = dict(row)
    try:
        d["expected_keywords"] = json.loads(d.get("expected_keywords") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["expected_keywords"] = []
    try:
        d["source_ref"] = json.loads(d.get("source_ref") or "{}")
    except (json.JSONDecodeError, TypeError):
        d["source_ref"] = {}
    if redact:
        d["question"] = redact_log_text(d.get("question") or "")
        d["reference_answer"] = redact_log_text(d.get("reference_answer") or "")
    return d


def list_candidates(dataset_id: str = "", status: str = "",
                    limit: int = 50, offset: int = 0) -> dict:
    """候选列表（分页 + 状态计数；返回前脱敏，原始落库不改写）。"""
    where, params = "1=1", []
    if dataset_id:
        where += " AND dataset_id = ?"
        params.append(dataset_id)
    if status:
        where += " AND status = ?"
        params.append(status)
    total = int((db.query_one(
        f"SELECT COUNT(*) AS n FROM eval_case_candidates WHERE {where}", tuple(params)
    ) or {}).get("n") or 0)
    rows = db.query(
        f"SELECT * FROM eval_case_candidates WHERE {where}"
        " ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (*params, max(1, min(200, limit)), max(0, offset)))
    counts = {r["status"]: r["n"] for r in db.query(
        "SELECT status, COUNT(*) AS n FROM eval_case_candidates GROUP BY status")}
    return {"items": [_candidate_to_dict(r) for r in rows], "total": total,
            "pending_count": int(counts.get("pending") or 0)}


def approve_candidate(candidate_id: str, user: dict, edits: Optional[dict] = None) -> dict:
    """审核通过：可编辑字段后写入 eval_cases（origin 继承），候选标记 approved。"""
    row = db.query_one("SELECT * FROM eval_case_candidates WHERE id = ?", (candidate_id,))
    if not row:
        raise ValueError("候选不存在")
    if row["status"] != "pending":
        raise ValueError("候选已审核，不能重复操作")
    if not db.query_one("SELECT id FROM eval_datasets WHERE id = ?", (row["dataset_id"],)):
        raise ValueError("候选所属评测集已删除")
    edits = edits or {}
    question = str(edits.get("question") or row["question"]).strip()
    if not question:
        raise ValueError("用例问题不能为空")
    reference = str(edits.get("reference_answer")
                    if edits.get("reference_answer") is not None
                    else row["reference_answer"])
    keywords = edits.get("expected_keywords")
    keywords_json = (_clean_keywords(keywords) if keywords is not None
                     else row["expected_keywords"])
    source = str(edits.get("expected_source")
                 if edits.get("expected_source") is not None
                 else row["expected_source"])
    case_id = _new_id()
    db.execute(
        "INSERT INTO eval_cases (id, dataset_id, question, expected_keywords,"
        " reference_answer, expected_source, origin, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (case_id, row["dataset_id"], question[:500], keywords_json,
         reference[:2000], source[:200], row["origin"], _now()),
    )
    user_id = (user or {}).get("id") or (user or {}).get("username") or ""
    db.execute(
        "UPDATE eval_case_candidates SET status = 'approved', review_note = ?,"
        " reviewed_at = ?, reviewed_by = ? WHERE id = ?",
        (str(edits.get("review_note") or "")[:500], _now(), user_id, candidate_id),
    )
    return {"case_id": case_id, "dataset_id": row["dataset_id"], "origin": row["origin"]}


def reject_candidate(candidate_id: str, user: dict, note: str = "") -> dict:
    """审核拒绝（不入集，保留记录可追溯）。"""
    row = db.query_one("SELECT id, status FROM eval_case_candidates WHERE id = ?", (candidate_id,))
    if not row:
        raise ValueError("候选不存在")
    if row["status"] != "pending":
        raise ValueError("候选已审核，不能重复操作")
    user_id = (user or {}).get("id") or (user or {}).get("username") or ""
    db.execute(
        "UPDATE eval_case_candidates SET status = 'rejected', review_note = ?,"
        " reviewed_at = ?, reviewed_by = ? WHERE id = ?",
        ((note or "")[:500], _now(), user_id, candidate_id),
    )
    return {"id": candidate_id, "status": "rejected"}


def batch_approve(candidate_ids: List[str], user: dict) -> dict:
    """批量通过（仅处理 pending；逐条隔离，单条失败不影响其余）。"""
    approved, skipped = 0, 0
    for cid in candidate_ids or []:
        try:
            approve_candidate(cid, user)
            approved += 1
        except ValueError:
            skipped += 1
    return {"approved": approved, "skipped": skipped}


# ---------------------------------------------------------------------------
# 周期任务（lifespan 启停；测试环境不启动，显式调用 mine_from_logs）
# ---------------------------------------------------------------------------
def mining_enabled() -> bool:
    """生效开关：系统设置页运行时覆盖 > .env 默认"""
    from app.services.config_manager import runtime_config
    value = runtime_config.get("testset_mining_enabled")
    default = bool(getattr(settings, "TESTSET_MINING_ENABLED", True))
    return default if value is None else bool(value)


async def _periodic_loop():
    interval = max(60, int(getattr(settings, "TESTSET_MINING_INTERVAL_SECONDS", 3600)))
    while True:
        await asyncio.sleep(interval)
        try:
            await mine_from_logs()
        except Exception as e:
            logger.warning("[测试集构建] 周期挖掘异常（下轮重试）: %s", e)


def start_periodic() -> None:
    global _PERIODIC_TASK
    if not mining_enabled():
        return
    if _PERIODIC_TASK and not _PERIODIC_TASK.done():
        return
    try:
        _PERIODIC_TASK = asyncio.create_task(_periodic_loop())
        logger.info("[测试集构建] 日志挖掘周期任务已启动（间隔 %ds，达量阈值 %d 对）",
                    getattr(settings, "TESTSET_MINING_INTERVAL_SECONDS", 3600),
                    getattr(settings, "LOG_MINING_MIN_PAIRS", 20))
    except RuntimeError:
        logger.warning("[测试集构建] 无运行中的事件循环，周期任务未启动")


async def stop_periodic() -> None:
    global _PERIODIC_TASK
    if _PERIODIC_TASK and not _PERIODIC_TASK.done():
        _PERIODIC_TASK.cancel()
        try:
            await _PERIODIC_TASK
        except (asyncio.CancelledError, Exception):
            pass
    _PERIODIC_TASK = None