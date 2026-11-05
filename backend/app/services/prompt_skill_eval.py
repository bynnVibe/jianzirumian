"""
见字如面 - Prompt / Skill 评测与优化上线服务

在既有 Ragas 风格问答回归之外，新增对「Prompt（系统提示词）」与「Skill（用户技能）」
两类可治理资产的专项评测，结合业界主流评测技术：

1. 静态断言（Rule-based checks）：确定性质量门禁——长度区间、角色/行为约束完备、
   无敏感信息、playbook JSON 合法且规则完整（复用 skill 校验），不依赖 LLM；
2. Rubric LLM-as-judge（多维度打分）：按维度 1-5 打分 + 逐维理由，加权得总分，
   是 Prompt 评测的主流方法（对齐 G-Eval / rubric grading 思路）；
3. A/B 行为对比（Regression）：把「当前生效版本」与「优化候选版本」分别灌入真实问答链路
   （检索 → 知识优先 prompt → 生成），对同一批评测集用例做 pairwise LLM 裁判，
   统计 win/tie/loss 与胜率，量化优化是否真的带来提升；
4. 优化建议（LLM optimizer）：基于 Rubric 低分维度与失败断言，生成改进后的正文 + 理由 + 预期收益；
5. 上线（Publish）：Prompt → 发布为新 prompt_version（chat 主流程即刻采用）；
   Skill → 经 SkillService.update_skill 落库（自动重跑安全预审）后生效。

设计要点：评测/建议/A-B 均为后台任务写回 prompt_skill_evals 行，单点失败降级不中断；
所有 LLM 输出宽容解析；上线为显式动作，需管理员确认，不自动改动线上资产。
"""
import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime
from typing import List, Optional

from app.config import settings
from app.core import db
from app.core.llm import FINAL_ANSWER_RULES
from app.services import evaluation as ev
from app.services import prompt_store
from app.services.knowledge import knowledge_service
from app.services.skill import skill_service

logger = logging.getLogger("jianziruyang.prompt_skill_eval")

# 单次评测/A-B 的 LLM 调用超时（秒）
JUDGE_TIMEOUT = 40
# A/B 对比最多取用的评测集用例数（控制 token 成本）
AB_MAX_CASES = 8

# 敏感信息（与记忆/技能服务一致的底线）
_SENSITIVE_RE = re.compile(r"密码|password|token|api[_\s-]?key|身份证|银行卡|信用卡", re.I)

# Rubric 维度定义：prompt 类文本（系统提示词 / prompt 技能）
PROMPT_DIMENSIONS = [
    ("clarity", "清晰度", "表述是否清楚无歧义、结构是否易于模型理解", 1.0),
    ("specificity", "具体可执行", "是否给出明确、可操作的指令而非空泛要求", 1.2),
    ("instruction_coverage", "约束完备", "是否覆盖角色、能力边界、输出/引用格式等关键约束", 1.2),
    ("domain_fit", "领域契合", "是否贴合健康养生知识库问答场景，不跑题、不越界", 1.0),
    ("safety", "安全合规", "是否避免诱导/替代医疗诊断、无敏感信息、无有害指令", 1.3),
    ("conciseness", "简洁性", "是否无冗余重复、长度与信息量匹配", 0.8),
]

# Rubric 维度定义：playbook 技能（结构化策略）
PLAYBOOK_DIMENSIONS = [
    ("validity", "规则合法完整", "字段是否齐全、match/策略是否明确可执行", 1.3),
    ("coverage", "覆盖合理", "命中范围是否恰当，不过宽误伤、不过窄失效", 1.0),
    ("safety", "安全合规", "是否无敏感信息、无危险抓取/清洗指令", 1.3),
    ("clarity", "说明清晰", "note/描述是否清楚说明策略意图", 0.9),
]

