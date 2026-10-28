"""请求级总预算：每段异步等待共享同一 monotonic deadline。"""
import asyncio
import time

import anyio
from starlette.responses import StreamingResponse


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("请求总预算已耗尽")
    return value


async def iterate(iterator, deadline):
    """在当前任务内迭代，保留 contextvars，超时关闭下游生成器。"""
    try:
        while True:
            async with asyncio.timeout(remaining(deadline)):
                try:
                    item = await anext(iterator)
                except StopAsyncIteration:
                    return
            yield item
    finally:
        await close_resources(iterator)


async def close_resources(*resources):
    """断线取消期间仍给网络资源有界的关闭机会。"""
    with anyio.CancelScope(shield=True):
        for resource in resources:
            if resource is not None and hasattr(resource, "aclose"):
                try:
                    async with asyncio.timeout(5):
                        await resource.aclose()
                except (Exception, asyncio.CancelledError):
                    pass


class ClosingStreamingResponse(StreamingResponse):
    """包括 ASGI send 抛出断线异常的路径也显式关闭响应生成器。"""
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await close_resources(self.body_iterator)


async def until_disconnect(request, operation):
    """非流式模型调用也监听断开，将取消传到上游并等待终态记录。"""
    task = asyncio.create_task(operation)

    async def monitor():
        while not task.done():
            if await request.is_disconnected():
                task.cancel()
                return
            await asyncio.sleep(0.05)

    watcher = asyncio.create_task(monitor())
    try:
        return await task
    finally:
        watcher.cancel()
        task.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(task, watcher, return_exceptions=True)
