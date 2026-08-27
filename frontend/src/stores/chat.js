import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
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
    try {
      const res = await listSessions()
      sessions.value = res.data.sessions || []
      if (res.data.current_session_id) {
        currentSessionId.value = res.data.current_session_id
      }
    } catch (err) {
      console.error('加载会话列表失败:', err)
    }
  }

  async function newSession() {
    try {
      const res = await createSession()
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
    try {
      await switchSession(sessionId)
      currentSessionId.value = sessionId
      const res = await getChatHistory(sessionId)
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

    // 如果没有会话，先创建
    if (!currentSessionId.value) {
      await newSession()
    }

    // 添加用户消息
    messages.value.push({
      role: 'user',
      content: query,
      sources: [],
      search_results: [],
      use_knowledge: useKnowledge.value,
      use_search: useSearch.value,
      attachments: (files || []).map((f) => ({ name: f.name })),
    })

    // 初始化生成状态
    isGenerating.value = true
    currentResponse.value = ''
    sources.value = []
    searchResults.value = []
    generationPhase.value = ''
    phaseHistory.value = []
    toolCalls.value = []

    let streamDoneReceived = false
    pendingDone.value = null

    try {
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
          )
        : await sendChatMessage({
            session_id: currentSessionId.value,
            query,
            use_knowledge: useKnowledge.value,
            use_search: useSearch.value,
            llm_provider: llmProvider.value,
          })

      // 处理非 200 响应（如 403 游客限制）
      if (!response.ok) {
        let errorMsg = '请求失败'
        try {
          const errData = await response.json()
          errorMsg = errData.detail || errData.message || errorMsg
        } catch {
          // ignore
        }
        currentResponse.value = errorMsg
        isGenerating.value = false
        // 将错误响应添加到消息列表
        messages.value.push({
          role: 'assistant',
          content: errorMsg,
          sources: [],
          search_results: [],
          use_knowledge: useKnowledge.value,
          use_search: useSearch.value,
        })
        currentResponse.value = ''
        return
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        if (streamDoneReceived) break
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          const jsonStr = line.slice(6).trim()
          if (!jsonStr) continue

          try {
            const data = JSON.parse(jsonStr)
            handleStreamEvent(data)
            // 收到 done 事件后标记完成，下次循环主动退出
            // 避免 reader.read() 在代理环境下永不返回 done:true
            if (data.event === 'done') {
              streamDoneReceived = true
            }
          } catch (e) {
            // 忽略解析错误
          }
        }
      }
    } catch (err) {
      console.error('发送消息失败:', err)
      currentResponse.value = '抱歉，消息发送失败，请检查网络连接后重试。'
    } finally {
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
        currentResponse.value = data.message || '生成回答时发生错误'
        sources.value = []
        searchResults.value = []
        isGenerating.value = false
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

  function clearMessages() {
    messages.value = []
    if (currentSessionId.value) {
      clearChatHistory(currentSessionId.value).catch(() => {})
    }
  }

  /** 清空所有会话状态（用于切换游客身份时） */
  function clearAllSessionData() {
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
    startReask,
    clearMessages,
    clearAllSessionData,
  }
})
