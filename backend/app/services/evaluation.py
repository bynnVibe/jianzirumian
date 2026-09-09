"""
见字如面 - Agent 回归评测服务

对问答主链路（检索 → 知识优先 prompt 组装 → 单次 LLM 生成）跑离线回归评测，
量化检索命中、引用忠实度、答案质量（LLM-as-judge）与延迟，支持运行历史对比，
防止检索/prompt/模型优化引入回归。

设计要点：
  - 以发起用户身份调用 retrieve_for_user，尊重知识库权限；
  - 复用 chat 相同的「知识优先」prompt 组装（build_knowledge_prompt）；
  - 忠实度复用 core.evidence_checker（证据充分性 + 引用编号核对）；
  - 评测流程独立于线上会话：不写 chat_sessions/chat_messages、不计 usage、不过游客限流；
  - 用例级异常隔离：单条用例出错记 status=error，不中断整个 run。
"""
import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime
from typing import List, Optional

from app.core import db
from app.core.evidence_checker import check_evidence_sufficiency, verify_citations
from app.core.llm import LLMFactory
from app.services.knowledge import knowledge_service

logger = logging.getLogger("jianziruyang.evaluation")


class ConcurrentRunError(Exception):
    """同一评测集已有正在执行的 run（并发保护）"""


# ---------------------------------------------------------------------------
# LLM-as-judge 中文评分 prompt
# ---------------------------------------------------------------------------
JUDGE_SYSTEM_PROMPT = (
    "你是严格的答案质量评审专家。请对照【参考答案】评估【待评答案】对用户问题的回答质量，"
    "从事实正确性、要点覆盖度、与知识库/参考答案一致性三个维度综合打分。\n\n"
    "评分标准（1-5 分整数）：\n"
    "5 分：完全覆盖参考答案要点，准确无误，表述清晰\n"
    "4 分：覆盖主要要点，个别细节缺失或表述略有出入\n"
    "3 分：覆盖部分要点，存在明显遗漏或少量错误\n"
    "2 分：仅覆盖少量要点，或有较多错误/答非所问\n"
    "1 分：完全错误、答非所问或空泛无实质内容\n\n"
    "只输出 JSON，不要输出任何解释、前后缀或 markdown 标记，格式：\n"
    '{"score": 4, "reason": "一句话说明打分理由"}'
)

# judge 调用超时（秒）：超时按 None 处理，不影响其余指标
JUDGE_TIMEOUT = 30


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now().isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


async def _generate(messages: List[dict], llm_provider: str = None) -> str:
    """单次非流式 LLM 生成，收集全部输出为一个字符串"""
    llm = LLMFactory.create(llm_provider)
    collected = ""
    async for chunk in llm.chat(messages, stream=False):
        collected += chunk
    return collected


def _parse_judge(raw: str) -> Optional[dict]:
    """宽容解析 judge 输出的 JSON（容忍 markdown 包裹与前后噪声）"""
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(data, dict) and "score" in data:
        return data
    return None


async def _judge_answer(question: str, answer: str, reference: str,
                        llm_provider: str = None) -> tuple[Optional[float], str]:
    """LLM-as-judge：对照参考答案给待评答案打 1-5 分 + 一句理由。

    无参考答案或调用失败时返回 (None, "")。
    """
    if not reference or not reference.strip():
        return None, ""
    messages = [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"【用户问题】\n{question}\n\n"
                f"【参考答案】\n{reference}\n\n"
                f"【待评答案】\n{answer or '(空)'}"
            ),
        },
    ]
    try:
        raw = await asyncio.wait_for(_generate(messages, llm_provider), timeout=JUDGE_TIMEOUT)
        parsed = _parse_judge(raw)
        if not parsed:
            logger.warning("judge 输出无法解析: %r", (raw or "")[:120])
            return None, ""
        score = parsed.get("score")
        try:
            score_val = float(score)
        except (TypeError, ValueError):
            return None, ""
        # 收敛到 1-5 区间
        score_val = max(1.0, min(5.0, score_val))
        reason = str(parsed.get("reason", "")).strip()[:500]
        return score_val, reason
    except asyncio.TimeoutError:
        logger.warning("judge 评分超时（%ds）", JUDGE_TIMEOUT)
        return None, ""
    except Exception as e:
        logger.warning("judge 评分失败: %s", e)
        return None, ""


