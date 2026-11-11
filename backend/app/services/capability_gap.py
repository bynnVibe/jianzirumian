"""
见字如面 - 能力缺口（Capability Gap）治理：CLI 兜底 + HITL + 沙箱 + 技能沉淀

当系统现有工具/能力解决不了用户问题（如「查询今天香港的天气」仅靠联网搜索拿不到
结构化实时数据、且模型不知道「今天」是几号），本模块负责（需求5/6）：

  1. 识别缺口：路由无合适工具 / 实时查询但搜索结果不充分 → 归一化为 scenario；
  2. 生成候选方案：从内置「能力配方」（CAPABILITY_RECIPES）产出可执行的 CLI/URL
     方案（如 curl wttr.in、curl open-meteo、date 取今日日期），供 human-in-the-loop；
  3. HITL 选择：把方案随 SSE capability_gap 事件下发，由用户选定想怎么实现；
  4. 沙箱执行：经 CliRunner（无 shell / env 白名单 / 临时 cwd / 超时熔断）执行所选方案；
     风险等级 high/critical 的方案必须先经用户确认（confirmed）才执行，否则暂停返回；
  5. 频次沉淀：同 scenario 累计达阈值（默认 3 次）即在可观测性提示
     「该场景频繁出现，是否沉淀为 skill/工具」，体现系统可进化性（需求6）。

所有缺口与执行均落库 capability_gaps 表，供可观测性「技能沉淀建议」面板聚合。
"""
import json
import logging
import re
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from app.core import db
from app.services.tools.cli_runner import default_runner
from app.services.sandbox import ExecutionSpec, sandbox_controller

logger = logging.getLogger("jianziruyang.capability_gap")

# 同场景累计达到该次数即提示沉淀为 skill/工具（需求6 的「频繁性」阈值）
SEDIMENT_THRESHOLD = 3

# 天气类实时信号（与 tools.router 的实时词表同源，独立一份避免循环依赖）
_WEATHER_RE = re.compile(r"天气|气温|温度|多少度|下雨|降雨|降雪|台风|雾霾")
# 城市抽取：「XX的天气/气温」中的地名——可选前导查询动词 + 可选时间词，
# 城市名本身不能以时间/动词字开头，避免把「查询今天」误入城市名。
_CITY_RE = re.compile(
    r"(?:查询|查一下|查|看看|看下|想知道|了解)?"
    r"(?:今天|今日|明天|现在|最新)?"
    r"([\u4e00-\u9fa5A-Za-z]{2,8}?)的?"
    r"(?:今天|今日|现在|最新)?(?:天气|气温|温度)"
)
# 实时/外部数据信号：含此类信号且无任何工具/内容可用才判定 no_tool，
# 避免纯知识库问答（KB 未命中）误报能力缺口。
_EXTERNAL_SIGNAL_RE = re.compile(
    r"实时|今天|今日|现在|当前|最新|股价|汇率|新闻|天气|气温|https?://"
)

