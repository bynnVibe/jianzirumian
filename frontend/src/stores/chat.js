import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { consumeChatStream, createTextPresenter, mergeChatAction } from '@/utils/chatStream'
import {
  sendChatMessage,
  sendChatMessageWithFiles,
  createSession,
  listSessions,
  deleteSession,
  switchSession,
  getChatHistory,
  clearChatHistory,
  stopChatGeneration,
  listCheckpoints,
  abandonCheckpoint,
} from '@/api'

export const useChatStore = defineStore('chat', () => {
  // ---- 状态 ----
  const sessions = ref([])
  const currentSessionId = ref(null)
  const messages = ref([])
  const isGenerating = ref(false)
  const currentResponse = ref('')
  const sources = ref([])
  const searchResults = ref([])
  const toolSources = ref([])
  const toolDraft = ref(null)
  const generationPhase = ref('') // tool_start | ocr_retrieval | rewriting | retrieving | reranking | gap_checking | rechecking | retrieved | not_found | searching | searched | generating | citations_checked
  const phaseHistory = ref([]) // 本次对话经历过的阶段序列（供流水线进度条标记已完成步骤）
  const toolCalls = ref([]) // 本次对话附件工具调用结果 [{tool_name, filename, success, chunk_count}, ...]
  const editingDraft = ref(null) // 再编辑草稿 { text }，由 ChatInput 消费
  let controller = null
  let generationId = 0
  // 会话消息内存快照（上限 20 个会话）：切会话先展示快照不闪空，注销即清
  const sessionSnapshots = new Map()
  function saveSnapshot(sessionId, msgs) {
    if (!sessionId || !msgs?.length) return
    sessionSnapshots.delete(sessionId)
    sessionSnapshots.set(sessionId, msgs)
    while (sessionSnapshots.size > 20) {
      sessionSnapshots.delete(sessionSnapshots.keys().next().value)
    }
  }
  const pendingDone = ref(null) // 最近一次 done 事件数据（含 message_id）
  const cacheHitInfo = ref(null) // 本轮是否命中语义缓存 { score }（命中则绕过检索路由直回）
  const capabilityGap = ref(null) // 本轮能力受限说明（DeepSeek 式降级）：随回答附带限制说明与应用内动作建议
  const streamError = ref(null) // 本轮结构化错误 {code, stage, message}：过程面板定位失败步骤
  const skillDraft = ref(null) // 本轮 Skill 沉淀配置草稿：随回答呈现确认卡，确认后系统自动导入
  const pendingCheckpoint = ref(null) // 中途停止任务的 checkpoint：界面呈现「是否继续」，继续则在前序基础上续答

  // ---- Agent（复杂度分流）过程状态 ----
  const agentPlan = ref([]) // Plan&Execute 步骤清单 [{tool, purpose, query}]
  const agentThoughts = ref([]) // ReAct 思考链文本
  const agentTools = ref([]) // 工具调用进度 [{tool, state, query, summary, index, total, purpose, failed}]

  // ---- 配置 ----
  const useKnowledge = ref(true)
  const useSearch = ref(false)
  const llmProvider = ref('')
  const selectedModel = ref('') // 用户在聊天页显式选择的出网模型（仅限匹配规则上游模型；空=规则内均衡解析）

  // ---- 计算属性 ----
  const currentSession = computed(() => {
    return sessions.value.find((s) => s.session_id === currentSessionId.value)
  })

  // ---- 方法 ----
  async function loadSessions() {
    const epoch = generationId
    try {
      const res = await listSessions()
      if (epoch !== generationId) return
      sessions.value = res.data.sessions || []
      if (res.data.current_session_id) {
        currentSessionId.value = res.data.current_session_id
      }
    } catch (err) {
      console.error('加载会话列表失败:', err)
    }
  }

  async function newSession() {
    stopGeneration()
    const epoch = generationId
    try {
      const res = await createSession()
      if (epoch !== generationId) return null
      const session = res.data
      sessions.value.unshift(session)
      currentSessionId.value = session.session_id
      messages.value = []
      pendingCheckpoint.value = null
      return session
    } catch (err) {
      console.error('创建会话失败:', err)
      return null
    }
  }

  async function switchToSession(sessionId) {
    stopGeneration()
    const epoch = generationId
    // 留存旧会话快照，切回时可直接展示
    saveSnapshot(currentSessionId.value, messages.value)
    currentSessionId.value = sessionId
    // 内存快照先展示（无快照才置空），后台拉取最新历史
    messages.value = sessionSnapshots.get(sessionId) || []
    try {
      // 切换标记与历史拉取互不依赖，并行发起；保留 generationId 防旧响应覆盖
      const [, res] = await Promise.all([switchSession(sessionId), getChatHistory(sessionId)])
      if (epoch !== generationId) return
      const fresh = (res.data.messages || []).map(normalizeMessage)
      messages.value = fresh
      saveSnapshot(sessionId, fresh)
      await refreshCheckpoints()
    } catch (err) {
      console.error('切换会话失败:', err)
    }
  }

  /** 统一消息结构：后端附件元数据 {file_id, filename, kind} → 前端 chip {name, file_id, kind} */
  function normalizeMessage(m) {
    return {
      ...m,
      cache_hit: !!m.cache_info?.hit,
      cache_score: m.cache_info?.score || 0,
      attachments: (m.attachments || []).map((a) => ({
        name: a.filename || a.name || '',
        file_id: a.file_id || '',
        kind: a.kind || '',
      })),
    }
  }

  async function removeSession(sessionId) {
    if (sessionId === currentSessionId.value) stopGeneration()
    try {
      await deleteSession(sessionId)
      sessions.value = sessions.value.filter((s) => s.session_id !== sessionId)

      if (currentSessionId.value === sessionId) {
        if (sessions.value.length > 0) {
          await switchToSession(sessions.value[0].session_id)
        } else {
          await newSession()
        }
      }
    } catch (err) {
      console.error('删除会话失败:', err)
    }
  }

  async function sendMessage(query, files = [], options = {}) {
    if (isGenerating.value || (!query.trim() && !(files && files.length))) return

    // 初始化生成状态
    isGenerating.value = true
    currentResponse.value = ''
    sources.value = []
    searchResults.value = []
    generationPhase.value = ''
    phaseHistory.value = []
    toolCalls.value = []
    agentPlan.value = []
    agentThoughts.value = []
    agentTools.value = []
    cacheHitInfo.value = null
    capabilityGap.value = null
    streamError.value = null
    skillDraft.value = null
    toolDraft.value = null
    toolSources.value = []
    resolveHitl() // 清除上一轮遗留的 HITL 卡片
    const skipSearchPrompt = !!(options && options.skipSearchPrompt)

    let streamDoneReceived = false
    const requestId = ++generationId
    const requestController = new AbortController()
    controller = requestController
    pendingDone.value = null
    const presenter = createTextPresenter(text => {
      if (requestId === generationId) handleStreamEvent({ event: 'token', content: text })
    }, requestController.signal)

    try {
      if (!currentSessionId.value) {
        const { data } = await createSession()
        if (requestId !== generationId) return
        sessions.value.unshift(data)
        currentSessionId.value = data.session_id
        messages.value = []
      }
      messages.value.push({
        role: 'user', content: query, sources: [], search_results: [],
        use_knowledge: useKnowledge.value, use_search: useSearch.value,
        attachments: (files || []).map(f => ({ name: f.name })),
      })
      const hasFiles = files && files.length > 0
      const response = hasFiles
        ? await sendChatMessageWithFiles(
            {
              session_id: currentSessionId.value,
              query,
              use_knowledge: useKnowledge.value,
              use_search: useSearch.value,
              llm_provider: llmProvider.value,
              model: selectedModel.value,
              skip_search_prompt: skipSearchPrompt,
              force_refresh: !!options.forceRefresh,
              command: options.command || '',
              resume_checkpoint_id: options.resumeCheckpointId || '',
              reuse_retrieval: !!options.reuseRetrieval,
            },
            files,
            requestController.signal,
          )
        : await sendChatMessage({
            session_id: currentSessionId.value,
            query,
            use_knowledge: useKnowledge.value,
            use_search: useSearch.value,
            llm_provider: llmProvider.value,
            model: selectedModel.value,
            skip_search_prompt: skipSearchPrompt,
            force_refresh: !!options.forceRefresh,
            command: options.command || '',
            resume_checkpoint_id: options.resumeCheckpointId || '',
            reuse_retrieval: !!options.reuseRetrieval,
          }, requestController.signal)

      await consumeChatStream(response, data => {
        if (requestId !== generationId) return
        if (data.event === 'token') presenter.push(data.content || '')
        else handleStreamEvent(data)
      }, requestController.signal)
      await presenter.finish()
      streamDoneReceived = !requestController.signal.aborted
    } catch (err) {
      await presenter.finish()
      if (requestId === generationId) {
        const reason = err.name === 'AbortError' ? '已停止生成' : err.message || '网络连接中断'
        // 结构化错误码/失败阶段随中断提示展示，不再只有笼统「未知错误」
        const detail = err.code ? `［${err.code}${err.stage ? ' · ' + err.stage : ''}］` : ''
        if (err.code && !streamError.value) streamError.value = { code: err.code, stage: err.stage || '', message: err.message || '' }
        currentResponse.value += `\n\n[生成已中断，内容不完整：${reason}${detail}]`
        sources.value = []
        searchResults.value = []
      }
    } finally {
      presenter.cancel()
      requestController.abort()
      if (requestId !== generationId) return
      controller = null
      isGenerating.value = false

      // 回填后端生成的用户消息 message_id（反馈绑定需要）
      const lastUser = [...messages.value].reverse().find((m) => m.role === 'user')
      if (lastUser && pendingDone.value?.user_message_id) {
        lastUser.message_id = pendingDone.value.user_message_id
      }

      // 将完整响应添加到消息列表
      if (currentResponse.value) {
        messages.value.push({
          message_id: pendingDone.value?.message?.message_id || '',
          role: 'assistant',
          content: currentResponse.value,
          interrupted: !streamDoneReceived,
          sources: sources.value,
          search_results: searchResults.value,
          tool_sources: toolSources.value,
          use_knowledge: useKnowledge.value,
          use_search: useSearch.value,
          feedback: null,
          cache_hit: !!cacheHitInfo.value?.hit,
          cache_score: cacheHitInfo.value?.score || 0,
          cache_info: cacheHitInfo.value || pendingDone.value?.message?.cache_info || {},
          capability_gap: capabilityGap.value || null,
          skill_drafts: skillDraft.value ? [skillDraft.value] : [],
          tool_drafts: toolDraft.value ? [toolDraft.value] : [],
          // Agent 中间过程快照：随答案保留为灰色可折叠区（默认收缩，可展开回看）
          agent_process: pendingDone.value?.message?.agent_process || ((agentPlan.value.length || agentTools.value.length || streamError.value)
            ? { plan: [...agentPlan.value], tools: [...agentTools.value],
                ...(streamError.value ? { error: streamError.value, status: 'error' } : {}) }
            : null),
          process_collapsed: true,
        })
      }

      currentResponse.value = ''
      generationPhase.value = ''
    }
  }

  function handleStreamEvent(data) {
    const { event } = data

    switch (event) {
      case 'cache_hit':
        cacheHitInfo.value = data.cache_info || { hit: true, score: data.score || 0 }
        break
      case 'tool_confirmation':
        mergeChatAction(messages.value, data.action)
        break
      case 'status':
        generationPhase.value = data.phase || ''
        if (data.phase === 'cache_hit') {
          cacheHitInfo.value = { hit: true, score: data.score || 0 }
        }
        if (data.phase && phaseHistory.value[phaseHistory.value.length - 1] !== data.phase) {
          phaseHistory.value.push(data.phase)
        }
        if (data.sources) {
          sources.value = data.sources
        }
        if (data.search_results) {
          searchResults.value = data.search_results
        }
        if (data.tool_calls) {
          toolCalls.value = data.tool_calls
        }
        if (Array.isArray(data.tool_sources)) toolSources.value = data.tool_sources
        if (data.attachments) {
          // 后端保存附件后回传 file_id 等信息，回填到当前用户消息，使 chip 可点击预览
          const lastUser = [...messages.value].reverse().find((m) => m.role === 'user')
          if (lastUser) {
            lastUser.message_id = data.message_id || lastUser.message_id
            lastUser.attachments = (data.attachments || []).map((a) => ({
              name: a.filename || a.name || '',
              file_id: a.file_id || '',
              kind: a.kind || '',
            }))
          }
        }
        break

      case 'token':
        currentResponse.value += data.content || ''
        break

      case 'thought':
        // 原始内部推理不进入界面或历史快照。
        break

      case 'plan':
        // Plan&Execute 步骤清单
        agentPlan.value = Array.isArray(data.steps) ? data.steps : []
        break

      case 'tool':
        handleAgentTool(data)
        break

      case 'need_search':
        // HITL：知识库无结果或计划含联网步骤（执行前授权）→ 弹卡询问是否联网搜索
        pushHitlMessage({
          type: 'need_search',
          query: data.query || '',
          message: data.message || '知识库中未检索到相关内容，是否联网搜索？',
          reason: data.reason || '',
        })
        break

      case 'clarify':
        // HITL：证据不足且意图模糊 → 弹卡追问澄清
        pushHitlMessage({
          type: 'clarify',
          question: data.question || '',
          options: Array.isArray(data.options) ? data.options : [],
        })
        break

      case 'capability_gap':
        // 能力受限（DeepSeek 式降级）：诚实说明限制，提供应用内动作建议，
        // 不再要求用户执行命令行方案
        capabilityGap.value = {
          gap_id: data.gap_id || '',
          scenario: data.scenario || '',
          scenario_label: data.scenario_label || '',
          kind: data.kind || 'limitation',
          suggestions: Array.isArray(data.suggestions) ? data.suggestions : [],
          query: data.query || '',
          message: data.message || '',
        }
        break

      case 'skill_draft':
        // Skill 沉淀草稿：不直接写库，随回答呈现配置确认卡，用户确认后系统自动导入
        skillDraft.value = data.draft || null
        break

      case 'tool_draft':
        toolDraft.value = data.draft || null
        break

      case 'error':
        toolSources.value = []
        // 结构化错误闭环：保留错误码与失败阶段，过程面板据此指明失败步骤
        streamError.value = { code: data.code || '', stage: data.stage || '', message: data.message || '' }
        // LLM 错误 — 显示错误消息并立即结束；
        // 同时清理知识来源/搜索结果：生成失败时展示检索来源没有意义
        sources.value = []
        searchResults.value = []
        break

      case 'done':
        // 保存 done 事件数据（含 message_id）；优先更新当前会话快照与摘要，
        // 全量列表后台对账（标题可能已变）
        pendingDone.value = data
        saveSnapshot(currentSessionId.value, messages.value)
        loadSessions()
        break
    }
  }

  /** 将历史问题填入输入框再编辑（需求：问题再编辑） */
  function startReask(text) {
    editingDraft.value = { text }
  }

  // ---- HITL 卡片与 Agent 过程 ----

  /** 把 HITL 卡片作为一条 assistant 消息推入列表（后端暂停时不落库，仅前端呈现） */
  function pushHitlMessage(hitl) {
    resolveHitl() // 先清除可能存在的旧卡片，避免重复
    messages.value.push({
      role: 'assistant',
      content: '',
      hitl: { ...hitl, pending: true },
      sources: [],
      search_results: [],
    })
  }

  /** 移除消息列表中所有待处理的 HITL 卡片 */
  function resolveHitl() {
    if (messages.value.some((m) => m.role === 'assistant' && m.hitl)) {
      messages.value = messages.value.filter((m) => !(m.role === 'assistant' && m.hitl))
    }
  }

  /** 记录 Agent 工具调用进度（start/end 配对） */
  function handleAgentTool(data) {
    if (data.state === 'start') {
      agentTools.value.push({
        tool: data.tool,
        call_id: data.call_id || '',
        state: 'start',
        query: (data.args && data.args.query) || '',
        index: data.index,
        total: data.total,
        purpose: data.purpose || '',
      })
    } else {
      const last = [...agentTools.value].reverse().find((x) => (data.call_id ? x.call_id === data.call_id : x.tool === data.tool) && x.state === 'start')
      const terminal = { tool: data.tool, call_id: data.call_id || '', state: 'end', summary: data.summary || '', failed: !!data.failed,
        reused: !!data.reused,
        elapsed_ms: data.elapsed_ms, execution_status: data.execution_status, validation_status: data.validation_status, requirement_status: data.requirement_status }
      // 复用步骤只有 end 事件：序号/步骤说明随终态一并记录；普通步骤 end 不带说明时不覆盖 start 已记的值
      if (data.index !== undefined && data.index !== null) terminal.index = data.index
      if (data.total !== undefined && data.total !== null) terminal.total = data.total
      if (data.purpose) terminal.purpose = data.purpose
      if (last) Object.assign(last, terminal)
      else agentTools.value.push(terminal)
    }
  }

  /** HITL「联网搜索」：开启联网后重发原问题（后端视作新一轮，会再次落库用户消息） */
  async function resendWithSearch(query) {
    if (isGenerating.value) return
    const prev = useSearch.value
    useSearch.value = true
    try {
      // 同一问题重发：命中上一轮检索快照时不重复知识检索，只补联网缺口
      await sendMessage(query || '', [], { reuseRetrieval: true })
    } finally {
      useSearch.value = prev
    }
  }

  /** 能力受限卡「开启联网搜索并重新提问」：持久开启联网开关后重发原问题，
   *  动作一旦发起即标记卡片已处理（按钮标灰不可点），并携带检索复用标记避免重复检索 */
  async function enableSearchAndResend(query, gap = null) {
    if (isGenerating.value || !query) return
    if (gap && gap.resolved) return
    useSearch.value = true
    if (gap) {
      gap.resolved = true
      gap.resolved_action = 'enable_search'
    }
    await sendMessage(query, [], { reuseRetrieval: true })
  }

  /** HITL「直接回答」：带 skip_search_prompt 重发，不再询问、用模型自身知识作答 */
  async function answerWithoutSearch(query) {
    if (isGenerating.value) return
    await sendMessage(query || '', [], { skipSearchPrompt: true })
  }

  /** HITL 澄清选项：作为新的用户消息发送 */
  async function sendClarifyOption(text) {
    if (isGenerating.value || !text) return
    await sendMessage(text)
  }

  function stopGeneration() {
    generationId++
    // 通知后端取消会话生成任务：前端 abort 仅断开 SSE，
    // 缺这一步后端会在上游重试/故障切换中继续运行（页面中断≠后端停止）
    if (currentSessionId.value) stopChatGeneration(currentSessionId.value).catch(() => {})
    if (!controller) return
    controller.abort()
    controller = null
    if (currentResponse.value || messages.value.at(-1)?.role === 'user') {
      messages.value.push({ role: 'assistant', content: `${currentResponse.value}\n\n[生成已中断，内容不完整]`, interrupted: true, sources: [...sources.value], search_results: [...searchResults.value],
        skill_drafts: skillDraft.value ? [skillDraft.value] : [], tool_drafts: toolDraft.value ? [toolDraft.value] : [],
        agent_process: { plan: [...agentPlan.value], tools: agentTools.value.map(t => t.state === 'start'
          ? { ...t, state: 'end', failed: true, execution_status: 'cancellation_requested', summary: '已请求停止；历史记录可核验终态' } : { ...t }) } })
    }
    currentResponse.value = ''
    generationPhase.value = ''
    isGenerating.value = false
    // 后端在取消后才打包 checkpoint，稍延迟拉取「是否继续」
    setTimeout(() => { refreshCheckpoints() }, 800)
  }

  /** 拉取当前会话待处理的中断任务 checkpoint（停止/切换会话后调用） */
  async function refreshCheckpoints() {
    if (!currentSessionId.value) {
      pendingCheckpoint.value = null
      return
    }
    const epoch = generationId
    try {
      const res = await listCheckpoints(currentSessionId.value)
      if (epoch !== generationId) return
      const list = res.data.checkpoints || []
      pendingCheckpoint.value = list[0] || null
    } catch (err) {
      // 静默降级：checkpoint 提示失败不影响问答主链路
    }
  }

  /** 「继续」：在前序 checkpoint 基础上续答（后端 claim + 注入前序上下文） */
  async function resumeCheckpoint() {
    const cp = pendingCheckpoint.value
    if (!cp || isGenerating.value) return
    pendingCheckpoint.value = null
    await sendMessage(cp.query, [], { resumeCheckpointId: cp.id })
  }

  /** 「不继续」：软删除 checkpoint（后端缓存保留 48h，记忆/日志留痕可检索） */
  async function abandonPendingCheckpoint() {
    const cp = pendingCheckpoint.value
    if (!cp) return
    pendingCheckpoint.value = null
    try {
      await abandonCheckpoint(cp.id)
    } catch (err) {
      console.error('放弃 checkpoint 失败:', err)
    }
  }

  function updateAction(action) {
    mergeChatAction(messages.value, action)
  }

  /** 更新 Skill 草稿状态（确认导入/取消/过期后同步卡片与本轮引用） */
  function updateSkillDraft(draft) {
    if (!draft || !draft.id) return
    if (skillDraft.value && skillDraft.value.id === draft.id) skillDraft.value = draft
    for (const m of messages.value) {
      const list = m.skill_drafts || []
      const idx = list.findIndex((d) => d.id === draft.id)
      if (idx >= 0) list.splice(idx, 1, draft)
    }
  }

  function updateToolDraft(draft) {
    if (!draft?.id) return
    if (toolDraft.value?.id === draft.id) toolDraft.value = draft
    for (const m of messages.value) {
      const idx = (m.tool_drafts || []).findIndex(d => d.id === draft.id)
      if (idx >= 0) m.tool_drafts.splice(idx, 1, draft)
    }
  }

  function clearMessages() {
    stopGeneration()
    messages.value = []
    if (currentSessionId.value) {
      clearChatHistory(currentSessionId.value).catch(() => {})
    }
  }

  /** 清空所有会话状态（用于切换游客身份时） */
  function clearAllSessionData() {
    stopGeneration()
    sessionSnapshots.clear()
    sessions.value = []
    currentSessionId.value = null
    messages.value = []
    currentResponse.value = ''
    sources.value = []
    searchResults.value = []
    generationPhase.value = ''
    phaseHistory.value = []
    toolCalls.value = []
    toolSources.value = []
    toolDraft.value = null
    skillDraft.value = null
    agentTools.value = []
    agentPlan.value = []
    pendingCheckpoint.value = null
  }

  return {
    // 状态
    sessions,
    currentSessionId,
    messages,
    isGenerating,
    currentResponse,
    sources,
    searchResults,
    generationPhase,
    phaseHistory,
    toolCalls,
    editingDraft,
    cacheHitInfo,
    skillDraft,
    toolDraft,
    toolSources,
    streamError,
    pendingCheckpoint,
    updateToolDraft,
    agentPlan,
    agentThoughts,
    agentTools,
    // 配置
    useKnowledge,
    useSearch,
    llmProvider,
    selectedModel,
    // 计算
    currentSession,
    // 方法
    loadSessions,
    newSession,
    switchToSession,
    removeSession,
    sendMessage,
    stopGeneration,
    updateAction,
    updateSkillDraft,
    startReask,
    resendWithSearch,
    enableSearchAndResend,
    answerWithoutSearch,
    sendClarifyOption,
    resolveHitl,
    clearMessages,
    clearAllSessionData,
    refreshCheckpoints,
    resumeCheckpoint,
    abandonPendingCheckpoint,
  }
})
