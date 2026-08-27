"""
见字如面 - 全局配置
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"


class Settings:
    # ---- Server ----
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))
    # 默认关闭 DEBUG，避免生产环境意外输出调试日志；本地开发在 .env 中显式开启
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"

    # ---- LLM 配置 ----
    # provider: "ollama" | "openai" | "custom"
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "ollama")

    # Ollama
    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

    # OpenAI 兼容 API (通义千问 / DeepSeek / OpenAI ...)
    API_BASE_URL: str = os.getenv("API_BASE_URL", "https://api.deepseek.com/v1")
    API_KEY: str = os.getenv("API_KEY", "")
    API_MODEL: str = os.getenv("API_MODEL", "deepseek-chat")

    # ---- Embedding 配置 ----
    EMBEDDING_PROVIDER: str = os.getenv("EMBEDDING_PROVIDER", "ollama")
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "bge-m3:latest")
    EMBEDDING_API_BASE: str = os.getenv("EMBEDDING_API_BASE", "http://localhost:11434")
    EMBEDDING_API_KEY: str = os.getenv("EMBEDDING_API_KEY", "")
    EMBEDDING_API_MODEL: str = os.getenv("EMBEDDING_API_MODEL", "text-embedding-3-small")

    # ---- OCR 配置 ----
    # provider: "local" | "aliyun" | "custom_api"
    OCR_PROVIDER: str = os.getenv("OCR_PROVIDER", "local")

    # 阿里云百炼 OCR (OpenAI 兼容接口)
    # 使用 qwen3.5-ocr 等多模态模型识别图片文字
    ALIYUN_ACCESS_KEY_ID: str = os.getenv("ALIYUN_ACCESS_KEY_ID", "")
    ALIYUN_ACCESS_KEY_SECRET: str = os.getenv("ALIYUN_ACCESS_KEY_SECRET", "")
    ALIYUN_OCR_ENDPOINT: str = os.getenv("ALIYUN_OCR_ENDPOINT", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    BAILIAN_OCR_API_KEY: str = os.getenv("BAILIAN_OCR_API_KEY") or os.getenv("ALIYUN_ACCESS_KEY_SECRET") or os.getenv("EMBEDDING_API_KEY", "")
    BAILIAN_OCR_MODEL: str = os.getenv("BAILIAN_OCR_MODEL") or os.getenv("ALIYUN_ACCESS_KEY_ID") or "qwen3.5-ocr"
    BAILIAN_OCR_BASE_URL: str = os.getenv("BAILIAN_OCR_BASE_URL") or os.getenv("ALIYUN_OCR_ENDPOINT", "https://dashscope.aliyuncs.com/compatible-mode/v1")

    # 自定义远程 OCR API
    CUSTOM_OCR_API_URL: str = os.getenv("CUSTOM_OCR_API_URL", "")
    CUSTOM_OCR_API_KEY: str = os.getenv("CUSTOM_OCR_API_KEY", "")

    # PaddleOCR 本地配置
    PADDLE_OCR_LANG: str = os.getenv("PADDLE_OCR_LANG", "ch")
    PADDLE_OCR_USE_GPU: bool = os.getenv("PADDLE_OCR_USE_GPU", "false").lower() == "true"

    # ---- 向量存储 ----
    VECTOR_STORE_PATH: str = os.getenv(
        "VECTOR_STORE_PATH",
        str(DATA_DIR / "vector_store" / "faiss_index"),
    )
    CHUNK_SIZE: int = int(os.getenv("CHUNK_SIZE", "500"))
    CHUNK_OVERLAP: int = int(os.getenv("CHUNK_OVERLAP", "50"))
    TOP_K_RETRIEVAL: int = int(os.getenv("TOP_K_RETRIEVAL", "3"))

    # ---- Reranker ----
    # provider: "local" (本地 CrossEncoder 模型) | "dashscope" (阿里云 DashScope rerank API)
    RERANKER_PROVIDER: str = os.getenv("RERANKER_PROVIDER", "local")
    RERANKER_MODEL: str = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
    # 阿里云 DashScope OpenAI 兼容 rerank 接口（base_url 为 .../compatible-api/v1，请求路径 /reranks）
    # Key 未单独配置时回退到通用的 DASHSCOPE_API_KEY
    DASHSCOPE_RERANK_BASE_URL: str = os.getenv("DASHSCOPE_RERANK_BASE_URL", "")
    DASHSCOPE_RERANK_API_KEY: str = os.getenv("DASHSCOPE_RERANK_API_KEY", "") or os.getenv("DASHSCOPE_API_KEY", "")
    DASHSCOPE_RERANK_MODEL: str = os.getenv("DASHSCOPE_RERANK_MODEL", "qwen3-rerank")

    # ---- 联网搜索 ----
    # provider: "duckduckgo" | "bing" | "serpapi" | "anysearch"
    SEARCH_PROVIDER: str = os.getenv("SEARCH_PROVIDER", "duckduckgo")
    BING_API_KEY: str = os.getenv("BING_API_KEY", "")
    SERPAPI_API_KEY: str = os.getenv("SERPAPI_API_KEY", "")
    # AnySearch (统一实时搜索，Key 可选，匿名可用但限额较低)
    # Skill 模块位于 backend/.skills/anysearch
    ANYSEARCH_API_KEY: str = os.getenv("ANYSEARCH_API_KEY", "")

    # ---- 数据目录 ----
    UPLOAD_DIR: str = str(DATA_DIR / "uploads")
    KNOWLEDGE_DIR: str = str(DATA_DIR / "knowledge")
    # llm-wiki 百科镜像目录：编译产物同时以 Markdown 落盘，可浏览/导出为知识站点
    WIKI_DIR: str = os.getenv("WIKI_DIR", str(DATA_DIR / "wiki"))

    # ---- 会话 ----
    SESSION_MAX_HISTORY: int = int(os.getenv("SESSION_MAX_HISTORY", "20"))
    # 登录态有效期（秒），每次请求滑动续期，默认 24 小时
    SESSION_DURATION: int = int(os.getenv("SESSION_DURATION", "86400"))

    # ---- Redis 文件缓存 ----
    # 知识检索/知识库跳转的原始文件（图片/Word/PDF）缓存；
    # 公共库文件全局共享缓存，个人库文件按属主隔离；Redis 不可用时自动降级直读磁盘
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    FILE_CACHE_ENABLED: bool = os.getenv("FILE_CACHE_ENABLED", "true").lower() == "true"
    # 缓存过期时间（秒），默认 24 小时
    FILE_CACHE_TTL: int = int(os.getenv("FILE_CACHE_TTL", "86400"))
    # 单文件缓存大小上限（MB），超限不缓存直接读盘
    FILE_CACHE_MAX_MB: int = int(os.getenv("FILE_CACHE_MAX_MB", "20"))

    # ---- llm-wiki 知识编译层 ----
    # 上传入库后后台用 LLM 把资料编译成结构化百科卡片（source_type=wiki）并回写向量库，
    # 检索时优先命中预编译知识；Redis 不可用/LLM 失败时自动降级为普通 RAG，不影响现有功能
    WIKI_COMPILE_ENABLED: bool = os.getenv("WIKI_COMPILE_ENABLED", "true").lower() == "true"
    # 参与编译的单资料最大字符数（超出截断，控制 LLM 输入成本）
    WIKI_MAX_SOURCE_CHARS: int = int(os.getenv("WIKI_MAX_SOURCE_CHARS", "6000"))
    # 低于该字符数的资料不编译（OCR 零碎片段编译无意义）
    WIKI_MIN_SOURCE_CHARS: int = int(os.getenv("WIKI_MIN_SOURCE_CHARS", "80"))
    # 相邻两次编译 LLM 请求的最小间隔秒数（存量补编译时缓解免费模型限流）
    WIKI_COMPILE_INTERVAL: float = float(os.getenv("WIKI_COMPILE_INTERVAL", "3"))


settings = Settings()

# 创建必要的目录
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
os.makedirs(Path(settings.VECTOR_STORE_PATH).parent, exist_ok=True)
os.makedirs(settings.KNOWLEDGE_DIR, exist_ok=True)
os.makedirs(settings.WIKI_DIR, exist_ok=True)
