"""
见字如面 - 日志配置
- 控制台 + 文件双输出：文件按日期保存在 backend/log/YYYY-MM-DD.log，
  追加写入（当天重启服务不会覆盖），跨天自动切换到新日期文件
- 每条日志在时间前附带当前请求的用户名（ContextVar，由鉴权中间件写入）
"""
import logging
import os
from contextvars import ContextVar
from datetime import date
from pathlib import Path

# backend/log 目录（app/core/log_setup.py → backend）
LOG_DIR = Path(__file__).resolve().parent.parent.parent / "log"

# 当前请求的用户名（未登录/系统日志显示 "-"）
current_username: ContextVar[str] = ContextVar("current_username", default="-")

# 用户名放在时间前面
LOG_FORMAT = "[%(username)s] %(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class UsernameFilter(logging.Filter):
    """把 ContextVar 中的用户名注入每条日志记录"""

    def filter(self, record):
        record.username = current_username.get()
        return True


class DailyFileHandler(logging.FileHandler):
    """按日期写入 log/YYYY-MM-DD.log 的文件处理器

    - 追加模式：同一天内多次启动服务，日志都写进当天文件
    - 跨天时自动关闭旧文件并切换到新日期的文件
    """

    def __init__(self, log_dir: Path):
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._current_day = date.today()
        super().__init__(self._path_for(self._current_day), mode="a", encoding="utf-8")

    def _path_for(self, day: date) -> str:
        return str(self._log_dir / f"{day.isoformat()}.log")

    def emit(self, record):
        today = date.today()
        if today != self._current_day:
            self._current_day = today
            self.close()
            self.baseFilename = os.path.abspath(self._path_for(today))
            # stream 置空后，父类 emit 会自动按新路径重新打开文件
            self.stream = None
        super().emit(record)


def setup_logging(debug: bool = False):
    """初始化根日志器：控制台 + 按日期滚动的文件"""
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    user_filter = UsernameFilter()

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(user_filter)

    file_handler = DailyFileHandler(LOG_DIR)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(user_filter)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    # 清理已有 handler（避免与 basicConfig / reload 残留叠加导致重复输出）
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)
