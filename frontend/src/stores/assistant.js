import { defineStore } from 'pinia'
import { ref } from 'vue'
import { assistantChat, approveAssistantEdit, rejectAssistantEdit } from '@/api'

/**
 * 知识助手 Agent 状态（右侧栏问答）。
 *
 * 维护面板折叠态、当前界面上下文（页面/路由）、对话消息流与待确认编辑。
 * 消息通过 SSE 增量构建：status/thought/tool/token/confirm/done/error 事件
 * 分别驱动状态行、思考、工具进度、答案正文与编辑确认卡片。
 */
export const useAssistantStore = defineStore('assistant', () => {
  // ---- 状态 ----
  const isOpen = ref(false) // 面板折叠态（默认收起，仅显示悬浮入口）
  const isGenerating = ref(false)
  const statusText = ref('')
  const messages = ref([]) // [{role, content, thoughts[], tools[], edit, error, stopped, streaming}]

  // 当前界面上下文：由视图（WikiView 等）与组件（路由变化）写入
  // open_doc_* 为用户在页面中打开/预览的知识文档（模态预览），随请求注入助手上下文
  const context = ref({
    page_id: '',
    page_title: '',
    route_name: '',
    route_label: '',
    open_doc_title: '',
    open_doc_content: '',
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
  async function send(text) {
    const message = (text || '').trim()
    if (!message || isGenerating.value) return

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
      edit: null,
      error: false,
      stopped: false,
      streaming: true,
    })
    const idx = messages.value.length - 1

    isGenerating.value = true
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
          user_actions: actionTrail.value.slice(-MAX_ACTIONS),
          history,
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
    }
  }

  function _handleEvent(m, ev) {
    if (!m) return
    switch (ev.event) {
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
  }

  return {
    // 状态
    isOpen,
    isGenerating,
    statusText,
    messages,
    context,
    actionTrail,
    // 方法
    setContext,
    clearPageContext,
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
  }
})

