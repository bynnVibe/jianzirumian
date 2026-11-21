"""
见字如面 - 能力缺口（Capability Gap）治理：服务端工具链自动解决 + DeepSeek 式降级

当系统现有工具/能力解决不了用户问题（如「查询今天深圳的天气」仅开启知识检索），
本模块负责：

  1. 识别缺口：路由无合适工具 / 实时查询但知识库无结果 → 归一化为 scenario；
  2. 生成候选方案：从内置「能力配方」（CAPABILITY_RECIPES）产出服务端可执行的
     只读工具链（如 地理编码 → open-meteo 天气、wttr.in），与主流 function-calling
     实现一致——全部由服务端调用，绝不要求用户执行命令；
  3. 自动解决：auto_resolve 逐轮执行低风险方案并经 LLM 判定是否解决用户问题，
     解决后把结果回喂主回答链路；
  4. 降级说明：自动解决失败时，随 SSE capability_gap 事件下发「能力受限」说明
     （kind=limitation）与应用内动作建议（如开启联网搜索并重新提问），
     参考 DeepSeek 的诚实降级设计，不向用户暴露 CLI；
  5. 频次沉淀：同 scenario 累计达阈值（默认 3 次）即在可观测性提示
     「该场景频繁出现，是否沉淀为 skill/工具」，体现系统可进化性。

所有缺口与执行均落库 capability_gaps 表，供可观测性「技能沉淀建议」面板聚合。
"""
import asyncio
import hashlib
import json
import logging
import re
import unicodedata
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from urllib.parse import quote

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
                "id": "cli_geocode",
                "label": "先把 {city} 解析为经纬度（公开地理编码接口）",
                "kind": "cli",
                "argv": ["curl", "-s", "--max-time", "15",
                         "https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=zh&format=json"],
                "risk_level": "low",
                "note": "为 open-meteo 天气接口提供坐标，不猜测城市位置",
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
    """安全填充：只替换 ctx 中存在的占位符，其余保留 {name} 原样。

    URL 内的非 ASCII 值（如中文城市名）做百分号编码：部分公开接口
    （geocoding-api.open-meteo.com 前置 nginx）对原始 UTF-8 查询串返回 403。
    """
    is_url = (text or "").startswith("http")

    def repl(m: re.Match) -> str:
        key = m.group(1)
        if key not in ctx:
            return m.group(0)
        val = str(ctx[key])
        if is_url:
            val = re.sub(r"[^\x00-\x7F]", lambda ch: quote(ch.group(0)), val)
        return val

    return _PLACEHOLDER_RE.sub(repl, text or "")


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


def gap_identity(query: str, scenario: str, user_id: str) -> tuple[str, str, str]:
    """窄意图分组；未知能力保留问题实体和约束，不按宽泛场景合并。"""
    normalized = " ".join(unicodedata.normalize("NFC", query or "").split())
    normalized = re.sub(r"^(请问|请|帮我|麻烦)?(查询|查一下|查看)?", "", normalized).strip()
    if scenario == "realtime_weather":
        intent = ("weather.alerts" if re.search(r"预警|台风|警报", normalized) else
                  "weather.forecast" if re.search(r"预报|明天|后天|未来|本周|下周", normalized) else "weather.current")
    elif re.search(r"海拔|elevation", normalized, re.I):
        intent = "geography.elevation"
    else:
        intent = normalized[:500]
    missing = {"weather.current": "weather_current_api", "weather.forecast": "weather_forecast_api",
               "weather.alerts": "weather_alert_api", "geography.elevation": "elevation_api"}.get(intent, "external_read")
    key = hashlib.sha256(json.dumps([user_id, intent, missing], ensure_ascii=False).encode()).hexdigest()
    return intent, missing, key


