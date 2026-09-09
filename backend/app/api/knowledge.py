"""
见字如面 - 知识库 API 路由
"""
import asyncio
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from pydantic import BaseModel

from app.api.deps import get_current_user, get_optional_user, require_admin
from app.services.guest_limiter import guest_limiter
from app.config import settings
from app.core.llm import LLMFactory
from app.core.vector_store import vector_store
from app.models.schemas import (
    DocumentPreviewResponse,
    KnowledgeEntryInfo,
    KnowledgeEntryList,
    KnowledgeStats,
    OCRPreviewResponse,
    SaveDocumentPagesRequest,
    UploadRecord,
    UploadRecordList,
    UploadRecordUpdate,
    UploadResponse,
)
from app.services.knowledge import DOC_TYPE_EXTENSIONS, knowledge_service
from app.services.records import upload_records
from app.services.upload_pipeline import upload_pipeline
from app.services.wiki import wiki_service

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

ALLOWED_TYPES = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}

# 文档上传允许类型
ALLOWED_DOC_TYPES = set()
for _exts in DOC_TYPE_EXTENSIONS.values():
    ALLOWED_DOC_TYPES.update(_exts)

# 单张图片上传大小上限（字节）
MAX_UPLOAD_SIZE = 5 * 1024 * 1024  # 5MB
# 文档上传大小上限（字节）
MAX_DOC_SIZE = 20 * 1024 * 1024  # 20MB


def _validate_ext(filename: str):
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型: {ext}，支持: {', '.join(ALLOWED_TYPES)}",
        )
    return ext


def _validate_size(content: bytes, filename: str):
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"文件 {filename} 超过大小限制 ({MAX_UPLOAD_SIZE // 1024 // 1024}MB)",
        )


async def _save_upload(file: UploadFile) -> tuple[str, str]:
    """保存上传文件，返回 (save_path_str, file_id)"""
    ext = _validate_ext(file.filename)
    file_id = str(uuid.uuid4())
    save_name = f"{file_id}{ext}"
    save_path = Path(settings.UPLOAD_DIR) / save_name
    save_path.parent.mkdir(parents=True, exist_ok=True)

    content = await file.read()
    _validate_size(content, file.filename)
    with open(save_path, "wb") as f:
        f.write(content)
    return str(save_path), file_id


def _validate_doc_ext(filename: str, doc_type: str):
    """校验文档扩展名与声明类型是否匹配"""
    ext = Path(filename).suffix.lower()
    allowed = DOC_TYPE_EXTENSIONS.get(doc_type, set())
    if ext not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型: {ext}，{doc_type} 支持: {', '.join(allowed)}",
        )
    return ext


def _validate_doc_size(content: bytes, filename: str):
    """校验文档大小"""
    if len(content) > MAX_DOC_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"文件 {filename} 超过大小限制 ({MAX_DOC_SIZE // 1024 // 1024}MB)",
        )


def _normalize_docx(path: Path) -> None:
    """修复内部条目名使用反斜杠的非标准 docx 包。

    部分工具导出的 docx 使用 'word\\document.xml' 这类条目名，
    浏览器端 mammoth/JSZip 及 python-docx 均无法正确解析。
    此处将条目名统一改写为正斜杠后重新打包。
    """
    import io
    import zipfile

    try:
        with zipfile.ZipFile(path, "r") as zin:
            names = zin.namelist()
            if not any("\\" in n for n in names):
                return  # 结构正常，无需修复
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
                for info in zin.infolist():
                    data = zin.read(info.filename)
                    fixed_name = info.filename.replace("\\", "/")
                    zout.writestr(fixed_name, data)
        path.write_bytes(buf.getvalue())
        logger.info(f"已规范化非标准 docx 包: {path.name}")
    except Exception as e:
        logger.warning(f"docx 规范化失败（保留原文件）: {e}")


async def _save_document(file: UploadFile, doc_type: str) -> tuple[str, str]:
    """保存上传文档，返回 (save_path_str, file_id)"""
    ext = _validate_doc_ext(file.filename, doc_type)
    file_id = str(uuid.uuid4())
    save_name = f"{file_id}{ext}"
    save_path = Path(settings.UPLOAD_DIR) / save_name
    save_path.parent.mkdir(parents=True, exist_ok=True)

    content = await file.read()
    _validate_doc_size(content, file.filename)
    with open(save_path, "wb") as f:
        f.write(content)
    if doc_type == "word":
        _normalize_docx(save_path)
    return str(save_path), file_id


# ============================================
# 单文件上传
# ============================================


@router.post("/upload", response_model=UploadResponse)
async def upload_image(
    file: UploadFile = File(...),
    ocr_provider: str = Form(None),
    visibility: str = Form("public"),
    kb_id: str = Form(""),
    override_text: str = Form(""),
    title: str = Form(""),
    current_user: dict = Depends(get_current_user),
):
    """上传图片并进行 OCR 识别、向量化存储（可指定目标知识库）

    override_text 非空时直接使用用户已编辑/润色的文本，不重新 OCR。
    """
    save_path, file_id = await _save_upload(file)
    owner_id = current_user.get("id") or current_user.get("username") or ""
    save_title = title.strip() or Path(file.filename).stem

    try:
        result = await knowledge_service.process_image(
            image_path=save_path,
            ocr_provider=ocr_provider,
            visibility=visibility,
            owner_id=owner_id,
            kb_id=kb_id,
            override_text=override_text,
        )

        # 保存上传记录
        upload_records.add_record(
            filename=file.filename,
            image_path=save_path,
            ocr_text=result["ocr_text"],
            ocr_provider=ocr_provider or settings.OCR_PROVIDER,
            chunk_count=result["chunk_count"],
            doc_ids=result["doc_ids"],
            visibility=visibility,
            owner_id=owner_id,
            source_type="image",
            title=save_title,
        )

        # 用量统计：上传次数
        if result["chunk_count"] > 0:
            from app.services.usage import record_upload
            record_upload(owner_id)

            # llm-wiki：后台编译百科卡片（kb 解析与入库保持一致，使卡片能命中知识库路由）
            eff_kb, eff_vis, eff_owner = knowledge_service._resolve_kb(kb_id, visibility, owner_id)
            wiki_service.schedule_compile(
                source_image=save_path,
                full_text=result["ocr_text"],
                kb_id=eff_kb,
                visibility=eff_vis,
                owner_id=eff_owner,
                source_type="image",
                title=save_title,
            )

        return UploadResponse(
            filename=file.filename,
            title=save_title,
            image_path=save_path,
            ocr_text=result["ocr_text"],
            chunk_count=result["chunk_count"],
            doc_ids=result["doc_ids"],
            success=result["chunk_count"] > 0,
            visibility=visibility,
            source_type="image",
        )
    except ImportError as e:
        if os.path.exists(save_path):
            os.remove(save_path)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        if os.path.exists(save_path):
            os.remove(save_path)
        raise HTTPException(status_code=500, detail=f"处理失败: {str(e)}")


