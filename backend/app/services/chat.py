"""
见字如面 - 问答聊天服务
核心逻辑:
  1. 管理多个会话 (session) 及历史消息
  2. 处理用户提问:
     a. 可选: 知识库检索 → 注入上下文
     b. 可选: 联网搜索 → 注入上下文
     c. LLM 生成回答 (流式)
  3. 回答中携带来源信息: 原文 / 图片 / 网络链接

会话数据持久化到 SQLite (backend/data/app.db)
"""
import asyncio
import anyio
import json
import logging
import time
import uuid
from datetime import datetime
from contextlib import aclosing
from typing import AsyncGenerator, List, Optional

from app.config import settings
from app.core import db
from app.core.request_budget import iterate, close_resources
from app.core import observability as obs
from app.core.evidence_checker import check_evidence_sufficiency, verify_citations
from app.core.llm import FINAL_ANSWER_RULES, LLMFactory
from app.core.intent_rewriter import rewrite_query
from app.core.vector_store import vector_store
from app.services.config_manager import runtime_config
from app.services.knowledge import knowledge_service
from app.services.memory import memory_service
from app.services.skill import skill_service
from app.services.records import upload_records
from app.services import session_summary
from app.services import tool_orchestrator
from app.services.web_search import web_search
from app.services import chat_agent
from app.services import semantic_cache
from app.core.query_complexity import classify_complexity
from app.core.intent_clarifier import decide_clarification

logger = logging.getLogger("jianziruyang.chat")

# HITL 卡片展示/复用的问题文本上限（纯截图提问时识别内容可能数百字，不宜整段塞进卡片）
_CARD_QUERY_CHARS = 60


def _card_query(raw_query: str, effective_query: str) -> str:
    """HITL 卡片用的问题文本。

    用户有输入文本时原样使用；纯截图提问（文本为空、以 OCR 摘录作为问题）时截断，
    避免卡片被整段识别文字占满，同时保证前端「联网搜索」按钮复用的是可读的短查询。
    """
    if raw_query.strip() or len(effective_query) <= _CARD_QUERY_CHARS:
        return effective_query
    return effective_query[:_CARD_QUERY_CHARS].rstrip() + "…"


class Message:
    """单条消息"""

    def __init__(
        self,
        role: str,
        content: str,
        sources: Optional[List[dict]] = None,
        search_results: Optional[List[dict]] = None,
        use_knowledge: bool = False,
        use_search: bool = False,
        message_id: str = None,
        attachments: Optional[List[dict]] = None,
        interrupted: bool = False,
    ):
        self.message_id = message_id or str(uuid.uuid4())
        self.role = role  # "user" | "assistant"
        self.content = content
        self.interrupted = interrupted
        self.sources = sources or []
        self.search_results = search_results or []
        self.use_knowledge = use_knowledge
        self.use_search = use_search
        self.attachments = attachments or []  # 用户消息附件元数据 [{file_id, filename, kind}]

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "role": self.role,
            "content": self.content,
            "sources": self.sources,
            "search_results": self.search_results,
            "use_knowledge": self.use_knowledge,
            "use_search": self.use_search,
            "attachments": self.attachments,
            "interrupted": self.interrupted,
        }


class ChatSession:
    """单个聊天会话"""

    def __init__(self, session_id: str = None, user_id: str = ""):
        self.session_id = session_id or str(uuid.uuid4())
        self.user_id = user_id  # 所属用户，空字符串表示旧数据兼容
        self.title = "新对话"
        self.messages: List[Message] = []
        self.created_at = datetime.now().isoformat()
        self.updated_at = self.created_at

    def add_message(self, message: Message):
        self.messages.append(message)
        self.updated_at = datetime.now().isoformat()

        # 根据第一条用户消息更新标题
        if len(self.messages) == 1 and message.role == "user":
            text = (message.content or "").strip()
            # 纯截图/附件提问（未输入文本）时用附件名，避免会话标题为空
            if not text and message.attachments:
                text = message.attachments[0].get("filename") or "图片提问"
            self.title = (text[:30] + "..." if len(text) > 30 else text) or self.title

    def get_history(self, max_count: int = None) -> List[dict]:
        """获取历史消息 (用于 LLM 上下文)"""
        max_count = max_count or settings.SESSION_MAX_HISTORY
        recent = self.messages[-max_count * 2:]  # 保留最近 N 轮
        return [m.to_dict() for m in recent]

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": len(self.messages),
        }


