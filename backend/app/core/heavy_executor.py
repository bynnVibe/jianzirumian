"""
后台重活专用线程池：与 asyncio 默认线程池隔离

上传流水线的解析（PyMuPDF/python-docx/LibreOffice）、向量化入库、wiki 落库等
都是长时间占用 CPU/GIL 的重活。若与交互请求共用 asyncio 默认线程池
（asyncio.to_thread），小核数 ECS 上默认池仅 min(32, cpu+4) 个工作线程，
几个后台任务即可占满线程池并长时间持有 GIL，导致其他账号的普通 API
请求排队、事件循环被饿死，表现为「一人上传、全站变卡」。

约定：
- 后台流水线重活统一走 run_in_heavy_pool()（独立小池，忙时排队不扩散）；
- 交互请求路径的短时同步活仍走 asyncio.to_thread（默认池，启动时显式限界）；
- 两池隔离后，后台任务再多也只影响自身队列，不侵占交互请求的线程资源。
"""
import asyncio
import contextvars
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

logger = logging.getLogger("jianziruyang.heavy")


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


# 重活池上限默认随核数收敛（2 核 ECS → 2），显式 HEAVY_WORKERS 可覆盖
_DEFAULT_HEAVY_WORKERS = _clamp((os.cpu_count() or 2), 1, 4)
HEAVY_WORKERS = _clamp(int(os.getenv("HEAVY_WORKERS", _DEFAULT_HEAVY_WORKERS)), 1, 16)

_EXECUTOR: Optional[ThreadPoolExecutor] = None
_INTERACTIVE_EXECUTOR: Optional[ThreadPoolExecutor] = None

# 交互默认池上限：专供请求路径的短时同步活（检索/可观测聚合等 to_thread），
# 给足线程避免突发并发请求排队；上限收敛防止线程爆炸
_DEFAULT_INTERACTIVE_WORKERS = _clamp((os.cpu_count() or 2) * 2 + 4, 8, 32)
INTERACTIVE_WORKERS = _clamp(
    int(os.getenv("INTERACTIVE_WORKERS", _DEFAULT_INTERACTIVE_WORKERS)), 4, 64
)


def get_heavy_executor() -> ThreadPoolExecutor:
    """重活池单例（懒初始化；线程常驻，避免解析任务间歇期反复建线程）"""
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = ThreadPoolExecutor(
            max_workers=HEAVY_WORKERS, thread_name_prefix="heavy"
        )
        logger.info("后台重活线程池初始化: workers=%d（与交互默认池隔离）", HEAVY_WORKERS)
    return _EXECUTOR


async def run_in_heavy_pool(fn: Callable, /, *args, **kwargs):
    """在重活池执行同步函数（替代后台任务里的 asyncio.to_thread）。

    显式复制调用方 contextvars（run_in_executor 不会自动传播），
    使后台流水线内的 embedding/OCR/摘要等出网调用继承触发用户的
    CallContext 归属，不计入系统内部调用。
    """
    loop = asyncio.get_running_loop()
    if kwargs:
        from functools import partial

        fn = partial(fn, **kwargs)
    ctx = contextvars.copy_context()
    return await loop.run_in_executor(get_heavy_executor(), ctx.run, fn, *args)


def get_interactive_executor() -> ThreadPoolExecutor:
    """交互默认池单例：应用启动时设为事件循环默认 executor，
    使交互路径的 asyncio.to_thread 不再与后台重活争抢同一池"""
    global _INTERACTIVE_EXECUTOR
    if _INTERACTIVE_EXECUTOR is None:
        _INTERACTIVE_EXECUTOR = ThreadPoolExecutor(
            max_workers=INTERACTIVE_WORKERS, thread_name_prefix="interactive"
        )
        logger.info("交互默认线程池初始化: workers=%d", INTERACTIVE_WORKERS)
    return _INTERACTIVE_EXECUTOR


def shutdown() -> None:
    """应用关停时回收两个线程池（等待在跑任务完成，不取消）"""
    global _EXECUTOR, _INTERACTIVE_EXECUTOR
    for pool in (_EXECUTOR, _INTERACTIVE_EXECUTOR):
        if pool is not None:
            pool.shutdown(wait=False)
    _EXECUTOR = None
    _INTERACTIVE_EXECUTOR = None
