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
import json
import logging
import uuid
from datetime import datetime
from typing import AsyncGenerator, List, Optional

from app.config import settings
from app.core import db
from app.core.evidence_checker import check_evidence_sufficiency, verify_citations
from app.core.llm import LLMFactory
from app.core.intent_rewriter import rewrite_query
from app.core.vector_store import vector_store
from app.services.config_manager import runtime_config
from app.services.knowledge import knowledge_service
from app.services.memory import memory_service
from app.services.records import upload_records
from app.services import session_summary
from app.services import tool_orchestrator
from app.services.web_search import web_search

logger = logging.getLogger("jianziruyang.chat")


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
    ):
        self.message_id = message_id or str(uuid.uuid4())
        self.role = role  # "user" | "assistant"
        self.content = content
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
            self.title = message.content[:30] + "..." if len(message.content) > 30 else message.content

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
                ))
            self.sessions[session_id] = session
            return session
        except Exception as e:
            logger.error("按需加载会话失败: %s", e)
            return None

    def _persist_session(self, session: ChatSession):
        """将单个会话完整写入 SQLite（会话行 upsert + 消息全量替换）"""
        try:
            db.execute(
                "INSERT INTO chat_sessions (session_id, user_id, title, created_at, updated_at)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                " user_id=excluded.user_id, title=excluded.title, updated_at=excluded.updated_at",
                (session.session_id, session.user_id, session.title,
                 session.created_at, session.updated_at),
            )
            db.execute("DELETE FROM chat_messages WHERE session_id = ?", (session.session_id,))
            db.executemany(
                "INSERT INTO chat_messages (message_id, session_id, seq, role, content,"
                " sources, search_results, use_knowledge, use_search, attachments, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        m.message_id, session.session_id, seq, m.role, m.content,
                        json.dumps(m.sources, ensure_ascii=False),
                        json.dumps(m.search_results, ensure_ascii=False),
                        1 if m.use_knowledge else 0,
                        1 if m.use_search else 0,
                        json.dumps(m.attachments, ensure_ascii=False),
                        session.created_at,
                    )
                    for seq, m in enumerate(session.messages)
                ],
            )
        except Exception as e:
            logger.error("保存会话失败: %s", e)

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
    ) -> AsyncGenerator[str, None]:
        """流式聊天

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
        logger.info("开始处理聊天: session=%s, query=%.50s, knowledge=%s, search=%s",
                     session_id, query, use_knowledge, use_search)

        # 1. 获取或创建会话（已存在时校验归属）
        session = self.get_session(session_id)
        if session and session.user_id and session.user_id != user_id:
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
        if att_meta:
            yield f"data: {self._json_event('status', {'phase': 'attachments_saved', 'attachments': att_meta})}\n\n"

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
            if tool_records:
                yield f"data: {self._json_event('status', {'phase': 'tool_done', 'message': '附件处理完成', 'tool_calls': tool_records})}\n\n"

        # 3. 构建 LLM 消息列表：系统 prompt + 用户跨会话记忆 + 历史消息（摘要压缩后）+ 工具结果
        llm = LLMFactory.create(llm_provider)
        llm_messages = self._build_system_prompt()

        relevant_memories = memory_service.retrieve_relevant(user_id, query)
        if relevant_memories:
            memory_text = "\n".join(f"- {m['content']}" for m in relevant_memories)
            llm_messages.append({
                "role": "system",
                "content": f"【关于该用户的长期记忆，供参考，不要逐字复述】\n{memory_text}",
            })

        history_messages = session_summary.build_context_messages(
            session, settings.SESSION_MAX_HISTORY
        )
        # 历史消息不含本次刚保存的用户提问（最后一条）
        llm_messages.extend(history_messages[:-1] if history_messages else [])

        if tool_context_text:
            llm_messages.append({"role": "system", "content": tool_context_text})

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

            # 意图改写：用 LLM 将用户问题改写为更适合检索的查询（超时保护，避免卡死流水线）
            yield f"data: {self._json_event('status', {'phase': 'rewriting', 'message': '正在理解你的问题...'})}\n\n"
            try:
                search_query = await asyncio.wait_for(
                    rewrite_query(query, llm_provider), timeout=15
                )
            except asyncio.TimeoutError:
                logger.warning("意图改写超时，使用原问题检索")
                search_query = query.strip()
            if search_query != query.strip():
                logger.info("检索查询改写: %r → %r", query[:50], search_query[:50])

            retrieve_task = asyncio.create_task(
                asyncio.to_thread(knowledge_service.retrieve_for_user, search_query, user_id)
            )

        if use_search:
            yield f"data: {self._json_event('status', {'phase': 'searching', 'message': '正在联网搜索...'})}\n\n"
            search_task = asyncio.create_task(web_search(query))

        # 等待检索结果（与联网搜索并行进行中）
        if retrieve_task is not None:
            results = []
            try:
                results = await retrieve_task
            except Exception as e:
                logger.error("知识检索失败: %s", e)
            if results:
                # Rerank 重排序 + 相关度过滤（同步计算移入线程池，避免阻塞事件循环）
                yield f"data: {self._json_event('status', {'phase': 'reranking', 'message': '正在对检索结果重排序...'})}\n\n"
                context_text, sources = await asyncio.to_thread(
                    knowledge_service.format_knowledge_context, query, results
                )

                # 轻量 Self-RAG：信息缺口判断，证据不足时改写查询重检一次
                if context_text.strip():
                    yield f"data: {self._json_event('status', {'phase': 'gap_checking', 'message': '正在评估证据是否充分...'})}\n\n"
                    sufficient, missing = await check_evidence_sufficiency(
                        query, context_text, llm_provider
                    )
                    if not sufficient:
                        recheck_msg = f'证据不足（{missing or "信息缺口"}），正在改写重检...'
                        yield f"data: {self._json_event('status', {'phase': 'rechecking', 'message': recheck_msg})}\n\n"
                        results, context_text, sources = await self._recheck_once(
                            query, missing, user_id, results, llm_provider
                        )

                augmented_query = knowledge_service.build_knowledge_prompt(query, context_text, True)

                # 发出检索完成信号
                yield f"data: {self._json_event('status', {'phase': 'retrieved', 'message': f'检索到 {len(sources)} 条相关知识', 'sources': sources})}\n\n"
            else:
                augmented_query = knowledge_service.build_knowledge_prompt(query, "", True)
                yield f"data: {self._json_event('status', {'phase': 'not_found', 'message': '知识库中未检索到相关内容'})}\n\n"
        else:
            augmented_query = knowledge_service.build_knowledge_prompt(query, "", False)

        # 等待联网搜索结果
        if search_task is not None:
            try:
                web_results = await search_task
                search_context = knowledge_service.build_search_context(web_results)

                if search_context:
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

        logger.info("开始 LLM 生成: provider=%s, history_messages=%d",
                     llm_provider or runtime_config.llm_provider or settings.LLM_PROVIDER,
                     len(llm_messages))
        full_response = ""
        llm_error = None
        try:
            async for chunk in llm.chat(llm_messages, stream=True):
                full_response += chunk
                yield f"data: {self._json_event('token', {'content': chunk})}\n\n"
        except Exception as e:
            llm_error = e

        # OpenRouter 免费模型降级：尚无输出且当前模型不可用时，
        # 重新拉取免费模型列表并随机切换一个免费模型重试
        if llm_error is not None and not full_response:
            from app.core.openrouter import try_openrouter_fallback
            fallback_model = await try_openrouter_fallback(llm_provider)
            if fallback_model:
                yield f"data: {self._json_event('status', {'phase': 'fallback', 'message': f'当前模型不可用，已自动切换为免费模型 {fallback_model}'})}\n\n"
                llm = LLMFactory.create(llm_provider)
                try:
                    async for chunk in llm.chat(llm_messages, stream=True):
                        full_response += chunk
                        yield f"data: {self._json_event('token', {'content': chunk})}\n\n"
                    llm_error = None
                except Exception as e2:
                    llm_error = e2

        if llm_error is not None:
            error_msg = str(llm_error)
            from app.core.llm import _log_llm_failure
            _log_llm_failure("生成", llm_error)

            # 判读具体错误类型，给出用户可理解的提示
            low_msg = error_msg.lower()
            if "openrouter" in low_msg or "rate limit" in low_msg or "rate-limited" in low_msg or "free-models" in low_msg:
                hint = ("当前免费模型被限流（OpenRouter 免费额度暂时用完）。\n\n"
                        "解决方式：\n"
                        "1. 稍后重试（限流按分钟/每日重置）\n"
                        "2. 或在系统设置中切换其他免费模型（系统也会自动降级切换）\n"
                        "3. 或切换 LLM_PROVIDER 为 ollama（本地模型）/ 付费提供商")
            elif "insufficient_quota" in error_msg or "429" in error_msg:
                hint = ("API 额度不足，请检查你的 ModelScope / 百炼账户余额。\n\n"
                        "解决方式：\n"
                        "1. 登录 https://modelscope.cn 充值\n"
                        "2. 或切换 LLM_PROVIDER 为 ollama（本地模型）\n"
                        "3. 或更换 API_KEY 使用其他提供商（DeepSeek / OpenAI）")
            elif "connect" in error_msg.lower() or "connection" in error_msg.lower():
                hint = ("无法连接到 LLM 服务，请检查：\n"
                        "1. 如果使用 Ollama: 运行 `ollama serve` 启动服务\n"
                        "2. 如果使用 API: 检查 API_BASE_URL 和 API_KEY 是否正确\n"
                        "3. 检查网络连接")
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
        session.add_message(assistant_msg)
        self._persist_session(session)  # 每次对话后持久化

        # 后台异步触发会话摘要压缩与用户跨会话记忆抽取，失败不影响主流程
        asyncio.create_task(self._post_turn_background(session, user_msg, assistant_msg, llm_provider))

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

    async def _recheck_once(
        self,
        query: str,
        missing: str,
        user_id: str,
        first_results: list,
        llm_provider: str = None,
    ) -> tuple[list, str, list]:
        """证据不足时的单次改写重检（轻量 Self-RAG 的第二步）

        用缺失要点引导 LLM 改写出新检索查询，重新检索并与首轮结果去重合并，
        再整体重排序一次。任何环节失败都静默降级为首轮结果。

        Returns:
            (merged_results, context_text, sources)
        """
        try:
            # 1. 结合缺失要点改写检索查询（超时保护）
            rewrite_input = f"{query}（补充关注：{missing}）" if missing else query
            try:
                improved_query = await asyncio.wait_for(
                    rewrite_query(rewrite_input, llm_provider), timeout=15
                )
            except asyncio.TimeoutError:
                improved_query = rewrite_input
            logger.info("Self-RAG 改写重检: %r", improved_query[:60])

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
        """对话结束后的后台任务：会话摘要压缩 + 用户记忆抽取（都内含异常降级，不抛出）"""
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
        """构建系统提示词"""
        return [
            {
                "role": "system",
                "content": (
                    "你是「见字如面」智能助手，一个专注于健康养生知识库的问答系统。\n\n"
                    "你的能力：\n"
                    "1. 基于知识库内容回答用户关于健康、养生、中医等方面的问题\n"
                    "2. 引用知识库原文和原始图片来源\n"
                    "3. 结合联网搜索结果提供更全面的信息\n\n"
                    "行为准则：\n"
                    "- 回答要准确、专业、有据可查\n"
                    "- 不确定的内容不要编造，明确告知用户\n"
                    "- 引用来源时，清晰标注原文出处\n"
                    "- 以温暖、关怀的语气与用户交流\n"
                ),
            }
        ]

    @staticmethod
    def _json_event(event: str, data: dict) -> str:
        """构建 SSE 事件"""
        payload = {"event": event, **data}
        return json.dumps(payload, ensure_ascii=False)


# 全局单例
chat_service = ChatService()