# ============================================
# 批量上传（SSE 流式进度）
# ============================================


@router.post("/upload/batch")
async def upload_images_batch(
    files: list[UploadFile] = File(...),
    ocr_provider: str = Form(None),
    visibility: str = Form("public"),
    kb_id: str = Form(""),
    title: str = Form(""),
    current_user: dict = Depends(get_current_user),
):
    """批量上传图片，通过 SSE 流返回实时进度"""
    owner_id = current_user.get("id") or current_user.get("username") or ""

    # 先读入上传内容到内存：StreamingResponse 生成器在请求结束后才执行，
    # 届时上传临时文件已被框架关闭，再读会报 read of closed file
    pending_files = [
        {"filename": f.filename, "content": await f.read()}
        for f in files
    ]

    async def event_stream():
        total = len(pending_files)
        yield f"data: {json.dumps({'event': 'batch_start', 'total': total}, ensure_ascii=False)}\n\n"

        results = []
        errors = []

        for i, item in enumerate(pending_files):
            filename = item["filename"]
            try:
                ext = _validate_ext(filename)
                file_id = str(uuid.uuid4())
                save_name = f"{file_id}{ext}"
                save_path = Path(settings.UPLOAD_DIR) / save_name
                save_path.parent.mkdir(parents=True, exist_ok=True)

                content = item["content"]
                _validate_size(content, filename)
                with open(save_path, "wb") as f:
                    f.write(content)

                yield f"data: {json.dumps({'event': 'file_start', 'index': i, 'filename': filename}, ensure_ascii=False)}\n\n"

                result = await knowledge_service.process_image(
                    image_path=str(save_path),
                    ocr_provider=ocr_provider,
                    visibility=visibility,
                    owner_id=owner_id,
                    kb_id=kb_id,
                )

                # 保存记录（批量时未提供单文件标题，则使用文件名作为默认标题）
                file_title = title.strip() or Path(filename).stem
                record = upload_records.add_record(
                    filename=filename,
                    image_path=str(save_path),
                    ocr_text=result["ocr_text"],
                    ocr_provider=ocr_provider or settings.OCR_PROVIDER,
                    chunk_count=result["chunk_count"],
                    doc_ids=result["doc_ids"],
                    visibility=visibility,
                    owner_id=owner_id,
                    source_type="image",
                    title=file_title,
                )

                file_result = {
                    "filename": filename,
                    "title": file_title,
                    "success": result["chunk_count"] > 0,
                    "chunk_count": result["chunk_count"],
                    "ocr_text": result["ocr_text"],
                    "record_id": record["id"],
                    "source_type": "image",
                }
                results.append(file_result)

                # llm-wiki：批量上传也逐张后台编译百科卡片
                if result["chunk_count"] > 0:
                    eff_kb, eff_vis, eff_owner = knowledge_service._resolve_kb(
                        kb_id, visibility, owner_id
                    )
                    wiki_service.schedule_compile(
                        source_image=str(save_path),
                        full_text=result["ocr_text"],
                        kb_id=eff_kb,
                        visibility=eff_vis,
                        owner_id=eff_owner,
                        source_type="image",
                        title=file_title,
                    )

                yield f"data: {json.dumps({'event': 'file_done', 'index': i, **file_result}, ensure_ascii=False)}\n\n"

            except Exception as e:
                logger.warning("批量上传处理失败 [%s]: %s", filename, e)
                err = {"filename": filename, "error": str(e)}
                errors.append(err)
                yield f"data: {json.dumps({'event': 'file_error', 'index': i, **err}, ensure_ascii=False)}\n\n"

        yield f"data: {json.dumps({'event': 'batch_done', 'total': total, 'success': len(results), 'errors': len(errors)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ============================================
# 文档上传
# ============================================


@router.post("/upload/document", response_model=UploadResponse)
async def upload_document(
    file: UploadFile = File(...),
    doc_type: str = Form(...),
    visibility: str = Form("public"),
    kb_id: str = Form(""),
    title: str = Form(""),
    current_user: dict = Depends(get_current_user),
):
    """上传 Word/PDF 文档，提取文本后向量化存储（可指定目标知识库）"""
    if doc_type not in DOC_TYPE_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"不支持的文档类型: {doc_type}")

    save_path, file_id = await _save_document(file, doc_type)
    owner_id = current_user.get("id") or current_user.get("username") or ""
    save_title = title.strip() or Path(file.filename).stem

    try:
        result = await knowledge_service.process_document(
            file_path=save_path,
            doc_type=doc_type,
            visibility=visibility,
            owner_id=owner_id,
            kb_id=kb_id,
        )

        # 保存上传记录
        record = upload_records.add_record(
            filename=file.filename,
            image_path=save_path,
            ocr_text=result["text"],
            ocr_provider=f"document:{doc_type}",
            chunk_count=result["chunk_count"],
            doc_ids=result["doc_ids"],
            visibility=visibility,
            owner_id=owner_id,
            source_type=doc_type,
            title=save_title,
        )

        # llm-wiki：后台编译文档百科卡片
        if result["chunk_count"] > 0:
            eff_kb, eff_vis, eff_owner = knowledge_service._resolve_kb(kb_id, visibility, owner_id)
            wiki_service.schedule_compile(
                source_image=save_path,
                full_text=result["text"],
                kb_id=eff_kb,
                visibility=eff_vis,
                owner_id=eff_owner,
                source_type=doc_type,
                title=save_title,
            )

        return UploadResponse(
            filename=file.filename,
            title=save_title,
            image_path=save_path,
            ocr_text=result["text"],
            chunk_count=result["chunk_count"],
            doc_ids=result["doc_ids"],
            success=result["chunk_count"] > 0,
            visibility=visibility,
            source_type=doc_type,
        )
    except ImportError as e:
        if os.path.exists(save_path):
            os.remove(save_path)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        if os.path.exists(save_path):
            os.remove(save_path)
        raise HTTPException(status_code=500, detail=f"处理失败: {str(e)}")


