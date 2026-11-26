"""
见字如面 - 记忆 Markdown 持久层（参考 Claude Code 记忆系统，静态/动态双轨架构）

所有记忆均以 Markdown 文件为持久记录载体，SQLite 仅作查询镜像（评分/检索/UI/观测），
写路径双写、恢复路径文件优先。记忆按用户维度隔离（users/<uid>/...）。

一、静态记忆（全局约定的规矩，全量加载；去掉 Claude Code 的 Managed/Team 层）：
  User     <memory_root>/static/USER.md      输出友好度约定（中文/表格优先/先摘要后细问）
  Project  <memory_root>/static/PROJECT.md   知识库系统整体要求/规则/结果格式（先结论后引用，
                                             引用带页码/章节；原 Managed 平台策略并入此层）
  Local    <memory_root>/static/LOCAL.md     知识检索约束（仅限用户权限内知识/系统能力边界）
  个人定制叠加层（保留现有设计，叠加在全局静态层之后注入）：
    users/<uid>/USER.md          个人指令镜像（user_instructions 单一事实源）
    kb/<kb_id>/PROJECT.md        知识库级共享规则（KB 属主/管理员可写）
    users/<uid>/local/<kb_id>.md 用户在某知识库下的私人笔记（仅本人）

二、动态记忆 · 短期（会话记忆，一个完整 session，users/<uid>/sessions/<sid>/）：
  transcript.md   完整对话流水账：最原始、未经压缩、只读镜像（chat_messages 为源，
                  随会话持久化全量重生成，从头到尾不丢细节，压缩链不改写此文件）
  summary.md      Session Memory 摘要笔记：后台 forked agent 从 transcript 提取
                  （首次上下文 ≥10K token 创建；后续 +5K token/工具调用/对话暂停更新）
  task_state.md   Task State 任务进度：从 transcript/agent_process 投影（plan/steps/状态）
  Compaction      上下文达窗口 80% 以上时用 summary.md 替换历史（session_summary 实现）

三、动态记忆 · 长期（跨会话沉淀，users/<uid>/）：
  MEMORY.md                 索引文件（每条记忆一行链接+描述）
  memories/<type>_<id8>.md  每记忆独立 topic 文件，四类型 user/feedback/project/reference
  提取动作：extractMemories（每轮 query loop 结束后后台代理扫描整轮 transcript，
  hasMemoryWritesSince 过滤刚写过的内容）与 AutoDream（三重门控：24h/5 新会话/
  .consolidate-lock PID 并发锁，独立后台子代理按固定 Prompt 整理记忆）。

记忆根目录与 SQLite 文件同址（backend/data/memory），隔离测试/内存 DB 不落盘，
避免测试污染真实数据。
"""
import logging
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.core import db

logger = logging.getLogger("jianziruyang.memory_store")

# 存储层名（API/观测同源）：静态三层 + 长期记忆（auto 为历史沿用键名）
LAYERS = ("user", "project", "local", "auto")
LAYER_LABELS = {
    "user": "静态·User（输出约定）", "project": "静态·Project（系统规则）",
    "local": "静态·Local（检索约束）", "auto": "长期记忆（跨会话）",
}
STATIC_LAYERS = ("user", "project", "local")

# ---- 静态记忆全局种子（融合系统现有全局要求：FINAL_ANSWER_RULES / 默认系统正文 /
#      原 Managed 平台策略；管理员可经 API 演进，种子仅在文件缺失时写入）----

_STATIC_USER_SEED = (
    "# 静态记忆 · User（输出友好度约定）\n\n"
    "- 默认使用中文回答，术语首次出现时给出通俗解释。\n"
    "- 对比、清单、打分、多项参数等结构化内容优先用 Markdown 表格呈现。\n"
    "- 长文本类内容先给摘要与要点，由用户决定是否展开细节，不直接倾倒长篇原文。\n"
    "- 先给结论，再给依据与推导；语气温暖简洁，不堆砌套话。\n"
    "- 用户最新表述优先于陈旧记忆；不确定时明确说明，不臆测。\n"
)

