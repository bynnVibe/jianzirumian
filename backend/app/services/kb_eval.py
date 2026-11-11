"""
见字如面 - 按知识库的 RAG 评估
用例（手动添加 / LLM 自动生成）→ 运行（KB 内混合检索 + LLM 作答 + Ragas 四指标 + judge）
→ 逐用例结果与汇总。复用 evaluation 服务的指标实现，检索限定单个 KB。
"""
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from typing import List

from app.config import settings
from app.core import db
from app.core.llm import LLMFactory
from app.services import evaluation, wiki
from app.services.kb_content import build_kb_context
from app.services.knowledge import KnowledgeService

logger = logging.getLogger("jianziruyang.kb_eval")

_BG_TASKS: set = set()

_ANSWER_PROMPT = (
    "你是知识库问答助手。请仅依据给定的知识库检索片段回答用户问题；"
    "片段不足以回答时明确说明「知识库未涵盖」，不得编造。用中文简洁作答。"
)

_CASE_GEN_PROMPT = (
    "你是「RAG 评估出题专家」。请基于给定的知识库内容，生成用于检索增强问答评估的用例，"
    "只输出一个 JSON 对象，不要输出任何解释、不要用代码块包裹。格式：\n"
    '{"cases": [{"question": "用户问题", "reference_answer": "基于内容的参考答案",'
    ' "expected_keywords": ["答案应命中的关键词，2-4 个"]}]}\n'
    "要求：问题覆盖内容中的不同主题，答案严格忠于内容，关键词取自参考答案。"
)


def _now() -> str:
    return datetime.now().isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# 用例管理
# ---------------------------------------------------------------------------
def list_cases(kb_id: str) -> List[dict]:
    rows = db.query(
        "SELECT * FROM kb_eval_cases WHERE kb_id = ? ORDER BY created_at DESC", (kb_id,)
    )
    for r in rows:
        r["expected_keywords"] = json.loads(r["expected_keywords"] or "[]")
    return rows


def add_case(kb_id: str, question: str, reference_answer: str,
             expected_keywords: List[str], user_id: str) -> dict:
    question = (question or "").strip()
    if not question:
        raise ValueError("问题不能为空")
    case = {
        "id": _new_id(),
        "kb_id": kb_id,
        "question": question[:500],
        "reference_answer": (reference_answer or "").strip()[:2000],
        "expected_keywords": json.dumps(
            [str(k).strip() for k in (expected_keywords or []) if str(k).strip()][:6],
            ensure_ascii=False,
        ),
        "created_by": user_id,
        "created_at": _now(),
    }
    db.execute(
        "INSERT INTO kb_eval_cases (id, kb_id, question, reference_answer,"
        " expected_keywords, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
        (case["id"], kb_id, case["question"], case["reference_answer"],
         case["expected_keywords"], user_id, case["created_at"]),
    )
    case["expected_keywords"] = json.loads(case["expected_keywords"])
    return case


def delete_case(kb_id: str, case_id: str) -> bool:
    return db.execute(
        "DELETE FROM kb_eval_cases WHERE id = ? AND kb_id = ?", (case_id, kb_id)
    ) > 0


async def generate_cases(kb_id: str, kb_name: str, user_id: str, count: int = 5) -> List[dict]:
    context = build_kb_context(kb_id, 8000)
    if not context.strip():
        raise ValueError("知识库暂无内容，无法自动生成评估用例")
    messages = [
        {"role": "system", "content": _CASE_GEN_PROMPT},
        {"role": "user", "content": f"【知识库】{kb_name}\n\n【内容】\n{context}\n\n请生成 {count} 个用例。"},
    ]
    raw = await wiki._llm_complete(messages)
    data = wiki._extract_json(raw)
    items = data.get("cases") or []
    added = []
    for item in items[:count]:
        if not isinstance(item, dict) or not (item.get("question") or "").strip():
            continue
        added.append(add_case(
            kb_id, item["question"], item.get("reference_answer") or "",
            item.get("expected_keywords") or [], user_id,
        ))
    if not added:
        raise ValueError("LLM 未返回有效用例，请重试")
    return added


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------
def list_runs(kb_id: str) -> List[dict]:
    rows = db.query(
        "SELECT * FROM kb_eval_runs WHERE kb_id = ? ORDER BY started_at DESC LIMIT 50",
        (kb_id,),
    )
    for r in rows:
        r["summary"] = json.loads(r["summary"] or "{}")
    return rows


def get_run(kb_id: str, run_id: str) -> dict | None:
    run = db.query_one(
        "SELECT * FROM kb_eval_runs WHERE id = ? AND kb_id = ?", (run_id, kb_id)
    )
    if not run:
        return None
    run["summary"] = json.loads(run["summary"] or "{}")
    results = db.query(
        "SELECT * FROM kb_eval_results WHERE run_id = ? ORDER BY rowid", (run_id,)
    )
    for r in results:
        r["contexts"] = json.loads(r["contexts"] or "[]")
    run["results"] = results
    return run