def record_gap(*, session_id: str = "", user_id: str = "", trace_id: str = "",
               query: str = "", scenario: str = "", context: Optional[dict] = None,
               request_id: str = "", failure_reason: str = "") -> str:
    """留痕一次能力缺口（返回 gap id）。同场景重复出现会累计，供频次沉淀。"""
    if re.search(r"^\s*@(?:skills?|tools?)\b|(?:添加|创建|沉淀|安装).{0,12}(?:技能|工具|skill|tool)", query or "", re.I):
        return ""
    gap_id = str(uuid.uuid4())
    intent, missing, group = gap_identity(query, scenario, user_id)
    request_key = request_id or trace_id
    solutions = propose_solutions(scenario, context)
    try:
        db.execute(
            "INSERT INTO capability_gaps (id, session_id, user_id, trace_id, query, scenario,"
            " context_json, proposed_json, chosen, status, executions, last_result, created_at,"
            " intent_key,missing_capability,group_key,request_key,failure_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,0,'',?,?,?,?,?,?)",
            (gap_id, session_id, user_id, trace_id, (query or "")[:500], scenario,
             json.dumps(context or {}, ensure_ascii=False),
             json.dumps(solutions, ensure_ascii=False), "", "pending",
             datetime.now().isoformat(), intent, missing, group, request_key, failure_reason[:80]),
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
    if success and solution_id == "cli_geocode":
        _absorb_geocode(gap_id, gap, res.stdout)
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


def _absorb_geocode(gap_id: str, gap: dict, stdout: str) -> None:
    """地理编码成功后把坐标写回缺口上下文，并重填候选方案占位符。

    主流工具链做法：城市名 → 地理编码 → 经纬度 → 天气接口，全程服务端完成，
    不让用户补坐标；解析失败则保持缺参数状态，绝不猜测。
    """
    try:
        data = json.loads(stdout or "")
        hit = (data.get("results") or [None])[0]
        if not isinstance(hit, dict) or "latitude" not in hit or "longitude" not in hit:
            return
        ctx = dict(gap.get("context") or {})
        ctx["lat"] = str(hit["latitude"])
        ctx["lon"] = str(hit["longitude"])
        if hit.get("timezone"):
            ctx["timezone"] = str(hit["timezone"])
        solutions = propose_solutions(gap.get("scenario", ""), ctx)
        db.execute(
            "UPDATE capability_gaps SET context_json=?, proposed_json=? WHERE id=?",
            (json.dumps(ctx, ensure_ascii=False), json.dumps(solutions, ensure_ascii=False), gap_id),
        )
    except (ValueError, TypeError, KeyError, IndexError) as e:
        logger.warning("地理编码结果解析失败（保持缺参数）: %s", e)


def limitation_payload(gap_id: str, scenario: str, scenario_label: str, query: str,
                       *, use_search: bool = False) -> dict:
    """能力受限降级（参考 DeepSeek）：如实说明限制 + 应用内可操作建议。

    不再向用户下发需要自行执行的命令行方案；系统侧工具调用已在 auto_resolve
    内完成，这里只呈现「为什么答不了」与「在应用里能做什么」。
    """
    if use_search:
        message = ("抱歉，这个问题需要实时数据，但联网搜索与现有工具都未能取得可靠结果，"
                   "我无法给出准确答案。可以稍后重试，或补充更具体的时间、地点后再问。")
        suggestions = [{"id": "retry", "label": "重新提问"}]
    else:
        message = ("抱歉，当前仅开启了知识检索，知识库中没有这类实时数据，我无法直接回答。"
                   "开启「联网搜索」后重新提问，我可以结合网络信息作答。")
        suggestions = [{"id": "enable_search", "label": "开启联网搜索并重新提问"}]
    return {
        "kind": "limitation",
        "gap_id": gap_id,
        "scenario": scenario,
        "scenario_label": scenario_label,
        "query": query,
        "message": message,
        "suggestions": suggestions,
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


MAX_AUTO_ROUNDS = 4


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
    low = [s for s in proposed if (s.get("risk_level") or "low") == "low" and not s.get("needs_argument")]
    attempts: List[dict] = []
    if not low:
        return {"resolved": False, "needs_hitl": True, "solutions": proposed,
                "hint": "该场景方案需用户确认后执行", "attempts": attempts}
    tried = set()
    current_url = url
    for _ in range(MAX_AUTO_ROUNDS):
        # 每轮重读缺口：地理编码等前序步骤可能已补齐后续方案的参数（如经纬度）
        fresh = get_gap(gap_id) or {}
        candidates = fresh.get("proposed") or proposed
        low = [s for s in candidates
               if (s.get("risk_level") or "low") == "low" and not s.get("needs_argument")]
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
            current_url = res.get("next_url") or current_url
    return {"resolved": False, "needs_hitl": True, "solutions": proposed,
            "hint": "已自动尝试低危方案仍未解决，请手动选择", "attempts": attempts}


def frequency_suggestions(threshold: int = SEDIMENT_THRESHOLD, days: int = 30,
                          *, user_id: str = "", include_cooldown: bool = False) -> dict:
    """个人建议按独立请求聚合；无属主时仅返回脱敏全局趋势。"""
    threshold, days = max(3, min(50, int(threshold))), max(1, min(180, int(days)))
    since = (datetime.now() - timedelta(days=days)).isoformat()
    cooldown = (datetime.now() - timedelta(days=7)).isoformat()
    args = [since]
    owner_clause = ""
    if user_id:
        owner_clause = " AND g.user_id=?"
        args.append(user_id)
    rows = db.query(
        "SELECT g.user_id,g.group_key,g.intent_key,g.missing_capability,g.scenario,"
        "COUNT(DISTINCT g.request_key) n,MAX(g.created_at) last_at,MAX(g.query) sample_query,"
        "s.status suggestion_status,s.last_prompted_at,s.draft_id,d.status draft_status,d.expires_at draft_expires_at,u.id installed_skill_id "
        "FROM capability_gaps g LEFT JOIN capability_suggestions s "
        "ON s.owner_id=g.user_id AND s.group_key=g.group_key "
        "LEFT JOIN skill_drafts d ON d.owner_id=s.owner_id AND d.id=s.draft_id "
        "LEFT JOIN user_skills u ON u.owner_id=d.owner_id AND u.id=d.skill_id "
        "WHERE g.created_at>=? AND g.user_id NOT IN ('','guest') AND g.request_key!='' AND g.group_key!='' "
        "AND g.scenario!='' AND g.status NOT IN ('resolved','sedimented') AND g.failure_reason='' "
        "AND NOT EXISTS (SELECT 1 FROM capability_gaps done WHERE done.user_id=g.user_id "
        "AND done.request_key=g.request_key AND (done.status='resolved' OR done.failure_reason!=''))" + owner_clause +
        " GROUP BY g.user_id,g.group_key HAVING n>=? ORDER BY n DESC,last_at DESC",
        (*args, threshold))
    suggestions = []
    trend_owners = {}
    for row in rows:
        if row["suggestion_status"] == "resolved":
            continue
        active_draft = ((row["draft_status"] == "pending" and (row["draft_expires_at"] or "") > datetime.now().isoformat())
                        or (row["draft_status"] == "imported" and row["installed_skill_id"]))
        if user_id and not include_cooldown and row["suggestion_status"] == "drafted" and active_draft:
            continue
        if user_id and not include_cooldown and (row["last_prompted_at"] or "") > cooldown:
            continue
        label = {"weather_current_api": "当前天气查询", "weather_forecast_api": "天气预报",
                 "weather_alert_api": "天气预警", "elevation_api": "坐标海拔查询"}.get(
                     row["missing_capability"], "外部只读能力")
        trend_owners.setdefault((row["scenario"], row["missing_capability"]), set()).add(row["user_id"])
        suggestions.append({
            "id": row["group_key"] if user_id else "", "scenario": row["scenario"], "label": label,
            "count": row["n"], "last_at": row["last_at"],
            "sample_query": row["sample_query"] if user_id else "",
            "intent": row["intent_key"] if user_id else "",
            "missing_capability": row["missing_capability"], "suggested_name": f"auto_{row['scenario']}",
            "suggested_kind": "skill", "solutions": [], "draft_id": row["draft_id"] if user_id else "",
            "message": f"近 {days} 天有 {row['n']} 条独立请求缺少{label}，可进入个人 Skill 草稿流程；添加不等于验收通过。"})
    if not user_id:
        trends = {}
        for item in suggestions:
            key = (item["scenario"], item["missing_capability"])
            trend = trends.setdefault(key, {**item, "count": 0, "owners": 0})
            trend["count"] += item["count"]
            trend["owners"] = len(trend_owners[key])
            trend["last_at"] = max(trend["last_at"], item["last_at"])
            trend["message"] = f"{trend['owners']} 位用户存在同类未满足能力，共 {trend['count']} 条独立请求；仅展示脱敏趋势。"
        suggestions = list(trends.values())
    elif not include_cooldown:
        suggestions = suggestions[:20]
    return {"threshold": threshold, "days": days, "suggestions": suggestions, "scope": "personal" if user_id else "aggregate"}


def mark_suggestions_seen(user_id: str, group_keys: list[str]) -> dict:
    """显式展示回执领取七天提示窗口；GET 本身不改变状态。"""
    eligible = {s["id"] for s in frequency_suggestions(user_id=user_id)["suggestions"]}
    now = datetime.now()
    marked = []
    with db.transaction() as conn:
        for key in dict.fromkeys(group_keys[:20]):
            if key not in eligible:
                continue
            cursor = conn.execute(
                "INSERT INTO capability_suggestions(owner_id,group_key,status,last_prompted_at,updated_at) "
                "VALUES(?,?,'shown',?,?) ON CONFLICT(owner_id,group_key) DO UPDATE SET "
                "status='shown',last_prompted_at=excluded.last_prompted_at,updated_at=excluded.updated_at "
                "WHERE capability_suggestions.last_prompted_at<=? AND capability_suggestions.status!='resolved' "
                "AND (capability_suggestions.status!='processing' OR capability_suggestions.updated_at<=?) "
                "AND NOT EXISTS (SELECT 1 FROM skill_drafts d WHERE d.owner_id=capability_suggestions.owner_id "
                "AND d.id=capability_suggestions.draft_id AND ((d.status='pending' AND d.expires_at>?) "
                "OR (d.status='imported' AND EXISTS (SELECT 1 FROM user_skills u WHERE u.id=d.skill_id AND u.owner_id=d.owner_id))))",
                (user_id, key, now.isoformat(), now.isoformat(), (now - timedelta(days=7)).isoformat(),
                 (now - timedelta(minutes=5)).isoformat(), now.isoformat()))
            if cursor.rowcount:
                marked.append(key)
    return {"ids": marked, "cooldown_days": 7}


def dismiss_suggestion(user_id: str, group_key: str):
    from fastapi import HTTPException
    if not any(s["id"] == group_key for s in frequency_suggestions(user_id=user_id, include_cooldown=True)["suggestions"]):
        raise HTTPException(404, "建议不存在或不属于当前用户")
    now = datetime.now().isoformat()
    with db.transaction() as conn:
        row = conn.execute("SELECT status FROM capability_suggestions WHERE owner_id=? AND group_key=?",
                           (user_id, group_key)).fetchone()
        if row and row[0] in ("processing", "drafted", "resolved"):
            raise HTTPException(409, "建议已有发现任务或待验收能力，请处理已有草稿")
        conn.execute("INSERT INTO capability_suggestions(owner_id,group_key,status,last_prompted_at,updated_at) "
                     "VALUES(?,?,'dismissed',?,?) ON CONFLICT(owner_id,group_key) DO UPDATE SET "
                     "status='dismissed',last_prompted_at=excluded.last_prompted_at,updated_at=excluded.updated_at",
                     (user_id, group_key, now, now))
    return {"status": "dismissed", "cooldown_days": 7}


async def accept_suggestion(user_id: str, group_key: str):
    """接受建议只启动已有草稿流程；原子领取防止并发重复生成，不直接安装。"""
    from fastapi import HTTPException
    from app.services import skill_drafts, skill_workflow
    from app.services.chat import chat_service, Message
    candidates = frequency_suggestions(user_id=user_id, include_cooldown=True)["suggestions"]
    candidate = next((s for s in candidates if s["id"] == group_key), None)
    if not candidate:
        raise HTTPException(404, "建议不存在或不属于当前用户")
    now = datetime.now()
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM capability_suggestions WHERE owner_id=? AND group_key=?",
                           (user_id, group_key)).fetchone()
        previous = dict(row) if row else {}
        if previous.get("status") == "processing" and previous["updated_at"] > (now - timedelta(minutes=5)).isoformat():
            raise HTTPException(409, "正在生成同一建议的草稿，请稍后刷新")
        old_draft = conn.execute("SELECT id FROM skill_drafts WHERE owner_id=? AND session_id=? AND message_id=? "
                                 "AND ((status='imported' AND EXISTS (SELECT 1 FROM user_skills u WHERE u.id=skill_drafts.skill_id "
                                 "AND u.owner_id=skill_drafts.owner_id)) OR (status='pending' AND expires_at>?)) ORDER BY created_at DESC LIMIT 1",
                                 (user_id, previous.get("session_id", ""), previous.get("message_id", ""), now.isoformat())).fetchone() if previous else None
        if old_draft:
            draft_id = old_draft[0]
            conn.execute("UPDATE capability_suggestions SET status='drafted',draft_id=?,last_prompted_at=?,updated_at=? "
                         "WHERE owner_id=? AND group_key=?",
                         (draft_id, now.isoformat(), now.isoformat(), user_id, group_key))
        else:
            draft_id = ""
            if (previous.get("status") not in ("shown", "open", "processing")
                    and previous.get("last_prompted_at", "") > (now - timedelta(days=7)).isoformat()):
                raise HTTPException(409, "同类建议处于 7 天冷却期；可在对话中主动发起 @skills")
            session_id, message_id = str(uuid.uuid4()), str(uuid.uuid4())
            conn.execute("INSERT INTO capability_suggestions(owner_id,group_key,status,session_id,message_id,updated_at) "
                         "VALUES(?,?,'processing',?,?,?) ON CONFLICT(owner_id,group_key) DO UPDATE SET "
                         "status='processing',session_id=excluded.session_id,message_id=excluded.message_id,updated_at=excluded.updated_at",
                         (user_id, group_key, session_id, message_id, now.isoformat()))
    if draft_id:
        return {"draft": skill_drafts.get_draft(draft_id, user_id), "session_id": previous["session_id"]}
    task = asyncio.current_task()
    session = assistant = None
    chat_service._active_tasks.setdefault(session_id, set()).add(task)
    try:
        session = chat_service.create_session(user_id=user_id, session_id=session_id)
        session.add_message(Message(role="user", content="@skills " + candidate["sample_query"]))
        assistant = Message(role="assistant", content="正在根据个人能力建议查找资料并生成草稿。", message_id=message_id)
        session.add_message(assistant)
        chat_service._persist_session(session)
        async with asyncio.timeout(180):
            draft = await skill_workflow.build_draft(user_id, session_id, message_id, candidate["sample_query"])
        # 即使外部发现器吞掉取消，也不得恢复已删除会话或产生可确认的迟到草稿。
        if task.cancelling() or not db.query_one(
                "SELECT 1 FROM chat_sessions WHERE session_id=? AND user_id=?", (session_id, user_id)):
            raise asyncio.CancelledError()
        assistant.content = f"已准备《{draft['name']}》的个人 Skill 草稿。请确认来源与依赖；保存不代表能力验收通过。"
        timestamp = datetime.now().isoformat()
        with db.transaction() as conn:
            chat_service._persist_session_transaction(conn, session)
            conn.execute("UPDATE capability_suggestions SET status='drafted',draft_id=?,last_prompted_at=?,updated_at=? "
                         "WHERE owner_id=? AND group_key=? AND message_id=?",
                         (draft["id"], timestamp, timestamp, user_id, group_key, message_id))
        return {"draft": draft, "session_id": session_id}
    except BaseException:
        exists = db.query_one("SELECT 1 FROM chat_sessions WHERE session_id=? AND user_id=?", (session_id, user_id))
        if not exists:
            skill_drafts.invalidate_session(session_id)
            chat_service.sessions.pop(session_id, None)
        elif assistant:
            assistant.content = "草稿发现未完成，未安装能力。可返回个人能力建议重试。"
            assistant.interrupted = True
            chat_service._persist_session(session)
        db.execute("UPDATE capability_suggestions SET status='open',last_prompted_at='',updated_at=? "
                   "WHERE owner_id=? AND group_key=? AND message_id=?",
                   (datetime.now().isoformat(), user_id, group_key, message_id))
        raise
    finally:
        active = chat_service._active_tasks.get(session_id, set())
        active.discard(task)
        if not active:
            chat_service._active_tasks.pop(session_id, None)


def _business_messages(conn, owner, session_id, user_message_id, message_id):
    from fastapi import HTTPException
    rows = conn.execute(
        "SELECT m.* FROM chat_messages m JOIN chat_sessions s ON s.session_id=m.session_id "
        "WHERE s.user_id=? AND m.session_id=? AND m.message_id IN (?,?)",
        (owner, session_id, user_message_id, message_id)).fetchall()
    user = next((dict(r) for r in rows if r["message_id"] == user_message_id and r["role"] == "user"), None)
    answer = next((dict(r) for r in rows if r["message_id"] == message_id and r["role"] == "assistant"), None)
    if (not user or not answer or answer["seq"] != user["seq"] + 1 or answer.get("interrupted")
            or not answer["content"].strip() or answer_indicates_failure(answer["content"])):
        raise HTTPException(409, "业务轮次不存在、已中断或没有完整可核验的回答")
    return user, answer


def _business_executions(conn, owner, session_id, message_id, identity):
    from fastapi import HTTPException
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM personal_tool_executions WHERE owner_id=? AND session_id=? AND message_id=? ORDER BY id",
        (owner, session_id, message_id))]
    # 数据结构通过与业务满足分开：unverified 需求只能等待用户验收，不能自动算成功。
    if (not rows or not identity.get("bindings") or any(
            r["test_id"] or r["result_origin"] != "live" or r["execution_status"] != "completed"
            or r["validation_status"] != "passed" or r["requirement_status"] not in ("satisfied", "unverified")
            or r["error_code"] or not r["finished_at"] for r in rows)):
        raise HTTPException(409, "需要真实业务执行且数据验证通过；样例、缓存、失败或未验证数据不能作为验收证据")
    actual = {(r["tool_id"], r["version"]) for r in rows}
    required = {(b["tool_id"], b["version"]) for b in identity["bindings"]}
    if actual != required:
        raise HTTPException(409, "本轮没有完整执行当前 Skill 绑定的工具版本")
    return rows


