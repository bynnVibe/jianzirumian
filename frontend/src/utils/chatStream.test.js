import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { consumeChatStream, mergeChatAction } from './chatStream.js'
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
  plugins: [{
    name: 'isolated-chat-api', enforce: 'pre',
    resolveId(id) {
      if (id === '@/api') return '\0chat-test-api'
      if (id === '@/utils/chatStream') return fileURLToPath(new URL('./chatStream.js', import.meta.url))
    },
    load(id) {
      if (id === '\0chat-test-api') return ['sendChatMessage', 'sendChatMessageWithFiles', 'createSession', 'listSessions', 'deleteSession', 'switchSession', 'getChatHistory', 'clearChatHistory']
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
