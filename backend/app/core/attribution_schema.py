"""
见字如面 - 全链路归属与前端同步的增量迁移（独立版本门控，不改旧迁移）

call_attribution_v1：
- gateway_logs 补归属列：user_id/billing_user_id/trace_id/span_id/job_id/origin/
  attribution_source/usage_event_id。存量行保持空值=「历史未归属」，不做无证据回填；
- usage_event_id 部分唯一索引：同一用量事件幂等落库（重试不重复计量）。

sync_events_v1：
- resource_revisions：资源域版本号（事务内 +1，前端缓存失效依据）；
- resource_change_events：变更事件流水（SSE /api/sync/events 的补发窗口，24h 清理）。

user_default_keys_v1：
- user_default_keys：用户→默认出网密钥唯一映射（注册同事务创建）；
- 存量回填：单一绑定设默认、多绑定沿用最早有效、从未绑定才新建独立密钥；
  不重签现有密钥，仅登记映射。

sync_triggers_v1：
- 资源表变更触发器：业务写入与版本 bump/变更事件在同一条 SQL 事务内原子完成，
  跨进程（独立 Proxy/后台任务）一致进入同一记录源，不依赖进程内事件；
- 事件不携带业务正文/秘密，仅资源域+版本+授权范围（SSE 补发窗口 24h）。
"""
import logging
from datetime import datetime

logger = logging.getLogger("jianziruyang.db.attribution")


def migrate(conn) -> None:
    if not conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version='call_attribution_v1'").fetchone():
        cols = {r[1] for r in conn.execute("PRAGMA table_info(gateway_logs)")}
        for col in ("user_id", "billing_user_id", "trace_id", "span_id",
                    "job_id", "origin", "attribution_source", "usage_event_id"):
            if col not in cols:
                conn.execute(f"ALTER TABLE gateway_logs ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
                logger.info("gateway_logs 补加列: %s", col)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gateway_logs_user"
                     " ON gateway_logs(user_id, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gateway_logs_trace"
                     " ON gateway_logs(trace_id)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_gateway_logs_usage_event"
                     " ON gateway_logs(usage_event_id) WHERE usage_event_id <> ''")
        conn.execute("INSERT INTO schema_migrations VALUES ('call_attribution_v1',?)",
                     (datetime.now().isoformat(),))
    if not conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version='sync_events_v1'").fetchone():
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resource_revisions (
                resource   TEXT PRIMARY KEY,
                version    INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resource_change_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                resource   TEXT NOT NULL,
                version    INTEGER NOT NULL DEFAULT 0,
                scope      TEXT NOT NULL DEFAULT '',
                user_id    TEXT NOT NULL DEFAULT '',
                payload    TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_resource_events_resource"
                     " ON resource_change_events(resource, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_resource_events_created"
                     " ON resource_change_events(created_at)")
        conn.execute("INSERT INTO schema_migrations VALUES ('sync_events_v1',?)",
                     (datetime.now().isoformat(),))
    if not conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version='user_default_keys_v1'").fetchone():
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_default_keys (
                user_id    TEXT PRIMARY KEY,
                key_id     TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_default_keys_key"
                     " ON user_default_keys(key_id)")
        # 存量用户回填默认密钥：沿用现有有效绑定（不重签），从未绑定才新建
        from app.services.gateway import keys as keys_svc
        for u in conn.execute("SELECT id, username FROM users").fetchall():
            keys_svc._ensure_default_conn(conn, str(u["id"]), u["username"] or "")
        conn.execute("INSERT INTO schema_migrations VALUES ('user_default_keys_v1',?)",
                     (datetime.now().isoformat(),))
    if not conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version='sync_triggers_v1'").fetchone():
        _install_sync_triggers(conn)
        conn.execute("INSERT INTO schema_migrations VALUES ('sync_triggers_v1',?)",
                     (datetime.now().isoformat(),))


# 资源域 →（授权范围，触发表清单）：global=全体登录用户可见，ops=管理员/运维，
# user=仅属主（会话类单独处理，携带 user_id）。高频监控类（usage）由 SSE 端
# 按资源合并限频（≤1 次/秒），不逐条推送。
_SYNC_RESOURCES = {
    "tools": ("global", ["personal_tools", "personal_tool_versions",
                          "personal_tool_drafts", "personal_tool_credentials"]),
    "skills": ("global", ["user_skills", "skill_drafts",
                           "skill_package_versions", "skill_tool_bindings"]),
    "knowledge": ("global", ["knowledge_bases", "upload_records",
                              "url_imports", "pending_uploads"]),
    "wiki": ("global", ["wiki_pages", "wiki_compile_queue"]),
    "gateway": ("ops", ["gateway_keys", "gateway_rules", "gateway_upstreams",
                         "gateway_role_routes", "gateway_settings",
                         "gateway_channels", "gateway_models"]),
    "users": ("ops", ["users"]),
    "alerts": ("ops", ["gateway_security_events", "obs_alerts"]),
    "usage": ("ops", ["gateway_logs", "usage_daily"]),
}

_NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%f','now','localtime')"


def _bump_sql(resource: str, scope: str, user_expr: str) -> str:
    """触发器体：版本 +1 与变更事件同事务原子落库（不携带业务正文）。"""
    return (
        f"INSERT INTO resource_revisions(resource, version, updated_at)"
        f" VALUES('{resource}', 1, {_NOW_SQL})"
        f" ON CONFLICT(resource) DO UPDATE SET version=version+1,"
        f" updated_at=excluded.updated_at;"
        f"INSERT INTO resource_change_events(resource, version, scope, user_id, payload, created_at)"
        f" VALUES('{resource}', (SELECT version FROM resource_revisions WHERE resource='{resource}'),"
        f" '{scope}', {user_expr}, '{{}}', {_NOW_SQL});"
    )


def _install_sync_triggers(conn) -> None:
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for resource, (scope, tables) in _SYNC_RESOURCES.items():
        for table in tables:
            if table not in existing:
                continue
            for op, suffix in (("INSERT", "ai"), ("UPDATE", "au"), ("DELETE", "ad")):
                body = _bump_sql(resource, scope, "''")
                conn.execute(
                    f"CREATE TRIGGER IF NOT EXISTS sync_{table}_{suffix}"
                    f" AFTER {op} ON {table} BEGIN {body} END")
    # 会话类（user 范围）：属主随行携带，跨用户不可见他人会话变更
    if "chat_sessions" in existing:
        for op, suffix, ref in (("INSERT", "ai", "NEW"), ("UPDATE", "au", "NEW"),
                                ("DELETE", "ad", "OLD")):
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS sync_chat_sessions_{suffix}"
                f" AFTER {op} ON chat_sessions BEGIN"
                f" {_bump_sql('sessions', 'user', f'{ref}.user_id')} END")
    if "chat_messages" in existing:
        # 消息表无 user_id：经会话反查属主（会话已删时兜底空串，避免 NOT NULL 违约）；
        # 仅插入/删除触发（更新不改变会话列表摘要）
        for op, suffix, ref in (("INSERT", "ai", "NEW"), ("DELETE", "ad", "OLD")):
            owner_expr = ("COALESCE((SELECT user_id FROM chat_sessions"
                          f" WHERE session_id={ref}.session_id), '')")
            body = _bump_sql("sessions", "user", owner_expr)
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS sync_chat_messages_{suffix}"
                f" AFTER {op} ON chat_messages BEGIN {body} END")
    logger.info("资源变更同步触发器安装完成（sync_triggers_v1）")