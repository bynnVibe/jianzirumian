import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { parse, compileScript } from '@vue/compiler-sfc'
import * as Vue from 'vue'
import { renderToString } from '@vue/server-renderer'

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')
const observation = read('../views/ObservabilityView.vue')
const business = {
  days: 7, retention_days: 30,
  overview: { total: 4, completed: 3, succeeded: 2, failed: 1, processing: 1, cancelled: 0, completion_rate: 2 / 3, avg_response_seconds: 2.5 },
  daily: [{ day: '2026-10-30', total: 4, succeeded: 2, failed: 1 }],
  by_activity: [{ activity: '智能问答', total: 4, succeeded: 2, completion_rate: 2 / 3, avg_response_seconds: 2.5 }],
}

// 编译真实组件，以模拟 API 和挂载钩子执行逻辑，再用 Vue 渲染；不依赖浏览器或网络。
async function observationHarness(canOps = false, fail = false) {
  const calls = []
  let mounted
  const data = {
    getMyObservabilityMetrics: business,
    getObservabilityMetrics: { overview: { total: 0, success_rate: 0, prompt_tokens: 0, completion_tokens: 0 }, daily: [], by_agent: [], by_tool: [] },
    getObservabilityTraces: { items: [], total: 0 },
    getObservabilityTrace: {}, getObservabilityTraceLogs: {},
  }
  const api = Object.fromEntries(Object.entries(data).map(([name, value]) => [name, async (...args) => {
    calls.push([name, ...args])
    if (fail) throw new Error('private-tool-agent-error')
    return { data: value }
  }]))
  const { descriptor, errors } = parse(observation)
  assert.deepEqual(errors, [])
  const compiled = compileScript(descriptor, { id: 'obs-test', inlineTemplate: true, genDefaultAs: '__component' })
  const code = compiled.content.replace(/import\s+\{([^}]+)\}\s+from\s+['"]([^'"]+)['"];?/g, (_, names, source) => {
    return `const { ${names.replace(/\bas\b/g, ':')} } = modules[${JSON.stringify(source)}];`
  })
  const component = new Function('modules', `${code}\nreturn __component;`)({
    vue: { ...Vue, onMounted: (fn) => { mounted = fn } },
    '@/stores/auth': { useAuthStore: () => ({ canOps }) }, '@/api': api,
  })
  const render = component.setup({}, { expose() {} })
  await mounted()
  return {
    calls,
    html: () => renderToString(Vue.createSSRApp({ render })),
    nodes: () => render({}, []),
  }
}

function findNode(node, predicate) {
  if (!node || typeof node !== 'object') return undefined
  if (predicate(node)) return node
  for (const child of Array.isArray(node.children) ? node.children : []) {
    const match = findNode(child, predicate)
    if (match) return match
  }
}

test('普通用户只请求个人指标，渲染结果不包含技术视图', async () => {
  const view = await observationHarness()
  assert.deepEqual(view.calls, [['getMyObservabilityMetrics', 7]])
  const html = await view.html()
  for (const text of ['我的使用概览', '使用次数', '服务完成率', '66.7%', '2.5 秒', '智能问答']) assert.ok(html.includes(text))
  assert.doesNotMatch(html, /Agent|Token|工具调用|请求追踪|LLM|PROMPT|RESPONSE/)
  const refresh = findNode(view.nodes(), (node) => node.type === 'button' && typeof node.children === 'string' && node.children.trim() === '刷新')
  assert.ok(refresh)
  await refresh.props.onClick()
  assert.deepEqual(view.calls, [['getMyObservabilityMetrics', 7], ['getMyObservabilityMetrics', 7]])
})

test('管理员和运维分支保留全站指标与追踪入口', async () => {
  const view = await observationHarness(true)
  assert.deepEqual(view.calls.map(([name]) => name), ['getObservabilityMetrics', 'getObservabilityTraces'])
  const html = await view.html()
  assert.match(html, /Agent 可观测性/)
  assert.match(html, /请求追踪/)
  assert.match(html, /Token 消耗/)
  assert.doesNotMatch(html, /我的使用概览|业务功能使用情况/)
})

test('普通用户加载失败不显示技术错误原文', async () => {
  const view = await observationHarness(false, true)
  const html = await view.html()
  assert.match(html, /个人使用概览加载失败，请稍后刷新重试/)
  assert.doesNotMatch(html, /private-tool-agent-error/)
})