# ---------------------------------------------------------------------------
# 能力配方：scenario → 候选解决方案（CLI / URL / API）
# argv 中支持 {city} / {date} 占位符，执行前用缺口上下文替换。
# risk_level: low 直接执行；high/critical 需用户确认（HITL）并在沙箱运行。
# ---------------------------------------------------------------------------
CAPABILITY_RECIPES: Dict[str, dict] = {
    "realtime_weather": {
        "label": "实时天气查询",
        "solutions": [
            {
                "id": "cli_wttr",
                "label": "用命令行访问 wttr.in 获取 {city} 实时天气",
                "kind": "cli",
                "argv": ["curl", "-s", "--max-time", "15", "wttr.in/{city}?format=3&lang=zh"],
                "risk_level": "low",
                "note": "wttr.in 是公开天气服务，返回单行摘要",
            },
            {
                "id": "cli_open_meteo",
                "label": "调用 open-meteo 公开天气接口（需先提供该城市的纬度 {lat} / 经度 {lon}）",
                "kind": "cli",
                "argv": ["curl", "-s", "--max-time", "15",
                         "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
                         "&current_weather=true"],
                "risk_level": "low",
                "note": "open-meteo 免密钥；坐标须由城市地理编码得到，缺失时标记为缺参数，不用固定城市兜底",
            },
            {
                "id": "cli_today_date",
                "label": "先用 date 命令确认「今天」的真实日期，再据此解读搜索结果",
                "kind": "cli",
                "argv": ["date", "+%Y-%m-%d %A"],
                "risk_level": "low",
                "note": "解决「不知道今天指几号」的时效锚定问题",
            },
        ],
    },
    "realtime_general": {
        "label": "实时/时效信息获取",
        "solutions": [
            {
                "id": "cli_today_date",
                "label": "用 date 命令确认当前日期时间",
                "kind": "cli",
                "argv": ["date", "+%Y-%m-%d %H:%M %A"],
                "risk_level": "low",
                "note": "为时效类问题提供时间锚点",
            },
            {
                "id": "cli_curl_url",
                "label": "用 curl 抓取指定公开网页/接口原文（需提供 URL）",
                "kind": "cli",
                "argv": ["curl", "-s", "--max-time", "15", "{url}"],
                "risk_level": "medium",
                "note": "访问任意外部 URL，注意仅用于公开资源",
            },
        ],
    },
    "no_tool": {
        "label": "系统无对应工具",
        "solutions": [
            {
                "id": "cli_curl_url",
                "label": "用 curl 调用已知公开接口获取数据（需提供 URL）",
                "kind": "cli",
                "argv": ["curl", "-s", "--max-time", "15", "{url}"],
                "risk_level": "medium",
                "note": "把「系统不具备的能力」外化为一次受治理的命令行调用",
            },
        ],
    },
}


def detect_scenario(query: str, *, is_realtime: bool, has_suitable_tool: bool,
                    search_sufficient: bool, kb_has_content: bool = False) -> Optional[str]:
    """判定能力缺口场景；无缺口返回 None。

    缺口成立条件：
      - 实时查询但知识库/联网搜索都拿不到内容 → 天气类 realtime_weather，
        其余 realtime_general；
      - 路由无合适工具，且知识库也无内容，且问题带实时/外部数据信号 → no_tool。
        （纯知识库问答即使 KB 未命中也不属于能力缺口，避免误报）
    """
    q = (query or "").strip()
    if not q:
        return None
    if is_realtime and not search_sufficient and not kb_has_content:
        return "realtime_weather" if _WEATHER_RE.search(q) else "realtime_general"
    if not has_suitable_tool and not kb_has_content and not search_sufficient \
            and _EXTERNAL_SIGNAL_RE.search(q):
        return "no_tool"
    return None


# 回答失败启发式（需求8）：回答文本命中此类表述即视为「未真正解决用户问题」，
# 供聊天双路径（chat.py / chat_agent.py）在生成后触发能力缺口 CLI 方案闭环。
_ANSWER_FAILED_RE = re.compile(
    r"(无法|未能|不能|没有)(获取|提供|回答|告诉|查到|找到|给出)"
    r"|不足以回答|没有任何一条|不能凭空|无法根据|无法确定"
    r"|建议直接查看|知识库中未|未检索到|抱歉[，,].{0,12}(无法|不能|没有)"
)


def answer_indicates_failure(text: str) -> bool:
    """回答文本是否表明「未能解决用户问题」（触发能力缺口闭环的启发式）。"""
    return bool(_ANSWER_FAILED_RE.search(text or ""))


def extract_context(query: str) -> dict:
    """从问题抽取方案模板所需上下文（城市/URL 占位）。"""
    ctx: dict = {}
    m = _CITY_RE.search(query or "")
    if m:
        ctx["city"] = m.group(1)
    mu = re.search(r"https?://\S+", query or "")
    if mu:
        ctx["url"] = mu.group(0)
    return ctx


