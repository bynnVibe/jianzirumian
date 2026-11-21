"""受控答案缓存独立迁移；不读取或搬运旧答案。"""
from datetime import datetime


def migrate(conn):
    version = "answer_cache_v2"
    if conn.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
        return
    conn.execute("""CREATE TABLE semantic_cache_v2 (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, question TEXT NOT NULL,
        question_norm TEXT NOT NULL, answer TEXT NOT NULL, sources TEXT NOT NULL DEFAULT '[]',
        embedding TEXT NOT NULL DEFAULT '[]', hit_count INTEGER NOT NULL DEFAULT 0,
        scope TEXT NOT NULL DEFAULT 'personal', is_realtime INTEGER NOT NULL DEFAULT 0,
        cache_kind TEXT NOT NULL, fingerprint TEXT NOT NULL, evidence_hash TEXT NOT NULL,
        revisions TEXT NOT NULL, model TEXT NOT NULL, upstream_id TEXT NOT NULL,
        expires_at TEXT NOT NULL, created_at TEXT NOT NULL, last_used_at TEXT NOT NULL,
        UNIQUE(user_id, fingerprint, question_norm))""")
    conn.execute("CREATE INDEX idx_cache_v2_lookup ON semantic_cache_v2(user_id, fingerprint)")
    conn.execute("CREATE TABLE cache_revisions (kind TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0)")
    for kind in ("knowledge", "permissions", "context"):
        conn.execute("INSERT INTO cache_revisions(kind) VALUES (?)", (kind,))
    # 固定表名白名单；跨进程写入亦触发版本变更，不依赖应用层是否记得失效。
    tables = {
        "upload_records": ("knowledge", None),
        "knowledge_bases": ("knowledge", None),
        "wiki_pages": ("knowledge", None),
        "users": ("permissions", "role,is_active"),
        "user_skills": ("context", "content,enabled"),
        "user_instructions": ("context", "content"),
        "user_memories": ("context", "content"),
        "agent_memories": ("context", "content"),
    }
    for table, (kind, columns) in tables.items():
        for event in ("INSERT", "UPDATE", "DELETE"):
            update = f"UPDATE OF {columns}" if event == "UPDATE" and columns else event
            conn.execute(f"""CREATE TRIGGER cache_revision_{table}_{event.lower()}
                AFTER {update} ON {table} BEGIN
                UPDATE cache_revisions SET revision=revision+1 WHERE kind='{kind}'; END""")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(semantic_cache_events)")}
    for name, definition in {
        "outcome": "TEXT NOT NULL DEFAULT 'legacy'",
        "reason": "TEXT NOT NULL DEFAULT ''",
        "validation_ms": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE semantic_cache_events ADD COLUMN {name} {definition}")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_messages)")}
    if "cache_info" not in cols:
        conn.execute("ALTER TABLE chat_messages ADD COLUMN cache_info TEXT NOT NULL DEFAULT '{}'")
    conn.execute("INSERT INTO schema_migrations VALUES (?,?)", (version, datetime.now().isoformat()))


def migrate_profile(conn):
    """整改2：常驻画像与缓存复用解耦。

    1) semantic_cache_v2 增加 profile_hash：同用户画像稳定时允许复用，
       画像变更后旧条目不再命中（探针与指纹均校验）。
    2) 移除 user_memories 上的 context 版本触发器：每轮对话后台记忆抽取/
       checkpoint 留痕都会写 user_memories，旧触发器使全体用户缓存条目
       在下一轮即失效（高相似重复提问永远不命中）。个性化安全改由
       查询相关记忆门禁 + profile_hash 精确保证，不再依赖粗粒度全局版本。
    """
    version = "semcache_profile_v1"
    if conn.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(semantic_cache_v2)")}
    if "profile_hash" not in cols:
        conn.execute("ALTER TABLE semantic_cache_v2 ADD COLUMN profile_hash TEXT NOT NULL DEFAULT ''")
    for event in ("insert", "update", "delete"):
        conn.execute(f"DROP TRIGGER IF EXISTS cache_revision_user_memories_{event}")
    conn.execute("INSERT INTO schema_migrations VALUES (?,?)", (version, datetime.now().isoformat()))


def migrate_v3(conn):
    """语义缓存 v3：归一化双键匹配 + 文档级依赖失效 + 用户稳定上下文哈希。

    1) search_query/query_embedding：意图改写后的归一化查询及其向量，
       与原始问题向量组成双键匹配（同义改述在归一化键上相似度显著更高）。
    2) deps：写入时来源文档的版本指纹（upload_records.updated_at /
       wiki_pages.updated_at+source_hash / 知识库可见性快照）。读侧仅当
       被引用文档自身变更/删除/权限变化时失效，取代全局 knowledge 版本
       与 FAISS 文件 mtime 耦合（任意上传不再清空全部缓存）。
    3) skills_hash：用户 Skills+指令的稳定上下文哈希，取代「有 Skills
       就整体旁路探针」的过严门禁（与 profile_hash 同理）。
    """
    version = "semcache_v3"
    if conn.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(semantic_cache_v2)")}
    for name, definition in {
        "search_query": "TEXT NOT NULL DEFAULT ''",
        "query_embedding": "TEXT NOT NULL DEFAULT '[]'",
        "skills_hash": "TEXT NOT NULL DEFAULT ''",
        "deps": "TEXT NOT NULL DEFAULT '{}'",
    }.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE semantic_cache_v2 ADD COLUMN {name} {definition}")
    conn.execute("INSERT INTO schema_migrations VALUES (?,?)", (version, datetime.now().isoformat()))
