import test from 'node:test'
import assert from 'node:assert/strict'
import {
  DEFAULT_THRESHOLDS, fmtTok, mergeThresholds, buildTicks, headroomText,
  compactLabel, spanTokenText, spanMeta, spanMetaText, traceTokenSummary,
} from './contextTokens.js'

test('量纲统一 K=1000：整千不带小数，128K 而非 131.1K', () => {
  assert.equal(fmtTok(128000), '128K')
  assert.equal(fmtTok(95000), '95K')
  assert.equal(fmtTok(20000), '20K')
  assert.equal(fmtTok(4000), '4K')
  // 非整千保留一位（兜底阈值 119040 = 128000 × 93%）
  assert.equal(fmtTok(119040), '119.0K')
  assert.equal(fmtTok(143), '143')
  assert.equal(fmtTok(0), '0')
  assert.equal(fmtTok(undefined), '0')
  assert.equal(fmtTok('abc'), '0')
})

test('阈值默认值即四层口径：窗口 128K / 输出帽 4K / 微压缩 20K / Auto 95K / 兜底 119040', () => {
  assert.deepEqual(DEFAULT_THRESHOLDS, {
    window: 128000, output_cap: 4000, micro_trigger: 20000,
    auto: 95000, full: 119040, summary_reserve: 20000,
  })
  // Auto 阈值 = 窗口 − 摘要预留 20K − 输入缓冲 13K
  assert.equal(DEFAULT_THRESHOLDS.window - DEFAULT_THRESHOLDS.summary_reserve - 13000,
    DEFAULT_THRESHOLDS.auto)
})

test('阈值合并只接受有效正数，脏数据回落默认值', () => {
  assert.deepEqual(mergeThresholds(undefined), DEFAULT_THRESHOLDS)
  assert.deepEqual(mergeThresholds({ auto: 0, full: null, window: 'x' }), DEFAULT_THRESHOLDS)
  const merged = mergeThresholds({ window: 64000, auto: 40000 })
  assert.equal(merged.window, 64000)
  assert.equal(merged.auto, 40000)
  assert.equal(merged.full, 119040) // 未提供的字段保持默认
})

test('三条刻度按窗口比例定位，标签带 K 量纲', () => {
  const ticks = buildTicks(128000, DEFAULT_THRESHOLDS)
  assert.deepEqual(ticks.map(t => t.key), ['micro', 'auto', 'full'])
  assert.deepEqual(ticks.map(t => t.label), ['微压缩 20K', '自动 95K', '兜底 119.0K'])
  assert.equal(ticks[0].left, '15.625%')
  assert.equal(ticks[1].left, '74.21875%')
  assert.match(ticks[2].title, /兜底 119\.0K Token 触发/)
  // 窗口缺失时用阈值窗口兜底；超窗阈值被夹到 100% 不溢出堆叠条
  assert.equal(buildTicks(0, { auto: 200000 })[1].left, '100%')
})

test('距自动压缩余量：未达阈值显示还剩，已达阈值明确提示，缺数据不谎报', () => {
  assert.equal(headroomText(94857), '还剩 94.9K')
  assert.equal(headroomText(0), '已达阈值')
  assert.equal(headroomText(-1200), '已达阈值')
  assert.equal(headroomText(undefined), '--')
  assert.equal(headroomText(null), '--')
})

test('压缩类型文案覆盖四层新枚举，未知类型原样透出', () => {
  assert.equal(compactLabel('session_memory'), 'Session Memory（后台笔记）')
  assert.equal(compactLabel('auto_compact_hit'), '自动压缩·摘要命中')
  assert.equal(compactLabel('auto_compact'), '自动压缩·生成摘要')
  assert.equal(compactLabel('full_fallback'), '兜底压缩·确定性')
  assert.equal(compactLabel('manual'), '手动压缩')
  assert.equal(compactLabel('window80'), 'window80')
  assert.equal(compactLabel(''), '')
})