RUBRIC_SYSTEM_PROMPT = (
    "你是严格的 Prompt/指令质量评审专家。请依据给定的【评分维度】逐项为【待评文本】打分。"
    "每个维度打 1-5 分整数（5=优秀，1=很差），并给出一句话理由。评分要客观、可复现，"
    "紧扣维度定义，不因文本长短而偏袒。\n"
    "只输出 JSON，不要输出任何解释、前后缀或 markdown 标记，格式：\n"
    '{"scores": {"维度key": {"score": 4, "reason": "一句话理由"}}}'
)

OPTIMIZE_SYSTEM_PROMPT = (
    "你是资深 Prompt 工程师。请依据【评分反馈】（低分维度与理由）与【未通过的静态断言】，"
    "在保留原有正确意图与领域约束的前提下，改写出一版更优的文本：补足缺失约束、消除歧义与冗余、"
    "强化安全合规，且不引入原文没有的新业务事实。改写后仍须是同一种资产（提示词正文 / 技能内容），"
    "playbook 必须保持合法 JSON 结构。\n"
    "只输出 JSON，不要输出任何解释、前后缀或 markdown 标记，格式：\n"
    '{"optimized": "改进后的完整文本", "rationale": "总体改进说明",'
    ' "gains": [{"dimension": "维度key或断言key", "note": "预期改善点"}]}'
)

PAIRWISE_JUDGE_PROMPT = (
    "你是严格的答案质量评审专家。针对同一【用户问题】，有两个候选回答 A 与 B。"
    "请从事实正确性、要点覆盖度、清晰与安全合规综合判断哪个更好（A=当前生效版本，B=优化候选版本）。"
    "两者相当则判 tie。\n"
    "只输出 JSON，不要输出任何解释、前后缀或 markdown 标记，格式：\n"
    '{"verdict": "A" 或 "B" 或 "tie", "reason": "一句话理由"}'
)


class ConcurrentEvalError(Exception):
    """同一目标已有正在执行的评测（并发保护）"""


def _now() -> str:
    return datetime.now().isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# 目标解析
# ---------------------------------------------------------------------------
def list_targets(owner_id: str) -> List[dict]:
    """可评测目标清单：系统提示词（当前生效）+ 该管理员可见的用户技能。"""
    targets = [{
        "target_type": "prompt",
        "target_id": "system_prompt",
        "name": "系统提示词（当前生效）",
        "kind": "prompt",
        "scope": "chat",
        "builtin": True,
        "text": prompt_store.get_active_body(),
    }]
    for s in skill_service.list_skills(owner_id or ""):
        targets.append({
            "target_type": "skill",
            "target_id": s["id"],
            "name": s["name"],
            "kind": s["kind"],
            "scope": s["scope"],
            "builtin": s["builtin"],
            "text": s["content"],
        })
    return targets


def _resolve_target(target_type: str, target_id: str, owner_id: str) -> Optional[dict]:
    """解析评测目标的当前文本与元信息；不存在返回 None。"""
    if target_type == "prompt":
        return {
            "target_type": "prompt", "target_id": "system_prompt",
            "name": "系统提示词（当前生效）", "kind": "prompt", "scope": "chat",
            "owner_id": "", "text": prompt_store.get_active_body(),
        }
    if target_type == "skill":
        s = skill_service.get_skill(owner_id or "", target_id)
        if not s:
            return None
        return {
            "target_type": "skill", "target_id": s["id"], "name": s["name"],
            "kind": s["kind"], "scope": s["scope"], "owner_id": owner_id or "",
            "text": s["content"], "builtin": s["builtin"],
        }
    return None