# 占位符 {name}：仅填充已提供的上下文键，未提供的保留原样（不猜测默认值）。
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def _fill_placeholders(text: str, ctx: dict) -> str:
    """安全填充：只替换 ctx 中存在的占位符，其余保留 {name} 原样。"""
    return _PLACEHOLDER_RE.sub(
        lambda m: str(ctx[m.group(1)]) if m.group(1) in ctx else m.group(0),
        text or "",
    )


def propose_solutions(scenario: str, context: Optional[dict] = None) -> List[dict]:
    """返回某场景的候选方案（占位符仅用已提供的上下文填充）。

    P0 修正：不再用「深圳 / example.com」等默认值兑底缺失占位符。未能填充的
    占位符保留原样，并把该方案标记为 needs_argument（缺参数），由上层追问，
    绝不静默猜测城市/URL（§4.2 missing_argument）。
    """
    recipe = CAPABILITY_RECIPES.get(scenario)
    if not recipe:
        return []
    ctx = {k: v for k, v in (context or {}).items() if v not in (None, "")}
    out = []
    for sol in recipe["solutions"]:
        argv = [_fill_placeholders(part, ctx) for part in sol.get("argv", [])]
        label = _fill_placeholders(sol.get("label", sol["id"]), ctx)
        unfilled = sorted({m.group(1) for part in argv for m in _PLACEHOLDER_RE.finditer(part)})
        item = {
            "id": sol["id"],
            "label": label,
            "kind": sol.get("kind", "cli"),
            "argv": argv,
            "risk_level": sol.get("risk_level", "low"),
            "note": sol.get("note", ""),
        }
        if unfilled:
            item["needs_argument"] = True
            item["missing_arguments"] = unfilled
        out.append(item)
    return out


def record_gap(*, session_id: str = "", user_id: str = "", trace_id: str = "",
               query: str = "", scenario: str = "", context: Optional[dict] = None) -> str:
    """留痕一次能力缺口（返回 gap id）。同场景重复出现会累计，供频次沉淀。"""
    gap_id = str(uuid.uuid4())
    solutions = propose_solutions(scenario, context)
    try:
        db.execute(
            "INSERT INTO capability_gaps (id, session_id, user_id, trace_id, query, scenario,"
            " context_json, proposed_json, chosen, status, executions, last_result, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,0,'',?)",
            (gap_id, session_id, user_id, trace_id, (query or "")[:500], scenario,
             json.dumps(context or {}, ensure_ascii=False),
             json.dumps(solutions, ensure_ascii=False), "", "pending",
             datetime.now().isoformat()),
        )
    except Exception as e:
        logger.warning("能力缺口留痕失败（忽略）: %s", e)
        return ""
    return gap_id


def get_gap(gap_id: str) -> Optional[dict]:
    row = db.query_one("SELECT * FROM capability_gaps WHERE id = ?", (gap_id,))
    if not row:
        return None
    d = dict(row)
    d["proposed"] = json.loads(d.pop("proposed_json", "[]") or "[]")
    d["context"] = json.loads(d.pop("context_json", "{}") or "{}")
    return d


