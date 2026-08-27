"""
见字如面 - 游客查询限制器
基于 SQLite + 短期内存缓存实现游客查询次数限制；
次数上限由管理员在系统设置中配置（runtime_config.guest_query_limit）
"""
import hashlib
import logging
from datetime import date, datetime
from typing import Tuple

from app.core import db

logger = logging.getLogger(__name__)

DEFAULT_GUEST_QUERY_LIMIT = 3


def _configured_limit() -> int:
    """读取管理员配置的游客免费次数（延迟导入避免循环依赖）"""
    try:
        from app.services.config_manager import runtime_config
        return runtime_config.guest_query_limit
    except Exception:
        return DEFAULT_GUEST_QUERY_LIMIT


class GuestLimiter:
    """游客查询限制器，基于 IP 地址计数"""

    def __init__(self):
        # {(ip_hash, day): count} 短缓存；权威计数在 SQLite guest_usage
        self._counts: dict[tuple[str, str], int] = {}

    @staticmethod
    def _ip_hash(client_ip: str) -> str:
        return hashlib.sha256((client_ip or "unknown").encode()).hexdigest()

    @staticmethod
    def _today() -> str:
        return date.today().isoformat()

    def _get_count(self, client_ip: str) -> int:
        key = (self._ip_hash(client_ip), self._today())
        if key in self._counts:
            return self._counts[key]
        row = db.query_one(
            "SELECT count FROM guest_usage WHERE ip_hash = ? AND day = ?",
            key,
        )
        count = int(row["count"]) if row else 0
        self._counts[key] = count
        return count

    @property
    def limit(self) -> int:
        return _configured_limit()

    def check_limit(self, client_ip: str) -> Tuple[bool, int]:
        """
        检查游客是否还有查询次数
        返回 (是否可用, 剩余次数)
        """
        limit = self.limit
        current = self._get_count(client_ip)
        remaining = max(0, limit - current)
        return current < limit, remaining

    def increment(self, client_ip: str) -> int:
        """增加查询计数，返回剩余次数"""
        ip_hash = self._ip_hash(client_ip)
        day = self._today()
        key = (ip_hash, day)
        current = self._get_count(client_ip) + 1
        self._counts[key] = current
        db.execute(
            "INSERT INTO guest_usage (ip_hash, day, count, updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(ip_hash, day) DO UPDATE SET count = excluded.count, updated_at = excluded.updated_at",
            (ip_hash, day, current, datetime.now().isoformat()),
        )
        remaining = max(0, self.limit - current)
        logger.info(f"[游客限制] IP={client_ip} 查询次数+1, 剩余={remaining}")
        return remaining

    def get_remaining(self, client_ip: str) -> int:
        """获取剩余查询次数"""
        current = self._get_count(client_ip)
        return max(0, self.limit - current)


# 全局单例
guest_limiter = GuestLimiter()
