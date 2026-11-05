"""
见字如面 - SQLite 存储层
统一管理用户、会话、上传记录、收藏、知识库、反馈、用量统计的持久化
"""
import logging
import sqlite3
import threading
import time
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("jianziruyang.db")

# SQLite 数据库存放在 backend 项目内部：backend/data/app.db
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
DB_PATH = BACKEND_DIR / "data" / "app.db"

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def get_conn() -> sqlite3.Connection:
    """获取全局 SQLite 连接（懒初始化，WAL 模式）"""
    global _conn
    with _lock:
        if _conn is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA busy_timeout=30000")
                deadline = time.monotonic() + 30
                while True:
                    try:
                        conn.execute("PRAGMA journal_mode=WAL")
                        break
                    except sqlite3.OperationalError as exc:
                        # 模式切换的锁升级可能直接返回 BUSY，不经过 busy_timeout。
                        code = getattr(exc, "sqlite_errorcode", 0) & 0xff
                        if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or time.monotonic() >= deadline:
                            raise
                        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
                conn.execute("PRAGMA foreign_keys=ON")
                init_db(conn)
            except BaseException:
                conn.rollback()
                conn.close()
                raise
            _conn = conn
        return _conn


def init_db(conn: sqlite3.Connection):
    """建表（幂等）"""
    conn.executescript(
        """
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chat_pending_actions (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            file_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_hash TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            kind TEXT NOT NULL,
            kb_id TEXT NOT NULL DEFAULT '',
            visibility TEXT NOT NULL DEFAULT 'private',
            status TEXT NOT NULL DEFAULT 'pending',
            result TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            UNIQUE(session_id, message_id, file_id)
        );
        CREATE INDEX IF NOT EXISTS idx_chat_actions_session
            ON chat_pending_actions(session_id, message_id);
        CREATE TABLE IF NOT EXISTS users (
            id            TEXT PRIMARY KEY,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt          TEXT NOT NULL,
            hash_algo     TEXT NOT NULL DEFAULT 'pbkdf2',
            contact       TEXT NOT NULL DEFAULT '',
            role          TEXT NOT NULL DEFAULT 'user',
            is_active     INTEGER NOT NULL DEFAULT 1,
            created_at    REAL NOT NULL,
            updated_at    REAL NOT NULL,
            last_login_at REAL
        );

        CREATE TABLE IF NOT EXISTS chat_sessions (
            session_id TEXT PRIMARY KEY,
            user_id    TEXT NOT NULL DEFAULT '',
            title      TEXT NOT NULL DEFAULT '新对话',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chat_messages (
            message_id     TEXT PRIMARY KEY,
            session_id     TEXT NOT NULL,
            seq            INTEGER NOT NULL,
            role           TEXT NOT NULL,
            content        TEXT NOT NULL,
            sources        TEXT NOT NULL DEFAULT '[]',
            search_results TEXT NOT NULL DEFAULT '[]',
            use_knowledge  INTEGER NOT NULL DEFAULT 0,
            use_search     INTEGER NOT NULL DEFAULT 0,
            attachments    TEXT NOT NULL DEFAULT '[]',
            created_at     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_session ON chat_messages(session_id, seq);

        CREATE TABLE IF NOT EXISTS upload_records (
            id               TEXT PRIMARY KEY,
            filename         TEXT NOT NULL,
            title            TEXT NOT NULL DEFAULT '',
            image_path       TEXT NOT NULL,
            ocr_text         TEXT NOT NULL DEFAULT '',
            ocr_provider     TEXT NOT NULL DEFAULT 'local',
            chunk_count      INTEGER NOT NULL DEFAULT 0,
            doc_ids          TEXT NOT NULL DEFAULT '[]',
            visibility       TEXT NOT NULL DEFAULT 'public',
            owner_id         TEXT NOT NULL DEFAULT '',
            source_type      TEXT NOT NULL DEFAULT 'image',
            pages            TEXT NOT NULL DEFAULT '[]',
            pdf_preview_path TEXT,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bookmarks (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            title       TEXT NOT NULL,
            tags        TEXT NOT NULL DEFAULT '[]',
            content     TEXT NOT NULL DEFAULT '',
            source_type TEXT NOT NULL DEFAULT 'knowledge',
            source_info TEXT NOT NULL DEFAULT '{}',
            created_at  REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_bookmarks_user ON bookmarks(user_id);

        CREATE TABLE IF NOT EXISTS knowledge_bases (
            id         TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            topic      TEXT NOT NULL DEFAULT '其他',
            visibility TEXT NOT NULL DEFAULT 'public',
            owner_id   TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS message_feedbacks (
            id         TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            user_id    TEXT NOT NULL DEFAULT '',
            rating     TEXT NOT NULL,
            comment    TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_message
            ON message_feedbacks(message_id, user_id);

        CREATE TABLE IF NOT EXISTS usage_daily (
            user_id         TEXT NOT NULL,
            day             TEXT NOT NULL,
            query_count     INTEGER NOT NULL DEFAULT 0,
            prompt_chars    INTEGER NOT NULL DEFAULT 0,
            completion_chars INTEGER NOT NULL DEFAULT 0,
            UNIQUE(user_id, day)
        );

        CREATE TABLE IF NOT EXISTS auth_sessions (
            token_hash   TEXT PRIMARY KEY,
            user_id      TEXT NOT NULL,
            expires_at   REAL NOT NULL,
            created_at   REAL NOT NULL,
            last_seen_at REAL NOT NULL,
            user_agent   TEXT NOT NULL DEFAULT '',
            ip           TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);
        CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires ON auth_sessions(expires_at);

        CREATE TABLE IF NOT EXISTS guest_usage (
            ip_hash    TEXT NOT NULL,
            day        TEXT NOT NULL,
            count      INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(ip_hash, day)
        );

        CREATE TABLE IF NOT EXISTS user_memories (
            id                TEXT PRIMARY KEY,
            user_id           TEXT NOT NULL,
            memory_type       TEXT NOT NULL DEFAULT 'fact',
            content           TEXT NOT NULL,
            keywords          TEXT NOT NULL DEFAULT '[]',
            source_session_id TEXT NOT NULL DEFAULT '',
            source_message_id TEXT NOT NULL DEFAULT '',
            importance        REAL NOT NULL DEFAULT 0.5,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL,
            last_used_at      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_user_memories_user ON user_memories(user_id, updated_at);

        CREATE TABLE IF NOT EXISTS user_skills (
            id          TEXT PRIMARY KEY,
            owner_id    TEXT NOT NULL,
            name        TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            kind        TEXT NOT NULL DEFAULT 'prompt',
            scope       TEXT NOT NULL DEFAULT 'chat',
            content     TEXT NOT NULL DEFAULT '',
            enabled     INTEGER NOT NULL DEFAULT 1,
            builtin     INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_user_skills_owner ON user_skills(owner_id, updated_at);

        CREATE TABLE IF NOT EXISTS skill_review_logs (
            id              TEXT PRIMARY KEY,
            user_id         TEXT NOT NULL,
            username        TEXT NOT NULL DEFAULT '',
            action          TEXT NOT NULL DEFAULT 'create',
            skill_name      TEXT NOT NULL DEFAULT '',
            kind            TEXT NOT NULL DEFAULT '',
            scope           TEXT NOT NULL DEFAULT '',
            verdict         TEXT NOT NULL DEFAULT 'pass',
            risk            TEXT NOT NULL DEFAULT '',
            reasons         TEXT NOT NULL DEFAULT '[]',
            content_excerpt TEXT NOT NULL DEFAULT '',
            created_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_skill_review_logs_created ON skill_review_logs(created_at);

        CREATE TABLE IF NOT EXISTS chat_session_summaries (
            session_id               TEXT PRIMARY KEY,
            user_id                  TEXT NOT NULL,
            summary                  TEXT NOT NULL,
            covered_until_message_id TEXT NOT NULL DEFAULT '',
            token_estimate           INTEGER NOT NULL DEFAULT 0,
            created_at               TEXT NOT NULL,
            updated_at               TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tool_calls (
            id          TEXT PRIMARY KEY,
            session_id  TEXT NOT NULL,
            message_id  TEXT NOT NULL,
            user_id     TEXT NOT NULL DEFAULT '',
            tool_name   TEXT NOT NULL,
            input_json  TEXT NOT NULL DEFAULT '{}',
            output_json TEXT NOT NULL DEFAULT '{}',
            status      TEXT NOT NULL DEFAULT 'success',
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id, created_at);

        -- llm-wiki 知识编译层：把原始资料编译成结构化百科卡片/综合报告
        -- page_type: source(单来源知识卡片) | digest(跨素材综合报告)
        -- source_hash: 原始资料文本 SHA256，用于增量编译（未变更跳过）
        -- analysis:    两步思维链 Step1 产出的结构化分析 JSON（实体/论点/关联/矛盾）
        CREATE TABLE IF NOT EXISTS wiki_pages (
            id          TEXT PRIMARY KEY,
            page_type   TEXT NOT NULL DEFAULT 'source',
            title       TEXT NOT NULL,
            content     TEXT NOT NULL DEFAULT '',
            kb_id       TEXT NOT NULL DEFAULT '',
            visibility  TEXT NOT NULL DEFAULT 'public',
            owner_id    TEXT NOT NULL DEFAULT '',
            source_image TEXT NOT NULL DEFAULT '',
            source_refs TEXT NOT NULL DEFAULT '[]',
            doc_ids     TEXT NOT NULL DEFAULT '[]',
            source_hash TEXT NOT NULL DEFAULT '',
            analysis    TEXT NOT NULL DEFAULT '{}',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_wiki_pages_kb ON wiki_pages(kb_id);
        CREATE INDEX IF NOT EXISTS idx_wiki_pages_source ON wiki_pages(source_image);

        -- llm-wiki 持久化编译队列：串行处理防止并发 LLM 调用，重启后自动恢复未完成任务，
        -- 失败任务自动重试（最多 WIKI_COMPILE_MAX_ATTEMPTS 次）。
        -- status: pending(排队) | processing(处理中) | done(完成) | skipped(哈希未变跳过) | failed(重试耗尽)
        CREATE TABLE IF NOT EXISTS wiki_compile_queue (
            id           TEXT PRIMARY KEY,
            source_image TEXT NOT NULL DEFAULT '',
            source_hash  TEXT NOT NULL DEFAULT '',
            payload      TEXT NOT NULL DEFAULT '{}',
            status       TEXT NOT NULL DEFAULT 'pending',
            attempts     INTEGER NOT NULL DEFAULT 0,
            error        TEXT NOT NULL DEFAULT '',
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_wiki_queue_status ON wiki_compile_queue(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_wiki_queue_source ON wiki_compile_queue(source_image);

        -- 知识助手 Agent 提议的百科页面编辑（human-in-the-loop）：Agent 不直接改库，
        -- 而是生成待确认编辑，由用户在助手中确认后 apply_edit 才真正落库/重索引/镜像。
        -- status: pending(待确认) | approved(已批准并应用) | rejected(已驳回)
        CREATE TABLE IF NOT EXISTS wiki_pending_edits (
            id          TEXT PRIMARY KEY,
            page_id     TEXT NOT NULL,
            page_title  TEXT NOT NULL DEFAULT '',
            old_content TEXT NOT NULL DEFAULT '',
            new_content TEXT NOT NULL DEFAULT '',
            reason      TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'pending',
            created_by  TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_wiki_pending_status ON wiki_pending_edits(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_wiki_pending_page ON wiki_pending_edits(page_id);

        -- 知识助手历史会话：按用户持久化问答，供面板回看与多轮上下文。
        CREATE TABLE IF NOT EXISTS assistant_sessions (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL DEFAULT '',
            title       TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_assistant_sessions_user ON assistant_sessions(user_id, updated_at);

        -- 知识助手消息：role(user/assistant) + meta(JSON: thoughts/tools/clarify/edit/error/stopped)。
        CREATE TABLE IF NOT EXISTS assistant_messages (
            id          TEXT PRIMARY KEY,
            session_id  TEXT NOT NULL,
            seq         INTEGER NOT NULL,
            role        TEXT NOT NULL,
            content     TEXT NOT NULL DEFAULT '',
            meta        TEXT NOT NULL DEFAULT '{}',
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_assistant_messages_session ON assistant_messages(session_id, seq);

        -- 知识库上传流水线：文件上传后先后台解析，用户确认后再入库。
        -- status: pending(排队中) | parsing(解析中) | done(解析完成待入库) | error(解析失败)
        CREATE TABLE IF NOT EXISTS pending_uploads (
            id               TEXT PRIMARY KEY,
            filename         TEXT NOT NULL,
            source_type      TEXT NOT NULL DEFAULT 'image',
            file_path        TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'pending',
            parsed_text      TEXT NOT NULL DEFAULT '',
            pages            TEXT NOT NULL DEFAULT '[]',
            pdf_preview_path TEXT,
            ocr_provider     TEXT NOT NULL DEFAULT '',
            error            TEXT NOT NULL DEFAULT '',
            owner_id         TEXT NOT NULL DEFAULT '',
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_pending_owner ON pending_uploads(owner_id, created_at);

        CREATE TABLE IF NOT EXISTS url_imports (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            source_url TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            parsed_text TEXT NOT NULL DEFAULT '',
            original_title TEXT NOT NULL DEFAULT '',
            original_text TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            phase TEXT NOT NULL DEFAULT 'parse',
            revision INTEGER NOT NULL DEFAULT 1,
            kb_id TEXT NOT NULL DEFAULT '',
            file_path TEXT NOT NULL DEFAULT '',
            result TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            error_code TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_url_import_owner ON url_imports(owner_id, created_at);

        -- Agent 回归评测模块：评测集 → 用例 → 运行 → 逐用例结果
        CREATE TABLE IF NOT EXISTS eval_datasets (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS eval_cases (
            id               TEXT PRIMARY KEY,
            dataset_id       TEXT NOT NULL,
            question         TEXT NOT NULL,
            expected_keywords TEXT NOT NULL DEFAULT '[]',
            reference_answer TEXT NOT NULL DEFAULT '',
            expected_source  TEXT NOT NULL DEFAULT '',
            created_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_eval_cases_dataset ON eval_cases(dataset_id);

        -- status: running(执行中) | finished(完成) | failed(整体失败) | interrupted(重启中断)
        CREATE TABLE IF NOT EXISTS eval_runs (
            id          TEXT PRIMARY KEY,
            dataset_id  TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'running',
            started_at  TEXT NOT NULL,
            finished_at TEXT,
            summary     TEXT NOT NULL DEFAULT '{}',
            created_by  TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_eval_runs_dataset ON eval_runs(dataset_id, started_at);

        CREATE TABLE IF NOT EXISTS eval_results (
            id                TEXT PRIMARY KEY,
            run_id            TEXT NOT NULL,
            case_id           TEXT NOT NULL,
            question          TEXT NOT NULL DEFAULT '',
            answer            TEXT NOT NULL DEFAULT '',
            retrieved_sources TEXT NOT NULL DEFAULT '[]',
            retrieval_hit     INTEGER NOT NULL DEFAULT 0,
            faithfulness_pass INTEGER NOT NULL DEFAULT 0,
            faithfulness      REAL,
            answer_relevance  REAL,
            context_precision REAL,
            context_recall    REAL,
            metric_details    TEXT NOT NULL DEFAULT '{}',
            judge_score       REAL,
            judge_reason      TEXT NOT NULL DEFAULT '',
            latency_ms        INTEGER NOT NULL DEFAULT 0,
            status            TEXT NOT NULL DEFAULT 'ok',
            error             TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_eval_results_run ON eval_results(run_id);

        -- Agent 可观测性（Tracing 主表）：一次用户请求 / 一次 Agent 运行 = 一条 trace。
        -- status: running(进行中) | ok(成功) | error(失败)；结束时由 span 聚合回写调用数与 token 数。
        CREATE TABLE IF NOT EXISTS agent_traces (
            id                TEXT PRIMARY KEY,
            agent             TEXT NOT NULL DEFAULT '',
            user_id           TEXT NOT NULL DEFAULT '',
            input             TEXT NOT NULL DEFAULT '',
            status            TEXT NOT NULL DEFAULT 'running',
            error             TEXT NOT NULL DEFAULT '',
            llm_calls         INTEGER NOT NULL DEFAULT 0,
            tool_calls        INTEGER NOT NULL DEFAULT 0,
            prompt_tokens     INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            latency_ms        INTEGER NOT NULL DEFAULT 0,
            started_at        TEXT NOT NULL,
            ended_at          TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_agent_traces_started ON agent_traces(started_at);
        CREATE INDEX IF NOT EXISTS idx_agent_traces_agent ON agent_traces(agent, started_at);

        -- Agent 可观测性（Tracing 明细表）：trace 内每一步 = 一条 span。
        -- kind: llm(LLM 调用，input/output 存完整 prompt 与 response) | tool(工具调用) | step(流程步骤)
        CREATE TABLE IF NOT EXISTS agent_spans (
            id                TEXT PRIMARY KEY,
            trace_id          TEXT NOT NULL,
            seq               INTEGER NOT NULL DEFAULT 0,
            kind              TEXT NOT NULL DEFAULT '',
            name              TEXT NOT NULL DEFAULT '',
            input             TEXT NOT NULL DEFAULT '',
            output            TEXT NOT NULL DEFAULT '',
            status            TEXT NOT NULL DEFAULT 'ok',
            error             TEXT NOT NULL DEFAULT '',
            prompt_tokens     INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            latency_ms        INTEGER NOT NULL DEFAULT 0,
            started_at        TEXT NOT NULL,
            ended_at          TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_agent_spans_trace ON agent_spans(trace_id, seq);
        CREATE INDEX IF NOT EXISTS idx_agent_spans_started ON agent_spans(started_at);

        -- Agent 可观测性（segment 级 Token 度量）：一次请求组装 LLM 上下文时，按语义段
        -- （system/memory/history/knowledge/search/tool）分别估算并沉淀 token 占用，
        -- 供管理员定位“Token 大户”与量化优化效果（GET /api/observability/metrics 的 segment_breakdown）。
        -- 与 agent_spans 分开存储：span 的 token 仅计 LLM 调用真实/估算用量，segment 只作上下文构成分析，避免重复计入 trace 总量。
        CREATE TABLE IF NOT EXISTS agent_token_segments (
            id          TEXT PRIMARY KEY,
            trace_id    TEXT NOT NULL DEFAULT '',
            agent       TEXT NOT NULL DEFAULT '',
            segment     TEXT NOT NULL DEFAULT '',
            tokens      INTEGER NOT NULL DEFAULT 0,
            chars       INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_token_segments_started ON agent_token_segments(created_at);
        CREATE INDEX IF NOT EXISTS idx_token_segments_seg ON agent_token_segments(segment, created_at);

        -- CI/CD 流水线（运维角色）：一次流水线运行 = 一条 pipeline_run，每个阶段 = 一条 pipeline_stage。
        -- 阶段固定六段：commit(代码提交) → unit_test(单元测试) → eval_regression(评估回归/EDD)
        --   → build_image(构建镜像) → canary(灰度发布) → full(全量上线)。
        -- run.status: running | finished(全部通过) | failed(某阶段失败并阻断) | aborted(人工中止)
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            id            TEXT PRIMARY KEY,
            trigger       TEXT NOT NULL DEFAULT 'manual',
            git_ref       TEXT NOT NULL DEFAULT '',
            status        TEXT NOT NULL DEFAULT 'running',
            current_stage TEXT NOT NULL DEFAULT '',
            edd_score     REAL,
            edd_threshold REAL NOT NULL DEFAULT 0.85,
            summary       TEXT NOT NULL DEFAULT '{}',
            created_by    TEXT NOT NULL DEFAULT '',
            created_by_name TEXT NOT NULL DEFAULT '',
            started_at    TEXT NOT NULL,
            finished_at   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pipeline_runs_started ON pipeline_runs(started_at);

        -- stage.status: pending | running | passed | failed | skipped | aborted
        CREATE TABLE IF NOT EXISTS pipeline_stages (
            id          TEXT PRIMARY KEY,
            run_id      TEXT NOT NULL,
            seq         INTEGER NOT NULL,
            name        TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending',
            log         TEXT NOT NULL DEFAULT '',
            detail      TEXT NOT NULL DEFAULT '{}',
            started_at  TEXT,
            finished_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pipeline_stages_run ON pipeline_stages(run_id, seq);

        -- 流水线全局配置（单行 id='default'）：EDD 阈值/评测集、各阶段开关与自定义命令
        CREATE TABLE IF NOT EXISTS pipeline_settings (
            id              TEXT PRIMARY KEY,
            edd_threshold   REAL NOT NULL DEFAULT 0.85,
            edd_dataset_id  TEXT NOT NULL DEFAULT '',
            run_unit_test   INTEGER NOT NULL DEFAULT 1,
            run_build_image INTEGER NOT NULL DEFAULT 0,
            build_cmd       TEXT NOT NULL DEFAULT '',
            canary_cmd      TEXT NOT NULL DEFAULT '',
            full_cmd        TEXT NOT NULL DEFAULT '',
            updated_at      TEXT NOT NULL
        );

        -- =====================================================================
        -- 模型网关（LLM Local Gateway）：统一上游模型配置与负载、本地密钥分发、
        -- 请求日志与安全审计。参考 WaLiAPI 设计。
        -- =====================================================================

        -- 上游渠道：protocol(openai|anthropic|ollama) + base_url + api_key + 模型清单/映射；
        -- 按 priority(越大越优先) + weight(同级内按权重随机) 选路，失败自动切换其他渠道。
        -- health: healthy(健康) | degraded(连续失败降级) | down(熔断)
        CREATE TABLE IF NOT EXISTS gateway_channels (
            id            TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            protocol      TEXT NOT NULL DEFAULT 'openai',
            base_url      TEXT NOT NULL DEFAULT '',
            api_key       TEXT NOT NULL DEFAULT '',
            models        TEXT NOT NULL DEFAULT '[]',
            model_mapping TEXT NOT NULL DEFAULT '{}',
            priority      INTEGER NOT NULL DEFAULT 0,
            weight        INTEGER NOT NULL DEFAULT 1,
            enabled       INTEGER NOT NULL DEFAULT 1,
            health        TEXT NOT NULL DEFAULT 'healthy',
            fail_count    INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            last_error    TEXT NOT NULL DEFAULT '',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_gateway_channels_enabled ON gateway_channels(enabled, priority);

        -- 本地访问密钥：为每个下游用户/应用生成独立密钥并设配额，真实上游 Key 不外发。
        -- 仅存 key_hash(sha256) 与 key_prefix(便于识别)，明文只在创建时返回一次。
        CREATE TABLE IF NOT EXISTS gateway_keys (
            id             TEXT PRIMARY KEY,
            name           TEXT NOT NULL,
            key_prefix     TEXT NOT NULL DEFAULT '',
            key_hash       TEXT NOT NULL,
            token_quota    INTEGER NOT NULL DEFAULT 0,
            token_used     INTEGER NOT NULL DEFAULT 0,
            request_quota  INTEGER NOT NULL DEFAULT 0,
            request_count  INTEGER NOT NULL DEFAULT 0,
            allowed_models TEXT NOT NULL DEFAULT '[]',
            enabled        INTEGER NOT NULL DEFAULT 1,
            expires_at     TEXT,
            created_by     TEXT NOT NULL DEFAULT '',
            created_at     TEXT NOT NULL,
            last_used_at   TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_gateway_keys_hash ON gateway_keys(key_hash);

        -- 请求日志与审计：状态码/Token/上游路由/工具调用/请求参数全部入库，
        -- 附带风险明细(risks JSON)、审计动作(audit_action) 与可视化调用链路(trace_steps JSON)。
        -- status: ok | error | blocked；risk_level: none|low|medium|high|critical
        CREATE TABLE IF NOT EXISTS gateway_logs (
            id                TEXT PRIMARY KEY,
            key_id            TEXT NOT NULL DEFAULT '',
            key_name          TEXT NOT NULL DEFAULT '',
            model_requested   TEXT NOT NULL DEFAULT '',
            model_mapped      TEXT NOT NULL DEFAULT '',
            channel_id        TEXT NOT NULL DEFAULT '',
            channel_name      TEXT NOT NULL DEFAULT '',
            upstream_url      TEXT NOT NULL DEFAULT '',
            stream            INTEGER NOT NULL DEFAULT 0,
            status_code       INTEGER NOT NULL DEFAULT 0,
            status            TEXT NOT NULL DEFAULT 'ok',
            prompt_tokens     INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens      INTEGER NOT NULL DEFAULT 0,
            latency_ms        INTEGER NOT NULL DEFAULT 0,
            retries           INTEGER NOT NULL DEFAULT 0,
            tool_calls        TEXT NOT NULL DEFAULT '[]',
            request_body      TEXT NOT NULL DEFAULT '',
            response_preview  TEXT NOT NULL DEFAULT '',
            risk_level        TEXT NOT NULL DEFAULT 'none',
            risk_score        INTEGER NOT NULL DEFAULT 0,
            risks             TEXT NOT NULL DEFAULT '[]',
            audit_action      TEXT NOT NULL DEFAULT '',
            trace_steps       TEXT NOT NULL DEFAULT '[]',
            client_ip         TEXT NOT NULL DEFAULT '',
            error             TEXT NOT NULL DEFAULT '',
            role              TEXT NOT NULL DEFAULT '',
            created_at        TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_gateway_logs_created ON gateway_logs(created_at);
        CREATE INDEX IF NOT EXISTS idx_gateway_logs_key ON gateway_logs(key_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_gateway_logs_risk ON gateway_logs(risk_level, created_at);

        -- 网关全局配置（单行 id='default'）：审计模式/检测开关/内置规则停用/自定义黑白名单/重试
        -- audit_mode: audit(只审计) | warn(警告) | mask(脱敏) | block(阻断)
        CREATE TABLE IF NOT EXISTS gateway_settings (
            id                 TEXT PRIMARY KEY,
            audit_mode         TEXT NOT NULL DEFAULT 'audit',
            unicode_detection  INTEGER NOT NULL DEFAULT 1,
            tool_cmd_detection INTEGER NOT NULL DEFAULT 1,
            outbound_detection INTEGER NOT NULL DEFAULT 1,
            retry_count        INTEGER NOT NULL DEFAULT 2,
            disabled_rules     TEXT NOT NULL DEFAULT '[]',
            blacklist          TEXT NOT NULL DEFAULT '[]',
            whitelist          TEXT NOT NULL DEFAULT '[]',
            log_retention_days INTEGER NOT NULL DEFAULT 30,
            updated_at         TEXT NOT NULL
        );

        -- 模型注册表：网关“最终调用的模型”清单（渠道 models 字段引用本表 key）。
        -- source: standard(手动配置的固定上游模型) | openrouter_free(OpenRouter 免费模型，运行时拉取列表)
        CREATE TABLE IF NOT EXISTS gateway_models (
            id           TEXT PRIMARY KEY,
            key          TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            protocol     TEXT NOT NULL DEFAULT 'openai',
            base_url     TEXT NOT NULL DEFAULT '',
            api_key      TEXT NOT NULL DEFAULT '',
            model_id     TEXT NOT NULL DEFAULT '',
            source       TEXT NOT NULL DEFAULT 'standard',
            enabled      INTEGER NOT NULL DEFAULT 1,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_gateway_models_key ON gateway_models(key);

        -- 业务流→模型路由：为不同业务角色(chat_answer/assistant/intent_rewrite/...)指定渠道+模型。
        -- 未配置的 role 回退到默认渠道 + 注册表默认模型。
        CREATE TABLE IF NOT EXISTS gateway_role_routes (
            id         TEXT PRIMARY KEY,
            role       TEXT NOT NULL,
            channel_id TEXT NOT NULL DEFAULT '',
            model_key  TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_gateway_role_routes_role ON gateway_role_routes(role);

        -- 上游端点（厂商地址 + 密钥）：一处维护、模型下拉复用；网关出网连接配置的唯一归属。
        -- 渠道不再保存连接配置（存量行仅作兼容回退），密钥·配额页管的是下游访问密钥。
        CREATE TABLE IF NOT EXISTS gateway_upstreams (
            id         TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            protocol   TEXT NOT NULL DEFAULT 'openai',
            base_url   TEXT NOT NULL DEFAULT '',
            api_key    TEXT NOT NULL DEFAULT '',
            remark     TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    version = 'production_ab_v1'
    if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
        _migrate(conn)
        from app.core.metric_counters import initialize
        initialize(conn)
        conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)",
                     (version, datetime.now().isoformat()))
    if not conn.execute("SELECT 1 FROM schema_migrations WHERE version='production_ab_v2'").fetchone():
        columns = {r[1] for r in conn.execute("PRAGMA table_info(chat_messages)")}
        if "interrupted" not in columns:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN interrupted INTEGER NOT NULL DEFAULT 0")
        conn.execute("INSERT INTO schema_migrations VALUES ('production_ab_v2',?)", (datetime.now().isoformat(),))
    if not conn.execute("SELECT 1 FROM schema_migrations WHERE version='url_import_v1'").fetchone():
        columns = {r[1] for r in conn.execute("PRAGMA table_info(upload_records)")}
        if "source_metadata" not in columns:
            conn.execute("ALTER TABLE upload_records ADD COLUMN source_metadata TEXT NOT NULL DEFAULT '{}'")
        if "url_import_id" not in columns:
            conn.execute("ALTER TABLE upload_records ADD COLUMN url_import_id TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_record_url_import ON upload_records(url_import_id)")
        conn.execute("INSERT INTO schema_migrations VALUES ('url_import_v1',?)", (datetime.now().isoformat(),))
    if not conn.execute("SELECT 1 FROM schema_migrations WHERE version='gateway_routing_v1'").fetchone():
        columns = {r[1] for r in conn.execute("PRAGMA table_info(gateway_channels)")}
        # lb_strategy: round_robin|weighted|random|priority（默认 weighted，保留存量 A-Res 行为）
        if "lb_strategy" not in columns:
            conn.execute("ALTER TABLE gateway_channels ADD COLUMN lb_strategy TEXT NOT NULL DEFAULT 'weighted'")
        # is_default: 全局唯一默认渠道（用户未指定渠道时绑定）
        if "is_default" not in columns:
            conn.execute("ALTER TABLE gateway_channels ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0")
        # allowed_users: JSON 用户 id 列表（空=全员可用）
        if "allowed_users" not in columns:
            conn.execute("ALTER TABLE gateway_channels ADD COLUMN allowed_users TEXT NOT NULL DEFAULT '[]'")
        conn.execute("INSERT INTO schema_migrations VALUES ('gateway_routing_v1',?)", (datetime.now().isoformat(),))
    if not conn.execute("SELECT 1 FROM schema_migrations WHERE version='gateway_upstreams_v1'").fetchone():
        conn.execute(
            "CREATE TABLE IF NOT EXISTS gateway_upstreams ("
            "id TEXT PRIMARY KEY, name TEXT NOT NULL, protocol TEXT NOT NULL DEFAULT 'openai',"
            "base_url TEXT NOT NULL DEFAULT '', api_key TEXT NOT NULL DEFAULT '',"
            "remark TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        mcols = {r[1] for r in conn.execute("PRAGMA table_info(gateway_models)")}
        if "upstream_id" not in mcols:
            conn.execute("ALTER TABLE gateway_models ADD COLUMN upstream_id TEXT NOT NULL DEFAULT ''")
        # 存量回填：模型行旧内联连接配置按 (protocol+base_url+api_key) 归并为上游端点，行内联配置清零
        import uuid as _uuid
        seen: dict = {}
        legacy = conn.execute(
            "SELECT id, protocol, base_url, api_key FROM gateway_models WHERE base_url <> ''").fetchall()
        for r in legacy:
            sig = ((r[1] or "openai"), (r[2] or "").rstrip("/"), r[3] or "")
            if sig not in seen:
                uid = str(_uuid.uuid4())
                host = sig[1].split("//")[-1].split("/")[0] or "upstream"
                conn.execute(
                    "INSERT INTO gateway_upstreams (id, name, protocol, base_url, api_key, remark,"
                    " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (uid, host, sig[0], sig[1], sig[2], "存量模型配置自动归并",
                     datetime.now().isoformat(), datetime.now().isoformat()),
                )
                seen[sig] = uid
            conn.execute(
                "UPDATE gateway_models SET upstream_id=?, protocol='openai', base_url='', api_key=''"
                " WHERE id=?",
                (seen[sig], r[0]),
            )
        conn.execute("INSERT INTO schema_migrations VALUES ('gateway_upstreams_v1',?)",
                     (datetime.now().isoformat(),))
    if not conn.execute("SELECT 1 FROM schema_migrations WHERE version='gateway_upstreams_v2'").fetchone():
        # 归并遗留内联 Key：未绑端点但自带内联 Key 的 openrouter_free 模型归并为上游端点（官方地址），
        # 连接配置完全收敛到上游端点，模型界面仅下拉绑定（否则遗留 Key 可见但无处修改）
        import uuid as _uuid2
        seen2: dict = {}
        legacy2 = conn.execute(
            "SELECT id, api_key FROM gateway_models WHERE upstream_id='' AND api_key <> ''"
            " AND source='openrouter_free'").fetchall()
        for r in legacy2:
            key = r[1] or ""
            if key not in seen2:
                uid = str(_uuid2.uuid4())
                conn.execute(
                    "INSERT INTO gateway_upstreams (id, name, protocol, base_url, api_key, remark,"
                    " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (uid, f"openrouter.ai（legacy 密钥 {key[-4:]}）", "openai",
                     "https://openrouter.ai/api/v1", key, "遗留模型内联 Key 自动归并",
                     datetime.now().isoformat(), datetime.now().isoformat()),
                )
                seen2[key] = uid
            conn.execute("UPDATE gateway_models SET upstream_id=?, api_key='' WHERE id=?",
                         (seen2[key], r[0]))
        conn.execute("INSERT INTO schema_migrations VALUES ('gateway_upstreams_v2',?)",
                     (datetime.now().isoformat(),))
    if not conn.execute("SELECT 1 FROM schema_migrations WHERE version='gateway_rules_v1'").fetchone():
        # ---- 上游管理（璇玑式）：上游自身持有模型清单/成本层级/权重/健康度/延迟 ----
        import json as _json
        import uuid as _uuid3
        now = datetime.now().isoformat()
        ucols = {r[1] for r in conn.execute("PRAGMA table_info(gateway_upstreams)")}
        for col, ddl in (
                ("tier", "TEXT NOT NULL DEFAULT 'paid'"),
                ("models", "TEXT NOT NULL DEFAULT '[]'"),
                ("weight", "INTEGER NOT NULL DEFAULT 100"),
                ("enabled", "INTEGER NOT NULL DEFAULT 1"),
                ("health", "TEXT NOT NULL DEFAULT 'healthy'"),
                ("fail_count", "INTEGER NOT NULL DEFAULT 0"),
                ("success_count", "INTEGER NOT NULL DEFAULT 0"),
                ("last_error", "TEXT NOT NULL DEFAULT ''"),
                ("latency_ms", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in ucols:
                conn.execute(f"ALTER TABLE gateway_upstreams ADD COLUMN {col} {ddl}")
        # 存量回填：注册表模型并入其上游（模型清单/成本层级），注册表随后退役
        for u in conn.execute("SELECT id FROM gateway_upstreams").fetchall():
            rows = conn.execute(
                "SELECT model_id, key, source FROM gateway_models WHERE upstream_id=?",
                (u[0],)).fetchall()
            if rows:
                names = [r[0] or r[1] for r in rows]
                tier = "free" if any(r[2] == "openrouter_free" for r in rows) else "paid"
                conn.execute("UPDATE gateway_upstreams SET models=?, tier=? WHERE id=?",
                             (_json.dumps(names, ensure_ascii=False), tier, u[0]))
        orphans = conn.execute(
            "SELECT model_id, key FROM gateway_models WHERE upstream_id=''").fetchall()
        if orphans:
            conn.execute(
                "INSERT INTO gateway_upstreams (id, name, protocol, base_url, api_key, remark,"
                " tier, models, weight, enabled, health, fail_count, success_count, last_error,"
                " latency_ms, created_at, updated_at)"
                " VALUES (?,?,?,?,?,'注册表未绑模型自动归并','free',?,100,1,'healthy',0,0,'',0,?,?)",
                (str(_uuid3.uuid4()), "openrouter-free（系统回退）", "openai", "", "",
                 _json.dumps([r[0] or r[1] for r in orphans], ensure_ascii=False), now, now))
        # ---- 路由规则：模型模式（支持通配符）→ 上游列表 + 负载均衡策略 ----
        conn.execute(
            "CREATE TABLE IF NOT EXISTS gateway_rules ("
            "id TEXT PRIMARY KEY, model_pattern TEXT NOT NULL,"
            " upstreams TEXT NOT NULL DEFAULT '[]', lb_strategy TEXT NOT NULL DEFAULT 'weighted',"
            " priority INTEGER NOT NULL DEFAULT 0, is_default INTEGER NOT NULL DEFAULT 0,"
            " allowed_users TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1,"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        all_ups = [r[0] for r in conn.execute("SELECT id FROM gateway_upstreams").fetchall()]
        up_by_model = {}
        for r in conn.execute("SELECT key, model_id, upstream_id FROM gateway_models").fetchall():
            up_by_model[r[0]] = r[2]
            if r[1]:
                up_by_model[r[1]] = r[2]
        ch_map = {}
        for ch in conn.execute(
                "SELECT id, models, model_mapping, lb_strategy, priority, is_default,"
                " allowed_users, enabled FROM gateway_channels").fetchall():
            patterns = [str(m) for m in (_json.loads(ch[1] or "[]") or [])]
            patterns += [str(k) for k in (_json.loads(ch[2] or "{}") or {})]
            patterns = list(dict.fromkeys(patterns))
            if not patterns:
                patterns = ["*"]
            first = ""
            for p in patterns:
                ups = [up_by_model[p]] if (p != "*" and up_by_model.get(p)) else list(all_ups)
                if not ups:
                    continue
                rid = str(_uuid3.uuid4())
                conn.execute(
                    "INSERT INTO gateway_rules (id, model_pattern, upstreams, lb_strategy,"
                    " priority, is_default, allowed_users, enabled, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (rid, p, _json.dumps(ups, ensure_ascii=False), ch[3] or "weighted",
                     int(ch[4] or 0), int(ch[5] or 0), ch[6] or "[]", int(ch[7] or 0), now, now))
                first = first or rid
            if ch[0]:
                ch_map[ch[0]] = first
        if not conn.execute("SELECT COUNT(*) FROM gateway_rules").fetchone()[0] and all_ups:
            # 无存量渠道时种子默认兜底规则，保证迁移后网关即可用
            conn.execute(
                "INSERT INTO gateway_rules (id, model_pattern, upstreams, lb_strategy, priority,"
                " is_default, allowed_users, enabled, created_at, updated_at)"
                " VALUES (?,'*',?,'weighted',0,1,'[]',1,?,?)",
                (str(_uuid3.uuid4()), _json.dumps(all_ups, ensure_ascii=False), now, now))
        # ---- 业务路由：channel_id→rule_id、model_key→model_name（值同步映射） ----
        rcols = {r[1] for r in conn.execute("PRAGMA table_info(gateway_role_routes)")}
        if "channel_id" in rcols:
            conn.execute("ALTER TABLE gateway_role_routes RENAME COLUMN channel_id TO rule_id")
        if "model_key" in rcols:
            conn.execute("ALTER TABLE gateway_role_routes RENAME COLUMN model_key TO model_name")
        for rt in conn.execute("SELECT role, rule_id, model_name FROM gateway_role_routes").fetchall():
            mrow = conn.execute("SELECT model_id FROM gateway_models WHERE key=?", (rt[2],)).fetchone() \
                if rt[2] else None
            conn.execute("UPDATE gateway_role_routes SET rule_id=?, model_name=? WHERE role=?",
                         (ch_map.get(rt[1], ""), (mrow[0] if mrow else rt[2]), rt[0]))
        conn.execute("INSERT INTO schema_migrations VALUES ('gateway_rules_v1',?)", (now,))
    if not conn.execute("SELECT 1 FROM schema_migrations WHERE version='gateway_settings_v2'").fetchone():
        # 网关默认模型（业务路由未指定模型名时的兜底模型）+ 系统内部出网密钥明文
        # （系统各模块经网关 /v1 出网所用，本地信任边界内保存，与上游 api_key 同级）
        scols = {r[1] for r in conn.execute("PRAGMA table_info(gateway_settings)")}
        if "default_model" not in scols:
            conn.execute("ALTER TABLE gateway_settings ADD COLUMN default_model TEXT NOT NULL DEFAULT ''")
        if "internal_key" not in scols:
            conn.execute("ALTER TABLE gateway_settings ADD COLUMN internal_key TEXT NOT NULL DEFAULT ''")
        conn.execute("INSERT INTO schema_migrations VALUES ('gateway_settings_v2',?)",
                     (datetime.now().isoformat(),))
    if not conn.execute("SELECT 1 FROM schema_migrations WHERE version='gateway_logs_v2'").fetchone():
        # 业务场景（role）：审计页展示链路发起方（记忆抽取/意图改写/聊天回答…）
        lcols = {r[1] for r in conn.execute("PRAGMA table_info(gateway_logs)")}
        if "role" not in lcols:
            conn.execute("ALTER TABLE gateway_logs ADD COLUMN role TEXT NOT NULL DEFAULT ''")
        conn.execute("INSERT INTO schema_migrations VALUES ('gateway_logs_v2',?)",
                     (datetime.now().isoformat(),))
    if not conn.execute("SELECT 1 FROM schema_migrations WHERE version='gateway_upstream_category_v1'").fetchone():
        # 上游分类：llm(默认) | ocr | embedding | rerank。多模态上游（OCR/Embedding/ReRank）
        # 收纳进网关后，业务路由按 role→category 过滤候选上游，防 LLM 规则误选 embedding 上游。
        uccols = {r[1] for r in conn.execute("PRAGMA table_info(gateway_upstreams)")}
        if "category" not in uccols:
            conn.execute("ALTER TABLE gateway_upstreams ADD COLUMN category TEXT NOT NULL DEFAULT 'llm'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gateway_upstreams_category"
                     " ON gateway_upstreams(category)")
        conn.execute("INSERT INTO schema_migrations VALUES ('gateway_upstream_category_v1',?)",
                     (datetime.now().isoformat(),))
    if not conn.execute("SELECT 1 FROM schema_migrations WHERE version='ingest_dedup_v1'").fetchone():
        # 重复入库校验：来源指纹表（文件=内容 sha256，链接=规范化 URL sha256）。
        # UNIQUE(fingerprint, kb_id)：同一知识库内相同来源只入库一次，入口即拦截；
        # 跨知识库仍可分别入库（同一份公共资料归入不同主题库是合法场景）。
        # record_id 关联 upload_records，来源删除时据此注销，保证删除后可重新入库。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ingest_fingerprints (
                id          TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                kind        TEXT NOT NULL DEFAULT 'file',
                kb_id       TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT '',
                title       TEXT NOT NULL DEFAULT '',
                owner_id    TEXT NOT NULL DEFAULT '',
                record_id   TEXT NOT NULL DEFAULT '',
                source_ref  TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL
            );
            """
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_ingest_fp_kb"
                     " ON ingest_fingerprints(fingerprint, kb_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ingest_fp_record"
                     " ON ingest_fingerprints(record_id)")
        conn.execute("INSERT INTO schema_migrations VALUES ('ingest_dedup_v1',?)",
                     (datetime.now().isoformat(),))
    if not conn.execute("SELECT 1 FROM schema_migrations WHERE version='wiki_topic_digest_v1'").fetchone():
        # 对话/检索历史沉淀（llm-wiki 路径A）：记录已从高频主题综合出的私人 digest 词条，
        # UNIQUE(user_id, topic_hash) 防止同一用户的同一主题被重复编译；hits 记录沉淀时命中次数，
        # page_id 关联 wiki_pages，词条删除后可据 topic_hash 清理或允许重新沉淀。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wiki_topic_digest_log (
                id         TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL,
                topic      TEXT NOT NULL,
                topic_hash TEXT NOT NULL,
                page_id    TEXT NOT NULL DEFAULT '',
                hits       INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_topic_digest_user_hash"
                     " ON wiki_topic_digest_log(user_id, topic_hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_topic_digest_page"
                     " ON wiki_topic_digest_log(page_id)")
        conn.execute("INSERT INTO schema_migrations VALUES ('wiki_topic_digest_v1',?)",
                     (datetime.now().isoformat(),))
    if not conn.execute("SELECT 1 FROM schema_migrations WHERE version='semantic_cache_v1'").fetchone():
        # 主问答语义缓存：按用户存储历史问答的问题向量，相似问题命中时直接回复缓存内容、
        # 绕过检索/生成。embedding 以 JSON 数组存（per-user 条目量小，内存余弦匹配即可）；
        # UNIQUE(user_id, question_norm) 避免同一用户重复归一化问题重复入库。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS semantic_cache (
                id             TEXT PRIMARY KEY,
                user_id        TEXT NOT NULL,
                question       TEXT NOT NULL,
                question_norm  TEXT NOT NULL,
                answer         TEXT NOT NULL,
                sources        TEXT NOT NULL DEFAULT '[]',
                embedding      TEXT NOT NULL DEFAULT '[]',
                hit_count      INTEGER NOT NULL DEFAULT 0,
                created_at     TEXT NOT NULL,
                last_used_at   TEXT NOT NULL
            );
            """
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_cache_user_norm"
                     " ON semantic_cache(user_id, question_norm)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_semantic_cache_user_used"
                     " ON semantic_cache(user_id, last_used_at)")
        conn.execute("INSERT INTO schema_migrations VALUES ('semantic_cache_v1',?)",
                     (datetime.now().isoformat(),))
    if not conn.execute("SELECT 1 FROM schema_migrations WHERE version='prompt_skill_eval_v1'").fetchone():
        # Prompt/Skill 评测与上线：
        # - prompt_versions：系统提示词版本库，status=published 的版本被 chat 主流程实际采用（「上线」落地）；
        #   同一时刻至多一个 published，发布新版本时旧 published 归档为 archived。
        # - prompt_skill_evals：一次评测 = 一行，target_type=prompt|skill；沉淀静态断言(assertions)、
        #   Rubric 多维打分(rubric)、可选 A/B 行为对比(ab)、LLM 优化建议(suggestion)与上线状态。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prompt_versions (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL DEFAULT '',
                content     TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'draft',
                note        TEXT NOT NULL DEFAULT '',
                created_by  TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_prompt_versions_status"
                     " ON prompt_versions(status, updated_at)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prompt_skill_evals (
                id            TEXT PRIMARY KEY,
                target_type   TEXT NOT NULL,
                target_id     TEXT NOT NULL DEFAULT '',
                target_name   TEXT NOT NULL DEFAULT '',
                owner_id      TEXT NOT NULL DEFAULT '',
                status        TEXT NOT NULL DEFAULT 'running',
                baseline_text TEXT NOT NULL DEFAULT '',
                assertions    TEXT NOT NULL DEFAULT '[]',
                rubric        TEXT NOT NULL DEFAULT '{}',
                overall_score REAL,
                suggestion    TEXT NOT NULL DEFAULT '{}',
                ab            TEXT NOT NULL DEFAULT '{}',
                published     INTEGER NOT NULL DEFAULT 0,
                error         TEXT NOT NULL DEFAULT '',
                created_by    TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL,
                finished_at   TEXT
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_prompt_skill_evals_target"
                     " ON prompt_skill_evals(target_type, target_id, created_at)")
        conn.execute("INSERT INTO schema_migrations VALUES ('prompt_skill_eval_v1',?)",
                     (datetime.now().isoformat(),))
    conn.commit()


def _migrate(conn: sqlite3.Connection):
    """对存量数据库补加新列（幂等）"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(usage_daily)")}
    for col in ("knowledge_count", "web_search_count", "upload_count"):
        if col not in cols:
            conn.execute(
                f"ALTER TABLE usage_daily ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0"
            )
            logger.info("usage_daily 补加列: %s", col)

    # 聊天消息附件元数据（[{file_id, filename, kind}] JSON），用于历史回显与新页预览
    msg_cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_messages)")}
    if "attachments" not in msg_cols:
        conn.execute(
            "ALTER TABLE chat_messages ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'"
        )
        logger.info("chat_messages 补加列: attachments")

    # 上传记录自定义标题（用于知识库管理搜索与展示）
    upload_cols = {r[1] for r in conn.execute("PRAGMA table_info(upload_records)")}
    if "title" not in upload_cols:
        conn.execute(
            "ALTER TABLE upload_records ADD COLUMN title TEXT NOT NULL DEFAULT ''"
        )
        logger.info("upload_records 补加列: title")

    # CI/CD 流水线：运行记录补加发起人用户名（历史列表显性展示，存量数据按 created_by 回查）
    run_cols = {r[1] for r in conn.execute("PRAGMA table_info(pipeline_runs)")}
    if "created_by_name" not in run_cols:
        conn.execute(
            "ALTER TABLE pipeline_runs ADD COLUMN created_by_name TEXT NOT NULL DEFAULT ''"
        )
        logger.info("pipeline_runs 补加列: created_by_name")

    # llm-wiki：wiki_pages 补加 source_hash（增量编译）与 analysis（两步思维链分析结果）
    wiki_cols = {r[1] for r in conn.execute("PRAGMA table_info(wiki_pages)")}
    if "source_hash" not in wiki_cols:
        conn.execute(
            "ALTER TABLE wiki_pages ADD COLUMN source_hash TEXT NOT NULL DEFAULT ''"
        )
        logger.info("wiki_pages 补加列: source_hash")
    if "analysis" not in wiki_cols:
        conn.execute(
            "ALTER TABLE wiki_pages ADD COLUMN analysis TEXT NOT NULL DEFAULT '{}'"
        )
        logger.info("wiki_pages 补加列: analysis")

    # 回归评测 Ragas 化：eval_results 补加四项指标列与判定明细列（幂等）
    eval_cols = {r[1] for r in conn.execute("PRAGMA table_info(eval_results)")}
    for col, ddl in (
        ("faithfulness", "REAL"),
        ("answer_relevance", "REAL"),
        ("context_precision", "REAL"),
        ("context_recall", "REAL"),
        ("metric_details", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        if col not in eval_cols:
            conn.execute(f"ALTER TABLE eval_results ADD COLUMN {col} {ddl}")
            logger.info("eval_results 补加列: %s", col)

    # 运行状态恢复只允许持有独占锁的 Agent 在启动时执行。
    # CI/CD 流水线：仅初始化默认配置行。
    try:
        row = conn.execute("SELECT id FROM pipeline_settings WHERE id = 'default'").fetchone()
        if not row:
            conn.execute(
                "INSERT OR IGNORE INTO pipeline_settings (id, edd_threshold, edd_dataset_id, run_unit_test,"
                " run_build_image, build_cmd, canary_cmd, full_cmd, updated_at)"
                " VALUES ('default', 0.85, '', 1, 0, '', '', '', ?)",
                (datetime.now().isoformat(),),
            )
            logger.info("pipeline_settings 初始化默认配置行")
    except sqlite3.OperationalError:
        pass

    # 模型网关：确保默认配置行存在（审计模式 audit、重试 2 次、日志保留 30 天）
    try:
        row = conn.execute("SELECT id FROM gateway_settings WHERE id = 'default'").fetchone()
        if not row:
            conn.execute(
                "INSERT OR IGNORE INTO gateway_settings (id, audit_mode, unicode_detection, tool_cmd_detection,"
                " outbound_detection, retry_count, disabled_rules, blacklist, whitelist,"
                " log_retention_days, updated_at)"
                " VALUES ('default', 'audit', 1, 1, 1, 2, '[]', '[]', '[]', 30, ?)",
                (datetime.now().isoformat(),),
            )
            logger.info("gateway_settings 初始化默认配置行")
    except sqlite3.OperationalError:
        pass


@contextmanager
def transaction():
    """跨进程串行写事务；事务内仅使用返回连接，避免再次取得全局锁。"""
    conn = get_conn()
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def query(sql: str, params: tuple = ()) -> List[dict]:
    """执行查询，返回 dict 列表"""
    conn = get_conn()
    with _lock:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def query_one(sql: str, params: tuple = ()) -> Optional[dict]:
    """执行查询，返回单行 dict 或 None"""
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> int:
    """执行写操作并提交，返回受影响行数"""
    conn = get_conn()
    with _lock:
        cur = conn.execute(sql, params)
        conn.commit()
    return cur.rowcount


def executemany(sql: str, seq_params: list) -> None:
    """批量执行写操作并提交"""
    conn = get_conn()
    with _lock:
        conn.executemany(sql, seq_params)
        conn.commit()
