import MarkdownIt from 'markdown-it'

export function safeArticleUrl(value) {
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : ''
  } catch { return '' }
}

const markdown = new MarkdownIt({ html: false, linkify: false, breaks: true })
markdown.validateLink = (url) => !!safeArticleUrl(url)
markdown.renderer.rules.image = (tokens, index) => {
  const label = markdown.utils.escapeHtml(tokens[index].content || '图片')
  const url = safeArticleUrl(tokens[index].attrGet('src'))
  return url ? `<a href="${markdown.utils.escapeHtml(url)}" target="_blank" rel="noopener noreferrer">[图片] ${label}</a>` : `[图片] ${label}`
}
markdown.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
  tokens[index].attrSet('target', '_blank')
  tokens[index].attrSet('rel', 'noopener noreferrer')
  return renderer.renderToken(tokens, index, options)
}
export function renderArticle(text) { return markdown.render(String(text || '')) }
export function urlStatusLabel(item) {
  if (item.status === 'error') return item.phase === 'ingest' ? '入库失败，需核对' : '解析失败，可补充正文'
  return { pending: '排队中', parsing: '解析中', done: '待确认', ingesting: '入库中', succeeded: '已入库', interrupted: '执行中断，需核对' }[item.status] || item.status
}
export function urlDraftFingerprint(title, text) { return JSON.stringify([title || '', text || '']) }
