import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { consumeChatStream, mergeChatAction } from '@/utils/chatStream'
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
  const generationPhase = ref('') // tool_start | rewriting | retrieving | reranking | gap_checking | rechecking | retrieved | not_found | searching | searched | generating | citations_checked
  const phaseHistory = ref([]) // 本次对话经历过的阶段序列（供流水线进度条标记已完成步骤）
  const toolCalls = ref([]) // 本次对话附件工具调用结果 [{tool_name, filename, success, chunk_count}, ...]
  const editingDraft = ref(null) // 再编辑草稿 { text }，由 ChatInput 消费
  let controller = null
  let generationId = 0
  const pendingDone = ref(null) // 最近一次 done 事件数据（含 message_id）

  // ---- 配置 ----
  const useKnowledge = ref(true)
  const useSearch = ref(false)
  const llmProvider = ref('')

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

  async function sendMessage(query, files = []) {
    if (isGenerating.value || (!query.trim() && !(files && files.length))) return

    // 初始化生成状态
    isGenerating.value = true
    currentResponse.value = ''
    sources.value = []
    searchResults.value = []
    generationPhase.value = ''
    phaseHistory.value = []
    toolCalls.value = []

    let streamDoneReceived = false
    const requestId = ++generationId
    const requestController = new AbortController()
    controller = requestController
    pendingDone.value = null

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
          }, requestController.signal)

      await consumeChatStream(response, data => {
        if (requestId === generationId) handleStreamEvent(data)
      }, requestController.signal)
      streamDoneReceived = true
    } catch (err) {
      if (requestId === generationId) {
        const reason = err.name === 'AbortError' ? '已停止生成' : err.message || '网络连接中断'
        currentResponse.value += `\n\n[生成已中断，内容不完整：${reason}]`
        sources.value = []
        searchResults.value = []
      }
    } finally {
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
    // 配置
    useKnowledge,
    useSearch,
    llmProvider,
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
    clearMessages,
    clearAllSessionData,
  }
})
