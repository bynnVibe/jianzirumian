/**
 * 上下文四层压缩的展示口径（纯函数，供 ContextUsageBadge / ObservabilityView 共用）。
 *
 * 量纲统一：一个会话一个 128K 连续窗口，K = 1000（整千不带小数，128K 而非 131.1K）。
 * Token 展示分三类口径：
 *   - LLM span：上游真实 usage（prompt + completion，缓存命中另注）；
 *   - TOOL / STEP span：估算口径，带 ≈ 前缀，避免与账单混淆；
 *   - context.* 压缩动作 span：压缩前 → 压缩后。
 */

// 四层阈值默认值（与后端 context_window.thresholds() 同源；接口缺字段时兜底）
export const DEFAULT_THRESHOLDS = {
  window: 128000,
  output_cap: 4000,
  micro_trigger: 20000,
  auto: 95000,
  full: 119040,
  summary_reserve: 20000,
}

/** 量纲统一 K=1000：整千不带小数（128K），非整千保留一位（119.0K） */
export function fmtTok(n) {
  const v = Number(n || 0)
  if (!Number.isFinite(v)) return '0'
  if (v < 1000) return String(v)
  const k = v / 1000
  return Number.isInteger(k) ? `${k}K` : `${k.toFixed(1)}K`
}

/** 接口阈值与默认值合并（只接受有限数值，避免脏数据把刻度算飞） */
export function mergeThresholds(raw) {
  const out = { ...DEFAULT_THRESHOLDS }
  for (const key of Object.keys(out)) {
    const v = Number(raw?.[key])
    if (Number.isFinite(v) && v > 0) out[key] = v
  }
  return out
}

/** 三条阈值刻度：微压缩进入 20K / 自动压缩 95K / 兜底压缩 119K（按窗口比例定位） */
export function buildTicks(contextWindow, thresholds) {
  const t = mergeThresholds(thresholds)
  const w = Number(contextWindow) || t.window || 1
  const defs = [
    { key: 'micro', label: `微压缩 ${fmtTok(t.micro_trigger)}`, at: t.micro_trigger },
    { key: 'auto', label: `自动 ${fmtTok(t.auto)}`, at: t.auto },
    { key: 'full', label: `兜底 ${fmtTok(t.full)}`, at: t.full },
  ]
  return defs.map((d) => ({
    ...d,
    left: `${Math.max(0, Math.min(100, (d.at / w) * 100))}%`,
    title: `${d.label} Token 触发`,
  }))
}

/** 距自动压缩余量文案：未达阈值显示还剩多少，已达阈值明确提示 */
export function headroomText(left) {
  if (left === undefined || left === null || left === '') return '--'
  const n = Number(left)
  if (!Number.isFinite(n)) return '--'
  return n > 0 ? `还剩 ${fmtTok(n)}` : '已达阈值'
}

// 压缩类型中文展示名（与后端 compact_kind / 事件 kind 枚举同源）
export const COMPACT_LABELS = {
  session_memory: 'Session Memory（后台笔记）',
  auto_compact_hit: '自动压缩·摘要命中',
  auto_compact: '自动压缩·生成摘要',
  full: '兜底压缩',
  full_fallback: '兜底压缩·确定性',
  manual: '手动压缩',
  note: '用户编辑笔记',
}

export function compactLabel(kind) {
  return COMPACT_LABELS[kind] || kind || ''
}

/** 每个 span 的 Token 文案；无用量数据返回空串（模板据此不渲染分隔点） */
export function spanTokenText(span) {
  if (!span) return ''
  const name = span.name || ''
  if (name.startsWith('context.')) {
    return `${fmtTok(span.input_tokens)}→${fmtTok(span.output_tokens)} tok`
  }
  if (span.kind === 'llm') {
    const pt = Number(span.prompt_tokens || 0)
    const ct = Number(span.completion_tokens || 0)
    if (!pt && !ct) return ''
    const cached = Number(span.cached_tokens || 0)
    return `${pt}+${ct} tok${cached ? `（缓存 ${cached}）` : ''}`
  }
  const it = Number(span.input_tokens || 0)
  const ot = Number(span.output_tokens || 0)
  if (!it && !ot) return ''
  return `≈入 ${it} · 出 ${ot} tok`
}

/** span.meta 兼容对象与 JSON 字符串两种落库形态；空对象视为无标记 */
export function spanMeta(span) {
  const raw = span?.meta
  if (!raw) return null
  if (typeof raw === 'object') return Object.keys(raw).length ? raw : null
  try {
    const obj = JSON.parse(raw)
    return obj && typeof obj === 'object' && Object.keys(obj).length ? obj : null
  } catch {
    return null
  }
}

/** 压缩标记文案：落盘路径 / 去重引用 / 微压缩清除 / 原始大小 / 命中与触发原因 */
export function spanMetaText(span) {
  const m = spanMeta(span)
  if (!m) return ''
  const bits = []
  if (m.path) bits.push(`落盘：${m.path}`)
  if (m.offloaded) bits.push('已落盘（上下文只留预览）')
  if (m.dedup_ref) bits.push(`去重引用：#${m.dedup_ref}`)
  if (m.cleared) bits.push(`微压缩清除 ${m.cleared} 个结果`)
  if (m.freed) bits.push(`释放 ${fmtTok(m.freed)} tok`)
  if (m.raw_tokens) bits.push(`原始 ${fmtTok(m.raw_tokens)} tok`)
  if (m.hit !== undefined) bits.push(m.hit ? '摘要命中（零模型调用）' : '生成摘要（1 次模型调用）')
  if (m.compact_kind) bits.push(`类型：${m.compact_kind}`)
  if (m.reason) bits.push(`触发：${m.reason}`)
  return bits.join(' · ')
}

/**
 * 链路弹窗头部累计：LLM 真实 Token / 非 LLM span 的结果估算 Token / 窗口水位峰值。
 * 水位取整条链路最大值（压缩后水位会回落，峰值才代表本次请求的真实占用）。
 */
export function traceTokenSummary(spans) {
  let llm = 0
  let tool = 0
  let ctx = 0
  for (const s of spans || []) {
    if (s.kind === 'llm') llm += Number(s.prompt_tokens || 0) + Number(s.completion_tokens || 0)
    else tool += Number(s.output_tokens || 0)
    if (Number(s.ctx_tokens || 0) > ctx) ctx = Number(s.ctx_tokens || 0)
  }
  return { llm, tool, ctx, window: DEFAULT_THRESHOLDS.window }
}