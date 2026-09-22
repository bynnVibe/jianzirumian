"""
见字如面 - FastAPI 主入口
"""
import logging
import os
import sys
import time
from pathlib import Path

# 确保项目根目录在路径中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# -------- JSON → SQLite 一次性迁移（必须在服务单例初始化前执行） --------
from app.core.migrate_json_to_sqlite import run_migration
run_migration()

from app.api import chat as chat_api
from app.api import config_api
from app.api import knowledge as knowledge_api
from app.api import auth_api
from app.api import bookmark_api
from app.api import wiki as wiki_api
from app.api import eval as eval_api
from app.api import observability as observability_api
from app.services import auth as auth_service
from app.config import settings
from app.core.log_setup import setup_logging, current_username
from app.core.vector_store import vector_store
from app.services.config_manager import runtime_config

# -------- 日志配置 --------
# 控制台 + backend/log/YYYY-MM-DD.log 双输出，每条日志时间前带当前用户名
setup_logging(debug=settings.DEBUG)
logger = logging.getLogger("jianziruyang")


class RequestLogMiddleware(BaseHTTPMiddleware):
    """记录所有 HTTP 请求的中间件"""

    async def dispatch(self, request: Request, call_next):
        start = time.time()
        # 注意：不读取请求体。读取 body 会把上传的大文件整包载入内存，
        # 且登录/注册等请求体中含明文密码，绝不能写入日志。
        logger.info(
            ">>> %s %s | client=%s | content-length=%s",
            request.method,
            request.url.path,
            request.client.host if request.client else "unknown",
            request.headers.get("content-length", "0"),
        )

        try:
            response = await call_next(request)
        except Exception as exc:
            elapsed = time.time() - start
            logger.error("XXX %s %s FAILED after %.3fs: %s",
                         request.method, request.url.path, elapsed, exc)
            raise

        elapsed = time.time() - start
        logger.info(
            "<<< %s %s -> %s | %.3fs",
            request.method,
            request.url.path,
            response.status_code,
            elapsed,
        )
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    # 启动时: 尝试加载向量库（失败不阻塞启动）
    try:
        vector_store.initialize()
    except Exception:
        pass
    # 存量向量归入默认公共知识库（依赖向量库元数据，不依赖 Embedding 服务）
    try:
        from app.services import kb as kb_service
        kb_service.patch_legacy_metadata()
    except Exception as e:
        logger.warning("知识库存量补丁失败: %s", e)
    # 上传流水线：恢复重启前未完成解析的文件（后台多线程继续解析）
    try:
        from app.services.upload_pipeline import upload_pipeline
        upload_pipeline.recover()
    except Exception as e:
        logger.warning("上传流水线恢复失败: %s", e)
    # llm-wiki：初始化标准化知识库目录结构，并恢复重启前未完成的编译队列
    try:
        from app.services import wiki_store
        from app.services.wiki import wiki_service
        wiki_store.ensure_layout()
        wiki_store.backfill_raw()  # 存量原始素材按类别归档到 wiki/raw/（幂等）
        recovered = wiki_service.recover_queue()
        if recovered:
            logger.info("llm-wiki 编译队列恢复：%d 条待处理任务", recovered)
    except Exception as e:
        logger.warning("llm-wiki 初始化/队列恢复失败: %s", e)
    # Agent 可观测性：清理过期 trace/span（保留 30 天，防止完整 prompt 撑大数据库）
    try:
        from app.core import observability as obs
        obs.purge_old()
    except Exception as e:
        logger.warning("Agent 可观测性过期数据清理失败: %s", e)
    print(f"[见字如面] 服务启动完成")
    print(f"  - LLM: {runtime_config.llm_provider or settings.LLM_PROVIDER} (默认: {settings.LLM_PROVIDER})")
    print(f"  - OCR: {runtime_config.ocr_provider or settings.OCR_PROVIDER} (默认: {settings.OCR_PROVIDER})")
    print(f"  - 搜索: {runtime_config.search_provider or settings.SEARCH_PROVIDER} (默认: {settings.SEARCH_PROVIDER})")
    print(f"  - Embedding: {settings.EMBEDDING_PROVIDER}")
    print(f"  - 向量库: {'可用' if vector_store.is_available() else '⚠ 不可用(请检查Embedding服务)'}")
    print(f"  - 上传目录: {settings.UPLOAD_DIR}")
    yield
    print("[见字如面] 服务关闭")


app = FastAPI(
    title="见字如面",
    description="手写笔记知识库问答系统 - 将手写笔记转化为可检索的知识，智能问答",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS 配置 - 允许前端跨域访问
# 默认仅覆盖本地开发地址；部署环境的域名/IP 通过 CORS_ORIGINS 环境变量配置（逗号分隔）
_default_origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]
_env_origins = os.getenv("CORS_ORIGINS", "")
if _env_origins:
    _extra_origins = [o.strip() for o in _env_origins.split(",") if o.strip()]
    _default_origins.extend(_extra_origins)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_default_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 请求日志中间件（放在 CORS 之后，优先记录）
