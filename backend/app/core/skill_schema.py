"""Skill 包及原轮次草稿的独立版本迁移，保留旧 prompt/playbook。"""
from datetime import datetime
import hashlib
import json


def migrate_gap_governance(conn):
    version = "capability_suggestions_v2"
    if conn.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
        return
    columns = {r[1] for r in conn.execute("PRAGMA table_info(capability_gaps)")}
    for name in ("intent_key", "missing_capability", "group_key", "request_key", "failure_reason"):
        if name not in columns:
            conn.execute(f"ALTER TABLE capability_gaps ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE INDEX idx_cap_gaps_owner_group ON capability_gaps(user_id,group_key,created_at)")
    conn.execute("""CREATE TABLE capability_suggestions (
        owner_id TEXT NOT NULL, group_key TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
        last_prompted_at TEXT NOT NULL DEFAULT '', draft_id TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '', message_id TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT '', PRIMARY KEY(owner_id,group_key))""")
    conn.execute("INSERT INTO schema_migrations VALUES (?,?)", (version, datetime.now().isoformat()))


def migrate(conn):
    migrate_gap_governance(conn)
    migrate_packages(conn)
    migrate_dependencies(conn)
    migrate_acceptance(conn)


def migrate_acceptance(conn):
    version = "capability_acceptance_v1"
    if conn.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
        return
    conn.execute("""CREATE TABLE capability_acceptance_evidence (
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, group_key TEXT NOT NULL,
        draft_id TEXT NOT NULL, skill_id TEXT NOT NULL, identity_json TEXT NOT NULL,
        session_id TEXT NOT NULL, user_message_id TEXT NOT NULL, message_id TEXT NOT NULL,
        query_hash TEXT NOT NULL, answer_hash TEXT NOT NULL, execution_ids_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, confirmed_at TEXT NOT NULL DEFAULT '',
        UNIQUE(owner_id,group_key,message_id))""")
    conn.execute("CREATE INDEX idx_capability_acceptance_owner ON capability_acceptance_evidence(owner_id,group_key,created_at)")
    conn.execute("INSERT INTO schema_migrations VALUES (?,?)", (version, datetime.now().isoformat()))


def migrate_dependencies(conn):
    version = "skill_tool_dependencies_v1"
    if conn.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
        return
    conn.execute("""CREATE TABLE skill_tool_links (
        owner_id TEXT NOT NULL, draft_id TEXT NOT NULL REFERENCES skill_drafts(id) ON DELETE CASCADE,
        requirement TEXT NOT NULL, tool_draft_id TEXT NOT NULL DEFAULT '',
        tool_id TEXT NOT NULL DEFAULT '', tool_version TEXT NOT NULL DEFAULT '', tool_hash TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(owner_id,draft_id,requirement))""")
    conn.execute("""CREATE TABLE skill_tool_bindings (
        owner_id TEXT NOT NULL, skill_id TEXT NOT NULL, package_version TEXT NOT NULL,
        requirement TEXT NOT NULL, tool_id TEXT NOT NULL, tool_version TEXT NOT NULL, tool_hash TEXT NOT NULL,
        PRIMARY KEY(owner_id,skill_id,package_version,requirement),
        FOREIGN KEY(owner_id,skill_id,package_version)
            REFERENCES skill_package_versions(owner_id,skill_id,version) ON DELETE CASCADE)""")
    conn.execute("INSERT INTO schema_migrations VALUES (?,?)", (version, datetime.now().isoformat()))


def migrate_packages(conn):
    version = "skill_packages_v1"
    if conn.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
        return
    columns = {r[1] for r in conn.execute("PRAGMA table_info(skill_drafts)")}
    for name, definition in {
        "message_id": "TEXT NOT NULL DEFAULT ''",
        "kind": "TEXT NOT NULL DEFAULT 'prompt'",
        "package_json": "TEXT NOT NULL DEFAULT '{}'",
        "content_hash": "TEXT NOT NULL DEFAULT ''",
        "review_json": "TEXT NOT NULL DEFAULT '{}'",
    }.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE skill_drafts ADD COLUMN {name} {definition}")
    conn.execute("CREATE INDEX idx_skill_drafts_message ON skill_drafts(owner_id,session_id,message_id)")
    conn.execute("""CREATE TABLE skill_package_versions (
        owner_id TEXT NOT NULL, skill_id TEXT NOT NULL REFERENCES user_skills(id) ON DELETE CASCADE,
        version TEXT NOT NULL, slug TEXT NOT NULL, package_json TEXT NOT NULL,
        review_json TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(owner_id,skill_id,version))""")
    conn.execute("""CREATE TABLE skill_package_files (
        owner_id TEXT NOT NULL, skill_id TEXT NOT NULL, version TEXT NOT NULL,
        path TEXT NOT NULL, content BLOB NOT NULL, content_hash TEXT NOT NULL,
        PRIMARY KEY(owner_id,skill_id,version,path),
        FOREIGN KEY(owner_id,skill_id,version)
            REFERENCES skill_package_versions(owner_id,skill_id,version) ON DELETE CASCADE)""")
    for row in conn.execute("SELECT id,owner_id,session_id,name,description,content,created_at FROM skill_drafts").fetchall():
        rid, owner, session, name, description, content, created = tuple(row)
        value = {"name": name, "description": description, "content": content, "kind": "prompt", "package": {}}
        fingerprint = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                               separators=(",", ":")).encode()).hexdigest()
        # 旧草稿使用创建时间定位随后第一条助手消息，无法定位则留空，不挂到最新轮次。
        message = conn.execute(
            "SELECT m.message_id FROM chat_messages m JOIN chat_sessions s ON s.session_id=m.session_id "
            "WHERE m.session_id=? AND s.user_id=? AND m.role='assistant' AND m.created_at>=? "
            "ORDER BY m.created_at LIMIT 1", (session, owner, created)).fetchone()
        conn.execute("UPDATE skill_drafts SET content_hash=?,message_id=? WHERE id=?",
                     (fingerprint, message[0] if message else "", rid))
    conn.execute("INSERT INTO schema_migrations VALUES (?,?)", (version, datetime.now().isoformat()))
