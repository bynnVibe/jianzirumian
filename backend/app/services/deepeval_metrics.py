"""
见字如面 - DeepEval 风格评测指标（自研实现，零新增重依赖）

基于 DeepEval 的技术原理与功能模块映射到本系统：
- LLMTestCase：对齐 DeepEval 的用例抽象（input / actual_output / expected_output /
  retrieval_context），离线评测集与在线采样评测共用同一载体；
- 指标注册表：Ragas 四指标（faithfulness / answer_relevancy / contextual_precision /
  contextual_recall）委托既有 evaluation.py 实现（单一事实源，不重写）；
  新增 DeepEval 特色指标：
  - HallucinationMetric：答案拆原子陈述，逐条判定是否与检索上下文矛盾，
    输出矛盾率（0~1，越低越好；与 Faithfulness 的「可推导率」互补——
    不可推导≠矛盾，矛盾才是幻觉）；
  - GEval：自定义准则（criteria 文本）驱动的链式评估，LLM 按准则输出 1-10 分
    与理由，归一化到 0~1（对齐 DeepEval GEval 的 criteria-based scoring）。

设计要点（与既有评测体系一致）：
- 全部经 LLMFactory 驱动，单点失败/超时降级为 None（不计入均值），不中断 run；
- 不引入 deepeval 官方包：兼容阿里云离线部署与既有钉住依赖。
"""
import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

from app.config import settings

logger = logging.getLogger("jianziruyang.deepeval_metrics")

# 单项指标 LLM 调用超时（秒）：与 evaluation.METRIC_TIMEOUT 同口径
METRIC_TIMEOUT = 30

# 指标注册表：名称 → 类别（ragas 委托既有实现 / deepeval 本模块实现）
RAGAS_METRICS = ("faithfulness", "answer_relevancy", "contextual_precision", "contextual_recall")
DEEPEVAL_METRICS = ("hallucination", "geval")
ALL_METRICS = RAGAS_METRICS + DEEPEVAL_METRICS

METRIC_LABELS = {
    "faithfulness": "忠实度",
    "answer_relevancy": "回答相关性",
    "contextual_precision": "上下文精确率",
    "contextual_recall": "上下文召回率",
    "hallucination": "幻觉率",
    "geval": "GEval 准则评分",
}

# 默认评估器指标集（空配置兼容存量：仅 Ragas 四指标）
DEFAULT_METRICS = list(RAGAS_METRICS)


@dataclass
class LLMTestCase:
    """DeepEval 风格用例载体：离线评测与在线采样评测共用。"""
    input: str
    actual_output: str
    expected_output: str = ""
    retrieval_context: List[str] = field(default_factory=list)

    @property
    def context_text(self) -> str:
        return "\n\n".join(c for c in (self.retrieval_context or []) if (c or "").strip())


