"""
见字如面 - 场景感知工具路由（Tool Routing）

根据用户问题与可用工具集，决定「主来源工具」与「补充工具」，从路由层面修复
「实时查询（如『查询今天深圳的天气』）被知识库优先逻辑淹没」导致的答非所问（图4）。

根因回顾：天气这类实时问题走 fast 流水线时，知识库（健康养生域）必然无命中，
旧逻辑却先注入「知识库未收录，请勿编造」，再把联网搜索结果降级为「仅供参考、
优先以知识库为准」，两条矛盾指令下模型忽略真实搜索结果 → 答非所问。

路由信号：
  - 时效性检测：问题含「今天/最新/天气/气温/新闻/股价/比分…」等实时信号
    → 判定 realtime 场景，主来源指向 web_search（而非健康养生知识库）。
  - 健康域检测：问题含「健康/养生/中医/穴位/功效…」→ 主来源 search_knowledge。
  - 注册表场景匹配：用工具 scenarios/tags（base.find_by_scenario）把关键词映射到工具，
    使路由决策与标准化工具注册表（req1）同源，新增工具即自动纳入路由候选。

输出 RouteDecision 供 chat.py / chat_agent 构造正确的回答提示词：
  - primary_tool：主来源工具（回答的主要依据）
  - is_realtime：是否实时查询（决定联网搜索结果是否作为主依据而非「仅供参考」）
  - search_primary：联网搜索结果是否应作为主来源（KB 无命中或实时查询时为 True）
"""
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

from app.services.tools import base as tools_base

logger = logging.getLogger("jianziruyang.tools.router")

# 时效性/实时信号词：命中即倾向把 web_search 作为主来源
_REALTIME_HINTS = (
    "今天", "今日", "现在", "此刻", "最新", "最近", "实时", "当前", "本周", "本月", "今年",
    "天气", "气温", "温度", "多少度", "下雨", "降雨", "降雪", "台风", "雾霾", "空气",
    "新闻", "热点", "股价", "股票", "基金", "汇率", "比分", "赛果", "赛程", "油价",
    "发布", "更新时间", "几点", "日期", "星期几", "限行",
)
# 健康域信号词：命中即倾向把 search_knowledge 作为主来源
_HEALTH_HINTS = (
    "健康", "养生", "中医", "穴位", "经络", "食材", "功效", "调理", "食疗", "中药", "按摩",
    "失眠", "血压", "血糖", "免疫", "营养", "膳食", "节气", "体质", "症状", "治疗", "预防",
    "红枣", "枸杞", "人参", "黄芪", "上火", "湿气", "补气", "补血", "阴阳", "五脏",
)
# 实时信号的正则（时效词 + 天气类组合，如「深圳天气」「今天气温」）
_REALTIME_RE = re.compile("|".join(re.escape(h) for h in _REALTIME_HINTS))
_HEALTH_RE = re.compile("|".join(re.escape(h) for h in _HEALTH_HINTS))


@dataclass
class RouteDecision:
    """一次工具路由决策。"""

    primary_tool: str = ""                 # 主来源工具名
    secondary_tools: List[str] = field(default_factory=list)
    is_realtime: bool = False              # 是否实时/时效性查询
    search_primary: bool = False           # 联网搜索结果是否应作为主依据
    scenario: str = ""                     # 命中的场景标签（realtime/health/general）
    reason: str = ""                       # 路由理由（供可观测性/调试）

    def to_dict(self) -> dict:
        return {
            "primary_tool": self.primary_tool,
            "secondary_tools": list(self.secondary_tools),
            "is_realtime": self.is_realtime,
            "search_primary": self.search_primary,
            "scenario": self.scenario,
            "reason": self.reason,
        }


def detect_realtime(query: str) -> bool:
    """检测问题是否为实时/时效性查询（天气、新闻、股价、今日之事等）。"""
    q = (query or "").strip()
    if not q:
        return False
    return bool(_REALTIME_RE.search(q))


def detect_health(query: str) -> bool:
    """检测问题是否属于健康养生域（知识库主场景）。"""
    q = (query or "").strip()
    if not q:
        return False
    return bool(_HEALTH_RE.search(q))


def route_tools(
    query: str,
    *,
    use_knowledge: bool = True,
    use_search: bool = False,
    allowed_tools: Optional[List[str]] = None,
) -> RouteDecision:
    """场景感知路由：决定主来源工具与联网搜索是否作为主依据。

    Args:
        query: 用户问题
        use_knowledge: 是否启用知识库检索
        use_search: 是否启用联网搜索
        allowed_tools: 允许的工具名集合（缺省=注册表全部 active 检索工具）

    Returns:
        RouteDecision。决策优先级：
          1. 实时查询 + 可用 web_search → primary=web_search, is_realtime=True, search_primary=True
          2. 健康域 + 可用 search_knowledge → primary=search_knowledge
          3. 仅知识库可用 → primary=search_knowledge
          4. 仅联网可用 → primary=web_search, search_primary=True
          5. 都不可用 → 直接作答（primary 空）
    """
    q = (query or "").strip()
    allowed = set(allowed_tools) if allowed_tools is not None else {
        t.name for t in tools_base.active_tools(namespaces=("retrieval",))
    }
    can_kb = use_knowledge and "search_knowledge" in allowed
    can_web = use_search and "web_search" in allowed

    is_realtime = detect_realtime(q)
    is_health = detect_health(q)

    # 1. 实时查询且可联网 → 联网搜索为主来源（修复天气答非所问的核心路由）
    if is_realtime and can_web:
        secondary = ["search_knowledge"] if can_kb else []
        return RouteDecision(
            primary_tool="web_search", secondary_tools=secondary,
            is_realtime=True, search_primary=True, scenario="realtime",
            reason="检测到实时/时效性查询信号，主来源指向联网搜索",
        )
    # 2. 健康域且可知识检索 → 知识库为主来源
    if is_health and can_kb:
        secondary = ["web_search"] if can_web else []
        return RouteDecision(
            primary_tool="search_knowledge", secondary_tools=secondary,
            is_realtime=False, search_primary=False, scenario="health",
            reason="检测到健康养生域信号，主来源指向知识库检索",
        )
    # 3. 仅知识库可用（或默认）→ 知识库为主，联网为补充
    if can_kb:
        secondary = ["web_search"] if can_web else []
        return RouteDecision(
            primary_tool="search_knowledge", secondary_tools=secondary,
            is_realtime=is_realtime, search_primary=False, scenario="general",
            reason="默认以知识库检索为主来源",
        )
    # 4. 仅联网可用 → 联网为主
    if can_web:
        return RouteDecision(
            primary_tool="web_search", secondary_tools=[],
            is_realtime=is_realtime, search_primary=True, scenario="general",
            reason="仅联网搜索可用，以其为主来源",
        )
    # 5. 都不可用 → 直接作答
    return RouteDecision(
        primary_tool="", secondary_tools=[],
        is_realtime=is_realtime, search_primary=False, scenario="none",
        reason="无可用检索工具，直接依据模型自身知识作答",
    )
