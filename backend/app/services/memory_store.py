"""
见字如面 - 记忆 Markdown 持久层（Claude Code 五层记忆存储架构，剔除 Team 层）

五层存储分级（自下而上叠加注入 system prompt，对齐 Claude Code Managed/User/Project/Local/AutoMem）：
  Managed  <memory_root>/managed/MANAGED.md        平台强制层：仅管理员可写，公司级强制策略
  User     <memory_root>/users/<uid>/USER.md       个人全局层：用户跨知识库/会话生效（user_instructions 镜像）
  Project  <memory_root>/kb/<kb_id>/PROJECT.md     项目共享层：知识库级规则，KB 属主/管理员可写
  Local    <memory_root>/users/<uid>/local/<kb_id>.md  本机私有层：用户在某知识库下的私人笔记，仅本人
  AutoMem  <memory_root>/users/<uid>/auto/         AI 自学层：MEMORY.md 索引 + 每记忆独立 topic .md

设计原则（融合 Claude Code 原始设计与小林 coding 图解）：
- 记忆都由 markdown 文件记录和持续演进：文件是持久记录载体（人读/管理/扩展），
  SQLite 仅作查询镜像（评分/检索/ UI /可观测性），写路径双写、恢复路径文件优先；
- 写入路径：对话结束（无 tool call）→ stopHook 等价后台任务 fork extract 代理 →
  更新/新增 topic 记忆文件 → 重建索引 MEMORY.md（每条记忆单独成文件，索引行含链接与描述）；
- 读取路径：静态层（Managed→User→Project→Local）组装 system prompt +
  AutoMem 常驻画像卡 + findRelevantMemories 两层过滤 Top-K（最多 5 条）；
- 记忆根目录与 SQLite 文件同址（backend/data/memory），隔离测试/内存 DB 不落盘，
  避免测试污染真实数据；会话级 SESSION.md 由上下文压缩链 SessionMemoryCompact 写入。
"""
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.core import db

logger = logging.getLogger("jianziruyang.memory_store")

# 五层存储层名（API/观测同源）
LAYERS = ("managed", "user", "project", "local", "auto")
LAYER_LABELS = {
    "managed": "平台强制", "user": "个人全局", "project": "项目共享",
    "local": "本地私有", "auto": "AI 自学",
}

# Managed 层默认平台策略（种子化，管理员可演进）
_MANAGED_SEED = (
    "# 平台记忆策略（Managed）\n\n"
    "- 回答涉及健康/医疗内容时须标注信息来源，不做诊断性结论。\n"
    "- 不记录密码、密钥、证件号等敏感信息；用户最新表述优先于陈旧记忆。\n"
    "- 记忆仅用于提升回答相关性，不得跨用户泄露私人记忆内容。\n"
)

# 迁移期间 base_dir 的临时覆盖（migrate_from_conn 传入的连接可能早于全局 _conn 建立）
_override_base: Optional[Path] = None


def base_dir() -> Optional[Path]:
    """记忆根目录：与 SQLite 文件同址的 memory/ 目录；内存 DB（测试）返回 None 不落盘。"""
    if _override_base is not None:
        return _override_base
    try:
        conn = db.get_conn()
        row = conn.execute("PRAGMA database_list").fetchone()
        file = row[2] if row else ""
        if not file or file == ":memory:":
            return None
        return Path(file).parent / "memory"
    except Exception:
        return None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        return ""


def _write(path: Path, text: str):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as e:
        logger.warning("记忆文件写入失败（忽略，DB 镜像仍生效）: %s %s", path, e)


def layer_path(layer: str, user_id: str = "", kb_id: str = "") -> Optional[Path]:
    base = base_dir()
    if base is None:
        return None
    if layer == "managed":
        return base / "managed" / "MANAGED.md"
    if layer == "user":
        return base / "users" / user_id / "USER.md"
    if layer == "project":
        return base / "kb" / kb_id / "PROJECT.md"
    if layer == "local":
        return base / "users" / user_id / "local" / f"{kb_id}.md"
    if layer == "auto":
        return base / "users" / user_id / "auto" / "MEMORY.md"
    return None


def read_layer(layer: str, user_id: str = "", kb_id: str = "") -> str:
    """读取静态层 markdown 正文（Managed/User/Project/Local）；无文件返回空串。"""
    if layer == "managed":
        p = layer_path("managed")
        return _read(p) if p else ""
    if not user_id or user_id in {"guest", "system", "-"}:
        return ""
    if layer in ("project", "local") and not kb_id:
        return ""
    p = layer_path(layer, user_id, kb_id)
    return _read(p) if p else ""