# ---------------------------------------------------------------------------
# 静态断言（Rule-based checks）
# ---------------------------------------------------------------------------
def _assertions_prompt(text: str) -> List[dict]:
    """Prompt/prompt 技能类静态断言。"""
    t = (text or "").strip()
    n = len(t)
    has_role = bool(re.search(r"你是|作为一个|角色|助手|You are", t))
    has_rule = bool(re.search(r"准则|规则|要求|不要|必须|请|需|输出|引用|-|1\.|约束", t))
    return [
        {"key": "length_range", "label": "长度合理（50~4000 字）",
         "passed": 50 <= n <= 4000, "detail": f"当前 {n} 字"},
        {"key": "has_role", "label": "含角色定义",
         "passed": has_role, "detail": "应明确「你是…」等角色设定" if not has_role else "已包含角色设定"},
        {"key": "has_behavior_rule", "label": "含行为/输出约束",
         "passed": has_rule, "detail": "应给出可执行的行为或输出约束" if not has_rule else "已包含行为约束"},
        {"key": "no_sensitive", "label": "无敏感信息",
         "passed": not _SENSITIVE_RE.search(t), "detail": "不得包含密码/密钥/证件号等"},
    ]


def _assertions_playbook(text: str, scope: str) -> List[dict]:
    """playbook 技能静态断言：复用 skill 校验器 + 结构统计。"""
    from app.services import skill as skill_mod

    err = skill_mod._validate_playbook(text or "", scope or "url_import")
    valid = err is None
    detail = err or "JSON 合法、规则完整"
    rules_n = 0
    if valid:
        try:
            parsed = json.loads(text)
            rules_n = len(parsed) if isinstance(parsed, list) else 1
        except (ValueError, TypeError):
            rules_n = 0
    return [
        {"key": "playbook_valid", "label": "结构合法", "passed": valid, "detail": detail},
        {"key": "playbook_nonempty", "label": "规则非空",
         "passed": valid and rules_n > 0, "detail": f"规则/字段数 {rules_n}"},
        {"key": "no_sensitive", "label": "无敏感信息",
         "passed": not _SENSITIVE_RE.search(text or ""), "detail": "不得包含密码/密钥/证件号等"},
    ]


def run_assertions(target_type: str, kind: str, scope: str, text: str) -> List[dict]:
    """按目标类型/技能类别选择断言集。"""
    if target_type == "skill" and kind == "playbook":
        return _assertions_playbook(text, scope)
    return _assertions_prompt(text)


def _assertion_pass_rate(assertions: List[dict]) -> float:
    if not assertions:
        return 0.0
    return round(sum(1 for a in assertions if a["passed"]) / len(assertions), 4)


# ---------------------------------------------------------------------------
# Rubric LLM-as-judge（多维度打分）
# ---------------------------------------------------------------------------
def _dimensions_for(target_type: str, kind: str) -> List[tuple]:
    if target_type == "skill" and kind == "playbook":
        return PLAYBOOK_DIMENSIONS
    return PROMPT_DIMENSIONS


async def _rubric_judge(target_type: str, kind: str, name: str, text: str) -> dict:
    """多维度打分：返回 {dimensions:[{key,label,score,reason,weight}], overall, max:5}。

    LLM 失败时所有维度记 None，overall=None（前端提示评分不可用，不阻断断言/建议）。
    """
    dims = _dimensions_for(target_type, kind)
    dim_lines = "\n".join(f"- {key}（{label}）：{desc}" for key, label, desc, _ in dims)
    messages = [
        {"role": "system", "content": RUBRIC_SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"【资产类型】{'Skill(' + kind + ')' if target_type == 'skill' else '系统提示词'}\n"
            f"【资产名称】{name}\n\n【评分维度】\n{dim_lines}\n\n【待评文本】\n{text or '(空)'}"
        )},
    ]
    parsed = None
    try:
        raw = await asyncio.wait_for(ev._generate(messages), timeout=JUDGE_TIMEOUT)
        parsed = ev._parse_json(raw)
    except asyncio.TimeoutError:
        logger.warning("Rubric 打分超时（%ds）", JUDGE_TIMEOUT)
    except Exception as e:
        logger.warning("Rubric 打分失败: %s", e)
    scores = (parsed or {}).get("scores") if isinstance(parsed, dict) else None
    scores = scores if isinstance(scores, dict) else {}

    out_dims = []
    num = den = 0.0
    for key, label, _desc, weight in dims:
        item = scores.get(key) or {}
        raw_score = item.get("score") if isinstance(item, dict) else None
        try:
            score = max(1.0, min(5.0, float(raw_score)))
        except (TypeError, ValueError):
            score = None
        reason = str(item.get("reason", "")).strip()[:300] if isinstance(item, dict) else ""
        out_dims.append({"key": key, "label": label, "score": score,
                         "reason": reason, "weight": weight})
        if score is not None:
            num += score * weight
            den += weight
    overall = round(num / den, 3) if den else None
    return {"dimensions": out_dims, "overall": overall, "max": 5}