def record_business_evidence(owner, session_id, user_message_id, message_id, identity, result_status):
    """仅聊天收尾调用，记录待人工验收证据；不将安装、样例或模型判断标成已解决。"""
    from fastapi import HTTPException
    from app.services import skill_dependencies, skill_packages
    if not identity or result_status not in ("ok", "unverified"):
        return
    try:
        with db.transaction() as conn:
            if skill_dependencies.execution_identity(owner, identity["skill_id"], conn=conn) != identity:
                return
            user, answer = _business_messages(conn, owner, session_id, user_message_id, message_id)
            executions = _business_executions(conn, owner, session_id, message_id, identity)
            suggestions = conn.execute(
                "SELECT s.group_key,s.draft_id FROM capability_suggestions s JOIN skill_drafts d "
                "ON d.owner_id=s.owner_id AND d.id=s.draft_id WHERE s.owner_id=? AND d.skill_id=? "
                "AND d.status='imported' AND s.status NOT IN ('resolved','processing')",
                (owner, identity["skill_id"])).fetchall()
            for suggestion in suggestions:
                gap = conn.execute("SELECT scenario FROM capability_gaps WHERE user_id=? AND group_key=? "
                                   "ORDER BY created_at DESC LIMIT 1", (owner, suggestion["group_key"])).fetchone()
                if not gap or gap_identity(user["content"], gap["scenario"], owner)[2] != suggestion["group_key"]:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO capability_acceptance_evidence(id,owner_id,group_key,draft_id,skill_id,identity_json,"
                    "session_id,user_message_id,message_id,query_hash,answer_hash,execution_ids_json,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, owner, suggestion["group_key"], suggestion["draft_id"], identity["skill_id"],
                     skill_packages.canonical(identity), session_id, user_message_id, message_id,
                     skill_packages.sha(user["content"].encode()), skill_packages.sha(answer["content"].encode()),
                     skill_packages.canonical([r["id"] for r in executions]), datetime.now().isoformat()))
    except (HTTPException, ValueError, KeyError, TypeError):
        return
    except Exception:
        logger.exception("业务验收证据记录失败，建议保持未解决")


