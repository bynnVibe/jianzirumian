import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { consumeChatStream, createTextPresenter, groupKnowledgeSources, mergeChatAction } from './chatStream.js'
import vue from '@vitejs/plugin-vue'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createServer } from 'vite'
import { createPinia, setActivePinia } from 'pinia'
import { fileURLToPath } from 'node:url'

const encode = text => new TextEncoder().encode(text)
function response(lines) {
  let closed = false
  const stream = new ReadableStream({
    start(controller) { lines.forEach(line => controller.enqueue(encode(line))) },
    cancel() { closed = true },
  })
  return { value: new Response(stream), closed: () => closed }
}

test('SSE 正常结束后释放 reader', async () => {
  const source = response(['data: {"event":"token","content":"你好"}\n\n', 'data: {"event":"done"}\n\n'])
  const events = []
  await consumeChatStream(source.value, event => events.push(event))
  assert.deepEqual(events.map(e => e.event), ['token', 'done'])
  assert.equal(source.closed(), true)
  assert.equal(source.value.body.locked, false)
})

test('无 done 的 EOF 不视为成功', async () => {
  await assert.rejects(consumeChatStream(new Response('data: {"event":"token","content":"部分"}\n\n'), () => {}), /完成信号/)
})

test('error 与取消均关闭资源', async () => {
  for (const cancelled of [false, true]) {
    const source = response(['data: {"event":"error","message":"超时"}\n\n'])
    const controller = new AbortController()
    if (cancelled) controller.abort()
    await assert.rejects(consumeChatStream(source.value, () => {}, controller.signal))
    assert.equal(source.closed(), true)
    assert.equal(source.value.body.locked, false)
  }
})

test('确认事件按消息关联并去重，刷新后保留状态', () => {
  const messages = [{ role: 'user', message_id: 'm', attachments: [{ file_id: 'f' }], actions: [] }]
  mergeChatAction(messages, { id: 'a', message_id: 'm', file_id: 'f', status: 'pending' })
  mergeChatAction(messages, { id: 'a', message_id: 'm', file_id: 'f', status: 'running' })
  assert.equal(messages[0].actions.length, 1)
  assert.equal(JSON.parse(JSON.stringify(messages))[0].actions[0].status, 'running')
  mergeChatAction(messages, { id: 'foreign', message_id: 'other', file_id: 'other' })
  assert.equal(messages[0].actions.length, 1)
})

const root = fileURLToPath(new URL('../../', import.meta.url))
const server = await createServer({
  configFile: false, root, envDir: fileURLToPath(new URL('../../../node_modules/.cache/no-env', import.meta.url)),
  server: { middlewareMode: true }, appType: 'custom',
  optimizeDeps: { noDiscovery: true, include: [] },
  plugins: [vue(), {
    name: 'isolated-chat-api', enforce: 'pre',
    resolveId(id) {
      if (id === '@/api') return '\0chat-test-api'
      if (id === '@/utils/chatStream') return fileURLToPath(new URL('./chatStream.js', import.meta.url))
      // ChatMessage 新增依赖的真实模块（UrlSource 及其 markdown 工具），不用全局 @ 别名以免绕过上方测试桩
      if (id === '@/components/UrlSource.vue') return fileURLToPath(new URL('../components/UrlSource.vue', import.meta.url))
      if (id === '@/utils/urlArticles') return fileURLToPath(new URL('./urlArticles.js', import.meta.url))
    },
    load(id) {
      if (id === '\0chat-test-api') return ['sendChatMessage', 'sendChatMessageWithFiles', 'createSession', 'listSessions', 'deleteSession', 'switchSession', 'getChatHistory', 'clearChatHistory', 'createBookmark', 'getImageUrl', 'getAttachmentFileUrl', 'getUrlSnapshot']
        .map(name => `export const ${name} = (...args) => globalThis.__chatTestApi.${name}(...args);`).join('\n')
    },
  }],
})
after(() => server.close())
const { useChatStore } = await server.ssrLoadModule('/src/stores/chat.js')
function store() {
  setActivePinia(createPinia())
  globalThis.__chatTestApi = {
    listSessions: async () => ({ data: { sessions: [] } }),
    createSession: async () => ({ data: { session_id: 'new' } }),
    switchSession: async () => ({}), getChatHistory: async () => ({ data: { messages: [] } }),
    clearChatHistory: async () => ({}),
  }
  return useChatStore()
}
function blockedFetch(signal, onToken) {
  return Promise.resolve(new Response(new ReadableStream({ start(controller) {
    controller.enqueue(encode('data: {"event":"token","content":"部分内容"}\n\n'))
    signal.addEventListener('abort', () => controller.error(new DOMException('停止', 'AbortError')), { once: true })
    onToken?.()
  } })))
}
const tick = () => new Promise(resolve => setTimeout(resolve, 10))

