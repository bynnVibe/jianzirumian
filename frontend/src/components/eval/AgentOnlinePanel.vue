<template>
  <div class="agent-online">
    <!-- ============ 工具条：窗口 + 刷新 ============ -->
    <div class="ao-toolbar">
      <select v-model.number="days" class="input ao-days" @change="load(true)">
        <option :value="7">近 7 天</option>
        <option :value="14">近 14 天</option>
        <option :value="30">近 30 天</option>
      </select>
      <button class="btn btn-outline sm" :disabled="loading" @click="load(true)">刷新</button>
      <span class="ao-note">
        在线模式：后台按 {{ sampleRateText }} 采样率对已结束的 chat Agent 轨迹自动评测（单轮上限 5 条），
        无参考六指标评分，质量均分回流可观测性并参与告警。共 {{ total }} 条样本，文本已脱敏。
      </span>
    </div>

    <!-- ============ 聚合卡 ============ -->
    <div v-if="agg" class="monitor-metrics">
      <div class="monitor-metric"><span class="mm-value">{{ agg.samples }}</span><span class="mm-label">窗口样本数</span></div>
      <div class="monitor-metric"><span class="mm-value" :class="scoreCls(agg.avg_task_completion)">{{ pct(agg.avg_task_completion) }}</span><span class="mm-label">平均任务完成</span></div>
      <div class="monitor-metric"><span class="mm-value" :class="scoreCls(agg.avg_tool_correctness)">{{ pct(agg.avg_tool_correctness) }}</span><span class="mm-label">平均工具正确</span></div>
      <div class="monitor-metric"><span class="mm-value" :class="scoreCls(agg.avg_step_efficiency)">{{ pct(agg.avg_step_efficiency) }}</span><span class="mm-label">平均步骤效率</span></div>
      <div class="monitor-metric"><span class="mm-value" :class="scoreCls(agg.avg_trajectory_judge)">{{ pct(agg.avg_trajectory_judge) }}</span><span class="mm-label">平均轨迹评分</span></div>
      <div class="monitor-metric"><span class="mm-value" :class="agg.low_count ? 'bad' : ''">{{ agg.low_count }}</span><span class="mm-label">低分样本（&lt;60%）</span></div>
    </div>

    <p v-if="errorMsg" class="inline-error">{{ errorMsg }}</p>

    <!-- ============ 手动单 trace 评测入口 ============ -->
    <section class="ao-card">
      <div class="ao-card-head">
        <h3>手动 trace 评测</h3>
        <span class="ao-card-sub">粘贴一条 Agent trace ID，可附期望工具序列做轨迹匹配（无参考六指标始终评估）</span>
      </div>
      <div class="manual-form">
        <label class="field span-2">
          <span class="field-label">Trace ID <em>*</em></span>
          <input class="input" v-model.trim="manualForm.traceId" placeholder="如：trc_xxxxxxxx（可观测性 → Agent 轨迹列表复制）" />
        </label>
        <label class="field span-2">
          <span class="field-label">期望工具序列（逗号分隔，有序；留空=不校验匹配）</span>
          <input class="input" v-model.trim="manualForm.tools" placeholder="如：search_knowledge, web_search" />
        </label>
        <label class="field">
          <span class="field-label">匹配模式</span>
          <select class="input" v-model="manualForm.matchMode">
            <option v-for="m in MATCH_MODES" :key="m.key" :value="m.key">{{ m.label }}</option>
          </select>
        </label>
        <label class="field ao-args-field">
          <span class="field-label">参数匹配</span>
          <span class="ao-args"><input type="checkbox" v-model="manualForm.argsMatch" /> 比较调用参数</span>
        </label>
        <div class="manual-foot">
          <button class="btn btn-primary sm" :disabled="evaluating || !manualForm.traceId" @click="doEvaluate">
            {{ evaluating ? '评测中…' : '评测该 trace' }}
          </button>
        </div>
      </div>

      <div v-if="manualResult" class="manual-result">
        <div class="mr-head">
          <span class="mr-title">评测结果</span>
          <span class="mono-id">{{ shortId(manualResult.trace_id) }}</span>
          <span v-if="manualResult.trajectory_match != null" class="chip match" :class="manualResult.trajectory_match ? 'ok' : 'bad'">
            {{ manualResult.trajectory_match ? '匹配通过' : '匹配失败' }}
          </span>
          <span class="status-pill sm" :class="manualResult.status === 'ok' ? 'finished' : 'failed'">{{ manualResult.status === 'ok' ? '成功' : '错误' }}</span>
        </div>
        <div class="rc-scores">
          <span v-for="m in METRICS" :key="m.key" class="rc-score" :title="m.layer">
            <em>{{ m.label }}</em>
            <b :class="scoreCls(manualResult[m.key])">{{ pct(manualResult[m.key]) }}</b>
          </span>
        </div>
        <ul v-if="reasonLines(manualResult).length" class="reason-list">
          <li v-for="(r, i) in reasonLines(manualResult)" :key="i"><b>{{ r.label }}：</b>{{ r.text }}</li>
        </ul>
      </div>
    </section>

    <!-- ============ 在线评测明细 ============ -->
    <div v-if="loading && !items.length" class="mini-loading">加载在线 Agent 评测中…</div>
    <div v-else-if="!items.length" class="empty-block">
      <p>暂无在线 Agent 评测样本。发起 Agent 对话后后台会自动采样评测，稍后刷新即可查看；也可用上方手动入口评测指定 trace。</p>
    </div>
    <table v-else class="data-table ao-table">
      <thead>
        <tr>
          <th>时间</th><th>Trace</th><th style="width:34%">输入（脱敏）</th>
          <th>任务完成</th><th>工具正确</th><th>步骤效率</th><th>轨迹评分</th><th>状态</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="row in items" :key="row.id">
          <tr class="ao-row" @click="toggleExpand(row.id)">
            <td class="cell-time"><span class="exp-caret" :class="{ open: expanded.has(row.id) }">▸</span>{{ row.created_at }}</td>
            <td><span class="mono-id">{{ shortId(row.trace_id) }}</span></td>
            <td class="q-cell">{{ row.input || '(空)' }}</td>
            <td><span class="score" :class="scoreCls(row.task_completion)">{{ pct(row.task_completion) }}</span></td>
            <td><span class="score" :class="scoreCls(row.tool_correctness)">{{ pct(row.tool_correctness) }}</span></td>
            <td><span class="score" :class="scoreCls(row.step_efficiency)">{{ pct(row.step_efficiency) }}</span></td>
            <td><span class="score" :class="scoreCls(row.trajectory_judge)">{{ pct(row.trajectory_judge) }}</span></td>
            <td><span class="status-pill sm" :class="row.status === 'ok' ? 'finished' : 'failed'">{{ row.status === 'ok' ? '成功' : '跳过/错误' }}</span></td>
          </tr>
          <tr v-if="expanded.has(row.id)" class="expand-row">
            <td colspan="8">
              <div class="expand-inner">
                <div class="d-block">
                  <span class="d-title">工具序列（{{ (row.tools_called || []).length }}）</span>
                  <div v-if="row.tools_called && row.tools_called.length" class="tool-seq">
                    <span v-for="(t, i) in row.tools_called" :key="i" class="tool-chip" :class="{ err: t.status === 'error' }" :title="toolArgs(t)">
                      {{ i + 1 }}. {{ t.name }}
                    </span>
                  </div>
                  <span v-else class="muted">无工具调用</span>
                </div>
                <div class="d-block">
                  <span class="d-title">回答摘要（脱敏）</span>
                  <div class="answer-box">{{ row.answer || '(空)' }}</div>
                </div>
                <div class="d-block">
                  <span class="d-title">六指标</span>
                  <div class="rc-scores">
                    <span v-for="m in METRICS" :key="m.key" class="rc-score" :title="m.layer">
                      <em>{{ m.label }}</em>
                      <b :class="scoreCls(row[m.key])">{{ pct(row[m.key]) }}</b>
                    </span>
                  </div>
                </div>
                <div v-if="reasonLines(row).length" class="d-block">
                  <span class="d-title">评分理由</span>
                  <ul class="reason-list">
                    <li v-for="(r, i) in reasonLines(row)" :key="i"><b>{{ r.label }}：</b>{{ r.text }}</li>
                  </ul>
                </div>
                <div class="d-block">
                  <button class="btn btn-ghost xs" @click="prefillManual(row)">用此 trace 做手动评测（附期望）</button>
                </div>
              </div>
            </td>
          </tr>
        </template>
      </tbody>
    </table>

    <div v-if="total > items.length || offset > 0" class="ao-pager">
      <button class="btn btn-outline sm" :disabled="offset <= 0 || loading" @click="page(-1)">上一页</button>
      <span class="ao-page-text">{{ offset + 1 }}–{{ Math.min(offset + items.length, total) }} / {{ total }}</span>
      <button class="btn btn-outline sm" :disabled="offset + items.length >= total || loading" @click="page(1)">下一页</button>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { getAgentOnlineEval, evaluateAgentTrace } from '@/api'

