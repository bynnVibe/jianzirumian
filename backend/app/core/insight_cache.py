"""
见字如面 - 洞察产物 JSON 缓存（思维导图 / 知识图谱 / 知识百科列表）

需求：生成物「本地（SQLite）存一份 + Redis 缓存一份」，页面加载优先读缓存。
- 读：cache_get(key) 命中直接返回 dict；未命中或 Redis 不可用返回 None，由调用方回源 SQLite；
- 写：回源后 cache_set(key, payload) 异步写入（TTL 兜底，防脏数据长期驻留）；
- 失效：重建/编辑/删除时 cache_delete(key) 精确删除。

Redis 不可用时进入冷却期并静默降级为「仅本地存储」，不影响主流程。
"""
import json
import logging
import time
from typing import Optional

logger = logging.getLogger("jianziruyang.insight_cache")

_RECONNECT_COOLDOWN = 60.0
_client = None
_failed_until = 0.0


def _get_client():
    """懒加载 Redis 客户端；不可用时冷却并返回 None（降级为仅本地）。"""
    global _client, _failed_until
    if time.time() < _failed_until:
        return None
    if _client is None:
        try:
            from app.core.file_cache import effective_redis_url
            url = effective_redis_url()
            if not url:
                _failed_until = time.time() + _RECONNECT_COOLDOWN
                return None
            import redis
            client = redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=5)
            client.ping()
            _client = client
        except Exception as e:
            _failed_until = time.time() + _RECONNECT_COOLDOWN
            logger.debug("洞察缓存 Redis 不可用（%ss 冷却）: %s", _RECONNECT_COOLDOWN, e)
            return None
    return _client


def cache_get(key: str) -> Optional[dict]:
    """命中返回 dict；未命中/不可用/损坏返回 None。"""
    client = _get_client()
    if not client:
        return None
    try:
        raw = client.get(key)
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        logger.debug("洞察缓存读取失败 key=%s: %s", key, e)
        return None


def cache_set(key: str, payload: dict, ttl: int = 1800) -> bool:
    """写入缓存（同步、轻量载荷）；失败静默降级。"""
    client = _get_client()
    if not client:
        return False
    try:
        client.setex(key, ttl, json.dumps(payload, ensure_ascii=False))
        return True
    except Exception as e:
        global _client, _failed_until
        _client = None
        _failed_until = time.time() + _RECONNECT_COOLDOWN
        logger.debug("洞察缓存写入失败 key=%s: %s", key, e)
        return False


def cache_delete(key: str) -> None:
    """精确失效；Redis 不可用时忽略（TTL 兜底）。"""
    client = _get_client()
    if not client:
        return
    try:
        client.delete(key)
    except Exception as e:
        logger.debug("洞察缓存失效失败 key=%s: %s", key, e)


def mindmap_key(kb_id: str) -> str:
    return f"jzry:insight:mindmap:{kb_id}"


def graph_key(kb_id: str) -> str:
    return f"jzry:insight:graph:{kb_id}"


def wiki_list_key(kb_id: str) -> str:
    return f"jzry:insight:wikilist:{kb_id}"
