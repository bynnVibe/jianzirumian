/**
 * 见字如面 - 路由级按需预加载（悬停/空闲触发，最多 2 并发）
 *
 * 与 router 的 lazy import 指向同一 chunk：预加载只是提前触发模块加载，
 * 不改变路由懒加载策略，也不恢复 manualChunks（Vite8 rolldown 循环初始化问题）。
 */
const LOADERS = {
  '/chat': () => import('@/views/ChatView.vue'),
  '/skills': () => import('@/views/SkillsView.vue'),
  '/admin/tools': () => import('@/views/ToolGovernanceView.vue'),
  '/kb': () => import('@/views/KnowledgeOverviewView.vue'),
  '/observability': () => import('@/views/ObservabilityView.vue'),
  '/admin/gateway': () => import('@/views/GatewayView.vue'),
  '/admin/observability': () => import('@/views/ObservabilityView.vue'),
}

const MAX_CONCURRENT = 2
const done = new Set()
const queued = new Set()
const queue = []
let active = 0

/** 悬停/空闲预加载入口：同路径去重，失败静默（真正导航时仍会正常加载） */
export function preloadPath(path) {
  if (!LOADERS[path] || done.has(path) || queued.has(path)) return
  queued.add(path)
  queue.push(path)
  drain()
}

function drain() {
  while (active < MAX_CONCURRENT && queue.length) {
    const path = queue.shift()
    queued.delete(path)
    if (done.has(path)) continue
    active += 1
    LOADERS[path]()
      .then(() => done.add(path))
      .catch(() => {})
      .finally(() => { active -= 1; drain() })
  }
}

/** 空闲时段批量预加载（不阻塞首屏交互） */
export function scheduleIdlePreload(paths) {
  const run = () => (paths || []).forEach(preloadPath)
  if (typeof requestIdleCallback === 'function') requestIdleCallback(run, { timeout: 5000 })
  else setTimeout(run, 1500)
}