def write_layer(layer: str, content: str, user_id: str = "", kb_id: str = "") -> bool:
    """写入静态层 markdown（权限校验在 API 层）；返回是否落盘。"""
    p = layer_path(layer, user_id, kb_id)
    if p is None:
        return False
    _write(p, (content or "").strip() + ("\n" if (content or "").strip() else ""))
    return True


def seed_managed():
    p = layer_path("managed")
    if p is not None and not p.is_file():
        _write(p, _MANAGED_SEED)


# 静态层单层注入字符预算（防超长 markdown 挤占上下文窗口）
MAX_LAYER_INJECT_CHARS = 1500
# 静态层单层写入字符上限（API 编辑预算；注入时再按 MAX_LAYER_INJECT_CHARS 截断）
MAX_LAYER_WRITE_CHARS = 4000


def static_layer_messages(user_id: str = "", kb_id: str = "") -> list:
    """读取路径：静态层 system 消息组装（Managed→Project→Local，自下而上叠加）。

    User 层由 user_instructions 渲染（单一事实源），此处不重复输出；
    注入顺序约定 Managed 最先（优先级最低）、Project/Local 靠后（recency bias）。
    返回 [{"segment", "content"}]，segment 供观测 note_segment 同源登记；空层不注入。
    kb_id 的可见性校验由调用方（chat/API）负责，此处仅按传入 id 读取。
    """
    out = []
    managed = read_layer("managed").strip()
    if managed:
        out.append({"segment": "memory_managed",
                    "content": f"【平台记忆策略（Managed 层，须遵循）】\n{managed[:MAX_LAYER_INJECT_CHARS]}"})
    if user_id and user_id not in {"guest", "system", "-"} and kb_id:
        project = read_layer("project", user_id=user_id, kb_id=kb_id).strip()
        if project:
            out.append({"segment": "memory_project",
                        "content": f"【知识库共享规则（Project 层）】\n{project[:MAX_LAYER_INJECT_CHARS]}"})
        local = read_layer("local", user_id=user_id, kb_id=kb_id).strip()
        if local:
            out.append({"segment": "memory_local",
                        "content": f"【我在该知识库的私人笔记（Local 层，仅本人可见）】\n{local[:MAX_LAYER_INJECT_CHARS]}"})
    return out


# ---- AutoMem：每记忆独立 topic 文件 + MEMORY.md 索引 ----

def _frontmatter(row: dict) -> str:
    keys = ("id", "memory_type", "importance", "confidence", "created_at",
            "updated_at", "last_used_at", "source_session_id")
    lines = ["---"]
    for k in keys:
        v = row.get(k)
        lines.append(f"{k}: {'' if v is None else v}")
    lines.append("---")
    return "\n".join(lines)


def parse_frontmatter(text: str) -> dict:
    """解析 topic 文件头元数据（恢复路径用）；无头返回空 dict。"""
    m = re.match(r"^---\n(.*?)\n---\n?", text or "", re.S)
    if not m:
        return {}
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta


def memory_file_name(row: dict) -> str:
    return f"{row.get('memory_type') or 'fact'}_{(row.get('id') or '')[:8]}.md"


def sync_memory(row: dict) -> str:
    """写路径：DB 镜像行 → topic markdown 文件 + 重建索引（抽取/手动共用单一写点）。

    返回相对记忆根目录的文件路径（供回填 user_memories.file_path）；未落盘返回空串。
    """
    base = base_dir()
    if base is None or not row.get("id"):
        return ""
    auto = base / "users" / row["user_id"] / "auto"
    name = memory_file_name(row)
    _write(auto / name,
           _frontmatter(row) + "\n" + (row.get("content") or "").strip() + "\n")
    rebuild_index(row["user_id"])
    return f"users/{row['user_id']}/auto/{name}"


def remove_memory(user_id: str, memory_id: str):
    base = base_dir()
    if base is None:
        return
    auto = base / "users" / user_id / "auto"
    if auto.is_dir():
        for f in auto.glob(f"*_{memory_id[:8]}.md"):
            try:
                f.unlink()
            except OSError:
                pass
    rebuild_index(user_id)


def rebuild_index(user_id: str, conn=None):
    """重建 MEMORY.md 索引：每条记忆单独成文件，索引行 `- [file](file) — 描述`。

    conn 可注入（迁移期全局连接尚未建立，直接用传入连接查询，避免递归重入）。
    """
    base = base_dir()
    if base is None:
        return
    sql = ("SELECT id, memory_type, content, updated_at FROM user_memories"
           " WHERE user_id = ? ORDER BY updated_at DESC")
    if conn is not None:
        cur = conn.execute(sql, (user_id,))
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    else:
        rows = db.query(sql, (user_id,))
    lines = ["# Memory 索引", "", "本索引用于快速定位各类记忆文件。", ""]
    for r in rows:
        name = memory_file_name(r)
        desc = (r.get("content") or "").strip().replace("\n", " ")[:60]
        lines.append(f"- [{name}]({name}) — {desc}")
    _write(base / "users" / user_id / "auto" / "MEMORY.md", "\n".join(lines) + "\n")