test('普通用户切换到近30天仍只调用个人统计接口', async () => {
  const view = await observationHarness()
  const button = findNode(view.nodes(), (node) => node.type === 'button' && node.children === '近30天')
  assert.ok(button)
  button.props.onClick()
  await new Promise((resolve) => setImmediate(resolve))
  assert.deepEqual(view.calls, [['getMyObservabilityMetrics', 7], ['getMyObservabilityMetrics', 30]])
})

test('个人路由按角色分流且不放宽管理控制台权限', async () => {
  let guard, routes, auth
  const source = read('../router/index.js').replace(/^import .*$/gm, '').replace('export default router', '')
  new Function('createRouter', 'createWebHistory', 'useAuthStore', 'AdminConsoleLayout', 'localStorage', source)(
    (options) => { routes = options.routes; return { beforeEach: (fn) => { guard = fn } } },
    () => ({}), () => auth, {}, { getItem: () => null },
  )
  const personal = routes.find((route) => route.path === '/observability')
  const consoleRoute = routes.find((route) => route.path === '/admin')
  const technical = consoleRoute.children.find((route) => route.name === 'Observability')
  assert.equal(personal.name, 'MyObservability')
  assert.equal(personal.meta.requiresAuth, true)
  assert.equal(personal.meta.allowGuest, undefined)
  for (const role of ['user', 'admin', 'ops', 'guest']) {
    auth = { isAuthenticated: role !== 'guest', isGuest: role === 'guest', isOps: role === 'ops', isAdmin: role === 'admin', canOps: ['admin', 'ops'].includes(role) }
    let target
    await guard({ ...personal, fullPath: personal.path }, {}, (value) => { target = value })
    assert.equal(target?.name, role === 'guest' ? 'Login' : role === 'user' ? undefined : 'Observability')
    await guard({ ...technical, meta: { ...consoleRoute.meta, ...technical.meta }, fullPath: '/admin/observability' }, {}, (value) => { target = value })
    assert.equal(target?.name, role === 'guest' ? 'Login' : role === 'user' ? 'Chat' : undefined)
  }
})

test('登录页帮助入口共用不换行容器，样例在新标签打开', () => {
  const { descriptor, errors } = parse(read('../views/LoginView.vue'))
  assert.deepEqual(errors, [])
  const nav = descriptor.template.content.match(/<nav class="auth-help-links"[\s\S]*?<\/nav>/)?.[0]
  assert.ok(nav)
  assert.match(nav, /to="\/guide"/)
  assert.match(nav, /href="\/系统样例.html" target="_blank" rel="noopener"/)
  assert.equal((nav.match(/class="guest-icon"/g) || []).length, 2)
  assert.match(descriptor.styles[0].content, /\.auth-help-links\s*\{[^}]*display: flex;[^}]*flex-wrap: nowrap;/)
})

test('两份静态样例一致，网关导航和可观测性角色切换脚本可执行', () => {
  const html = read('../../public/系统样例.html')
  assert.equal(html, read('../../../系统样例.html'))
  assert.match(html, /均为模拟数据/)
  assert.match(html, /id="demo-obs-technical" hidden/)
  const nodes = new Map()
  function node(id) {
    if (!nodes.has(id)) {
      const classes = new Set()
      nodes.set(id, { classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c) } })
    }
    return nodes.get(id)
  }
  for (const [, id] of html.matchAll(/id="([^"]+)"/g)) node(id)
  const views = [...nodes].filter(([id]) => id.startsWith('view-')).map(([, value]) => value)
  const buttons = ['business', 'technical'].map((mode) => ({ dataset: { obsMode: mode }, setAttribute(name, value) { this[name] = value } }))
  const context = {
    document: {
      getElementById: (id) => nodes.get(id),
      querySelector: () => null,
      querySelectorAll: (selector) => selector === '.view' ? views : selector === '[data-obs-mode]' ? buttons : [],
    },
  }
  runInNewContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context)
  for (const page of ['gateway', 'observability']) {
    assert.ok(html.includes(`data-view="${page}"`))
    assert.ok(html.includes(`onclick="switchView('${page}', this)"`))
    context.switchView(page, null)
    assert.equal(node(`view-${page}`).classList.contains('active'), true)
    assert.equal(views.filter((view) => view.classList.contains('active')).length, 1)
  }
  for (const mode of ['technical', 'business']) {
    context.setObservationMode(mode)
    assert.equal(node('demo-obs-business').hidden, mode !== 'business')
    assert.equal(node('demo-obs-technical').hidden, mode !== 'technical')
    assert.equal(buttons.find((button) => button.dataset.obsMode === mode)['aria-pressed'], 'true')
  }
})