def start_run(kb_id: str, user_id: str) -> str:
    running = db.query_one(
        "SELECT id FROM kb_eval_runs WHERE kb_id = ? AND status = 'running'", (kb_id,)
    )
    if running:
        raise evaluation.ConcurrentRunError("该知识库已有评估运行中，请等待完成")
    cases = list_cases(kb_id)
    if not cases:
        raise ValueError("请先添加或自动生成评估用例")
    run_id = _new_id()
    db.execute(
        "INSERT INTO kb_eval_runs (id, kb_id, status, summary, created_by, started_at)"
        " VALUES (?,?,?,?,?,?)",
        (run_id, kb_id, "running", "{}", user_id, _now()),
    )
    task = asyncio.create_task(_run_all(run_id, kb_id, cases, user_id))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return run_id


async def _answer_question(question: str, context_text: str) -> str:
    llm = LLMFactory.create_for_role("chat_answer")
    messages = [
        {"role": "system", "content": _ANSWER_PROMPT},
        {"role": "user", "content": f"【检索片段】\n{context_text or '(无)'}\n\n【问题】\n{question}"},
    ]
    collected = []
    async for chunk in llm.chat(messages, stream=True):
        collected.append(chunk)
    return "".join(collected).strip()


async def _run_single(run_id: str, case: dict, kb_id: str, user_id: str) -> dict:
    t0 = time.monotonic()
    question = case["question"]
    reference = case.get("reference_answer") or ""
    keywords = case.get("expected_keywords") or []
    result = {
        "id": _new_id(), "run_id": run_id, "case_id": case["id"],
        "question": question, "answer": "", "contexts": [],
        "retrieval_hit": 0, "faithfulness": None, "answer_relevance": None,
        "context_precision": None, "context_recall": None,
        "judge_score": None, "judge_reason": "", "latency_ms": 0,
        "status": "ok", "error": "",
    }
    try:
        retrieved = await asyncio.to_thread(
            KnowledgeService._hybrid_search, question, user_id, {kb_id},
            settings.TOP_K_RETRIEVAL,
        )
        contexts = [text for text, _meta, _score in retrieved]
        result["contexts"] = contexts
        context_text = "\n\n".join(f"[{i + 1}] {c}" for i, c in enumerate(contexts))

        answer = await _answer_question(question, context_text)
        result["answer"] = answer

        (faith, _), (relevance, _), (precision, _), (recall, _) = await asyncio.gather(
            evaluation._statement_metric(answer, context_text),
            evaluation._compute_answer_relevance(question, answer),
            evaluation._compute_context_precision(question, reference, contexts),
            evaluation._statement_metric(reference, context_text) if reference.strip()
            else _none_metric(),
        )
        result["faithfulness"] = faith
        result["answer_relevance"] = relevance
        result["context_precision"] = precision
        result["context_recall"] = recall
        result["retrieval_hit"] = evaluation._compute_retrieval_hit(
            keywords, "", context_text, []
        )
        judge_score, judge_reason = await evaluation._judge_answer(question, answer, reference)
        result["judge_score"] = judge_score
        result["judge_reason"] = judge_reason
    except Exception as e:
        logger.warning("[KB_EVAL] 用例执行失败 case=%s: %s", case["id"][:8], e)
        result["status"] = "error"
        result["error"] = str(e)[:500]
    result["latency_ms"] = int((time.monotonic() - t0) * 1000)
    return result


async def _none_metric():
    return None, {}


def _avg(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


async def _run_all(run_id: str, kb_id: str, cases: List[dict], user_id: str) -> None:
    results = []
    try:
        for case in cases:
            r = await _run_single(run_id, case, kb_id, user_id)
            results.append(r)
            db.execute(
                "INSERT INTO kb_eval_results (id, run_id, case_id, question, answer, contexts,"
                " retrieval_hit, faithfulness, answer_relevance, context_precision, context_recall,"
                " judge_score, judge_reason, latency_ms, status, error)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["id"], run_id, r["case_id"], r["question"], r["answer"],
                 json.dumps(r["contexts"], ensure_ascii=False), r["retrieval_hit"],
                 r["faithfulness"], r["answer_relevance"], r["context_precision"],
                 r["context_recall"], r["judge_score"], r["judge_reason"],
                 r["latency_ms"], r["status"], r["error"]),
            )
        ok_rows = [r for r in results if r["status"] == "ok"]
        ok = len(ok_rows)
        summary = {
            "total": len(results),
            "ok": ok,
            "error": len(results) - ok,
            "retrieval_hit_rate": round(sum(r["retrieval_hit"] for r in ok_rows) / ok, 4) if ok else 0.0,
            "avg_faithfulness": _avg([r["faithfulness"] for r in ok_rows]),
            "avg_answer_relevance": _avg([r["answer_relevance"] for r in ok_rows]),
            "avg_context_precision": _avg([r["context_precision"] for r in ok_rows]),
            "avg_context_recall": _avg([r["context_recall"] for r in ok_rows]),
            "avg_judge_score": _avg([r["judge_score"] for r in ok_rows]),
            "avg_latency_ms": int(sum(r["latency_ms"] for r in ok_rows) / ok) if ok else 0,
        }
        status = "finished" if ok else "failed"
    except Exception as e:
        logger.exception("[KB_EVAL] 运行整体失败 run=%s", run_id[:8])
        summary = {"error": str(e)[:500]}
        status = "failed"
    db.execute(
        "UPDATE kb_eval_runs SET status = ?, summary = ?, finished_at = ? WHERE id = ?",
        (status, json.dumps(summary, ensure_ascii=False), _now(), run_id),
    )
    logger.info("[KB_EVAL] 运行完成 run=%s status=%s cases=%d", run_id[:8], status, len(results))