_STATIC_PROJECT_SEED = (
    "# 静态记忆 · Project（知识库系统整体要求与结果格式）\n\n"
    "- 本系统是健康养生知识库助手：基于知识库作答，可引用原文与图片，联网搜索仅作补充。\n"
    "- 回答准确、专业、有据，不编造知识库与搜索结果中都没有的内容。\n"
    "- 结果格式：先结论后引用；引用必须标注来源，并带页码/章节/段落等定位信息。\n"
    "- 涉及健康/医疗内容须标注信息来源，不做诊断性结论，必要时提醒咨询专业医师。\n"
    "- 不记录密码、密钥、证件号等敏感信息；记忆仅用于提升回答相关性。\n"
    "- 记忆与知识按用户维度隔离，不得跨用户泄露私人记忆或私有知识库内容。\n"
)

_STATIC_LOCAL_SEED = (
    "# 静态记忆 · Local（知识检索约束与能力边界）\n\n"
    "- 检索结果只限于当前用户权限范围内的知识：公共知识库 + 本人私有知识库；\n"
    "  不得检索、引用或暗示其他用户的私有知识内容。\n"
    "- 知识库未命中时如实说明，可用联网搜索补充并明确标注「网络来源」。\n"
    "- 系统能力边界：知识库问答、联网搜索、图片识别、个人工具与助手任务；\n"
    "  不承诺超出边界的能力（如诊疗处方、实时交易、线下操作）。\n"
    "- 检索受限或证据不足时，明确告知缺口，不用猜测填补。\n"
)

_STATIC_SEEDS = {"user": _STATIC_USER_SEED, "project": _STATIC_PROJECT_SEED,
                 "local": _STATIC_LOCAL_SEED}

# 原 Managed 层种子（v3 迁移时并入 static/PROJECT.md，不再单独成层）
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


# ---- 静态记忆：全局三层（全量加载）----

def static_path(layer: str) -> Optional[Path]:
    """全局静态层文件路径：static/USER.md | PROJECT.md | LOCAL.md。"""
    base = base_dir()
    if base is None or layer not in STATIC_LAYERS:
        return None
    return base / "static" / f"{layer.upper()}.md"


def read_static(layer: str) -> str:
    p = static_path(layer)
    return _read(p) if p else ""


def write_static(layer: str, content: str) -> bool:
    p = static_path(layer)
    if p is None:
        return False
    _write(p, (content or "").strip() + ("\n" if (content or "").strip() else ""))
    return True


def seed_static():
    """全局静态三层种子化（仅文件缺失时写入，管理员演进内容不被覆盖）。"""
    for layer, seed in _STATIC_SEEDS.items():
        p = static_path(layer)
        if p is not None and not p.is_file():
            _write(p, seed)


def seed_managed():
    """兼容旧调用：Managed 层已并入静态 Project 层，此处转种子化静态三层。"""
    seed_static()


# ---- 个人定制叠加层（保留现有设计：个人指令/KB 共享规则/本地私人笔记）----

def layer_path(layer: str, user_id: str = "", kb_id: str = "") -> Optional[Path]:
    base = base_dir()
    if base is None:
        return None
    if layer == "user":
        return base / "users" / user_id / "USER.md"
    if layer == "project":
        return base / "kb" / kb_id / "PROJECT.md"
    if layer == "local":
        return base / "users" / user_id / "local" / f"{kb_id}.md"
    if layer == "auto":
        return base / "users" / user_id / "MEMORY.md"
    return None


def read_layer(layer: str, user_id: str = "", kb_id: str = "") -> str:
    """读取个人定制叠加层 markdown 正文；无文件返回空串。"""
    if not user_id or user_id in {"guest", "system", "-"}:
        return ""
    if layer in ("project", "local") and not kb_id:
        return ""
    p = layer_path(layer, user_id, kb_id)
    return _read(p) if p else ""


