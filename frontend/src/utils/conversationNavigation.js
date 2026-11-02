// 当前会话内的稳定锚点；旧消息的序号仅在本会话中使用。
export function messageAnchor(sessionId, message, index) {
  return `turn-${encodeURIComponent(sessionId || 'new')}-${encodeURIComponent(message.message_id || `legacy-${index}`)}`
}

export function conversationEntries(messages, sessionId) {
  return messages.flatMap((message, index) => {
    if (message.role !== 'user') return []
    const text = String(message.content || '')
      .replace(/<[^>]*>/g, '').replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
      .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1').replace(/[#*_`>~]/g, '')
      .replace(/\s+/g, ' ').trim()
    const attachments = (message.attachments || []).map((file) => file.filename).filter(Boolean).join('、')
    return [{ id: messageAnchor(sessionId, message, index), title: (text || attachments || '附件提问').slice(0, 100) }]
  })
}

export function nearBottom(element, threshold = 100) {
  return !element || element.scrollHeight - element.clientHeight - element.scrollTop <= threshold
}

export function activeEntry(entries, positions, viewportTop) {
  let active = entries[0]?.id || ''
  for (let i = 0; i < entries.length; i++) {
    if (positions[i] <= viewportTop + 80) active = entries[i].id
    else break
  }
  return active
}

export function sampledEntries(entries, limit = 32) {
  if (entries.length <= limit) return entries
  return Array.from({ length: limit }, (_, i) => entries[Math.round(i * (entries.length - 1) / (limit - 1))])
}