def _weak_dimensions(rubric: dict, threshold: float = 3.0) -> List[dict]:
    """挑出低分维度（<=threshold）供优化器聚焦。"""
    out = []
    for d in rubric.get("dimensions", []):
        if d.get("score") is not None and d["score"] <= threshold:
            out.append({"key": d["key"], "label": d["label"],
                        "score": d["score"], "reason": d.get("reason", "")})
    return out


# ---------------------------------------------------------------------------
# 优化建议（LLM optimizer）
# ---------------------------------------------------------------------------
async def _suggest_optimization(target_type: str, kind: str, name: str, text: str,
                                assertions: List[dict], rubric: dict) -> dict:
    """基于低分维度 + 失败断言生成改进文本；返回 {optimized, rationale, gains}。"""
    weak = _weak_dimensions(rubric)
    failed = [a for a in assertions if not a["passed"]]
    if not weak and not failed:
        return {"optimized": text, "rationale": "当前版本各维度达标、断言全通过，无需优化。",
                "gains": [], "changed": False}
    feedback = {
        "低分维度": [{"维度": w["label"], "得分": w["score"], "问题": w["reason"]} for w in weak],
        "未通过断言": [{"项": a["label"], "说明": a["detail"]} for a in failed],
    }
    messages = [
        {"role": "system", "content": OPTIMIZE_SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"【资产类型】{'Skill(' + kind + ')' if target_type == 'skill' else '系统提示词'}\n"
            f"【资产名称】{name}\n\n【当前文本】\n{text or '(空)'}\n\n"
            f"【评分反馈】\n{json.dumps(feedback, ensure_ascii=False)}"
        )},
    ]
    try:
        raw = await asyncio.wait_for(ev._generate(messages), timeout=JUDGE_TIMEOUT)
        parsed = ev._parse_json(raw)
        if isinstance(parsed, dict) and (parsed.get("optimized") or "").strip():
            optimized = parsed["optimized"].strip()
            # playbook 优化后仍须合法，否则回退原文（避免上线坏结构）
            if target_type == "skill" and kind == "playbook":
                from app.services import skill as skill_mod
                if skill_mod._validate_playbook(optimized, "url_import") is not None:
                    return {"optimized": text, "rationale": "优化建议未产出合法 playbook，已回退原文。",
                            "gains": [], "changed": False}
            gains = parsed.get("gains") if isinstance(parsed.get("gains"), list) else []
            return {"optimized": optimized,
                    "rationale": str(parsed.get("rationale", "")).strip()[:800],
                    "gains": gains[:8], "changed": optimized.strip() != (text or "").strip()}
    except asyncio.TimeoutError:
        logger.warning("优化建议生成超时（%ds）", JUDGE_TIMEOUT)
    except Exception as e:
        logger.warning("优化建议生成失败: %s", e)
    return {"optimized": text, "rationale": "优化建议生成失败，请稍后重试。",
            "gains": [], "changed": False}


# ---------------------------------------------------------------------------
# A/B 行为对比（真实问答链路 + pairwise 裁判）
# ---------------------------------------------------------------------------
async def _answer_with_system(question: str, system_content: str, user_id: str) -> str:
    """以指定系统提示词跑一遍真实问答链路（检索 → 知识优先 prompt → 生成），返回答案。"""
    results = await asyncio.to_thread(knowledge_service.retrieve_for_user, question, user_id)
    context_text, _sources = await asyncio.to_thread(
        knowledge_service.format_knowledge_context, question, results
    )
    augmented = knowledge_service.build_knowledge_prompt(question, context_text, True)
    messages = [{"role": "system", "content": system_content},
                {"role": "user", "content": augmented}]
    return await ev._generate(messages)