async def execute_solution(gap_id: str, solution_id: str, *, confirmed: bool = False,
                           url: str = "") -> dict:
    """执行用户选定的方案（HITL 第二步），统一收敛到可信 Sandbox Controller。

    P0 改造（《整改方案》§17「移除直接执行与假成功降级」）：
      - 不再由本模块直接调用 CliRunner，而是构造 ExecutionSpec 交 SandboxController，
        由其强制执行合同边界（白名单二进制 / spec_id 绑定 / 网络出口审核 /
        隔离不可用则 sandbox_unavailable，绝不回退主机子进程）；
      - 缺参数不猜测：若 argv 仍有未填充占位符（如 {url}/{lat}/{lon}）→ 返回
        blocked + missing_argument，由上层追问，绝不静默填默认城市/URL。

    风险治理：方案 risk_level 为 high/critical 且未 confirmed 时不执行，返回
    needs_confirmation=True 让前端弹窗二次确认。
    """
    gap = get_gap(gap_id)
    if not gap:
        return {"success": False, "error": "gap 不存在"}
    solution = next((s for s in gap["proposed"] if s["id"] == solution_id), None)
    if not solution:
        return {"success": False, "error": "方案不存在"}

    risk = solution.get("risk_level", "low")
    if risk in ("high", "critical") and not confirmed:
        return {"success": False, "needs_confirmation": True, "risk_level": risk,
                "error": f"方案风险等级为 {risk}，需确认后在受控沙箱执行"}

    # 用户提供的 URL 作为上下文参与占位符填充（不猜测其它缺省值）
    ctx = dict(gap.get("context") or {})
    if url:
        ctx["url"] = url
    argv = [_fill_placeholders(part, ctx) for part in solution.get("argv", [])]

    # 缺参数不猜测：仍有未填充占位符 → blocked/missing_argument（§4.2）
    unfilled = sorted({m.group(1) for part in argv for m in _PLACEHOLDER_RE.finditer(part)})
    if unfilled:
        return {
            "success": False, "needs_confirmation": False, "risk_level": risk,
            "blocked": True, "execution_status": "blocked", "error_code": "missing_argument",
            "missing_arguments": unfilled, "argv": argv, "sandbox": True,
            "error": f"方案缺少必要参数：{'、'.join(unfilled)}，请补充后重试（不自动填充默认值）",
        }

    # 统一经受控 Sandbox Controller 执行（隔离不可用则拒绝，不回退主机）
    spec = ExecutionSpec(
        executor_kind="cli_spec",
        argv=argv,
        spec_id=solution_id,
        owner_id=str(gap.get("user_id", "") or ""),
        session_id=str(gap.get("session_id", "") or ""),
        timeout=30.0,
    )
    res = await sandbox_controller.submit_execution(spec)

    success = res.execution_status == "completed" and res.exit_code == 0
    status = "executed" if success else "pending"
    try:
        db.execute(
            "UPDATE capability_gaps SET chosen=?, status=?, executions=executions+1,"
            " last_result=? WHERE id=?",
            (solution_id, status, (res.stdout or res.error or "")[:500], gap_id),
        )
    except Exception as e:
        logger.warning("能力缺口执行回写失败（忽略）: %s", e)
    return {
        "success": success,
        "needs_confirmation": False,
        "risk_level": risk,
        "execution_status": res.execution_status,
        "error_code": res.error_code,
        "stdout": res.stdout,
        "stderr": res.stderr,
        "exit_code": res.exit_code,
        "error": res.error,
        "argv": res.argv or argv,
        "sandbox": True,
        "isolated": res.isolated,
    }


# ---------------------------------------------------------------------------
# Claude Code 式闭环（需求8）：执行后把结果回喂 Agent 再判断下一步
# ---------------------------------------------------------------------------
# 回喂判定提示词：要求 LLM 仅输出一个 JSON 对象，判定执行结果是否已解决用户问题。
_JUDGE_PROMPT = (
    "你是 Agent 的决策器。用户在对话中提出了一个系统当时无法直接解决的问题，"
    "系统已在沙箱中执行了一条命令行方案，现在把执行结果回喂给你。"
    "请判断该结果是否已足以解决用户的原始问题，并决定下一步动作。\n"
    "只输出一个 JSON 对象，不要输出多余文字，字段如下：\n"
    '{"resolved": true或false, '
    '"answer": "若 resolved=true，用中文把执行结果整理成对用户问题的直接回答；否则留空", '
    '"reasoning": "简短判断依据", '
    '"next_solution_id": "若 resolved=false 且需要继续，填下一步方案 id（只能从【可选下一步方案 id】中选，否则留空）", '
    '"next_url": "若下一步是抓取网页/接口，给出 URL，否则留空", '
    '"next_hint": "若 resolved=false，给用户的一句中文说明"}'
)


