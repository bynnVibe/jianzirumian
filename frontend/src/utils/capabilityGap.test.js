import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createPinia } from 'pinia'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath } from 'node:url'

// 能力受限卡渲染契约：动作执行完毕后按钮标灰不可点，避免重复重发与重复检索
const root = fileURLToPath(new URL('../../', import.meta.url))
const server = await createServer({
  configFile: false, root, envDir: fileURLToPath(new URL('../../../node_modules/.cache/no-env', import.meta.url)),
  server: { middlewareMode: true }, appType: 'custom',
  resolve: { alias: { '@': fileURLToPath(new URL('../', import.meta.url)) } },
  optimizeDeps: { noDiscovery: true, include: [] },
  plugins: [vue()],
})
after(() => server.close())
const { default: CapabilityGapCard } = await server.ssrLoadModule('/src/components/CapabilityGapCard.vue')

function render(gap) {
  const app = createSSRApp(CapabilityGapCard, { gap })
  app.use(createPinia())
  return renderToString(app)
}

const GAP = {
  gap_id: 'g1', scenario: 'realtime_weather', scenario_label: '实时天气',
  query: '帮我规划3个月的调理方案', message: '当前能力不足以直接回答。',
  suggestions: [{ id: 'enable_search', label: '开启联网搜索并重新提问' }],
}

test('未执行动作：按钮可点且文案为原始动作', async () => {
  const html = await render({ ...GAP })
  assert.match(html, /开启联网搜索并重新提问/)
  assert.ok(!html.includes('disabled'), '未执行时不应禁用')
  assert.ok(!html.includes('该动作已执行'))
})

test('动作已执行：按钮标灰禁用、文案改为已完成态并给出说明', async () => {
  const html = await render({ ...GAP, resolved: true, resolved_action: 'enable_search' })
  assert.match(html, /已开启联网搜索并重新提问/)
  assert.match(html, /disabled/)
  assert.match(html, /resolved gap-btn/)
  assert.match(html, /该动作已执行/)
  assert.ok(!html.includes('gap-btn primary'), '已执行不再是主操作样式')
})

test('存量历史卡（无 suggestions）降级为应用内建议，执行后同样标灰', async () => {
  const html = await render({ ...GAP, suggestions: [], resolved: true })
  assert.match(html, /已开启联网搜索并重新提问/)
  assert.match(html, /disabled/)
})