def _system_for(target_type: str, body: str) -> str:
    """把待评正文装配为完整系统提示词（与 chat 主流程同构）。"""
    if target_type == "skill":
        return (prompt_store.DEFAULT_BODY + FINAL_ANSWER_RULES
                + "\n\n请额外遵循以下用户定制指令：\n- " + (body or "").strip())
    return (body or "").strip() + FINAL_ANSWER_RULES


async def _pairwise_judge(question: str, ans_a: str, ans_b: str) -> tuple:
    messages = [
        {"role": "system", "content": PAIRWISE_JUDGE_PROMPT},
        {"role": "user", "content": (
            f"【用户问题】\n{question}\n\n【回答 A】\n{ans_a or '(空)'}\n\n【回答 B】\n{ans_b or '(空)'}"
        )},
    ]
    try:
        raw = await asyncio.wait_for(ev._generate(messages), timeout=JUDGE_TIMEOUT)
        parsed = ev._parse_json(raw)
        if isinstance(parsed, dict):
            verdict = str(parsed.get("verdict", "")).strip().upper()
            if verdict in ("A", "B", "TIE"):
                return ("tie" if verdict == "TIE" else verdict), str(parsed.get("reason", ""))[:300]
    except asyncio.TimeoutError:
        logger.warning("pairwise 裁判超时（%ds）", JUDGE_TIMEOUT)
    except Exception as e:
        logger.warning("pairwise 裁判失败: %s", e)
    return "error", ""


async def _run_ab(eval_id: str, dataset_id: str, user_id: str):
    """后台执行 A/B：当前生效版本(A) vs 优化候选版本(B)，pairwise 统计胜率。"""
    row = db.query_one("SELECT * FROM prompt_skill_evals WHERE id = ?", (eval_id,))
    if not row:
        return
    try:
        suggestion = json.loads(row.get("suggestion") or "{}")
        candidate = (suggestion.get("optimized") or "").strip()
        baseline = (row.get("baseline_text") or "").strip()
        if not candidate or candidate == baseline:
            db.execute("UPDATE prompt_skill_evals SET ab = ?, status = 'finished' WHERE id = ?",
                       (json.dumps({"skipped": True, "reason": "无有效优化候选，跳过 A/B"},
                                   ensure_ascii=False), eval_id))
            return
        cases = db.query(
            "SELECT question FROM eval_cases WHERE dataset_id = ? ORDER BY created_at, rowid LIMIT ?",
            (dataset_id, AB_MAX_CASES),
        )
        if not cases:
            db.execute("UPDATE prompt_skill_evals SET ab = ? WHERE id = ?",
                       (json.dumps({"skipped": True, "reason": "评测集无可用用例"},
                                   ensure_ascii=False), eval_id))
            return
        sys_a = _system_for(row["target_type"], baseline)
        sys_b = _system_for(row["target_type"], candidate)
        wins = ties = losses = errors = 0
        items = []
        for c in cases:
            q = (c["question"] or "").strip()
            if not q:
                continue
            try:
                ans_a, ans_b = await asyncio.gather(
                    _answer_with_system(q, sys_a, user_id),
                    _answer_with_system(q, sys_b, user_id),
                )
                verdict, reason = await _pairwise_judge(q, ans_a, ans_b)
            except Exception as e:
                logger.warning("A/B 用例失败 q=%s: %s", q[:30], e)
                verdict, reason = "error", str(e)[:200]
            if verdict == "B":
                wins += 1
            elif verdict == "A":
                losses += 1
            elif verdict == "tie":
                ties += 1
            else:
                errors += 1
            items.append({"question": q[:80], "verdict": verdict, "reason": reason})
        judged = wins + ties + losses
        ab = {
            "dataset_id": dataset_id,
            "cases": len(items),
            "wins": wins, "ties": ties, "losses": losses, "errors": errors,
            # 胜率：候选(B)胜出占已判定用例的比例；tie 记半分（主流 win-rate 口径）
            "win_rate": round((wins + 0.5 * ties) / judged, 4) if judged else None,
            "recommend_publish": bool(judged and (wins + 0.5 * ties) / judged >= 0.5 and losses <= wins),
            "items": items,
        }
        db.execute("UPDATE prompt_skill_evals SET ab = ?, status = 'finished', finished_at = ?"
                   " WHERE id = ?", (json.dumps(ab, ensure_ascii=False), _now(), eval_id))
        logger.info("Prompt/Skill A/B 完成 eval=%s | win=%d tie=%d loss=%d", eval_id, wins, ties, losses)
    except Exception as e:
        logger.error("A/B 整体失败 eval=%s: %s", eval_id, e)
        db.execute("UPDATE prompt_skill_evals SET ab = ?, status = 'finished' WHERE id = ?",
                   (json.dumps({"error": str(e)[:300]}, ensure_ascii=False), eval_id))


