"""
见字如面 - 确定性工具直调（direct_tool 执行模式）

需求契约参数齐全（域/实体/时间明确）时，按固定计划直接调用已授权工具：
不创建决策模型、不让模型猜测参数；答案由确定性模板渲染（带单位/来源/适用时刻）。
任一环节不满足（工具未授权、实体缺失、复杂诉求）返回 None 回退模型决策。
绝不绕过治理门禁：执行仍走 invoke_tool（约束钩子/可观测性/限流/凭据隔离）。
"""
import asyncio
import logging
import re
import time

from app.core import observability as obs
from app.core.requirement_contract import build_contract
from app.services.tools.base import ToolResult
from app.services.tools import base as tools_base
from app.services.tools.invoke import invoke_tool

logger = logging.getLogger("jianziruyang.tools.direct")

# 建议/分析/对比等复杂诉求 → 留给模型决策；直调只承担纯数据读取
_COMPLEX_RE = re.compile(r"建议|适合|应该|穿|带伞|分析|为什么|对比|结合|根据我的|笔记|资料|怎么样")

# WMO 天气代码 → 中文描述（Open-Meteo weather_code）
_WMO = {0: "晴", 1: "基本晴", 2: "局部多云", 3: "阴", 45: "雾", 48: "冻雾",
        51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨",
        66: "冻雨", 67: "强冻雨", 71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
        80: "小阵雨", 81: "阵雨", 82: "强阵雨", 85: "阵雪", 86: "强阵雪",
        95: "雷暴", 96: "雷暴伴冰雹", 99: "强雷暴伴冰雹"}


def _connector_tools(owner, tools):
    """已授权目录 → {连接器类型: 函数名}；混入非个人连接器工具时保守回退（None）。"""
    from app.services import personal_tools
    found = {}
    for t in tools:
        name = t["function"]["name"]
        # 个人工具以功能名注册、不在内置注册表；混入内置工具时保守回退（None）
        if not isinstance(name, str) or tools_base.get_tool(name) is not None:
            return None
        tool = personal_tools.resolve(owner, name)
        if tool is None or tool.spec.get("executor") != "connector":
            return None
        found[tool.spec["config"].get("connector")] = name
    return found


def plan(query, owner, tools):
    """构建确定性直调计划；参数不全/工具缺失返回 None（回退模型决策，不猜测）。

    首版覆盖天气域：城市名经 geocode 解析坐标（唯一命中才继续，歧义请用户选择），
    今天/明天 → weather_forecast，现在/实时 → weather（实况）。
    """
    q = (query or "").strip()
    if not q or _COMPLEX_RE.search(q):
        return None
    contract = build_contract(q)
    if contract.domain != "weather" or not contract.entity_resolved or contract.missing_arguments:
        return None
    if contract.requested_time not in ("now", "today", "tomorrow"):
        return None
    try:
        connectors = _connector_tools(owner, tools)
    except Exception as e:
        logger.warning("直调计划解析工具目录失败（回退模型决策）: %s", e)
        return None
    if not connectors:
        return None
    weather_kind = "weather" if contract.data_kind == "live" else "weather_forecast"
    weather_tool, geocode_tool = connectors.get(weather_kind), connectors.get("geocode")
    if not weather_tool or not geocode_tool:
        return None
    return {"city": contract.entity, "data_kind": contract.data_kind,
            "requested_time": contract.requested_time,
            "geocode_tool": geocode_tool, "weather_tool": weather_tool}


def _collect_sources(name, outcome, result):
    """与 native.run_tool_loop 同口径：仅公开规范化 URL 与元信息，不下发原始返回值。"""
    from app.services.url_fetch import normalize_url
    for source in (outcome.extra or {}).get("sources", [])[:5]:
        try:
            url = normalize_url(source.get("url"))
        except (ValueError, AttributeError):
            continue
        result["tool_sources"].append({
            "index": len(result["tool_sources"]) + 1,
            "title": str(source.get("title") or name)[:200], "url": url, "tool": name,
            **{k: (outcome.extra or {}).get(k) for k in
               ("fetched_at", "observed_at", "published_at", "valid_at", "timezone", "unit", "entity")}})