const MATCH_MODES = [
  { key: 'superset', label: '超集（实际⊇期望）' },
  { key: 'strict', label: '严格（有序相等）' },
  { key: 'unordered', label: '无序（多重集相等）' },
  { key: 'subset', label: '子集（实际⊆期望）' },
]
const METRICS = [
  { key: 'task_completion', label: '任务完成', layer: '执行层 TaskCompletion' },
  { key: 'step_efficiency', label: '步骤效率', layer: '执行层 StepEfficiency' },
  { key: 'tool_correctness', label: '工具正确', layer: '动作层 ToolCorrectness' },
  { key: 'argument_correctness', label: '参数正确', layer: '动作层 ArgumentCorrectness' },
  { key: 'plan_quality', label: '规划质量', layer: '推理层 PlanQuality' },
  { key: 'plan_adherence', label: '规划遵循', layer: '推理层 PlanAdherence' },
  { key: 'trajectory_judge', label: '轨迹评分', layer: 'AgentEvals 整体轨迹 judge' },
]
const PAGE_SIZE = 50
const sampleRateText = '10%'

const days = ref(7)
const agg = ref(null)
const items = ref([])
const total = ref(0)
const offset = ref(0)
const loading = ref(false)
const errorMsg = ref('')
const expanded = ref(new Set())

