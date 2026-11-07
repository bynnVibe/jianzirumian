import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  assistantChat,
  approveAssistantEdit,
  rejectAssistantEdit,
  listAssistantSessions,
  getAssistantSessionMessages,
  deleteAssistantSession,
} from '@/api'

/**
 * 知识助手 Agent 状态（右侧栏问答）。
 *
 * 维护面板折叠态、当前界面上下文（页面/路由）、对话消息流、待确认编辑与历史会话。
 * 消息通过 SSE 增量构建：session/status/thought/tool/clarify/token/confirm/done/error
 * 事件分别驱动会话归回、状态行、思考、工具进度、追问卡片、答案正文与编辑确认卡片。
 *
 * 后台续跑：SSE 与 messages 均在 app 级 store，应用内导航不中断；只要不调 stop()/reset()，
 * 切回页面即见实时流（isGenerating 为真时面板入口显示“进行中”徽标）。
 */
export const useAssistantStore = defineStore('assistant', () => {
  // ---- 状态 ----
  const isOpen = ref(false) // 面板折叠态（默认收起，仅显示悬浮入口）
  const isGenerating = ref(false)
  const statusText = ref('')
  const messages = ref([]) // [{role, content, thoughts[], tools[], clarify, edit, error, stopped, streaming}]

  // ---- 历史会话 ----
  const sessions = ref([]) // [{id, title, created_at, updated_at}]
  const currentSessionId = ref('') // 当前活动会话 id（空=待后端首轮回传）
  const isViewingHistory = ref(false) // 是否正在回看只读历史会话（非当前流式会话）
  const historyDrawerOpen = ref(false)
  const historyMessages = ref([]) // 回看只读历史会话的消息（与活动 messages 分离，不影响在途流）
  const viewingSessionId = ref('') // 正在回看的历史会话 id
  const historyLoading = ref(false)

  // 当前界面上下文：由视图（WikiView 等）与组件（路由变化）写入
  // open_doc_* 为用户在页面中打开/预览的知识文档（模态预览），随请求注入助手上下文
  // page_state 为视图写入的"当前界面实时快照"（如知识库管理页展示的文档清单/筛选/统计），
  // 使助手无需重新检索即可感知用户此刻屏幕上正在看的内容
  const context = ref({
    page_id: '',
    page_title: '',
    route_name: '',
    route_label: '',
    open_doc_title: '',
    open_doc_content: '',
    page_state: null,
  })

  // 用户最近的行文/浏览动作轨迹（打开文档、检索、翻页等），随请求提供前后文
  const actionTrail = ref([])
  const MAX_ACTIONS = 8

  // 当前请求的中断控制器（用于手动停止）
  let abortController = null

  // ---- 上下文管理 ----
  function setContext(patch) {
    context.value = { ...context.value, ...(patch || {}) }
  }

  /** 离开词条详情时清除页面上下文（保留路由上下文） */
  function clearPageContext() {
    context.value = { ...context.value, page_id: '', page_title: '' }
  }

  /**
   * 写入"当前界面实时快照"：由具体视图（如知识库管理页）在展示内容变化时调用，
   * 携带此刻屏幕上正在展示的文档清单、筛选条件、统计等结构化信息，随请求注入助手上下文。
   * state 为 null/空对象时视为清除。
   */
  function setPageState(state) {
    context.value = { ...context.value, page_state: state && Object.keys(state).length ? state : null }
  }

  /** 清除界面实时快照（离开对应视图/标签时调用） */
  function clearPageState() {
    context.value = { ...context.value, page_state: null }
  }

  /** 记录一条行文/浏览动作（超出上限丢弃最旧的） */
  function pushAction(text) {
    const t = (text || '').trim()
    if (!t) return
    actionTrail.value = [...actionTrail.value, t].slice(-MAX_ACTIONS)
  }

  /**
   * 用户在页面中打开/预览一篇知识文档时调用：注入文档标题与正文，
   * 并记录一条动作轨迹，使助手能结合"正在看的内容 + 前后行文动作"作答。
   */
  function setOpenDoc({ title = '', content = '' } = {}) {
    const t = (title || '').trim()
    setContext({ open_doc_title: t, open_doc_content: content || '' })
    if (t) pushAction(`打开知识文档《${t}》`)
  }

  /** 关闭文档预览时清除文档上下文 */
  function clearOpenDoc() {
    const t = context.value.open_doc_title
    setContext({ open_doc_title: '', open_doc_content: '' })
    if (t) pushAction(`关闭知识文档《${t}》`)
  }

  function toggle() {
    isOpen.value = !isOpen.value
  }

  function open() {
    isOpen.value = true
  }

  /** 手动停止当前请求：中断 SSE 流并标记最后一条助手消息 */
  function stop() {
    if (!isGenerating.value) return
    if (abortController) {
      abortController.abort()
      abortController = null
    }
    const last = messages.value[messages.value.length - 1]
    if (last && last.role === 'assistant' && last.streaming) {
      last.streaming = false
      last.stopped = true
      if (!last.content) last.content = '（已停止生成）'
    }
    isGenerating.value = false
    statusText.value = ''
  }

  // ---- 对话 ----
  // mentions：用户在本条消息中通过 @ 引用的当前界面文档/片段（[{title}]），随请求做联合分析
  async function send(text, mentions) {
    const message = (text || '').trim()
    if (!message || isGenerating.value) return
    const refs = Array.isArray(mentions) ? mentions.filter((x) => x && x.title) : []

    // 多轮上下文：仅取已完成的 user/assistant 文本，最多最近 6 条
    const history = messages.value
      .filter((m) => (m.role === 'user' || m.role === 'assistant') && m.content && !m.streaming)
      .map((m) => ({ role: m.role, content: m.content }))
      .slice(-6)

    messages.value.push({ role: 'user', content: message })
    messages.value.push({
      role: 'assistant',
      content: '',
      thoughts: [],
      tools: [],
      clarify: null,
      edit: null,
      error: false,
      stopped: false,
      streaming: true,
    })
    const idx = messages.value.length - 1

    isGenerating.value = true
    isViewingHistory.value = false // 发问即回到活动会话
    statusText.value = '正在思考…'
    abortController = new AbortController()
    pushAction(`向知识助手提问：${message.slice(0, 40)}`)

    try {
      await assistantChat(
        {
          message,
          page_id: context.value.page_id || '',
          page_title: context.value.page_title || '',
          route_name: context.value.route_name || '',
          route_label: context.value.route_label || '',
          open_doc_title: context.value.open_doc_title || '',
          open_doc_content: context.value.open_doc_content || '',
          page_state: context.value.page_state || null,
          mentions: refs,
          user_actions: actionTrail.value.slice(-MAX_ACTIONS),
          history,
          session_id: currentSessionId.value || '',
        },
        (ev) => _handleEvent(messages.value[idx], ev),
        abortController.signal,
      )
    } catch (e) {
      const m = messages.value[idx]
      if (e?.name === 'AbortError') {
        // 用户主动停止：不视为错误
        if (m) {
          m.stopped = true
          if (!m.content) m.content = '（已停止生成）'
        }
      } else if (m) {
        m.content = m.content || e?.message || '请求失败，请稍后重试'
        m.error = true
      }
    } finally {
      const m = messages.value[idx]
      if (m) m.streaming = false
      abortController = null
      isGenerating.value = false
      statusText.value = ''
      // 本轮结束后刷新会话列表（新会话/标题/时间可能变化；失败静默）
      loadSessions().catch(() => {})
    }
  }

  function _handleEvent(m, ev) {
    if (!m) return
    switch (ev.event) {
      case 'session':
        // 后端首轮回传会话 id：记录为当前活动会话
        if (ev.session_id) currentSessionId.value = ev.session_id
        break
      case 'status':
        statusText.value = ev.text || ''
        break
      case 'thought': {
        // 累积每一步思考，完整呈现 Agent 的推理过程
        const t = (ev.text || '').trim()
        if (t) m.thoughts.push(t)
        break
      }
      case 'tool':
        if (ev.state === 'start') {
          m.tools.push({ tool: ev.tool, args: ev.args || {}, state: 'running', summary: '' })
        } else {
          const t = m.tools[m.tools.length - 1]
          if (t) {
            t.state = 'done'
            t.summary = ev.summary || ''
          }
        }
        break
      case 'clarify':
        // 知识缺口/意图不明：助手追问用户，前端渲染可点选的下一步动作
        m.clarify = { question: ev.question || '', options: ev.options || [] }
        break
      case 'token':
        m.content += ev.content || ''
        break
      case 'confirm':
        m.edit = ev.edit || null
        break
      case 'error':
        m.content = m.content || ev.message || '生成回答时发生错误'
        m.error = true
        break
      case 'done':
        m.streaming = false
        break
    }
  }


  // ---- 编辑确认 ----
  function _markEdit(editId, status) {
    for (const m of messages.value) {
      if (m.edit && m.edit.id === editId) m.edit.status = status
    }
  }

  async function approveEdit(editId) {
    const res = await approveAssistantEdit(editId)
    _markEdit(editId, 'approved')
    // 通知页面（如 WikiView）刷新受影响词条
    window.dispatchEvent(new CustomEvent('wiki:page-updated', { detail: { editId } }))
    return res
  }

  async function rejectEdit(editId) {
    const res = await rejectAssistantEdit(editId)
    _markEdit(editId, 'rejected')
    return res
  }

  function reset() {
    if (abortController) {
      abortController.abort()
      abortController = null
    }
    messages.value = []
    isGenerating.value = false
    statusText.value = ''
    // 开启全新会话（历史抽屉保留，仅清当前活动对话）
    currentSessionId.value = ''
    isViewingHistory.value = false
    historyMessages.value = []
    viewingSessionId.value = ''
  }

  // ---- 历史会话管理 ----

  /** 拉取当前用户的历史会话列表（倒序） */
  async function loadSessions() {
    const res = await listAssistantSessions()
    sessions.value = res?.sessions || []
    return sessions.value
  }

  /**
   * 打开一个历史会话（只读回放）：拉取消息并映射为前端消息结构（含思考/工具/追问/编辑卡）。
   * 若打开的正是当前活动会话，则直接回到实时视图（保留在途流）。
   */
  async function openSession(id) {
    if (!id) return
    if (id === currentSessionId.value && messages.value.length) {
      isViewingHistory.value = false
      viewingSessionId.value = ''
      historyDrawerOpen.value = false
      return
    }
    historyLoading.value = true
    try {
      const res = await getAssistantSessionMessages(id)
      historyMessages.value = (res?.messages || []).map(_replayMessage)
      viewingSessionId.value = id
      isViewingHistory.value = true
      historyDrawerOpen.value = false
    } finally {
      historyLoading.value = false
    }
  }

  /** 后端历史消息 → 前端消息结构（回放 meta 中的思考/工具/追问/编辑） */
  function _replayMessage(msg) {
    const meta = msg.meta || {}
    const base = {
      role: msg.role === 'assistant' ? 'assistant' : 'user',
      content: msg.content || '',
      thoughts: [],
      tools: [],
      clarify: null,
      edit: null,
      error: false,
      stopped: false,
      streaming: false,
      _collapse: true, // 回放时思考过程默认折叠
    }
    if (base.role !== 'assistant') return base
    if (Array.isArray(meta.thoughts)) base.thoughts = meta.thoughts.filter(Boolean)
    if (Array.isArray(meta.tools)) {
      base.tools = meta.tools.map((t) => ({
        tool: t.tool || '',
        args: t.args || {},
        state: 'done',
        summary: t.summary || '',
      }))
    }
    if (meta.clarify) base.clarify = { question: meta.clarify.question || '', options: meta.clarify.options || [] }
    if (meta.edit) base.edit = { ...meta.edit, status: meta.edit.status || 'pending' }
    if (meta.error) base.error = true
    return base
  }

  /** 新建会话（清空当前活动对话，回到空白实时视图；不影响后台已有历史） */
  function newSession() {
    if (isGenerating.value) return // 在途生成中不新建，避免中断
    messages.value = []
    currentSessionId.value = ''
    isViewingHistory.value = false
    historyMessages.value = []
    viewingSessionId.value = ''
    historyDrawerOpen.value = false
  }

  /** 从只读历史回到当前活动会话的实时视图 */
  function backToLive() {
    isViewingHistory.value = false
    viewingSessionId.value = ''
  }

  /** 删除一个历史会话 */
  async function removeSession(id) {
    await deleteAssistantSession(id)
    sessions.value = sessions.value.filter((s) => s.id !== id)
    // 删除的正是当前活动会话 → 清空实时视图
    if (id === currentSessionId.value) {
      messages.value = []
      currentSessionId.value = ''
    }
    if (id === viewingSessionId.value) {
      isViewingHistory.value = false
      historyMessages.value = []
      viewingSessionId.value = ''
    }
  }

  function toggleHistoryDrawer() {
    historyDrawerOpen.value = !historyDrawerOpen.value
    if (historyDrawerOpen.value) loadSessions().catch(() => {})
  }

  return {
    // 状态
    isOpen,
    isGenerating,
    statusText,
    messages,
    context,
    actionTrail,
    sessions,
    currentSessionId,
    isViewingHistory,
    historyDrawerOpen,
    historyMessages,
    viewingSessionId,
    historyLoading,
    // 方法
    setContext,
    clearPageContext,
    setPageState,
    clearPageState,
    pushAction,
    setOpenDoc,
    clearOpenDoc,
    toggle,
    open,
    stop,
    send,
    approveEdit,
    rejectEdit,
    reset,
    loadSessions,
    openSession,
    newSession,
    backToLive,
    removeSession,
    toggleHistoryDrawer,
  }
})

