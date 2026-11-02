"""
见字如面 - 日志配置
- 控制台 + 文件双输出：文件按日期保存在 backend/log/YYYY-MM-DD.log，
  追加写入（当天重启服务不会覆盖），跨天自动切换到新日期文件
- 每条日志在时间前附带当前请求的用户名（ContextVar，由鉴权中间件写入）
"""
import logging
import os
import re
from contextvars import ContextVar
from datetime import date
from pathlib import Path

# backend/log 目录（app/core/log_setup.py → backend）
LOG_DIR = Path(__file__).resolve().parent.parent.parent / "log"

# 当前请求的用户名（未登录/系统日志显示 "-"）
current_username: ContextVar[str] = ContextVar("current_username", default="-")

# 用户名放在时间前面
LOG_FORMAT = "[%(username)s] %(asctime)s [%(levelname)s] [trace=%(trace_id)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class UsernameFilter(logging.Filter):
    """把 ContextVar 中的用户名注入每条日志记录"""

    def filter(self, record):
        from app.core.observability import current_trace_id

        record.username = current_username.get()
        record.trace_id = current_trace_id() or "-"
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


# 查询只接受服务端日志目录中的日期文件；不接受客户端传入路径。
_LOG_HEADER = re.compile(
    r"^\[[^\r\n]*?\] \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} "
    r"\[(?:DEBUG|INFO|WARNING|ERROR|CRITICAL)\] (.*)"
)
_TRACE_FIELD = re.compile(r"^\[trace=([0-9a-f-]{36}|-)\] ")
_LEGACY_TRACE = re.compile(r"^jianziruyang\.agent: \[AGENT\] trace=([0-9a-f-]{36})(?:\s|$)")
_LOG_CURSOR = re.compile(r"^(\d{4}-\d{2}-\d{2}):(\d{1,16})$")
_SCAN_BYTES = 8 * 1024 * 1024
_RECORD_BYTES = 200000
_PAGE_BYTES = 500000


def redact_log_text(text: str) -> str:
    """返回给管理界面的日志副本脱敏，不改写原始日志文件。"""
    text = re.sub(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", r"\1 [REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", text)
    text = re.sub(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^\s/@]+@", r"\1[REDACTED]@", text)
    # 同时覆盖 JSON、HTTP 头、key=value 与 URL 查询参数形式。
    text = re.sub(
        r'''(?ix)(["']?(?:api[\x20_-]?key|access[_-]?token|refresh[_-]?token|token|'''
        r'''authorization|password|passwd|secret|cookie|set-cookie)["']?\s*[:=]\s*)'''
        r'''(?:"[^"\r\n]*(?:"|$)|'[^'\r\n]*(?:'|$)|[^\s,;&}\r\n]+)''',
        r"\1[REDACTED]", text,
    )
    text = re.sub(r"(?im)(\b(?:set-cookie|cookie)\s*:\s*)[^\r\n]+", r"\1[REDACTED]", text)
    return text


def _log_records(stream, trace_id: str):
    """按日志头切分记录，异常堆栈保留在所属记录中，绝不按时间猜测归属。"""
    start = stream.tell()
    belongs = False
    parts = []
    size = 0
    clipped = False
    line_start = True
    while True:
        position = stream.tell()
        raw = stream.readline(65536)
        if not raw:
            if position > start:
                yield start, position, b"".join(parts), clipped
            return
        header = _LOG_HEADER.match(raw.decode("utf-8", errors="replace")) if line_start else None
        line_start = raw.endswith(b"\n")
        if header:
            if position > start:
                yield start, position, b"".join(parts), clipped
            start, parts, size, clipped = position, [], 0, False
            field = _TRACE_FIELD.match(header[1]) or _LEGACY_TRACE.match(header[1])
            belongs = bool(field and field[1] == trace_id)
        if belongs:
            remaining = max(0, _RECORD_BYTES - size)
            if remaining:
                parts.append(raw[:remaining])
            size += len(raw)
            clipped = size > _RECORD_BYTES


def read_trace_logs(trace: dict, cursor: str = "", limit: int = 100) -> dict:
    """分页读取当前请求已落盘的日志；游标是日期与字节位置，可跨午夜续读。"""
    start_day = str(trace.get("started_at") or "")[:10]
    end_day = str(trace.get("ended_at") or date.today().isoformat())[:10]
    warnings = []
    result = {"trace_id": trace["id"], "entries": [], "next_cursor": None, "warnings": warnings}
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start_day) or end_day < start_day:
        warnings.append("请求时间信息不完整，无法定位日志文件。")
        return result
    cursor_day, cursor_offset = start_day, 0
    if cursor:
        match = _LOG_CURSOR.fullmatch(cursor)
        if not match or not start_day <= match[1] <= end_day:
            raise ValueError("日志分页游标无效，请刷新后重试")
        cursor_day, cursor_offset = match[1], int(match[2])
    try:
        files = sorted(
            path for path in LOG_DIR.glob("????-??-??.log")
            if start_day <= path.stem <= end_day and not path.is_symlink() and path.is_file()
        )
    except OSError:
        warnings.append("日志目录暂时无法读取。")
        return result
    if not files:
        warnings.append("未找到该请求日期范围内的日志文件，可能尚未落盘或已清理。")
        return result
    scanned = payload = 0
    for path in files:
        if path.stem < cursor_day:
            continue
        offset = cursor_offset if path.stem == cursor_day else 0
        try:
            with path.open("rb") as stream:
                if offset > path.stat().st_size:
                    warnings.append("日志文件已变化，请刷新后重新读取。")
                    continue
                stream.seek(offset)
                for begin, end, raw, clipped in _log_records(stream, trace["id"]):
                    scanned += end - begin
                    if raw:
                        text = redact_log_text(raw.decode("utf-8", errors="replace"))
                        if clipped:
                            text += "\n[单条日志超过 200000 字节，已截断]"
                        result["entries"].append({
                            "id": f"{path.stem}:{begin}", "source": path.name,
                            "text": text, "truncated": clipped,
                        })
                        payload += len(raw)
                    if len(result["entries"]) >= limit or payload >= _PAGE_BYTES or scanned >= _SCAN_BYTES:
                        result["next_cursor"] = f"{path.stem}:{end}"
                        return result
        except OSError:
            warnings.append(f"日志文件 {path.name} 无法读取，可能已清理或权限不足。")
    return result


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