def _compute_retrieval_hit(expected_keywords: List[str], expected_source: str,
                           context_text: str, sources: List[dict]) -> int:
    """检索命中判定：

    - expected_keywords 任一命中检索片段文本；或
    - expected_source 命中来源标题（文件名/百科标题/知识库名）
    命中返回 1，否则 0。无任何期望信号时返回 0。
    """
    ctx = (context_text or "").lower()
    for kw in expected_keywords or []:
        kw = (kw or "").strip().lower()
        if kw and kw in ctx:
            return 1

    exp_src = (expected_source or "").strip().lower()
    if exp_src:
        for s in sources or []:
            title_fields = [
                s.get("wiki_title") or "",
                s.get("kb_name") or "",
                str(s.get("source_image") or "").split("/")[-1],
                s.get("text") or "",
            ]
            for field in title_fields:
                if field and (exp_src in field.lower() or field.lower() in exp_src):
                    return 1
    return 0


async def _compute_faithfulness(question: str, answer: str, context_text: str,
                                sources: List[dict], llm_provider: str = None) -> int:
    """忠实度判定（复用 evidence_checker）：

    - 无检索结果 → 0；
    - 证据充分（check_evidence_sufficiency）且回答引用编号无越界（verify_citations）→ 1；
    - 否则 → 0。
    """
    if not sources:
        return 0
    try:
        sufficient, _missing = await check_evidence_sufficiency(
            question, context_text, llm_provider
        )
    except Exception as e:
        logger.warning("忠实度证据判断异常，按不通过处理: %s", e)
        return 0
    invalid_citations = verify_citations(answer or "", len(sources))
    return 1 if (sufficient and not invalid_citations) else 0


# ---------------------------------------------------------------------------
# 单用例执行
# ---------------------------------------------------------------------------
async def _run_single_case(case: dict, user_id: str, llm_provider: str = None) -> dict:
    """执行单条评测用例，返回结果字段 dict（不含 run_id/id）。

    全流程异常隔离：任何步骤出错都返回 status=error + error 信息。
    """
    question = case["question"]
    try:
        expected_keywords = json.loads(case.get("expected_keywords") or "[]")
    except (json.JSONDecodeError, TypeError):
        expected_keywords = []
    reference_answer = case.get("reference_answer") or ""
    expected_source = case.get("expected_source") or ""

    started = time.monotonic()
    try:
        # 1. 检索（以发起用户身份，尊重权限）
        results = await asyncio.to_thread(
            knowledge_service.retrieve_for_user, question, user_id
        )
        # 2. 知识优先 prompt 组装（与 chat 相同）
        context_text, sources = await asyncio.to_thread(
            knowledge_service.format_knowledge_context, question, results
        )
        augmented_query = knowledge_service.build_knowledge_prompt(
            question, context_text, True
        )

        # 3. 单次非流式 LLM 生成
        messages = [
            {
                "role": "system",
                "content": (
                    "你是「见字如面」智能助手，一个专注于健康养生知识库的问答系统。"
                    "请基于知识库内容准确、有据可查地回答用户问题，引用来源时标注 [来源 X]，"
                    "不确定的内容不要编造。"
                ),
            },
            {"role": "user", "content": augmented_query},
        ]
        answer = await _generate(messages, llm_provider)
        latency_ms = int((time.monotonic() - started) * 1000)

        # 4. 指标计算
        retrieval_hit = _compute_retrieval_hit(
            expected_keywords, expected_source, context_text, sources
        )
        faithfulness_pass = await _compute_faithfulness(
            question, answer, context_text, sources, llm_provider
        )
        judge_score, judge_reason = await _judge_answer(
            question, answer, reference_answer, llm_provider
        )

        # 精简来源，避免结果表过大
        slim_sources = [
            {
                "index": s.get("index"),
                "text": (s.get("text") or "")[:200],
                "relevance": s.get("relevance"),
                "kb_name": s.get("kb_name"),
                "wiki_title": s.get("wiki_title"),
                "source_image": str(s.get("source_image") or "").split("/")[-1],
            }
            for s in (sources or [])
        ]

        return {
            "case_id": case["id"],
            "question": question,
            "answer": answer or "",
            "retrieved_sources": json.dumps(slim_sources, ensure_ascii=False),
            "retrieval_hit": retrieval_hit,
            "faithfulness_pass": faithfulness_pass,
            "judge_score": judge_score,
            "judge_reason": judge_reason,
            "latency_ms": latency_ms,
            "status": "ok",
            "error": "",
        }
    except Exception as e:
        latency_ms = int((time.monotonic() - started) * 1000)
        logger.warning("评测用例执行失败 case=%s: %s", case.get("id"), e)
        return {
            "case_id": case["id"],
            "question": question,
            "answer": "",
            "retrieved_sources": "[]",
            "retrieval_hit": 0,
            "faithfulness_pass": 0,
            "judge_score": None,
            "judge_reason": "",
            "latency_ms": latency_ms,
            "status": "error",
            "error": str(e)[:500],
        }


