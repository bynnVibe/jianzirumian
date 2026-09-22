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
  const messages = ref([]) // [{role, content, thought, tools[], edit, error, streaming}]

  // 当前界面上下文：由视图（WikiView 等）与组件（路由变化）写入
  const context = ref({ page_id: '', page_title: '', route_name: '', route_label: '' })

  // ---- 上下文管理 ----
  function setContext(patch) {
    context.value = { ...context.value, ...(patch || {}) }
  }

  /** 离开词条详情时清除页面上下文（保留路由上下文） */
  function clearPageContext() {
    context.value = { ...context.value, page_id: '', page_title: '' }
  }

  function toggle() {
    isOpen.value = !isOpen.value
  }

  function open() {
    isOpen.value = true
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
      thought: '',
      tools: [],
      edit: null,
      error: false,
      streaming: true,
    })
    const idx = messages.value.length - 1

    isGenerating.value = true
    statusText.value = '正在思考…'

    try {
      await assistantChat(
        {
          message,
          page_id: context.value.page_id || '',
          page_title: context.value.page_title || '',
          route_name: context.value.route_name || '',
          route_label: context.value.route_label || '',
          history,
        },
        (ev) => _handleEvent(messages.value[idx], ev),
      )
    } catch (e) {
      const m = messages.value[idx]
      if (m) {
        m.content = m.content || e?.message || '请求失败，请稍后重试'
        m.error = true
      }
    } finally {
      const m = messages.value[idx]
      if (m) m.streaming = false
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
      case 'thought':
        m.thought = ev.text || ''
        break
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
    // 方法
    setContext,
    clearPageContext,
    toggle,
    open,
    send,
    approveEdit,
    rejectEdit,
    reset,
  }
})
