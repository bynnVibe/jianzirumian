"""
见字如面 - 上传记录服务
基于 SQLite 持久化上传记录 (backend/data/app.db)
"""
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import settings
from app.core import db

logger = logging.getLogger("jianziruyang.records")


def _remove_record_files(record: dict) -> None:
    """删除记录关联的物理文件（原始图片/Word/PDF）

    仅允许删除上传目录内的文件，防止路径穿越误删。
    删除后同步失效 Redis 文件缓存，避免已删文件仍可被缓存命中。
    """
    from app.core.file_cache import file_cache

    upload_dir = Path(settings.UPLOAD_DIR).resolve()
    candidates = {record.get("image_path"), record.get("pdf_preview_path")}
    for p in candidates:
        if not p:
            continue
        try:
            fp = Path(p).resolve()
            if upload_dir in fp.parents and fp.is_file():
                fp.unlink()
                logger.info("已删除存储文件: %s", fp.name)
            # 无论物理文件是否存在，都清理对应缓存
            file_cache.invalidate_file(
                Path(p).name,
                record.get("visibility", "public"),
                record.get("owner_id") or "",
            )
        except OSError as e:
            logger.warning("删除存储文件失败 %s: %s", p, e)


def _row_to_record(row: dict) -> dict:
    """数据库行 → 业务 dict（JSON 字段反序列化）"""
    return {
        "id": row["id"],
        "filename": row["filename"],
        "title": row.get("title", ""),
        "image_path": row["image_path"],
        "ocr_text": row["ocr_text"],
        "ocr_provider": row["ocr_provider"],
        "chunk_count": row["chunk_count"],
        "doc_ids": json.loads(row["doc_ids"] or "[]"),
        "visibility": row["visibility"],
        "owner_id": row["owner_id"],
        "source_type": row["source_type"],
        "pages": json.loads(row["pages"] or "[]"),
        "pdf_preview_path": row["pdf_preview_path"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class UploadRecordService:
    """上传记录管理"""

    def add_record(
        self,
        filename: str,
        image_path: str,
        ocr_text: str,
        ocr_provider: str = "local",
        chunk_count: int = 0,
        doc_ids: Optional[list[str]] = None,
        visibility: str = "public",
        owner_id: str = "",
        source_type: str = "image",
        pages: Optional[list[dict]] = None,
        pdf_preview_path: Optional[str] = None,
        title: str = "",
    ) -> dict:
        """添加上传记录"""
        now = datetime.now().isoformat()
        record = {
            "id": str(uuid.uuid4()),
            "filename": filename,
            "title": title,
            "image_path": image_path,
            "ocr_text": ocr_text,
            "ocr_provider": ocr_provider,
            "chunk_count": chunk_count,
            "doc_ids": doc_ids or [],
            "visibility": visibility,
            "owner_id": owner_id,
            "source_type": source_type,
            "pages": pages or [],
            "pdf_preview_path": pdf_preview_path,
            "created_at": now,
            "updated_at": now,
        }
        db.execute(
            "INSERT INTO upload_records (id, filename, title, image_path, ocr_text, ocr_provider,"
            " chunk_count, doc_ids, visibility, owner_id, source_type, pages,"
            " pdf_preview_path, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record["id"], filename, title, image_path, ocr_text, ocr_provider,
                chunk_count, json.dumps(doc_ids or [], ensure_ascii=False),
                visibility, owner_id, source_type,
                json.dumps(pages or [], ensure_ascii=False), pdf_preview_path,
                now, now,
            ),
        )
        return record

    def list_records(self, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
        """获取上传记录列表，按时间倒序"""
        total_row = db.query_one("SELECT COUNT(*) AS n FROM upload_records")
        total = total_row["n"] if total_row else 0
        rows = db.query(
            "SELECT * FROM upload_records ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [_row_to_record(r) for r in rows], total

    def list_ingest_history(
        self, owner_id: str, limit: int = 50, offset: int = 0
    ) -> tuple[list[dict], int]:
        """入库历史：仅返回指定用户自己成功入库的记录，按入库时间倒序。

        「成功入库」判定依据：upload_records 表中的记录仅在知识入库流水线
        （upload_pipeline.ingest）确认写入向量库且 chunk_count > 0 后才创建，
        因此表内每条记录即代表一次成功入库；chunk_count 即入库知识片段数。
        """
        total_row = db.query_one(
            "SELECT COUNT(*) AS n FROM upload_records WHERE owner_id = ?", (owner_id,)
        )
        total = total_row["n"] if total_row else 0
        rows = db.query(
            "SELECT * FROM upload_records WHERE owner_id = ?"
            " ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (owner_id, limit, offset),
        )
        return [_row_to_record(r) for r in rows], total

    def get_record(self, record_id: str) -> Optional[dict]:
        """获取单条记录"""
        row = db.query_one("SELECT * FROM upload_records WHERE id = ?", (record_id,))
        return _row_to_record(row) if row else None

    def get_record_by_source(self, source_image: str) -> Optional[dict]:
        """按来源文件路径获取记录"""
        row = db.query_one(
            "SELECT * FROM upload_records WHERE image_path = ?", (source_image,)
        )
        return _row_to_record(row) if row else None

    def get_record_by_pdf_path(self, pdf_path: str) -> Optional[dict]:
        """按 Word 转换后的预览 PDF 路径反查记录（预览 PDF 无独立上传记录）"""
        row = db.query_one(
            "SELECT * FROM upload_records WHERE pdf_preview_path = ?", (pdf_path,)
        )
        return _row_to_record(row) if row else None

    def get_records_by_source(self, source_image: str) -> list[dict]:
        """按来源文件路径获取全部匹配记录。

        同一文件路径可能存在多条记录（不同属主分别入库），文件下发权限判定
        需遍历全部匹配记录，任一记录对当前请求者可访问即放行。
        """
        rows = db.query(
            "SELECT * FROM upload_records WHERE image_path = ?", (source_image,)
        )
        return [_row_to_record(r) for r in rows]

    def get_records_by_pdf_path(self, pdf_path: str) -> list[dict]:
        """按 Word 预览 PDF 路径获取全部匹配记录（同 get_records_by_source 语义）。"""
        rows = db.query(
            "SELECT * FROM upload_records WHERE pdf_preview_path = ?", (pdf_path,)
        )
        return [_row_to_record(r) for r in rows]

    def enrich_sources(self, sources: Optional[list]):
        """按上传记录为历史来源回填 pdf_preview_path / source_type 等缺失字段。

        旧版本 sources 未保存原始排版 PDF 路径，前端只能回退为「下载原文」；
        回填后历史检索与当下检索统一显示原文跳转/预览链接。原列表就地修改并返回。
        """
        if not sources:
            return sources
        for src in sources:
            if not isinstance(src, dict):
                continue
            img = (src.get("source_image") or "").strip()
            if not img or (src.get("pdf_preview_path") and src.get("source_type")):
                continue
            rec = self.get_record_by_source(img)
            if not rec:
                # 旧数据兼容：按文件名（basename）匹配
                name = img.rsplit("/", 1)[-1]
                row = db.query_one(
                    "SELECT * FROM upload_records WHERE filename = ?", (name,)
                )
                rec = _row_to_record(row) if row else None
            if not rec:
                continue
            if not src.get("pdf_preview_path") and rec.get("pdf_preview_path"):
                src["pdf_preview_path"] = rec["pdf_preview_path"]
            if not src.get("source_type"):
                low = img.lower()
                src["source_type"] = rec.get("source_type") or (
                    "word" if low.endswith((".docx", ".doc"))
                    else "pdf" if low.endswith(".pdf")
                    else "image"
                )
        return sources

    def update_record(self, record_id: str, ocr_text: str) -> Optional[dict]:
        """更新记录的 OCR 文本"""
        now = datetime.now().isoformat()
        affected = db.execute(
            "UPDATE upload_records SET ocr_text = ?, updated_at = ? WHERE id = ?",
            (ocr_text, now, record_id),
        )
        return self.get_record(record_id) if affected else None

    def update_record_field(self, record_id: str, field: str, value) -> Optional[dict]:
        """更新记录的指定字段（白名单字段）"""
        allowed = {"ocr_text", "chunk_count", "visibility", "owner_id", "pdf_preview_path"}
        if field not in allowed:
            return None
        now = datetime.now().isoformat()
        affected = db.execute(
            f"UPDATE upload_records SET {field} = ?, updated_at = ? WHERE id = ?",
            (value, now, record_id),
        )
        return self.get_record(record_id) if affected else None

    def update_visibility_by_source(self, source_image: str, visibility: str) -> bool:
        """按来源文件同步可见性（知识文档移动知识库时使用）"""
        now = datetime.now().isoformat()
        return db.execute(
            "UPDATE upload_records SET visibility = ?, updated_at = ? WHERE image_path = ?",
            (visibility, now, source_image),
        ) > 0

    def update_pdf_preview_path_by_source(
        self, source_image: str, pdf_preview_path: str
    ) -> bool:
        """按来源文件更新 pdf_preview_path（Word 按需转换为 PDF 后使用）"""
        now = datetime.now().isoformat()
        return (
            db.execute(
                "UPDATE upload_records SET pdf_preview_path = ?, updated_at = ? WHERE image_path = ?",
                (pdf_preview_path, now, source_image),
            )
            > 0
        )

    def delete_record(self, record_id: str) -> bool:
        """删除记录"""
        return db.execute("DELETE FROM upload_records WHERE id = ?", (record_id,)) > 0

    def delete_record_by_source(self, source_image: str) -> bool:
        """按来源文件删除记录及其物理文件"""
        row = db.query_one(
            "SELECT * FROM upload_records WHERE image_path = ?", (source_image,)
        )
        if not row:
            return False
        _remove_record_files(_row_to_record(row))
        db.execute("DELETE FROM upload_records WHERE id = ?", (row["id"],))
        return True

    def delete_records_by_sources(self, source_images) -> int:
        """批量按来源文件删除记录及其物理文件，返回删除条数"""
        deleted = 0
        for src in set(source_images):
            if src and self.delete_record_by_source(src):
                deleted += 1
        return deleted

    def delete_all_records(self) -> int:
        """删除全部记录及其物理文件（清空知识库用），返回删除条数"""
        rows = db.query("SELECT * FROM upload_records")
        for row in rows:
            _remove_record_files(_row_to_record(row))
        db.execute("DELETE FROM upload_records")
        return len(rows)


# 全局单例
upload_records = UploadRecordService()