# ============================================
# 上传流水线：统一上传 → 后台解析 → 确认入库（知识库上传页两大模块）
# ============================================

_IMAGE_EXTS = ALLOWED_TYPES


def _detect_source_type(filename: str) -> str:
    """按扩展名自动识别文件类型：image / word / pdf"""
    ext = Path(filename).suffix.lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in DOC_TYPE_EXTENSIONS["word"]:
        return "word"
    if ext in DOC_TYPE_EXTENSIONS["pdf"]:
        return "pdf"
    raise HTTPException(
        status_code=400,
        detail=f"不支持的文件类型: {ext}，支持图片({', '.join(sorted(_IMAGE_EXTS))})、.docx、.pdf",
    )


async def _save_pending_file(file: UploadFile, source_type: str) -> tuple[str, str]:
    """保存统一上传的文件，返回 (save_path, file_id)；仅落盘不解析"""
    ext = Path(file.filename).suffix.lower()
    file_id = str(uuid.uuid4())
    save_path = Path(settings.UPLOAD_DIR) / f"{file_id}{ext}"
    save_path.parent.mkdir(parents=True, exist_ok=True)

    content = await file.read()
    if source_type == "image":
        _validate_size(content, file.filename)
    else:
        _validate_doc_size(content, file.filename)
    with open(save_path, "wb") as f:
        f.write(content)
    if source_type == "word":
        _normalize_docx(save_path)
    return str(save_path), file_id


def _check_pending_owner(item: dict, current_user: dict) -> None:
    """权限：普通用户只能操作自己的待入库文件"""
    user_id = current_user.get("id") or current_user.get("username") or ""
    is_admin = current_user.get("role") == "admin"
    if not is_admin and item.get("owner_id") and item["owner_id"] != user_id:
        raise HTTPException(status_code=403, detail="只能操作自己上传的文件")


class PendingIngestRequest(BaseModel):
    """待入库文件确认入库请求（可附带入库前最新编辑结果）"""
    kb_id: str = ""
    title: str = ""
    parsed_text: Optional[str] = None
    pages: Optional[list] = None


class PendingTextEditRequest(BaseModel):
    """保存解析结果编辑（入库前检查修正）"""
    parsed_text: Optional[str] = None
    pages: Optional[list] = None


@router.post("/pending/upload")
async def pending_upload_file(
    file: UploadFile = File(...),
    ocr_provider: str = Form(""),
    current_user: dict = Depends(get_current_user),
):
    """统一文件上传（图片/Word/PDF 同一入口）：仅落盘登记，
    后台多线程自动解析（图片 OCR / 文档解析），不入库"""
    source_type = _detect_source_type(file.filename)
    owner_id = current_user.get("id") or current_user.get("username") or ""

    try:
        save_path, _file_id = await _save_pending_file(file, source_type)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文件保存失败: {str(e)}")

    item = upload_pipeline.create(
        filename=file.filename,
        source_type=source_type,
        file_path=save_path,
        owner_id=owner_id,
        ocr_provider=ocr_provider,
    )
    # 后台多线程解析，上传接口立即返回（前端轮询状态）
    upload_pipeline.schedule_parse(item["id"])
    logger.info("知识库上传登记 [%s] %s", source_type, file.filename)
    return {"success": True, "item": item}


@router.get("/pending")
async def list_pending_files(
    current_user: dict = Depends(get_current_user),
):
    """待处理文件列表（含解析状态，前端轮询用）：普通用户只看自己的"""
    user_id = current_user.get("id") or current_user.get("username") or ""
    is_admin = current_user.get("role") == "admin"
    items = upload_pipeline.list_items(owner_id=user_id, is_admin=is_admin)
    # 附带文件大小（前端展示用；文件不存在时记 0）
    for it in items:
        try:
            it["file_size"] = os.path.getsize(it["file_path"])
        except OSError:
            it["file_size"] = 0
    return {"success": True, "items": items}


@router.get("/pending/{item_id}")
async def get_pending_file(
    item_id: str,
    current_user: dict = Depends(get_current_user),
):
    """待处理文件详情（含解析全文/分页，供用户检查处理效果）"""
    item = upload_pipeline.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="文件不存在或已入库")
    _check_pending_owner(item, current_user)
    return {"success": True, "item": item}


@router.put("/pending/{item_id}")
async def update_pending_file(
    item_id: str,
    body: PendingTextEditRequest,
    current_user: dict = Depends(get_current_user),
):
    """保存用户对解析结果的编辑（入库前检查修正）"""
    item = upload_pipeline.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="文件不存在或已入库")
    _check_pending_owner(item, current_user)
    updated = upload_pipeline.update_text(
        item_id, parsed_text=body.parsed_text, pages=body.pages
    )
    return {"success": True, "item": updated}


@router.post("/pending/{item_id}/retry")
async def retry_pending_parse(
    item_id: str,
    current_user: dict = Depends(get_current_user),
):
    """解析失败后重新触发后台解析"""
    item = upload_pipeline.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="文件不存在或已入库")
    _check_pending_owner(item, current_user)
    upload_pipeline.schedule_parse(item_id)
    return {"success": True, "message": "已重新提交解析"}


@router.post("/pending/{item_id}/ingest")
async def ingest_pending_file(
    item_id: str,
    body: PendingIngestRequest,
    current_user: dict = Depends(get_current_user),
):
    """确认入库：向量化写入知识库 + 上传记录 + 后台编译 llm-wiki 卡片。
    与旧上传链路的原始文档/处理后文本对应逻辑完全一致"""
    item = upload_pipeline.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="文件不存在或已入库")
    _check_pending_owner(item, current_user)
    try:
        result = await upload_pipeline.ingest(
            item_id,
            kb_id=body.kb_id,
            title=body.title,
            parsed_text=body.parsed_text,
            pages=body.pages,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.warning("知识入库失败 [%s]: %s", item.get("filename"), e)
        raise HTTPException(status_code=500, detail=f"入库失败: {str(e)}")
    return {"success": True, **result}


@router.delete("/pending/{item_id}")
async def delete_pending_file(
    item_id: str,
    current_user: dict = Depends(get_current_user),
):
    """删除待入库文件（连同磁盘文件）"""
    item = upload_pipeline.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="文件不存在或已入库")
    _check_pending_owner(item, current_user)
    upload_pipeline.delete(item_id)
    return {"success": True, "message": "已删除"}