def _format_observation(exec_result: dict) -> str:
    """把沙箱执行结果格式化为回喂 LLM 的观测文本。"""
    argv = " ".join(exec_result.get("argv") or [])
    stdout = (exec_result.get("stdout") or "").strip()
    stderr = (exec_result.get("stderr") or "").strip()
    parts = [f"$ {argv}"]
    if stdout:
        parts.append(f"[stdout]\n{stdout[:4000]}")
    if stderr:
        parts.append(f"[stderr]\n{stderr[:1000]}")
    parts.append(f"[exit_code] {exec_result.get('exit_code')}")
    return "\n".join(parts)


def _parse_verdict(raw: str) -> dict:
    """解析 LLM 回喂判定输出。

    P0 修正（《整改方案》§2/§20：判定失败不再降级为已解决）：无 JSON /
    解析失败 / 非对象时，返回 resolved=False + resolution_status=unknown，
    保留原文供展示，绝不假成功。
    """
    text = (raw or "").strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {"resolved": False, "resolution_status": "unknown", "answer": "",
                "raw": text, "reasoning": "判定输出无 JSON，不降级为已解决"}
    try:
        data = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return {"resolved": False, "resolution_status": "unknown", "answer": "",
                "raw": text, "reasoning": "判定 JSON 解析失败，不降级为已解决"}
    if not isinstance(data, dict):
        return {"resolved": False, "resolution_status": "unknown", "answer": "",
                "raw": text, "reasoning": "判定非对象，不降级为已解决"}
    return data


async def _judge_next_action(query: str, scenario: str, executed_solution_id: str,
                             observation: str) -> dict:
    """回喂 LLM 判定下一步动作（resolved / answer / next_solution_id）。

    P0 修正：判定异常不再降级为已解决，而是 resolved=False + resolution_status=unknown。
    """
    from app.core.llm import LLMFactory

    allowed = [s["id"] for s in CAPABILITY_RECIPES.get(scenario, {}).get("solutions", [])
               if s["id"] != executed_solution_id]
    user_content = (
        f"【用户原始问题】\n{query}\n\n"
        f"【已执行方案 id】\n{executed_solution_id}\n\n"
        f"【可选下一步方案 id】\n{', '.join(allowed) if allowed else '（无）'}\n\n"
        f"【执行结果】\n{observation}\n"
    )
    try:
        llm = LLMFactory.create_assistant_llm()
    except Exception:
        llm = LLMFactory.create()
    try:
        raw = ""
        async for chunk in llm.chat(
            [{"role": "system", "content": _JUDGE_PROMPT},
             {"role": "user", "content": user_content}],
            stream=True,
        ):
            raw += chunk
        return _parse_verdict(raw)
    except Exception as e:
        logger.warning("能力缺口回喂判定异常（不降级为已解决）: %s", e)
        return {"resolved": False, "resolution_status": "unknown", "answer": "",
                "raw": observation, "reasoning": f"判定异常，保留未解决状态: {e}"}


def _persist_followup(session_id: str, answer: str) -> None:
    """把回喂生成的回答作为助手消息追加进原会话（Claude Code 式闭环收尾）。

    内存中有会话则 add_message + _persist_session（保持内存与 DB 一致）；
    否则直接 INSERT chat_messages（seq 取当前最大值 +1），避免加载整个会话。
    """
    if not session_id or not answer:
        return
    try:
        from app.services.chat import Message, chat_service
        session = chat_service.sessions.get(session_id)
        if session is not None:
            session.add_message(Message(role="assistant", content=answer, use_search=True))
            chat_service._persist_session(session)
            return
        row = db.query_one(
            "SELECT COALESCE(MAX(seq), -1) AS max_seq FROM chat_messages WHERE session_id=?",
            (session_id,),
        )
        seq = (row["max_seq"] if row else -1) + 1
        db.execute(
            "INSERT INTO chat_messages (message_id, session_id, seq, role, content,"
            " sources, search_results, use_knowledge, use_search, attachments, created_at, interrupted)"
            " VALUES (?,?,?,?,?,'[]','[]',0,1,'[]',?,0)",
            (str(uuid.uuid4()), session_id, seq, "assistant", answer, datetime.now().isoformat()),
        )
    except Exception as e:
        logger.warning("能力缺口回答落库失败（忽略）: %s", e)