def auto_files(user_id: str) -> dict:
    """AutoMem 文件视图（UI/观测）：索引正文 + topic 文件清单（含元数据）。"""
    base = base_dir()
    if base is None:
        return {"index": "", "files": []}
    auto = base / "users" / user_id / "auto"
    files = []
    if auto.is_dir():
        for f in sorted(auto.glob("*.md")):
            if f.name == "MEMORY.md":
                continue
            text = _read(f)
            meta = parse_frontmatter(text)
            body = re.sub(r"^---\n.*?\n---\n?", "", text, flags=re.S).strip()
            files.append({"name": f.name, "meta": meta, "content": body,
                          "chars": len(body)})
    return {"index": _read(auto / "MEMORY.md"), "files": files}


def sync_memory_by_id(user_id: str, memory_id: str) -> str:
    """按 id 取最新 DB 镜像行并同步 topic 文件（写路径统一入口），返回相对路径。"""
    row = db.query_one(
        "SELECT * FROM user_memories WHERE id = ? AND user_id = ?", (memory_id, user_id))
    if not row:
        return ""
    return sync_memory(row)


def layers_overview(user_id: str, kb_id: str = "") -> list:
    """五层存储概览（可观测性/UI）：每层字符量/落盘状态/相对路径（只读）。"""
    base = base_dir()
    out = []
    for layer in LAYERS:
        if layer == "auto":
            info = auto_files(user_id) if user_id else {"index": "", "files": []}
            chars = len(info["index"]) + sum(f["chars"] for f in info["files"])
            exists = bool(info["files"]) or bool(info["index"])
        else:
            text = read_layer(layer, user_id, kb_id)
            chars = len(text.strip())
            exists = bool(text.strip())
        p = layer_path(layer, user_id, kb_id)
        out.append({"layer": layer, "label": LAYER_LABELS[layer], "chars": chars,
                    "exists": exists,
                    "path": str(p.relative_to(base)) if (p is not None and base) else ""})
    return out


# ---- 静态层镜像同步 ----

def sync_instruction(user_id: str, content: str):
    """User 层：user_instructions DB 写入后同步 USER.md（markdown 为持久记录）。"""
    if user_id and user_id not in {"guest", "system", "-"}:
        write_layer("user", content, user_id=user_id)


def sync_agent_memory(user_id: str, agent_key: str, conn=None):
    """助手分域记忆 markdown 镜像：单文件按 DB 行重生成（域内记忆演进可人读回查）。"""
    base = base_dir()
    if base is None:
        return
    sql = ("SELECT memory_type, content, importance, updated_at FROM agent_memories"
           " WHERE user_id = ? AND agent_key = ? ORDER BY updated_at DESC")
    if conn is not None:
        cur = conn.execute(sql, (user_id, agent_key))
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    else:
        rows = db.query(sql, (user_id, agent_key))
    lines = [f"# 助手记忆 · {agent_key}", ""]
    for r in rows:
        lines.append(f"- [{r['memory_type']}] {r['content']}（重要度 {r['importance']}，更新 {r['updated_at'][:10]}）")
    _write(base / "users" / user_id / "agents" / f"{agent_key}.md", "\n".join(lines) + "\n")


# ---- SessionMemory（上下文压缩链第三级持久载体）----

def session_memory_path(session_id: str) -> Optional[Path]:
    base = base_dir()
    return base / "sessions" / session_id / "SESSION.md" if base else None


def write_session_memory(session_id: str, user_id: str, summary: str, meta: dict):
    """SessionMemoryCompact：压缩结果落 SESSION.md（frontmatter 记覆盖点/类型/编辑标记）。"""
    p = session_memory_path(session_id)
    if p is None:
        return
    lines = ["---", f"session_id: {session_id}", f"user_id: {user_id}"]
    for k in ("covered_until_message_id", "compact_kind", "edited_by_user", "updated_at", "before_tokens"):
        lines.append(f"{k}: {meta.get(k, '')}")
    lines.append("---")
    _write(p, "\n".join(lines) + "\n" + (summary or "").strip() + "\n")


def read_session_memory(session_id: str) -> Optional[dict]:
    """恢复路径：DB 摘要缺失时从 SESSION.md 恢复（文件优先的持久记录语义）。"""
    p = session_memory_path(session_id)
    if p is None or not p.is_file():
        return None
    text = _read(p)
    meta = parse_frontmatter(text)
    body = re.sub(r"^---\n.*?\n---\n?", "", text, flags=re.S).strip()
    if not body:
        return None
    return {"session_id": session_id, "summary": body, **meta}


