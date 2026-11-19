"""网关原生工具能力的独立迁移；存量模型默认未声明，不猜测能力。"""
from datetime import datetime


def migrate(conn):
    version = "gateway_native_tools_v1"
    if conn.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
        return
    columns = {r[1] for r in conn.execute("PRAGMA table_info(gateway_upstreams)")}
    if "tool_models" not in columns:
        conn.execute("ALTER TABLE gateway_upstreams ADD COLUMN tool_models TEXT NOT NULL DEFAULT '[]'")
    conn.execute("INSERT INTO schema_migrations VALUES (?,?)", (version, datetime.now().isoformat()))
