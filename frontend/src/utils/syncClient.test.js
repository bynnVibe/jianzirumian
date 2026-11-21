import assert from 'node:assert/strict'
import { test } from 'node:test'
import { parseFrame, isPushEnabled, isSyncRunning, startSync, stopSync } from './syncClient.js'

test('解析 snapshot 帧（id/event/data）', () => {
  const frame = parseFrame('event: snapshot\ndata: {"versions":{"tools":3},"watermark":42}')
  assert.equal(frame.event, 'snapshot')
  assert.deepEqual(frame.data, { versions: { tools: 3 }, watermark: 42 })
})

test('解析 change 帧并携带事件 id', () => {
  const frame = parseFrame('id: 17\nevent: change\ndata: {"resource":"usage","version":9,"id":17}')
  assert.equal(frame.id, 17)
  assert.equal(frame.event, 'change')
  assert.equal(frame.data.resource, 'usage')
  assert.equal(frame.data.version, 9)
})

test('心跳注释帧不产生数据', () => {
  const frame = parseFrame(': heartbeat')
  assert.equal(frame.data, null)
})

test('非法 JSON 数据优雅降级为 null', () => {
  const frame = parseFrame('event: change\ndata: {broken')
  assert.equal(frame.data, null)
})

test('data 冒号后首个空格按 SSE 规范剥离', () => {
  const frame = parseFrame('data:  {"a":1}')
  assert.deepEqual(frame.data, { a: 1 })
})

test('推送开关：默认开启，off 时关闭（可回滚纯轮询）', () => {
  const store = {}
  globalThis.localStorage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v) },
    removeItem: (k) => { delete store[k] },
  }
  try {
    assert.equal(isPushEnabled(), true)
    store.jzry_sync_push = 'off'
    assert.equal(isPushEnabled(), false)
  } finally {
    delete globalThis.localStorage
  }
})

test('未登录不启动同步；stopSync 复位状态', () => {
  const store = {}
  globalThis.localStorage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v) },
    removeItem: (k) => { delete store[k] },
  }
  try {
    startSync({ bumpVersion() {}, reconcileVersions() {} })
    assert.equal(isSyncRunning(), false) // 无 token 不建连
    stopSync()
    assert.equal(isSyncRunning(), false)
  } finally {
    delete globalThis.localStorage
  }
})