/**
 * 见字如面 - 公共非敏感数据的 IndexedDB 持久缓存（仅 queryCache persist 白名单使用）
 *
 * 边界（与整改方案一致）：
 * - 仅存公共能力目录 / UI 偏好等非敏感数据；聊天正文、私有文档、密钥、
 *   鉴权结果、原始日志、配置表单一律不进入本层；
 * - 上限 10MB / 24h，超限按最旧淘汰；
 * - 所有操作优雅降级：无 IndexedDB / 隐私模式 / 配额异常时静默失效，不影响主流程。
 */
const DB_NAME = 'jzry-public-cache'
const STORE = 'entries'
const MAX_BYTES = 10 * 1024 * 1024
const MAX_AGE_MS = 24 * 60 * 60 * 1000

let dbPromise = null

function openDb() {
  if (typeof indexedDB === 'undefined') return Promise.resolve(null)
  if (!dbPromise) {
    dbPromise = new Promise((resolve) => {
      try {
        const req = indexedDB.open(DB_NAME, 1)
        req.onupgradeneeded = () => {
          const db = req.result
          if (!db.objectStoreNames.contains(STORE)) {
            db.createObjectStore(STORE, { keyPath: 'key' })
          }
        }
        req.onsuccess = () => resolve(req.result)
        req.onerror = () => resolve(null)
        req.onblocked = () => resolve(null)
      } catch {
        resolve(null)
      }
    })
  }
  return dbPromise
}

function tx(db, mode) {
  return db.transaction(STORE, mode).objectStore(STORE)
}

/** 读取（过期即视为未命中并删除）；任何异常返回 null */
export async function idbGet(key) {
  try {
    const db = await openDb()
    if (!db) return null
    const row = await new Promise((resolve) => {
      const req = tx(db, 'readonly').get(key)
      req.onsuccess = () => resolve(req.result || null)
      req.onerror = () => resolve(null)
    })
    if (!row) return null
    if (Date.now() - row.ts > MAX_AGE_MS) {
      idbDelete(key)
      return null
    }
    return row.data
  } catch {
    return null
  }
}

function idbDelete(key) {
  openDb().then((db) => {
    if (!db) return
    try { tx(db, 'readwrite').delete(key) } catch { /* 忽略 */ }
  }).catch(() => {})
}

/** 写入（含大小估算与总量淘汰）；任何异常静默忽略 */
export async function idbPut(key, data) {
  try {
    const db = await openDb()
    if (!db) return
    const bytes = (JSON.stringify(data) || '').length
    if (bytes > MAX_BYTES) return // 单条超限不持久化
    await new Promise((resolve) => {
      const store = tx(db, 'readwrite')
      store.put({ key, data, ts: Date.now(), bytes })
      store.transaction.oncomplete = () => resolve()
      store.transaction.onerror = () => resolve()
      store.transaction.onabort = () => resolve()
    })
    await evict(db)
  } catch { /* 配额/隐私模式等：忽略 */ }
}

async function evict(db) {
  const rows = await new Promise((resolve) => {
    const req = tx(db, 'readonly').getAll()
    req.onsuccess = () => resolve(req.result || [])
    req.onerror = () => resolve([])
  })
  const now = Date.now()
  let total = rows.reduce((sum, r) => sum + (r.bytes || 0), 0)
  const expired = rows.filter((r) => now - r.ts > MAX_AGE_MS)
  const sorted = rows
    .filter((r) => now - r.ts <= MAX_AGE_MS)
    .sort((a, b) => a.ts - b.ts)
  const removeKeys = expired.map((r) => r.key)
  total -= expired.reduce((sum, r) => sum + (r.bytes || 0), 0)
  while (total > MAX_BYTES && sorted.length) {
    const oldest = sorted.shift()
    removeKeys.push(oldest.key)
    total -= oldest.bytes || 0
  }
  if (!removeKeys.length) return
  await new Promise((resolve) => {
    const store = tx(db, 'readwrite')
    removeKeys.forEach((k) => store.delete(k))
    store.transaction.oncomplete = () => resolve()
    store.transaction.onerror = () => resolve()
    store.transaction.onabort = () => resolve()
  })
}

/** 清空公共缓存命名空间（注销/切账号时由 queryCache.clearAll 调用） */
export async function idbClear() {
  try {
    const db = await openDb()
    if (!db) return
    await new Promise((resolve) => {
      const store = tx(db, 'readwrite')
      store.clear()
      store.transaction.oncomplete = () => resolve()
      store.transaction.onerror = () => resolve()
      store.transaction.onabort = () => resolve()
    })
  } catch { /* 忽略 */ }
}