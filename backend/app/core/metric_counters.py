"""持久化累计指标：触发器与业务终态写入共用 SQLite 事务。"""

_FIELDS = {
    "agent": ("agent_traces", ("prompt_tokens", "completion_tokens", "llm_calls", "tool_calls")),
    "gateway": ("gateway_logs", ("prompt_tokens", "completion_tokens")),
}


def initialize(conn):
    """仅在版本迁移事务内调用；以现存历史建立一次性基线。"""
    conn.execute("""CREATE TABLE IF NOT EXISTS metric_counters (
        scope TEXT NOT NULL, metric TEXT NOT NULL, status TEXT NOT NULL DEFAULT '',
        value INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(scope,metric,status)
    )""")
    for scope, (table, fields) in _FIELDS.items():
        where = " WHERE status != 'running'" if scope == "agent" else ""
        for row in conn.execute(f"SELECT status, COUNT(*) FROM {table}{where} GROUP BY status").fetchall():
            conn.execute("INSERT INTO metric_counters VALUES (?,?,?,?)", (scope, "requests", row[0], row[1]))
        for field in fields:
            value = conn.execute(f"SELECT COALESCE(SUM({field}),0) FROM {table}{where}").fetchone()[0]
            conn.execute("INSERT INTO metric_counters VALUES (?,?,?,?)", (scope, field, "", value))
        increments = [("requests", "NEW.status", "1")]
        increments.extend((field, "''", f"MAX(0,COALESCE(NEW.{field},0))") for field in fields)
        statements = "\n".join(
            f"INSERT INTO metric_counters(scope,metric,status,value) VALUES "
            f"('{scope}','{metric}',{status},{value}) ON CONFLICT(scope,metric,status) "
            "DO UPDATE SET value=metric_counters.value+excluded.value;"
            for metric, status, value in increments
        )
        condition = "WHEN NEW.status != 'running'" if scope == "agent" else ""
        conn.execute(f"CREATE TRIGGER metric_{scope}_insert AFTER INSERT ON {table} {condition} BEGIN {statements} END")
        if scope == "agent":
            conn.execute(f"""CREATE TRIGGER metric_agent_finish AFTER UPDATE ON {table}
                WHEN OLD.status='running' AND NEW.status!='running' BEGIN {statements} END""")


def read(scope):
    from app.core import db
    rows = db.query("SELECT metric,status,value FROM metric_counters WHERE scope=?", (scope,))
    return {(row["metric"], row["status"]): int(row["value"]) for row in rows}