def _validate_business_evidence(conn, owner, evidence):
    from fastapi import HTTPException
    from app.services import skill_dependencies, skill_packages
    identity = json.loads(evidence["identity_json"])
    if skill_dependencies.execution_identity(owner, evidence["skill_id"], conn=conn) != identity:
        raise HTTPException(409, "Skill 或工具配置已变更，请使用当前版本重新完成业务请求")
    user, answer = _business_messages(conn, owner, evidence["session_id"], evidence["user_message_id"], evidence["message_id"])
    if (skill_packages.sha(user["content"].encode()) != evidence["query_hash"]
            or skill_packages.sha(answer["content"].encode()) != evidence["answer_hash"]):
        raise HTTPException(409, "原业务问答已变化，不能复用旧验收证据")
    executions = _business_executions(conn, owner, evidence["session_id"], evidence["message_id"], identity)
    if [r["id"] for r in executions] != json.loads(evidence["execution_ids_json"]):
        raise HTTPException(409, "业务执行记录已变化，请刷新")
    return user, answer, executions


def _evidence_hash(evidence):
    from app.services import skill_packages
    return skill_packages.sha(skill_packages.canonical({k: evidence[k] for k in (
        "id", "owner_id", "group_key", "draft_id", "identity_json", "session_id", "user_message_id",
        "message_id", "query_hash", "answer_hash", "execution_ids_json", "created_at")}).encode())