def write_layer(layer: str, content: str, user_id: str = "", kb_id: str = "") -> bool:
    """写入个人定制叠加层 markdown（权限校验在 API 层）；返回是否落盘。"""
    p = layer_path(layer, user_id, kb_id)
    if p is None:
        return False
    _write(p, (content or "").strip() + ("\n" if (content or "").strip() else ""))
    return True


# 静态层单层注入字符预算（防超长 markdown 挤占上下文窗口）
MAX_LAYER_INJECT_CHARS = 1500
# 静态层单层写入字符上限（API 编辑预算；注入时再按 MAX_LAYER_INJECT_CHARS 截断）
MAX_LAYER_WRITE_CHARS = 4000


def static_layer_messages(user_id: str = "", kb_id: str = "") -> list:
    """读取路径：静态记忆全量加载 + 个人定制叠加层组装。

    顺序：全局 User → Project → Local（全量加载，靠后优先级更高，recency bias），
    随后 KB Project / 本人 Local 私人笔记叠加。个人指令（users/<uid>/USER.md）
    由 user_instructions 渲染（单一事实源），此处不重复输出。
    返回 [{"segment", "content"}]，segment 供观测 note_segment 同源登记；空层不注入。
    kb_id 的可见性校验由调用方（chat/API）负责，此处仅按传入 id 读取。
    """
    out = []
    headers = {
        "user": "【静态记忆 · User（输出友好度约定，须遵循）】",
        "project": "【静态记忆 · Project（系统规则与结果格式，须遵循）】",
        "local": "【静态记忆 · Local（检索约束与能力边界，须遵循）】",
    }
    for layer in STATIC_LAYERS:
        text = read_static(layer).strip()
        if text:
            out.append({"segment": f"memory_static_{layer}",
                        "content": f"{headers[layer]}\n{text[:MAX_LAYER_INJECT_CHARS]}"})
    if user_id and user_id not in {"guest", "system", "-"} and kb_id:
        project = read_layer("project", user_id=user_id, kb_id=kb_id).strip()
        if project:
            out.append({"segment": "memory_project",
                        "content": f"【知识库共享规则（Project 叠加层）】\n{project[:MAX_LAYER_INJECT_CHARS]}"})
        local = read_layer("local", user_id=user_id, kb_id=kb_id).strip()
        if local:
            out.append({"segment": "memory_local",
                        "content": f"【我在该知识库的私人笔记（Local 叠加层，仅本人可见）】\n{local[:MAX_LAYER_INJECT_CHARS]}"})
    return out


# ---- 长期记忆：每记忆独立 topic 文件 + MEMORY.md 索引（users/<uid>/memories/）----

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
    return f"{row.get('memory_type') or 'user'}_{(row.get('id') or '')[:8]}.md"


def memories_dir(user_id: str) -> Optional[Path]:
    base = base_dir()
    return base / "users" / user_id / "memories" if base else None


def sync_memory(row: dict) -> str:
    """写路径：DB 镜像行 → topic markdown 文件 + 重建索引（抽取/手动共用单一写点）。

    返回相对记忆根目录的文件路径（供回填 user_memories.file_path）；未落盘返回空串。
    """
    base = base_dir()
    if base is None or not row.get("id"):
        return ""
    mem_dir = base / "users" / row["user_id"] / "memories"
    name = memory_file_name(row)
    _write(mem_dir / name,
           _frontmatter(row) + "\n" + (row.get("content") or "").strip() + "\n")
    rebuild_index(row["user_id"])
    return f"users/{row['user_id']}/memories/{name}"


def remove_memory(user_id: str, memory_id: str):
    base = base_dir()
    if base is None:
        return
    # 新布局 memories/ 与旧布局 auto/ 都清理（迁移过渡期兼容）
    for sub in ("memories", "auto"):
        d = base / "users" / user_id / sub
        if d.is_dir():
            for f in d.glob(f"*_{memory_id[:8]}.md"):
                try:
                    f.unlink()
                except OSError:
                    pass
    rebuild_index(user_id)


