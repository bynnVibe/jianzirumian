import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createPinia, setActivePinia } from 'pinia'
import { useQueryCacheStore } from '../stores/queryCache.js'

function freshStore() {
  setActivePinia(createPinia())
  return useQueryCacheStore()
}
const tick = () => new Promise((resolve) => setTimeout(resolve, 10))

test('新鲜命中不重复发请求', async () => {
  const cache = freshStore()
  let calls = 0
  const fetcher = async () => { calls += 1; return { n: calls } }
  const a = await cache.cachedGet('tools', { kind: 'list' }, fetcher)
  const b = await cache.cachedGet('tools', { kind: 'list' }, fetcher)
  assert.equal(calls, 1)
  assert.deepEqual(a, b)
})

test('相同在途请求合并为一个 Promise', async () => {
  const cache = freshStore()
  let calls = 0
  const fetcher = () => { calls += 1; return new Promise((r) => setTimeout(() => r({ ok: true }), 20)) }
  const [a, b] = await Promise.all([
    cache.cachedGet('skills', null, fetcher),
    cache.cachedGet('skills', null, fetcher),
  ])
  assert.equal(calls, 1)
  assert.deepEqual(a, b)
})

test('参数不同则 Key 不同', async () => {
  const cache = freshStore()
  let calls = 0
  const fetcher = async () => { calls += 1; return calls }
  await cache.cachedGet('usage', { days: 7 }, fetcher)
  await cache.cachedGet('usage', { days: 30 }, fetcher)
  assert.equal(calls, 2)
})

test('切账号立即清空并重新拉取（身份隔离）', async () => {
  const cache = freshStore()
  cache.setIdentity('u1', 'user')
  let calls = 0
  const fetcher = async () => { calls += 1; return { owner: cache.userId } }
  await cache.cachedGet('knowledge', null, fetcher)
  cache.setIdentity('u2', 'user')
  const data = await cache.cachedGet('knowledge', null, fetcher)
  assert.equal(calls, 2)
  assert.equal(data.owner, 'u2')
})

test('权限变化同样清空缓存', async () => {
  const cache = freshStore()
  cache.setIdentity('u1', 'user')
  let calls = 0
  const fetcher = async () => { calls += 1; return calls }
  await cache.cachedGet('gateway', null, fetcher)
  cache.setIdentity('u1', 'admin')
  await cache.cachedGet('gateway', null, fetcher)
  assert.equal(calls, 2)
})

test('invalidate 定向失效：下次访问重验且旧值先展示', async () => {
  const cache = freshStore()
  let calls = 0
  const fetcher = async () => { calls += 1; return { v: calls } }
  await cache.cachedGet('tools', null, fetcher)
  cache.invalidate('tools')
  const stale = await cache.cachedGet('tools', null, fetcher)
  assert.deepEqual(stale, { v: 1 }) // 缓存先展示
  await tick()
  assert.equal(calls, 2) // 后台已重验
  const fresh = await cache.cachedGet('tools', null, fetcher)
  assert.deepEqual(fresh, { v: 2 })
})

test('bumpVersion 只升不降，且定向失效对应资源', async () => {
  const cache = freshStore()
  let toolCalls = 0
  let skillCalls = 0
  await cache.cachedGet('tools', null, async () => { toolCalls += 1; return 1 })
  await cache.cachedGet('skills', null, async () => { skillCalls += 1; return 1 })
  cache.bumpVersion('tools', 5)
  cache.bumpVersion('tools', 3) // 回退版本忽略
  assert.equal(cache.versions.tools, 5)
  await cache.cachedGet('tools', null, async () => { toolCalls += 1; return 2 })
  await cache.cachedGet('skills', null, async () => { skillCalls += 1; return 2 })
  await tick()
  assert.equal(toolCalls, 2)
  assert.equal(skillCalls, 1) // 未变更资源不重拉
})

test('reconcileVersions 仅失效版本落后的资源', async () => {
  const cache = freshStore()
  cache.bumpVersion('tools', 5)
  cache.bumpVersion('skills', 2)
  let toolCalls = 0
  let skillCalls = 0
  await cache.cachedGet('tools', null, async () => { toolCalls += 1; return 1 })
  await cache.cachedGet('skills', null, async () => { skillCalls += 1; return 1 })
  cache.reconcileVersions({ tools: 6, skills: 2 })
  await cache.cachedGet('tools', null, async () => { toolCalls += 1; return 2 })
  await cache.cachedGet('skills', null, async () => { skillCalls += 1; return 2 })
  await tick()
  assert.equal(toolCalls, 2)
  assert.equal(skillCalls, 1)
})

test('force 跳过新鲜命中（手动刷新）', async () => {
  const cache = freshStore()
  let calls = 0
  const fetcher = async () => { calls += 1; return calls }
  await cache.cachedGet('tools', null, fetcher)
  const data = await cache.cachedGet('tools', null, fetcher, { force: true })
  assert.equal(calls, 2)
  assert.equal(data, 2)
})

test('onRevalidate 在后台重验完成后回调', async () => {
  const cache = freshStore()
  let calls = 0
  const fetcher = async () => { calls += 1; return { v: calls } }
  await cache.cachedGet('tools', null, fetcher)
  cache.invalidate('tools')
  let revalidated = null
  await cache.cachedGet('tools', null, fetcher, { onRevalidate: (d) => { revalidated = d } })
  await tick()
  assert.deepEqual(revalidated, { v: 2 })
})

test('TTL 过期后重验', async () => {
  const cache = freshStore()
  let calls = 0
  const fetcher = async () => { calls += 1; return calls }
  await cache.cachedGet('tools', null, fetcher, { ttl: 1 })
  await tick()
  await cache.cachedGet('tools', null, fetcher, { ttl: 1 })
  await tick()
  assert.equal(calls, 2)
})

test('LRU 超出 200 条淘汰最旧', async () => {
  const cache = freshStore()
  const calls = {}
  const fetcherFor = (i) => async () => { calls[i] = (calls[i] || 0) + 1; return i }
  for (let i = 0; i < 201; i += 1) {
    await cache.cachedGet('tools', { i }, fetcherFor(i))
  }
  await cache.cachedGet('tools', { i: 0 }, fetcherFor(0)) // 最旧已被淘汰
  await cache.cachedGet('tools', { i: 200 }, fetcherFor(200)) // 最新仍在
  assert.equal(calls[0], 2)
  assert.equal(calls[200], 1)
})

test('身份切换后在途响应不写入缓存（epoch 防覆盖）', async () => {
  const cache = freshStore()
  cache.setIdentity('u1', 'user')
  let calls = 0
  let release
  const slow = () => { calls += 1; return new Promise((r) => { release = r }) }
  const pending = cache.cachedGet('tools', null, slow)
  cache.setIdentity('u2', 'user') // 响应返回前切账号
  release({ old: true })
  await pending
  const data = await cache.cachedGet('tools', null, async () => { calls += 1; return { fresh: true } })
  assert.deepEqual(data, { fresh: true })
  assert.equal(calls, 2)
})

test('clearAll 清空内存与版本', async () => {
  const cache = freshStore()
  await cache.cachedGet('tools', null, async () => 1)
  cache.bumpVersion('tools', 9)
  cache.clearAll()
  assert.deepEqual(cache.versions, {})
  let calls = 0
  await cache.cachedGet('tools', null, async () => { calls += 1; return 2 })
  assert.equal(calls, 1)
})