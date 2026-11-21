"""个人工具的独立迁移；草稿、不可变版本、测试、执行及凭据按属主隔离。"""
from datetime import datetime, timezone


def migrate(conn):
    migrate_tools(conn)
    version = "personal_tool_execution_origin_v1"
    if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
        conn.execute("ALTER TABLE personal_tool_executions ADD COLUMN result_origin TEXT NOT NULL DEFAULT 'unknown'")
        conn.execute("INSERT INTO schema_migrations VALUES (?,?)", (version, datetime.now(timezone.utc).isoformat()))
    version = "personal_tool_skill_parent_v1"
    if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
        conn.execute("ALTER TABLE personal_tool_drafts ADD COLUMN parent_skill_draft_id TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE personal_tool_drafts ADD COLUMN skill_requirement TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE personal_tool_drafts SET parent_skill_draft_id=COALESCE((SELECT l.draft_id FROM skill_tool_links l "
                     "WHERE l.owner_id=personal_tool_drafts.owner_id AND l.tool_draft_id=personal_tool_drafts.id LIMIT 1),'')")
        conn.execute("INSERT INTO schema_migrations VALUES (?,?)", (version, datetime.now(timezone.utc).isoformat()))
    version = "tool_eval_v1"
    if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
        # 工具标准化评测：样例测试通过后自动构建 kind='tool' 评测集并运行，
        # 全部用例通过（eval_status='passed'）才进入标准化工具注册表。
        for col in ("eval_status", "eval_dataset_id", "eval_run_id"):
            conn.execute(f"ALTER TABLE personal_tools ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        conn.execute("INSERT INTO schema_migrations VALUES (?,?)", (version, datetime.now(timezone.utc).isoformat()))
    version = "personal_tool_sharing_v1"
    if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
        # 工具默认全员可调用：blocked_users 为按工具的用户黑名单（JSON 数组）；
        # 执行记录保留属主 owner_id，另记实际调用者 caller_id 供审计与取消授权。
        conn.execute("ALTER TABLE personal_tools ADD COLUMN blocked_users TEXT NOT NULL DEFAULT '[]'")
        conn.execute("ALTER TABLE personal_tool_executions ADD COLUMN caller_id TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE personal_tool_executions SET caller_id=owner_id WHERE caller_id=''")
        conn.execute("INSERT INTO schema_migrations VALUES (?,?)", (version, datetime.now(timezone.utc).isoformat()))
    version = "personal_tool_naming_v1"
    if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
        # 功能名以功能命名为主：去掉版本表 function_name 的 UNIQUE（改为应用层当前版本唯一，
        # 功能名跨版本稳定），并把历史 pt_ 随机码名迁移为功能名（连接器内置功能名/声明标识/执行器兜底）。
        ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='personal_tool_versions'").fetchone()
        if ddl and "UNIQUE" in ddl[0]:
            conn.execute("ALTER TABLE personal_tool_versions RENAME TO personal_tool_versions_old")
            conn.execute("""CREATE TABLE personal_tool_versions (
                tool_id TEXT NOT NULL, owner_id TEXT NOT NULL, version TEXT NOT NULL,
                function_name TEXT NOT NULL, spec_json TEXT NOT NULL, review_json TEXT NOT NULL,
                approved_by TEXT NOT NULL DEFAULT '', approved_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, PRIMARY KEY(owner_id,tool_id,version),
                FOREIGN KEY(owner_id,tool_id) REFERENCES personal_tools(owner_id,id) ON DELETE CASCADE)""")
            conn.execute("INSERT INTO personal_tool_versions SELECT * FROM personal_tool_versions_old")
            conn.execute("DROP TABLE personal_tool_versions_old")
        import json
        from app.services import personal_tool_specs as specs
        from app.services.tools import base as tools_base
        if not tools_base.all_tools():
            try:
                from app.services.tools import bootstrap_registry
                bootstrap_registry()
            except Exception:
                pass
        used = {t["name"] for t in tools_base.registry_snapshot()}
        rows = conn.execute("SELECT t.id, v.spec_json, v.function_name FROM personal_tools t "
                            "JOIN personal_tool_versions v ON v.owner_id=t.owner_id AND v.tool_id=t.id "
                            "AND v.version=t.current_version ORDER BY t.created_at").fetchall()
        for r in rows:
            old = r["function_name"]
            if not specs.LEGACY_FUNCTION_NAME_RE.fullmatch(old):
                used.add(old)
                continue
            try:
                spec = json.loads(r["spec_json"])
            except ValueError:
                spec = {}
            base = specs.derive_function_name(spec) or ("http_api_reader" if spec.get("executor") == "http" else "python_compute")
            name, suffix = base[:60], 1
            while name in used:
                suffix += 1
                name = f"{base[:56]}_{suffix}"
            used.add(name)
            conn.execute("UPDATE personal_tool_versions SET function_name=? WHERE tool_id=?", (name, r["id"]))
        conn.execute("INSERT INTO schema_migrations VALUES (?,?)", (version, datetime.now(timezone.utc).isoformat()))


def migrate_tools(conn):
    version = "personal_tools_v1"
    if conn.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
        return
    statements = [
        """CREATE TABLE personal_tools (
            id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL,
            current_version TEXT NOT NULL, status TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
            credential_id TEXT NOT NULL DEFAULT '', terms_accepted INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(owner_id,id))""",
        "CREATE INDEX idx_personal_tools_owner ON personal_tools(owner_id,status)",
        """CREATE TABLE personal_tool_versions (
            tool_id TEXT NOT NULL, owner_id TEXT NOT NULL, version TEXT NOT NULL,
            function_name TEXT NOT NULL, spec_json TEXT NOT NULL, review_json TEXT NOT NULL,
            approved_by TEXT NOT NULL DEFAULT '', approved_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, PRIMARY KEY(owner_id,tool_id,version),
            FOREIGN KEY(owner_id,tool_id) REFERENCES personal_tools(owner_id,id) ON DELETE CASCADE)""",
        """CREATE TABLE personal_tool_drafts (
            id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, session_id TEXT NOT NULL, message_id TEXT NOT NULL,
            tool_id TEXT NOT NULL DEFAULT '', spec_json TEXT NOT NULL, content_hash TEXT NOT NULL,
            review_json TEXT NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL)""",
        "CREATE INDEX idx_personal_tool_drafts_turn ON personal_tool_drafts(owner_id,session_id,message_id)",
        """CREATE TABLE personal_tool_tests (
            id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, tool_id TEXT NOT NULL, version TEXT NOT NULL,
            status TEXT NOT NULL, report_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, finished_at TEXT NOT NULL DEFAULT '')""",
        "CREATE INDEX idx_personal_tool_tests_owner ON personal_tool_tests(owner_id,tool_id,created_at)",
        """CREATE TABLE personal_tool_executions (
            id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, tool_id TEXT NOT NULL, version TEXT NOT NULL,
            session_id TEXT NOT NULL, message_id TEXT NOT NULL, test_id TEXT NOT NULL DEFAULT '',
            args_hash TEXT NOT NULL, execution_status TEXT NOT NULL, validation_status TEXT NOT NULL,
            requirement_status TEXT NOT NULL, error_code TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, finished_at TEXT NOT NULL DEFAULT '')""",
        "CREATE INDEX idx_personal_tool_executions_owner ON personal_tool_executions(owner_id,tool_id,created_at)",
        """CREATE TABLE personal_tool_credentials (
            id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, tool_id TEXT NOT NULL, version TEXT NOT NULL,
            encrypted_value TEXT NOT NULL, created_at TEXT NOT NULL)""",
        """CREATE TABLE tool_result_cache (
            cache_key TEXT PRIMARY KEY, owner_id TEXT NOT NULL, tool_id TEXT NOT NULL, version TEXT NOT NULL,
            result_json TEXT NOT NULL, generated_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            hit_count INTEGER NOT NULL DEFAULT 0, last_access_at TEXT NOT NULL)""",
        "CREATE INDEX idx_tool_result_cache_owner ON tool_result_cache(owner_id,tool_id,expires_at)",
    ]
    for statement in statements:
        conn.execute(statement)
    conn.execute("INSERT INTO schema_migrations VALUES (?,?)", (version, datetime.now(timezone.utc).isoformat()))