# ---------------------------------------------------------------------------
# 评测主流程（后台）
# ---------------------------------------------------------------------------
async def _run_eval(eval_id: str, target: dict):
    row_text = target["text"]
    try:
        assertions = run_assertions(target["target_type"], target["kind"],
                                    target.get("scope", ""), row_text)
        rubric = await _rubric_judge(target["target_type"], target["kind"],
                                     target["name"], row_text)
        suggestion = await _suggest_optimization(
            target["target_type"], target["kind"], target["name"], row_text,
            assertions, rubric,
        )
        db.execute(
            "UPDATE prompt_skill_evals SET status = 'finished', assertions = ?, rubric = ?,"
            " overall_score = ?, suggestion = ?, finished_at = ? WHERE id = ?",
            (json.dumps(assertions, ensure_ascii=False), json.dumps(rubric, ensure_ascii=False),
             rubric.get("overall"), json.dumps(suggestion, ensure_ascii=False), _now(), eval_id),
        )
        logger.info("Prompt/Skill 评测完成 eval=%s target=%s overall=%s",
                    eval_id, target["name"], rubric.get("overall"))
    except Exception as e:
        logger.error("Prompt/Skill 评测整体失败 eval=%s: %s", eval_id, e)
        db.execute("UPDATE prompt_skill_evals SET status = 'failed', error = ?, finished_at = ?"
                   " WHERE id = ?", (str(e)[:500], _now(), eval_id))


def start_evaluation(target_type: str, target_id: str, owner_id: str, user: dict) -> str:
    """创建评测并后台执行（断言 + Rubric + 优化建议），立即返回 eval_id。"""
    if target_type not in ("prompt", "skill"):
        raise ValueError("评测目标类型仅支持 prompt 或 skill")
    target = _resolve_target(target_type, target_id, owner_id)
    if not target:
        raise ValueError("评测目标不存在")
    running = db.query_one(
        "SELECT id FROM prompt_skill_evals WHERE target_type = ? AND target_id = ?"
        " AND status = 'running'", (target_type, target["target_id"]),
    )
    if running:
        raise ConcurrentEvalError("该目标已有正在执行的评测，请等待其完成")
    eval_id = _new_id()
    created_by = user.get("id") or user.get("username") or ""
    db.execute(
        "INSERT INTO prompt_skill_evals (id, target_type, target_id, target_name, owner_id,"
        " status, baseline_text, created_by, created_at)"
        " VALUES (?,?,?,?,?,'running',?,?,?)",
        (eval_id, target_type, target["target_id"], target["name"], target.get("owner_id", ""),
         target["text"], created_by, _now()),
    )
    asyncio.create_task(_run_eval(eval_id, target))
    logger.info("发起 Prompt/Skill 评测 eval=%s target=%s(%s) by=%s",
                eval_id, target["name"], target_type, created_by)
    return eval_id


