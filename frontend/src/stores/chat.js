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
  const generationPhase = ref('') // tool_start | ocr_retrieval | rewriting | retrieving | reranking | gap_checking | rechecking | retrieved | not_found | searching | searched | generating | citations_checked
  const phaseHistory = ref([]) // 本次对话经历过的阶段序列（供流水线进度条标记已完成步骤）
  const toolCalls = ref([]) // 本次对话附件工具调用结果 [{tool_name, filename, success, chunk_count}, ...]
  const editingDraft = ref(null) // 再编辑草稿 { text }，由 ChatInput 消费
  let controller = null
  let generationId = 0
  const pendingDone = ref(null) // 最近一次 done 事件数据（含 message_id）
  const cacheHitInfo = ref(null) // 本轮是否命中语义缓存 { score }（命中则绕过检索路由直回）
  const capabilityGap = ref(null) // 本轮能力缺口（需求5）：候选 CLI 方案随回答附带，由用户 HITL 选择执行

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
      return session
    } catch (err) {
      console.error('创建会话失败:', err)
      return null
    }
  }

  async function switchToSession(sessionId) {
    stopGeneration()
    const epoch = generationId
    currentSessionId.value = sessionId
    messages.value = []
    try {
      await switchSession(sessionId)
      if (epoch !== generationId) return
      const res = await getChatHistory(sessionId)
      if (epoch !== generationId) return
      messages.value = (res.data.messages || []).map(normalizeMessage)
    } catch (err) {
      console.error('切换会话失败:', err)
    }
  }

  /** 统一消息结构：后端附件元数据 {file_id, filename, kind} → 前端 chip {name, file_id, kind} */
  function normalizeMessage(m) {
    return {
      ...m,
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
        currentResponse.value += `\n\n[生成已中断，内容不完整：${reason}]`
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
          use_knowledge: useKnowledge.value,
          use_search: useSearch.value,
          feedback: null,
          cache_hit: !!cacheHitInfo.value,
          cache_score: cacheHitInfo.value?.score || 0,
          capability_gap: capabilityGap.value || null,
        })
      }

      currentResponse.value = ''
      generationPhase.value = ''
    }
  }

  function handleStreamEvent(data) {
    const { event } = data

    switch (event) {
      case 'tool_confirmation':
        mergeChatAction(messages.value, data.action)
        break
      case 'status':
        generationPhase.value = data.phase || ''
        if (data.phase === 'cache_hit') {
          cacheHitInfo.value = { score: data.score || 0 }
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
        // ReAct 思考链：累积展示 Agent 的推理过程
        if (data.text) agentThoughts.value.push(data.text)
        break

      case 'plan':
        // Plan&Execute 步骤清单
        agentPlan.value = Array.isArray(data.steps) ? data.steps : []
        break

      case 'tool':
        handleAgentTool(data)
        break

      case 'need_search':
        // HITL：知识库无结果、未开联网 → 弹卡询问是否联网搜索
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
        // 能力缺口（需求5）：不暂停回答，方案卡片随本轮回答一起呈现，
        // 由用户选择 CLI 方案后调 /capability-gap/execute 在沙箱执行
        capabilityGap.value = {
          gap_id: data.gap_id || '',
          scenario: data.scenario || '',
          scenario_label: data.scenario_label || '',
          solutions: Array.isArray(data.solutions) ? data.solutions : [],
          query: data.query || '',
          message: data.message || '',
        }
        break

      case 'error':
        // LLM 错误 — 显示错误消息并立即结束；
        // 同时清理知识来源/搜索结果：生成失败时展示检索来源没有意义
        sources.value = []
        searchResults.value = []
        break

      case 'done':
        // 保存 done 事件数据（含 message_id），并更新会话列表（标题可能已变）
        pendingDone.value = data
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
        state: 'start',
        query: (data.args && data.args.query) || '',
        index: data.index,
        total: data.total,
        purpose: data.purpose || '',
      })
    } else {
      const last = [...agentTools.value].reverse().find((x) => x.tool === data.tool && x.state === 'start')
      if (last) {
        last.state = 'end'
        last.summary = data.summary || ''
        last.failed = !!data.failed
      }
    }
  }

  /** HITL「联网搜索」：开启联网后重发原问题（后端视作新一轮，会再次落库用户消息） */
  async function resendWithSearch(query) {
    if (isGenerating.value) return
    const prev = useSearch.value
    useSearch.value = true
    try {
      await sendMessage(query || '')
    } finally {
      useSearch.value = prev
    }
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
    if (!controller) return
    controller.abort()
    controller = null
    if (currentResponse.value || messages.value.at(-1)?.role === 'user') {
      messages.value.push({ role: 'assistant', content: `${currentResponse.value}\n\n[生成已中断，内容不完整]`, interrupted: true, sources: [], search_results: [] })
    }
    currentResponse.value = ''
    generationPhase.value = ''
    isGenerating.value = false
  }

  function updateAction(action) {
    mergeChatAction(messages.value, action)
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
    sessions.value = []
    currentSessionId.value = null
    messages.value = []
    currentResponse.value = ''
    sources.value = []
    searchResults.value = []
    generationPhase.value = ''
    phaseHistory.value = []
    toolCalls.value = []
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
    startReask,
    resendWithSearch,
    answerWithoutSearch,
    sendClarifyOption,
    resolveHitl,
    clearMessages,
    clearAllSessionData,
  }
})
