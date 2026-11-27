"""
见字如面 - 全局配置
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# 固定配置位置，避免调试或从项目根目录启动时误读上级目录的 .env。
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"


class Settings:
    # ---- Server ----
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))
    TRUSTED_PROXY_CIDRS: str = os.getenv("TRUSTED_PROXY_CIDRS", "")
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

    # ---- LLM Proxy（层3 自建网关）----
    # 业务出网默认统一走网关：各模块经 create_for_role 按「业务路由」解析后，
    # 请求 GATEWAY_INTERNAL_URL 指向的同进程 /v1（见下方），无需设 LLM_PROVIDER。
    # 下面 LLM_PROXY_URL/KEY 仅用于「显式 LLM_PROVIDER=gateway 直连外部独立 Proxy」
    # 或网关未初始化时的降级直连；本地开发默认 localhost:8001，compose 部署由环境变量覆盖为 http://llm-proxy:8001
    LLM_PROXY_URL: str = os.getenv("LLM_PROXY_URL", "http://localhost:8001")
    # 调用网关所用的本地服务密钥（sk-jzrm-...），在网关「密钥」页签生成
    LLM_PROXY_KEY: str = os.getenv("LLM_PROXY_KEY", "")
    LLM_PROXY_PORT: int = int(os.getenv("LLM_PROXY_PORT", "8001"))
    # 系统内部出网地址：各业务模块经本网 /v1 代理出网（同进程回环，默认后端 8000 端口）；
    # 独立部署 LLM Proxy 时可用环境变量指向其 /v1
    GATEWAY_INTERNAL_URL: str = os.getenv("GATEWAY_INTERNAL_URL", "http://127.0.0.1:8000/v1")

    # ---- Embedding 配置 ----
    EMBEDDING_PROVIDER: str = os.getenv("EMBEDDING_PROVIDER", "ollama")
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "bge-m3:latest")
    EMBEDDING_API_BASE: str = os.getenv("EMBEDDING_API_BASE", "http://localhost:11434")
    EMBEDDING_API_KEY: str = os.getenv("EMBEDDING_API_KEY", "")
    EMBEDDING_API_MODEL: str = os.getenv("EMBEDDING_API_MODEL", "text-embedding-3-small")
    EMBEDDING_REQUEST_TIMEOUT: float = float(os.getenv("EMBEDDING_REQUEST_TIMEOUT", "20"))
    # 独立于本地索引加载；启用时启动发送一条无敏感内容的真实模型请求。
    EMBEDDING_STARTUP_PROBE: bool = os.getenv("EMBEDDING_STARTUP_PROBE", "false").lower() == "true"

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
    # Markdown 章节整块上限：章节（含子小节）不超过该值时保持一个整块，保留模块完整性
    MARKDOWN_SECTION_MAX_CHARS: int = int(os.getenv("MARKDOWN_SECTION_MAX_CHARS", "1000"))
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

    # ---- 公开链接解析：浏览器仅在隔离容器运行 ----
    URL_RENDERER_ENABLED: bool = os.getenv("URL_RENDERER_ENABLED", "false").lower() == "true"
    URL_RENDERER_SOCKET: str = os.getenv("URL_RENDERER_SOCKET", "/ipc/renderer.sock")

    # ---- 数据目录 ----
    UPLOAD_DIR: str = str(DATA_DIR / "uploads")
    KNOWLEDGE_DIR: str = str(DATA_DIR / "knowledge")
    # llm-wiki 百科镜像目录：编译产物同时以 Markdown 落盘，可浏览/导出为知识站点
    WIKI_DIR: str = os.getenv("WIKI_DIR", str(DATA_DIR / "wiki"))
    # Skill 文档库：OpenClaw/Anthropic Agent Skills 结构，每个技能一个含 SKILL.md 的目录，
    # 页面卡片只展示标题+简述，完整说明书与 references 按需（渐进式/懒加载）拉取。
    # 位于后端项目目录 backend/skills（BASE_DIR 为仓库根）。
    SKILLS_DIR: str = os.getenv("SKILLS_DIR", str(BASE_DIR / "backend" / "skills"))

    # ---- 会话 ----
    # 历史窗口兼容参数：四层压缩改造后历史区按「连续窗口全量原文 + 压缩边界」装配，
    # 该值仅作为 build_context_messages/get_history 的兼容入参，不再按轮数裁剪历史。
    SESSION_MAX_HISTORY: int = int(os.getenv("SESSION_MAX_HISTORY", "8"))
    # 聊天页「上下文用量」展示口径：会话级上下文窗口大小（token，名义值 128K=128000）。
    # 一个会话 = 一个上下文窗口，四层压缩（日常约束/微压缩/系统压缩/兜底压缩）只作用
    # 于本会话窗口；所有 Token 量纲以 K=1000 对齐。部署模型窗口不同可环境变量覆盖。
    CHAT_CONTEXT_WINDOW: int = int(os.getenv("CHAT_CONTEXT_WINDOW", "128000"))
    CHAT_REQUEST_TIMEOUT: float = float(os.getenv("CHAT_REQUEST_TIMEOUT", "300"))
    # ---- L0 日常约束 · 模型输出上限（Output Cap，对齐 Claude Code 每轮输出帽）----
    # 主链路每轮模型输出上限（token）；压缩代理（memory_agent）显式用摘要输出预留值。
    LLM_OUTPUT_CAP_TOKENS: int = int(os.getenv("LLM_OUTPUT_CAP_TOKENS", "4000"))

    # ---- 上下文预算（Token 优化 P0）----
    # 附件工具结果注入对话上下文的字符预算：单附件上限 + 全部附件总量上限，超限截断并标注。
    # 附件（OCR/文档解析原文）是单次请求 Token 尖峰的最大来源，须设硬预算。
    ATTACHMENT_CTX_PER_ITEM_CHARS: int = int(os.getenv("ATTACHMENT_CTX_PER_ITEM_CHARS", "1500"))
    ATTACHMENT_CTX_TOTAL_CHARS: int = int(os.getenv("ATTACHMENT_CTX_TOTAL_CHARS", "4000"))
    # 截图检索：图片 OCR 文本并入知识检索查询时的字符上限。
    # 检索查询过长会稀释向量/BM25 信号（完整原文仍注入对话上下文），故单独设更紧的预算。
    OCR_RETRIEVAL_CHARS: int = int(os.getenv("OCR_RETRIEVAL_CHARS", "300"))
    # 实时类助手答案入历史即衰减的字符上限（位置无关规则，四层压缩改造后保留）。
    CLIP_OLD_CHARS: int = int(os.getenv("CLIP_OLD_CHARS", "200"))
    # ---- 上下文四层压缩治理（对齐 Claude Code：日常约束→微压缩→系统压缩→兜底压缩）----
    # 一个会话 = 一个上下文窗口（CHAT_CONTEXT_WINDOW，名义 128K）；以下阈值均由窗口推导。
    # 常驻「用户画像卡」注入字符预算（高重要度/偏好类记忆，每会话必载）。
    MEMORY_PROFILE_CHARS: int = int(os.getenv("MEMORY_PROFILE_CHARS", "300"))
    # 记忆动态加载（画像卡 + 相关检索）合计注入字符预算。
    MEMORY_CTX_CHARS: int = int(os.getenv("MEMORY_CTX_CHARS", "600"))
    # L0 工具结果落盘（Tool-Result Offloading）：单个结果超过该 token 数写盘，上下文只留预览+路径。
    TOOL_RESULT_OFFLOAD_TOKENS: int = int(os.getenv("TOOL_RESULT_OFFLOAD_TOKENS", "8000"))
    # L0 单轮工具结果合计预算：超出时从最大的结果开始落盘。
    TOOL_RESULT_TURN_BUDGET_TOKENS: int = int(os.getenv("TOOL_RESULT_TURN_BUDGET_TOKENS", "16000"))
    # L0 落盘结果在上下文保留的预览字节数（约 2KB，UTF-8 安全按行截断）。
    TOOL_RESULT_PREVIEW_BYTES: int = int(os.getenv("TOOL_RESULT_PREVIEW_BYTES", "2048"))
    # L0 回读工具单次分页返回的 token 上限。
    TOOL_RESULT_READ_PAGE_TOKENS: int = int(os.getenv("TOOL_RESULT_READ_PAGE_TOKENS", "4000"))
    # L1 微压缩（Micro Compaction）：窗口内工具结果合计达到该值进入微压缩。
    MICRO_COMPACT_TRIGGER_TOKENS: int = int(os.getenv("MICRO_COMPACT_TRIGGER_TOKENS", "20000"))
    # L1 可释放量低于该值则放弃微压缩（收益不抵前缀缓存失效代价）。
    MICRO_COMPACT_MIN_FREE_TOKENS: int = int(os.getenv("MICRO_COMPACT_MIN_FREE_TOKENS", "10000"))
    # L1 保护集：最近 N 个工具结果不清除（连同本轮结果）。
    MICRO_COMPACT_KEEP_RECENT: int = int(os.getenv("MICRO_COMPACT_KEEP_RECENT", "3"))
    # L2a Session Memory 首次触发：会话水位第一次达到该值创建 summary.md（对齐 CC 10K）。
    CONTEXT_COMPACT_TRIGGER_TOKENS: int = int(os.getenv("CONTEXT_COMPACT_TRIGGER_TOKENS", "10000"))
    # L2a Session Memory 后续更新：距上次摘要水位增长 ≥ 该值（与工具调用数、自然断点三者同时满足）。
    SESSION_MEMORY_GROWTH_TOKENS: int = int(os.getenv("SESSION_MEMORY_GROWTH_TOKENS", "5000"))
    # L2a Session Memory 后续更新：期间工具调用累计 ≥ 该次数。
    SESSION_MEMORY_TOOL_CALLS: int = int(os.getenv("SESSION_MEMORY_TOOL_CALLS", "3"))
    # L2a Session Memory 摘要正文 token 上限（九段式结构化摘要）。
    SESSION_MEMORY_MAX_TOKENS: int = int(os.getenv("SESSION_MEMORY_MAX_TOKENS", "2000"))
    # L2b Auto Compaction：预留给摘要输出的 token（压缩代理输出上限）。
    COMPACT_SUMMARY_RESERVE_TOKENS: int = int(os.getenv("COMPACT_SUMMARY_RESERVE_TOKENS", "20000"))
    # L2b Auto Compaction：输入安全缓冲 token。触发阈值 = 窗口 − 摘要预留 − 输入缓冲（128K→95K）。
    COMPACT_INPUT_BUFFER_TOKENS: int = int(os.getenv("COMPACT_INPUT_BUFFER_TOKENS", "13000"))
    # L2b 压缩后保留区下限 token（至少保留这么多原文）。
    AUTO_COMPACT_KEEP_MIN_TOKENS: int = int(os.getenv("AUTO_COMPACT_KEEP_MIN_TOKENS", "10000"))
    # L2b 压缩后至少保留最近 N 轮对话（一轮=一问一答，含其工具配对）。
    AUTO_COMPACT_KEEP_RECENT_TURNS: int = int(os.getenv("AUTO_COMPACT_KEEP_RECENT_TURNS", "3"))
    # L2b 保留区上限 token（命中零调用路径要求覆盖点之后全部消息可放入该上限）。
    AUTO_COMPACT_KEEP_MAX_TOKENS: int = int(os.getenv("AUTO_COMPACT_KEEP_MAX_TOKENS", "40000"))
    # L3 兜底压缩（Full Compaction）：上下文占窗口比例达到该值（93%）强制兜底。
    FULL_COMPACT_WINDOW_RATIO: float = float(os.getenv("FULL_COMPACT_WINDOW_RATIO", "0.93"))
    # L2/L3 熔断阈值：Auto 连续失败达到该值升级为 Full（force 手动不受限）。
    COMPACT_FAIL_STREAK_LIMIT: int = int(os.getenv("COMPACT_FAIL_STREAK_LIMIT", "3"))
    # ---- 已退役配置（保留仅为兼容旧 .env，四层压缩改造后不再生效）----
    # 对话暂停落盘 task_state 的空闲阈值（摘要触发语义已退役，落盘行为保留）。
    SESSION_MEMORY_PAUSE_MINUTES: float = float(os.getenv("SESSION_MEMORY_PAUSE_MINUTES", "10"))
    COMPACT_WINDOW_RATIO: float = float(os.getenv("COMPACT_WINDOW_RATIO", "0.8"))
    HISTORY_MSG_MAX_CHARS: int = int(os.getenv("HISTORY_MSG_MAX_CHARS", "800"))
    KEEP_RECENT_FULL: int = int(os.getenv("KEEP_RECENT_FULL", "4"))
    FULL_ZONE_MAX_CHARS: int = int(os.getenv("FULL_ZONE_MAX_CHARS", "1200"))
    SUMMARY_TRIGGER_MESSAGE_COUNT: int = int(os.getenv("SUMMARY_TRIGGER_MESSAGE_COUNT", "12"))
    SUMMARY_TRIGGER_CHAR_COUNT: int = int(os.getenv("SUMMARY_TRIGGER_CHAR_COUNT", "6000"))
    CLIP_MID_CHARS: int = int(os.getenv("CLIP_MID_CHARS", "400"))
    SUMMARY_BATCH_MIN_MESSAGES: int = int(os.getenv("SUMMARY_BATCH_MIN_MESSAGES", "6"))
    SUMMARY_MAX_CHARS: int = int(os.getenv("SUMMARY_MAX_CHARS", "400"))
    MICRO_COMPACT_IDLE_MINUTES: float = float(os.getenv("MICRO_COMPACT_IDLE_MINUTES", "60"))
    # ---- 用户 Skill 配置（可管理的技能：chat 提示注入 / url_import 站点抓取策略）----
    # 单用户可创建的自定义 Skill 上限（内置 Skill 不计入）。
    MAX_SKILLS_PER_USER: int = int(os.getenv("MAX_SKILLS_PER_USER", "50"))
    # 聊天注入的已启用 prompt 类 Skill 合计字符预算。
    SKILL_PROMPT_CHARS: int = int(os.getenv("SKILL_PROMPT_CHARS", "1500"))
    # 管理员 Token 告警阈值：单请求平均 prompt+completion token 超过该值即视为「用量偏高」。
    TOKEN_AVG_WARN_PER_REQUEST: int = int(os.getenv("TOKEN_AVG_WARN_PER_REQUEST", "6000"))
    PROXY_REQUEST_TIMEOUT: float = float(os.getenv("PROXY_REQUEST_TIMEOUT", "240"))
    PROXY_DIAGNOSTIC_TOKEN: str = os.getenv("PROXY_DIAGNOSTIC_TOKEN", "")
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
    # 两步思维链编译开关：Step1 输出结构化分析 JSON（实体/论点/关联/矛盾）→ Step2 基于分析生成
    WIKI_TWO_STEP_ENABLED: bool = os.getenv("WIKI_TWO_STEP_ENABLED", "true").lower() == "true"
    # SHA256 增量缓存：同一来源文本未变更则跳过重复编译（force 重编译可绕过）
    WIKI_INCREMENTAL_ENABLED: bool = os.getenv("WIKI_INCREMENTAL_ENABLED", "true").lower() == "true"
    # 持久化编译队列单任务最大尝试次数（含首次），失败达到上限标记 failed
    WIKI_COMPILE_MAX_ATTEMPTS: int = int(os.getenv("WIKI_COMPILE_MAX_ATTEMPTS", "3"))
    # 编译产物语言（影响两步思维链提示词与派生视图措辞）
    WIKI_LANG: str = os.getenv("WIKI_LANG", "zh")

    # ---- llm-wiki 对话/检索历史沉淀（路径A：高频主题 → digest 私人综合词条） ----
    # 独立开关：周期性从用户对话/检索历史归纳高频主题，自动综合成 visibility=private
    # 的私人百科词条（复用 digest 编译内核）；关闭后不影响上传编译与问答主链路。
    WIKI_CONVERSATION_COMPILE_ENABLED: bool = os.getenv("WIKI_CONVERSATION_COMPILE_ENABLED", "true").lower() == "true"
    # 质量闸门：主题需在窗口内被 ≥N 次不同提问命中才沉淀（过滤一次性提问噪声）
    WIKI_CONVERSATION_MIN_HITS: int = int(os.getenv("WIKI_CONVERSATION_MIN_HITS", "3"))
    # 统计回溯窗口（天）
    WIKI_CONVERSATION_WINDOW_DAYS: int = int(os.getenv("WIKI_CONVERSATION_WINDOW_DAYS", "30"))
    # 周期任务运行间隔（小时）
    WIKI_CONVERSATION_INTERVAL_HOURS: float = float(os.getenv("WIKI_CONVERSATION_INTERVAL_HOURS", "24"))
    # 单用户单次最多沉淀的主题数（控制 LLM 综合成本）
    WIKI_CONVERSATION_MAX_TOPICS: int = int(os.getenv("WIKI_CONVERSATION_MAX_TOPICS", "3"))
    # 参与归纳的近期提问条数上限（控制 LLM 输入成本）
    WIKI_CONVERSATION_MAX_QUERIES: int = int(os.getenv("WIKI_CONVERSATION_MAX_QUERIES", "60"))
    # 参与主题归纳的单条提问最小长度（过滤「好的」「谢谢」等寒暄）
    WIKI_CONVERSATION_MIN_QUERY_LEN: int = int(os.getenv("WIKI_CONVERSATION_MIN_QUERY_LEN", "4"))

    # ---- 语义缓存（构建在网关层：代理拦截 chat_answer，命中直回、跳过上游 LLM） ----
    # 缓存分「公共/个人」域：公共域所有用户共享复用，个人域仅本用户可见；
    # 并按「实时/非实时」+ 命中热度分档设置 TTL（阶梯形过期）。
    # 总开关（可在系统设置页运行时覆盖）
    SEMANTIC_CACHE_ENABLED: bool = os.getenv("SEMANTIC_CACHE_ENABLED", "true").lower() == "true"
    # 命中判定阈值（余弦相似度下限，clamp 0.5-0.99，系统设置页可运行时覆盖）。
    # 分层决策：[阈值, 0.95) 走 LLM 等价性二次验证防误命中；≥0.95 高置信直放。
    # 中文短问同义改述余弦实测约 0.74-0.90，默认 0.70 配合验证带兼顾命中与安全。
    SEMANTIC_CACHE_THRESHOLD: float = float(os.getenv("SEMANTIC_CACHE_THRESHOLD", "0.70"))
    # v2 合同必须由业务后端签发；缺少至少 32 字符的独立密钥时安全旁路。
    ANSWER_CACHE_SIGNING_KEY: str = os.getenv("ANSWER_CACHE_SIGNING_KEY", "")
    ANSWER_CACHE_TTL_SECONDS: int = int(os.getenv("ANSWER_CACHE_TTL_SECONDS", "86400"))
    # 单用户（个人域）缓存条目上限（超出按最近最少使用淘汰）
    SEMANTIC_CACHE_MAX_ENTRIES: int = int(os.getenv("SEMANTIC_CACHE_MAX_ENTRIES", "200"))
    # 参与匹配/写入的提问最小长度（过滤过短寒暄）
    SEMANTIC_CACHE_MIN_QUESTION_CHARS: int = int(os.getenv("SEMANTIC_CACHE_MIN_QUESTION_CHARS", "4"))
    # 可写入缓存的回答最小长度（过短回答不缓存，避免污染）
    SEMANTIC_CACHE_MIN_ANSWER_CHARS: int = int(os.getenv("SEMANTIC_CACHE_MIN_ANSWER_CHARS", "30"))
    # ---- 阶梯 TTL（实时短、非实时按命中热度升级；单位：小时/天）----
    # 实时类（天气/新闻/股价等时效问题）：固定短 TTL，过期即失效避免陈旧数据
    SEMANTIC_CACHE_TTL_REALTIME_HOURS: int = int(os.getenv("SEMANTIC_CACHE_TTL_REALTIME_HOURS", "2"))
    # 非实时类三档：冷数据(命中<WARM) / 常命中(WARM≤命中<HOT) / 热点(命中≥HOT)
    SEMANTIC_CACHE_TTL_COLD_DAYS: int = int(os.getenv("SEMANTIC_CACHE_TTL_COLD_DAYS", "3"))
    SEMANTIC_CACHE_TTL_WARM_DAYS: int = int(os.getenv("SEMANTIC_CACHE_TTL_WARM_DAYS", "7"))
    SEMANTIC_CACHE_TTL_HOT_DAYS: int = int(os.getenv("SEMANTIC_CACHE_TTL_HOT_DAYS", "30"))
    # 热度阶梯阈值：命中次数达到即升档（冷→常命中→热点）
    SEMANTIC_CACHE_WARM_HITS: int = int(os.getenv("SEMANTIC_CACHE_WARM_HITS", "3"))
    SEMANTIC_CACHE_HOT_HITS: int = int(os.getenv("SEMANTIC_CACHE_HOT_HITS", "10"))

    # ---- 可观测性告警实时分析（质量/性能指标越限 → 后台归因 + 原因分析/优化建议） ----
    ALERT_SCAN_INTERVAL_SECONDS: int = int(os.getenv("ALERT_SCAN_INTERVAL_SECONDS", "60"))
    # 同指纹告警冷却（分钟）：冷却期内不重复建单；指标回落后自动 resolved
    ALERT_COOLDOWN_MINUTES: int = int(os.getenv("ALERT_COOLDOWN_MINUTES", "30"))
    # 性能规则阈值：错误率（error+timeout+interrupted 占比）/ P95 延迟（ms）
    ALERT_ERROR_RATE_THRESHOLD: float = float(os.getenv("ALERT_ERROR_RATE_THRESHOLD", "0.10"))
    ALERT_P95_LATENCY_MS: int = int(os.getenv("ALERT_P95_LATENCY_MS", "30000"))
    # 质量规则阈值：回归均分下限 / 点踩占比上限 / 在线评测均分下限
    ALERT_EVAL_SCORE_MIN: float = float(os.getenv("ALERT_EVAL_SCORE_MIN", "0.6"))
    ALERT_DISLIKE_RATE_THRESHOLD: float = float(os.getenv("ALERT_DISLIKE_RATE_THRESHOLD", "0.30"))
    ALERT_ONLINE_SCORE_MIN: float = float(os.getenv("ALERT_ONLINE_SCORE_MIN", "0.6"))
    # 规则生效的最小样本量（避免小样本误报）
    ALERT_MIN_SAMPLES: int = int(os.getenv("ALERT_MIN_SAMPLES", "5"))

    # ---- 在线采样评测（DeepEval 风格：线上真实问答抽样跑质量指标，回流质量监控） ----
    ONLINE_EVAL_ENABLED: bool = os.getenv("ONLINE_EVAL_ENABLED", "true").lower() == "true"
    ONLINE_EVAL_INTERVAL_SECONDS: int = int(os.getenv("ONLINE_EVAL_INTERVAL_SECONDS", "300"))
    ONLINE_EVAL_SAMPLE_RATE: float = float(os.getenv("ONLINE_EVAL_SAMPLE_RATE", "0.1"))
    ONLINE_EVAL_MAX_PER_ROUND: int = int(os.getenv("ONLINE_EVAL_MAX_PER_ROUND", "5"))
    # GEval 默认评估准则（在线评测与未配置准则的离线 GEval 共用）
    ONLINE_EVAL_GEVAL_CRITERIA: str = os.getenv(
        "ONLINE_EVAL_GEVAL_CRITERIA", "回答准确、有用、与问题相关，表述清晰无误导")

    # ---- 评测体系升级：RAG 排名指标 / 测试集构建 / 在线业务指标 / BadCase / Agent 评测 ----
    # Hit@K 默认 K（评估器 hit_k 可覆盖）；MRR=首命中排名倒数，确定性排名匹配零 LLM 成本
    RETRIEVAL_HIT_K: int = int(os.getenv("RETRIEVAL_HIT_K", "5"))
    # 追问率口径：助手回答后窗口内同会话出现用户新消息计为追问
    FOLLOWUP_WINDOW_SECONDS: int = int(os.getenv("FOLLOWUP_WINDOW_SECONDS", "300"))
    # 日志挖掘：新增问答配对达阈值才触发候选生成；周期任务开关/间隔
    LOG_MINING_MIN_PAIRS: int = int(os.getenv("LOG_MINING_MIN_PAIRS", "20"))
    TESTSET_MINING_ENABLED: bool = os.getenv("TESTSET_MINING_ENABLED", "true").lower() == "true"
    TESTSET_MINING_INTERVAL_SECONDS: int = int(os.getenv("TESTSET_MINING_INTERVAL_SECONDS", "3600"))
    # BadCase 周期收集间隔（另在离线 run 结束时增量触发）
    BADCASE_SCAN_INTERVAL_SECONDS: int = int(os.getenv("BADCASE_SCAN_INTERVAL_SECONDS", "300"))
    # 质量告警新规则阈值：空回答率上限 / Agent 任务完成均分下限
    ALERT_EMPTY_ANSWER_RATE_THRESHOLD: float = float(os.getenv("ALERT_EMPTY_ANSWER_RATE_THRESHOLD", "0.10"))
    ALERT_AGENT_SCORE_MIN: float = float(os.getenv("ALERT_AGENT_SCORE_MIN", "0.6"))
    # Agent 在线 trace 采样评测（无参考指标）开关/间隔/采样率/单轮上限
    AGENT_ONLINE_EVAL_ENABLED: bool = os.getenv("AGENT_ONLINE_EVAL_ENABLED", "true").lower() == "true"
    AGENT_ONLINE_INTERVAL_SECONDS: int = int(os.getenv("AGENT_ONLINE_INTERVAL_SECONDS", "300"))
    AGENT_ONLINE_SAMPLE_RATE: float = float(os.getenv("AGENT_ONLINE_SAMPLE_RATE", "0.1"))
    AGENT_ONLINE_MAX_PER_ROUND: int = int(os.getenv("AGENT_ONLINE_MAX_PER_ROUND", "5"))
    # Agent 离线评测单用例真实运行超时（秒）：超时该用例降级 error，不中断 run
    AGENT_EVAL_CASE_TIMEOUT: int = int(os.getenv("AGENT_EVAL_CASE_TIMEOUT", "180"))


settings = Settings()

# 创建必要的目录
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
os.makedirs(Path(settings.VECTOR_STORE_PATH).parent, exist_ok=True)
os.makedirs(settings.KNOWLEDGE_DIR, exist_ok=True)
os.makedirs(settings.WIKI_DIR, exist_ok=True)
