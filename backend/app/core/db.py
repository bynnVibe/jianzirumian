"""
见字如面 - SQLite 存储层
统一管理用户、会话、上传记录、收藏、知识库、反馈、用量统计的持久化
"""
import logging
import sqlite3
import threading
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
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_wiki_pages_kb ON wiki_pages(kb_id);
        CREATE INDEX IF NOT EXISTS idx_wiki_pages_source ON wiki_pages(source_image);

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