# ============================================
# 文档分页预览（旧链路兼容：聊天附件等仍在用）
# ============================================


@router.post("/preview-document", response_model=DocumentPreviewResponse)
async def preview_document(
    file: UploadFile = File(...),
    doc_type: str = Form(...),
    current_user: dict = Depends(get_current_user),
):
    """上传 Word/PDF 文档，按页解析返回内容，不入库。"""
    if doc_type not in DOC_TYPE_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"不支持的文档类型: {doc_type}")

    save_path, file_id = await _save_document(file, doc_type)

    try:
        result = await knowledge_service.preview_document(
            file_path=save_path,
            doc_type=doc_type,
        )
        return DocumentPreviewResponse(**result)
    except ImportError as e:
        if os.path.exists(save_path):
            os.remove(save_path)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        if os.path.exists(save_path):
            os.remove(save_path)
        raise HTTPException(status_code=500, detail=f"解析失败: {str(e)}")


@router.post("/save-document-pages")
async def save_document_pages(
    body: SaveDocumentPagesRequest,
    current_user: dict = Depends(get_current_user),
):
    """保存用户确认的文档页面到知识库。"""
    if body.doc_type not in DOC_TYPE_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"不支持的文档类型: {body.doc_type}")

    file_path = body.file_path
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="文档文件不存在或已过期")

    owner_id = current_user.get("id") or current_user.get("username") or ""

    try:
        result = await knowledge_service.save_document_pages(
            file_path=file_path,
            doc_type=body.doc_type,
            pages=[p.model_dump() for p in body.pages],
            visibility=body.visibility,
            owner_id=owner_id,
            kb_id=body.kb_id,
            title=body.title,
        )

        # 用量统计：上传次数（文档导入也计入）
        if result["chunk_count"] > 0:
            from app.services.usage import record_upload
            record_upload(owner_id)

        return UploadResponse(
            filename=Path(file_path).name,
            title=result.get("title") or Path(file_path).stem,
            image_path=file_path,
            ocr_text=result["text"],
            chunk_count=result["chunk_count"],
            doc_ids=result["doc_ids"],
            success=result["chunk_count"] > 0,
            visibility=body.visibility,
            source_type=body.doc_type,
            pages=result.get("pages"),
            pdf_preview_path=result.get("pdf_preview_path"),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存失败: {str(e)}")


@router.post("/save-document-pages-stream")
async def save_document_pages_stream(
    body: SaveDocumentPagesRequest,
    current_user: dict = Depends(get_current_user),
):
    """保存文档页面到知识库，SSE 流式返回逐页实时进度。

    事件：save_start / page_saved / page_error / save_done。
    """
    if body.doc_type not in DOC_TYPE_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"不支持的文档类型: {body.doc_type}")

    file_path = body.file_path
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="文档文件不存在或已过期")

    owner_id = current_user.get("id") or current_user.get("username") or ""

    async def event_stream():
        async for ev in knowledge_service.save_document_pages_stream(
            file_path=file_path,
            doc_type=body.doc_type,
            pages=[p.model_dump() for p in body.pages],
            visibility=body.visibility,
            owner_id=owner_id,
            kb_id=body.kb_id,
            title=body.title,
        ):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class ConvertWordPdfRequest(BaseModel):
    """Word 原始文档转换为 PDF 预览请求"""
    source_image: str


@router.post("/convert-word-pdf")
async def convert_word_pdf(
    body: ConvertWordPdfRequest,
    current_user: dict = Depends(get_current_user),
):
    """为已有 Word 条目生成原始排版 PDF 预览（按需转换并更新元数据）。"""
    upload_dir = Path(settings.UPLOAD_DIR).resolve()
    try:
        file_path = (upload_dir / Path(body.source_image).name).resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="非法的文件路径")

    # 路径穿越防护 + 文件存在性
    if upload_dir not in file_path.parents or not file_path.exists():
        raise HTTPException(status_code=404, detail="原始文件不存在")
    if file_path.suffix.lower() != ".docx":
        raise HTTPException(status_code=400, detail="仅支持 Word 文档")

    # 权限检查：普通用户只能转换自己上传的文档
    user_id = current_user.get("id") or current_user.get("username") or ""
    is_admin = current_user.get("role") == "admin"
    if not is_admin:
        rec = upload_records.get_record_by_source(str(file_path))
        if rec and rec.get("owner_id") and rec.get("owner_id") != user_id:
            raise HTTPException(status_code=403, detail="只能转换自己上传的文档")

    try:
        pdf_path = await asyncio.to_thread(
            knowledge_service._convert_word_to_pdf, str(file_path)
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"转换失败: {str(e)}")

    if not pdf_path:
        # LibreOffice 未安装（如 SLIM 瘦身镜像）或转换失败：不抛 500，
        # 返回结构化结果，由前端自动降级为解析文本的逻辑分页预览
        return {"success": False, "pdf_preview_path": None, "reason": "libreoffice_unavailable"}

    # 同步更新上传记录与向量元数据，使问答来源链接也能定位到原始 PDF
    upload_records.update_pdf_preview_path_by_source(str(file_path), pdf_path)
    vector_store.update_pdf_preview_path(str(file_path), pdf_path)

    return {"success": True, "pdf_preview_path": pdf_path}


# ============================================
# 主题知识库管理
# ============================================


class CreateKBRequest(BaseModel):
    """创建知识库请求"""
    name: str
    topic: str
    visibility: str = "private"


class UpdateKBRequest(BaseModel):
    """修改知识库请求（名称/主题，字段可选）"""
    name: str = ""
    topic: str = ""


@router.get("/bases")
async def list_knowledge_bases(
    current_user: dict | None = Depends(get_optional_user),
):
    """获取当前用户可见的知识库列表（含预设主题列表）"""
    from app.services import kb as kb_service

    user_id = ""
    if current_user:
        user_id = current_user.get("id") or current_user.get("username") or ""
    bases = kb_service.list_accessible_kbs(user_id)
    return {
        "success": True,
        "bases": bases,
        "topics": kb_service.KB_TOPICS,
        "is_admin": bool(current_user and current_user.get("role") == "admin"),
    }


