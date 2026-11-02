"""链接草稿及显式确认状态机；解析与知识写入分离，不重放不确定的写入。"""
import asyncio
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException

from app.config import settings
from app.core import db, observability as obs
from app.core.agent_runtime import runtime
from app.services.url_fetch import UrlImportError, normalize_url

logger = logging.getLogger("jianziruyang.url_imports")
SOURCE_FIELDS = ("source_url", "final_url", "source_site", "fetched_at", "extraction_method", "content_origin")


def now():
    return datetime.now().isoformat()


def formal_user(user):
    if not user or user.get("role") not in {"user", "admin", "ops"} or not user.get("id") or user["id"] in {"guest", "system", "-"}:
        raise HTTPException(403, "请使用正式账号导入公开链接")
    return user


def owned(conn, task_id, user):
    formal_user(user)
    row = conn.execute("SELECT * FROM url_imports WHERE id=? AND owner_id=?", (task_id, user["id"])).fetchone()
    if not row:
        raise HTTPException(404, "链接任务不存在")
    return dict(row)


def writable_target(conn, kb_id, user):
    row = conn.execute("SELECT * FROM knowledge_bases WHERE id=?", (kb_id,)).fetchone()
    if not row:
        raise HTTPException(409, "目标知识库不存在，请重新选择")
    row = dict(row)
    if not (user["role"] == "admin" if row["visibility"] == "public" else row["owner_id"] == user["id"]):
        raise HTTPException(403, "无权写入目标知识库")
    return row


def item(row, full=True):
    metadata = json.loads(row["metadata"])
    value = {key: row[key] for key in ("id", "owner_id", "source_url", "title", "status", "phase", "revision", "kb_id", "file_path", "error", "error_code", "created_at", "updated_at")}
    value.update({key: metadata.get(key, "") for key in SOURCE_FIELDS if key != "source_url"})
    value.update(source_type="url", filename=row["title"] or urlsplit(row["source_url"]).hostname,
                 text_length=len(row["parsed_text"]), warnings=metadata.get("warnings", []),
                 result=json.loads(row["result"]), page_count=0)
    if full:
        value.update(parsed_text=row["parsed_text"], original_text=row["original_text"], original_title=row["original_title"])
    return value


