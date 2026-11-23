import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath } from 'node:url'
import { buildPlanTodos, planTodoSummary, todoStateLabel } from './agentTodos.js'

const plan = [
  { tool: 'search_knowledge', purpose: '检索差旅报销标准', query: '差旅 报销 标准' },
  { tool: 'search_knowledge', purpose: '检索审批流程', query: '报销 审批' },
]

test('计划下发即生成待办：全部待执行 + 收尾步骤，文案不含工具名', () => {
  const todos = buildPlanTodos({ plan, tools: [], live: true })
  assert.deepEqual(todos.map(t => t.state), ['pending', 'pending', 'pending'])
  assert.deepEqual(todos.map(t => t.text), ['检索差旅报销标准', '检索审批流程', '整理证据并生成回答'])
  assert.ok(!JSON.stringify(todos).includes('search_knowledge'))
  assert.equal(planTodoSummary(todos), '0/3 步完成')
})

test('tool start/end 事件实时驱动完成态：进行中→已完成，并按序号配对', () => {
  const tools = [{ tool: 'search_knowledge', state: 'start', index: 1, total: 2, purpose: '检索差旅报销标准' }]
  let todos = buildPlanTodos({ plan, tools, live: true })
  assert.deepEqual(todos.map(t => t.state), ['running', 'pending', 'pending'])

  // end 事件回填（store 对同一记录 Object.assign）：乱序到达也按 index 归位
  tools[0] = { ...tools[0], state: 'end', summary: '命中 3 条', elapsed_ms: 1200 }
  tools.push({ tool: 'search_knowledge', state: 'start', index: 2, total: 2 })
  todos = buildPlanTodos({ plan, tools, live: true })
  assert.deepEqual(todos.map(t => t.state), ['done', 'running', 'pending'])
  assert.equal(todos[0].detail, '命中 3 条')
  assert.equal(todos[0].elapsed, 1200)
  assert.equal(planTodoSummary(todos), '1/3 步完成')
})

test('步骤全部终态：流式期间收尾进行中，落库后收尾已完成', () => {
  const tools = [
    { tool: 'search_knowledge', state: 'end', index: 1, summary: '命中 3 条' },
    { tool: 'search_knowledge', state: 'end', index: 2, summary: '命中 2 条' },
  ]
  assert.equal(buildPlanTodos({ plan, tools, live: true }).at(-1).state, 'running')
  assert.equal(buildPlanTodos({ plan, tools, live: false }).at(-1).state, 'done')
  assert.equal(planTodoSummary(buildPlanTodos({ plan, tools, live: false })), '3/3 步完成')
})

test('失败步骤单独标红并给出可解释文案，收尾不误判完成', () => {
  const tools = [
    { tool: 'search_knowledge', state: 'end', index: 1, failed: true },
    { tool: 'search_knowledge', state: 'end', index: 2, summary: '命中 2 条' },
  ]
  const todos = buildPlanTodos({ plan, tools, live: true })
  assert.equal(todos[0].state, 'failed')
  assert.equal(todos[0].detail, '该步骤未取到结果')
  assert.equal(todos.at(-1).state, 'running')
  assert.equal(planTodoSummary(todos), '1/3 步完成（1 失败）')
  // 结构化失败归因存在时收尾保持待执行
  assert.equal(buildPlanTodos({ plan, tools, live: true, error: { stage: 'answer' } }).at(-1).state, 'pending')
})

test('旧快照缺序号按顺序兜底配对；无说明时兜底「步骤 N」且不泄露检索词以外的方法名', () => {
  const todos = buildPlanTodos({
    plan: [{ tool: 'pt_custom' }, { tool: 'web_search', query: '最新政策' }],
    tools: [{ tool: 'pt_custom', state: 'end', summary: 'ok' }],
    live: false,
  })
  assert.deepEqual(todos.map(t => t.text), ['步骤 1', '最新政策', '整理证据并生成回答'])
  assert.deepEqual(todos.map(t => t.state), ['done', 'pending', 'pending'])
  assert.ok(!JSON.stringify(todos).includes('pt_custom'))
})

test('无计划不产出待办（非 Plan&Execute 路径保持原呈现）', () => {
  assert.deepEqual(buildPlanTodos({ plan: [], tools: [{ tool: 'x', state: 'end' }] }), [])
  assert.equal(planTodoSummary([]), '')
  assert.equal(todoStateLabel('running'), '进行中')
  assert.equal(todoStateLabel('unknown'), '')
})