async def _call(name, ctx, args, result, holder):
    """执行一次直调并 yield 公开事件；outcome 写入 holder（失败为 None）。"""
    started = time.monotonic()
    result["calls"] += 1
    personal = tools_base.get_tool(name) is None
    yield {"event": "tool", "tool": name, "state": "start", "personal": personal}
    try:
        async with asyncio.timeout(25):
            outcome = await invoke_tool(name, ctx, args, routed_by="direct_contract")
    except TimeoutError:
        outcome = ToolResult(name, False, error="tool_timeout")
    failed = not outcome.success
    extra = outcome.extra or {}
    record = {"event": "tool", "tool": name, "state": "end",
              "summary": "工具执行结束，结果已返回" if not failed else "工具失败或被阻止，未取得有效结果",
              "failed": failed, "personal": personal, "elapsed_ms": round((time.monotonic() - started) * 1000),
              "execution_status": extra.get("execution_status", "completed" if not failed else "failed"),
              "validation_status": extra.get("validation_status", "passed" if not failed else "failed"),
              "requirement_status": extra.get("requirement_status", "unverified" if not failed else "unmet")}
    result["records"].append(record)
    result["tool_results"].append({"args": args, "result": outcome})
    if not failed:
        _collect_sources(name, outcome, result)
    yield record
    if failed:
        result.update(status="error", error=outcome.error or "tool_execution_failed")
        yield {"event": "error", "code": outcome.error or "tool_execution_failed",
               "stage": "tool_execution",
               "message": f"工具 {name} 执行失败（{outcome.error or 'unknown'}），未取得实时数据。"}
        holder["outcome"] = None
    else:
        holder["outcome"] = outcome


def _render(plan, place, outcome):
    """确定性答案模板：实体/单位/适用时刻/时区/来源署名，获取时间不冒充观测时间。"""
    extra = outcome.extra or {}
    out = extra.get("output", {})
    unit = out.get("unit", "°C")
    tz = out.get("timezone") or place.get("timezone") or ""
    if plan["data_kind"] == "live":
        desc = _WMO.get(out.get("weather_code"), f"天气代码 {out.get('weather_code')}")
        body = (f"{place['name']}当前天气：{desc}，气温 {out.get('temperature')}{unit}。\n"
                f"数据适用时刻：{out.get('valid_at')}（{tz}）。")
    else:
        lines = []
        for d in out.get("days", []):
            desc = _WMO.get(d.get("weather_code"), f"天气代码 {d.get('weather_code')}")
            line = f"{d.get('date')}：{desc}，{d.get('temp_min')}~{d.get('temp_max')}{unit}"
            if d.get("precipitation_probability") is not None:
                line += f"，降水概率 {d['precipitation_probability']}%"
            lines.append(line)
        body = f"{place['name']}未来 {len(lines)} 天天气预报（{tz}）：\n" + "\n".join(lines)
    src = (extra.get("sources") or [{}])[0]
    return (body + f"\n\n数据来源：{src.get('title') or 'Open-Meteo'}（{src.get('url') or ''}）；"
            f"获取时间：{extra.get('fetched_at') or ''}。天气为模型数据，不冒充现场观测。")


async def execute(plan, ctx, *, result):
    """执行直调计划：yield 公开事件（与 run_tool_loop 同形状），终态回填 result。"""
    result.update(answer="", status="running", calls=0, tool_results=[], tool_sources=[], records=[])
    obs.record_step("direct_tool_plan",
                    f"city={plan['city']} kind={plan['data_kind']} chain=geocode→{plan['weather_tool']}",
                    fail_trace=False)
    try:
        holder = {}
        async for ev in _call(plan["geocode_tool"], ctx, {"name": plan["city"]}, result, holder):
            yield ev
        geo = holder["outcome"]
        if geo is None:
            return
        candidates = (geo.extra or {}).get("output", {}).get("candidates", [])
        if not candidates:
            result.update(answer=f"未找到城市「{plan['city']}」的坐标信息，无法读取天气。请确认城市名称后重试。", status="ok")
            yield {"event": "token", "content": result["answer"]}
            return
        if len(candidates) > 1:
            # 城市歧义不猜测：列出候选请用户选择（缺参数不猜测原则）
            lines = [f"{i + 1}. {c['name']}" + (f"（{c['admin1']}，{c['country']}）"
                     if c.get("admin1") or c.get("country") else f"（{c.get('timezone') or ''}）")
                     for i, c in enumerate(candidates)]
            result.update(answer=f"「{plan['city']}」匹配到多个地点，请指明要查询哪一个：\n" + "\n".join(lines), status="ok")
            yield {"event": "token", "content": result["answer"]}
            return
        place = candidates[0]
        args = {"latitude": place["latitude"], "longitude": place["longitude"], "timezone": place["timezone"]}
        async for ev in _call(plan["weather_tool"], ctx, args, result, holder):
            yield ev
        weather = holder["outcome"]
        if weather is None:
            return
        answer = _render(plan, place, weather)
        result["answer"] = answer
        result["status"] = "ok" if (weather.extra or {}).get("requirement_status") == "satisfied" else "unverified"
        obs.record_step("direct_answer", "确定性模板渲染最终答案，未调用生成模型", fail_trace=False)
        # 最终回复 span 与模型路径同名（answer），观测界面统一关联真实最终回复
        obs.record_llm("answer", f"[direct_tool] {plan['city']} {plan['data_kind']}", answer, fail_trace=False)
        for i in range(0, len(answer), 24):
            yield {"event": "token", "content": answer[i:i + 24]}
            await asyncio.sleep(0)
    except asyncio.CancelledError:
        result.update(status="cancelled")
        raise