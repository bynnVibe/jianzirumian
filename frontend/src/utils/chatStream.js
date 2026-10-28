/** 消费聊天 SSE；仅 done 表示成功，所有出口释放 reader。 */
export async function consumeChatStream(response, onEvent, signal) {
  if (!response.ok) {
    let message = `请求失败（${response.status}）`
    try { message = (await response.json()).detail || message } catch { /* 保留状态码 */ }
    throw new Error(message)
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      if (signal?.aborted) throw new DOMException('生成已停止', 'AbortError')
      const { done, value } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      const lines = buffer.split('\n')
      buffer = done ? '' : lines.pop() || ''
      for (const raw of lines) {
        const line = raw.trim()
        if (!line.startsWith('data:')) continue
        const data = JSON.parse(line.slice(5).trim())
        onEvent(data)
        if (data.event === 'error') throw new Error(data.message || '生成失败')
        if (data.event === 'done') return data
      }
      if (done) throw new Error('连接已结束，但未收到完成信号')
    }
  } finally {
    try { await reader.cancel() } catch { /* 请求可能已经关闭 */ }
    reader.releaseLock()
  }
}

export function mergeChatAction(messages, action) {
  const message = messages.find(m => m.message_id === action.message_id) ||
    [...messages].reverse().find(m => m.role === 'user' && (m.attachments || []).some(a => a.file_id === action.file_id))
  if (!message) return
  message.message_id = action.message_id
  const actions = message.actions || []
  message.actions = [...actions.filter(a => a.id !== action.id), action]
}