def pending_acceptances(owner):
    """刷新可恢复已安装待验收项；仅返回属主本人资料和真实业务证据。"""
    from fastapi import HTTPException
    result = []
    with db.transaction() as conn:
        suggestions = conn.execute(
            "SELECT s.group_key,s.draft_id,d.skill_id,d.name FROM capability_suggestions s JOIN skill_drafts d "
            "ON s.owner_id=d.owner_id AND s.draft_id=d.id WHERE s.owner_id=? AND d.status='imported' "
            "AND s.status!='resolved' ORDER BY s.updated_at DESC LIMIT 20", (owner,)).fetchall()
        for suggestion in suggestions:
            item = dict(suggestion)
            gap = conn.execute("SELECT query FROM capability_gaps WHERE user_id=? AND group_key=? "
                               "ORDER BY created_at DESC LIMIT 1", (owner, item["group_key"])).fetchone()
            original = conn.execute("SELECT package_json FROM skill_drafts WHERE owner_id=? AND id=?", (owner, item["draft_id"])).fetchone()
            try:
                request = json.loads(original["package_json"]).get("provenance", {}).get("requested_capability", "") if original else ""
            except (ValueError, TypeError, AttributeError):
                request = ""
            previous = conn.execute("SELECT status,note,confirmed_at FROM capability_acceptance_evidence WHERE owner_id=? "
                                    "AND group_key=? AND status='rejected' ORDER BY confirmed_at DESC LIMIT 1",
                                    (owner, item["group_key"])).fetchone()
            item.update(sample_query=request or (gap["query"] if gap else ""), evidence=[],
                        previous_decision=dict(previous) if previous else None)
            rows = conn.execute("SELECT * FROM capability_acceptance_evidence WHERE owner_id=? AND group_key=? "
                                "AND draft_id=? AND status='pending' ORDER BY created_at DESC LIMIT 5",
                                (owner, item["group_key"], item["draft_id"])).fetchall()
            for row in rows:
                evidence = dict(row)
                try:
                    user, answer, executions = _validate_business_evidence(conn, owner, evidence)
                except (HTTPException, ValueError, KeyError, TypeError):
                    continue
                item["evidence"].append({"id": evidence["id"], "expected_hash": _evidence_hash(evidence),
                    "session_id": evidence["session_id"], "message_id": evidence["message_id"],
                    "query": user["content"], "answer": answer["content"], "created_at": evidence["created_at"],
                    "version": json.loads(evidence["identity_json"])["version"],
                    "executions": [{k: r[k] for k in ("tool_id", "version", "execution_status", "validation_status",
                                                       "requirement_status", "result_origin")} for r in executions]})
            result.append(item)
    return result