test('复用步骤：上一轮知识检索直接呈现已完成并明示复用，新步骤序号接续', () => {
  const reusePlan = [
    { tool: 'search_knowledge', purpose: '检索失眠调理资料', query: '失眠', reused: true },
    { tool: 'web_search', purpose: '补充联网运动建议', query: '失眠 运动' },
  ]
  const tools = [
    { tool: 'search_knowledge', state: 'end', index: 1, reused: true, summary: '检索到 3 条相关片段', elapsed_ms: 0 },
    { tool: 'web_search', state: 'start', index: 2 },
  ]
  const todos = buildPlanTodos({ plan: reusePlan, tools, live: true })
  assert.deepEqual(todos.map(t => t.state), ['done', 'running', 'pending'])
  assert.equal(todos[0].text, '检索失眠调理资料（复用上一轮结果）')
  assert.equal(todos[0].detail, '检索到 3 条相关片段')
  assert.equal(todos[0].reused, true)
  assert.equal(todos[1].text, '补充联网运动建议')
  assert.equal(planTodoSummary(todos), '1/3 步完成')
  assert.ok(!JSON.stringify(todos).includes('search_knowledge'))
})

test('待授权步骤：计划已生成未执行时明示「待授权联网搜索」，授权执行后恢复正常文案', () => {
  const authPlan = [
    { tool: 'search_knowledge', purpose: '检索知识库调理资料' },
    { tool: 'web_search', purpose: '补充运动方案', needs_auth: true },
  ]
  const todos = buildPlanTodos({ plan: authPlan, tools: [], live: false })
  assert.deepEqual(todos.map(t => t.state), ['pending', 'pending', 'pending'])
  assert.equal(todos[1].text, '补充运动方案（待授权联网搜索）')
  assert.equal(todos[1].needsAuth, true)
  assert.equal(todos[0].needsAuth, false)
  // 授权后续跑：步骤一旦开始执行不再标注待授权
  const running = buildPlanTodos({
    plan: authPlan, tools: [{ tool: 'web_search', state: 'start', index: 2 }], live: true,
  })
  assert.equal(running[1].text, '补充运动方案')
  assert.equal(running[1].state, 'running')
})

// ---- 面板渲染契约：步骤待办取代「解题计划/工具调用/内部阶段清单」 ----
const root = fileURLToPath(new URL('../../', import.meta.url))
const server = await createServer({
  configFile: false, root, envDir: fileURLToPath(new URL('../../../node_modules/.cache/no-env', import.meta.url)),
  server: { middlewareMode: true }, appType: 'custom',
  optimizeDeps: { noDiscovery: true, include: [] },
  plugins: [vue()],
})
after(() => server.close())
const { default: AgentProcessPanel } = await server.ssrLoadModule('/src/components/AgentProcessPanel.vue')

function render(props) {
  return renderToString(createSSRApp(AgentProcessPanel, { collapsed: false, ...props }))
}

test('Plan&Execute 面板只渲染步骤待办，不泄露工具名与内部阶段名', async () => {
  const html = await render({
    plan,
    tools: [
      { tool: 'search_knowledge', state: 'end', index: 1, summary: '命中 3 条', elapsed_ms: 900 },
      { tool: 'search_knowledge', state: 'start', index: 2 },
    ],
    steps: [
      { step_id: 's1', stage: 'complexity_classify', state: 'ok', elapsed_ms: 10 },
      { step_id: 's2', stage: 'search_knowledge#step1', state: 'ok', elapsed_ms: 900 },
    ],
    live: true,
  })
  assert.match(html, /步骤待办/)
  assert.match(html, /检索差旅报销标准/)
  assert.match(html, /todo-done/)
  assert.match(html, /todo-running/)
  assert.match(html, /1\/3 步完成/)
  assert.ok(!html.includes('search_knowledge'), '不得呈现工具名')
  assert.ok(!html.includes('complexity_classify'), '不得呈现内部阶段名')
  assert.ok(!html.includes('工具调用') && !html.includes('解题计划'))
})

test('非 Plan&Execute 面板保持阶段清单，且多步骤阶段只呈现「步骤 N」', async () => {
  const html = await render({
    plan: [],
    tools: [],
    steps: [{ step_id: 's1', stage: 'search_knowledge#step2', state: 'ok', elapsed_ms: 1200 }],
  })
  assert.match(html, /步骤 2/)
  assert.ok(!html.includes('search_knowledge'))
  assert.ok(!html.includes('process-todo-list'))
})

test('复用轮面板：知识步骤已完成·复用，联网补充步骤实时推进，不泄露工具名', async () => {
  const html = await render({
    plan: [
      { tool: 'search_knowledge', purpose: '检索失眠调理资料', reused: true },
      { tool: 'web_search', purpose: '补充联网运动建议' },
    ],
    tools: [
      { tool: 'search_knowledge', state: 'end', index: 1, reused: true, summary: '检索到 3 条相关片段' },
      { tool: 'web_search', state: 'start', index: 2 },
    ],
    live: true,
  })
  assert.match(html, /检索失眠调理资料（复用上一轮结果）/)
  assert.match(html, /todo-done/)
  assert.match(html, /todo-running/)
  assert.ok(!html.includes('search_knowledge') && !html.includes('web_search'))
})