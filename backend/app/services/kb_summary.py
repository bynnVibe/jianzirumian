"""
见字如面 - 知识总结（融合开源 book-to-skill 的结构化摘要）

文档入库后，若 owner 开启了内置技能「知识总结（book-to-skill）」（scope=kb_summary），
后台按「确定性切分 + LLM 按规范生成」产出五段式结构化摘要：
核心概览 / 章节结构 / 术语表 / 关键模式 / 速查清单，落 kb_doc_summaries（每文档一份）。

- 技能未开启：入库仅登记 waiting_skill，不做摘要动作；
- 「知识总结」页手动触发时若技能未开启，返回 skill_disabled 引导开启；
- 开启技能后 resume_pending() 将等待中的任务转后台继续执行。
"""
import asyncio
import json
import logging
import re
import threading
import uuid
from datetime import datetime
from typing import List, Optional

from app.core import db
from app.core.llm import LLMFactory
from app.services import skill as skill_svc
from app.services import wiki

logger = logging.getLogger("jianziruyang.kb_summary")

_BG_TASKS: set = set()
# 单文档送入 LLM 的正文字符预算（确定性提取器截断）
_MAX_TEXT_CHARS = 12000
# 章节切块目标大小
_SECTION_CHARS = 1500


def _now() -> str:
    return datetime.now().isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _spawn(coro) -> None:
    """在事件循环内创建后台任务；同步线程上下文（如技能开关 API）退化为独立线程。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        task = loop.create_task(coro)
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)
    else:
        threading.Thread(target=asyncio.run, args=(coro,), daemon=True).start()


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
def list_summaries(kb_id: str) -> List[dict]:
    rows = db.query(
        "SELECT id, kb_id, record_id, title, owner_id, status, error, created_at, updated_at"
        " FROM kb_doc_summaries WHERE kb_id = ? ORDER BY updated_at DESC", (kb_id,)
    )
    return rows


def get_summary(kb_id: str, summary_id: str) -> Optional[dict]:
    row = db.query_one(
        "SELECT * FROM kb_doc_summaries WHERE id = ? AND kb_id = ?", (summary_id, kb_id)
    )
    if not row:
        return None
    try:
        row["structure"] = json.loads(row["structure"] or "{}")
    except (json.JSONDecodeError, TypeError):
        row["structure"] = {}
    return row


def job_status(kb_id: str) -> dict:
    rows = db.query(
        "SELECT status, COUNT(*) AS n FROM kb_doc_summaries WHERE kb_id = ? GROUP BY status",
        (kb_id,),
    )
    counts = {r["status"]: int(r["n"]) for r in rows}
    return {
        "counts": counts,
        "running": counts.get("running", 0) + counts.get("pending", 0),
        "waiting_skill": counts.get("waiting_skill", 0),
        "done": counts.get("done", 0),
        "error": counts.get("error", 0),
    }


# ---------------------------------------------------------------------------
# 调度（入库钩子 / 手动触发 / 技能开启后恢复）
# ---------------------------------------------------------------------------
def _upsert(kb_id: str, record_id: str, title: str, owner_id: str, status: str) -> dict:
    existing = db.query_one(
        "SELECT * FROM kb_doc_summaries WHERE record_id = ?", (record_id,)
    )
    if existing:
        db.execute(
            "UPDATE kb_doc_summaries SET kb_id=?, title=?, owner_id=?, status=?, error='',"
            " updated_at=? WHERE record_id=?",
            (kb_id, title, owner_id, status, _now(), record_id),
        )
        return db.query_one("SELECT * FROM kb_doc_summaries WHERE record_id=?", (record_id,))
    sid = _new_id()
    db.execute(
        "INSERT INTO kb_doc_summaries (id, kb_id, record_id, title, owner_id, status,"
        " structure, error, created_at, updated_at) VALUES (?,?,?,?,?,?,'{}','',?,?)",
        (sid, kb_id, record_id, title, owner_id, status, _now(), _now()),
    )
    return db.query_one("SELECT * FROM kb_doc_summaries WHERE id=?", (sid,))


def schedule_summary(kb_id: str, record_id: str, title: str, owner_id: str,
                     force: bool = False) -> dict:
    """入库钩子：技能开启→pending 入后台；未开启→仅登记 waiting_skill（不摘要）。"""
    existing = db.query_one(
        "SELECT status FROM kb_doc_summaries WHERE record_id = ?", (record_id,)
    )
    if existing and existing["status"] in ("pending", "running"):
        return existing
    if existing and existing["status"] == "done" and not force:
        return existing
    enabled = bool(skill_svc.skill_service.kb_summary_spec(owner_id))
    row = _upsert(kb_id, record_id, title, owner_id,
                  "pending" if enabled else "waiting_skill")
    if enabled:
        _spawn(_run_summary(row["id"]))
    return row


def resume_pending() -> int:
    """技能开启后：将 waiting_skill 任务按其 owner 的技能状态转后台继续执行。"""
    rows = db.query(
        "SELECT * FROM kb_doc_summaries WHERE status = 'waiting_skill'"
        " ORDER BY created_at ASC"
    )
    resumed = 0
    for row in rows:
        if not skill_svc.skill_service.kb_summary_spec(row["owner_id"]):
            continue
        db.execute(
            "UPDATE kb_doc_summaries SET status='pending', updated_at=? WHERE id=?",
            (_now(), row["id"]),
        )
        _spawn(_run_summary(row["id"]))
        resumed += 1
    if resumed:
        logger.info("[KB_SUMMARY] 技能开启后恢复 %d 个等待中的摘要任务", resumed)
    return resumed


def kb_documents(kb_id: str) -> List[dict]:
    """KB 内原始文档清单（record_id/title/owner_id），按向量库元数据归属。"""
    from app.core.vector_store import vector_store
    from app.services.records import upload_records
    sources = []
    seen = set()
    for meta in vector_store._load_metadata().values():
        if meta.get("kb_id") != kb_id or meta.get("is_wiki_page"):
            continue
        src = meta.get("source_image", "")
        if src and src not in seen:
            seen.add(src)
            sources.append(src)
    all_records, _ = upload_records.list_records(limit=10000, offset=0)
    record_map = {rec["image_path"]: rec for rec in all_records}
    docs = []
    for src in sources:
        rec = record_map.get(src)
        if not rec:
            continue
        docs.append({
            "record_id": rec["id"],
            "title": (rec.get("title") or "").strip() or rec.get("filename", ""),
            "owner_id": rec.get("owner_id") or "",
        })
    return docs


def generate(kb_id: str, user_id: str, record_id: str = "", force: bool = False) -> dict:
    """手动触发摘要总结：技能未开启→登记 waiting_skill 并返回 skill_disabled 引导。"""
    if record_id:
        from app.services.records import upload_records
        rec = upload_records.get_record(record_id)
        if not rec:
            raise ValueError("文档不存在")
        targets = [{"record_id": rec["id"],
                    "title": (rec.get("title") or "").strip() or rec.get("filename", ""),
                    "owner_id": rec.get("owner_id") or user_id}]
    else:
        targets = kb_documents(kb_id)
        if not targets:
            raise ValueError("知识库暂无文档，无法生成知识总结")
    enabled = bool(skill_svc.skill_service.kb_summary_spec(user_id))
    scheduled = 0
    for doc in targets:
        owner = doc["owner_id"] or user_id
        existing = db.query_one(
            "SELECT status FROM kb_doc_summaries WHERE record_id = ?", (doc["record_id"],)
        )
        if existing and existing["status"] in ("pending", "running"):
            continue
        if existing and existing["status"] == "done" and not force:
            continue
        row = _upsert(kb_id, doc["record_id"], doc["title"], owner,
                      "pending" if enabled else "waiting_skill")
        if enabled:
            _spawn(_run_summary(row["id"]))
            scheduled += 1
    if not enabled:
        return {
            "skill_disabled": True,
            "skill_id": skill_svc.skill_service.kb_summary_skill_id(user_id),
            "skill_name": "知识总结（book-to-skill）",
            "waiting": len(targets),
        }
    return {"skill_disabled": False, "scheduled": scheduled}


# ---------------------------------------------------------------------------
# 确定性提取器 + LLM 生成器
# ---------------------------------------------------------------------------
_HEADING_RE = re.compile(r"^(第[一二三四五六七八九十百0-9]+[章节篇部分]|[0-9]+[\.、]|[一二三四五六七八九十]+[\.、])")


def _split_sections(text: str) -> List[str]:
    """确定性切分：优先按标题行切块，无标题时按固定字符窗口切块。"""
    lines = text.splitlines()
    sections: List[str] = []
    buf: List[str] = []
    for line in lines:
        if _HEADING_RE.match(line.strip()) and buf:
            sections.append("\n".join(buf))
            buf = []
        buf.append(line)
    if buf:
        sections.append("\n".join(buf))
    if len(sections) <= 1:
        sections = [text[i:i + _SECTION_CHARS]
                    for i in range(0, len(text), _SECTION_CHARS)]
    return [s for s in sections if s.strip()][ :12]


def _validate_structure(data: dict) -> bool:
    if not isinstance(data, dict) or not (data.get("overview") or "").strip():
        return False
    if not isinstance(data.get("chapters"), list) or not data["chapters"]:
        return False
    for ch in data["chapters"]:
        if not isinstance(ch, dict) or not (ch.get("title") or "").strip():
            return False
    return True


async def _llm_structure(spec: str, title: str, text: str, owner_id: str) -> dict:
    sections = _split_sections(text)
    outline = "\n".join(f"{i + 1}. {s.strip().splitlines()[0][:60]}"
                        for i, s in enumerate(sections))
    llm = LLMFactory.create_for_role("kb_summary", user_id=owner_id)
    messages = [
        {"role": "system", "content": spec},
        {"role": "user", "content": (
            f"【文档标题】{title}\n【章节结构概览】\n{outline}\n\n"
            f"【文档正文（节选）】\n{text[:_MAX_TEXT_CHARS]}\n\n"
            "请按规范输出结构化总结 JSON。"
        )},
    ]
    collected = []
    async for chunk in llm.chat(messages, stream=True):
        collected.append(chunk)
    return wiki._extract_json("".join(collected))


async def _run_summary(summary_id: str) -> None:
    row = db.query_one("SELECT * FROM kb_doc_summaries WHERE id = ?", (summary_id,))
    if not row:
        return
    db.execute("UPDATE kb_doc_summaries SET status='running', updated_at=? WHERE id=?",
               (_now(), summary_id))
    try:
        spec = skill_svc.skill_service.kb_summary_spec(row["owner_id"])
        if not spec:
            # 运行前技能被关闭：退回等待态，不执行摘要
            db.execute(
                "UPDATE kb_doc_summaries SET status='waiting_skill', updated_at=? WHERE id=?",
                (_now(), summary_id),
            )
            return
        rec = db.query_one("SELECT * FROM upload_records WHERE id = ?", (row["record_id"],))
        text = ((rec or {}).get("ocr_text") or "").strip()
        if not text:
            raise ValueError("文档正文为空，无法生成总结")
        data = await _llm_structure(spec, row["title"], text, row["owner_id"])
        if not _validate_structure(data):
            # 解析失败重试 1 次
            data = await _llm_structure(spec, row["title"], text, row["owner_id"])
        if not _validate_structure(data):
            raise ValueError("LLM 未返回有效的结构化总结，请重试")
        data.setdefault("glossary", [])
        data.setdefault("patterns", [])
        data.setdefault("cheatsheet", [])
        db.execute(
            "UPDATE kb_doc_summaries SET status='done', structure=?, error='', updated_at=?"
            " WHERE id=?",
            (json.dumps(data, ensure_ascii=False), _now(), summary_id),
        )
        logger.info("[KB_SUMMARY] 摘要完成 record=%s title=%s", row["record_id"][:8], row["title"])
    except Exception as e:
        logger.warning("[KB_SUMMARY] 摘要失败 summary=%s: %s", summary_id[:8], e)
        db.execute(
            "UPDATE kb_doc_summaries SET status='error', error=?, updated_at=? WHERE id=?",
            (str(e)[:500], _now(), summary_id),
        )