def start_ab(eval_id: str, dataset_id: str, user: dict) -> dict:
    """对已完成评测的优化候选发起 A/B 行为对比（后台执行）。"""
    row = db.query_one("SELECT * FROM prompt_skill_evals WHERE id = ?", (eval_id,))
    if not row:
        raise ValueError("评测记录不存在")
    if row["status"] == "running":
        raise ConcurrentEvalError("评测尚未完成，无法发起 A/B")
    if not db.query_one("SELECT id FROM eval_datasets WHERE id = ?", (dataset_id,)):
        raise ValueError("评测集不存在")
    user_id = user.get("id") or user.get("username") or ""
    db.execute("UPDATE prompt_skill_evals SET status = 'running' WHERE id = ?", (eval_id,))
    asyncio.create_task(_run_ab(eval_id, dataset_id, user_id))
    return {"eval_id": eval_id, "status": "running"}


# ---------------------------------------------------------------------------
# 上线（Publish）
# ---------------------------------------------------------------------------
def publish(eval_id: str, user: dict, content: Optional[str] = None) -> dict:
    """把评测产出的优化文本上线：Prompt 发布新版本；Skill 更新内容（重跑安全预审）。

    content 允许管理员在建议基础上二次编辑后再上线；缺省用建议 optimized 文本。
    """
    row = db.query_one("SELECT * FROM prompt_skill_evals WHERE id = ?", (eval_id,))
    if not row:
        return {"error": "评测记录不存在"}
    if row["status"] == "running":
        return {"error": "评测进行中，无法上线"}
    suggestion = json.loads(row.get("suggestion") or "{}")
    text = (content if content is not None else suggestion.get("optimized") or "").strip()
    if not text:
        return {"error": "上线内容为空"}
    actor = user.get("username") or user.get("id") or ""
    owner_id = row.get("owner_id") or (user.get("id") or "")

    if row["target_type"] == "prompt":
        res = prompt_store.publish(text, name=f"{row['target_name']} 优化版",
                                   created_by=actor, note=f"eval:{eval_id}")
        if res.get("error"):
            return res
    else:
        res = skill_service.update_skill(owner_id, row["target_id"], content=text,
                                         actor_name=actor)
        if res.get("error"):
            return {"error": res["error"], "review": res.get("review")}
    db.execute("UPDATE prompt_skill_evals SET published = 1 WHERE id = ?", (eval_id,))
    logger.info("Prompt/Skill 优化已上线 eval=%s target=%s by=%s",
                eval_id, row["target_name"], actor)
    return {"success": True, "target_type": row["target_type"], "target_id": row["target_id"]}


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
def _decode(row: dict) -> dict:
    d = dict(row)
    for key, default in (("assertions", "[]"), ("rubric", "{}"),
                         ("suggestion", "{}"), ("ab", "{}")):
        try:
            d[key] = json.loads(d.get(key) or default)
        except (json.JSONDecodeError, TypeError):
            d[key] = json.loads(default)
    d["published"] = bool(d.get("published"))
    d["assertion_pass_rate"] = _assertion_pass_rate(d.get("assertions") or [])
    return d


def list_evals(target_type: str = "", target_id: str = "", limit: int = 30) -> dict:
    sql = "SELECT * FROM prompt_skill_evals WHERE 1=1"
    params: list = []
    if target_type:
        sql += " AND target_type = ?"; params.append(target_type)
    if target_id:
        sql += " AND target_id = ?"; params.append(target_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = [_decode(r) for r in db.query(sql, tuple(params))]
    # 列表精简：去掉长文本正文，仅保留概要
    for r in rows:
        r.pop("baseline_text", None)
    return {"evals": rows, "total": len(rows)}


def get_eval(eval_id: str) -> Optional[dict]:
    row = db.query_one("SELECT * FROM prompt_skill_evals WHERE id = ?", (eval_id,))
    return _decode(row) if row else None
