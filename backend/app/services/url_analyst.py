"""urlAnalystAgent：公开链接获取、站点 Skill 提取及隔离渲染的确定性编排。"""
import asyncio
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from app.core import observability as obs
from app.config import settings
from app.services.skill import skill_service
from app.services.url_fetch import SafeFetcher, UrlImportError, normalize_url, MOBILE_UA
from app.services.url_renderer import render_url
from app.services.url_skills import extract_article


class UrlAnalystAgent:
    async def run(self, task_id, user_id, url):
        url = normalize_url(url)
        host = urlsplit(url).hostname or ""
        # 站点抓取策略（由已启用的 url_import playbook 技能驱动）：移动端 UA / 渲染优先。
        strategy = skill_service.url_strategy(user_id, host)
        ua = MOBILE_UA if strategy.get("mobile_ua") else None
        prefer_render = bool(strategy.get("prefer_render")) and settings.URL_RENDERER_ENABLED
        obs.start_trace("urlAnalystAgent", user_id, host)
        if strategy.get("notes"):
            obs.record_step("url_skill_strategy", "; ".join(strategy["notes"])[:200])
        fetcher = SafeFetcher()
        status, error = "ok", ""
        try:
            async with asyncio.timeout(60):
                method = "http"
                t0 = time.monotonic()
                response = None
                article = None
                # 渲染优先（如 CSDN 正文延迟加载/含反爬）：先试隔离渲染，失败再走直接抓取链。
                if prefer_render:
                    try:
                        method = "browser"
                        t0 = time.monotonic()
                        html, rendered_url = await render_url(task_id, url, fetcher)
                        obs.record_tool("isolated_browser_render", {}, {"bytes": len(html.encode())}, t0=t0)
                        article = await asyncio.to_thread(extract_article, html, rendered_url)
                        url = rendered_url
                    except UrlImportError as rex:
                        obs.record_step("render_first_failed", rex.code)
                        article = None
                        method = "http"
                        t0 = time.monotonic()
                if article is None:
                    try:
                        response = await fetcher.fetch(url, user_agent=ua)
                        url = response.url
                        obs.record_tool("safe_http_fetch", {}, {"bytes": len(response.body)}, t0=t0)
                        article = await asyncio.to_thread(extract_article, response.text(), url)
                        method = "http"
                    except UrlImportError as exc:
                        # 安全拒绝、访问控制、过大文档等不通过浏览器重试。
                        if exc.code not in {"empty_article", "fetch_failed", "tls_failed"}:
                            raise
                        obs.record_step("http_fallback", exc.code, t0=t0)
                        rendered = False
                        if settings.URL_RENDERER_ENABLED:
                            try:
                                method = "browser"
                                t0 = time.monotonic()
                                html, url = await render_url(task_id, url, fetcher)
                                obs.record_tool("isolated_browser_render", {}, {"bytes": len(html.encode())}, t0=t0)
                                article = await asyncio.to_thread(extract_article, html, url)
                                rendered = True
                            except UrlImportError as rex:
                                # 渲染不可用/失败：尚有直接抓取结果时可降级，否则原样报错
                                if rex.code not in {"renderer_unavailable", "renderer_protocol", "render_failed"} \
                                        or response is None:
                                    raise
                                obs.record_step("render_fallback_failed", rex.code)
                        if not rendered:
                            if response is None:
                                raise
                            # 渲染服务未启用/不可用：降级为宽松直接提取，避免整单解析失败
                            method = "http_relaxed"
                            t0 = time.monotonic()
                            try:
                                article = await asyncio.to_thread(extract_article, response.text(), url, True)
                            except UrlImportError:
                                hint = "渲染服务未启用，" if not settings.URL_RENDERER_ENABLED else "渲染降级后仍"
                                raise UrlImportError("empty_article",
                                                     f"未提取到足够正文；{hint}可启动隔离 renderer 或手动补充正文") from None
                            obs.record_step("relaxed_direct_extract", exc.code, t0=t0)
                fetcher.budget.check()
                obs.record_step("article_skill", f'{article["skill"]}@{article["skill_version"]}')
                return {**article, "final_url": url, "extraction_method": method,
                        "fetched_at": datetime.now(timezone.utc).isoformat(), "content_origin": "fetched"}
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except asyncio.TimeoutError:
            status, error = "error", "timeout"
            raise UrlImportError("timeout", "链接解析超时，请重试或手动补充正文") from None
        except Exception as exc:
            status, error = "error", getattr(exc, "code", "parse_failed")
            raise
        finally:
            fetcher.budget.cancelled = True
            obs.end_trace(status, error)


urlAnalystAgent = UrlAnalystAgent()