def _insert_result(run_id: str, r: dict):
    db.execute(
        "INSERT INTO eval_results (id, run_id, case_id, question, answer, retrieved_sources,"
        " retrieval_hit, faithfulness_pass, judge_score, judge_reason, latency_ms, status, error)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            _new_id(), run_id, r["case_id"], r["question"], r["answer"],
            r["retrieved_sources"], r["retrieval_hit"], r["faithfulness_pass"],
            r["judge_score"], r["judge_reason"], r["latency_ms"], r["status"], r["error"],
        ),
    )


def _compute_summary(results: List[dict]) -> dict:
    """聚合 run 级指标"""
    total = len(results)
    ok_rows = [r for r in results if r["status"] == "ok"]
    err_count = total - len(ok_rows)
    ok = len(ok_rows)

    hit_sum = sum(r["retrieval_hit"] for r in ok_rows)
    faith_sum = sum(r["faithfulness_pass"] for r in ok_rows)
    judge_scores = [r["judge_score"] for r in ok_rows if r["judge_score"] is not None]
    latencies = [r["latency_ms"] for r in ok_rows]

    return {
        "total": total,
        "ok": ok,
        "error": err_count,
        "retrieval_hit_rate": round(hit_sum / ok, 4) if ok else 0.0,
        "faithfulness_pass_rate": round(faith_sum / ok, 4) if ok else 0.0,
        "avg_judge_score": round(sum(judge_scores) / len(judge_scores), 2) if judge_scores else None,
        "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
    }


async def _run_all(run_id: str, cases: List[dict], user_id: str, llm_provider: str = None):
    """后台逐用例执行整个 run（顺序执行，避免并发压垮 LLM/Embedding）"""
    results: List[dict] = []
    try:
        for case in cases:
            r = await _run_single_case(case, user_id, llm_provider)
            _insert_result(run_id, r)
            results.append(r)
        summary = _compute_summary(results)
        db.execute(
            "UPDATE eval_runs SET status = 'finished', finished_at = ?, summary = ? WHERE id = ?",
            (_now(), json.dumps(summary, ensure_ascii=False), run_id),
        )
        logger.info("评测 run 完成: %s | %s", run_id, summary)
    except Exception as e:
        logger.error("评测 run 整体失败: %s | %s", run_id, e)
        db.execute(
            "UPDATE eval_runs SET status = 'failed', finished_at = ?, summary = ? WHERE id = ?",
            (_now(), json.dumps({"error": str(e)[:500]}, ensure_ascii=False), run_id),
        )


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def start_run(dataset_id: str, user: dict, llm_provider: str = None) -> str:
    """创建 run 并后台逐用例执行，立即返回 run_id。

    并发保护：同一 dataset 已有 running 的 run 时抛 ConcurrentRunError。
    """
    dataset = db.query_one("SELECT * FROM eval_datasets WHERE id = ?", (dataset_id,))
    if not dataset:
        raise ValueError("评测集不存在")

    cases = db.query(
        "SELECT * FROM eval_cases WHERE dataset_id = ? ORDER BY created_at, rowid",
        (dataset_id,),
    )
    if not cases:
        raise ValueError("评测集内没有用例，无法发起评测")

    running = db.query_one(
        "SELECT id FROM eval_runs WHERE dataset_id = ? AND status = 'running'",
        (dataset_id,),
    )
    if running:
        raise ConcurrentRunError("该评测集已有正在执行的评测，请等待其完成后再发起")

    run_id = _new_id()
    user_id = user.get("id") or user.get("username") or ""
    db.execute(
        "INSERT INTO eval_runs (id, dataset_id, status, started_at, finished_at, summary, created_by)"
        " VALUES (?,?,?,?,?,?,?)",
        (run_id, dataset_id, "running", _now(), None, "{}", user_id),
    )

    asyncio.create_task(_run_all(run_id, cases, user_id, llm_provider))
    logger.info("发起评测 run: %s | dataset=%s | cases=%d | by=%s",
                run_id, dataset_id, len(cases), user_id)
    return run_id
