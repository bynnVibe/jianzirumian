"""通过 Unix socket 驱动无网络 renderer；所有网页资源仍经过安全抓取器。"""
import asyncio
import base64
import time

import aiohttp

from app.config import settings
from app.services.url_fetch import UrlImportError, normalize_url

ALLOWED_RESOURCES = {"document", "script", "stylesheet", "xhr", "fetch"}


async def render_url(task_id, url, fetcher):
    if not settings.URL_RENDERER_ENABLED:
        raise UrlImportError("renderer_unavailable", "渲染服务未启用；请启动隔离 renderer 或手动补充正文")
    seen, redirects = set(), 0
    jobs = set()
    async def respond(ws, request):
        nonlocal redirects
        response = {"type": "resource", "task_id": task_id, "request_id": request["request_id"]}
        try:
            if request.get("resource_type") not in ALLOWED_RESOURCES:
                raise UrlImportError("resource", "不支持的网页资源")
            result = await fetcher.fetch(request.get("url"), resource=request["resource_type"] != "document",
                                         follow_redirects=False, method=request.get("method", "GET"))
            if result.location:
                redirects += 1
                if redirects > 5:
                    raise UrlImportError("redirect", "网页重定向次数过多")
            response.update(status=result.status, content_type=result.content_type,
                            body=base64.b64encode(result.body).decode("ascii"), location=result.location)
        except UrlImportError as exc:
            response.update(error=exc.code)
        if not ws.closed:
            await ws.send_json(response)
    try:
        remaining = max(.01, fetcher.budget.deadline - time.monotonic())
        async with asyncio.timeout(remaining):
            connector = aiohttp.UnixConnector(path=settings.URL_RENDERER_SOCKET)
            async with aiohttp.ClientSession(connector=connector, trust_env=False) as client:
                async with client.ws_connect("http://renderer/render", max_msg_size=8 * 1024 * 1024) as ws:
                    await ws.send_json({"type": "render", "task_id": task_id, "url": normalize_url(url), "timeout": min(45, remaining)})
                    async for message in ws:
                        fetcher.budget.check()
                        if message.type != aiohttp.WSMsgType.TEXT:
                            break
                        data = message.json()
                        if not isinstance(data, dict) or data.get("task_id") != task_id:
                            raise UrlImportError("renderer_protocol", "渲染服务返回了无效任务")
                        if data.get("type") == "fetch":
                            request_id = data.get("request_id")
                            if not isinstance(request_id, str) or len(request_id) > 80 or request_id in seen or len(seen) >= 100 or len(jobs) >= 8:
                                raise UrlImportError("renderer_protocol", "渲染请求超过限制")
                            seen.add(request_id)
                            task = asyncio.create_task(respond(ws, data))
                            jobs.add(task)
                            task.add_done_callback(jobs.discard)
                        elif data.get("type") == "result":
                            html = data.get("html")
                            if not isinstance(html, str) or len(html.encode("utf-8")) > 5 * 1024 * 1024:
                                raise UrlImportError("too_large", "渲染正文超出大小限制")
                            return html, normalize_url(data.get("url"))
                        else:
                            raise UrlImportError("render_failed", "浏览器未能解析正文，站点可能要求验证或不支持当前访问方式")
    except UrlImportError:
        raise
    except (OSError, aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        raise UrlImportError("renderer_unavailable", "渲染服务不可用或超时；请检查隔离容器，或手动补充正文") from None
    finally:
        for task in list(jobs):
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
    raise UrlImportError("render_failed", "渲染连接已中断，请重试或手动补充正文")