test('停止保留部分文本，新会话不被旧流污染', async () => {
  const chat = store()
  chat.currentSessionId = 'old'
  let signal
  globalThis.__chatTestApi.sendChatMessage = (_, s) => { signal = s; return blockedFetch(s) }
  const pending = chat.sendMessage('问题')
  await tick()
  assert.equal(chat.currentResponse, '部分内容')
  chat.stopGeneration()
  assert.equal(signal.aborted, true)
  assert.equal(chat.messages.at(-1).interrupted, true)
  assert.match(chat.messages.at(-1).content, /部分内容/)
  await chat.newSession()
  await pending
  assert.equal(chat.currentSessionId, 'new')
  assert.deepEqual(chat.messages, [])
  assert.equal(chat.isGenerating, false)
})

test('首次创建会话期间阻止重复发送，注销使迟到创建无效', async () => {
  const chat = store()
  let resolve, created = 0, sent = 0
  globalThis.__chatTestApi.createSession = () => { created++; return new Promise(r => { resolve = r }) }
  globalThis.__chatTestApi.sendChatMessage = () => { sent++; throw new Error('不应发起模型请求') }
  const pending = chat.sendMessage('第一个问题')
  await chat.sendMessage('重复问题')
  assert.equal(created, 1)
  chat.clearAllSessionData()
  resolve({ data: { session_id: 'late' } })
  await pending
  assert.equal(sent, 0)
  assert.equal(chat.currentSessionId, null)
  assert.equal(chat.isGenerating, false)
})

test('错误 EOF 保留部分结果且标为中断', async () => {
  const chat = store()
  chat.currentSessionId = 's'
  globalThis.__chatTestApi.sendChatMessage = async () => new Response('data: {"event":"token","content":"片段"}\n\n')
  await chat.sendMessage('问题')
  assert.equal(chat.isGenerating, false)
  assert.equal(chat.messages.at(-1).interrupted, true)
  assert.match(chat.messages.at(-1).content, /片段/)
})

test('相同文档合并、按最高相关度降序，旧引用号及页码不丢失', () => {
  const sources = [
    { index: 1, source_image: '/a.docx', relevance: 50, text: '百科', page_number: 1 },
    { index: 2, source_image: '/b.docx', relevance: 80, text: '其他' },
    { index: 8, source_image: '/a.docx', relevance: 92, text: '原文', page_number: 3 },
    { index: 9, source_image: '/a.docx', kb_id: 'other', relevance: 85, text: '另一库' },
  ]
  const before = JSON.stringify(sources)
  const groups = groupKnowledgeSources(sources)
  assert.deepEqual(groups.map(g => g.relevance), [92, 85, 80])
  assert.equal(groups[0].chunk_count, 2)
  assert.deepEqual(groups[0].citation_indices, [1, 8])
  assert.equal(groups.find(g => g.citation_indices.includes(8)).key, groups[0].key)
  assert.deepEqual(groups[0].chunks.map(c => c.page_number), [1, 3])
  assert.equal(JSON.stringify(sources), before)
  assert.equal(groupKnowledgeSources([{ index: 1, wiki_title: '同名' }, { index: 2, wiki_title: '同名' }]).length, 2)
})

