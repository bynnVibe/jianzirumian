/**
 * 见字如面 - 资源变更同步客户端（每标签页一条连接，模块级单例）
 *
 * - fetch SSE /api/sync/events：Authorization 放请求头（不放 URL），
 *   首连 snapshot / 超窗 reset 对账版本，change 帧定向失效查询缓存；
 * - 断线指数退避 1s→30s（含抖动）重连，携带游标补发窗口内增量；
 * - 连续失败 ≥3 次退化为 60s 轻量版本轮询 /api/sync/versions（可回滚的推送开关）；
 * - BroadcastChannel('jzry-sync') 仅广播 {resource, version} 失效信号，
 *   不广播 Token / 业务正文；其他标签页收到后只失效不重播；
 * - 后台标签暂停兜底轮询，恢复可见立即补一次版本同步并重连；
 * - 长连接不续期会话（后端 peek 只读校验），注销/过期由 stopSync 关闭。
 */
const BC_NAME = 'jzry-sync'
const POLL_FALLBACK_MS = 60 * 1000
const MAX_BACKOFF_MS = 30 * 1000
const FAILURE_THRESHOLD = 3

let running = false
let cache = null // queryCache store 实例（由 startSync 注入，避免模块内取 pinia）
let controller = null
let cursor = 0
let failures = 0
let retryTimer = null
let pollTimer = null
let bc = null
let connecting = false

/** 推送开关：localStorage 'jzry_sync_push' = 'off' 时退化为纯版本轮询（可回滚） */
export function isPushEnabled() {
  try { return localStorage.getItem('jzry_sync_push') !== 'off' } catch { return true }
}

function authHeaders() {
  const token = localStorage.getItem('auth_token') || ''
  return token ? { Authorization: `Bearer ${token}` } : {}
}

function ensureChannel() {
  if (bc || typeof BroadcastChannel === 'undefined') return
  try {
    bc = new BroadcastChannel(BC_NAME)
    bc.onmessage = (e) => {
      const { resource, version } = e.data || {}
      if (resource && typeof version === 'number') {
        cache?.bumpVersion(resource, version) // 只失效，不再向外广播
      }
    }
  } catch { bc = null }
}

function broadcast(resource, version) {
  try { bc?.postMessage({ resource, version }) } catch { /* 忽略 */ }
}

/** 解析一帧 SSE 文本（id/event/data 行；':' 开头为心跳注释） */
export function parseFrame(raw) {
  const frame = { id: 0, event: 'message', data: null }
  for (const line of raw.split('\n')) {
    if (!line || line.startsWith(':')) continue
    const idx = line.indexOf(':')
    const field = idx === -1 ? line : line.slice(0, idx)
    let value = idx === -1 ? '' : line.slice(idx + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'id') frame.id = parseInt(value, 10) || 0
    else if (field === 'event') frame.event = value
    else if (field === 'data') {
      try { frame.data = JSON.parse(value) } catch { frame.data = null }
    }
  }
  return frame
}

function handleFrame(frame) {
  if (!frame.data) return
  if (frame.event === 'snapshot' || frame.event === 'reset') {
    cache?.reconcileVersions(frame.data.versions)
    cursor = frame.data.watermark || 0
    return
  }
  if (frame.event === 'change') {
    const { resource, version, id } = frame.data
    if (frame.id > cursor) cursor = frame.id
    else if (id > cursor) cursor = id
    if (resource && typeof version === 'number') {
      cache?.bumpVersion(resource, version)
      broadcast(resource, version)
    }
  }
}

async function connect() {
  if (!running || connecting) return
  connecting = true
  controller = new AbortController()
  try {
    const headers = { ...authHeaders(), Accept: 'text/event-stream' }
    if (cursor > 0) headers['Last-Event-ID'] = String(cursor)
    const res = await fetch(`/api/sync/events?since=${cursor}`, {
      headers, signal: controller.signal,
    })
    if (res.status === 401 || res.status === 403) { stopSync(); return }
    if (!res.ok || !res.body) throw new Error(`sync http ${res.status}`)
    failures = 0
    stopPolling() // SSE 恢复即退出兜底轮询
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let sep
      while ((sep = buffer.indexOf('\n\n')) !== -1) {
        const raw = buffer.slice(0, sep)
        buffer = buffer.slice(sep + 2)
        handleFrame(parseFrame(raw))
      }
    }
    throw new Error('sync stream ended') // 服务端正常关闭也走重连
  } catch (err) {
    if (!running || err?.name === 'AbortError') return
    failures += 1
    if (failures >= FAILURE_THRESHOLD) startPolling()
    scheduleRetry()
  } finally {
    connecting = false
  }
}

function scheduleRetry() {
  if (!running || retryTimer) return
  const base = Math.min(MAX_BACKOFF_MS, 1000 * 2 ** Math.min(failures - 1, 5))
  const delay = base + Math.random() * 500
  retryTimer = setTimeout(() => {
    retryTimer = null
    if (isPushEnabled()) connect()
  }, delay)
}

/** 兜底轻量版本轮询：SSE 连续失败或推送开关关闭时使用 */
async function pollVersions() {
  if (!running) return
  if (typeof document !== 'undefined' && document.hidden) return // 后台标签暂停
  try {
    const res = await fetch('/api/sync/versions', { headers: authHeaders() })
    if (res.status === 401 || res.status === 403) { stopSync(); return }
    if (!res.ok) return
    const data = await res.json()
    if (data?.versions) cache?.reconcileVersions(data.versions)
  } catch { /* 瞬态失败：下个周期再试 */ }
}

function startPolling() {
  if (pollTimer || !running) return
  pollVersions()
  pollTimer = setInterval(pollVersions, POLL_FALLBACK_MS)
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

function onVisibility() {
  if (!running) return
  if (typeof document !== 'undefined' && !document.hidden) {
    // 恢复可见：立即补一次版本对账；SSE 未连接则重连
    pollVersions()
    if (!connecting && isPushEnabled()) connect()
  }
}

/**
 * 启动同步（登录态建立后调用）。
 * @param cacheStore queryCache store 实例
 */
export function startSync(cacheStore) {
  if (running) return
  if (!localStorage.getItem('auth_token')) return
  running = true
  cache = cacheStore
  cursor = 0
  failures = 0
  ensureChannel()
  if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', onVisibility)
  }
  if (isPushEnabled()) connect()
  else startPolling() // 推送关闭：纯轮询模式
}

/** 停止同步并清理（注销/401/身份失效时调用），游标与退避状态一并复位 */
export function stopSync() {
  running = false
  cache = null
  if (controller) { try { controller.abort() } catch { /* 忽略 */ } controller = null }
  if (retryTimer) { clearTimeout(retryTimer); retryTimer = null }
  stopPolling()
  if (bc) { try { bc.close() } catch { /* 忽略 */ } bc = null }
  if (typeof document !== 'undefined') {
    document.removeEventListener('visibilitychange', onVisibility)
  }
  cursor = 0
  failures = 0
  connecting = false
}

/** 测试/诊断用：当前是否运行中 */
export function isSyncRunning() { return running }