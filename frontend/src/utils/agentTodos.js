/**
 * Plan&Execute 步骤待办：把「计划步骤 + 工具进度事件」映射为用户可读的待办清单。
 *
 * 呈现契约（需求：只体现步骤说明，不呈现代理调用的方法名）：
 *   - 文案只取步骤说明 purpose，兜底检索词，再兜底「步骤 N」；绝不输出 tool 名；
 *   - 完成态由 tool 事件实时驱动：无记录=待执行、start=进行中、end=已完成/失败；
 *   - 计划步骤全部终态后追加收尾待办「整理证据并生成回答」，流式期间进行中、落库后已完成。
 */

export const TODO_STATE_LABELS = {
  pending: '待执行',
  running: '进行中',
  done: '已完成',
  failed: '失败',
}

export const todoStateLabel = state => TODO_STATE_LABELS[state] || ''

/** 步骤说明脱敏：工具名/内部阶段名一律不出现在待办文案里 */
function stepText(step, index) {
  const text = String((step && (step.purpose || step.query)) || '').trim()
  return text || `步骤 ${index + 1}`
}

/**
 * 计划步骤与工具记录配对：优先按后端步骤序号（index 从 1 开始），
 * 序号缺失（旧快照/仅 end 事件）时按未配对顺序兜底，避免错位勾选。
 */
function pairTools(plan, tools) {
  const used = new Set()
  return plan.map((_, i) => {
    let idx = tools.findIndex((t, ti) => !used.has(ti) && Number(t.index) === i + 1)
    if (idx < 0) idx = tools.findIndex((t, ti) => !used.has(ti) && (t.index === undefined || t.index === null))
    if (idx < 0) return null
    used.add(idx)
    return tools[idx]
  })
}

export function buildPlanTodos({ plan = [], tools = [], live = false, error = null } = {}) {
  const steps = Array.isArray(plan) ? plan : []
  const records = Array.isArray(tools) ? tools : []
  if (!steps.length) return []
  const paired = pairTools(steps, records)
  const items = steps.map((step, i) => {
    const rec = paired[i]
    const state = !rec ? 'pending' : rec.state === 'start' ? 'running' : rec.failed ? 'failed' : 'done'
    const summary = String((rec && rec.summary) || '').trim()
    // 复用步骤：上一轮已检索完成，本轮直接沿用结果（文案明示，避免用户以为又检索了一遍）
    const reused = !!(step && step.reused) || !!(rec && rec.reused)
    // 待授权联网步骤：计划已生成但未执行，授权通过后才真正跑（文案明示，避免误以为已检索）
    const needsAuth = !!(step && step.needs_auth) && state === 'pending'
    const base = stepText(step, i)
    const text = reused ? `${base}（复用上一轮结果）` : needsAuth ? `${base}（待授权联网搜索）` : base
    return {
      key: `step-${i}`,
      text,
      state,
      reused,
      needsAuth,
      detail: state === 'failed' ? (summary || '该步骤未取到结果') : state === 'done' ? summary : '',
      elapsed: rec && Number.isFinite(rec.elapsed_ms) ? rec.elapsed_ms : null,
    }
  })
  const settled = items.every(t => t.state === 'done' || t.state === 'failed')
  const finalState = error || !settled ? 'pending' : live ? 'running' : 'done'
  items.push({ key: 'answer', text: '整理证据并生成回答', state: finalState, detail: '', elapsed: null })
  return items
}

/** 头部摘要：x/N 步完成（k 失败），不暴露工具调用次数与内部阶段数 */
export function planTodoSummary(todos = []) {
  if (!todos.length) return ''
  const done = todos.filter(t => t.state === 'done').length
  const failed = todos.filter(t => t.state === 'failed').length
  return `${done}/${todos.length} 步完成${failed ? `（${failed} 失败）` : ''}`
}