def acceptance_history(owner):
    return db.query("SELECT e.id,e.skill_id,e.note,e.confirmed_at,COALESCE(d.name,'已删除 Skill') name "
                    "FROM capability_acceptance_evidence e LEFT JOIN skill_drafts d ON d.owner_id=e.owner_id AND d.id=e.draft_id "
                    "WHERE e.owner_id=? AND e.status='accepted' ORDER BY e.confirmed_at DESC LIMIT 20", (owner,))


def confirm_business_acceptance(owner, group_key, evidence_id, expected_hash, accepted, note):
    from fastapi import HTTPException
    if type(accepted) is not bool or not isinstance(note, str) or not 4 <= len(note.strip()) <= 1000:
        raise HTTPException(400, "请填写核对原需求的具体验收结论（4–1000 字）")
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM capability_acceptance_evidence WHERE owner_id=? AND group_key=? AND id=?",
                           (owner, group_key, evidence_id)).fetchone()
        suggestion = conn.execute("SELECT * FROM capability_suggestions WHERE owner_id=? AND group_key=?",
                                  (owner, group_key)).fetchone()
        if not row or not suggestion:
            raise HTTPException(404, "验收证据不存在或不属于当前用户")
        evidence = dict(row)
        if expected_hash != _evidence_hash(evidence) or suggestion["draft_id"] != evidence["draft_id"]:
            raise HTTPException(409, "验收目标已变化，请重新核对")
        status = "accepted" if accepted else "rejected"
        if evidence["status"] == status:
            return {"status": suggestion["status"], "evidence_id": evidence_id}
        if evidence["status"] != "pending" or suggestion["status"] == "resolved":
            raise HTTPException(409, "此业务结果已确认，不能重复改变验收结论")
        if not conn.execute("SELECT 1 FROM skill_drafts WHERE owner_id=? AND id=? AND skill_id=? AND status='imported'",
                            (owner, evidence["draft_id"], evidence["skill_id"])).fetchone():
            raise HTTPException(409, "建议对应的 Skill 尚未成功安装")
        if accepted:
            try:
                _validate_business_evidence(conn, owner, evidence)
            except (ValueError, KeyError, TypeError) as exc:
                raise HTTPException(409, "验收证据已失效，请重新完成业务请求") from exc
        now = datetime.now().isoformat()
        conn.execute("UPDATE capability_acceptance_evidence SET status=?,note=?,confirmed_at=? WHERE id=? AND owner_id=?",
                     (status, note.strip(), now, evidence_id, owner))
        conn.execute("UPDATE capability_suggestions SET status=?,updated_at=? WHERE owner_id=? AND group_key=?",
                     ("resolved" if accepted else "drafted", now, owner, group_key))
        if accepted:
            conn.execute("UPDATE capability_gaps SET status='resolved' WHERE user_id=? AND group_key=? AND failure_reason='' "
                         "AND created_at<=?", (owner, group_key, evidence["created_at"]))
    return {"status": "resolved" if accepted else "drafted", "evidence_id": evidence_id}