async def execute_and_continue(gap_id: str, solution_id: str, *, confirmed: bool = False,
                               url: str = "", persist: bool = True) -> dict:
    """执行方案后把结果回喂 LLM 判定下一步（Claude Code 式闭环，需求8）。

    流程：沙箱执行 → 回喂 LLM 判定 {resolved, answer, next_solution_id, next_url}
      - resolved：落 gap.followup_text + status=resolved，并把 answer 作为助手消息
        追加进原会话，对话得以「解决用户需求」收尾；
      - 未解决但有下一步：next_solution_id 仅限本场景 CAPABILITY_RECIPES 内 id（安全边界），
        返回 next_solutions 供前端再发一轮 HITL；
      - 未解决且无下一步：如实回喂执行结果，提示用户。
    执行本身失败（success=False 或 needs_confirmation）时直接透传，不回喂。
    """
    exec_result = await execute_solution(gap_id, solution_id, confirmed=confirmed, url=url)
    if exec_result.get("needs_confirmation") or not exec_result.get("success"):
        return exec_result

    gap = get_gap(gap_id) or {}
    query = gap.get("query", "")
    scenario = gap.get("scenario", "")
    session_id = gap.get("session_id", "") or ""

    observation = _format_observation(exec_result)
    verdict = await _judge_next_action(query, scenario, solution_id, observation)

    resolved = bool(verdict.get("resolved"))
    answer = (verdict.get("answer") or "").strip()
    next_solution_id = (verdict.get("next_solution_id") or "").strip()
    next_url = (verdict.get("next_url") or "").strip()

    out = dict(exec_result)
    out["resolved"] = resolved
    out["followup_answer"] = answer if resolved else ""
    out["judge_reasoning"] = verdict.get("reasoning", "")
    out["observation"] = observation

    # 安全边界：下一步方案 id 必须属于本场景配方
    allowed_ids = {s["id"] for s in CAPABILITY_RECIPES.get(scenario, {}).get("solutions", [])}
    if not resolved and next_solution_id and next_solution_id in allowed_ids \
            and next_solution_id != solution_id:
        next_ctx = dict(gap.get("context") or {})
        if next_url:
            next_ctx["url"] = next_url
        next_solutions = [s for s in propose_solutions(scenario, next_ctx)
                          if s["id"] == next_solution_id]
        out["next_solutions"] = next_solutions
        out["next_hint"] = verdict.get("next_hint", "")
        try:
            db.execute("UPDATE capability_gaps SET status='pending', chosen='' WHERE id=?", (gap_id,))
        except Exception as e:
            logger.warning("能力缺口下一步回写失败（忽略）: %s", e)
    elif resolved and answer:
        if persist:
            _persist_followup(session_id, answer)
        try:
            db.execute("UPDATE capability_gaps SET status='resolved', followup_text=? WHERE id=?",
                       (answer[:4000], gap_id))
        except Exception as e:
            logger.warning("能力缺口 resolved 回写失败（忽略）: %s", e)
    else:
        out["next_hint"] = verdict.get("next_hint", "") or "执行完成，但未能自动整理出最终回答，请查看上方结果。"
    return out


MAX_AUTO_ROUNDS = 3


