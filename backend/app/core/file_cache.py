"""
见字如面 - 原始文件 Redis 缓存

缓存知识检索/知识库跳转链接涉及的原始文件（图片、Word、PDF、Word 转换后的预览 PDF），
首次访问读盘并写入 Redis，后续访问直接命中缓存，加快检索来源跳转与文档预览速度。

缓存键按知识库可见性隔离：
- 公共知识库文件: jzry:file:pub:{filename}          —— 所有用户共享同一份缓存
- 个人知识库文件: jzry:file:usr:{owner_id}:{filename} —— 按属主隔离，仅本人可命中

Redis 不可用时自动降级为直读磁盘（功能不受影响），并在冷却期后自动重试连接。
"""
import asyncio
import logging
import mimetypes
import time
from typing import Optional, Tuple

from app.config import settings

logger = logging.getLogger(__name__)

KEY_PREFIX = "jzry:file"

# Redis 连接失败后的重试冷却时间（秒），期间所有请求直读磁盘
_RECONNECT_COOLDOWN = 60


def fmt_size(n: int) -> str:
    """字节数格式化为可读大小（日志用）"""
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}MB"
    return f"{n / 1024:.1f}KB"


# 后台写入任务引用集（防止任务在 pending 时被 GC）
_BG_TASKS: set = set()


def _spawn_background(fn, *args):
    """将缓存写入放入后台线程，避免慢速远程 Redis 上行阻塞请求与事件循环"""
    task = asyncio.create_task(asyncio.to_thread(fn, *args))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


def effective_redis_url() -> str:
    """生效的 Redis 地址：系统设置页保存的覆盖 > .env 默认"""
    from app.services.config_manager import runtime_config
    return (runtime_config.redis_url or "").strip() or settings.REDIS_URL


def effective_file_cache_enabled() -> bool:
    """生效的文件缓存开关：系统设置页可在线开关，默认取 .env"""
    from app.services.config_manager import runtime_config
    value = runtime_config.get("file_cache_enabled")
    return settings.FILE_CACHE_ENABLED if value is None else bool(value)


class FileCache:
    """原始文件缓存服务（Redis 哈希存储：data/mime 两个字段）"""

    def __init__(self):
        self._client = None
        self._failed_until: float = 0.0

    # ---------- 缓存键 ----------

    @staticmethod
    def public_key(filename: str) -> str:
        """公共知识库文件缓存键（全局共享）"""
        return f"{KEY_PREFIX}:pub:{filename}"

    @staticmethod
    def private_key(owner_id: str, filename: str) -> str:
        """个人知识库文件缓存键（按属主隔离）"""
        return f"{KEY_PREFIX}:usr:{owner_id}:{filename}"

    @staticmethod
    def key_for(filename: str, visibility: str, owner_id: str) -> str:
        """按可见性选择缓存键"""
        if visibility == "public" or not owner_id:
            return FileCache.public_key(filename)
        return FileCache.private_key(owner_id, filename)

    # ---------- 连接 ----------

    @property
    def enabled(self) -> bool:
        return effective_file_cache_enabled() and time.time() >= self._failed_until

    def reset(self):
        """配置变更后重置连接，下次访问时用新地址重连"""
        self._client = None
        self._failed_until = 0.0

    def _get_client(self):
        """懒加载 Redis 客户端；不可用时进入冷却期并返回 None"""
        if not effective_file_cache_enabled():
            return None
        if time.time() < self._failed_until:
            return None
        if self._client is None:
            try:
                import redis
                client = redis.Redis.from_url(
                    effective_redis_url(),
                    socket_connect_timeout=3,
                    # 远程 Redis 上行带宽有限，大文件写入耗时较长，超时放宽到 30s
                    socket_timeout=30,
                )
                client.ping()
                self._client = client
                logger.info("Redis 文件缓存已连接: %s", effective_redis_url())
            except Exception as e:
                self._failed_until = time.time() + _RECONNECT_COOLDOWN
                logger.warning("Redis 不可用，文件缓存暂禁用（%ss 后重试）: %s",
                               _RECONNECT_COOLDOWN, e)
                return None
        return self._client

    def _mark_failed(self):
        self._client = None
        self._failed_until = time.time() + _RECONNECT_COOLDOWN

    # ---------- 读写 ----------

    def get(self, key: str) -> Optional[Tuple[bytes, str]]:
        """命中返回 (文件字节, media_type)，未命中或缓存不可用返回 None"""
        client = self._get_client()
        if not client:
            return None
        try:
            data = client.hget(key, "data")
            if data is None:
                return None
            mime = client.hget(key, "mime")
            mime_str = mime.decode("utf-8") if mime else "application/octet-stream"
            return data, mime_str
        except Exception as e:
            logger.warning("文件缓存读取失败(%s): %s", key, e)
            self._mark_failed()
            return None

    def set(self, key: str, data: bytes, filename: str) -> bool:
        """写入缓存；返回是否写入成功（超限/不可用/失败为 False）"""
        if len(data) > settings.FILE_CACHE_MAX_MB * 1024 * 1024:
            return False
        client = self._get_client()
        if not client:
            return False
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        t0 = time.time()
        try:
            pipe = client.pipeline()
            pipe.delete(key)
            pipe.hset(key, mapping={"data": data, "mime": mime})
            pipe.expire(key, settings.FILE_CACHE_TTL)
            pipe.execute()
            logger.info("文件缓存写入成功 | key=%s | size=%s | %.2fs",
                        key, fmt_size(len(data)), time.time() - t0)
            return True
        except Exception as e:
            logger.warning("文件缓存写入失败(%s): %s", key, e)
            self._mark_failed()
            return False

    def invalidate_file(self, filename: str, visibility: str, owner_id: str) -> None:
        """删除某文件的缓存（记录删除/更新时调用）"""
        client = self._get_client()
        if not client:
            return
        keys = [self.public_key(filename)]
        if owner_id:
            keys.append(self.private_key(owner_id, filename))
        elif visibility != "public":
            # 未知属主的私有文件：按模式扫描清理
            try:
                keys.extend(k for k in client.scan_iter(
                    match=f"{KEY_PREFIX}:usr:*:{filename}", count=100))
            except Exception:
                pass
        try:
            client.delete(*keys)
        except Exception as e:
            logger.warning("文件缓存失效操作失败(%s): %s", filename, e)
            self._mark_failed()

    def status(self) -> dict:
        """缓存状态（健康检查/调试用）"""
        if not effective_file_cache_enabled():
            return {"enabled": False, "connected": False}
        client = self._get_client()
        return {"enabled": True, "connected": client is not None}