const manualForm = reactive({ traceId: '', tools: '', matchMode: 'superset', argsMatch: false })
const manualResult = ref(null)
const evaluating = ref(false)

onMounted(() => load(true))

function errMsg(e, fallback) {
  return e?.response?.data?.message || e?.response?.data?.detail || e?.message || fallback
}

async function load(reset = false) {
  if (reset) { offset.value = 0; expanded.value = new Set() }
  loading.value = true
  errorMsg.value = ''
  try {
    const res = await getAgentOnlineEval(days.value, PAGE_SIZE, offset.value)
    agg.value = res.data.aggregate || null
    items.value = res.data.items || []
    total.value = res.data.total || 0
  } catch (e) {
    errorMsg.value = errMsg(e, '加载在线 Agent 评测失败')
  } finally {
    loading.value = false
  }
}

function page(dir) {
  const next = offset.value + dir * PAGE_SIZE
  if (next < 0 || next >= total.value) return
  offset.value = next
  load()
}

function toggleExpand(id) {
  const next = new Set(expanded.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expanded.value = next
}

function prefillManual(row) {
  manualForm.traceId = row.trace_id || ''
  manualResult.value = null
  errorMsg.value = ''
}

async function doEvaluate() {
  evaluating.value = true
  errorMsg.value = ''
  manualResult.value = null
  try {
    const tools = manualForm.tools.split(/[,，]/).map((s) => s.trim()).filter(Boolean)
    const res = await evaluateAgentTrace(manualForm.traceId, {
      expectedTools: tools.length ? tools : undefined,
      matchMode: manualForm.matchMode,
      argsMatch: manualForm.argsMatch,
    })
    manualResult.value = res.data
    await load(true)
  } catch (e) {
    errorMsg.value = errMsg(e, '手动 trace 评测失败')
  } finally {
    evaluating.value = false
  }
}

function toolArgs(t) { return t?.args ? JSON.stringify(t.args) : '' }
function shortId(id) { return id ? String(id).slice(0, 12) : '' }

function reasonLines(row) {
  const md = row?.metric_details || {}
  const out = []
  if (md.task_completion?.reason) out.push({ label: '任务完成', text: md.task_completion.reason })
  if (md.trajectory_judge?.reason) out.push({ label: '轨迹评分', text: md.trajectory_judge.reason })
  if (md.plan?.plan_quality_reason) out.push({ label: '规划质量', text: md.plan.plan_quality_reason })
  if (md.plan?.plan_adherence_reason) out.push({ label: '规划遵循', text: md.plan.plan_adherence_reason })
  if (md.plan?.basis === 'no_plan_default') out.push({ label: '规划', text: '轨迹无 plan 步骤，按 DeepEval 默认记 1' })
  if (md.step_efficiency?.duplicate_penalty != null) out.push({ label: '步骤效率', text: `重复调用惩罚系数 ${md.step_efficiency.duplicate_penalty}` })
  return out
}

function pct(v) { return v == null ? '—' : (v * 100).toFixed(0) + '%' }
function scoreCls(v) {
  if (v == null) return 'na'
  if (v >= 0.8) return 'good'
  if (v >= 0.5) return 'mid'
  return 'low'
}
</script>

<style scoped>
.agent-online { display: flex; flex-direction: column; gap: 14px; }
.ao-toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.ao-days { width: 130px; flex: none; }
.ao-note { font-size: 12px; color: var(--color-text-secondary); line-height: 1.55; flex: 1; min-width: 240px; }

.inline-error { background: #fdecea; border: 1px solid #f5c6c0; color: #c0392b; font-size: 12.5px; padding: 9px 13px; border-radius: 10px; }
.mini-loading { padding: 26px; text-align: center; color: var(--color-text-secondary); font-size: 12.5px; }
.empty-block { border: 1px dashed var(--color-border); border-radius: 14px; padding: 36px 16px; text-align: center; color: var(--color-text-secondary); font-size: 12.5px; line-height: 1.6; background: rgba(255,255,255,0.5); }
.muted { color: #b9ad9e; }
.mono-id { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; color: var(--color-text-secondary); }

.monitor-metrics { display: flex; gap: 8px; flex-wrap: wrap; }
.monitor-metric { background: #faf7f2; border-radius: 9px; padding: 8px 12px; min-width: 84px; display: flex; flex-direction: column; align-items: center; gap: 1px; }
.mm-value { font-size: 15px; font-weight: 700; font-variant-numeric: tabular-nums; }
.mm-value.bad, .mm-value.low { color: #c0392b; }
.mm-value.good { color: #2e7d46; }
.mm-value.mid { color: #b5822a; }
.mm-value.na { color: var(--color-text-secondary); }
.mm-label { font-size: 10.5px; color: var(--color-text-secondary); }

.ao-card { background: #fff; border: 1px solid var(--color-border); border-radius: 14px; padding: 16px 18px; }
.ao-card-head { display: flex; align-items: baseline; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; }
.ao-card-head h3 { font-size: 14px; font-weight: 700; margin: 0; }
.ao-card-sub { font-size: 11.5px; color: var(--color-text-secondary); }
.manual-form { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.field { display: flex; flex-direction: column; gap: 5px; }
.field.span-2 { grid-column: span 2; }
.field-label { font-size: 11.5px; font-weight: 600; color: var(--color-text-secondary); }
.field-label em { color: #c0392b; font-style: normal; }
.ao-args-field { justify-content: flex-start; }
.ao-args { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; cursor: pointer; color: var(--color-text); padding: 9px 0; }
.ao-args input { accent-color: var(--color-primary); }
.manual-foot { grid-column: span 2; display: flex; justify-content: flex-end; }

.manual-result { margin-top: 14px; padding-top: 14px; border-top: 1px dashed var(--color-border); display: flex; flex-direction: column; gap: 10px; }
.mr-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.mr-title { font-size: 13px; font-weight: 700; }
.chip.match { font-size: 11px; font-weight: 600; padding: 2px 9px; border-radius: 6px; white-space: nowrap; }
.chip.match.ok { background: #e6f4ea; color: #2e7d46; }
.chip.match.bad { background: #fdecea; color: #c0392b; }

.data-table { width: 100%; border-collapse: collapse; font-size: 12.5px; background: #fff; border: 1px solid var(--color-border); border-radius: 12px; overflow: hidden; }
.data-table th, .data-table td { padding: 9px 10px; text-align: left; border-bottom: 1px solid var(--color-border); vertical-align: top; }
.data-table th { font-weight: 600; color: var(--color-text-secondary); white-space: nowrap; background: rgba(0,0,0,0.03); }
.data-table tbody tr:last-child td { border-bottom: none; }
.ao-row { cursor: pointer; }
.ao-row:hover { background: #fdfaf5; }
.q-cell { line-height: 1.5; }
.cell-time { white-space: nowrap; color: var(--color-text-secondary); }
.exp-caret { display: inline-block; margin-right: 5px; color: var(--color-primary); transition: transform 0.15s; font-size: 11px; }
.exp-caret.open { transform: rotate(90deg); }
.expand-row td { background: #fbf8f3; padding: 0; }
.expand-inner { display: flex; flex-direction: column; gap: 12px; padding: 14px 16px; }
.d-block { display: flex; flex-direction: column; gap: 6px; }
.d-title { font-size: 11px; font-weight: 700; color: var(--color-text-secondary); }
.tool-seq { display: flex; flex-wrap: wrap; gap: 4px; }
.tool-chip { background: #e3edfd; color: #1d4ed8; font-size: 11px; font-weight: 600; padding: 2px 9px; border-radius: 6px; }
.tool-chip.err { background: #fdecea; color: #c0392b; }
.answer-box { background: #fff; border: 1px solid var(--color-border); border-radius: 8px; padding: 9px 11px; font-size: 12.5px; line-height: 1.6; white-space: pre-wrap; max-height: 200px; overflow-y: auto; }
.reason-list { margin: 0; padding-left: 16px; display: flex; flex-direction: column; gap: 4px; font-size: 12px; line-height: 1.55; }

.rc-scores { display: flex; gap: 10px; flex-wrap: wrap; }
.rc-score { display: inline-flex; align-items: center; gap: 4px; font-size: 11.5px; }
.rc-score em { font-style: normal; color: var(--color-text-secondary); }
.rc-score b { font-variant-numeric: tabular-nums; }
.rc-score b.good { color: #2e7d46; }
.rc-score b.mid { color: #b5822a; }
.rc-score b.low { color: #c0392b; }
.rc-score b.na { color: var(--color-text-secondary); font-weight: 500; }
.score { font-weight: 700; font-variant-numeric: tabular-nums; }
.score.good { color: #2e7d46; }
.score.mid { color: #b5822a; }
.score.low { color: #c0392b; }
.score.na { color: var(--color-text-secondary); font-weight: 500; }
.status-pill { font-size: 11px; font-weight: 700; padding: 3px 10px; border-radius: 20px; white-space: nowrap; background: #ede6dc; color: var(--color-text-secondary); }
.status-pill.sm { padding: 2px 8px; }
.status-pill.finished { background: #e6f4ea; color: #2e7d46; }
.status-pill.failed { background: #fdecea; color: #c0392b; }

.ao-pager { display: flex; align-items: center; justify-content: center; gap: 12px; }
.ao-page-text { font-size: 12px; color: var(--color-text-secondary); font-variant-numeric: tabular-nums; }

.input { width: 100%; padding: 9px 11px; font-size: 13px; border: 1px solid var(--color-border); border-radius: 9px; background: #fffdfa; color: var(--color-text); outline: none; font-family: inherit; box-sizing: border-box; }
.input:focus { border-color: var(--color-primary); background: #fff; }
select.input { appearance: auto; background: #fff; }

.btn { display: inline-flex; align-items: center; gap: 6px; border: 1px solid transparent; cursor: pointer; border-radius: 9px; font-weight: 600; font-size: 13px; padding: 9px 16px; transition: all 0.16s; font-family: inherit; }
.btn.sm { padding: 6px 12px; font-size: 12.5px; }
.btn.xs { padding: 4px 10px; font-size: 11.5px; }
.btn:disabled { opacity: 0.55; cursor: not-allowed; }
.btn-primary { background: linear-gradient(135deg, #c98a4b, #d9a76a); color: #fff; }
.btn-primary:hover:not(:disabled) { background: linear-gradient(135deg, #b0763c, #cf9a55); }
.btn-outline { background: #fff; border-color: var(--color-border); color: var(--color-text-secondary); }
.btn-outline:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
.btn-ghost { background: transparent; color: var(--color-text-secondary); }
.btn-ghost:hover:not(:disabled) { background: #f3ece2; color: var(--color-primary); }

@media (max-width: 768px) {
  .manual-form { grid-template-columns: 1fr; }
  .field.span-2, .manual-foot { grid-column: span 1; }
}
</style>