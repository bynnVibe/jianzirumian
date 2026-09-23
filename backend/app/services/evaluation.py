"""
见字如面 - Agent 回归评测服务

对问答主链路（检索 → 知识优先 prompt 组装 → 单次 LLM 生成）跑离线回归评测，
评估框架仿照 Ragas（RAG Assessment），输出四项核心指标：
  - Faithfulness（忠实度）：答案拆原子陈述，逐条验证可否由检索上下文推导；
  - Answer Relevance（回答相关性）：LLM 从答案反推问题，与原问题做嵌入余弦相似度；
  - Context Precision（上下文精确率）：逐片段判定相关性，按 Ragas 加权精确率聚合；
  - Context Recall（上下文召回率）：参考答案拆陈述，逐条验证是否被检索上下文覆盖；
另保留检索命中率（规则判定）、LLM-as-judge 对照分与延迟作为辅助指标，支持运行历史对比，
防止检索/prompt/模型优化引入回归。

设计要点：
  - 以发起用户身份调用 retrieve_for_user，尊重知识库权限；
  - 复用 chat 相同的「知识优先」prompt 组装（build_knowledge_prompt）；
  - 四项 Ragas 指标由 LLM/Embedding 驱动，单点失败降级为 None（不计入均值），不中断 run；
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
from app.core.embeddings import EmbeddingFactory
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
# Ragas 风格指标 prompt 与阈值
# ---------------------------------------------------------------------------
# 单项 Ragas 指标 LLM 调用超时（秒）：超时该指标降级为 None
METRIC_TIMEOUT = 30
# 忠实度通过阈值：Faithfulness >= 该值记 faithfulness_pass=1
FAITH_PASS_THRESHOLD = 0.75
# 陈述拆分上限 / 反推问题上限 / 单片段送入判定的字符上限（控制 token 成本）
_MAX_STATEMENTS = 20
_MAX_GEN_QUESTIONS = 3
_MAX_CONTEXT_CHARS = 800

STATEMENT_SYSTEM_PROMPT = (
    "你是评估助手。请把给定文本拆分为若干条互相独立、可单独验证的原子陈述句，"
    "不遗漏关键信息，也不添加原文没有的内容。\n"
    "只输出 JSON，不要输出任何解释、前后缀或 markdown 标记，格式：\n"
    '{"statements": ["陈述1", "陈述2"]}'
)

NLI_SYSTEM_PROMPT = (
    "你是严格的自然语言推断评估器。给定【上下文】与一组编号【陈述】，"
    "逐条判断该陈述能否由上下文推导或与其一致（允许同义改写，"
    "但上下文没有的额外信息判 0）。\n"
    "只输出 JSON，不要输出任何解释、前后缀或 markdown 标记，格式：\n"
    '{"verdicts": [1, 0]}'
    "（1=可由上下文推导，0=不能；数组长度须与陈述条数一致）"
)

CTX_RELEVANCE_SYSTEM_PROMPT = (
    "你是检索质量评估器。给定【问题】、【参考答案】与按检索排序编号的若干【检索片段】，"
    "逐条判断该片段是否对回答问题有用（包含参考答案所需信息或相关线索）。\n"
    "只输出 JSON，不要输出任何解释、前后缀或 markdown 标记，格式：\n"
    '{"verdicts": [1, 0]}'
    "（1=相关有用，0=无关；数组长度须与片段条数一致）"
)

QUESTION_GEN_SYSTEM_PROMPT = (
    "你是评估助手。给定一段【回答】，请反推它可能在回答的 3 个不同问题，"
    "问题应互相独立、表述自然。\n"
    "只输出 JSON，不要输出任何解释、前后缀或 markdown 标记，格式：\n"
    '{"questions": ["问题1", "问题2", "问题3"]}'
)


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


def _parse_json(raw: str) -> Optional[dict]:
    """宽容解析 LLM 输出的 JSON 对象（容忍 markdown 包裹与前后噪声）"""
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
    return data if isinstance(data, dict) else None


def _parse_judge(raw: str) -> Optional[dict]:
    """解析 judge 输出（需含 score 字段）"""
    data = _parse_json(raw)
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


# ---------------------------------------------------------------------------
# Ragas 风格指标
# ---------------------------------------------------------------------------
def _cosine(a: List[float], b: List[float]) -> float:
    """向量余弦相似度（纯 Python 实现，避免额外数值依赖）"""
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _split_sentences(text: str) -> List[str]:
    """陈述拆分降级方案：按中文句读切分"""
    parts = re.split(r"[。！？；\n]+", text or "")
    return [p.strip() for p in parts if p and p.strip()][:_MAX_STATEMENTS]


def _coerce_verdicts(raw_verdicts: list, expect: int) -> List[int]:
    """把 LLM 返回的 verdicts 收敛为长度 expect 的 0/1 列表（不足补 0、超出截断）"""
    out = []
    for i in range(expect):
        v = raw_verdicts[i] if i < len(raw_verdicts) else 0
        try:
            out.append(1 if int(v) == 1 else 0)
        except (TypeError, ValueError):
            out.append(0)
    return out


async def _extract_statements(text: str, llm_provider: str = None) -> List[str]:
    """Ragas 陈述拆分：LLM 把文本拆为可独立验证的原子陈述；失败回退句读切分。"""
    text = (text or "").strip()
    if not text:
        return []
    messages = [
        {"role": "system", "content": STATEMENT_SYSTEM_PROMPT},
        {"role": "user", "content": f"【文本】\n{text}"},
    ]
    try:
        raw = await asyncio.wait_for(_generate(messages, llm_provider), timeout=METRIC_TIMEOUT)
        data = _parse_json(raw)
        stmts = data.get("statements") if data else None
        if isinstance(stmts, list):
            out = [str(s).strip() for s in stmts if str(s).strip()]
            if out:
                return out[:_MAX_STATEMENTS]
    except asyncio.TimeoutError:
        logger.warning("陈述拆分超时（%ds），回退句读切分", METRIC_TIMEOUT)
    except Exception as e:
        logger.warning("陈述拆分失败，回退句读切分: %s", e)
    return _split_sentences(text)


async def _verify_statements(statements: List[str], context_text: str,
                             llm_provider: str = None) -> Optional[List[int]]:
    """NLI 验证：逐条判断陈述能否由上下文推导，返回等长 0/1 列表；调用失败返回 None。"""
    if not statements:
        return []
    if not (context_text or "").strip():
        return [0] * len(statements)
    numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(statements))
    messages = [
        {"role": "system", "content": NLI_SYSTEM_PROMPT},
        {"role": "user", "content": f"【上下文】\n{context_text}\n\n【陈述】\n{numbered}"},
    ]
    try:
        raw = await asyncio.wait_for(_generate(messages, llm_provider), timeout=METRIC_TIMEOUT)
        data = _parse_json(raw)
        verdicts = data.get("verdicts") if data else None
        if isinstance(verdicts, list):
            return _coerce_verdicts(verdicts, len(statements))
    except asyncio.TimeoutError:
        logger.warning("陈述验证超时（%ds）", METRIC_TIMEOUT)
    except Exception as e:
        logger.warning("陈述验证失败: %s", e)
    return None


async def _statement_metric(text: str, context_text: str,
                            llm_provider: str = None) -> tuple:
    """陈述级指标通用流程（Faithfulness / Context Recall 共用）：
    拆陈述 → NLI 验证 → supported/total。返回 (score, detail)，无法计算时 score 为 None。
    """
    statements = await _extract_statements(text, llm_provider)
    if not statements:
        return None, {"statements": [], "verdicts": []}
    verdicts = await _verify_statements(statements, context_text, llm_provider)
    if verdicts is None:
        return None, {"statements": statements, "verdicts": []}
    score = round(sum(verdicts) / len(verdicts), 4)
    return score, {"statements": statements, "verdicts": verdicts}


def _weighted_precision(verdicts: List[int]) -> float:
    """Ragas 加权精确率：仅对相关片段累计 precision@k 求均值；无相关片段返回 0。"""
    relevant = sum(verdicts)
    if relevant == 0:
        return 0.0
    cum = 0
    acc = 0.0
    for rank, v in enumerate(verdicts, start=1):
        if v == 1:
            cum += 1
            acc += cum / rank
    return round(acc / relevant, 4)


async def _compute_context_precision(question: str, reference: str, contexts: List[str],
                                     llm_provider: str = None) -> tuple:
    """Ragas Context Precision：逐片段判定相关性，按加权精确率聚合。

    score = (Σ_{k: 第 k 片相关} precision@k) / 相关片段总数；有片段但无相关记 0；
    无检索片段或判定失败返回 None。
    """
    chunks = [c for c in (contexts or []) if (c or "").strip()]
    if not chunks:
        return None, {"verdicts": []}
    numbered = "\n".join(f"[{i + 1}] {c[:_MAX_CONTEXT_CHARS]}" for i, c in enumerate(chunks))
    messages = [
        {"role": "system", "content": CTX_RELEVANCE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"【问题】\n{question}\n\n"
                f"【参考答案】\n{reference or '(无)'}\n\n"
                f"【检索片段】\n{numbered}"
            ),
        },
    ]
    verdicts = None
    try:
        raw = await asyncio.wait_for(_generate(messages, llm_provider), timeout=METRIC_TIMEOUT)
        data = _parse_json(raw)
        raw_verdicts = data.get("verdicts") if data else None
        if isinstance(raw_verdicts, list):
            verdicts = _coerce_verdicts(raw_verdicts, len(chunks))
    except asyncio.TimeoutError:
        logger.warning("上下文相关性判定超时（%ds）", METRIC_TIMEOUT)
    except Exception as e:
        logger.warning("上下文相关性判定失败: %s", e)
    if verdicts is None:
        return None, {"verdicts": []}
    return _weighted_precision(verdicts), {"verdicts": verdicts}


async def _compute_answer_relevance(question: str, answer: str,
                                    llm_provider: str = None) -> tuple:
    """Ragas Answer Relevance：LLM 从答案反推问题，与原问题做嵌入余弦相似度取均值。"""
    if not (answer or "").strip():
        return None, {"questions": [], "sims": []}
    messages = [
        {"role": "system", "content": QUESTION_GEN_SYSTEM_PROMPT},
        {"role": "user", "content": f"【回答】\n{answer}"},
    ]
    questions: List[str] = []
    try:
        raw = await asyncio.wait_for(_generate(messages, llm_provider), timeout=METRIC_TIMEOUT)
        data = _parse_json(raw)
        qs = data.get("questions") if data else None
        if isinstance(qs, list):
            questions = [str(q).strip() for q in qs if str(q).strip()][:_MAX_GEN_QUESTIONS]
    except asyncio.TimeoutError:
        logger.warning("反推问题超时（%ds）", METRIC_TIMEOUT)
    except Exception as e:
        logger.warning("反推问题失败: %s", e)
    if not questions:
        return None, {"questions": [], "sims": []}
    try:
        emb = EmbeddingFactory.create()
        vectors = await asyncio.to_thread(emb.embed_documents, questions + [question])
        q_vec = vectors[-1]
        sims = [max(0.0, min(1.0, _cosine(v, q_vec))) for v in vectors[:-1]]
        return round(sum(sims) / len(sims), 4), {
            "questions": questions,
            "sims": [round(s, 4) for s in sims],
        }
    except Exception as e:
        logger.warning("回答相关性嵌入相似度计算失败: %s", e)
        return None, {"questions": questions, "sims": []}


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

        # 4. 指标计算（Ragas 四指标并发评估，单点失败降级 None；judge 作为辅助对照）
        retrieval_hit = _compute_retrieval_hit(
            expected_keywords, expected_source, context_text, sources
        )
        contexts = [s.get("text") or "" for s in (sources or [])]
        metric_out = await asyncio.gather(
            _statement_metric(answer, context_text, llm_provider),            # Faithfulness
            _compute_answer_relevance(question, answer, llm_provider),       # Answer Relevance
            _compute_context_precision(                                      # Context Precision
                question, reference_answer, contexts, llm_provider
            ),
            _statement_metric(reference_answer, context_text, llm_provider),  # Context Recall
            return_exceptions=True,
        )
        metric_scores = []
        metric_details = {}
        for key, item in zip(
            ("faithfulness", "answer_relevance", "context_precision", "context_recall"),
            metric_out,
        ):
            if isinstance(item, BaseException):
                logger.warning("指标 %s 计算异常 case=%s: %s", key, case.get("id"), item)
                item = (None, {})
            score, detail = item
            metric_scores.append(score)
            metric_details[key] = detail
        faith_score, rel_score, prec_score, recall_score = metric_scores
        faithfulness_pass = (
            1 if (faith_score is not None and faith_score >= FAITH_PASS_THRESHOLD) else 0
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
            "faithfulness": faith_score,
            "answer_relevance": rel_score,
            "context_precision": prec_score,
            "context_recall": recall_score,
            "metric_details": json.dumps(metric_details, ensure_ascii=False),
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
            "faithfulness": None,
            "answer_relevance": None,
            "context_precision": None,
            "context_recall": None,
            "metric_details": "{}",
            "judge_score": None,
            "judge_reason": "",
            "latency_ms": latency_ms,
            "status": "error",
            "error": str(e)[:500],
        }


def _insert_result(run_id: str, r: dict):
    db.execute(
        "INSERT INTO eval_results (id, run_id, case_id, question, answer, retrieved_sources,"
        " retrieval_hit, faithfulness_pass, faithfulness, answer_relevance, context_precision,"
        " context_recall, metric_details, judge_score, judge_reason, latency_ms, status, error)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            _new_id(), run_id, r["case_id"], r["question"], r["answer"],
            r["retrieved_sources"], r["retrieval_hit"], r["faithfulness_pass"],
            r["faithfulness"], r["answer_relevance"], r["context_precision"],
            r["context_recall"], r["metric_details"],
            r["judge_score"], r["judge_reason"], r["latency_ms"], r["status"], r["error"],
        ),
    )


def _compute_summary(results: List[dict]) -> dict:
    """聚合 run 级指标（Ragas 四指标取成功用例的非空均值）"""
    total = len(results)
    ok_rows = [r for r in results if r["status"] == "ok"]
    err_count = total - len(ok_rows)
    ok = len(ok_rows)

    hit_sum = sum(r["retrieval_hit"] for r in ok_rows)
    faith_sum = sum(r["faithfulness_pass"] for r in ok_rows)
    judge_scores = [r["judge_score"] for r in ok_rows if r["judge_score"] is not None]
    latencies = [r["latency_ms"] for r in ok_rows]

    def _avg(key: str) -> Optional[float]:
        vals = [r[key] for r in ok_rows if r.get(key) is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    return {
        "total": total,
        "ok": ok,
        "error": err_count,
        "retrieval_hit_rate": round(hit_sum / ok, 4) if ok else 0.0,
        "faithfulness_pass_rate": round(faith_sum / ok, 4) if ok else 0.0,
        "avg_faithfulness": _avg("faithfulness"),
        "avg_answer_relevance": _avg("answer_relevance"),
        "avg_context_precision": _avg("context_precision"),
        "avg_context_recall": _avg("context_recall"),
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
