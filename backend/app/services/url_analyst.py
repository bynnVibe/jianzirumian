"""urlAnalystAgent：公开链接获取、站点 Skill 提取及隔离渲染的确定性编排。"""
import asyncio
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from app.core import observability as obs
from app.services.url_fetch import SafeFetcher, UrlImportError, normalize_url
from app.services.url_renderer import render_url
from app.services.url_skills import extract_article


class UrlAnalystAgent:
    async def run(self, task_id, user_id, url):
        url = normalize_url(url)
        obs.start_trace("urlAnalystAgent", user_id, urlsplit(url).hostname or "")
        fetcher = SafeFetcher()
        status, error = "ok", ""
        try:
            async with asyncio.timeout(60):
                method = "http"
                t0 = time.monotonic()
                try:
                    response = await fetcher.fetch(url)
                    url = response.url
                    obs.record_tool("safe_http_fetch", {}, {"bytes": len(response.body)}, t0=t0)
                    article = await asyncio.to_thread(extract_article, response.text(), url)
                except UrlImportError as exc:
                    # 安全拒绝、访问控制、过大文档等不通过浏览器重试。
                    if exc.code not in {"empty_article", "fetch_failed"}:
                        raise
                    obs.record_step("http_fallback", exc.code, t0=t0)
                    method = "browser"
                    t0 = time.monotonic()
                    html, url = await render_url(task_id, url, fetcher)
                    obs.record_tool("isolated_browser_render", {}, {"bytes": len(html.encode())}, t0=t0)
                    article = await asyncio.to_thread(extract_article, html, url)
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