@router.post("/bases")
async def create_knowledge_base(
    body: CreateKBRequest,
    current_user: dict = Depends(get_current_user),
):
    """创建知识库：管理员可建公共库，普通用户只能建个人库"""
    from app.services import kb as kb_service

    user_id = current_user.get("id") or current_user.get("username") or ""
    is_admin = current_user.get("role") == "admin"
    try:
        kb = kb_service.create_kb(body.name, body.topic, body.visibility, user_id, is_admin)
        return {"success": True, "kb": kb}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/bases/{kb_id}")
async def delete_knowledge_base(
    kb_id: str,
    current_user: dict = Depends(get_current_user),
):
    """删除知识库及其全部条目（公共库仅管理员可删）"""
    from app.services import kb as kb_service

    user_id = current_user.get("id") or current_user.get("username") or ""
    is_admin = current_user.get("role") == "admin"
    try:
        ok = kb_service.delete_kb(kb_id, user_id, is_admin)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return {"success": True, "message": "知识库已删除"}


@router.patch("/bases/{kb_id}")
async def update_knowledge_base(
    kb_id: str,
    body: UpdateKBRequest,
    current_user: dict = Depends(get_current_user),
):
    """修改知识库名称/主题（公共库仅管理员可改）"""
    from app.services import kb as kb_service

    user_id = current_user.get("id") or current_user.get("username") or ""
    is_admin = current_user.get("role") == "admin"
    try:
        kb = kb_service.update_kb(kb_id, body.name, body.topic, user_id, is_admin)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return {"success": True, "kb": kb}


# ============================================
# OCR 预览
# ============================================


@router.post("/preview-ocr", response_model=OCRPreviewResponse)
async def preview_ocr(
    file: UploadFile = File(...),
    ocr_provider: str = Form(None),
):
    """预览 OCR 识别结果（不存储）"""
    ext = _validate_ext(file.filename)
    file_id = str(uuid.uuid4())
    save_name = f"{file_id}{ext}"
    save_path = Path(settings.UPLOAD_DIR) / save_name

    content = await file.read()
    _validate_size(content, file.filename)
    with open(save_path, "wb") as f:
        f.write(content)

    try:
        text = await knowledge_service.preview_ocr(
            image_path=str(save_path),
            ocr_provider=ocr_provider,
        )
        return OCRPreviewResponse(ocr_text=text)
    finally:
        if os.path.exists(save_path):
            os.remove(save_path)


# ============================================
# 上传记录 CRUD
# ============================================


@router.get("/records", response_model=UploadRecordList)
async def list_records(
    request: Request,
    limit: int = Query(50, ge=1, le=10000),
    offset: int = Query(0, ge=0),
):
    """获取上传记录列表（游客仅返回前 3 条）"""
    is_guest = getattr(request.state, "is_guest", False)
    if is_guest:
        # 游客强制只返回前 3 条
        limit = 3
        offset = 0
    records, total = upload_records.list_records(limit=limit, offset=offset)
    if is_guest:
        total = min(total, 3)
    return UploadRecordList(records=records, total=total)


@router.get("/records/{record_id}", response_model=UploadResponse)
async def get_record(record_id: str):
    """获取单条上传记录"""
    record = upload_records.get_record(record_id)
    if not record:
        raise HTTPException(status_code=404, detail="记录不存在")
    return record


@router.put("/records/{record_id}", response_model=UploadRecord)
async def update_record(record_id: str, body: UploadRecordUpdate):
    """更新上传记录的 OCR 文本"""
    record = upload_records.update_record(record_id, body.ocr_text)
    if not record:
        raise HTTPException(status_code=404, detail="记录不存在")
    return record


@router.delete("/records/{record_id}")
async def delete_record(record_id: str, _admin: dict = Depends(require_admin)):
    """删除上传记录（仅管理员）"""
    ok = upload_records.delete_record(record_id)
    if not ok:
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"message": "记录已删除"}


# ============================================
# 入库历史（仅本人成功入库记录）
# ============================================


def _build_history_item(rec: dict) -> dict:
    """上传记录 → 入库历史列表项"""
    source_path = rec.get("image_path") or ""
    ocr_text = (rec.get("ocr_text") or "").strip()
    pages = rec.get("pages") or []
    return {
        "record_id": rec["id"],
        "ingested_at": rec.get("created_at", ""),
        "source_name": rec.get("filename") or Path(source_path).name,
        "title": rec.get("title") or "",
        "source_type": rec.get("source_type", "image"),
        "source_path": source_path,
        "pdf_preview_path": rec.get("pdf_preview_path") or "",
        "source_file_exists": bool(source_path) and os.path.exists(source_path),
        "entry_count": rec.get("chunk_count", 0),
        "has_parsed_content": bool(ocr_text or pages),
    }


@router.get("/ingest-history")
async def list_ingest_history(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user),
):
    """入库历史列表：仅返回当前登录用户自己成功入库的知识记录（含 admin）。

    成功入库判定：upload_records 表中的记录仅在入库流水线确认写入向量库后创建，
    故每条记录即代表一次成功入库；created_at 即入库时间，chunk_count 即入库片段数。
    """
    user_id = current_user.get("id") or current_user.get("username") or ""
    records, total = upload_records.list_ingest_history(
        owner_id=user_id, limit=limit, offset=offset
    )
    items = [_build_history_item(r) for r in records]
    return {"success": True, "items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/ingest-history/{record_id}/content")
async def get_ingest_history_content(
    record_id: str,
    current_user: dict = Depends(get_current_user),
):
    """入库历史详情：返回该记录的解析文档内容（解析/OCR 后的文本片段，含页码）。

    仅本人可查看，保持数据隔离一致性。
    """
    user_id = current_user.get("id") or current_user.get("username") or ""
    rec = upload_records.get_record(record_id)
    if not rec:
        raise HTTPException(status_code=404, detail="记录不存在")
    if rec.get("owner_id") != user_id:
        raise HTTPException(status_code=403, detail="只能查看自己的入库记录")

    # 组装解析文本片段：Word/PDF 按页返回（带页码），图片整篇作为单一片段
    fragments = []
    pages = rec.get("pages") or []
    if pages:
        for p in pages:
            text = (p.get("text") or "").strip()
            if not text:
                continue
            fragments.append({
                "page_number": p.get("page_number"),
                "text": text,
            })
    else:
        ocr_text = (rec.get("ocr_text") or "").strip()
        if ocr_text:
            fragments.append({"page_number": None, "text": ocr_text})

    return {
        "success": True,
        "record": _build_history_item(rec),
        "fragments": fragments,
    }