class ChatService:
    """聊天服务（会话/消息持久化到 SQLite，内存缓存加速读取）"""

    def __init__(self):
        # 仅缓存活跃会话；不再启动时全量加载历史消息，避免数据增长后占用内存
        self.sessions: dict[str, ChatSession] = {}
        self._active_tasks = {}

    def cancel_generation(self, session_id):
        for task in tuple(self._active_tasks.get(session_id, ())):
            task.cancel()

    # ---- SQLite 持久化 ----

    def _load_session(self, session_id: str) -> Optional[ChatSession]:
        """按需从 SQLite 加载单个会话及其消息"""
        try:
            s_row = db.query_one("SELECT * FROM chat_sessions WHERE session_id = ?", (session_id,))
            if not s_row:
                return None
            session = ChatSession(session_id=s_row["session_id"], user_id=s_row["user_id"])
            session.title = s_row["title"]
            session.created_at = s_row["created_at"]
            session.updated_at = s_row["updated_at"]
            for m_row in db.query(
                "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ):
                session.messages.append(Message(
                    role=m_row["role"],
                    content=m_row["content"],
                    # 历史来源回填预览 PDF 路径等字段，保证旧会话也显示原文跳转链接
                    sources=upload_records.enrich_sources(json.loads(m_row["sources"] or "[]")),
                    search_results=json.loads(m_row["search_results"] or "[]"),
                    use_knowledge=bool(m_row["use_knowledge"]),
                    use_search=bool(m_row["use_search"]),
                    message_id=m_row["message_id"],
                    attachments=json.loads(m_row["attachments"] or "[]"),
                    interrupted=bool(m_row.get("interrupted", 0)),
                ))
            self.sessions[session_id] = session
            return session
        except Exception as e:
            logger.error("按需加载会话失败: %s", e)
            return None

    def _persist_session(self, session: ChatSession):
        """将单个会话完整写入 SQLite（会话行 upsert + 消息全量替换）"""
        try:
            with db.transaction() as conn:
                self._persist_session_transaction(conn, session)
        except Exception as e:
            logger.error("保存会话失败: %s", e)
            raise

    def _persist_session_transaction(self, conn, session):
            conn.execute(
                "INSERT INTO chat_sessions (session_id, user_id, title, created_at, updated_at)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                " user_id=excluded.user_id, title=excluded.title, updated_at=excluded.updated_at",
                (session.session_id, session.user_id, session.title,
                 session.created_at, session.updated_at),
            )
            conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session.session_id,))
            conn.executemany(
                "INSERT INTO chat_messages (message_id, session_id, seq, role, content,"
                " sources, search_results, use_knowledge, use_search, attachments, created_at, interrupted)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        m.message_id, session.session_id, seq, m.role, m.content,
                        json.dumps(m.sources, ensure_ascii=False),
                        json.dumps(m.search_results, ensure_ascii=False),
                        1 if m.use_knowledge else 0,
                        1 if m.use_search else 0,
                        json.dumps(m.attachments, ensure_ascii=False),
                        session.created_at, int(m.interrupted),
                    )
                    for seq, m in enumerate(session.messages)
                ],
            )

    def _save_sessions(self):
        """兼容旧调用：仅持久化最近变更的会话由调用方显式指定，此方法保留为空操作"""
        pass

    # ---- 会话管理 ----

    def create_session(self, user_id: str = "", session_id: str = None) -> ChatSession:
        """创建新会话（可指定 session_id，避免二次注册产生孤儿条目）"""
        session = ChatSession(session_id=session_id, user_id=user_id)
        self.sessions[session.session_id] = session
        self._persist_session(session)
        return session

    def get_session(self, session_id: str) -> Optional[ChatSession]:
        if not session_id:
            return None
        return self.sessions.get(session_id) or self._load_session(session_id)

    def get_user_session(self, session_id: str, user_id: str) -> Optional[ChatSession]:
        """获取会话并校验归属（空 user_id 的旧数据兼容）"""
        session = self.get_session(session_id)
        if not session:
            return None
        if session.user_id and session.user_id != user_id:
            return None
        return session

    def delete_session(self, session_id: str, user_id: str = ""):
        session = self.get_session(session_id)
        if session:
            # 仅允许删除自己的会话（空 user_id 的旧数据允许任何人删除）
            if session.user_id and session.user_id != user_id:
                return False
            self.cancel_generation(session_id)
            from app.services.chat_actions import invalidate_session
            invalidate_session(session_id)
            self.sessions.pop(session_id, None)
            db.execute("DELETE FROM chat_sessions WHERE session_id = ?", (session_id,))
            db.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
            db.execute("DELETE FROM message_feedbacks WHERE session_id = ?", (session_id,))
            db.execute("DELETE FROM chat_session_summaries WHERE session_id = ?", (session_id,))
            db.execute("DELETE FROM tool_calls WHERE session_id = ?", (session_id,))
            return True
        return False

    def list_sessions(self, user_id: str = "") -> List[dict]:
        """列出指定用户的会话（直接从 SQLite 查询，不遍历内存缓存）"""
        rows = db.query(
            """
            SELECT s.session_id, s.user_id, s.title, s.created_at, s.updated_at,
                   COUNT(m.message_id) AS message_count
            FROM chat_sessions s
            LEFT JOIN chat_messages m ON m.session_id = s.session_id
            WHERE s.user_id = '' OR s.user_id = ?
            GROUP BY s.session_id
            ORDER BY s.updated_at DESC
            LIMIT 200
            """,
            (user_id,),
        )
        return [dict(r) for r in rows]

    async def chat_stream(
        self,
        session_id: str,
        query: str,
        use_knowledge: bool = True,
        use_search: bool = False,
        llm_provider: str = None,
        user_id: str = "",
        attachments: Optional[List[dict]] = None,
        kb_id: str = "",
        rule_id: str = "",
        model: str = "",
        skip_search_prompt: bool = False,
    ) -> AsyncGenerator[str, None]:
        """流式聊天（外层负责可观测性追踪：一次提问 = 一条 trace）

        Args:
            session_id: 会话 ID
            query: 用户问题
            use_knowledge: 是否启用知识库检索
            use_search: 是否启用联网搜索
            llm_provider: LLM 提供商
            user_id: 用户 ID
            attachments: 聊天框附件列表 [{path, filename, kind}, ...]，由工具调用框架处理
            kb_id: 附件入库时的目标知识库 ID（可为空，走默认库）

        Yields:
            SSE 格式的事件流
        """
        obs.start_trace("chat", user_id, query)
        deadline = time.monotonic() + max(0.01, settings.CHAT_REQUEST_TIMEOUT)
        tasks, partial = set(), []
        current_task = asyncio.current_task()
        self._active_tasks.setdefault(session_id, set()).add(current_task)
        user_message_id = None
        status = "error"
        iterator = iterate(self._chat_stream_inner(
            session_id, query, use_knowledge, use_search,
            llm_provider, user_id, attachments, kb_id, tasks, rule_id,
            skip_search_prompt, model,
        ), deadline)
        try:
            async for event in iterator:
                data = json.loads(event.strip()[5:].strip())
                if user_message_id is None:
                    session = self.sessions.get(session_id)
                    user_message_id = next((m.message_id for m in reversed(session.messages) if m.role == "user"), None) if session else None
                if data.get("event") == "token":
                    partial.append(data.get("content", ""))
                elif data.get("event") == "done":
                    status = "ok"
                yield event
                if data.get("event") == "error":
                    return
            if status != "ok":
                obs.mark_failed("响应没有正常完成")
        except TimeoutError:
            status = "timeout"
            yield f"data: {self._json_event('error', {'message': '请求总预算耗尽，生成已中断', 'code': 'request_timeout'})}\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            if status != "ok":
                status = "cancelled"
            raise
        except Exception as e:
            obs.mark_failed(str(e))
            yield f"data: {self._json_event('error', {'message': str(e)})}\n\n"
        finally:
            try:
                await close_resources(iterator)
                for task in tasks:
                    task.cancel()
                with anyio.CancelScope(shield=True):
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                if status != "ok" and partial:
                    session = self.sessions.get(session_id)
                    if session and db.query_one("SELECT message_id FROM chat_messages WHERE message_id=? AND session_id=?", (user_message_id, session_id)):
                        session.add_message(Message(role="assistant", content="".join(partial) + "\n\n[生成已中断，内容不完整]", interrupted=True))
                        self._persist_session(session)
            finally:
                active = self._active_tasks.get(session_id, set())
                active.discard(current_task)
                if not active:
                    self._active_tasks.pop(session_id, None)
                obs.end_trace(status=status)

    async def _chat_stream_inner(
        self,
        session_id: str,
        query: str,
        use_knowledge: bool,
        use_search: bool,
        llm_provider: str,
        user_id: str,
        attachments: Optional[List[dict]],
        kb_id: str,
        tasks: set,
        rule_id: str = "",
        skip_search_prompt: bool = False,
        model: str = "",
    ) -> AsyncGenerator[str, None]:
        """流式聊天内层主流程（检索/搜索/工具/生成各环节记录 trace span）"""
        logger.info("开始处理聊天: session=%s, query=%.50s, knowledge=%s, search=%s",
                     session_id, query, use_knowledge, use_search)

        # 1. 获取或创建会话（已存在时校验归属）
        session = self.get_session(session_id)
        if session and session.user_id and session.user_id != user_id:
            obs.mark_failed("无权访问该会话")
            yield f"data: {self._json_event('error', {'message': '无权访问该会话'})}\n\n"
            return
        if not session:
            session = self.create_session(user_id=user_id, session_id=session_id)

        # 2. 保存用户消息（附件元数据随消息持久化，供历史回显与新页预览）
        att_meta = [
            {"file_id": a.get("file_id", ""), "filename": a.get("filename", ""), "kind": a.get("kind", "")}
            for a in (attachments or [])
        ]
        user_msg = Message(role="user", content=query, attachments=att_meta)
        session.add_message(user_msg)
        self._persist_session(session)
        if att_meta:
            yield f"data: {self._json_event('status', {'phase': 'attachments_saved', 'attachments': att_meta, 'message_id': user_msg.message_id})}\n\n"

        # 2.4 语义缓存命中直回：针对当前用户历史问答，语义相似的问题直接回复缓存内容，
        # 绕过知识检索 / Agent / LLM 生成的业务路由（仅纯文本、启用知识库、登录用户时生效）
        if (not attachments and use_knowledge and semantic_cache.semantic_cache_enabled()
                and user_id and user_id != "guest"):
            try:
                cache_hit = await semantic_cache.lookup(user_id, query)
            except Exception as e:
                logger.warning("语义缓存查询失败，走正常流程: %s", e)
                cache_hit = None
            if cache_hit:
                cached_answer = cache_hit.get("answer", "")
                obs.record_step(
                    "semantic_cache",
                    f"命中语义缓存（相似度 {cache_hit.get('score')}），直接回复历史回答",
                )
                yield f"data: {self._json_event('status', {'phase': 'cache_hit', 'message': '命中语义缓存，直接回复历史回答', 'score': cache_hit.get('score')})}\n\n"
                yield f"data: {self._json_event('token', {'content': cached_answer})}\n\n"
                assistant_msg = Message(
                    role="assistant",
                    content=cached_answer,
                    sources=cache_hit.get("sources", []),
                    use_knowledge=use_knowledge,
                    use_search=False,
                )
                session.add_message(assistant_msg)
                self._persist_session(session)
                from app.services.usage import record_usage
                record_usage(user_id, len(query), len(cached_answer),
                             use_knowledge=use_knowledge, use_search=False)
                yield f"data: {self._json_event('done', {'session_id': session.session_id, 'user_message_id': user_msg.message_id, 'message': assistant_msg.to_dict(), 'cache_hit': True, 'cache_score': cache_hit.get('score')})}\n\n"
                return

        # 2.5 聊天框附件工具调用：按用户文本意图路由到 OCR / 入库 / 总结工具
        tool_context_text = ""
        tool_records: List[dict] = []
        if attachments:
            yield f"data: {self._json_event('status', {'phase': 'tool_start', 'message': '正在处理附件...'})}\n\n"
            try:
                tool_context_text, tool_records = await tool_orchestrator.handle_attachments(
                    query, attachments, user_id, kb_id, session.session_id, user_msg.message_id,
                )
            except Exception as e:
                logger.error("附件工具调用失败: %s", e)
                tool_context_text = ""
            for rec in tool_records:
                if rec.get("confirmation"):
                    yield f"data: {self._json_event('tool_confirmation', {'action': rec['confirmation']})}\n\n"
                    continue
                obs.record_tool(
                    rec.get("tool_name") or "attachment_tool",
                    {"filename": rec.get("filename", "")},
                    # 识别原文不入 span（已由 note_segment("tool") 记录完整上下文），避免 trace 膨胀
                    {k: v for k, v in rec.items() if k != "text"},
                    status="ok" if rec.get("success") else "error",
                    error="" if rec.get("success") else "工具执行失败",
                    fail_trace=False,
                )
            executed = [rec for rec in tool_records if not rec.get("confirmation")]
            if executed:
                yield f"data: {self._json_event('status', {'phase': 'tool_done', 'message': '附件处理完成', 'tool_calls': [{k: v for k, v in r.items() if k != 'text'} for r in executed]})}\n\n"

        # 2.6 截图检索：图片 OCR 文本并入知识检索查询
        # retrieval_query=检索用（用户文本 + 图中文字）；effective_query=提问用
        # （用户只贴图不打字时，图中文字就是他想问的内容）
        retrieval_query, effective_query, ocr_excerpt = query, query, ""
        if tool_records:
            retrieval_query, ocr_excerpt = tool_orchestrator.build_ocr_retrieval_query(
                query, tool_records
            )
            if ocr_excerpt:
                if not query.strip():
                    effective_query = ocr_excerpt
                obs.record_step(
                    "ocr_retrieval",
                    f"图片识别 {len(ocr_excerpt)} 字并入检索查询"
                    + ("（用户未输入文本，以识别内容作为提问）" if not query.strip() else ""),
                )
                yield f"data: {self._json_event('status', {'phase': 'ocr_retrieval', 'message': f'已识别图片文字（{len(ocr_excerpt)} 字），将结合图中内容检索作答', 'ocr_chars': len(ocr_excerpt)})}\n\n"

        # 3. 构建 LLM 消息列表：系统 prompt + 用户跨会话记忆 + 历史消息（摘要压缩后）+ 工具结果
        # 统一走网关 chat_answer 业务路由（规则+模型由网关解析；llm_provider 参数已废弃）
        # model 为聊天页用户显式选择的模型（空=按业务路由/规则内均衡解析）
        llm = LLMFactory.create_for_role("chat_answer", user_id, rule_id, model or None)
        llm_messages = self._build_system_prompt()
        # segment 级 token 度量：系统提示段
        obs.note_segment("system", "\n".join(m.get("content", "") for m in llm_messages))

        # 记忆两层加载（借鉴 Claude Code：常驻索引层 + 按需检索层），合计受字符预算约束
        memory_text = ""
        profile_memories = memory_service.profile_memories(user_id)
        profile_text = "\n".join(f"- {m['content']}" for m in profile_memories)
        obs.note_segment("memory_profile", profile_text)
        relevant_memories = memory_service.retrieve_relevant(
            user_id, effective_query, exclude_ids=[m["id"] for m in profile_memories]
        )
        ctx_budget = int(getattr(settings, "MEMORY_CTX_CHARS", 600))
        relevant_lines, used = [], len(profile_text)
        for m in relevant_memories:
            line = f"- {m['content']}"
            if used + len(line) > ctx_budget:
                break
            relevant_lines.append(line)
            used += len(line)
        relevant_text = "\n".join(relevant_lines)
        obs.note_segment("memory_relevant", relevant_text)
        sections = []
        if profile_text:
            sections.append(f"用户画像（常驻）:\n{profile_text}")
        if relevant_text:
            sections.append(f"相关记忆:\n{relevant_text}")
        if sections:
            memory_text = "\n".join(sections)
            llm_messages.append({
                "role": "system",
                "content": f"【关于该用户的长期记忆，供参考，不要逐字复述】\n{memory_text}",
            })

        # 已启用的用户 Skill（prompt 类）注入：用户可在侧边栏「Skill 配置」定制并开关
        skills_text = skill_service.build_prompt_text(user_id)
        obs.note_segment("skills", skills_text)
        if skills_text:
            llm_messages.append({
                "role": "system",
                "content": f"【用户已启用的技能要求，请在回答中遵循】\n{skills_text}",
            })

        history_messages = session_summary.build_context_messages(
            session, settings.SESSION_MAX_HISTORY
        )
        # 历史消息不含本次刚保存的用户提问（最后一条）
        history_ctx = history_messages[:-1] if history_messages else []
        llm_messages.extend(history_ctx)
        obs.note_segment("history", "\n".join(m.get("content", "") for m in history_ctx))

        if tool_context_text:
            llm_messages.append({"role": "system", "content": tool_context_text})
            obs.note_segment("tool", tool_context_text)

        # 3.5 复杂度分流：simple→fast（下方现有流水线不变），moderate→ReAct，complex→Plan&Execute。
        # 带附件工具结果时强制 fast（附件为专用流程，Agent 不处理工具上下文）。
        if not tool_context_text:
            complexity = await classify_complexity(query, history_ctx)
            mode = complexity.get("mode", "fast")
            if mode in ("react", "plan_execute"):
                agent_result = {
                    "status": "ok", "answer": "", "sources": [],
                    "search_results": [], "hitl": None, "error": "",
                }
                agent_failed_early = False
                try:
                    async for ev in chat_agent.run_chat_agent(
                        mode=mode, query=query, user_id=user_id, rule_id=rule_id,
                        model=model,
                        use_knowledge=use_knowledge, use_search=use_search,
                        skip_search_prompt=skip_search_prompt, history_ctx=history_ctx,
                        memory_text=memory_text, system_prompt=self._build_system_prompt(),
                        result=agent_result,
                    ):
                        yield ev
                except (asyncio.CancelledError, GeneratorExit):
                    raise
                except Exception as e:
                    logger.error("主聊天 Agent 异常: %s", e)
                    obs.record_step("chat_agent", "", status="error", error=str(e), fail_trace=False)
                    # 未产出正文/卡片 → 早期失败，回退现有 fast 流水线（最坏情况=现状）
                    if not agent_result.get("answer") and not agent_result.get("hitl"):
                        agent_failed_early = True
                    else:
                        yield f"data: {self._json_event('error', {'message': f'复杂问答处理失败：{e}'})}\n\n"
                        return
                if not agent_failed_early:
                    async for ev in self._finalize_agent_turn(
                        session, user_msg, agent_result, use_knowledge, use_search, llm_provider
                    ):
                        yield ev
                    return
                logger.warning("主聊天 Agent 早期失败，回退现有流水线: mode=%s", mode)

        # 4. 知识检索 + 5. 联网搜索
        # 两者同时启用时并行执行；同步检索（embedding/rerank）放入线程池，
        # 避免阻塞事件循环拖慢其他请求
        sources = []
        search_results = []
        retrieve_task = None
        search_task = None

        if use_knowledge:
            # 发出检索开始信号
            yield f"data: {self._json_event('status', {'phase': 'retrieving', 'message': '正在检索知识库...'})}\n\n"

            # 意图改写：用 LLM 将检索查询改写为更适合向量检索的查询（超时保护，避免卡死流水线）
            # 检索查询=用户文本 + 图片 OCR 摘录（截图检索），改写负责把长摘录压成检索式
            yield f"data: {self._json_event('status', {'phase': 'rewriting', 'message': '正在理解你的问题...'})}\n\n"
            t_rewrite = time.monotonic()
            try:
                # 改写是增强而非必需：8s 内未完成即回退原问题检索，避免慢上游吃满 15s 旧预算
                search_query = await asyncio.wait_for(
                    rewrite_query(retrieval_query, llm_provider), timeout=8
                )
                obs.record_llm("intent_rewrite", retrieval_query, search_query, t0=t_rewrite, fail_trace=False)
            except asyncio.TimeoutError:
                logger.warning("意图改写超时，使用原问题检索")
                obs.record_llm("intent_rewrite", retrieval_query, "", t0=t_rewrite,
                               status="error", error="timeout", fail_trace=False)
                # 贴图场景回退时按预算截断（OCR 摘录可能数百字，过长会稀释检索信号）；
                # 纯文本提问保持原样，行为与改造前一致
                search_query = retrieval_query.strip()
                if ocr_excerpt:
                    search_query = search_query[:max(60, settings.OCR_RETRIEVAL_CHARS)]
            if search_query != retrieval_query.strip():
                logger.info("检索查询改写: %r → %r", retrieval_query[:50], search_query[:50])

            retrieve_task = asyncio.create_task(
                asyncio.to_thread(knowledge_service.retrieve_for_user, search_query, user_id)
            )
            tasks.add(retrieve_task)

        if use_search:
            yield f"data: {self._json_event('status', {'phase': 'searching', 'message': '正在联网搜索...'})}\n\n"
            search_task = asyncio.create_task(web_search(effective_query))
            tasks.add(search_task)

        # 等待检索结果（与联网搜索并行进行中）
        if retrieve_task is not None:
            results = []
            t_retrieve = time.monotonic()
            try:
                results = await retrieve_task
                obs.record_step("knowledge_retrieve", f"命中 {len(results)} 条相关片段", t0=t_retrieve)
            except Exception as e:
                logger.error("知识检索失败: %s", e)
                obs.record_step("knowledge_retrieve", "", t0=t_retrieve,
                                status="error", error=str(e), fail_trace=False)
            if results:
                # Rerank 重排序 + 相关度过滤（同步计算移入线程池，避免阻塞事件循环）
                yield f"data: {self._json_event('status', {'phase': 'reranking', 'message': '正在对检索结果重排序...'})}\n\n"
                context_text, sources = await asyncio.to_thread(
                    knowledge_service.format_knowledge_context, effective_query, results
                )

                # 轻量 Self-RAG：信息缺口判断，证据不足时改写查询重检一次
                if context_text.strip():
                    yield f"data: {self._json_event('status', {'phase': 'gap_checking', 'message': '正在评估证据是否充分...'})}\n\n"
                    t_evidence = time.monotonic()
                    sufficient, missing = await check_evidence_sufficiency(
                        effective_query, context_text, llm_provider
                    )
                    obs.record_llm(
                        "evidence_check",
                        f"问题：{effective_query}\n\n证据摘录：\n{context_text[:2000]}",
                        json.dumps({"sufficient": sufficient, "missing": missing or ""}, ensure_ascii=False),
                        t0=t_evidence, fail_trace=False,
                    )
                    if not sufficient:
                        recheck_msg = f'证据不足（{missing or "信息缺口"}），正在补充重检...'
                        yield f"data: {self._json_event('status', {'phase': 'rechecking', 'message': recheck_msg})}\n\n"
                        results, context_text, sources = await self._recheck_once(
                            effective_query, missing, user_id, results, llm_provider
                        )
                        # HITL：重检后证据仍不充分 → 判断缺口是否源于意图模糊，需要澄清追问
                        clarification = await decide_clarification(effective_query, context_text, missing)
                        if clarification.get("should_clarify"):
                            yield chat_agent.clarify_event(
                                clarification["question"], clarification["options"]
                            )
                            yield f"data: {self._json_event('done', {'session_id': session.session_id, 'user_message_id': user_msg.message_id, 'hitl': 'clarify'})}\n\n"
                            return

                augmented_query = knowledge_service.build_knowledge_prompt(effective_query, context_text, True)
                # segment 级 token 度量：知识上下文段（含重检后的最终上下文）
                obs.note_segment("knowledge", context_text)

                # 发出检索完成信号
                yield f"data: {self._json_event('status', {'phase': 'retrieved', 'message': f'检索到 {len(sources)} 份相关知识文档', 'sources': sources})}\n\n"
            else:
                # HITL：只知识检索、未开联网、未跳过询问 → 弹卡询问是否联网搜索，结束本轮
                if not use_search and not skip_search_prompt:
                    yield chat_agent.need_search_event(_card_query(query, effective_query))
                    yield f"data: {self._json_event('done', {'session_id': session.session_id, 'user_message_id': user_msg.message_id, 'hitl': 'need_search'})}\n\n"
                    return
                # 用户已选「直接回答」（skip_search_prompt）→ 不受知识库约束，用模型自身知识作答
                kb_flag = False if skip_search_prompt else True
                augmented_query = knowledge_service.build_knowledge_prompt(effective_query, "", kb_flag)
                yield f"data: {self._json_event('status', {'phase': 'not_found', 'message': '知识库中未检索到相关内容'})}\n\n"
        else:
            augmented_query = knowledge_service.build_knowledge_prompt(effective_query, "", False)

        # 等待联网搜索结果
        if search_task is not None:
            try:
                web_results = await search_task
                search_context = knowledge_service.build_search_context(web_results)

                if search_context:
                    # segment 级 token 度量：联网搜索上下文段
                    obs.note_segment("search", search_context)
                    if use_knowledge:
                        # 同时启用知识库和搜索：知识库优先，搜索为辅
                        augmented_query += (
                            f"\n\n【联网搜索结果（供参考）】\n{search_context}\n\n"
                            f"要求：\n"
                            f"1. 【核心】优先以知识库内容为主要回答依据\n"
                            f"2. 联网搜索结果仅作为补充参考，可用于佐证或扩展知识库中的内容\n"
                            f"3. 如果知识库内容和搜索内容有冲突，以知识库为准\n"
                            f"4. 标注引用来源时，区分「来源 X」（知识库）和「网络来源 X」（搜索）"
                        )
                    else:
                        augmented_query += f"\n\n【联网搜索结果】\n{search_context}\n\n请基于以上搜索结果回答问题，并标注来源链接。"
                    search_results = [r.to_dict() for r in web_results]
                    yield f"data: {self._json_event('status', {'phase': 'searched', 'message': f'联网搜索到 {len(web_results)} 条结果', 'search_results': search_results})}\n\n"
                else:
                    yield f"data: {self._json_event('status', {'phase': 'search_empty', 'message': '联网搜索无结果'})}\n\n"
            except Exception as e:
                yield f"data: {self._json_event('status', {'phase': 'search_error', 'message': f'联网搜索出错: {str(e)}'})}\n\n"

        # 6. 添加用户问题
        llm_messages.append({"role": "user", "content": augmented_query})

        # 7. 流式 LLM 生成
        yield f"data: {self._json_event('status', {'phase': 'generating', 'message': '正在生成回答...'})}\n\n"

        logger.info("开始 LLM 生成: 路由=网关业务路由(chat_answer), history_messages=%d",
                     len(llm_messages))
        full_response = ""
        llm_error = None
        t_gen = time.monotonic()
        try:
            async with aclosing(llm.chat(llm_messages, stream=True)) as stream:
                async for chunk in stream:
                    full_response += chunk
                    yield f"data: {self._json_event('token', {'content': chunk})}\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            obs.record_llm("answer", llm_messages, full_response, t0=t_gen, status="cancelled")
            raise
        except Exception as e:
            llm_error = e
        # 注：旧 OpenRouter 免费模型降级已移除——网关代理在多上游间自动故障切换，
        # 全部上游失败时错误直接下传（日志含路由规则/模型/上游链路）

        # 追踪层：最终生成 LLM 调用（完整 prompt + 回答；失败即标记 trace 失败）
        answer_pt, _ = obs.record_llm(
            "answer", llm_messages, full_response, t0=t_gen,
            status="error" if llm_error is not None else "ok",
            error=str(llm_error) if llm_error is not None else "",
        )
        # segment 级 token 度量：按最终生成调用的真实 prompt_tokens 分摊落库（与成本口径同源）
        obs.flush_segments(llm_messages, answer_pt)

        if llm_error is not None:
            error_msg = str(llm_error)
            from app.core.llm import _log_llm_failure
            _log_llm_failure("生成", llm_error)

            # 判读具体错误类型，给出用户可理解的提示
            low_msg = error_msg.lower()
            if "openrouter" in low_msg or "rate limit" in low_msg or "rate-limited" in low_msg or "free-models" in low_msg:
                hint = ("当前模型被限流（上游免费额度暂时用完），网关已尝试全部候选上游仍失败。\n\n"
                        "解决方式：\n"
                        "1. 稍后重试（限流按分钟/每日重置）\n"
                        "2. 或在模型网关「上游管理/路由规则」中补充其他上游与免费模型\n"
                        "3. 或在「业务路由」为本角色切换其他规则/模型")
            elif "insufficient_quota" in error_msg or "429" in error_msg:
                hint = ("API 额度不足或限流，请检查上游账户余额。\n\n"
                        "解决方式：\n"
                        "1. 登录上游控制台充值/查看配额\n"
                        "2. 或在模型网关「上游管理」中启用其他上游（网关自动故障切换）\n"
                        "3. 或在「业务路由」为本角色切换其他规则/模型")
            elif "connect" in error_msg.lower() or "connection" in error_msg.lower():
                hint = ("无法连接到模型网关上游，请检查：\n"
                        "1. 模型网关「上游管理」中上游地址与密钥是否正确\n"
                        "2. 上游服务是否可达（可用上游行「测试」按钮验证）\n"
                        "3. 本机网络与代理设置")
            else:
                hint = f"生成回答时出错: {error_msg}"

            # 发出错误事件，让前端显示错误而不是一直转圈
            yield f"data: {self._json_event('error', {'message': hint})}\n\n"
            full_response = hint

        # 7.5 轻量 Self-RAG：核对回答中引用编号的真实性
        # 引用越界（如来源只有 2 条却标注 [来源 5]）时追加提醒，避免误导用户
        if use_knowledge and full_response and llm_error is None:
            invalid_citations = verify_citations(full_response, len(sources))
            if invalid_citations:
                logger.warning("引用编号核对: 发现越界引用 %s（实际来源 %d 条）",
                               invalid_citations, len(sources))
                notice = (
                    "\n\n> ⚠️ 引用核对提醒：回答中的来源编号 "
                    + "、".join(f"[来源 {i}]" for i in invalid_citations)
                    + " 与实际知识来源不对应，请注意甄别。"
                )
                yield f"data: {self._json_event('token', {'content': notice})}\n\n"
                full_response += notice
                yield f"data: {self._json_event('status', {'phase': 'citations_checked', 'message': '已核对引用编号', 'invalid_citations': invalid_citations})}\n\n"

        # 8. 保存助手消息（LLM 生成失败时不保存知识来源，避免展示与回答无关的检索结果）
        assistant_msg = Message(
            role="assistant",
            content=full_response,
            sources=sources if llm_error is None else [],
            search_results=search_results if llm_error is None else [],
            use_knowledge=use_knowledge,
            use_search=use_search,
        )
        # 后处理也属于本轮总预算；取消后不再发起摘要或记忆模型调用。
        from copy import copy
        summary_session = copy(session)
        summary_session.messages = [*session.messages, assistant_msg]
        await self._post_turn_background(summary_session, user_msg, assistant_msg, llm_provider)
        if not db.query_one("SELECT message_id FROM chat_messages WHERE message_id=?", (user_msg.message_id,)):
            raise asyncio.CancelledError()
        session.add_message(assistant_msg)
        self._persist_session(session)  # 每次对话后持久化

        # 8.5 写入语义缓存：本次正常作答（无错误、纯文本、启用知识库）沉淀为当前用户缓存，
        # 供后续相似问题命中直回；失败静默降级，不影响 done。
        if llm_error is None and not attachments and use_knowledge:
            await semantic_cache.store(user_id, effective_query, full_response, sources)

        # 用量统计埋点：查询次数 + 字符数（Token 估算由统计层完成）
        from app.services.usage import record_usage
        prompt_chars = sum(len(m.get("content", "")) for m in llm_messages)
        record_usage(
            user_id,
            prompt_chars,
            len(full_response),
            use_knowledge=use_knowledge,
            use_search=use_search,
        )

        # 9. 发送完成事件
        done_data = {
            'session_id': session.session_id,
            'user_message_id': user_msg.message_id,
            'message': assistant_msg.to_dict(),
        }
        yield f"data: {self._json_event('done', done_data)}\n\n"

    async def _finalize_agent_turn(self, session, user_msg, result, use_knowledge, use_search, llm_provider):
        """Agent 轮次收尾：HITL 暂停只发 done；正常完成则落库/摘要/记忆/用量后发 done。

        Agent 本身不发 done（单一 done 所有者=chat.py）；答案元信息由 Agent 回填到 result。
        """
        # HITL 暂停：不落库助手消息、不做摘要/记忆/用量，仅发 done（携带 hitl 标记）
        if result.get("hitl"):
            yield f"data: {self._json_event('done', {'session_id': session.session_id, 'user_message_id': user_msg.message_id, 'hitl': result['hitl']})}\n\n"
            return
        # Agent 内部已出错（已发 error 事件）：不再落库，直接结束（外层见 error 会 return）
        if result.get("status") == "error":
            return
        full_response = result.get("answer", "")
        assistant_msg = Message(
            role="assistant",
            content=full_response,
            sources=result.get("sources", []),
            search_results=result.get("search_results", []),
            use_knowledge=use_knowledge,
            use_search=use_search,
        )
        from copy import copy
        summary_session = copy(session)
        summary_session.messages = [*session.messages, assistant_msg]
        await self._post_turn_background(summary_session, user_msg, assistant_msg, llm_provider)
        if not db.query_one("SELECT message_id FROM chat_messages WHERE message_id=?", (user_msg.message_id,)):
            raise asyncio.CancelledError()
        session.add_message(assistant_msg)
        self._persist_session(session)
        # 写入语义缓存（Agent 轮次的正常作答同样沉淀，纯文本、启用知识库时）
        if use_knowledge and not user_msg.attachments:
            await semantic_cache.store(session.user_id, user_msg.content, full_response,
                                       result.get("sources", []))
        from app.services.usage import record_usage
        record_usage(
            session.user_id,
            result.get("prompt_chars", len(full_response)),
            len(full_response),
            use_knowledge=use_knowledge,
            use_search=use_search,
        )
        yield f"data: {self._json_event('done', {'session_id': session.session_id, 'user_message_id': user_msg.message_id, 'message': assistant_msg.to_dict()})}\n\n"

    async def _recheck_once(
        self,
        query: str,
        missing: str,
        user_id: str,
        first_results: list,
        llm_provider: str = None,
    ) -> tuple[list, str, list]:
        """证据不足时的单次补充重检（轻量 Self-RAG 的第二步）

        用缺失要点确定性拼接出新检索查询（省一次改写 LLM 调用，2~15s），
        重新检索并与首轮结果去重合并，再整体重排序一次。任何环节失败都静默降级为首轮结果。

        Returns:
            (merged_results, context_text, sources)
        """
        try:
            # 1. 结合缺失要点确定性构造重检查询（与改写超时兜底同语义，免去一次 LLM 往返）
            improved_query = f"{query.strip()} {missing.strip()}"[:120] if missing else query.strip()
            logger.info("Self-RAG 补充重检: %r", improved_query[:60])

            # 2. 重新检索（同步检索移入线程池）
            new_results = await asyncio.to_thread(
                knowledge_service.retrieve_for_user, improved_query, user_id
            )
            if not new_results:
                context_text, sources = await asyncio.to_thread(
                    knowledge_service.format_knowledge_context, query, first_results
                )
                return first_results, context_text, sources

            # 3. 与首轮结果去重合并（按向量库 result_key）
            merged: list = list(first_results)
            seen_keys = {vector_store.result_key(t, m) for t, m, _ in first_results}
            for text, meta, score in new_results:
                key = vector_store.result_key(text, meta)
                if key not in seen_keys:
                    seen_keys.add(key)
                    merged.append((text, meta, score))

            # 4. 合并后整体重排序一次
            context_text, sources = await asyncio.to_thread(
                knowledge_service.format_knowledge_context, query, merged
            )
            return merged, context_text, sources
        except Exception as e:
            logger.warning("Self-RAG 重检失败，使用首轮结果: %s", e)
            context_text, sources = await asyncio.to_thread(
                knowledge_service.format_knowledge_context, query, first_results
            )
            return first_results, context_text, sources

    async def _post_turn_background(self, session, user_msg, assistant_msg, llm_provider):
        """在当前请求总预算内完成摘要与记忆；普通错误降级，取消继续传播。"""
        try:
            await session_summary.maybe_summarize(session, llm_provider)
        except Exception as e:
            logger.warning("会话摘要任务异常: %s", e)
        try:
            await memory_service.extract_from_turn(
                session.user_id, session.session_id, user_msg.content, assistant_msg.content
            )
        except Exception as e:
            logger.warning("用户记忆抽取任务异常: %s", e)

    def _build_system_prompt(self) -> List[dict]:
        """构建系统提示词。

        正文由 prompt_store 托管：默认回退内置正文，若 Prompt 评测「上线」了新版本，
        则采用已发布版本正文（固定尾规 FINAL_ANSWER_RULES 始终追加，保证引用/证据契约不变）。
        """
        from app.services import prompt_store

        return [
            {
                "role": "system",
                "content": prompt_store.get_active_body() + FINAL_ANSWER_RULES,
            }
        ]

    @staticmethod
    def _json_event(event: str, data: dict) -> str:
        """构建 SSE 事件"""
        payload = {"event": event, **data}
        return json.dumps(payload, ensure_ascii=False)


# 全局单例
chat_service = ChatService()