file_cache = FileCache()


async def serve_cached_file(file_path, user: dict):
    """按「缓存 → 磁盘」顺序下发知识库原始文件，并执行公共/个人权限隔离。

    - 有上传记录：按可见性选择共享缓存键（pub）或属主隔离缓存键（usr），
      个人文件仅属主与管理员可访问；
    - 无记录（历史遗留文件）：不缓存、不限制，直读磁盘（保持旧行为）。
    返回 fastapi.Response；文件不存在时返回 None 由调用方抛 404。
    """
    import asyncio
    from fastapi import HTTPException
    from fastapi.responses import Response
    from app.services.records import upload_records

    file_path = file_path.resolve()
    if not file_path.is_file():
        return None

    filename = file_path.name
    # Word 转换出的预览 PDF 无独立记录，需按 pdf_preview_path 反查
    # 同一文件路径可能存在多条记录（不同属主分别入库），需全部取出做权限判定
    recs = upload_records.get_records_by_source(str(file_path)) \
        or upload_records.get_records_by_pdf_path(str(file_path))

    if recs:
        user_id = user.get("id") or user.get("username") or ""
        is_admin = user.get("role") == "admin"
        # 在所有匹配记录中挑选一条当前请求者可访问的记录：
        # 公共记录对所有人（含游客）可见；个人记录仅属主本人与管理员可见。
        # 任一记录可访问即放行，避免按路径取第一条命中非属主记录导致合法属主被误拒。
        rec = None
        for r in recs:
            visibility = r.get("visibility", "public")
            owner_id = r.get("owner_id") or ""
            if visibility == "public" or is_admin or (owner_id and owner_id == user_id):
                rec = r
                break
        if rec is None:
            raise HTTPException(status_code=403, detail="无权访问该文件")
        # 缓存键沿用可访问记录的可见性/属主，保持公共共享 / 个人隔离架构不变
        visibility = rec.get("visibility", "public")
        owner_id = rec.get("owner_id") or ""
        cache_key = file_cache.key_for(filename, visibility, owner_id)

        t0 = time.time()
        hit = await asyncio.to_thread(file_cache.get, cache_key)
        if hit:
            data, mime = hit
            logger.info(
                "文件下发 [HIT] %s | 来源=Redis 缓存 | key=%s | size=%s | %.3fs",
                filename, cache_key, fmt_size(len(data)), time.time() - t0,
            )
            return Response(
                content=data, media_type=mime,
                headers={"X-File-Cache": "HIT"},
            )
        data = await asyncio.to_thread(file_path.read_bytes)
        if file_cache.enabled:
            if len(data) > settings.FILE_CACHE_MAX_MB * 1024 * 1024:
                logger.info(
                    "文件下发 [MISS] %s | 来源=本地磁盘 | size=%s | 超过大小上限，不缓存",
                    filename, fmt_size(len(data)),
                )
            else:
                _spawn_background(file_cache.set, cache_key, data, filename)
                logger.info(
                    "文件下发 [MISS] %s | 来源=本地磁盘 | key=%s | size=%s | 已提交后台写入缓存",
                    filename, cache_key, fmt_size(len(data)),
                )
        else:
            logger.info(
                "文件下发 [SKIP] %s | 来源=本地磁盘 | size=%s | 缓存不可用（已关闭或连接冷却中）",
                filename, fmt_size(len(data)),
            )
        return Response(
            content=data,
            media_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
            headers={"X-File-Cache": "MISS"},
        )

    # 无上传记录：不缓存，直接下发
    data = await asyncio.to_thread(file_path.read_bytes)
    logger.info(
        "文件下发 [BYPASS] %s | 来源=本地磁盘 | 无上传记录，不缓存 | size=%s",
        filename, fmt_size(len(data)),
    )
    return Response(
        content=data,
        media_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
        headers={"X-File-Cache": "BYPASS"},
    )