# ============================================
# 知识库统计 & 管理
# ============================================


@router.get("/stats", response_model=KnowledgeStats)
async def get_knowledge_stats():
    """获取知识库统计信息"""
    doc_count = vector_store.get_document_count()
    _, total_records = upload_records.list_records(limit=1, offset=0)
    return KnowledgeStats(
        document_count=doc_count,
        total_entries=total_records,
    )


@router.delete("/clear")
async def clear_knowledge(_admin: dict = Depends(require_admin)):
    """清空知识库（仅管理员）：向量条目 + 上传记录 + 原始文件"""
    vector_store.clear_all()
    removed = upload_records.delete_all_records()
    return {"message": "知识库已清空", "removed_records": removed}


# ============================================
# 知识库条目管理
# ============================================


@router.get("/entries", response_model=KnowledgeEntryList)
async def list_knowledge_entries(
    request: Request,
    q: str = "",
    current_user: dict | None = Depends(get_optional_user),
):
    """列出知识库中可见条目（按来源图片分组，游客仅返回前 3 条公开条目）

    支持通过 q 参数按标题（title）或文件名（filename）模糊搜索。
    """
    is_guest = getattr(request.state, "is_guest", False)

    entries = vector_store.get_all_entries()
    if not entries:
        return KnowledgeEntryList(entries=[], total=0)

    if is_guest or not current_user:
        current_user_id = ""
        is_admin = False
    else:
        current_user_id = current_user.get("id") or current_user.get("username") or ""
        is_admin = current_user.get("role") == "admin"

    # 主题知识库可见性：所有 public + 自己的 private
    from app.core import db as sqlite_db
    kb_rows = sqlite_db.query("SELECT id, name, visibility, owner_id FROM knowledge_bases")
    kb_names = {r["id"]: r["name"] for r in kb_rows}
    accessible_kb_ids = {
        r["id"] for r in kb_rows
        if r["visibility"] == "public" or r["owner_id"] == current_user_id
    }

    # 合并上传记录信息（文件名、文本预览、创建时间、可见性）
    record_map = {}
    all_records, _ = upload_records.list_records(limit=10000, offset=0)
    for rec in all_records:
        record_map[rec["image_path"]] = rec

    result = []
    for entry in entries:
        src = entry["source_image"]
        rec = record_map.get(src, {})

        kb_id = entry.get("kb_id", "")
        # 知识库隔离：不属于可见库的条目直接跳过
        if kb_id and kb_id not in accessible_kb_ids:
            continue

        visibility = rec.get("visibility", "public")
        owner_id = rec.get("owner_id", "")

        # 可见性过滤：管理员看全部，普通用户/游客只看公共和自己的私人
        if is_guest:
            # 游客只看公开条目
            if visibility == "private":
                continue
        elif visibility == "private" and not is_admin and owner_id and owner_id != current_user_id:
            continue

        # 从上传记录中获取文本预览与自定义标题
        ocr_text = rec.get("ocr_text", "")
        text_preview = ocr_text[:150] + "..." if len(ocr_text) > 150 else ocr_text
        filename = rec.get("filename", Path(src).name)
        title = rec.get("title", "").strip() or Path(filename).stem
        created_at = rec.get("created_at", "")

        # 按标题/文件名搜索过滤（支持子串精确匹配 + 字符级模糊匹配）
        if q and q.strip():
            keyword = q.strip().lower()
            t_lower = title.lower()
            f_lower = filename.lower()

            # 1. 精确子串匹配
            exact_match = keyword in t_lower or keyword in f_lower

            # 2. 字符级模糊匹配：去除空格后计算关键词中的字符在标题中的命中比例
            #    当命中率 >= 60% 时认为匹配（避免单字搜索触发全量命中）
            def _fuzzy_char_match(kw: str, target: str) -> bool:
                kw_clean = kw.replace(' ', '')
                if len(kw_clean) < 2:
                    return False
                hit = sum(1 for ch in kw_clean if ch in target)
                return hit / len(kw_clean) >= 0.6

            fuzzy_match = _fuzzy_char_match(keyword, t_lower) or _fuzzy_char_match(keyword, f_lower)

            if not exact_match and not fuzzy_match:
                continue

        source_type = rec.get("source_type", "image")
        pdf_preview_path = rec.get("pdf_preview_path")
        # Word 类型不再使用 pdf_preview_path（前端直接用 mammoth.js 渲染原始 docx）

        result.append(KnowledgeEntryInfo(
            source_image=src,
            filename=filename,
            title=title,
            chunk_count=entry["chunk_count"],
            text_preview=text_preview,
            created_at=created_at,
            doc_ids=entry["doc_ids"],
            visibility=visibility,
            owner_id=owner_id,
            source_type=source_type,
            pdf_preview_path=pdf_preview_path,
            pages=rec.get("pages") or None,
            kb_id=kb_id,
            kb_name=kb_names.get(kb_id, ""),
        ))

    # 按创建时间降序
    result.sort(key=lambda x: x.created_at, reverse=True)

    # 游客仅返回前 3 条
    if is_guest:
        result = result[:3]

    return KnowledgeEntryList(entries=result, total=len(result))


