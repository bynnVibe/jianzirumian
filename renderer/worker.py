"""无网络浏览器工作进程：仅接收本机 Unix socket 控制，网页资源由 Agent 提供。"""
import asyncio
import base64
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web, WSMsgType
from playwright.async_api import async_playwright

SERIAL = asyncio.Semaphore(1)
SOCKET = os.getenv("URL_RENDERER_SOCKET", "/ipc/renderer.sock")


async def render(ws, command, pending):
    task_id = command.get("task_id")
    resource_slots = asyncio.Semaphore(4)
    async with SERIAL:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, chromium_sandbox=True,
                args=["--disable-background-networking", "--disable-component-update", "--disable-sync"])
            try:
                context = await browser.new_context(service_workers="block", accept_downloads=False,
                                                    viewport={"width": 1280, "height": 900})
                page = await context.new_page()
                async def route_resource(route):
                    request = route.request
                    if request.resource_type not in {"document", "script", "stylesheet", "xhr", "fetch"} or request.method not in {"GET", "HEAD"}:
                        await route.abort()
                        return
                    if urlsplit(request.url).scheme not in {"http", "https"}:
                        await route.abort()
                        return
                    # 禁止页面自行开新的窗口或子文档。
                    if request.resource_type == "document" and request.frame != page.main_frame:
                        await route.abort()
                        return
                    async with resource_slots:
                        request_id = uuid.uuid4().hex
                        future = asyncio.get_running_loop().create_future()
                        pending[request_id] = future
                        try:
                            await ws.send_json({"type": "fetch", "task_id": task_id, "request_id": request_id,
                                                "url": request.url, "method": request.method, "resource_type": request.resource_type})
                            result = await asyncio.wait_for(future, 16)
                            if result.get("error"):
                                await route.abort()
                            else:
                                body = base64.b64decode(result.get("body", ""), validate=True)
                                if len(body) > 5 * 1024 * 1024:
                                    raise ValueError("资源过大")
                                headers = {"Content-Type": result.get("content_type", "text/plain")}
                                if result.get("location"):
                                    headers["Location"] = result["location"]
                                await route.fulfill(status=result.get("status", 200), headers=headers, body=body)
                        except (asyncio.TimeoutError, ValueError):
                            await route.abort()
                        finally:
                            pending.pop(request_id, None)
                await context.route("**/*", route_resource)
                await context.route_web_socket("**/*", lambda socket: socket.close())
                async def close_popup(popup):
                    if popup != page:
                        await popup.close()
                context.on("page", close_popup)
                page.on("download", lambda download: download.cancel())
                async with asyncio.timeout(min(45, max(1, float(command.get("timeout", 40))))):
                    await page.goto(command["url"], wait_until="domcontentloaded", timeout=35000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass
                    html = await page.content()
                    if len(html.encode("utf-8")) > 5 * 1024 * 1024:
                        raise ValueError("正文过大")
                    await ws.send_json({"type": "result", "task_id": task_id, "html": html, "url": page.url})
            finally:
                await browser.close()


async def handle(request):
    ws = web.WebSocketResponse(max_msg_size=8 * 1024 * 1024, heartbeat=20)
    await ws.prepare(request)
    task, task_id, pending = None, None, {}
    async def run(command):
        try:
            await render(ws, command, pending)
        except Exception:
            if not ws.closed:
                await ws.send_json({"type": "error", "task_id": command["task_id"]})
    try:
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                break
            data = message.json()
            if not isinstance(data, dict):
                break
            if data.get("type") == "render" and task is None:
                task_id = data.get("task_id")
                if not isinstance(task_id, str) or len(task_id) > 80 or urlsplit(data.get("url", "")).scheme not in {"http", "https"}:
                    break
                task = asyncio.create_task(run(data))
            elif data.get("type") == "resource" and data.get("task_id") == task_id:
                future = pending.get(data.get("request_id"))
                if future and not future.done():
                    future.set_result(data)
            else:
                break
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for future in pending.values():
            future.cancel()
    return ws


if __name__ == "__main__":
    os.umask(0o007)
    path = Path(SOCKET)
    path.parent.mkdir(mode=0o770, parents=True, exist_ok=True)
    if path.is_socket():
        path.unlink()
    app = web.Application(client_max_size=8 * 1024 * 1024)
    app.router.add_get("/render", handle)
    web.run_app(app, path=SOCKET, access_log=None)
