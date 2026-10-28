"""单机 Agent 写者资格；Proxy 不取得此锁，也不恢复业务任务。"""
import os
from datetime import datetime
from pathlib import Path

from filelock import FileLock, Timeout

from app.core import db


class AgentRuntime:
    def __init__(self):
        self._lock = None
        self._pid = None
        self.ready = False

    def acquire(self):
        if self.owns_process():
            raise RuntimeError("同一进程不能重复启动 Agent lifespan")
        path = db.DB_PATH.parent / ".agent-runtime.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(path), timeout=0, thread_local=False)
        try:
            lock.acquire()
        except Timeout as exc:
            raise RuntimeError("已有 Agent 持有此数据库的写者锁；只支持单 Agent、单 worker") from exc
        self._lock, self._pid = lock, os.getpid()
        self.ready = False

    def owns_process(self):
        return bool(self._pid == os.getpid() and self._lock and self._lock.is_locked)

    def require_owner(self):
        if not self.owns_process():
            raise RuntimeError("业务恢复必须由持有独占锁的 Agent 执行")

    def recover(self):
        self.require_owner()
        now = datetime.now().isoformat()
        with db.transaction() as conn:
            conn.execute("UPDATE wiki_compile_queue SET status='pending',updated_at=? WHERE status='processing'", (now,))
            conn.execute("UPDATE pending_uploads SET status='pending',updated_at=? WHERE status='parsing'", (now,))
            conn.execute("UPDATE eval_runs SET status='interrupted',finished_at=COALESCE(finished_at,?) WHERE status='running'", (now,))
            conn.execute("""UPDATE pipeline_stages SET status='aborted',finished_at=COALESCE(finished_at,?)
                WHERE status IN ('pending','running') AND run_id IN
                (SELECT id FROM pipeline_runs WHERE status='running')""", (now,))
            conn.execute("UPDATE pipeline_runs SET status='aborted',finished_at=COALESCE(finished_at,?) WHERE status='running'", (now,))
            conn.execute("""UPDATE chat_pending_actions SET status='interrupted',updated_at=?,
                error='服务重启，执行结果可能部分落库，请核对知识库后再操作；不会自动重试'
                WHERE status='running'""", (now,))

    def stop(self):
        self.ready = False
        # 不提前释放：线程池仍可能写入；锁的句柄保留至进程退出。


runtime = AgentRuntime()


def readiness(service):
    """仅检查本地必需资源，不调用模型或可选缓存。"""
    from app.config import settings
    checks = {}
    try:
        checks["database"] = db.query_one("SELECT 1 AS ok")["ok"] == 1
        checks["schema"] = db.query_one("SELECT COUNT(*) AS n FROM schema_migrations WHERE version IN ('production_ab_v1','production_ab_v2')")["n"] == 2
    except Exception:
        checks["database"] = False
    directories = {"database_directory": db.DB_PATH.parent}
    if service == "agent":
        checks["runtime"] = runtime.owns_process() and runtime.ready
        directories.update(uploads=settings.UPLOAD_DIR, knowledge=settings.KNOWLEDGE_DIR,
                           wiki=settings.WIKI_DIR, index=Path(settings.VECTOR_STORE_PATH).parent)
    for name, path in directories.items():
        checks[name] = Path(path).is_dir() and os.access(path, os.R_OK | os.W_OK | os.X_OK)
    ready = all(checks.values())
    return {"status": "ready" if ready else "not_ready", "service": service, "checks": checks}