app.add_middleware(RequestLogMiddleware)

# 注册路由
app.include_router(chat_api.router)
app.include_router(knowledge_api.router)
app.include_router(config_api.router)
app.include_router(auth_api.router)
app.include_router(bookmark_api.router)
app.include_router(wiki_api.router)
app.include_router(eval_api.router)
app.include_router(observability_api.router)


# ---- 鉴权中间件：除 /api/auth/* 和 /api/health 外都需要登录 ----
class AuthMiddleware(BaseHTTPMiddleware):
    """验证所有 API 请求的登录态（白名单除外），支持游客访问"""

    # 不需要登录的路径前缀
    WHITELIST = ("/api/auth/", "/api/health")
    # 图片类资源：<img> 标签无法携带 Authorization 头，允许通过 ?token= 鉴权
    IMAGE_PREFIXES = ("/api/knowledge/image", "/uploads")
    # 游客可访问的 API 路径前缀（无 token 时标记为游客，有 token 时正常验证）
    GUEST_ALLOWED_PATHS = (
        "/api/knowledge/records",
        "/api/knowledge/entries",
        "/api/knowledge/search",
        "/api/bookmarks",
        "/api/chat/send",
        "/api/chat/sessions",
        "/api/chat/attachment/",
        "/api/config/public",
    )

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # 白名单放行
        if any(path.startswith(p) for p in self.WHITELIST):
            return await call_next(request)

        # 提取 token：优先 Authorization 头，图片资源允许查询参数
        auth_header = request.headers.get("Authorization", "")
        token = ""
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        if not token and any(path.startswith(p) for p in self.IMAGE_PREFIXES):
            token = request.query_params.get("token", "")

        # 游客访问：无 token 但路径在游客白名单中
        is_guest = not token and any(path.startswith(p) for p in self.GUEST_ALLOWED_PATHS)
        request.state.is_guest = is_guest

        if not token:
            if is_guest:
                # 游客放行，标记 is_guest=True
                return await call_next(request)
            return JSONResponse(
                status_code=401,
                content={"detail": "未登录，请先登录"},
            )

        user = auth_service.get_session_user(token)
        if not user:
            return JSONResponse(
                status_code=401,
                content={"detail": "登录已过期，请重新登录"},
            )

        # 把当前用户名写入日志上下文（AuthMiddleware 是最外层中间件，
        # 后续请求日志/业务日志均能带上用户名）
        ctx_token = current_username.set(user.get("username") or "-")
        try:
            return await call_next(request)
        finally:
            current_username.reset(ctx_token)


# 鉴权中间件注册（在路由注册之后，类定义之后）
app.add_middleware(AuthMiddleware)


@app.get("/api/health")
async def health_check():
    """健康检查"""
    embedding_ok = vector_store.is_available()
    doc_count = vector_store.get_document_count() if embedding_ok else 0
    from app.core.file_cache import file_cache
    return {
        "status": "ok",
        "version": "1.0.0",
        "embedding": "available" if embedding_ok else "unavailable",
        "knowledge_count": doc_count,
        "session_count": len(chat_api.chat_service.sessions) if hasattr(chat_api, 'chat_service') else 0,
        "file_cache": file_cache.status(),
    }


@app.post("/api/vector-store/retry")
async def retry_vector_store():
    """重新尝试连接 Embedding 服务并初始化向量库"""
    try:
        if vector_store.retry_init():
            return {"message": "向量库初始化成功", "status": "ok"}
        return {
            "message": "Embedding 服务仍不可用，请检查 Ollama 是否已启动或 .env 配置",
            "status": "error",
        }
    except Exception as e:
        return {"message": f"向量库初始化失败: {str(e)}", "status": "error"}


# 提供上传文件访问（替代原 StaticFiles 挂载，接入 Redis 文件缓存）
@app.get("/uploads/{filename:path}")
async def serve_upload_file(filename: str, request: Request):
    """下发上传目录中的原始文件（图片/Word/PDF），带 Redis 缓存

    鉴权中间件已校验 token（支持 ?token= 查询参数），此处再解析出用户身份，
    用于公共/个人知识库文件的权限隔离与缓存键选择：
    - 公共库文件：所有用户共享同一份缓存
    - 个人库文件：仅属主/管理员可访问，缓存按属主隔离
    """
    from app.core.file_cache import serve_cached_file

    token = request.query_params.get("token", "")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    user = auth_service.get_session_user(token) if token else None
    if not user:
        return JSONResponse(status_code=401, content={"detail": "未登录，请先登录"})

    upload_dir = Path(settings.UPLOAD_DIR).resolve()
    file_path = (upload_dir / filename).resolve()
    # 路径穿越防护：解析后必须仍在上传目录内
    if upload_dir not in file_path.parents:
        return JSONResponse(status_code=400, content={"detail": "非法的文件路径"})

    response = await serve_cached_file(file_path, user)
    if response is None:
        return JSONResponse(status_code=404, content={"detail": "文件不存在"})
    return response


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
    )