test('LLM span 用真实 usage：入+出 tok，缓存命中另注；无用量不渲染', () => {
  assert.equal(spanTokenText({ kind: 'llm', name: 'answer', prompt_tokens: 12000, completion_tokens: 800 }),
    '12000+800 tok')
  assert.equal(spanTokenText({ kind: 'llm', name: 'answer', prompt_tokens: 12000, completion_tokens: 800, cached_tokens: 9000 }),
    '12000+800 tok（缓存 9000）')
  assert.equal(spanTokenText({ kind: 'llm', name: 'answer' }), '')
  assert.equal(spanTokenText(null), '')
})

test('TOOL / STEP span 用估算口径并带 ≈，与账单口径区分', () => {
  assert.equal(spanTokenText({ kind: 'tool', name: 'search_knowledge', input_tokens: 40, output_tokens: 3200 }),
    '≈入 40 · 出 3200 tok')
  assert.equal(spanTokenText({ kind: 'step', name: 'rerank', input_tokens: 0, output_tokens: 0 }), '')
  // 只有入参没有结果（如 record_skip）也要能看到 Token
  assert.equal(spanTokenText({ kind: 'step', name: 'plan', input_tokens: 120 }), '≈入 120 · 出 0 tok')
})

test('压缩动作 span 展示「压缩前→压缩后」，按 name 前缀识别而非 kind', () => {
  assert.equal(spanTokenText({ kind: 'step', name: 'context.micro_compact', input_tokens: 24000, output_tokens: 9000 }),
    '24K→9K tok')
  assert.equal(spanTokenText({ kind: 'llm', name: 'context.auto_compact', input_tokens: 96000, output_tokens: 18000 }),
    '96K→18K tok')
})

test('span.meta 兼容对象与 JSON 字符串，空对象/坏 JSON 视为无标记', () => {
  assert.deepEqual(spanMeta({ meta: { offloaded: true } }), { offloaded: true })
  assert.deepEqual(spanMeta({ meta: '{"cleared":3}' }), { cleared: 3 })
  assert.equal(spanMeta({ meta: '{}' }), null)
  assert.equal(spanMeta({ meta: 'not-json' }), null)
  assert.equal(spanMeta({}), null)
})

test('压缩标记文案：落盘路径 / 去重引用 / 清除数 / 原始量 / 命中与触发原因', () => {
  assert.equal(spanMetaText({ meta: { path: 'tool-results/a.txt', offloaded: true, raw_tokens: 12000 } }),
    '落盘：tool-results/a.txt · 已落盘（上下文只留预览） · 原始 12K tok')
  assert.equal(spanMetaText({ meta: { dedup_ref: 'r1' } }), '去重引用：#r1')
  assert.equal(spanMetaText({ meta: { cleared: 4, freed: 15000 } }), '微压缩清除 4 个结果 · 释放 15K tok')
  assert.equal(spanMetaText({ meta: { hit: true, compact_kind: 'auto_compact_hit' } }),
    '摘要命中（零模型调用） · 类型：auto_compact_hit')
  assert.equal(spanMetaText({ meta: { hit: false, reason: 'pre_call' } }),
    '生成摘要（1 次模型调用） · 触发：pre_call')
  assert.equal(spanMetaText({}), '')
})

test('链路头部累计：LLM 真实量与非 LLM 估算量分列，水位取峰值（压缩后回落不掩盖占用）', () => {
  const spans = [
    { kind: 'step', name: 'plan', output_tokens: 200, ctx_tokens: 30000 },
    { kind: 'tool', name: 'search_knowledge', output_tokens: 3200, ctx_tokens: 96000 },
    { kind: 'llm', name: 'answer', prompt_tokens: 96000, completion_tokens: 900, ctx_tokens: 96000 },
    { kind: 'step', name: 'context.auto_compact', input_tokens: 96000, output_tokens: 18000, ctx_tokens: 18000 },
  ]
  const sum = traceTokenSummary(spans)
  assert.equal(sum.llm, 96900)
  // 非 LLM span 的 output_tokens 累计（含压缩动作的结果量）
  assert.equal(sum.tool, 200 + 3200 + 18000)
  assert.equal(sum.ctx, 96000)
  assert.equal(sum.window, 128000)
  assert.deepEqual(traceTokenSummary([]), { llm: 0, tool: 0, ctx: 0, window: 128000 })
  assert.deepEqual(traceTokenSummary(undefined), { llm: 0, tool: 0, ctx: 0, window: 128000 })
})