@router.delete("/entries/by-source")
async def delete_knowledge_by_source(
    source_image: str = Query(...),
    current_user: dict = Depends(get_current_user),
):
    """按来源文件删除知识条目、上传记录及原始文件。
    管理员可删除任意条目；普通用户仅可删除自己个人知识库中的条目"""
    user_id = current_user.get("id") or current_user.get("username") or ""
    is_admin = current_user.get("role") == "admin"

    if not is_admin:
        from app.core import db as sqlite_db

        # 查找该来源条目所属知识库
        kb_id = ""
        meta_map = vector_store._load_metadata()
        for meta in meta_map.values():
            if meta.get("source_image") == source_image:
                kb_id = meta.get("kb_id") or ""
                break

        if kb_id:
            kb_row = sqlite_db.query_one(
                "SELECT visibility, owner_id FROM knowledge_bases WHERE id = ?", (kb_id,)
            )
            if kb_row:
                if kb_row["visibility"] == "public":
                    raise HTTPException(status_code=403, detail="仅管理员可删除公共知识库中的条目")
                if kb_row["owner_id"] != user_id:
                    raise HTTPException(status_code=403, detail="只能删除自己个人知识库中的条目")
        else:
            # 存量数据无 kb_id：按上传记录归属判断
            rec_row = sqlite_db.query_one(
                "SELECT owner_id FROM upload_records WHERE image_path = ?", (source_image,)
            )
            if rec_row and rec_row["owner_id"] and rec_row["owner_id"] != user_id:
                raise HTTPException(status_code=403, detail="只能删除自己上传的知识条目")

    ok = vector_store.delete_by_source(source_image)
    if not ok:
        raise HTTPException(status_code=404, detail="未找到该来源的知识条目")
    # 同步删除上传记录与磁盘上的原始 Word/PDF/图片文件
    upload_records.delete_record_by_source(source_image)
    # llm-wiki：联动删除该来源编译出的百科卡片（向量 chunk + 镜像 + 数据行）
    wiki_service.delete_page_by_source(source_image)
    return {"message": "条目已删除", "source_image": source_image}


class MoveSourceRequest(BaseModel):
    """知识文档移动知识库请求"""
    source_image: str
    target_kb_id: str


@router.post("/entries/move-source")
async def move_source_to_kb(
    body: MoveSourceRequest,
    current_user: dict = Depends(get_current_user),
):
    """将知识文档（同来源全部 chunk）移动到目标知识库。
    普通用户仅可移动自己个人知识库中的条目，目标须为公共库或自己的个人库；管理员不受限"""
    user_id = current_user.get("id") or current_user.get("username") or ""
    is_admin = current_user.get("role") == "admin"

    from app.services import kb as kb_service

    target = kb_service.get_kb(body.target_kb_id)
    if not target:
        raise HTTPException(status_code=404, detail="目标知识库不存在")
    if not is_admin and target["visibility"] == "private" and target["owner_id"] != user_id:
        raise HTTPException(status_code=403, detail="只能移动到公共知识库或自己的个人知识库")

    if not is_admin:
        from app.core import db as sqlite_db

        # 查找该来源条目当前所属知识库
        kb_id = ""
        meta_map = vector_store._load_metadata()
        for meta in meta_map.values():
            if meta.get("source_image") == body.source_image:
                kb_id = meta.get("kb_id") or ""
                break

        if kb_id:
            kb_row = sqlite_db.query_one(
                "SELECT visibility, owner_id FROM knowledge_bases WHERE id = ?", (kb_id,)
            )
            if kb_row:
                if kb_row["visibility"] == "public":
                    raise HTTPException(status_code=403, detail="仅管理员可移动公共知识库中的条目")
                if kb_row["owner_id"] != user_id:
                    raise HTTPException(status_code=403, detail="只能移动自己个人知识库中的条目")
        else:
            # 存量数据无 kb_id：按上传记录归属判断
            rec_row = sqlite_db.query_one(
                "SELECT owner_id FROM upload_records WHERE image_path = ?", (body.source_image,)
            )
            if rec_row and rec_row["owner_id"] and rec_row["owner_id"] != user_id:
                raise HTTPException(status_code=403, detail="只能移动自己上传的知识条目")

    moved = vector_store.move_source(body.source_image, target["id"], target["visibility"])
    if not moved:
        raise HTTPException(status_code=404, detail="未找到该来源的知识条目")
    # 同步上传记录可见性，保持列表过滤与向量元数据一致
    upload_records.update_visibility_by_source(body.source_image, target["visibility"])
    return {
        "message": "已移动",
        "source_image": body.source_image,
        "target_kb_id": target["id"],
        "target_kb_name": target["name"],
        "moved_chunks": moved,
    }


# ============================================
# 知识库搜索检索
# ============================================


class KnowledgeSearchRequest(BaseModel):
    """知识库检索请求"""
    query: str
    top_k: int = 5


@router.post("/search")
async def search_knowledge(
    request: Request,
    body: KnowledgeSearchRequest,
    current_user: dict | None = Depends(get_optional_user),
):
    """在知识库中搜索相关内容（游客有查询次数限制）"""
    is_guest = getattr(request.state, "is_guest", False)

    if is_guest:
        client_ip = request.client.host if request.client else "unknown"
        allowed, remaining = guest_limiter.check_limit(client_ip)
        if not allowed:
            return JSONResponse(
                status_code=403,
                content={
                    "success": False,
                    "results": [],
                    "message": "游客查询次数已用完，请登录后继续使用",
                },
            )

    if not body.query.strip():
        return {"success": False, "results": [], "message": "查询内容为空"}

    try:
        if is_guest or not current_user:
            current_user_id = ""
        else:
            current_user_id = current_user.get("id") or current_user.get("username") or ""

        results = await asyncio.to_thread(
            vector_store.similarity_search,
            body.query, k=body.top_k, current_user_id=current_user_id,
        )

        # 上传记录映射（补充 title、source_type、pdf_preview_path 等展示字段）
        from app.core import db as sqlite_db
        kb_names = {r["id"]: r["name"] for r in sqlite_db.query("SELECT id, name FROM knowledge_bases")}
        all_records, _ = upload_records.list_records(limit=10000, offset=0)
        record_map = {rec["image_path"]: rec for rec in all_records}

        items = []
        seen = set()
        for text, metadata, score in results:
            # 去重
            text_key = text[:80]
            if text_key in seen:
                continue
            seen.add(text_key)
            source_image = metadata.get("source_image", "")
            # relevance score 本身已归一化到 0~1，直接换算百分比
            relevance_pct = max(0, min(int(score * 100), 99))

            # 从上传记录补充文件信息
            rec = record_map.get(source_image, {})
            filename = rec.get("filename", Path(source_image).name if source_image else "")
            title = rec.get("title", "").strip() or Path(filename).stem
            kb_id_val = metadata.get("kb_id", "")

            items.append({
                "text": text,
                "source_image": source_image,
                "score": round(score, 4),
                "relevance": relevance_pct,
                "page_number": metadata.get("page_number"),
                "source_type": metadata.get("source_type", rec.get("source_type", "image")),
                "pdf_preview_path": metadata.get("pdf_preview_path", rec.get("pdf_preview_path")),
                "filename": filename,
                "title": title,
                "kb_id": kb_id_val,
                "kb_name": kb_names.get(kb_id_val, ""),
                "is_wiki_page": bool(metadata.get("is_wiki_page")),
                "wiki_title": metadata.get("wiki_title", ""),
            })

        if is_guest:
            client_ip = request.client.host if request.client else "unknown"
            guest_limiter.increment(client_ip)

        return {"success": True, "results": items, "total": len(items)}
    except Exception as e:
        return {"success": False, "results": [], "message": f"检索失败: {str(e)}"}