async def auto_resolve(gap_id: str, *, url: str = "") -> dict:
    """需求7：把「正常调用 CLI 能力」内化为模型自动执行——低危方案无需用户确认，
    循环执行 + 回喂判定，直到解决用户问题或用尽低危方案。

    medium/high/critical 方案不自动执行；若回喂判定给出的下一步属于非低危，
    则回退 HITL（needs_hitl=True）交由用户确认。persist=False：本函数在聊天流内
    被调用，最终回答由调用方拼接到 full_response 统一落库，避免重复写入。

    返回 {resolved, answer, needs_hitl, solutions, hint, attempts}。
    """
    gap = get_gap(gap_id) or {}
    proposed = gap.get("proposed") or []
    low = [s for s in proposed if (s.get("risk_level") or "low") == "low"]
    attempts: List[dict] = []
    if not low:
        return {"resolved": False, "needs_hitl": True, "solutions": proposed,
                "hint": "该场景方案需用户确认后执行", "attempts": attempts}
    tried = set()
    current_url = url
    for _ in range(MAX_AUTO_ROUNDS):
        nxt = next((s for s in low if s["id"] not in tried), None)
        if nxt is None:
            break
        tried.add(nxt["id"])
        res = await execute_and_continue(gap_id, nxt["id"], url=current_url, persist=False)
        attempts.append({
            "id": nxt["id"], "label": nxt.get("label", ""),
            "ok": bool(res.get("success")),
            "summary": (res.get("stdout") or res.get("error") or "")[:120],
        })
        if res.get("needs_confirmation") or not res.get("success"):
            continue  # 执行失败/需确认 → 尝试下一个低危方案
        if res.get("resolved"):
            return {"resolved": True, "answer": res.get("followup_answer", ""),
                    "needs_hitl": False, "solutions": [], "hint": "", "attempts": attempts}
        next_sols = res.get("next_solutions") or []
        if next_sols:
            ns = next_sols[0]
            if (ns.get("risk_level") or "low") != "low":
                return {"resolved": False, "needs_hitl": True, "solutions": [ns],
                        "hint": res.get("next_hint", "") or "下一步方案需用户确认",
                        "attempts": attempts}
            if ns["id"] not in tried:
                low.append(ns)
            current_url = res.get("next_url") or current_url
    return {"resolved": False, "needs_hitl": True, "solutions": proposed,
            "hint": "已自动尝试低危方案仍未解决，请手动选择", "attempts": attempts}


def frequency_suggestions(threshold: int = SEDIMENT_THRESHOLD, days: int = 30) -> dict:
    """聚合频繁能力缺口，产出「沉淀为 skill/工具」建议（需求6 可进化性）。"""
    from datetime import timedelta

    since = (datetime.now() - timedelta(days=days)).isoformat()
    rows = db.query(
        "SELECT scenario, COUNT(*) AS n, MAX(created_at) AS last_at,"
        " MAX(query) AS sample_query"
        " FROM capability_gaps WHERE created_at >= ? AND scenario != ''"
        " GROUP BY scenario HAVING n >= ? ORDER BY n DESC",
        (since, threshold),
    )
    suggestions = []
    for r in rows:
        scenario = r["scenario"]
        recipe = CAPABILITY_RECIPES.get(scenario, {})
        suggestions.append({
            "scenario": scenario,
            "label": recipe.get("label", scenario),
            "count": int(r["n"]),
            "last_at": r["last_at"],
            "sample_query": (r["sample_query"] or "")[:60],
            "suggested_name": f"auto_{scenario}",
            "suggested_kind": "skill" if scenario.startswith("realtime") else "tool",
            "solutions": propose_solutions(scenario),
            "message": f"「{recipe.get('label', scenario)}」场景近 {days} 天出现 {int(r['n'])} 次，"
                       f"是否沉淀为 {('Skill' if scenario.startswith('realtime') else '工具')}？",
        })
    return {"threshold": threshold, "days": days, "suggestions": suggestions}