class UrlImportService:
    def __init__(self):
        self.tasks = {}
        self.semaphore = None

    def get(self, task_id, user):
        with db.transaction() as conn:
            return item(owned(conn, task_id, user))

    def list(self, user):
        formal_user(user)
        rows = db.query("SELECT * FROM url_imports WHERE owner_id=? ORDER BY created_at DESC LIMIT 200", (user["id"],))
        return [item(row, False) for row in rows]

    def _capacity(self, conn, user):
        count = conn.execute("SELECT COUNT(*) FROM url_imports WHERE owner_id=? AND status IN ('pending','parsing','ingesting')", (user["id"],)).fetchone()[0]
        if count >= 10:
            raise HTTPException(429, "最多同时处理 10 个链接，请等待现有任务完成")

    def create(self, url, user):
        runtime.require_owner()
        formal_user(user)
        try:
            url = normalize_url(url)
        except UrlImportError as exc:
            raise HTTPException(422, str(exc)) from None
        task_id, timestamp = str(uuid.uuid4()), now()
        with db.transaction() as conn:
            self._capacity(conn, user)
            conn.execute("INSERT INTO url_imports(id,owner_id,source_url,created_at,updated_at) VALUES(?,?,?,?,?)",
                         (task_id, user["id"], url, timestamp, timestamp))
        self.schedule(task_id)
        return self.get(task_id, user)

    def save(self, task_id, user, revision, title, text):
        runtime.require_owner()
        with db.transaction() as conn:
            row = owned(conn, task_id, user)
            if row["revision"] != revision or row["status"] not in {"done", "error"} or row["phase"] != "parse":
                raise HTTPException(409, "草稿版本或状态已变化，请重新加载；本地编辑尚未保存")
            metadata = json.loads(row["metadata"])
            metadata["content_origin"] = "user_supplied" if not row["original_text"] else (
                "user_edited" if text.strip() != row["original_text"] or title.strip() != row["original_title"] else "fetched")
            conn.execute("UPDATE url_imports SET title=?,parsed_text=?,metadata=?,revision=revision+1,status=?,updated_at=? WHERE id=?",
                         (title.strip(), text.strip(), json.dumps(metadata, ensure_ascii=False), "done" if text.strip() else "error", now(), task_id))
        return self.get(task_id, user)

    def retry(self, task_id, user, revision):
        runtime.require_owner()
        with db.transaction() as conn:
            row = owned(conn, task_id, user)
            if row["revision"] != revision or row["status"] not in {"done", "error"} or row["phase"] != "parse":
                raise HTTPException(409, "当前状态不允许重新解析；不确定的入库不会重放")
            self._capacity(conn, user)
            conn.execute("UPDATE url_imports SET status='pending',revision=revision+1,error='',error_code='',updated_at=? WHERE id=?", (now(), task_id))
        self.schedule(task_id)
        return self.get(task_id, user)

    def delete(self, task_id, user):
        runtime.require_owner()
        with db.transaction() as conn:
            row = owned(conn, task_id, user)
            if row["phase"] == "ingest":
                raise HTTPException(409, "已确认的任务请在入库历史或知识库管理中核对，不能作为草稿删除")
            conn.execute("DELETE FROM url_imports WHERE id=?", (task_id,))
        task = self.tasks.get(task_id)
        if task:
            task.cancel()

    def schedule(self, task_id):
        runtime.require_owner()
        if task_id in self.tasks:
            return
        task = asyncio.create_task(self.parse(task_id))
        self._track(task_id, task)

    def _track(self, task_id, task):
        self.tasks[task_id] = task
        def finished(result):
            if self.tasks.get(task_id) is result:
                self.tasks.pop(task_id, None)
            if not result.cancelled() and result.exception():
                logger.error("链接任务异常：%s (%s)", task_id, type(result.exception()).__name__)
        task.add_done_callback(finished)

    async def parse(self, task_id):
        from app.services.url_analyst import urlAnalystAgent
        if self.semaphore is None:
            self.semaphore = asyncio.Semaphore(2)
        async with self.semaphore:
            with db.transaction() as conn:
                row = conn.execute("SELECT * FROM url_imports WHERE id=? AND status='pending'", (task_id,)).fetchone()
                if not row:
                    return
                row = dict(row)
                conn.execute("UPDATE url_imports SET status='parsing',updated_at=? WHERE id=?", (now(), task_id))
            try:
                article = await urlAnalystAgent.run(task_id, row["owner_id"], row["source_url"])
                metadata = {key: value for key, value in article.items() if key not in {"title", "body"}}
                db.execute("UPDATE url_imports SET status='done',title=?,parsed_text=?,original_title=?,original_text=?,metadata=?,error='',error_code='',updated_at=? WHERE id=? AND status='parsing' AND revision=?",
                           (article["title"], article["body"], article["title"], article["body"], json.dumps(metadata, ensure_ascii=False), now(), task_id, row["revision"]))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                message = str(exc) if isinstance(exc, UrlImportError) else "网页解析失败，请重试或手动补充正文"
                db.execute("UPDATE url_imports SET status='error',error=?,error_code=?,updated_at=? WHERE id=? AND status='parsing' AND revision=?",
                           (message, getattr(exc, "code", "parse_failed"), now(), task_id, row["revision"]))

    def confirm(self, task_id, user, revision, kb_id):
        runtime.require_owner()
        with db.transaction() as conn:
            row = owned(conn, task_id, user)
            if row["revision"] != revision:
                raise HTTPException(409, "草稿版本已变化，请重新检查后确认")
            if row["status"] in {"ingesting", "succeeded"} and row["kb_id"] == kb_id:
                return item(row)
            if row["status"] != "done" or row["phase"] != "parse" or not row["parsed_text"].strip():
                raise HTTPException(409, "当前任务不能确认入库，请先检查正文或核对上次执行结果")
            writable_target(conn, kb_id, user)
            path = str(Path(settings.UPLOAD_DIR) / f"url-{task_id}.md")
            changed = conn.execute("UPDATE url_imports SET status='ingesting',phase='ingest',kb_id=?,file_path=?,updated_at=? WHERE id=? AND status='done' AND revision=?",
                                   (kb_id, path, now(), task_id, revision)).rowcount
            if not changed:
                raise HTTPException(409, "任务已被确认")
        self._track(task_id, asyncio.create_task(self.ingest(task_id)))
        return self.get(task_id, user)

    async def ingest(self, task_id):
        from app.core.vector_store import vector_store
        from app.services.records import upload_records
        from app.services.usage import record_upload
        from app.services.wiki import wiki_service
        row = db.query_one("SELECT * FROM url_imports WHERE id=? AND status='ingesting'", (task_id,))
        if not row:
            return
        obs.start_trace("url_import_confirmation", row["owner_id"], task_id)
        status, error = "ok", ""
        try:
            with db.transaction() as conn:
                user_row = conn.execute("SELECT id,role,is_active FROM users WHERE id=?", (row["owner_id"],)).fetchone()
                if not user_row or not user_row["is_active"]:
                    raise HTTPException(403, "账号已失效")
                user = formal_user(dict(user_row))
                target = writable_target(conn, row["kb_id"], user)
            path = Path(row["file_path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(row["parsed_text"], encoding="utf-8")
            metadata = {**json.loads(row["metadata"]), "source_url": row["source_url"]}
            provenance = {key: metadata.get(key, "") for key in SOURCE_FIELDS}
            title = row["title"] or urlsplit(row["source_url"]).hostname
            def vectorize():
                chunks = vector_store.split_text(row["parsed_text"], str(path), target["visibility"], row["owner_id"],
                                                  kb_id=row["kb_id"], source_type="url", document_title=title, url_import_id=task_id, **provenance)
                if not chunks:
                    raise ValueError("empty_chunks")
                return vector_store.add_texts([chunk[0] for chunk in chunks], [chunk[1] for chunk in chunks])
            doc_ids = await asyncio.to_thread(vectorize)
            record = upload_records.add_record(filename=f"{title}.md", image_path=str(path), ocr_text=row["parsed_text"],
                ocr_provider="url:" + metadata.get("extraction_method", "manual"), chunk_count=len(doc_ids), doc_ids=doc_ids,
                visibility=target["visibility"], owner_id=row["owner_id"], source_type="url", title=title,
                source_metadata=provenance, url_import_id=task_id)
            result = {"record_id": record["id"], "chunk_count": len(doc_ids), "doc_ids": doc_ids, "kb_id": row["kb_id"]}
            db.execute("UPDATE url_imports SET result=?,updated_at=? WHERE id=?", (json.dumps(result), now(), task_id))
            record_upload(row["owner_id"])
            try:
                wiki_service.schedule_compile(source_image=str(path), full_text=row["parsed_text"], kb_id=row["kb_id"],
                    visibility=target["visibility"], owner_id=row["owner_id"], source_type="url", title=title, source_metadata=provenance)
            except Exception:
                result["wiki_warning"] = "知识已入库，百科编译调度失败，请在百科中核对"
            db.execute("UPDATE url_imports SET status='succeeded',result=?,error='',error_code='',updated_at=? WHERE id=? AND status='ingesting'",
                       (json.dumps(result, ensure_ascii=False), now(), task_id))
        except asyncio.CancelledError:
            status, error = "cancelled", "interrupted"
            db.execute("UPDATE url_imports SET status='interrupted',error='执行中断，可能部分入库，请核对；不会自动重试',error_code='interrupted',updated_at=? WHERE id=? AND status='ingesting'", (now(), task_id))
            raise
        except Exception as exc:
            status, error = "error", type(exc).__name__
            message = exc.detail if isinstance(exc, HTTPException) else "入库执行失败，可能已部分写入，请核对入库历史及知识库；不会自动重试"
            db.execute("UPDATE url_imports SET status='error',error=?,error_code='ingest_failed',updated_at=? WHERE id=? AND status='ingesting'", (message, now(), task_id))
        finally:
            obs.end_trace(status, error)

    def recover(self):
        runtime.require_owner()
        for row in db.query("SELECT id FROM url_imports WHERE status='pending'"):
            self.schedule(row["id"])

    async def shutdown(self):
        for task_id, task in list(self.tasks.items()):
            row = db.query_one("SELECT phase FROM url_imports WHERE id=?", (task_id,))
            if not row or row["phase"] == "parse":
                task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)


url_imports = UrlImportService()
