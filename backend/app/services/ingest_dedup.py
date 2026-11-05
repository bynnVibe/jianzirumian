"""
见字如面 - 重复入库校验服务

在入库入口用「来源指纹」拦截重复来源，避免同一份内容被反复解析 / 向量化 /
编译 llm-wiki 百科卡片，减少系统重复工作与资源消耗：

- 文件 / 图片：文件字节 sha256（与文件名无关，重命名或重复选择同一文件都能识别）；
- 链接：规范化 URL 的 sha256（忽略大小写、尾斜杠、默认端口等差异）。

指纹按 (fingerprint, kb_id) 唯一：同一知识库内相同来源只入库一次；跨知识库仍可
分别入库（同一份公共资料归入不同主题库是合法场景）。入库成功后登记指纹，来源删除
后注销，保证「删除 → 重新入库」不被误拦。
"""
import hashlib
import logging
import uuid
from datetime import datetime
from typing import Optional

from app.core import db

logger = logging.getLogger("jianziruyang.ingest_dedup")

KIND_FILE = "file"
KIND_URL = "url"


class DuplicateIngestError(Exception):
    """重复入库：来源指纹已在目标知识库登记。API 层应转为 HTTP 409。"""

    def __init__(self, message: str, existing: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.existing = existing or {}


# ---------- 指纹计算 ----------

def compute_file_fingerprint(file_path: str) -> str:
    """文件字节 sha256（分块读取，避免大文件占内存）；文件缺失返回空串。"""
    if not file_path:
        return ""
    try:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError as e:
        logger.warning("计算文件指纹失败 %s: %s", file_path, e)
        return ""


def compute_bytes_fingerprint(data: bytes) -> str:
    """内存字节 sha256（批量上传等已持有内容的场景，免回读磁盘）。"""
    if not data:
        return ""
    return hashlib.sha256(data).hexdigest()


def compute_url_fingerprint(url: str) -> str:
    """规范化 URL 的 sha256。

    url_imports 存储的 source_url 已是 normalize_url 结果，此处再次规范化保证幂等；
    无法规范化时退回「去尾斜杠」的原串，仍能得到稳定指纹。
    """
    norm = (url or "").strip()
    if not norm:
        return ""
    try:
        from app.services.url_fetch import normalize_url
        norm = normalize_url(norm)
    except Exception:
        norm = norm.rstrip("/")
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


# ---------- 查询 ----------

def find(fingerprint: str) -> list[dict]:
    """按指纹查全部登记（跨知识库），按登记时间倒序。"""
    if not fingerprint:
        return []
    return db.query(
        "SELECT * FROM ingest_fingerprints WHERE fingerprint = ? ORDER BY created_at DESC",
        (fingerprint,),
    )


def find_in_kb(fingerprint: str, kb_id: str, conn=None) -> Optional[dict]:
    """按 (指纹, 知识库) 查既有登记；conn 非空时在事务内直接查（避免重入全局锁）。"""
    if not fingerprint:
        return None
    sql = "SELECT * FROM ingest_fingerprints WHERE fingerprint = ? AND kb_id = ?"
    params = (fingerprint, kb_id or "")
    if conn is not None:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    return db.query_one(sql, params)


def _kb_name(kb_id: str) -> str:
    if not kb_id:
        return "默认知识库"
    row = db.query_one("SELECT name FROM knowledge_bases WHERE id = ?", (kb_id,))
    return (row["name"] if row else "") or "知识库"


# ---------- 入口校验 ----------

def ensure_can_ingest(fingerprint: str, kb_id: str, kind: str = KIND_FILE,
                      label: str = "", conn=None) -> None:
    """入库入口校验：同一知识库内已存在相同来源指纹时抛 DuplicateIngestError（转 409）。

    跨知识库不拦截（合法场景）；调用方可用 duplicate_hint 生成非阻断预警。
    """
    if not fingerprint:
        return
    existing = find_in_kb(fingerprint, kb_id, conn=conn)
    if not existing:
        return
    title = existing.get("title") or label or "该来源"
    when = (existing.get("created_at") or "")[:10]
    where = f"（《{title}》" + (f"，入库于 {when}" if when else "") + "）"
    raise DuplicateIngestError(
        f"重复入库：该内容已在当前知识库入库{where}，无需重复入库。"
        f"如确需入库到其他知识库，请先切换目标知识库再入库。",
        existing,
    )


def duplicate_hint(fingerprint: str, kb_id: str = "") -> dict:
    """上传 / 入口预警：返回该来源在系统内的既有入库情况（不拦截，供前端提示）。"""
    rows = find(fingerprint)
    if not rows:
        return {"found": False, "same_kb": False, "items": [], "message": ""}
    items = [
        {
            "kb_id": r.get("kb_id") or "",
            "kb_name": _kb_name(r.get("kb_id") or ""),
            "title": r.get("title") or "",
            "source_type": r.get("source_type") or "",
            "created_at": r.get("created_at") or "",
        }
        for r in rows
    ]
    same_kb = bool(kb_id) and any((r.get("kb_id") or "") == kb_id for r in rows)
    first = items[0]
    if same_kb:
        msg = (f"检测到重复来源：该内容已在《{first['kb_name']}》入库（{first['title']}）；"
               f"再次入库到同一知识库将被拦截。")
    else:
        names = "、".join(dict.fromkeys(i["kb_name"] for i in items))
        msg = f"检测到重复来源：该内容已入库到 {names}；如无需入库到当前知识库，可直接删除本条。"
    return {"found": True, "same_kb": same_kb, "items": items, "message": msg}


# ---------- 登记 / 注销 ----------

def register(fingerprint: str, kind: str, kb_id: str, source_type: str = "",
             title: str = "", owner_id: str = "", record_id: str = "",
             source_ref: str = "", conn=None) -> None:
    """入库成功后登记来源指纹（幂等：同 (fingerprint, kb_id) 已存在则忽略）。"""
    if not fingerprint:
        return
    sql = ("INSERT OR IGNORE INTO ingest_fingerprints (id, fingerprint, kind, kb_id,"
           " source_type, title, owner_id, record_id, source_ref, created_at)"
           " VALUES (?,?,?,?,?,?,?,?,?,?)")
    params = (str(uuid.uuid4()), fingerprint, kind, kb_id or "", source_type or "",
              title or "", owner_id or "", record_id or "", source_ref or "",
              datetime.now().isoformat())
    if conn is not None:
        conn.execute(sql, params)
    else:
        db.execute(sql, params)


def unregister_by_record(record_id: str) -> int:
    """来源记录删除后注销其指纹，保证「删除 → 重新入库」不被误拦。"""
    if not record_id:
        return 0
    return db.execute("DELETE FROM ingest_fingerprints WHERE record_id = ?", (record_id,))


def unregister_by_source(source_ref: str) -> int:
    """按来源引用（文件路径 / URL）注销指纹。"""
    if not source_ref:
        return 0
    return db.execute("DELETE FROM ingest_fingerprints WHERE source_ref = ?", (source_ref,))


def unregister_all() -> int:
    """清空全部指纹（清空知识库时用），返回删除条数。"""
    row = db.query_one("SELECT COUNT(*) AS n FROM ingest_fingerprints")
    db.execute("DELETE FROM ingest_fingerprints")
    return row["n"] if row else 0