# ============================================
# LLM 润色 OCR 文本
# ============================================


class PolishOCRRequest(BaseModel):
    """润色 OCR 文本请求"""
    ocr_text: str


def _clean_polished_text(text: str) -> str:
    """去除 LLM 可能添加的前缀、后缀和 Markdown 代码块等杂质。"""
    text = text.strip()
    if not text:
        return text

    # 优先提取 <polished>...</polished> 标签包裹的内容
    polished_matches = re.findall(r"<polished>([\s\S]*?)</polished>", text)
    if polished_matches:
        # LLM 可能在解释时构造示例标签，取内容最长的一段作为真正结果
        return max(polished_matches, key=len).strip()

    # 去除首尾 Markdown 代码块标记（如 ```text\n...\n```）
    if text.startswith("```"):
        text = text[3:].strip()
        if "\n" in text:
            # 跳过第一行的语言标识
            text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text[:-3].strip()

    # 如果 LLM 输出了“最终润色文本：”等结果标记，直接提取其后的内容
    result_markers = [
        "最终润色文本：", "润色后的文本：", "润色结果：", "修改后的文本：",
        "整理后的文本：", "最终文本：", "结果：", "最终输出文本。",
    ]
    for marker in result_markers:
        idx = text.rfind(marker)
        if idx != -1:
            candidate = text[idx + len(marker):].strip()
            if candidate:
                text = candidate
                break

    # 对每一行循环去除常见前缀引导语，直到没有为止
    prefixes = [
        "原文：", "润色后：", "润色结果：", "修改后：", "整理后：",
        "注意：", "说明：", "以下是", "这是", "答案：", "结果：",
    ]

    def _strip_prefixes(line: str) -> str:
        changed = True
        while changed:
            changed = False
            for prefix in prefixes:
                if line.startswith(prefix):
                    line = line[len(prefix):].strip()
                    changed = True
                    break
        return line

    cleaned_lines = [_strip_prefixes(line.strip()) for line in text.split("\n")]
    text = "\n".join(line for line in cleaned_lines if line)

    # 从第一个出现的尾部说明标记处截断
    cut_marks = [
        "\n\n注意：", "\n\n说明：", "\n\n以上", "\n\n希望",
        "\n注意：", "\n说明：", "\n以上", "\n希望",
    ]
    for mark in cut_marks:
        idx = text.find(mark)
        if idx != -1:
            text = text[:idx].strip()
            break

    return text


@router.post("/polish-ocr")
async def polish_ocr(body: PolishOCRRequest):
    """使用 LLM 对 OCR 识别结果进行语序润色（不改变原文意思）"""
    if not body.ocr_text.strip():
        return {"success": False, "message": "文本为空", "polished_text": ""}

    try:
        llm = LLMFactory.create()
        system_prompt = {
            "role": "system",
            "content": (
                "你是 OCR 文本整理助手。请对用户提供的手写笔记 OCR 文本进行最小化润色，"
                "并严格按照示例格式输出。\n\n"
                "润色范围：修正明显错别字、调整语序、合并错误断句。\n"
                "限制：保持原意，不新增、不删除、不扩写、不解释。\n\n"
                "输出格式（必须遵守）：\n"
                "- 只输出一行：'<polished>润色后的文本</polished>'。\n"
                "- 严禁输出任何解释、分析、备注、引导词或 Markdown 代码块。\n"
                "- 严禁输出思考过程。"
            ),
        }
        user_prompt = {
            "role": "user",
            "content": (
                "示例1：\n"
                "输入：这是 一段OCR 识别 出来的文本，存在 多余空格。\n"
                "输出：<polished>这是一段OCR识别出来的文本，存在多余空格。</polished>\n\n"
                "示例2：\n"
                "输入：今天 天气 很好，我们 去 公园 玩。\n"
                "输出：<polished>今天天气很好，我们去公园玩。</polished>\n\n"
                "请处理以下文本：\n"
                f"输入：{body.ocr_text}\n"
                "输出："
            ),
        }

        collected = ""
        async for chunk in llm.chat([system_prompt, user_prompt], stream=True):
            collected += chunk

        polished = _clean_polished_text(collected)
        if not polished:
            polished = body.ocr_text  # 保底返回原文

        return {"success": True, "message": "润色完成", "polished_text": polished}

    except Exception as e:
        return {"success": False, "message": f"润色失败: {str(e)}", "polished_text": body.ocr_text}


# ============================================
# 图片获取
# ============================================


@router.get("/image/{image_name}")
async def get_image(image_name: str, request: Request):
    """获取知识库中的图片（需登录，鉴权由中间件完成），带 Redis 缓存"""
    from app.core.file_cache import serve_cached_file
    from app.services import auth as auth_service

    # 解析当前用户（中间件已校验 token），用于公共/个人缓存隔离
    token = request.query_params.get("token", "")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    user = auth_service.get_session_user(token) if token else None
    if not user:
        raise HTTPException(status_code=401, detail="未登录，请先登录")

    upload_dir = Path(settings.UPLOAD_DIR).resolve()
    image_path = (upload_dir / image_name).resolve()

    # 路径穿越防护：解析后必须仍在上传目录内
    if upload_dir not in image_path.parents:
        raise HTTPException(status_code=400, detail="非法的图片路径")

    if not image_path.exists():
        for f in upload_dir.iterdir():
            if f.name == image_name or f.stem == image_name:
                image_path = f
                break
        else:
            raise HTTPException(status_code=404, detail="图片不存在")

    response = await serve_cached_file(image_path, user)
    if response is None:
        raise HTTPException(status_code=404, detail="图片不存在")
    return response
