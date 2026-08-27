"""
见字如面 - JSON 数据一次性迁移到 SQLite
启动时自动检测：SQLite 无数据且 JSON 文件存在时执行迁移，
成功后将 JSON 重命名为 *.json.bak 防止重复迁移。
"""
import json
import logging
import uuid
from datetime import datetime

from app.config import DATA_DIR
from app.core import db

logger = logging.getLogger("jianziruyang.migrate")


def _load_json(path) -> list | dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, IOError) as e:
        logger.warning("读取 %s 失败: %s", path, e)
        return None


def _backup(path):
    """迁移成功后将 JSON 重命名为 .bak"""
    if path.exists():
        path.rename(path.with_suffix(path.suffix + ".bak"))


def migrate_users() -> int:
    path = DATA_DIR / "users.json"
    users = _load_json(path)
    if not users:
        return 0
    rows = [
        (
            u["id"], u["username"], u["password_hash"], u["salt"],
            u.get("hash_algo", "pbkdf2"), u.get("contact", ""),
            u.get("role", "user"), 1 if u.get("is_active", True) else 0,
            u.get("created_at", 0), u.get("updated_at", 0), None,
        )
        for u in users
    ]
    db.executemany(
        "INSERT OR IGNORE INTO users (id, username, password_hash, salt, hash_algo,"
        " contact, role, is_active, created_at, updated_at, last_login_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    _backup(path)
    return len(rows)


def migrate_chat_sessions() -> int:
    path = DATA_DIR / "chat_sessions.json"
    data = _load_json(path)
    if not data:
        return 0
    sessions = data.get("sessions", [])
    session_rows = []
    message_rows = []
    for s in sessions:
        session_rows.append((
            s["session_id"], s.get("user_id", ""), s.get("title", "新对话"),
            s.get("created_at", ""), s.get("updated_at", s.get("created_at", "")),
        ))
        for seq, m in enumerate(s.get("messages", [])):
            message_rows.append((
                # 旧数据无 message_id，补生成
                m.get("message_id") or str(uuid.uuid4()),
                s["session_id"], seq,
                m.get("role", "user"), m.get("content", ""),
                json.dumps(m.get("sources", []), ensure_ascii=False),
                json.dumps(m.get("search_results", []), ensure_ascii=False),
                1 if m.get("use_knowledge") else 0,
                1 if m.get("use_search") else 0,
                s.get("created_at", datetime.now().isoformat()),
            ))
    db.executemany(
        "INSERT OR IGNORE INTO chat_sessions (session_id, user_id, title, created_at, updated_at)"
        " VALUES (?,?,?,?,?)",
        session_rows,
    )
    db.executemany(
        "INSERT OR IGNORE INTO chat_messages (message_id, session_id, seq, role, content,"
        " sources, search_results, use_knowledge, use_search, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        message_rows,
    )
    _backup(path)
    return len(session_rows)


def migrate_upload_records() -> int:
    path = DATA_DIR / "upload_records.json"
    records = _load_json(path)
    if not records:
        return 0
    rows = [
        (
            r["id"], r.get("filename", ""), r.get("image_path", ""),
            r.get("ocr_text", ""), r.get("ocr_provider", "local"),
            r.get("chunk_count", 0),
            json.dumps(r.get("doc_ids", []), ensure_ascii=False),
            r.get("visibility", "public"), r.get("owner_id", ""),
            r.get("source_type", "image"),
            json.dumps(r.get("pages", []), ensure_ascii=False),
            r.get("pdf_preview_path"),
            r.get("created_at", ""), r.get("updated_at", r.get("created_at", "")),
        )
        for r in records
    ]
    db.executemany(
        "INSERT OR IGNORE INTO upload_records (id, filename, image_path, ocr_text,"
        " ocr_provider, chunk_count, doc_ids, visibility, owner_id, source_type,"
        " pages, pdf_preview_path, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    _backup(path)
    return len(rows)


def migrate_bookmarks() -> int:
    path = DATA_DIR / "bookmarks.json"
    items = _load_json(path)
    if not items:
        return 0
    rows = [
        (
            b["id"], b["user_id"], b.get("title", "未命名收藏"),
            json.dumps(b.get("tags", []), ensure_ascii=False),
            b.get("content", ""), b.get("source_type", "knowledge"),
            json.dumps(b.get("source_info", {}), ensure_ascii=False),
            b.get("created_at", 0),
        )
        for b in items
    ]
    db.executemany(
        "INSERT OR IGNORE INTO bookmarks (id, user_id, title, tags, content,"
        " source_type, source_info, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    _backup(path)
    return len(rows)


def run_migration() -> bool:
    """执行一次性迁移。返回是否发生了迁移。"""
    # 已有用户数据则视为已迁移（跳过）
    if db.query_one("SELECT id FROM users LIMIT 1"):
        return False
    if not (DATA_DIR / "users.json").exists():
        return False

    logger.info("检测到 JSON 存量数据，开始迁移到 SQLite...")
    n_users = migrate_users()
    n_sessions = migrate_chat_sessions()
    n_records = migrate_upload_records()
    n_bookmarks = migrate_bookmarks()
    logger.info(
        "迁移完成: 用户 %d, 会话 %d, 上传记录 %d, 收藏 %d",
        n_users, n_sessions, n_records, n_bookmarks,
    )
    return True