test('文档级新响应保留完整片段及回填预览字段', () => {
  const [doc] = groupKnowledgeSources([{
    index: 1, document_id: 'doc1', document_title: '文档标题', relevance: 90,
    source_image: '/a.docx', source_type: 'word', pdf_preview_path: '/a.pdf',
    chunks: [{ text: '全文'.repeat(300), page_number: 3, pdf_preview_path: null }, { text: '第二页', page_number: 2 }],
  }])
  assert.equal(doc.document_title, '文档标题')
  assert.equal(doc.chunk_count, 2)
  assert.equal(doc.chunks[0].text.length, 600)
  assert.equal(doc.chunks[0].pdf_preview_path, '/a.pdf')
  assert.deepEqual(doc.citation_indices, [1])
})

test('知识来源默认收起，数量按文档统计', async () => {
  const { default: ChatMessage } = await server.ssrLoadModule('/src/components/ChatMessage.vue')
  const html = await renderToString(createSSRApp(ChatMessage, {
    role: 'assistant', content: '结论[来源 8]', sources: [
      { index: 1, source_image: '/a.pdf', relevance: 50, text: '不应默认显示的片段' },
      { index: 8, source_image: '/a.pdf', relevance: 92, text: '另一个片段' },
    ],
  }))
  assert.match(html, /知识来源 · 1 份文档/)
  assert.match(html, /2 个片段/)
  assert.match(html, /aria-expanded="false"/)
  assert.doesNotMatch(html, /不应默认显示的片段/)
  assert.match(html, /data-cite-num="8"/)
})

test('完整 token 在 done 之前交付，不等待响应结束', async () => {
  let writer
  const events = []
  const source = new Response(new ReadableStream({ start(controller) { writer = controller } }))
  const pending = consumeChatStream(source, event => events.push(event.event))
  writer.enqueue(encode('data: {"event":"token","content":"首块"}\n\n'))
  await tick()
  assert.deepEqual(events, ['token'])
  writer.enqueue(encode('data: {"event":"done"}\n\n'))
  await pending
})

test('展示队列分帧处理大块文本，保留顺序和完整 Unicode', async () => {
  const seen = []
  const presenter = createTextPresenter(text => seen.push(text))
  const text = '中'.repeat(15) + '🌿'.repeat(40)
  presenter.push(text)
  presenter.push('结尾')
  assert.ok(seen.join('').length < text.length)
  await presenter.finish()
  assert.ok(seen.length > 1)
  assert.equal(seen.join(''), text + '结尾')
  for (const part of seen) assert.equal(part.isWellFormed(), true)
})

test('done 等待展示排空再保存完整消息', async () => {
  const chat = store()
  chat.currentSessionId = 's'
  const text = '完整回答'.repeat(30)
  globalThis.__chatTestApi.sendChatMessage = async () => new Response(
    `data: ${JSON.stringify({ event: 'token', content: text })}\n\ndata: {"event":"done","message":{"message_id":"answer"}}\n\n`,
  )
  const pending = chat.sendMessage('问题')
  await tick()
  assert.equal(chat.isGenerating, true)
  assert.ok(chat.currentResponse.length > 0 && chat.currentResponse.length < text.length)
  await pending
  assert.equal(chat.messages.at(-1).content, text)
  assert.equal(chat.messages.at(-1).message_id, 'answer')
  assert.equal(chat.messages.at(-1).interrupted, false)
})

test('排队展示期间停止并切会话，旧文本不能污染新会话', async () => {
  const chat = store()
  chat.currentSessionId = 's'
  globalThis.__chatTestApi.sendChatMessage = async () => new Response(
    `data: ${JSON.stringify({ event: 'token', content: '旧会话'.repeat(200) })}\n\ndata: {"event":"done"}\n\n`,
  )
  const pending = chat.sendMessage('问题')
  await tick()
  chat.stopGeneration()
  assert.equal(chat.messages.at(-1).interrupted, true)
  await chat.newSession()
  await pending
  await new Promise(resolve => setTimeout(resolve, 60))
  assert.equal(chat.currentResponse, '')
  assert.deepEqual(chat.messages, [])
})
