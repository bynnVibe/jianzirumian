import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { parse, compileScript } from '@vue/compiler-sfc'
import * as Vue from 'vue'
import { renderToString } from '@vue/server-renderer'
import MarkdownIt from 'markdown-it'

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
    getObservabilityMetrics: {
      overview: { total: 0, success_rate: 0, errors: 0, avg_latency_ms: 0, p95_latency_ms: 0, prompt_tokens: 0, completion_tokens: 0, avg_tokens_per_request: 0, llm_calls: 0, tool_calls: 0, token_overuse: false, token_warn_threshold: 0 },
      daily: [], by_agent: [], by_tool: [],
      golden_signals: { rate_per_day: 0, error_rate: 0, duration: { avg_ms: 0, p95_ms: 0 }, saturation: { avg_tokens_per_request: 0, overuse: false, utilization: 0 } },
      token_comparison: { current_avg: 0, previous_avg: 0, delta_pct: 0, trend: 'flat' },
    },
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
  }).replace(/import\s+([A-Za-z_$][\w$]*)\s+from\s+['"]([^'"]+)['"];?/g, (_, name, source) => {
    // 子组件（ObsDashboard/ObsLogs 等）默认导入：以空渲染桩替代，聚焦父视图逻辑
    return `const ${name} = (modules[${JSON.stringify(source)}] || {}).default;`
  })
  const component = new Function('modules', `${code}\nreturn __component;`)({
    vue: { ...Vue, onMounted: (fn) => { mounted = fn } },
    '@/stores/auth': { useAuthStore: () => ({ canOps }) }, '@/api': api,
    'vue-router': { useRoute: () => Vue.reactive({ query: {} }), useRouter: () => ({ push() {} }) },
    '@/components/observability/ObsDashboard.vue': { default: { name: 'ObsDashboard', props: ['metrics', 'monitor', 'loading', 'rangeLabel'], render: () => null } },
    '@/components/observability/ObsLogs.vue': { default: { name: 'ObsLogs', render: () => null } },
  })
  const render = component.setup({}, { expose() {} })
  await mounted()
  return {
    calls,
    html: () => renderToString(Vue.createSSRApp({ render })),
    nodes: () => render({}, []),
  }
}

async function suggestionsHarness(overrides = {}) {
  let mounted, unmounted
  const calls = []
  const draft = { id: 'draft-1', name: '待确认天气能力', status: 'pending', session_id: 's1', message_id: 'm1' }
  const defaults = {
    listSkills: async () => ({ data: { skills: [] } }),
    listPersonalSuggestions: async () => ({ data: { suggestions: [{ id: 'mine', count: 3, label: '当前天气查询', sample_query: '<script>我的天气</script>' }], drafts: [] } }),
    markPersonalSuggestionsSeen: async () => ({ data: { ids: ['mine'] } }),
    acceptPersonalSuggestion: async () => ({ data: { draft } }),
    dismissPersonalSuggestion: async () => ({ data: {} }),
    ...overrides,
  }
  const api = Object.fromEntries(Object.entries(defaults).map(([name, fn]) => [name, async (...args) => {
    calls.push([name, ...args])
    return fn(...args)
  }]))
  const { descriptor, errors } = parse(read('../views/SkillsView.vue'))
  assert.deepEqual(errors, [])
  const compiled = compileScript(descriptor, { id: 'suggestions-test', inlineTemplate: true, genDefaultAs: '__component' })
  const code = compiled.content.replace(/import\s+\{([^}]+)\}\s+from\s+['"]([^'"]+)['"];?/g, (_, names, source) =>
    `const { ${names.replace(/\bas\b/g, ':')} } = modules[${JSON.stringify(source)}];`
  ).replace(/import\s+([A-Za-z_$][\w$]*)\s+from\s+['"]([^'"]+)['"];?/g, (_, name, source) =>
    `const ${name} = modules[${JSON.stringify(source)}].default;`
  )
  const component = new Function('modules', `${code}\nreturn __component;`)({
    vue: { ...Vue, onMounted: fn => { mounted = fn }, onUnmounted: fn => { unmounted = fn } },
    'vue-router': { useRouter: () => ({ push() {} }) }, 'markdown-it': { default: MarkdownIt }, '@/api': api,
    '@/components/PersonalToolsPanel.vue': { default: { render: () => null } },
    '@/components/SkillDraftCard.vue': { default: { props: ['draft'], render() { return Vue.h('p', `${this.draft.name}:${this.draft.status}`) } } },
  })
  const render = component.setup({}, { expose() {} })
  await mounted()
  return { calls, draft, unmount: () => unmounted(),
    html: () => renderToString(Vue.createSSRApp({ render })),
    button: text => findNode(render({}, []), node => node.type === 'button' && typeof node.children === 'string' && node.children.trim() === text),
  }
}

test('个人建议只领展示窗口，接受后仅呈现草稿并转义问题', async () => {
  const view = await suggestionsHarness()
  assert.deepEqual(view.calls.map(([name]) => name), ['listSkills', 'listPersonalSuggestions', 'markPersonalSuggestionsSeen'])
  let html = await view.html()
  assert.match(html, /仅本人可见|7 天内不重复/)
  assert.match(html, /&lt;script&gt;我的天气&lt;\/script&gt;/)
  await view.button('查找资料并生成草稿').props.onClick()
  html = await view.html()
  assert.match(html, /待确认天气能力:pending/)
  assert.equal(view.button('查找资料并生成草稿'), undefined)
  assert.deepEqual(view.calls.map(([name]) => name), ['listSkills', 'listPersonalSuggestions', 'markPersonalSuggestionsSeen', 'acceptPersonalSuggestion'])
  view.unmount()
})

