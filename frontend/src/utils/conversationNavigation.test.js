import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  messageAnchor,
  conversationEntries,
  nearBottom,
  activeEntry,
  sampledEntries,
} from './conversationNavigation.js'

test('锚点使用 message_id，旧消息回退到会话内稳定序号', () => {
  assert.equal(messageAnchor('s1', { message_id: 'm9' }, 3), 'turn-s1-m9')
  assert.equal(messageAnchor('s1', {}, 3), 'turn-s1-legacy-3')
  // 会话 id 参与编码，切换会话不会撞锚点
  assert.notEqual(messageAnchor('s1', { message_id: 'm' }, 0), messageAnchor('s2', { message_id: 'm' }, 0))
  assert.equal(messageAnchor('', { message_id: 'm' }, 0), 'turn-new-m')
})

test('条目仅取用户提问，保留重复项并生成纯文本摘要', () => {
  const messages = [
    { role: 'user', message_id: 'a', content: '# 标题 **加粗** 问题' },
    { role: 'assistant', message_id: 'b', content: '回答不应出现' },
    { role: 'user', message_id: 'c', content: '重复问题' },
    { role: 'user', message_id: 'd', content: '重复问题' },
  ]
  const entries = conversationEntries(messages, 's')
  assert.deepEqual(entries.map(e => e.title), ['标题 加粗 问题', '重复问题', '重复问题'])
  assert.equal(entries.length, 3)
  assert.ok(entries.every(e => e.id.startsWith('turn-s-')))
})

test('附件提问使用文件名，缺失时回退到占位标题', () => {
  const withFile = conversationEntries([
    { role: 'user', message_id: 'a', content: '', attachments: [{ filename: '手写.jpg' }, { filename: '笔记.pdf' }] },
  ], 's')
  assert.equal(withFile[0].title, '手写.jpg、笔记.pdf')

  const empty = conversationEntries([{ role: 'user', message_id: 'b', content: '   ' }], 's')
  assert.equal(empty[0].title, '附件提问')
})

test('标题去除图片语法并截断到 100 字', () => {
  const long = 'x'.repeat(200)
  const entries = conversationEntries([
    { role: 'user', message_id: 'a', content: `![图](http://x/y.png) ${long}` },
  ], 's')
  assert.equal(entries[0].title.length, 100)
  assert.ok(entries[0].title.startsWith('图 '))
  assert.ok(!entries[0].title.includes('http'))
})

test('nearBottom 仅在距底不超过阈值时为真', () => {
  assert.equal(nearBottom(null), true)
  assert.equal(nearBottom({ scrollHeight: 1000, clientHeight: 400, scrollTop: 550 }, 100), true)
  assert.equal(nearBottom({ scrollHeight: 1000, clientHeight: 400, scrollTop: 100 }, 100), false)
})

test('activeEntry 选中视口上方最近的提问', () => {
  const entries = [{ id: 'a' }, { id: 'b' }, { id: 'c' }]
  const positions = [0, 300, 700]
  assert.equal(activeEntry(entries, positions, 0), 'a')
  assert.equal(activeEntry(entries, positions, 320), 'b')
  assert.equal(activeEntry(entries, positions, 900), 'c')
  assert.equal(activeEntry([], [], 0), '')
})

test('sampledEntries 长列表压缩到上限并保留首尾', () => {
  const entries = Array.from({ length: 100 }, (_, i) => ({ id: `t${i}` }))
  const sampled = sampledEntries(entries, 32)
  assert.equal(sampled.length, 32)
  assert.equal(sampled[0].id, 't0')
  assert.equal(sampled[31].id, 't99')
  // 短列表原样返回
  const short = [{ id: 'a' }, { id: 'b' }]
  assert.deepEqual(sampledEntries(short, 32), short)
})
