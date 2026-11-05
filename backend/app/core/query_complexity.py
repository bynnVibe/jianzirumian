"""
见字如面 - 指令复杂度分类器

在知识库检索前判断用户问题的复杂度，据此分流主聊天的处理模式：
  - simple   → fast         现有固定流水线（改写→检索→证据校验→生成），视作 ReAct 单步快路
  - moderate → react        多轮 ReAct 工具循环（search_knowledge / web_search）
  - complex  → plan_execute 先规划步骤清单，再顺序执行工具，最后汇总回答

判断维度：推理步数、工具依赖、上下文依赖。

两级策略（控制延迟与成本）：
  1. 启发式预筛：极短问句且不含多步/对比/递进连接词 → 直接 fast，跳过 LLM，
     避免给简单问题增加分类延迟。
  2. LLM 细判：预筛未命中时调用网关 intent_rewrite 角色，严格 JSON 输出
     {"level":"simple|moderate|complex","reason":"..."}，asyncio.wait_for 超时保护。

保底原则：任何异常/超时/解析失败 → fast（等价于改造前的现状行为），绝不阻断主流程。
"""
import asyncio
import json
import logging
import re
import time
from typing import List, Optional

from app.core import observability as obs
from app.core.llm import LLMFactory

logger = logging.getLogger("jianziruyang.query_complexity")

# LLM 分类超时（秒）：超时保底 fast，不拖慢主流程
CLASSIFY_TIMEOUT = 8

# 短问句阈值（字符）：不超过该长度且无多步连接词时，启发式直接判为 fast
_SHORT_QUERY_LEN = 30

# 多步/对比/递进/规划类连接词——命中其一说明可能需要多步推理，交 LLM 细判
_MULTI_STEP_HINTS = (
    "然后", "并且", "此外", "另外", "同时", "接着", "再", "分别", "各自", "逐一",
    "对比", "比较", "有何区别", "有什么区别", "哪种更", "哪个更", "以及", "综合",
    "总结", "梳理", "为什么", "原因", "分析", "评估", "制定", "规划", "步骤",
    "方案", "计划", "如果", "假如", "先", "第一步", "第二步", "列举", "有什么关系",
)

_LEVEL_TO_MODE = {"simple": "fast", "moderate": "react", "complex": "plan_execute"}

_CLASSIFY_SYSTEM = (
    "你是「见字如面」健康养生问答系统的指令复杂度分类器。请判断用户问题的复杂度，"
    "用于选择处理方式（直接回答 / 多轮检索 / 先规划再执行）。\n\n"
    "# 判断维度\n"
    "1. 推理步数：回答需要几步推理或几个子任务。\n"
    "2. 工具依赖：是否需要多次知识库检索或联网搜索、是否需要跨来源综合。\n"
    "3. 上下文依赖：是否强依赖历史对话、是否含多个约束条件或指代。\n\n"
    "# 分级标准\n"
    "- simple：单步、单一意图、单实体或寒暄，知识库一次检索或直接作答即可"
    "（如「红枣的功效」「你好」「丰隆穴在哪」）。\n"
    "- moderate：需要 2~3 步推理或 1~2 次工具调用、轻度对比或轻度依赖上下文"
    "（如「对比人参和黄芪的补气差异」「根据上面说的再展开讲讲 X」）。\n"
    "- complex：需要多步规划、多工具协同、强上下文依赖，或要求产出方案/计划/报告"
    "（如「先检索穴位知识，再结合联网资料，为我制定一份针对失眠的综合调理方案并说明每步理由」）。\n\n"
    "# 输出协议（严格输出单个 JSON 对象，不要用代码块包裹，不要输出多余文字）\n"
    '{"level":"simple|moderate|complex","reason":"一句话说明判断依据"}'
)


def _parse_json_lenient(text: str) -> dict:
    """宽容解析 LLM 输出的 JSON（容忍 markdown 代码块与前后噪声）。"""
    text = (text or "").strip()
    if not text:
        return {}
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            data = json.loads(text[start:end + 1])
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _heuristic_prescreen(query: str) -> Optional[dict]:
    """启发式预筛：极短问句且无多步连接词 → 直接 fast，跳过 LLM。

    命中返回 {"level","mode","reason"}；不确定时返回 None 交 LLM 细判。
    """
    q = (query or "").strip()
    if not q:
        return {"level": "simple", "mode": "fast", "reason": "空问题"}
    if any(h in q for h in _MULTI_STEP_HINTS):
        return None  # 含多步/对比/规划信号，交 LLM 细判
    if len(q) <= _SHORT_QUERY_LEN:
        return {"level": "simple", "mode": "fast", "reason": "简短问句，无多步/对比意图"}
    return None


async def _llm_classify(query: str, history: List[dict]) -> dict:
    """调用网关 intent_rewrite 角色对问题做三级复杂度分类。"""
    hist_lines: List[str] = []
    for h in (history or [])[-6:]:
        role = h.get("role") if h.get("role") in ("user", "assistant") else "user"
        content = (h.get("content") or "").strip()
        if content:
            hist_lines.append(f"{role}: {content[:200]}")
    hist_block = "\n".join(hist_lines) if hist_lines else "（无历史对话）"
    messages = [
        {"role": "system", "content": _CLASSIFY_SYSTEM},
        {"role": "user", "content": f"【历史对话】\n{hist_block}\n\n【当前问题】\n{query}\n/no_think"},
    ]
    llm = LLMFactory.create_for_role("intent_rewrite")
    t0 = time.monotonic()
    collected = ""
    async for chunk in llm.chat(messages, stream=True):
        collected += chunk
    parsed = _parse_json_lenient(collected)
    level = str(parsed.get("level") or "").strip().lower()
    if level not in _LEVEL_TO_MODE:
        level = "simple"
    reason = str(parsed.get("reason") or "").strip()[:120]
    obs.record_llm("complexity_classify", messages, collected, t0=t0, fail_trace=False)
    return {"level": level, "reason": reason or "LLM 分类"}


async def classify_complexity(query: str, history: Optional[List[dict]] = None) -> dict:
    """判断问题复杂度并映射到处理模式。

    Args:
        query: 用户问题
        history: 最近历史消息（dict 列表，含 role/content），用于判断上下文依赖

    Returns:
        {"mode": "fast"|"react"|"plan_execute", "level": str, "reason": str}
        任何异常/超时/解析失败都保底 mode="fast"。
    """
    q = (query or "").strip()
    pre = _heuristic_prescreen(q)
    if pre is not None:
        logger.info("[复杂度] level=%s mode=%s reason=%s（启发式预筛）",
                    pre["level"], pre["mode"], pre["reason"])
        obs.record_step("complexity_classify", f"预筛 {pre['level']}", fail_trace=False)
        return pre
    try:
        result = await asyncio.wait_for(_llm_classify(q, history or []), timeout=CLASSIFY_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("[复杂度] LLM 分类超时（%ds），保底 fast", CLASSIFY_TIMEOUT)
        return {"level": "simple", "mode": "fast", "reason": f"分类超时（{CLASSIFY_TIMEOUT}s）保底 fast"}
    except Exception as e:
        logger.warning("[复杂度] LLM 分类异常，保底 fast: %s", e)
        return {"level": "simple", "mode": "fast", "reason": f"分类异常保底 fast: {e}"}
    mode = _LEVEL_TO_MODE.get(result["level"], "fast")
    logger.info("[复杂度] level=%s mode=%s reason=%s", result["level"], mode, result["reason"])
    return {"level": result["level"], "mode": mode, "reason": result["reason"]}