test('个人建议刷新恢复草稿但不重放发现，忽略后移除提示', async () => {
  const view = await suggestionsHarness()
  await view.button('忽略 7 天').props.onClick()
  assert.equal(view.button('忽略 7 天'), undefined)
  assert.equal(view.calls.at(-1)[0], 'dismissPersonalSuggestion')
  view.unmount()
  const restored = await suggestionsHarness({ listPersonalSuggestions: async () => ({ data: { suggestions: [], drafts: [{ id: 'old', name: '原轮次草稿', status: 'pending' }] } }) })
  assert.match(await restored.html(), /原轮次草稿:pending/)
  assert.deepEqual(restored.calls.map(([name]) => name), ['listSkills', 'listPersonalSuggestions'])
  restored.unmount()
})

test('切换页面取消建议发现且忽略迟到响应', async () => {
  let complete, signal
  const view = await suggestionsHarness({ acceptPersonalSuggestion: (id, s) => { signal = s; return new Promise(resolve => { complete = resolve }) } })
  const pending = view.button('查找资料并生成草稿').props.onClick()
  view.unmount()
  assert.equal(signal.aborted, true)
  complete({ data: { draft: view.draft } })
  await pending
  assert.doesNotMatch(await view.html(), /待确认天气能力:pending/)
})

test('取消发现可用，加载失败不泄露内部错误', async () => {
  const view = await suggestionsHarness({ acceptPersonalSuggestion: (id, signal) => new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => reject(new Error('internal-discovery-error')), { once: true })
  }) })
  const pending = view.button('查找资料并生成草稿').props.onClick()
  view.button('取消发现').props.onClick()
  await pending
  assert.match(await view.html(), /已停止等待，未自动安装/)
  view.unmount()
  const failed = await suggestionsHarness({ listPersonalSuggestions: async () => { throw new Error('internal-discovery-error') } })
  assert.match(await failed.html(), /个人建议加载失败/)
  assert.doesNotMatch(await failed.html(), /internal-discovery-error/)
  failed.unmount()
})

test('建议 API 只对发现放宽超时且传递取消信号', async () => {
  const source = read('../api/index.js')
  const declaration = source.match(/export const acceptPersonalSuggestion = [^\n]+/)[0].replace('export ', '')
  const calls = []
  const accept = new Function('api', `${declaration}; return acceptPersonalSuggestion;`)({ post: (...args) => calls.push(args) })
  const signal = new AbortController().signal
  accept('a/b', signal)
  assert.equal(calls[0][0], '/skills/suggestions/a%2Fb/accept')
  assert.equal(calls[0][2].timeout, 200000)
  assert.equal(calls[0][2].signal, signal)
  assert.match(source, /timeout: 30000/)
  assert.doesNotMatch(observation, /s\.sample_query|openSedimentDialog|submitSediment/)
})

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
  assert.deepEqual(view.calls, [['getMyObservabilityMetrics', 7, 'days']])
  const html = await view.html()
  for (const text of ['我的使用概览', '使用次数', '服务完成率', '66.7%', '2.5 秒', '智能问答']) assert.ok(html.includes(text))
  // SSR 会保留模板注释（含技术词汇），断言前剥离，仅校验用户可见内容
  assert.doesNotMatch(html.replace(/<!--[\s\S]*?-->/g, ''), /Agent|Token|工具调用|请求追踪|LLM|PROMPT|RESPONSE/)
  const refresh = findNode(view.nodes(), (node) => node.type === 'button' && typeof node.children === 'string' && node.children.trim() === '刷新')
  assert.ok(refresh)
  await refresh.props.onClick()
  assert.deepEqual(view.calls, [['getMyObservabilityMetrics', 7, 'days'], ['getMyObservabilityMetrics', 7, 'days']])
})

test('管理员和运维分支保留全站指标与追踪入口', async () => {
  const view = await observationHarness(true)
  assert.deepEqual(view.calls.map(([name]) => name), ['getObservabilityMetrics', 'getObservabilityTraces'])
  let html = await view.html()
  assert.match(html, /Agent 可观测性/)
  assert.match(html, /DashBoard/)
  assert.doesNotMatch(html, /我的使用概览|业务功能使用情况/)
  const clickTab = async (label) => {
    const tab = findNode(view.nodes(), (node) => node.type === 'button' && typeof node.children === 'string' && node.children.trim() === label)
    assert.ok(tab)
    tab.props.onClick()
    return view.html()
  }
  html = await clickTab('指标监控')
  assert.match(html, /Token 消耗/)
  html = await clickTab('链路追踪')
  assert.match(html, /请求追踪/)
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
  assert.deepEqual(view.calls, [['getMyObservabilityMetrics', 7, 'days'], ['getMyObservabilityMetrics', 30, 'days']])
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
      nodes.set(id, { querySelector: () => null, classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c) } })
    }
    return nodes.get(id)
  }
  for (const [, id] of html.matchAll(/id="([^"]+)"/g)) node(id)
  const views = [...nodes].filter(([id]) => id.startsWith('view-')).map(([, value]) => value)
  const buttons = ['business', 'technical'].map((mode) => ({ dataset: { obsMode: mode }, setAttribute(name, value) { this[name] = value } }))
  const context = {
    document: {
      addEventListener() {},
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