def rebuild_index(user_id: str, conn=None):
    """重建 MEMORY.md 索引（users/<uid>/MEMORY.md）：索引行 `- [file](file) — 描述`。

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
    lines = ["# Memory 索引", "",
             "长期记忆索引（跨会话沉淀）：四类型 user/feedback/project/reference，"
             "每条记忆单独成文件，按更新时间倒序。", ""]
    for r in rows:
        name = memory_file_name(r)
        desc = (r.get("content") or "").strip().replace("\n", " ")[:60]
        lines.append(f"- [memories/{name}](memories/{name}) — {desc}")
    _write(base / "users" / user_id / "MEMORY.md", "\n".join(lines) + "\n")


def auto_files(user_id: str) -> dict:
    """长期记忆文件视图（UI/观测）：MEMORY.md 索引正文 + topic 文件清单（含元数据）。

    函数名沿用 auto_files（API 兼容）；读取新布局 memories/，旧布局 auto/ 兜底。
    """
    base = base_dir()
    if base is None:
        return {"index": "", "files": []}
    mem_dir = base / "users" / user_id / "memories"
    legacy = base / "users" / user_id / "auto"
    files = []
    for d in (mem_dir, legacy):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            if f.name == "MEMORY.md":
                continue
            text = _read(f)
            meta = parse_frontmatter(text)
            body = re.sub(r"^---\n.*?\n---\n?", "", text, flags=re.S).strip()
            files.append({"name": f.name, "meta": meta, "content": body,
                          "chars": len(body)})
    index = _read(base / "users" / user_id / "MEMORY.md")
    if not index:
        index = _read(legacy / "MEMORY.md")
    return {"index": index, "files": files}


def sync_memory_by_id(user_id: str, memory_id: str) -> str:
    """按 id 取最新 DB 镜像行并同步 topic 文件（写路径统一入口），返回相对路径。"""
    row = db.query_one(
        "SELECT * FROM user_memories WHERE id = ? AND user_id = ?", (memory_id, user_id))
    if not row:
        return ""
    return sync_memory(row)


def layers_overview(user_id: str, kb_id: str = "") -> list:
    """存储概览（可观测性/UI）：静态三层 + 长期记忆，每层字符量/落盘状态/相对路径。"""
    base = base_dir()
    out = []
    for layer in LAYERS:
        if layer == "auto":
            info = auto_files(user_id) if user_id else {"index": "", "files": []}
            chars = len(info["index"]) + sum(f["chars"] for f in info["files"])
            exists = bool(info["files"]) or bool(info["index"])
            p = layer_path("auto", user_id)
        else:
            # 静态层字符量 = 全局静态文件 + 个人定制叠加层
            text = read_static(layer)
            if user_id:
                text += read_layer(layer, user_id, kb_id)
            chars = len(text.strip())
            exists = bool(text.strip())
            p = static_path(layer)
        out.append({"layer": layer, "label": LAYER_LABELS[layer], "chars": chars,
                    "exists": exists,
                    "path": str(p.relative_to(base)) if (p is not None and base) else ""})
    return out


# ---- 静态层镜像同步 ----

def sync_instruction(user_id: str, content: str):
    """User 个人定制层：user_instructions DB 写入后同步 USER.md（markdown 为持久记录）。"""
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


# ---- 短期记忆：会话目录 users/<uid>/sessions/<sid>/（transcript/summary/task_state）----

def _session_user_id(session_id: str) -> str:
    """解析会话属主：摘要镜像行 → 会话行（文件路径按用户维度隔离需要 uid）。"""
    try:
        row = db.query_one(
            "SELECT user_id FROM chat_session_summaries WHERE session_id = ?", (session_id,))
        if row and row.get("user_id"):
            return row["user_id"]
        row = db.query_one(
            "SELECT user_id FROM chat_sessions WHERE session_id = ?", (session_id,))
        if row and row.get("user_id"):
            return row["user_id"]
    except Exception:
        pass
    return ""


def session_dir(session_id: str, user_id: str = "") -> Optional[Path]:
    base = base_dir()
    if base is None:
        return None
    uid = user_id or _session_user_id(session_id) or "legacy"
    return base / "users" / uid / "sessions" / session_id


def session_memory_path(session_id: str, user_id: str = "") -> Optional[Path]:
    """Session Memory 摘要笔记路径：users/<uid>/sessions/<sid>/summary.md。"""
    d = session_dir(session_id, user_id)
    return d / "summary.md" if d else None


def write_session_memory(session_id: str, user_id: str, summary: str, meta: dict):
    """Session Memory：摘要笔记落 summary.md（frontmatter 记覆盖点/类型/编辑标记/水位）。"""
    p = session_memory_path(session_id, user_id)
    if p is None:
        return
    lines = ["---", f"session_id: {session_id}", f"user_id: {user_id}"]
    for k in ("covered_until_message_id", "compact_kind", "edited_by_user", "updated_at",
              "before_tokens", "ctx_tokens_at_update"):
        lines.append(f"{k}: {meta.get(k, '')}")
    lines.append("---")
    _write(p, "\n".join(lines) + "\n" + (summary or "").strip() + "\n")


def read_session_memory(session_id: str) -> Optional[dict]:
    """恢复路径：DB 摘要缺失时从 summary.md 恢复（文件优先）；旧 SESSION.md 兜底。"""
    p = session_memory_path(session_id)
    if p is None:
        return None
    if not p.is_file():
        # 属主行缺失（摘要/会话行均无）时按目录扫描定位，保证文件恢复不失效
        base = base_dir()
        hits = sorted(base.glob(f"users/*/sessions/{session_id}/summary.md")) if base else []
        legacy = base / "sessions" / session_id / "SESSION.md" if base else None
        if hits:
            p = hits[0]
        elif legacy and legacy.is_file():
            p = legacy
        else:
            return None
    text = _read(p)
    meta = parse_frontmatter(text)
    body = re.sub(r"^---\n.*?\n---\n?", "", text, flags=re.S).strip()
    if not body:
        return None
    return {"session_id": session_id, "summary": body, **meta}


def transcript_path(session_id: str, user_id: str = "") -> Optional[Path]:
    d = session_dir(session_id, user_id)
    return d / "transcript.md" if d else None


def write_transcript(session) -> bool:
    """短期记忆 · transcript：整个 session 的完整对话流水账（只读原始记录镜像）。

    与现有完整会话记录模块融合：chat_messages 为源，每次会话持久化后全量重生成，
    未经任何压缩、不丢细节；压缩链（micro/auto/Compaction）只改读时投影与摘要，
    不改写此文件。返回是否落盘。
    """
    sid = getattr(session, "session_id", "")
    uid = getattr(session, "user_id", "") or "legacy"
    p = transcript_path(sid, uid)
    if p is None or not sid:
        return False
    msgs = getattr(session, "messages", []) or []
    lines = ["---", f"session_id: {sid}", f"user_id: {uid}",
             f"title: {getattr(session, 'title', '')}",
             f"created_at: {getattr(session, 'created_at', '')}",
             f"updated_at: {getattr(session, 'updated_at', '')}",
             f"message_count: {len(msgs)}", "---", "",
             "# Transcript（完整对话流水账 · 只读原始记录）", ""]
    for seq, m in enumerate(msgs):
        role = "用户" if getattr(m, "role", "") == "user" else "助手"
        flags = []
        if getattr(m, "use_knowledge", False):
            flags.append("知识库")
        if getattr(m, "use_search", False):
            flags.append("联网")
        if getattr(m, "is_realtime", False):
            flags.append("实时")
        if getattr(m, "interrupted", False):
            flags.append("已中断")
        flag_text = f"（{'/'.join(flags)}）" if flags else ""
        lines.append(f"## [{seq}] {role}{flag_text}")
        lines.append("")
        lines.append((getattr(m, "content", "") or "").rstrip())
        lines.append("")
    _write(p, "\n".join(lines) + "\n")
    return True


def task_state_path(session_id: str, user_id: str = "") -> Optional[Path]:
    d = session_dir(session_id, user_id)
    return d / "task_state.md" if d else None


def write_task_state(session, assistant_msg=None) -> bool:
    """短期记忆 · Task State：从 transcript/agent_process 投影任务进度。

    与现有任务进度模块整合：Agent plan/steps/status/intent/error 快照 + 会话目标
    （首条用户消息）投影为 markdown，每轮收尾刷新；无 Agent 过程的快路径轮
    仅记录目标与最近 activity。返回是否落盘。
    """
    sid = getattr(session, "session_id", "")
    uid = getattr(session, "user_id", "") or "legacy"
    p = task_state_path(sid, uid)
    if p is None or not sid:
        return False
    msgs = getattr(session, "messages", []) or []
    goal = next((m.content for m in msgs if getattr(m, "role", "") == "user"), "")
    proc = getattr(assistant_msg, "agent_process", None) or {}
    lines = ["---", f"session_id: {sid}", f"user_id: {uid}",
             f"updated_at: {datetime.now().isoformat()}", "---", "",
             "# Task State（任务进度投影）", "",
             f"- 会话目标：{(goal or '').strip()[:120] or '无'}",
             f"- 消息总数：{len(msgs)}"]
    status = proc.get("status") or ""
    if status:
        lines.append(f"- 本轮状态：{status}")
    if proc.get("intent"):
        lines.append(f"- 意图识别：{proc['intent']}")
    plan = proc.get("plan") or []
    if plan:
        lines.extend(["", "## 计划步骤"])
        for i, step in enumerate(plan):
            if isinstance(step, dict):
                text = step.get("title") or step.get("description") or str(step)
                st = step.get("status") or ""
                lines.append(f"{i + 1}. {text}" + (f"（{st}）" if st else ""))
            else:
                lines.append(f"{i + 1}. {step}")
    steps = proc.get("steps") or []
    if steps:
        lines.extend(["", "## 执行轨迹（最近 10 步）"])
        for step in steps[-10:]:
            if isinstance(step, dict):
                name = step.get("tool") or step.get("name") or step.get("phase") or "step"
                st = step.get("status") or ""
                lines.append(f"- {name}" + (f"：{st}" if st else ""))
            else:
                lines.append(f"- {step}")
    if proc.get("error"):
        lines.extend(["", f"## 错误与卡点\n{str(proc['error'])[:300]}"])
    _write(p, "\n".join(lines) + "\n")
    return True


def session_files_overview(session_id: str, user_id: str = "") -> dict:
    """短期记忆四模块文件视图（UI/观测）：transcript/summary/task_state 字符量与存在性。"""
    d = session_dir(session_id, user_id)
    out = {"session_id": session_id, "dir": "", "modules": []}
    if d is None:
        return out
    base = base_dir()
    out["dir"] = str(d.relative_to(base)) if base else ""
    for name, label in (("transcript.md", "Transcript 流水账"),
                        ("summary.md", "Session Memory 摘要"),
                        ("task_state.md", "Task State 任务进度")):
        text = _read(d / name)
        out["modules"].append({"file": name, "label": label,
                               "exists": bool(text), "chars": len(text)})
    return out


def read_session_files(session_id: str, user_id: str = "") -> dict:
    """短期记忆三文件正文（本人会话限定，属主校验由调用方负责）。"""
    d = session_dir(session_id, user_id)
    if d is None:
        return {}
    return {name: _read(d / name)
            for name in ("transcript.md", "summary.md", "task_state.md")}


# ---- AutoDream 并发锁（.consolidate-lock PID 文件锁，防多进程重复整合）----

def consolidate_lock_path(user_id: str) -> Optional[Path]:
    base = base_dir()
    return base / "users" / user_id / ".consolidate-lock" if base else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在但无权限发信号
    except (OSError, ValueError):
        return False


def acquire_consolidate_lock(user_id: str) -> bool:
    """尝试获取整合锁：锁文件存在且 PID 存活 → False（他人正在整合）；否则写入本进程 PID。

    内存 DB（测试）无落盘根目录 → 视为无锁竞争，直接放行。
    """
    p = consolidate_lock_path(user_id)
    if p is None:
        return True
    try:
        if p.is_file():
            try:
                pid = int(_read(p).strip() or 0)
            except ValueError:
                pid = 0
            if pid and pid != os.getpid() and _pid_alive(pid):
                return False
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except OSError:
        return True  # 锁文件读写失败不阻断整合（降级为无锁）


def release_consolidate_lock(user_id: str):
    p = consolidate_lock_path(user_id)
    if p is None:
        return
    try:
        if p.is_file() and _read(p).strip() == str(os.getpid()):
            p.unlink()
    except OSError:
        pass


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
        seed_static()
        users = {r[0] for r in conn.execute("SELECT DISTINCT user_id FROM user_memories")}
        cols = [c[0] for c in conn.execute("SELECT * FROM user_memories LIMIT 0").description]
        for uid in users:
            rows = [dict(zip(cols, r))
                    for r in conn.execute("SELECT * FROM user_memories WHERE user_id=?", (uid,))]
            for r in rows:
                p = base / "users" / uid / "memories" / memory_file_name(r)
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

    文件视图/观测以 file_path 为口径，列为空会让「文件层」与「DB 镜像」口径不一致。
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
            rel = f"users/{uid}/memories/{memory_file_name(r)}"
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


# ---- 存量迁移：五层 → 静态/动态双轨（memory_taxonomy_v3，db._migrate 调用）----

# 旧记忆类型 → 新四类型映射（preference/fact 归 user，goal 归 project，feedback 保留）
LEGACY_TYPE_MAP = {"preference": "user", "fact": "user", "goal": "project",
                   "feedback": "feedback"}


def migrate_taxonomy_v3(conn):
    """静态/动态双轨改造迁移：

    1) chat_session_summaries 补 ctx_tokens_at_update 列（Session Memory 增长触发水位）；
    2) user_memories 旧类型重映射为 user/feedback/project/reference，topic 文件
       auto/ → memories/ 改名重写（frontmatter 同步），file_path 回填，MEMORY.md
       索引上移至 users/<uid>/MEMORY.md；
    3) 原 Managed 层自定义内容并入 static/PROJECT.md（种子内容已融合，不重复追加）；
    4) 会话摘要 sessions/<sid>/SESSION.md → users/<uid>/sessions/<sid>/summary.md；
    5) 存量会话按 chat_messages 补写 transcript.md（只读流水账镜像）。
    """
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
        seed_static()
        # 1) 摘要水位列
        sum_cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_session_summaries)")}
        if "ctx_tokens_at_update" not in sum_cols:
            conn.execute("ALTER TABLE chat_session_summaries"
                         " ADD COLUMN ctx_tokens_at_update INTEGER NOT NULL DEFAULT 0")
        # 2) 类型重映射 + 文件迁移
        cols = [c[0] for c in conn.execute("SELECT * FROM user_memories LIMIT 0").description]
        rows = [dict(zip(cols, r)) for r in conn.execute("SELECT * FROM user_memories")]
        touched = set()
        for r in rows:
            uid = r.get("user_id") or ""
            if not uid or not r.get("id"):
                continue
            old_type = r.get("memory_type") or "fact"
            new_type = LEGACY_TYPE_MAP.get(old_type, old_type)
            if new_type not in ("user", "feedback", "project", "reference"):
                new_type = "user"
            if new_type != old_type:
                conn.execute("UPDATE user_memories SET memory_type=? WHERE id=?",
                             (new_type, r["id"]))
                r["memory_type"] = new_type
            name = memory_file_name(r)
            new_rel = f"users/{uid}/memories/{name}"
            new_p = base / new_rel
            old_p = base / "users" / uid / "auto" / f"{old_type}_{r['id'][:8]}.md"
            if not new_p.is_file():
                if old_p.is_file():
                    # 旧文件内容保留，frontmatter 按新类型重写
                    body = re.sub(r"^---\n.*?\n---\n?", "", _read(old_p), flags=re.S).strip()
                    _write(new_p, _frontmatter(r) + "\n" + body + "\n")
                    try:
                        old_p.unlink()
                    except OSError:
                        pass
                else:
                    _write(new_p, _frontmatter(r) + "\n" + (r.get("content") or "").strip() + "\n")
            if "file_path" in cols:
                conn.execute("UPDATE user_memories SET file_path=? WHERE id=?", (new_rel, r["id"]))
            touched.add(uid)
        for uid in touched:
            rebuild_index(uid, conn=conn)
            # 旧索引文件清理（索引已上移至 users/<uid>/MEMORY.md）
            legacy_index = base / "users" / uid / "auto" / "MEMORY.md"
            try:
                if legacy_index.is_file():
                    legacy_index.unlink()
            except OSError:
                pass
        # 3) Managed 自定义内容并入静态 Project
        managed_p = base / "managed" / "MANAGED.md"
        if managed_p.is_file():
            managed_text = _read(managed_p).strip()
            if managed_text and managed_text != _MANAGED_SEED.strip():
                proj_p = base / "static" / "PROJECT.md"
                proj = _read(proj_p)
                if "原 Managed 层并入" not in proj:
                    _write(proj_p, proj.rstrip() + "\n\n## 平台策略（原 Managed 层并入）\n\n"
                           + re.sub(r"^# .*\n+", "", managed_text) + "\n")
            try:
                managed_p.unlink()
            except OSError:
                pass
        # 4) SESSION.md → summary.md（按用户维度隔离）
        legacy_sessions = base / "sessions"
        if legacy_sessions.is_dir():
            for d in legacy_sessions.iterdir():
                old_md = d / "SESSION.md"
                if not old_md.is_file():
                    continue
                sid = d.name
                uid_row = conn.execute(
                    "SELECT user_id FROM chat_session_summaries WHERE session_id=?", (sid,)).fetchone()
                uid = (uid_row[0] if uid_row else "") or ""
                if not uid:
                    s_row = conn.execute(
                        "SELECT user_id FROM chat_sessions WHERE session_id=?", (sid,)).fetchone()
                    uid = (s_row[0] if s_row else "") or "legacy"
                new_p = base / "users" / uid / "sessions" / sid / "summary.md"
                if not new_p.is_file():
                    _write(new_p, _read(old_md))
                try:
                    old_md.unlink()
                except OSError:
                    pass
        # 5) 存量会话 transcript 补写（chat_messages 为源，全量不压缩）
        s_cols = [c[0] for c in conn.execute("SELECT * FROM chat_sessions LIMIT 0").description]
        m_cols = [c[0] for c in conn.execute("SELECT * FROM chat_messages LIMIT 0").description]
        for s_row in conn.execute("SELECT * FROM chat_sessions"):
            s = dict(zip(s_cols, s_row))
            sid, uid = s.get("session_id"), s.get("user_id") or "legacy"
            tp = base / "users" / uid / "sessions" / sid / "transcript.md"
            if tp.is_file():
                continue
            m_rows = [dict(zip(m_cols, r)) for r in conn.execute(
                "SELECT * FROM chat_messages WHERE session_id=? ORDER BY seq", (sid,))]
            lines = ["---", f"session_id: {sid}", f"user_id: {uid}",
                     f"title: {s.get('title', '')}",
                     f"created_at: {s.get('created_at', '')}",
                     f"updated_at: {s.get('updated_at', '')}",
                     f"message_count: {len(m_rows)}", "---", "",
                     "# Transcript（完整对话流水账 · 只读原始记录）", ""]
            for seq, m in enumerate(m_rows):
                role = "用户" if m.get("role") == "user" else "助手"
                lines.append(f"## [{seq}] {role}")
                lines.append("")
                lines.append((m.get("content") or "").rstrip())
                lines.append("")
            _write(tp, "\n".join(lines) + "\n")
        logger.info("记忆静态/动态双轨迁移完成（memory_taxonomy_v3）: %s", base)
    except Exception as e:
        logger.warning("记忆双轨迁移失败（忽略，DB 镜像仍生效）: %s", e)
    finally:
        _override_base = None