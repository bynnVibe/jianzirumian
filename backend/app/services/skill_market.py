"""
见字如面 - 技能市场相似 Skill 搜索聚合服务

为「沉淀为 Skill」编辑界面提供从主流技能市场搜寻类似功能 Skill 的能力：
并发查询多个市场（Skills.sh / ClawHub / SkillsMP / SkillHub 等），归一化为统一卡片
（名称/描述/指令内容/来源/热度/链接），供前端展示并一键应用到沉淀表单。

设计原则：
- 每个市场适配器独立、best-effort：单个市场超时/被墙/解析失败不影响其它市场；
- 优先保证可用源（Skills.sh 可回源 GitHub 拉取真实 SKILL.md 指令内容）；
- 整体并发 + 短超时，避免阻塞编辑界面；结果去重、按热度排序、截断。
"""
import asyncio
import logging
import re

import httpx

logger = logging.getLogger("jianziruyang.skill_market")

_UA = {"User-Agent": "Mozilla/5.0 (jianziruyang-skill-market)", "Accept": "application/json"}

# 注册的市场来源（id → 展示名）。适配器按 id 分发；未实现/不可达的来源自动跳过。
MARKET_SOURCES = [
    {"id": "skills.sh", "name": "Skills.sh", "url": "https://skills.sh"},
    {"id": "clawhub", "name": "ClawHub", "url": "https://clawhub.ai"},
    {"id": "skillsmp", "name": "SkillsMP", "url": "https://skillsmp.com"},
    {"id": "agentskillshub", "name": "AgentSkillsHub", "url": "https://agentskillshub.top"},
    {"id": "skillhub", "name": "SkillHub.club", "url": "https://skillhub.club"},
    {"id": "skillatlas", "name": "Skill Atlas", "url": "https://ai.skillatlas.cn"},
    {"id": "skilldock", "name": "SkillDock.io", "url": "https://skilldock.io"},
]
_SOURCE_NAME = {s["id"]: s["name"] for s in MARKET_SOURCES}


def _parse_skill_md(text: str) -> tuple[str, str]:
    """解析 SKILL.md：返回 (description, body 指令内容)。容忍无 frontmatter。"""
    desc = ""
    body = text or ""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text or "", re.S)
    if m:
        fm = m.group(1)
        body = (text or "")[m.end():]
        dm = re.search(r"^description:\s*(.*)$", fm, re.M)
        if dm:
            desc = dm.group(1).strip().strip('"').strip("'")
            # YAML 块标量（>- / | 等）：拼接后续缩进行
            if desc in (">-", ">", "|", "|-", "+"):
                lines = []
                for ln in fm[dm.end():].splitlines():
                    if ln.startswith((" ", "\t")) and ln.strip():
                        lines.append(ln.strip())
                    elif ln.strip():
                        break
                desc = " ".join(lines)
    return desc, body.strip()


async def _github_skill_md(client: httpx.AsyncClient, source: str, skill_id: str) -> str:
    """回源 GitHub 拉取 SKILL.md（尝试常见分支/路径），失败返回空串。"""
    candidates = [
        f"https://raw.githubusercontent.com/{source}/HEAD/skills/{skill_id}/SKILL.md",
        f"https://raw.githubusercontent.com/{source}/main/skills/{skill_id}/SKILL.md",
        f"https://raw.githubusercontent.com/{source}/HEAD/{skill_id}/SKILL.md",
        f"https://raw.githubusercontent.com/{source}/main/{skill_id}/SKILL.md",
    ]
    for u in candidates:
        try:
            r = await client.get(u)
            if r.status_code == 200 and r.text.strip():
                return r.text
        except Exception:
            continue
    return ""


