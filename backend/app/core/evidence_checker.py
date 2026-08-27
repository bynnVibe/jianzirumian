"""
见字如面 - 轻量 Self-RAG 模块

1. 信息缺口判断：检索完成后由 LLM 判断证据是否足以回答用户问题，
   不足时返回缺失要点，由调用方改写查询重检一次（仅一次，保持轻量）。
2. 引用编号核对：生成结束后扫描回答中的 [来源 X] 编号，
   校验是否落在实际来源范围内，防止模型编造引用。

所有判断均带超时保护与失败降级：任何异常都视为"充分/通过"，不阻断主流程。
"""
import asyncio
import json
import logging
import re
from typing import List, Tuple

from app.core.llm import LLMFactory

logger = logging.getLogger("jianziruyang.evidence_checker")

# Self-RAG 总开关（如需关闭可在运行时改为 False）
SELF_RAG_ENABLED = True

# 证据判断的超时时间（秒）：超时时按"证据充分"处理，不拖慢回答
CHECK_TIMEOUT = 15

# 送入证据判断的上下文最大长度（截断以控制判断调用成本）
MAX_CONTEXT_CHARS = 4000

EVIDENCE_CHECK_PROMPT = (
    "你是检索证据评估助手。请判断给定的知识库证据是否足以回答用户的问题。\n\n"
    "要求：\n"
    "1. 证据覆盖了问题的核心要点、能支撑实质性回答时，判为充分\n"
    "2. 证据与问题无关、只覆盖一小部分、或缺少回答问题所需的关键信息时，判为不充分，"
    "并用一句话指出缺失的要点（作为补充检索的关键词）\n"
    "3. 只输出 JSON，不要输出任何解释、前后缀或 markdown 标记，格式：\n"
    "{\"sufficient\": true}\n"
    "或 {\"sufficient\": false, \"missing\": \"缺失要点\"}"
)

# 引用编号提取：[来源 1] / 【来源 1】 / (来源 1)，排除「网络来源 X」
_CITATION_PATTERN = re.compile(r"[\[【（(]\s*(?<!网络)来源\s*(\d+)\s*[\]】）)]")


async def check_evidence_sufficiency(
    query: str,
    context_text: str,
    llm_provider: str = None,
) -> Tuple[bool, str]:
    """判断检索证据是否足以回答用户问题

    Returns:
        (sufficient, missing_hint)
        sufficient=True 时 missing_hint 为空；
        sufficient=False 时 missing_hint 为缺失要点，可用于改写重检。
        任何异常/超时均降级为 (True, "")。
    """
    if not SELF_RAG_ENABLED or not context_text or not context_text.strip():
        return True, ""

    messages = [
        {"role": "system", "content": EVIDENCE_CHECK_PROMPT},
        {
            "role": "user",
            "content": (
                f"【用户问题】\n{query}\n\n"
                f"【知识库证据】\n{context_text[:MAX_CONTEXT_CHARS]}"
            ),
        },
    ]

    try:
        llm = LLMFactory.create(llm_provider)

        async def _collect() -> str:
            collected = ""
            async for chunk in llm.chat(messages, stream=True):
                collected += chunk
            return collected

        raw = await asyncio.wait_for(_collect(), timeout=CHECK_TIMEOUT)
        parsed = _parse_check_result(raw)
        if parsed is None:
            logger.warning("证据判断输出无法解析，按充分处理: %r", raw[:100])
            return True, ""
        sufficient = bool(parsed.get("sufficient", True))
        missing = str(parsed.get("missing", "")).strip()[:100]
        logger.info("证据判断: sufficient=%s, missing=%r", sufficient, missing)
        return sufficient, missing
    except asyncio.TimeoutError:
        logger.warning("证据判断超时（%ds），按充分处理", CHECK_TIMEOUT)
        return True, ""
    except Exception as e:
        logger.warning("证据判断失败，按充分处理: %s", e)
        return True, ""


def _parse_check_result(raw: str) -> dict | None:
    """宽容解析 LLM 输出的 JSON（容忍 markdown 代码块与前后噪声）"""
    if not raw:
        return None
    text = raw.strip()
    # 去除 ```json ... ``` 包裹
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # 截取首个 { ... } 片段，容忍前后多余文字
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        if isinstance(data, dict) and "sufficient" in data:
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def verify_citations(answer_text: str, source_count: int) -> List[int]:
    """核对回答中的引用编号真实性

    Args:
        answer_text: LLM 生成的完整回答
        source_count: 实际来源条数（[来源 1..N] 的 N）

    Returns:
        越界或不存在的引用编号列表（空列表表示全部合法）
    """
    if not answer_text:
        return []
    invalid = []
    for m in _CITATION_PATTERN.finditer(answer_text):
        try:
            idx = int(m.group(1))
        except ValueError:
            continue
        if idx < 1 or idx > source_count:
            if idx not in invalid:
                invalid.append(idx)
    return invalid