# ---- 观测事件（记忆写路径 / 上下文压缩链）----

def log_memory_event(user_id: str, action: str, layer: str, memory_type: str = "", detail: str = ""):
    try:
        db.execute(
            "INSERT INTO memory_write_events (id, user_id, action, layer, memory_type, detail, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), user_id or "", action, layer, memory_type,
             (detail or "")[:200], datetime.now().isoformat()))
    except Exception:
        pass  # 观测埋点失败不阻断主流程


def log_compact_event(session_id: str, user_id: str, kind: str,
                      before_tokens: int = 0, after_tokens: int = 0, detail: str = ""):
    try:
        db.execute(
            "INSERT INTO context_compact_events (id, session_id, user_id, kind, before_tokens, after_tokens, detail, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), session_id or "", user_id or "", kind,
             int(before_tokens), int(after_tokens), (detail or "")[:200],
             datetime.now().isoformat()))
    except Exception:
        pass


# ---- 存量迁移：DB → markdown（memory_markdown_v1，db._migrate 调用）----

def migrate_from_conn(conn):
    """把存量 user_memories/user_instructions/agent_memories 导出为 markdown 文件。"""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        file = row[2] if row else ""
        if not file or file == ":memory:":
            return
        base = Path(file).parent / "memory"
    except Exception:
        return
    global _override_base
    _override_base = base
    try:
        seed_managed()
        users = {r[0] for r in conn.execute("SELECT DISTINCT user_id FROM user_memories")}
        cols = [c[0] for c in conn.execute("SELECT * FROM user_memories LIMIT 0").description]
        for uid in users:
            rows = [dict(zip(cols, r))
                    for r in conn.execute("SELECT * FROM user_memories WHERE user_id=?", (uid,))]
            for r in rows:
                p = base / "users" / uid / "auto" / memory_file_name(r)
                if not p.is_file():
                    _write(p, _frontmatter(r) + "\n" + (r.get("content") or "").strip() + "\n")
            rebuild_index(uid, conn=conn)
        for uid, content in conn.execute("SELECT user_id, content FROM user_instructions"):
            p = base / "users" / uid / "USER.md"
            if not p.is_file() and (content or "").strip():
                _write(p, content.strip() + "\n")
        for uid, agent in conn.execute("SELECT DISTINCT user_id, agent_key FROM agent_memories"):
            sync_agent_memory(uid, agent, conn=conn)
        logger.info("记忆 markdown 迁移完成（memory_markdown_v1）: %s", base)
    except Exception as e:
        logger.warning("记忆 markdown 迁移失败（忽略，DB 仍为查询镜像）: %s", e)
    finally:
        _override_base = None


def backfill_file_paths(conn) -> int:
    """memory_markdown_v2：回填存量 user_memories.file_path（v1 只导出文件未回填镜像列）。

    五层文件视图/观测以 file_path 为口径，列为空会让「文件层」与「DB 镜像」口径不一致。
    文件缺失时按镜像行补写，保证回填后路径必定可读。返回回填条数（幂等）。
    与 migrate_from_conn 同约束：init_db 期间直接用传入 conn，不经 db.query/get_conn。
    """
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        file = row[2] if row else ""
        if not file or file == ":memory:":
            return 0
        base = Path(file).parent / "memory"
    except Exception:
        return 0
    try:
        cols = [c[0] for c in conn.execute("SELECT * FROM user_memories LIMIT 0").description]
        if "file_path" not in cols:
            return 0
        rows = [dict(zip(cols, r)) for r in conn.execute(
            "SELECT * FROM user_memories WHERE file_path = '' OR file_path IS NULL")]
        touched, n = set(), 0
        for r in rows:
            uid = r.get("user_id") or ""
            if not uid or not r.get("id"):
                continue
            rel = f"users/{uid}/auto/{memory_file_name(r)}"
            p = base / rel
            if not p.is_file():
                _write(p, _frontmatter(r) + "\n" + (r.get("content") or "").strip() + "\n")
            conn.execute("UPDATE user_memories SET file_path=? WHERE id=?", (rel, r["id"]))
            touched.add(uid)
            n += 1
        # 迁移期全局连接未建立，rebuild_index 靠 base_dir() 取根目录，需临时覆盖
        global _override_base
        _override_base = base
        try:
            for uid in touched:
                rebuild_index(uid, conn=conn)
        finally:
            _override_base = None
        if n:
            logger.info("记忆 file_path 回填完成（memory_markdown_v2）: %d 条", n)
        return n
    except Exception as e:
        logger.warning("记忆 file_path 回填失败（忽略，DB 镜像仍生效）: %s", e)
        return 0