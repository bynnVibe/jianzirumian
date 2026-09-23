"""
见字如面 - SQLite 存储层
统一管理用户、会话、上传记录、收藏、知识库、反馈、用量统计的持久化
"""
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("jianziruyang.db")

# SQLite 数据库存放在 backend 项目内部：backend/data/app.db
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
DB_PATH = BACKEND_DIR / "data" / "app.db"

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def get_conn() -> sqlite3.Connection:
    """获取全局 SQLite 连接（懒初始化，WAL 模式）"""
    global _conn
    with _lock:
        if _conn is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA foreign_keys=ON")
            init_db(_conn)
        return _conn


def init_db(conn: sqlite3.Connection):
    """建表（幂等）"""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            TEXT PRIMARY KEY,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt          TEXT NOT NULL,
            hash_algo     TEXT NOT NULL DEFAULT 'pbkdf2',
            contact       TEXT NOT NULL DEFAULT '',
            role          TEXT NOT NULL DEFAULT 'user',
            is_active     INTEGER NOT NULL DEFAULT 1,
            created_at    REAL NOT NULL,
            updated_at    REAL NOT NULL,
            last_login_at REAL
        );

        CREATE TABLE IF NOT EXISTS chat_sessions (
            session_id TEXT PRIMARY KEY,
            user_id    TEXT NOT NULL DEFAULT '',
            title      TEXT NOT NULL DEFAULT '新对话',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chat_messages (
            message_id     TEXT PRIMARY KEY,
            session_id     TEXT NOT NULL,
            seq            INTEGER NOT NULL,
            role           TEXT NOT NULL,
            content        TEXT NOT NULL,
            sources        TEXT NOT NULL DEFAULT '[]',
            search_results TEXT NOT NULL DEFAULT '[]',
            use_knowledge  INTEGER NOT NULL DEFAULT 0,
            use_search     INTEGER NOT NULL DEFAULT 0,
            attachments    TEXT NOT NULL DEFAULT '[]',
            created_at     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_session ON chat_messages(session_id, seq);

        CREATE TABLE IF NOT EXISTS upload_records (
            id               TEXT PRIMARY KEY,
            filename         TEXT NOT NULL,
            title            TEXT NOT NULL DEFAULT '',
            image_path       TEXT NOT NULL,
            ocr_text         TEXT NOT NULL DEFAULT '',
            ocr_provider     TEXT NOT NULL DEFAULT 'local',
            chunk_count      INTEGER NOT NULL DEFAULT 0,
            doc_ids          TEXT NOT NULL DEFAULT '[]',
            visibility       TEXT NOT NULL DEFAULT 'public',
            owner_id         TEXT NOT NULL DEFAULT '',
            source_type      TEXT NOT NULL DEFAULT 'image',
            pages            TEXT NOT NULL DEFAULT '[]',
            pdf_preview_path TEXT,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bookmarks (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            title       TEXT NOT NULL,
            tags        TEXT NOT NULL DEFAULT '[]',
            content     TEXT NOT NULL DEFAULT '',
            source_type TEXT NOT NULL DEFAULT 'knowledge',
            source_info TEXT NOT NULL DEFAULT '{}',
            created_at  REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_bookmarks_user ON bookmarks(user_id);

        CREATE TABLE IF NOT EXISTS knowledge_bases (
            id         TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            topic      TEXT NOT NULL DEFAULT '其他',
            visibility TEXT NOT NULL DEFAULT 'public',
            owner_id   TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS message_feedbacks (
            id         TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            user_id    TEXT NOT NULL DEFAULT '',
            rating     TEXT NOT NULL,
            comment    TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_message
            ON message_feedbacks(message_id, user_id);

        CREATE TABLE IF NOT EXISTS usage_daily (
            user_id         TEXT NOT NULL,
            day             TEXT NOT NULL,
            query_count     INTEGER NOT NULL DEFAULT 0,
            prompt_chars    INTEGER NOT NULL DEFAULT 0,
            completion_chars INTEGER NOT NULL DEFAULT 0,
            UNIQUE(user_id, day)
        );

        CREATE TABLE IF NOT EXISTS auth_sessions (
            token_hash   TEXT PRIMARY KEY,
            user_id      TEXT NOT NULL,
            expires_at   REAL NOT NULL,
            created_at   REAL NOT NULL,
            last_seen_at REAL NOT NULL,
            user_agent   TEXT NOT NULL DEFAULT '',
            ip           TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);
        CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires ON auth_sessions(expires_at);

        CREATE TABLE IF NOT EXISTS guest_usage (
            ip_hash    TEXT NOT NULL,
            day        TEXT NOT NULL,
            count      INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(ip_hash, day)
        );

        CREATE TABLE IF NOT EXISTS user_memories (
            id                TEXT PRIMARY KEY,
            user_id           TEXT NOT NULL,
            memory_type       TEXT NOT NULL DEFAULT 'fact',
            content           TEXT NOT NULL,
            keywords          TEXT NOT NULL DEFAULT '[]',
            source_session_id TEXT NOT NULL DEFAULT '',
            source_message_id TEXT NOT NULL DEFAULT '',
            importance        REAL NOT NULL DEFAULT 0.5,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL,
            last_used_at      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_user_memories_user ON user_memories(user_id, updated_at);

        CREATE TABLE IF NOT EXISTS chat_session_summaries (
            session_id               TEXT PRIMARY KEY,
            user_id                  TEXT NOT NULL,
            summary                  TEXT NOT NULL,
            covered_until_message_id TEXT NOT NULL DEFAULT '',
            token_estimate           INTEGER NOT NULL DEFAULT 0,
            created_at               TEXT NOT NULL,
            updated_at               TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tool_calls (
            id          TEXT PRIMARY KEY,
            session_id  TEXT NOT NULL,
            message_id  TEXT NOT NULL,
            user_id     TEXT NOT NULL DEFAULT '',
            tool_name   TEXT NOT NULL,
            input_json  TEXT NOT NULL DEFAULT '{}',
            output_json TEXT NOT NULL DEFAULT '{}',
            status      TEXT NOT NULL DEFAULT 'success',
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id, created_at);

        -- llm-wiki 知识编译层：把原始资料编译成结构化百科卡片/综合报告
        -- page_type: source(单来源知识卡片) | digest(跨素材综合报告)
        -- source_hash: 原始资料文本 SHA256，用于增量编译（未变更跳过）
        -- analysis:    两步思维链 Step1 产出的结构化分析 JSON（实体/论点/关联/矛盾）
        CREATE TABLE IF NOT EXISTS wiki_pages (
            id          TEXT PRIMARY KEY,
            page_type   TEXT NOT NULL DEFAULT 'source',
            title       TEXT NOT NULL,
            content     TEXT NOT NULL DEFAULT '',
            kb_id       TEXT NOT NULL DEFAULT '',
            visibility  TEXT NOT NULL DEFAULT 'public',
            owner_id    TEXT NOT NULL DEFAULT '',
            source_image TEXT NOT NULL DEFAULT '',
            source_refs TEXT NOT NULL DEFAULT '[]',
            doc_ids     TEXT NOT NULL DEFAULT '[]',
            source_hash TEXT NOT NULL DEFAULT '',
            analysis    TEXT NOT NULL DEFAULT '{}',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_wiki_pages_kb ON wiki_pages(kb_id);
        CREATE INDEX IF NOT EXISTS idx_wiki_pages_source ON wiki_pages(source_image);

        -- llm-wiki 持久化编译队列：串行处理防止并发 LLM 调用，重启后自动恢复未完成任务，
        -- 失败任务自动重试（最多 WIKI_COMPILE_MAX_ATTEMPTS 次）。
        -- status: pending(排队) | processing(处理中) | done(完成) | skipped(哈希未变跳过) | failed(重试耗尽)
        CREATE TABLE IF NOT EXISTS wiki_compile_queue (
            id           TEXT PRIMARY KEY,
            source_image TEXT NOT NULL DEFAULT '',
            source_hash  TEXT NOT NULL DEFAULT '',
            payload      TEXT NOT NULL DEFAULT '{}',
            status       TEXT NOT NULL DEFAULT 'pending',
            attempts     INTEGER NOT NULL DEFAULT 0,
            error        TEXT NOT NULL DEFAULT '',
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_wiki_queue_status ON wiki_compile_queue(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_wiki_queue_source ON wiki_compile_queue(source_image);

        -- 知识助手 Agent 提议的百科页面编辑（human-in-the-loop）：Agent 不直接改库，
        -- 而是生成待确认编辑，由用户在助手中确认后 apply_edit 才真正落库/重索引/镜像。
        -- status: pending(待确认) | approved(已批准并应用) | rejected(已驳回)
        CREATE TABLE IF NOT EXISTS wiki_pending_edits (
            id          TEXT PRIMARY KEY,
            page_id     TEXT NOT NULL,
            page_title  TEXT NOT NULL DEFAULT '',
            old_content TEXT NOT NULL DEFAULT '',
            new_content TEXT NOT NULL DEFAULT '',
            reason      TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'pending',
            created_by  TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_wiki_pending_status ON wiki_pending_edits(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_wiki_pending_page ON wiki_pending_edits(page_id);

        -- 知识库上传流水线：文件上传后先后台解析，用户确认后再入库。
        -- status: pending(排队中) | parsing(解析中) | done(解析完成待入库) | error(解析失败)
        CREATE TABLE IF NOT EXISTS pending_uploads (
            id               TEXT PRIMARY KEY,
            filename         TEXT NOT NULL,
            source_type      TEXT NOT NULL DEFAULT 'image',
            file_path        TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'pending',
            parsed_text      TEXT NOT NULL DEFAULT '',
            pages            TEXT NOT NULL DEFAULT '[]',
            pdf_preview_path TEXT,
            ocr_provider     TEXT NOT NULL DEFAULT '',
            error            TEXT NOT NULL DEFAULT '',
            owner_id         TEXT NOT NULL DEFAULT '',
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_pending_owner ON pending_uploads(owner_id, created_at);

        -- Agent 回归评测模块：评测集 → 用例 → 运行 → 逐用例结果
        CREATE TABLE IF NOT EXISTS eval_datasets (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS eval_cases (
            id               TEXT PRIMARY KEY,
            dataset_id       TEXT NOT NULL,
            question         TEXT NOT NULL,
            expected_keywords TEXT NOT NULL DEFAULT '[]',
            reference_answer TEXT NOT NULL DEFAULT '',
            expected_source  TEXT NOT NULL DEFAULT '',
            created_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_eval_cases_dataset ON eval_cases(dataset_id);

        -- status: running(执行中) | finished(完成) | failed(整体失败) | interrupted(重启中断)
        CREATE TABLE IF NOT EXISTS eval_runs (
            id          TEXT PRIMARY KEY,
            dataset_id  TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'running',
            started_at  TEXT NOT NULL,
            finished_at TEXT,
            summary     TEXT NOT NULL DEFAULT '{}',
            created_by  TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_eval_runs_dataset ON eval_runs(dataset_id, started_at);

        CREATE TABLE IF NOT EXISTS eval_results (
            id                TEXT PRIMARY KEY,
            run_id            TEXT NOT NULL,
            case_id           TEXT NOT NULL,
            question          TEXT NOT NULL DEFAULT '',
            answer            TEXT NOT NULL DEFAULT '',
            retrieved_sources TEXT NOT NULL DEFAULT '[]',
            retrieval_hit     INTEGER NOT NULL DEFAULT 0,
            faithfulness_pass INTEGER NOT NULL DEFAULT 0,
            faithfulness      REAL,
            answer_relevance  REAL,
            context_precision REAL,
            context_recall    REAL,
            metric_details    TEXT NOT NULL DEFAULT '{}',
            judge_score       REAL,
            judge_reason      TEXT NOT NULL DEFAULT '',
            latency_ms        INTEGER NOT NULL DEFAULT 0,
            status            TEXT NOT NULL DEFAULT 'ok',
            error             TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_eval_results_run ON eval_results(run_id);

        -- Agent 可观测性（Tracing 主表）：一次用户请求 / 一次 Agent 运行 = 一条 trace。
        -- status: running(进行中) | ok(成功) | error(失败)；结束时由 span 聚合回写调用数与 token 数。
        CREATE TABLE IF NOT EXISTS agent_traces (
            id                TEXT PRIMARY KEY,
            agent             TEXT NOT NULL DEFAULT '',
            user_id           TEXT NOT NULL DEFAULT '',
            input             TEXT NOT NULL DEFAULT '',
            status            TEXT NOT NULL DEFAULT 'running',
            error             TEXT NOT NULL DEFAULT '',
            llm_calls         INTEGER NOT NULL DEFAULT 0,
            tool_calls        INTEGER NOT NULL DEFAULT 0,
            prompt_tokens     INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            latency_ms        INTEGER NOT NULL DEFAULT 0,
            started_at        TEXT NOT NULL,
            ended_at          TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_agent_traces_started ON agent_traces(started_at);
        CREATE INDEX IF NOT EXISTS idx_agent_traces_agent ON agent_traces(agent, started_at);

        -- Agent 可观测性（Tracing 明细表）：trace 内每一步 = 一条 span。
        -- kind: llm(LLM 调用，input/output 存完整 prompt 与 response) | tool(工具调用) | step(流程步骤)
        CREATE TABLE IF NOT EXISTS agent_spans (
            id                TEXT PRIMARY KEY,
            trace_id          TEXT NOT NULL,
            seq               INTEGER NOT NULL DEFAULT 0,
            kind              TEXT NOT NULL DEFAULT '',
            name              TEXT NOT NULL DEFAULT '',
            input             TEXT NOT NULL DEFAULT '',
            output            TEXT NOT NULL DEFAULT '',
            status            TEXT NOT NULL DEFAULT 'ok',
            error             TEXT NOT NULL DEFAULT '',
            prompt_tokens     INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            latency_ms        INTEGER NOT NULL DEFAULT 0,
            started_at        TEXT NOT NULL,
            ended_at          TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_agent_spans_trace ON agent_spans(trace_id, seq);
        CREATE INDEX IF NOT EXISTS idx_agent_spans_started ON agent_spans(started_at);
        """
    )
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection):
    """对存量数据库补加新列（幂等）"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(usage_daily)")}
    for col in ("knowledge_count", "web_search_count", "upload_count"):
        if col not in cols:
            conn.execute(
                f"ALTER TABLE usage_daily ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0"
            )
            logger.info("usage_daily 补加列: %s", col)

    # 聊天消息附件元数据（[{file_id, filename, kind}] JSON），用于历史回显与新页预览
    msg_cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_messages)")}
    if "attachments" not in msg_cols:
        conn.execute(
            "ALTER TABLE chat_messages ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'"
        )
        logger.info("chat_messages 补加列: attachments")

    # 上传记录自定义标题（用于知识库管理搜索与展示）
    upload_cols = {r[1] for r in conn.execute("PRAGMA table_info(upload_records)")}
    if "title" not in upload_cols:
        conn.execute(
            "ALTER TABLE upload_records ADD COLUMN title TEXT NOT NULL DEFAULT ''"
        )
        logger.info("upload_records 补加列: title")

    # llm-wiki：wiki_pages 补加 source_hash（增量编译）与 analysis（两步思维链分析结果）
    wiki_cols = {r[1] for r in conn.execute("PRAGMA table_info(wiki_pages)")}
    if "source_hash" not in wiki_cols:
        conn.execute(
            "ALTER TABLE wiki_pages ADD COLUMN source_hash TEXT NOT NULL DEFAULT ''"
        )
        logger.info("wiki_pages 补加列: source_hash")
    if "analysis" not in wiki_cols:
        conn.execute(
            "ALTER TABLE wiki_pages ADD COLUMN analysis TEXT NOT NULL DEFAULT '{}'"
        )
        logger.info("wiki_pages 补加列: analysis")

    # llm-wiki：服务重启时把上次进程遗留的 processing 编译任务退回 pending，
    # 交由启动恢复流程重新入队处理（后台 worker 随进程重启已丢失）
    try:
        cur = conn.execute(
            "UPDATE wiki_compile_queue SET status = 'pending', updated_at = ?"
            " WHERE status = 'processing'",
            (datetime.now().isoformat(),),
        )
        if cur.rowcount:
            logger.info("wiki_compile_queue 遗留 processing 任务退回 pending: %d 条", cur.rowcount)
    except sqlite3.OperationalError:
        pass

    # 回归评测 Ragas 化：eval_results 补加四项指标列与判定明细列（幂等）
    eval_cols = {r[1] for r in conn.execute("PRAGMA table_info(eval_results)")}
    for col, ddl in (
        ("faithfulness", "REAL"),
        ("answer_relevance", "REAL"),
        ("context_precision", "REAL"),
        ("context_recall", "REAL"),
        ("metric_details", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        if col not in eval_cols:
            conn.execute(f"ALTER TABLE eval_results ADD COLUMN {col} {ddl}")
            logger.info("eval_results 补加列: %s", col)

    # 回归评测：服务启动时把上次进程遗留的 running 运行标记为 interrupted，
    # 避免历史列表出现永远"执行中"的僵尸运行（后台任务随进程重启已丢失）
    try:
        cur = conn.execute(
            "UPDATE eval_runs SET status = 'interrupted', finished_at = COALESCE(finished_at, ?)"
            " WHERE status = 'running'",
            (datetime.now().isoformat(),),
        )
        if cur.rowcount:
            logger.info("eval_runs 遗留 running 运行标记为 interrupted: %d 条", cur.rowcount)
    except sqlite3.OperationalError:
        # 表尚未创建（首次建表在 executescript 中已完成，此处仅防御）
        pass


def query(sql: str, params: tuple = ()) -> List[dict]:
    """执行查询，返回 dict 列表"""
    conn = get_conn()
    with _lock:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def query_one(sql: str, params: tuple = ()) -> Optional[dict]:
    """执行查询，返回单行 dict 或 None"""
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> int:
    """执行写操作并提交，返回受影响行数"""
    conn = get_conn()
    with _lock:
        cur = conn.execute(sql, params)
        conn.commit()
    return cur.rowcount


def executemany(sql: str, seq_params: list) -> None:
    """批量执行写操作并提交"""
    conn = get_conn()
    with _lock:
        conn.executemany(sql, seq_params)
        conn.commit()
