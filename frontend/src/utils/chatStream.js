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

/** 网络读取与展示解耦：小块即时交付，大块分帧；取消后不再更新界面。 */
export function createTextPresenter(onText, signal) {
  let pending = ''
  let timer = null
  let stopped = false
  let ending = false
  const waiters = []
  function settle() {
    if (timer !== null) clearTimeout(timer)
    timer = null
    signal?.removeEventListener('abort', cancel)
    waiters.splice(0).forEach(resolve => resolve())
  }
  function tick() {
    timer = null
    if (stopped) return
    if (pending) {
      let size = Math.max(16, Math.ceil(pending.length / 30))
      // 不在 UTF-16 代理对中间切开表情等字符。
      const code = pending.charCodeAt(size - 1)
      if (code >= 0xD800 && code <= 0xDBFF) size++
      const text = pending.slice(0, size)
      pending = pending.slice(size)
      onText(text)
    }
    if (ending && !pending) {
      stopped = true
      settle()
    } else {
      timer = setTimeout(() => {
        timer = null
        if (pending || ending) tick()
      }, 24)
    }
  }
  function cancel() {
    stopped = true
    pending = ''
    settle()
  }
  signal?.addEventListener('abort', cancel, { once: true })
  if (signal?.aborted) cancel()
  return {
    push(text) {
      if (stopped || ending || !text) return
      pending += text
      if (timer === null) tick()
    },
    finish() {
      if (stopped) return Promise.resolve()
      ending = true
      if (!pending) {
        stopped = true
        settle()
        return Promise.resolve()
      }
      return new Promise(resolve => {
        waiters.push(resolve)
        if (timer === null) tick()
      })
    },
    cancel,
  }
}

/** 新来源与旧会话统一为文档组；保留原引用号映射，不改写历史正文。 */
export function groupKnowledgeSources(sources = []) {
  const groups = new Map()
  const confidence = source => {
    if (source.relevance != null) return Number(source.relevance)
    const score = Number(source.score || 0)
    return score >= 0 && score <= 1 ? Math.round(score * 100) : Math.round(100 / (1 + Math.exp(-score)))
  }
  sources.forEach((source, position) => {
    const identity = source.source_image ? ['path', source.source_image]
      : source.document_id ? ['document', source.document_id] : ['unknown', position]
    const key = JSON.stringify([source.kb_id || '', ...identity])
    const number = Number(source.index)
    const relevance = confidence(source)
    if (!groups.has(key)) {
      groups.set(key, { ...source, key, relevance, citation_indices: [], chunks: [] })
    }
    const group = groups.get(key)
    group.citation_indices.push(number)
    if (relevance > group.relevance || (relevance === group.relevance && source.score > group.score)) {
      group.relevance = relevance
      group.score = source.score
    }
    if (!group.document_title && source.document_title) group.document_title = source.document_title
    const chunks = source.chunks?.length ? source.chunks : [source]
    group.chunks.push(...chunks.map(chunk => ({
      ...source, ...chunk, chunks: undefined, index: number,
      source_image: chunk.source_image || source.source_image,
      source_type: chunk.source_type || source.source_type,
      pdf_preview_path: chunk.pdf_preview_path || source.pdf_preview_path,
    })))
  })
  return [...groups.values()].sort((a, b) => b.relevance - a.relevance || (b.score || 0) - (a.score || 0)).map(group => ({
    ...group,
    chunk_count: group.chunks.length,
    document_title: group.document_title || (group.source_image || '').split('/').pop() || group.wiki_title || '未知文档',
  }))
}

export function mergeChatAction(messages, action) {
  const message = messages.find(m => m.message_id === action.message_id) ||
    [...messages].reverse().find(m => m.role === 'user' && (m.attachments || []).some(a => a.file_id === action.file_id))
  if (!message) return
  message.message_id = action.message_id
  const actions = message.actions || []
  message.actions = [...actions.filter(a => a.id !== action.id), action]
}