async def _search_skills_sh(client: httpx.AsyncClient, query: str, limit: int) -> list[dict]:
    r = await client.get("https://skills.sh/api/search", params={"q": query})
    r.raise_for_status()
    data = r.json()
    out = []
    for s in (data.get("skills") or [])[:limit]:
        source = s.get("source") or ""
        skill_id = s.get("skillId") or s.get("name") or ""
        out.append({
            "id": f"skills.sh:{s.get('id') or skill_id}",
            "source": "skills.sh",
            "name": s.get("name") or skill_id,
            "description": "",
            "content": "",
            "url": f"https://skills.sh/{s.get('id') or ''}",
            "heat": int(s.get("installs") or 0),
            "_gh": (source, skill_id),
        })
    # 并发回源补全描述/指令内容（仅前几条，控制时延）
    async def fill(item):
        src, sid = item.pop("_gh")
        if not src or not sid:
            return
        md = await _github_skill_md(client, src, sid)
        if md:
            desc, body = _parse_skill_md(md)
            item["description"] = desc
            item["content"] = body[:4000]
    await asyncio.gather(*(fill(i) for i in out[:4]))
    return out


async def _search_clawhub(client: httpx.AsyncClient, query: str, limit: int) -> list[dict]:
    r = await client.get("https://clawhub.ai/api/v1/search", params={"q": query})
    r.raise_for_status()
    data = r.json()
    out = []
    for s in (data.get("results") or [])[:limit]:
        native = s.get("native") or {}
        summary = native.get("summary") or native.get("description") or ""
        out.append({
            "id": f"clawhub:{s.get('id') or s.get('canonicalUrl')}",
            "source": "clawhub",
            "name": s.get("displayName") or (s.get("canonicalUrl") or "").split("/")[-1],
            "description": summary,
            "content": "",
            "url": f"https://clawhub.ai{s.get('canonicalUrl') or ''}",
            "heat": int(s.get("downloads") or 0),
        })
    return out


async def _search_skillsmp(client: httpx.AsyncClient, query: str, limit: int) -> list[dict]:
    # SkillsMP 语义搜索（常被 Cloudflare 拦截，best-effort）
    r = await client.get("https://skillsmp.com/api/skills/search", params={"q": query})
    r.raise_for_status()
    data = r.json()
    items = data.get("skills") or data.get("results") or []
    return [{
        "id": f"skillsmp:{s.get('id') or s.get('name')}",
        "source": "skillsmp",
        "name": s.get("name") or "",
        "description": s.get("description") or "",
        "content": s.get("content") or "",
        "url": s.get("url") or "https://skillsmp.com",
        "heat": int(s.get("stars") or s.get("installs") or 0),
    } for s in items[:limit]]


_ADAPTERS = {
    "skills.sh": _search_skills_sh,
    "clawhub": _search_clawhub,
    "skillsmp": _search_skillsmp,
}


async def search_market(query: str, limit: int = 6) -> dict:
    """并发搜索各市场，归一化卡片列表。单源失败不影响整体。"""
    q = (query or "").strip()
    if not q:
        return {"query": q, "results": [], "sources": [], "errors": ["查询为空"]}
    results: list[dict] = []
    errors: list[str] = []
    tried: list[str] = []

    async with httpx.AsyncClient(timeout=6, follow_redirects=True, headers=_UA) as client:
        async def run(sid):
            adapter = _ADAPTERS.get(sid)
            if not adapter:
                return
            tried.append(sid)
            try:
                items = await asyncio.wait_for(adapter(client, q, limit), timeout=7)
                results.extend(items)
            except Exception as e:
                errors.append(f"{_SOURCE_NAME.get(sid, sid)}: {type(e).__name__}")
                logger.debug("市场 %s 搜索失败: %s", sid, e)
        await asyncio.gather(*(run(s["id"]) for s in MARKET_SOURCES))

    # 去重（同来源同名）+ 按热度降序 + 截断
    seen = set()
    uniq = []
    for it in sorted(results, key=lambda x: x.get("heat", 0), reverse=True):
        key = (it["source"], (it.get("name") or "").lower())
        if key in seen or not it.get("name"):
            continue
        seen.add(key)
        it["source_name"] = _SOURCE_NAME.get(it["source"], it["source"])
        uniq.append(it)
    return {"query": q, "results": uniq[:limit], "sources": tried, "errors": errors}


class SkillMarketService:
    """技能市场搜索服务单例（与 skill_service 等保持一致的注入方式）。"""

    async def search(self, query: str, limit: int = 6) -> dict:
        return await search_market(query, limit)


skill_market = SkillMarketService()