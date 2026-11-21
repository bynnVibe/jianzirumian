/**
 * 见字如面 - 统一查询缓存（单一 Pinia store，不引入第二套数据框架）
 *
 * - Key 含 Schema 版本 + 用户 + 权限版本 + 资源 + 规范化参数：切账号/撤权不串数据；
 * - 相同在途请求合并为一个 Promise；身份切换 epoch 防旧响应覆盖新页面；
 * - 内存 LRU 上限 200 条 / 20MB，仅缓存调用方显式白名单的安全 GET；
 * - 过期/被推送失效的查询「缓存先展示 + 后台重验」，可用旧列表不闪空；
 * - IndexedDB 仅持久化公共非敏感资源（persist=true 白名单），10MB / 24h；
 * - 权限/配额/执行许可仍由后端实时校验，缓存只用于展示。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { idbGet, idbPut, idbClear } from '../utils/idbCache.js'

const SCHEMA_VERSION = 1
const MAX_ENTRIES = 200
const MAX_BYTES = 20 * 1024 * 1024
export const TTL_META = 5 * 60 * 1000 // 稳定元数据兜底 TTL
export const TTL_MONITOR = 30 * 1000 // 运行监控/用量

export const useQueryCacheStore = defineStore('queryCache', () => {
  // key → {data, ts, ttl, bytes, resource, stale, version}（Map 迭代序=LRU 序）
  const entries = new Map()
  const inflight = new Map()
  const versions = ref({}) // 资源 → 远端最新版本（SSE/轮询同步）
  const userId = ref('')
  const permVersion = ref('')
  const epoch = ref(0) // 身份/清理事件计数：在途响应过期即丢弃
  let totalBytes = 0

  /** 登录态变化时绑定身份；身份或权限变化立即清空（撤权数据不留存） */
  function setIdentity(uid, role) {
    const nextUid = uid || ''
    const nextPerm = role || ''
    if (nextUid !== userId.value || nextPerm !== permVersion.value) {
      clearAll()
      userId.value = nextUid
      permVersion.value = nextPerm
    }
  }

  function buildKey(resource, params) {
    return JSON.stringify([SCHEMA_VERSION, userId.value, permVersion.value,
      resource, params ?? null])
  }

  function _evict() {
    while (entries.size > MAX_ENTRIES || totalBytes > MAX_BYTES) {
      const oldest = entries.keys().next().value
      if (oldest === undefined) break
      totalBytes -= entries.get(oldest).bytes || 0
      entries.delete(oldest)
    }
  }

  /**
   * 缓存先展示、按 TTL/资源版本重验的安全 GET 包装。
   * @param resource 资源域（与后端同步推送一致：tools/skills/knowledge/wiki/gateway/…）
   * @param params 规范化查询参数（参与 Key）
   * @param fetcher 实际请求函数（返回响应数据）
   * @param ttl 兜底过期时间；persist 仅公共非敏感白名单可 true；
   *        force 跳过新鲜命中（手动刷新）；onRevalidate 后台重验完成回调（视图 ref 同步）
   */
  async function cachedGet(resource, params, fetcher, { ttl = TTL_META, persist = false, force = false, onRevalidate } = {}) {
    const key = buildKey(resource, params)
    const hit = entries.get(key)
    const now = Date.now()
    const remoteVersion = versions.value[resource]
    const versionFresh = !hit || hit.version === undefined
      || remoteVersion === undefined || hit.version >= remoteVersion
    if (!force && hit && !hit.stale && versionFresh && now - hit.ts < hit.ttl) {
      entries.delete(key); entries.set(key, hit) // LRU 触达
      return hit.data
    }
    if (inflight.has(key)) {
      // 已有在途重验：旧值可用则先展示，不重复发请求
      if (hit && !force) {
        if (onRevalidate) inflight.get(key).then(onRevalidate).catch(() => {})
        return hit.data
      }
      return inflight.get(key)
    }
    const requestEpoch = epoch.value
    const promise = (async () => {
      try {
        const data = await fetcher()
        if (requestEpoch !== epoch.value) return data // 身份已切换：不写入缓存
        const bytes = (JSON.stringify(data) || '').length
        if (hit) totalBytes -= hit.bytes || 0
        entries.delete(key)
        entries.set(key, {
          data, ts: Date.now(), ttl, bytes, resource, stale: false,
          version: versions.value[resource],
        })
        totalBytes += bytes
        _evict()
        if (persist) idbPut(key, data).catch(() => {})
        return data
      } finally {
        inflight.delete(key)
      }
    })()
    inflight.set(key, promise)
    if (hit && !force) {
      // stale-while-revalidate：重验失败仍展示旧值（离线/断网不闪空）
      promise.then((d) => { if (onRevalidate) onRevalidate(d) }).catch(() => {})
      return hit.data
    }
    return promise
  }

  /** 公共非敏感资源的持久层预热（仅 persist 白名单写入过 IndexedDB） */
  async function warmPublic(resource, params) {
    const key = buildKey(resource, params)
    if (entries.has(key)) return
    const data = await idbGet(key).catch(() => null)
    if (data === null || data === undefined) return
    const bytes = (JSON.stringify(data) || '').length
    entries.set(key, { data, ts: 0, ttl: 0, bytes, resource, stale: true, version: undefined })
    totalBytes += bytes
    _evict()
  }

  /** 定向失效某资源域（写操作成功/远端推送）：标记 stale，下次访问重验 */
  function invalidate(resource) {
    for (const entry of entries.values()) {
      if (entry.resource === resource) entry.stale = true
    }
  }

  /** 收到远端新版本：记录版本并定向失效（不清空全部 store） */
  function bumpVersion(resource, version) {
    const cur = versions.value[resource]
    if (cur !== undefined && version <= cur) return
    versions.value = { ...versions.value, [resource]: version }
    invalidate(resource)
  }

  /** 版本快照对账（首连/reset/兜底轮询）：仅失效版本落后的资源 */
  function reconcileVersions(snapshot) {
    const next = { ...versions.value }
    for (const [resource, version] of Object.entries(snapshot || {})) {
      const cur = next[resource]
      if (cur === undefined || version > cur) {
        next[resource] = version
        invalidate(resource)
      }
    }
    versions.value = next
  }

  /** 退出登录/切账号：内存、在途与公共持久命名空间一并清理 */
  function clearAll() {
    entries.clear()
    inflight.clear()
    totalBytes = 0
    versions.value = {}
    epoch.value += 1
    idbClear().catch(() => {})
  }

  return {
    versions, userId, permVersion, epoch,
    setIdentity, buildKey, cachedGet, warmPublic,
    invalidate, bumpVersion, reconcileVersions, clearAll,
  }
})