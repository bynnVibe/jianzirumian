"""
见字如面 - 需求契约（Requirement Contract）

对应《Agent 能力缺口补救与动态命令执行整改方案》§4.1 与图5「请求上下文与需求分析」。

在进入 fast / ReAct / Plan&Execute 之前，先为本轮请求建立一个结构化「需求契约」，
把散落在查询文本里的意图显式化为可校验字段：域、实体、时间、时区、所需字段、
数据类别、时效策略、来源策略、本轮允许的能力范围与预算。

设计要点（不变量）：
  - 服务端可信时钟：当前时间/「今天」的日期区间由服务端锚定，不由模型自行推算；
  - 缺参数不猜测：城市/地点等关键实体缺失时标记 missing_argument，绝不默认某个城市；
  - 契约只是「需求描述」，不授予任何执行权限，permitted_capabilities 由上层按
    「系统允许 ∩ 用户有权 ∩ 本轮已授权」求交后回填。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

# ---- 域信号词表（规则优先，规模增大后再引入语义召回）----
_WEATHER_RE = re.compile(r"天气|气温|温度|多少度|下雨|降雨|降雪|台风|雾霾|空气质量")
_TIME_RE = re.compile(r"几点|时间|日期|今天几号|现在.*号|星期几|周几")
_NEWS_RE = re.compile(r"新闻|资讯|头条|最新.*消息|报道")
# 城市/地点抽取：可选查询动词 + 可选时间词 + 地名 + 的 + 可选时间词 + 天气类词
_CITY_RE = re.compile(
    r"(?:查询|查一下|查|看看|看下|想知道|了解)?"
    r"(?:今天|今日|明天|现在|最新)?"
    r"([\u4e00-\u9fa5A-Za-z]{2,8}?)"
    r"(?:今天|今日|现在|最新)?(?:的)?(?:天气|气温|温度|空气质量)"
)

# 时效期望阈值（分钟）：实况类默认 30 分钟，可按供应商更新频率调整（§13.2）
FRESHNESS_LIVE_MINUTES = 30
# 新闻默认检索时间窗（小时）：无明确要求时采用可配置窗口
NEWS_WINDOW_HOURS = 72

# 时间/时效词：城市抽取时若把这类词误当作实体，需剔除（缺参数不猜测）
_TIME_WORDS = frozenset({
    "今天", "今日", "明天", "明日", "昨天", "昨日", "后天", "现在", "当前",
    "最新", "实时", "这两天", "这几天", "本周", "这周",
})


def _clean_entity(raw: str) -> str:
    """清洗城市/地点实体：剔除时间词与结构助词误匹配，避免把「今天」当成城市。

    返回空串表示未解析出有效实体（→ 上层标记 missing_argument，绝不默认某城市）。
    """
    e = (raw or "").strip()
    if not e or e in _TIME_WORDS or "的" in e:
        return ""
    # 去除可能粘连在尾部的时间词（如「梅州今天」→「梅州」）
    for tw in _TIME_WORDS:
        if e.endswith(tw) and len(e) > len(tw):
            e = e[: -len(tw)]
    e = e.strip()
    return "" if e in _TIME_WORDS else e


@dataclass
class RequirementContract:
    """本轮请求的需求契约（§4.1 字段表）。"""

    raw_query: str = ""
    domain: str = "knowledge"          # weather | time | news | knowledge | realtime_general
    entity: str = ""                    # 地点/主题/对象（未解析出为空）
    entity_resolved: bool = False       # 实体是否已明确（用于 missing_argument 判定）
    requested_time: str = ""            # now | today | tomorrow | range | unspecified
    date_from: str = ""                 # 「今天」解析出的明确日期区间（服务端时钟锚定）
    date_to: str = ""
    timezone: str = ""                  # 解释日期所需时区；无法确定则空（应追问）
    required_fields: List[str] = field(default_factory=list)
    data_kind: str = ""                 # live | forecast | history | news
    freshness_policy: dict = field(default_factory=dict)
    source_policy: dict = field(default_factory=dict)
    permitted_capabilities: List[str] = field(default_factory=list)
    budgets: dict = field(default_factory=dict)
    missing_arguments: List[str] = field(default_factory=list)
    server_now: str = ""                # 服务端可信时钟锚点

    def to_dict(self) -> dict:
        return {
            "raw_query": self.raw_query,
            "domain": self.domain,
            "entity": self.entity,
            "entity_resolved": self.entity_resolved,
            "requested_time": self.requested_time,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "timezone": self.timezone,
            "required_fields": list(self.required_fields),
            "data_kind": self.data_kind,
            "freshness_policy": dict(self.freshness_policy),
            "source_policy": dict(self.source_policy),
            "permitted_capabilities": list(self.permitted_capabilities),
            "budgets": dict(self.budgets),
            "missing_arguments": list(self.missing_arguments),
            "server_now": self.server_now,
        }


def _default_budgets() -> dict:
    """初始预算（§10.3 设计起点，可被服务端配置覆盖）。"""
    from app.config import settings
    return {
        "total_seconds": int(getattr(settings, "CAP_TOTAL_BUDGET_SECONDS", 120)),
        "attempt_seconds": int(getattr(settings, "CAP_ATTEMPT_BUDGET_SECONDS", 20)),
        "max_dynamic_attempts": int(getattr(settings, "CAP_MAX_DYNAMIC_ATTEMPTS", 3)),
        "max_actions": int(getattr(settings, "CAP_MAX_ACTIONS", 6)),
        "max_http_requests": int(getattr(settings, "CAP_MAX_HTTP_REQUESTS", 8)),
    }


def _classify_domain(query: str) -> str:
    q = query or ""
    if _TIME_RE.search(q) and not _WEATHER_RE.search(q):
        return "time"
    if _WEATHER_RE.search(q):
        return "weather"
    if _NEWS_RE.search(q):
        return "news"
    return "knowledge"


def _detect_requested_time(query: str, now: datetime) -> tuple[str, str, str]:
    """返回 (requested_time, date_from, date_to)；日期区间按服务端时钟锚定。"""
    q = query or ""
    today = now.date().isoformat()
    if re.search(r"此刻|现在几点|当前时间|现在时间", q):
        return "now", today, today
    if re.search(r"明天", q):
        d = (now + timedelta(days=1)).date().isoformat()
        return "tomorrow", d, d
    if re.search(r"今天|今日", q):
        return "today", today, today
    if re.search(r"最新|实时|当前", q):
        return "now", today, today
    return "unspecified", "", ""


def build_contract(
    query: str,
    *,
    timezone: str = "",
    permitted_capabilities: Optional[List[str]] = None,
    allow_external: bool = False,
    now: Optional[datetime] = None,
) -> RequirementContract:
    """从用户问题构建需求契约（规则优先；含糊问题可由上层再让模型辅助补全）。

    Args:
        query: 用户原始问题
        timezone: 客户端/用户设置的时区提示（不作为真实当前时间的唯一权威）
        permitted_capabilities: 上层已求交的本轮允许能力（检索/外部查询/动态执行）
        allow_external: 本轮是否已授权外部查询（影响 source_policy 与缺口判定）
        now: 注入的服务端可信时钟（缺省取当前时间）
    """
    now = now or datetime.now()
    q = (query or "").strip()
    c = RequirementContract(raw_query=q, server_now=now.isoformat(), timezone=timezone or "")
    c.domain = _classify_domain(q)
    c.requested_time, c.date_from, c.date_to = _detect_requested_time(q, now)
    c.budgets = _default_budgets()
    c.permitted_capabilities = list(permitted_capabilities or [])
    c.source_policy = {"allow_external": bool(allow_external)}

    if c.domain == "weather":
        m = _CITY_RE.search(q)
        if m:
            c.entity = _clean_entity(m.group(1))
            c.entity_resolved = bool(c.entity)
        # 关键实体缺失 → missing_argument，绝不默认某个城市（§4.2 / P0）
        if not c.entity_resolved:
            c.missing_arguments.append("city")
        c.data_kind = "forecast" if c.requested_time in ("today", "tomorrow") else "live"
        c.required_fields = ["当前温度", "天气状况"] if c.data_kind == "live" \
            else ["今日最高温", "今日最低温", "天气状况"]
        c.freshness_policy = {
            "live_max_age_minutes": FRESHNESS_LIVE_MINUTES,
            "forecast": "校验发布时间与预报覆盖区间",
        }
    elif c.domain == "time":
        # 「现在几点」直接调用时间工具，不搜索、不生成系统命令（§4.1）
        c.data_kind = "live"
        c.required_fields = ["当前时间", "时区"]
        c.entity_resolved = True
        if not c.timezone:
            c.missing_arguments.append("timezone")
        c.freshness_policy = {"source": "server_clock", "note": "服务端时钟＋指定时区"}
    elif c.domain == "news":
        c.data_kind = "news"
        c.required_fields = ["标题", "原始发布时间"]
        c.entity = q
        c.entity_resolved = True
        c.freshness_policy = {"window_hours": NEWS_WINDOW_HOURS,
                              "note": "区分原始发布时间与转载时间；缺原始时间标记不确定"}
    else:
        c.data_kind = "knowledge"
        c.entity = q
        c.entity_resolved = True

    return c