def normalize_metrics(raw) -> List[str]:
    """把评估器 metrics 配置收敛为合法指标名列表；空/非法 → 默认 Ragas 四指标。

    兼容两种存储形态：JSON 数组（仅指标名）或 JSON 对象 {"metrics": [...], ...}。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else None
        except (json.JSONDecodeError, TypeError):
            raw = None
    if isinstance(raw, dict):
        raw = raw.get("metrics")
    if not isinstance(raw, list):
        return list(DEFAULT_METRICS)
    out = [str(m).strip() for m in raw if str(m).strip() in ALL_METRICS]
    return out or list(DEFAULT_METRICS)


# ---------------------------------------------------------------------------
# HallucinationMetric：矛盾判定（与 Faithfulness 的可推导判定互补）
# ---------------------------------------------------------------------------
CONTRADICTION_SYSTEM_PROMPT = (
    "你是严格的幻觉检测器。给定【上下文】与一组编号【陈述】，"
    "逐条判断该陈述是否与上下文矛盾（陈述断言的内容与上下文明确冲突，"
    "或上下文给出了相反的事实）。注意：上下文未提及的信息不算矛盾（判 0），"
    "只有明确冲突才判 1。\n"
    "只输出 JSON，不要输出任何解释、前后缀或 markdown 标记，格式：\n"
    '{"verdicts": [0, 1]}'
    "（1=与上下文矛盾，0=不矛盾；数组长度须与陈述条数一致）"
)


async def measure_hallucination(test_case: LLMTestCase,
                                llm_provider: str = None) -> tuple:
    """幻觉率 = 与上下文矛盾的陈述数 / 陈述总数（越低越好）。

    无答案/无上下文/判定失败 → (None, detail)，不计入均值。
    """
    from app.services import evaluation as ev

    answer = (test_case.actual_output or "").strip()
    context_text = test_case.context_text
    if not answer or not context_text.strip():
        return None, {"statements": [], "verdicts": [], "reason": "无答案或无上下文"}
    statements = await ev._extract_statements(answer, llm_provider)
    if not statements:
        return None, {"statements": [], "verdicts": []}
    numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(statements))
    messages = [
        {"role": "system", "content": CONTRADICTION_SYSTEM_PROMPT},
        {"role": "user", "content": f"【上下文】\n{context_text}\n\n【陈述】\n{numbered}"},
    ]
    try:
        raw = await asyncio.wait_for(ev._generate(messages, llm_provider), timeout=METRIC_TIMEOUT)
        data = ev._parse_json(raw)
        verdicts = data.get("verdicts") if data else None
        if isinstance(verdicts, list):
            verdicts = ev._coerce_verdicts(verdicts, len(statements))
            score = round(sum(verdicts) / len(verdicts), 4)
            return score, {"statements": statements, "verdicts": verdicts}
    except asyncio.TimeoutError:
        logger.warning("幻觉判定超时（%ds）", METRIC_TIMEOUT)
    except Exception as e:
        logger.warning("幻觉判定失败: %s", e)
    return None, {"statements": statements, "verdicts": []}


# ---------------------------------------------------------------------------
# GEval：自定义准则链式评估（criteria → 1-10 分 → 归一化 0~1）
# ---------------------------------------------------------------------------
GEVAL_SYSTEM_PROMPT = (
    "你是严格的评估专家。请根据给定的【评估准则】，对【用户问题】的【待评回答】"
    "进行链式评估：先逐条对照准则分析回答的表现（覆盖/缺失/错误），再综合打分。\n"
    "评分范围 1-10 整数：10=完全满足准则，1=完全不符合。\n"
    "只输出 JSON，不要输出任何解释、前后缀或 markdown 标记，格式：\n"
    '{"score": 8, "reason": "一句话说明打分理由"}'
)


def _parse_geval(raw: str) -> tuple:
    """解析 GEval 输出 → (归一化分 0~1, reason)；坏 JSON/越界 → (None, '')。"""
    text = (raw or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None, ""
    try:
        data = json.loads(m.group(0))
    except (json.JSONDecodeError, TypeError):
        return None, ""
    try:
        score = float(data.get("score"))
    except (TypeError, ValueError):
        return None, ""
    if not (1 <= score <= 10):
        return None, ""
    reason = str(data.get("reason") or "").strip()[:500]
    return round((score - 1) / 9, 4), reason


async def measure_geval(test_case: LLMTestCase, criteria: str = "",
                        llm_provider: str = None) -> tuple:
    """GEval 准则评分：返回 (归一化分 0~1, detail{score, reason, criteria})。"""
    from app.services import evaluation as ev

    criteria = (criteria or "").strip() or settings.ONLINE_EVAL_GEVAL_CRITERIA
    answer = (test_case.actual_output or "").strip()
    if not answer:
        return None, {"criteria": criteria, "reason": "无回答"}
    messages = [
        {"role": "system", "content": GEVAL_SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"【评估准则】\n{criteria}\n\n"
            f"【用户问题】\n{test_case.input or '(无)'}\n\n"
            f"【待评回答】\n{answer}"
        )},
    ]
    try:
        raw = await asyncio.wait_for(ev._generate(messages, llm_provider), timeout=METRIC_TIMEOUT)
        score, reason = _parse_geval(raw)
        if score is not None:
            return score, {"criteria": criteria, "reason": reason}
    except asyncio.TimeoutError:
        logger.warning("GEval 评估超时（%ds）", METRIC_TIMEOUT)
    except Exception as e:
        logger.warning("GEval 评估失败: %s", e)
    return None, {"criteria": criteria, "reason": ""}


# ---------------------------------------------------------------------------
# 统一入口：按指标集并发评估单用例（Ragas 委托既有实现，单一事实源）
# ---------------------------------------------------------------------------
async def _measure_ragas(name: str, test_case: LLMTestCase,
                         llm_provider: str = None) -> tuple:
    """Ragas 四指标委托 evaluation.py 既有实现（不重写，保持口径一致）。"""
    from app.services import evaluation as ev

    contexts = [c for c in (test_case.retrieval_context or []) if (c or "").strip()]
    context_text = test_case.context_text
    if name == "faithfulness":
        return await ev._statement_metric(test_case.actual_output, context_text, llm_provider)
    if name == "answer_relevancy":
        return await ev._compute_answer_relevance(test_case.input, test_case.actual_output, llm_provider)
    if name == "contextual_precision":
        return await ev._compute_context_precision(
            test_case.input, test_case.expected_output, contexts, llm_provider)
    if name == "contextual_recall":
        return await ev._statement_metric(test_case.expected_output, context_text, llm_provider)
    return None, {}


async def evaluate_case(test_case: LLMTestCase, metrics: Optional[List[str]] = None,
                        criteria: str = "", llm_provider: str = None) -> dict:
    """并发执行所选指标，返回 {指标名: (score, detail)}；单点失败降级 (None, {})。"""
    names = normalize_metrics(metrics)
    tasks = []
    for name in names:
        if name in RAGAS_METRICS:
            tasks.append(_measure_ragas(name, test_case, llm_provider))
        elif name == "hallucination":
            tasks.append(measure_hallucination(test_case, llm_provider))
        elif name == "geval":
            tasks.append(measure_geval(test_case, criteria, llm_provider))
    out = await asyncio.gather(*tasks, return_exceptions=True)
    result = {}
    for name, item in zip(names, out):
        if isinstance(item, BaseException):
            logger.warning("指标 %s 计算异常: %s", name, item)
            item = (None, {})
        result[name] = item
    return result