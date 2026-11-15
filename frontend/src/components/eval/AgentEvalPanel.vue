<template>
  <div class="agent-eval">
    <!-- ============ 工具条：数据集 + 匹配模式 + 运行 ============ -->
    <div class="ae-toolbar">
      <select class="input ae-ds" v-model="selectedDatasetId" @change="onSelectDataset">
        <option value="" disabled>选择 Agent 评测集</option>
        <option v-for="d in agentDatasets" :key="d.id" :value="d.id">{{ d.name }}（{{ d.case_count }} 用例）</option>
      </select>
      <button class="btn btn-outline sm" @click="openCreateDataset">＋ 新建 Agent 评测集</button>
      <div class="ae-toolbar-spacer"></div>
      <label class="ae-mode">
        <span class="ae-mode-l">匹配模式</span>
        <select class="input ae-mode-sel" v-model="matchMode">
          <option v-for="m in MATCH_MODES" :key="m.key" :value="m.key">{{ m.label }}</option>
        </select>
      </label>
      <label class="ae-args">
        <input type="checkbox" v-model="argsMatch" /> 参数匹配
      </label>
      <button class="btn btn-primary sm" :disabled="starting || !selectedDatasetId || !cases.length" @click="startRun">
        {{ starting ? '发起中…' : '发起轨迹评测' }}
      </button>
      <button class="btn btn-ghost sm" :disabled="loading" @click="reloadAll">刷新</button>
    </div>
    <p class="ae-hint">
      离线模式：对 kind=agent 评测集逐用例真实运行 Agent（react + 知识库 + 联网），按 AgentEvals 轨迹匹配与 DeepEval 六指标评分。
      匹配模式：<b>严格</b>=有序相等 · <b>无序</b>=多重集相等 · <b>子集</b>=实际⊆期望 · <b>超集</b>=实际⊇期望。
    </p>

    <p v-if="errorMsg" class="inline-error">{{ errorMsg }}</p>

    <div v-if="!selectedDatasetId" class="empty-block">
      <p>选择或新建一个 Agent 评测集开始轨迹评测。用例需填写期望工具序列（有序）与期望结果。</p>
    </div>

    <template v-else>
      <!-- ============ 用例管理 ============ -->
      <section class="ae-card">
        <div class="ae-card-head">
          <h3>用例管理 <span class="count-pill">{{ cases.length }}</span></h3>
        </div>
        <div class="case-form">
          <label class="field span-2">
            <span class="field-label">问题 / 任务 <em>*</em></span>
            <input class="input" v-model.trim="caseForm.question" placeholder="如：帮我查一下失眠的食疗方案" />
          </label>
          <div class="field span-2">
            <span class="field-label">期望工具序列（有序，可增删移；留空=不校验轨迹匹配）</span>
            <div class="tool-editor">
              <div v-for="(t, i) in caseForm.tools" :key="i" class="tool-row">
                <span class="tool-idx">{{ i + 1 }}</span>
                <input class="input tool-input" v-model.trim="caseForm.tools[i]" list="ae-tool-options" placeholder="工具名" />
                <button class="icon-btn" title="上移" :disabled="i === 0" @click="moveTool(i, -1)">↑</button>
                <button class="icon-btn" title="下移" :disabled="i === caseForm.tools.length - 1" @click="moveTool(i, 1)">↓</button>
                <button class="icon-btn danger" title="删除" @click="removeTool(i)">✕</button>
              </div>
              <button class="btn btn-outline xs" @click="addTool">＋ 添加工具</button>
              <datalist id="ae-tool-options">
                <option value="search_knowledge" /><option value="web_search" />
              </datalist>
            </div>
          </div>
          <label class="field span-2">
            <span class="field-label">期望结果（用于任务完成度评判）</span>
            <textarea class="input textarea" v-model.trim="caseForm.expected_outcome" rows="2" placeholder="如：给出小米粥等食疗建议"></textarea>
          </label>
          <div class="case-form-foot">
            <button class="btn btn-primary sm" :disabled="addingCase || !caseForm.question" @click="addCase">
              {{ addingCase ? '添加中…' : '添加用例' }}
            </button>
          </div>
        </div>

        <div v-if="loadingCases" class="mini-loading">加载用例中…</div>
        <table v-else-if="cases.length" class="data-table">
          <thead>
            <tr><th style="width:34%">问题</th><th>期望工具序列</th><th>期望结果</th><th style="width:48px"></th></tr>
          </thead>
          <tbody>
            <tr v-for="c in cases" :key="c.id">
              <td class="q-cell">{{ c.question }}</td>
              <td>
                <span v-if="c.expected_tools && c.expected_tools.length" class="tool-seq">
                  <span v-for="(t, i) in c.expected_tools" :key="i" class="tool-chip">{{ toolName(t) }}</span>
                </span>
                <span v-else class="muted">—</span>
              </td>
              <td><span class="muted-clip">{{ c.expected_outcome || '—' }}</span></td>
              <td>
                <button class="icon-btn danger" title="删除" @click="removeCase(c)">✕</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty-block sm"><p>暂无用例，先添加</p></div>
      </section>

      <!-- ============ 运行历史 ============ -->
      <section class="ae-card">
        <div class="ae-card-head">
          <h3>运行历史</h3>
          <span v-if="hasRunning" class="live-dot"></span>
        </div>
        <div v-if="loadingRuns && !runs.length" class="mini-loading">加载中…</div>
        <table v-else-if="runs.length" class="data-table">
          <thead>
            <tr><th>开始时间</th><th>状态</th><th>进度</th><th>匹配通过率</th><th>任务完成</th><th>工具正确</th><th>步骤效率</th><th>轨迹评分</th></tr>
          </thead>
          <tbody>
            <tr v-for="r in runs" :key="r.id" class="run-row" @click="openRun(r.id)">
              <td class="cell-time">{{ fmtTime(r.started_at) }}</td>
              <td><span class="status-pill sm" :class="r.status">{{ statusLabel(r.status) }}</span></td>
              <td>{{ r.completed }}/{{ r.summary.total != null ? r.summary.total : '—' }}</td>
              <td><span class="score" :class="scoreCls(r.summary.match_pass_rate)">{{ pct(r.summary.match_pass_rate) }}</span></td>
              <td><span class="score" :class="scoreCls(r.summary.avg_task_completion)">{{ pct(r.summary.avg_task_completion) }}</span></td>
              <td><span class="score" :class="scoreCls(r.summary.avg_tool_correctness)">{{ pct(r.summary.avg_tool_correctness) }}</span></td>
              <td><span class="score" :class="scoreCls(r.summary.avg_step_efficiency)">{{ pct(r.summary.avg_step_efficiency) }}</span></td>
              <td><span class="score" :class="scoreCls(r.summary.avg_trajectory_judge)">{{ pct(r.summary.avg_trajectory_judge) }}</span></td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty-block sm"><p>还没有运行记录，点击「发起轨迹评测」开始</p></div>
      </section>
    </template>

    <!-- ============ 结果抽屉 ============ -->
    <div v-if="runDetail" class="drawer-overlay" @click.self="closeRun">
      <aside class="drawer wide">
        <div class="drawer-head">
          <span class="drawer-title">轨迹评测结果</span>
          <span class="status-pill sm" :class="runDetail.run.status">{{ statusLabel(runDetail.run.status) }}</span>
          <span class="drawer-sub">{{ runDetail.dataset_name }}</span>
          <button class="icon-btn" title="关闭" @click="closeRun">✕</button>
        </div>
        <div class="drawer-body">
          <div v-if="summaryCards.length" class="summary-metrics">
            <div v-for="m in summaryCards" :key="m.label" class="sm-item">
              <span class="sm-v" :class="m.cls">{{ m.value }}</span>
              <span class="sm-l">{{ m.label }}</span>
            </div>
          </div>

          <div v-if="!runDetail.results.length" class="mini-loading">暂无用例结果，运行产出后自动刷新。</div>
          <div v-for="row in runDetail.results" :key="row.id" class="result-card">
            <div class="rc-head" @click="toggleExpand(row.id)">
              <span class="exp-caret" :class="{ open: expanded.has(row.id) }">▸</span>
              <span class="rc-q">{{ row.question }}</span>
              <span v-if="row.trajectory_match != null" class="chip match" :class="row.trajectory_match ? 'ok' : 'bad'">
                {{ row.trajectory_match ? '匹配通过' : '匹配失败' }} · {{ matchModeLabel(row.match_mode) }}
              </span>
              <span class="status-pill sm" :class="row.status === 'ok' ? 'finished' : 'failed'">{{ row.status === 'ok' ? '成功' : '错误' }}</span>
            </div>
            <div class="rc-scores">
              <span v-for="m in METRICS" :key="m.key" class="rc-score" :title="m.layer">
                <em>{{ m.label }}</em>
                <b :class="scoreCls(row[m.key])">{{ pct(row[m.key]) }}</b>
              </span>
            </div>
            <div v-if="expanded.has(row.id)" class="rc-detail">
              <div v-if="row.error" class="err-line">错误：{{ row.error }}</div>
              <div class="d-block">
                <span class="d-title">工具序列（实际 vs 期望）</span>
                <div class="seq-compare">
                  <div class="seq-col">
                    <span class="seq-l">实际调用</span>
                    <div v-if="row.tools_called.length" class="tool-seq">
                      <span v-for="(t, i) in row.tools_called" :key="i" class="tool-chip" :class="{ err: t.status === 'error' }" :title="toolArgs(t)">
                        {{ i + 1 }}. {{ t.name }}
                      </span>
                    </div>
                    <span v-else class="muted">无工具调用</span>
                  </div>
                  <div class="seq-col">
                    <span class="seq-l">期望序列</span>
                    <div v-if="row.expected_tools.length" class="tool-seq">
                      <span v-for="(t, i) in row.expected_tools" :key="i" class="tool-chip exp">{{ i + 1 }}. {{ toolName(t) }}</span>
                    </div>
                    <span v-else class="muted">无期望（不校验匹配）</span>
                  </div>
                </div>
              </div>
              <div class="d-block">
                <span class="d-title">最终回答</span>
                <div class="answer-box">{{ row.answer || '(空)' }}</div>
              </div>
              <div v-if="reasonLines(row).length" class="d-block">
                <span class="d-title">评分理由</span>
                <ul class="reason-list">
                  <li v-for="(r, i) in reasonLines(row)" :key="i"><b>{{ r.label }}：</b>{{ r.text }}</li>
                </ul>
              </div>
            </div>
          </div>
        </div>
      </aside>
    </div>

    <!-- ============ 新建数据集弹窗 ============ -->
    <div v-if="showCreateDataset" class="modal-overlay" @click.self="showCreateDataset = false">
      <div class="modal-box">
        <h3 class="modal-title">新建 Agent 评测集</h3>
        <p class="modal-sub">kind=agent，用于轨迹评测。创建后添加用例（问题 + 期望工具序列 + 期望结果）。</p>
        <label class="field">
          <span class="field-label">名称 <em>*</em></span>
          <input class="input" v-model.trim="datasetForm.name" placeholder="如：Agent 工具调用回归集" />
        </label>
        <label class="field">
          <span class="field-label">描述</span>
          <textarea class="input textarea" v-model.trim="datasetForm.description" rows="2"></textarea>
        </label>
        <div class="modal-actions">
          <button class="btn btn-outline" @click="showCreateDataset = false">取消</button>
          <button class="btn btn-primary" :disabled="creatingDataset || !datasetForm.name" @click="createDataset">
            {{ creatingDataset ? '创建中…' : '创建' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount } from 'vue'
import {
  listEvalDatasets, createEvalDataset, listEvalCases, createEvalCase, deleteEvalCase,
  startAgentEvalRun, listAgentEvalRuns, getAgentEvalRun,
} from '@/api'

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

const datasets = ref([])
const selectedDatasetId = ref('')
const cases = ref([])
const loadingCases = ref(false)
const addingCase = ref(false)
const caseForm = reactive({ question: '', tools: [], expected_outcome: '' })
const matchMode = ref('superset')
const argsMatch = ref(false)
const runs = ref([])
const loadingRuns = ref(false)
const starting = ref(false)
const loading = ref(false)
const errorMsg = ref('')
const runDetail = ref(null)
const expanded = ref(new Set())
let pollTimer = null

const agentDatasets = computed(() => datasets.value.filter((d) => (d.kind || 'rag') === 'agent'))
const hasRunning = computed(() => runs.value.some((r) => r.status === 'running'))

const summaryCards = computed(() => {
  const s = runDetail.value?.run?.summary || {}
  if (s.total == null) return []
  const cards = [
    { label: '用例', value: s.total },
    { label: '成功', value: s.ok ?? 0 },
    { label: '失败', value: s.error ?? 0, cls: s.error ? 'bad' : '' },
    { label: '匹配通过率', value: pct(s.match_pass_rate), cls: scoreCls(s.match_pass_rate) },
  ]
  for (const m of METRICS) cards.push({ label: m.label, value: pct(s['avg_' + m.key]), cls: scoreCls(s['avg_' + m.key]) })
  return cards
})

onMounted(async () => {
  await loadDatasets()
  if (agentDatasets.value.length) {
    selectedDatasetId.value = agentDatasets.value[0].id
    await onSelectDataset()
  }
})
onBeforeUnmount(stopPolling)

function errMsg(e, fallback) {
  return e?.response?.data?.message || e?.response?.data?.detail || e?.message || fallback
}
function toolName(t) { return typeof t === 'string' ? t : (t?.name || '') }
function toolArgs(t) { return t?.args ? JSON.stringify(t.args) : '' }
function matchModeLabel(mode) { return (MATCH_MODES.find((m) => m.key === mode) || {}).label || mode }

async function loadDatasets() {
  try {
    const res = await listEvalDatasets()
    datasets.value = res.data.datasets || []
  } catch (e) {
    errorMsg.value = errMsg(e, '加载评测集失败')
  }
}

async function onSelectDataset() {
  stopPolling()
  runDetail.value = null
  expanded.value = new Set()
  errorMsg.value = ''
  await Promise.all([loadCases(), loadRuns()])
}

async function reloadAll() {
  loading.value = true
  await loadDatasets()
  if (selectedDatasetId.value) await Promise.all([loadCases(), loadRuns()])
  loading.value = false
}

async function loadCases() {
  if (!selectedDatasetId.value) return
  loadingCases.value = true
  try {
    const res = await listEvalCases(selectedDatasetId.value)
    cases.value = res.data.cases || []
  } catch (e) {
    errorMsg.value = errMsg(e, '加载用例失败')
  } finally {
    loadingCases.value = false
  }
}

function addTool() { caseForm.tools.push('') }
function removeTool(i) { caseForm.tools.splice(i, 1) }
function moveTool(i, dir) {
  const j = i + dir
  if (j < 0 || j >= caseForm.tools.length) return
  const arr = caseForm.tools
  ;[arr[i], arr[j]] = [arr[j], arr[i]]
}

async function addCase() {
  addingCase.value = true
  errorMsg.value = ''
  try {
    const tools = caseForm.tools.map((t) => t.trim()).filter(Boolean)
    const res = await createEvalCase(selectedDatasetId.value, {
      question: caseForm.question,
      expected_tools: tools,
      expected_outcome: caseForm.expected_outcome,
    })
    cases.value.push(res.data.case)
    caseForm.question = ''
    caseForm.tools = []
    caseForm.expected_outcome = ''
    const d = datasets.value.find((x) => x.id === selectedDatasetId.value)
    if (d) d.case_count = cases.value.length
  } catch (e) {
    errorMsg.value = errMsg(e, '添加用例失败')
  } finally {
    addingCase.value = false
  }
}

async function removeCase(c) {
  if (!window.confirm('确认删除该用例？')) return
  try {
    await deleteEvalCase(c.id)
    cases.value = cases.value.filter((x) => x.id !== c.id)
    const d = datasets.value.find((x) => x.id === selectedDatasetId.value)
    if (d) d.case_count = cases.value.length
  } catch (e) {
    errorMsg.value = errMsg(e, '删除用例失败')
  }
}

async function startRun() {
  starting.value = true
  errorMsg.value = ''
  try {
    await startAgentEvalRun({ datasetId: selectedDatasetId.value, matchMode: matchMode.value, argsMatch: argsMatch.value })
    await loadRuns()
    managePolling()
  } catch (e) {
    errorMsg.value = e?.response?.status === 409
      ? (e.response.data?.message || '已有 Agent 评测在执行中，请等待完成')
      : errMsg(e, '发起轨迹评测失败')
  } finally {
    starting.value = false
  }
}

async function loadRuns() {
  if (!selectedDatasetId.value) return
  loadingRuns.value = true
  try {
    const res = await listAgentEvalRuns(selectedDatasetId.value)
    runs.value = res.data.runs || []
    managePolling()
  } catch (e) {
    errorMsg.value = errMsg(e, '加载运行历史失败')
  } finally {
    loadingRuns.value = false
  }
}

async function openRun(runId) {
  try {
    const res = await getAgentEvalRun(runId)
    runDetail.value = res.data.run
    expanded.value = new Set()
    managePolling()
  } catch (e) {
    errorMsg.value = errMsg(e, '加载运行详情失败')
  }
}

function closeRun() {
  runDetail.value = null
  managePolling()
}

function toggleExpand(id) {
  const next = new Set(expanded.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expanded.value = next
}

function managePolling() {
  const running = hasRunning.value || runDetail.value?.run?.status === 'running'
  if (running && !pollTimer) pollTimer = setInterval(pollTick, 3000)
  else if (!running && pollTimer) stopPolling()
}

async function pollTick() {
  try {
    await loadRuns()
    if (runDetail.value) {
      const res = await getAgentEvalRun(runDetail.value.run.id)
      runDetail.value = res.data.run
    }
  } catch (e) { /* 静默重试 */ }
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

function reasonLines(row) {
  const md = row.metric_details || {}
  const out = []
  if (md.task_completion?.reason) out.push({ label: '任务完成', text: md.task_completion.reason })
  if (md.trajectory_judge?.reason) out.push({ label: '轨迹评分', text: md.trajectory_judge.reason })
  if (md.plan?.plan_quality_reason) out.push({ label: '规划质量', text: md.plan.plan_quality_reason })
  if (md.plan?.plan_adherence_reason) out.push({ label: '规划遵循', text: md.plan.plan_adherence_reason })
  if (md.plan?.basis === 'no_plan_default') out.push({ label: '规划', text: '轨迹无 plan 步骤，按 DeepEval 默认记 1' })
  if (md.step_efficiency?.duplicate_penalty != null) out.push({ label: '步骤效率', text: `重复调用惩罚系数 ${md.step_efficiency.duplicate_penalty}` })
  return out
}

// ---- 新建数据集 ----
const showCreateDataset = ref(false)
const creatingDataset = ref(false)
const datasetForm = reactive({ name: '', description: '' })

function openCreateDataset() {
  datasetForm.name = ''
  datasetForm.description = ''
  showCreateDataset.value = true
}

async function createDataset() {
  creatingDataset.value = true
  errorMsg.value = ''
  try {
    const res = await createEvalDataset({ name: datasetForm.name, description: datasetForm.description, kind: 'agent' })
    showCreateDataset.value = false
    await loadDatasets()
    selectedDatasetId.value = res.data.dataset.id
    await onSelectDataset()
  } catch (e) {
    errorMsg.value = errMsg(e, '创建评测集失败')
  } finally {
    creatingDataset.value = false
  }
}

// ---- 格式化 ----
function pct(v) { return v == null ? '—' : (v * 100).toFixed(0) + '%' }
function scoreCls(v) {
  if (v == null) return 'na'
  if (v >= 0.8) return 'good'
  if (v >= 0.5) return 'mid'
  return 'low'
}
function statusLabel(st) { return { running: '执行中', finished: '已完成', failed: '失败' }[st] || st }
function fmtTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}
</script>

<style scoped>
.agent-eval { display: flex; flex-direction: column; gap: 14px; }
.ae-toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.ae-ds { width: 260px; flex: none; }
.ae-toolbar-spacer { flex: 1; }
.ae-mode { display: inline-flex; align-items: center; gap: 6px; }
.ae-mode-l { font-size: 12px; color: var(--color-text-secondary); }
.ae-mode-sel { width: 180px; }
.ae-args { display: inline-flex; align-items: center; gap: 5px; font-size: 12.5px; cursor: pointer; color: var(--color-text); }
.ae-args input { accent-color: var(--color-primary); }
.ae-hint { font-size: 12px; color: var(--color-text-secondary); line-height: 1.6; margin: 0; }
.ae-hint b { color: var(--color-text); }

.inline-error { background: #fdecea; border: 1px solid #f5c6c0; color: #c0392b; font-size: 12.5px; padding: 9px 13px; border-radius: 10px; }
.mini-loading { padding: 22px; text-align: center; color: var(--color-text-secondary); font-size: 12.5px; }
.empty-block { border: 1px dashed var(--color-border); border-radius: 14px; padding: 36px 16px; text-align: center; color: var(--color-text-secondary); font-size: 12.5px; line-height: 1.6; background: rgba(255,255,255,0.5); }
.empty-block.sm { padding: 22px 16px; }
.muted { color: #b9ad9e; }
.muted-clip { color: var(--color-text-secondary); display: inline-block; max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.ae-card { background: #fff; border: 1px solid var(--color-border); border-radius: 14px; padding: 16px 18px; }
.ae-card-head { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.ae-card-head h3 { font-size: 14px; font-weight: 700; margin: 0; }
.count-pill { font-size: 11px; background: #ede6dc; color: var(--color-text-secondary); padding: 1px 8px; border-radius: 8px; }
.live-dot { width: 7px; height: 7px; border-radius: 50%; background: #e0a038; animation: pulse 1.2s infinite; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }

.case-form { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; padding-bottom: 14px; border-bottom: 1px dashed var(--color-border); margin-bottom: 12px; }
.field { display: flex; flex-direction: column; gap: 5px; }
.field.span-2 { grid-column: span 2; }
.field-label { font-size: 11.5px; font-weight: 600; color: var(--color-text-secondary); }
.field-label em { color: #c0392b; font-style: normal; }
.case-form-foot { grid-column: span 2; display: flex; justify-content: flex-end; }
.tool-editor { display: flex; flex-direction: column; gap: 6px; align-items: flex-start; }
.tool-row { display: flex; align-items: center; gap: 6px; width: 100%; }
.tool-idx { font-size: 11px; color: var(--color-text-secondary); width: 18px; text-align: center; }
.tool-input { flex: 1; }

.data-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.data-table th, .data-table td { padding: 9px 10px; text-align: left; border-bottom: 1px solid var(--color-border); vertical-align: top; }
.data-table th { font-weight: 600; color: var(--color-text-secondary); white-space: nowrap; background: rgba(0,0,0,0.02); }
.data-table tbody tr:last-child td { border-bottom: none; }
.q-cell { line-height: 1.5; }
.cell-time { white-space: nowrap; color: var(--color-text-secondary); }
.run-row { cursor: pointer; }
.run-row:hover { background: #fdfaf5; }
.tool-seq { display: flex; flex-wrap: wrap; gap: 4px; }
.tool-chip { background: #e3edfd; color: #1d4ed8; font-size: 11px; font-weight: 600; padding: 2px 9px; border-radius: 6px; }
.tool-chip.exp { background: #f3e8fa; color: #8a4bb8; }
.tool-chip.err { background: #fdecea; color: #c0392b; }

.score { font-weight: 700; font-variant-numeric: tabular-nums; }
.score.good { color: #2e7d46; }
.score.mid { color: #b5822a; }
.score.low { color: #c0392b; }
.score.na { color: var(--color-text-secondary); font-weight: 500; }
.status-pill { font-size: 11px; font-weight: 700; padding: 3px 10px; border-radius: 20px; white-space: nowrap; background: #ede6dc; color: var(--color-text-secondary); }
.status-pill.sm { padding: 2px 8px; }
.status-pill.running { background: #fdf0d8; color: #b5822a; }
.status-pill.finished { background: #e6f4ea; color: #2e7d46; }
.status-pill.failed { background: #fdecea; color: #c0392b; }

/* ---- 抽屉 ---- */
.drawer-overlay { position: fixed; inset: 0; background: rgba(45,42,36,0.4); display: flex; justify-content: flex-end; z-index: 100; }
.drawer { width: 640px; max-width: 96vw; height: 100%; background: #fff; display: flex; flex-direction: column; box-shadow: -8px 0 32px rgba(0,0,0,0.16); }
.drawer.wide { width: 720px; }
.drawer-head { display: flex; align-items: center; gap: 10px; padding: 16px 20px; border-bottom: 1px solid var(--color-border); }
.drawer-title { font-size: 15px; font-weight: 700; }
.drawer-sub { font-size: 12px; color: var(--color-text-secondary); }
.drawer-head .icon-btn { margin-left: auto; }
.drawer-body { flex: 1; overflow-y: auto; padding: 18px 20px; display: flex; flex-direction: column; gap: 14px; }
.summary-metrics { display: flex; gap: 8px; flex-wrap: wrap; }
.sm-item { background: #faf7f2; border-radius: 9px; padding: 8px 12px; min-width: 72px; display: flex; flex-direction: column; align-items: center; gap: 1px; }
.sm-v { font-size: 15px; font-weight: 700; font-variant-numeric: tabular-nums; }
.sm-v.good { color: #2e7d46; }
.sm-v.mid { color: #b5822a; }
.sm-v.low, .sm-v.bad { color: #c0392b; }
.sm-v.na { color: var(--color-text-secondary); }
.sm-l { font-size: 10.5px; color: var(--color-text-secondary); }

.result-card { border: 1px solid var(--color-border); border-radius: 12px; padding: 12px 14px; display: flex; flex-direction: column; gap: 8px; }
.rc-head { display: flex; align-items: center; gap: 8px; cursor: pointer; }
.rc-q { flex: 1; font-size: 13px; font-weight: 600; line-height: 1.45; }
.exp-caret { display: inline-block; color: var(--color-primary); transition: transform 0.15s; font-size: 11px; }
.exp-caret.open { transform: rotate(90deg); }
.chip.match { font-size: 11px; font-weight: 600; padding: 2px 9px; border-radius: 6px; white-space: nowrap; }
.chip.match.ok { background: #e6f4ea; color: #2e7d46; }
.chip.match.bad { background: #fdecea; color: #c0392b; }
.rc-scores { display: flex; gap: 10px; flex-wrap: wrap; }
.rc-score { display: inline-flex; align-items: center; gap: 4px; font-size: 11.5px; }
.rc-score em { font-style: normal; color: var(--color-text-secondary); }
.rc-score b { font-variant-numeric: tabular-nums; }
.rc-score b.good { color: #2e7d46; }
.rc-score b.mid { color: #b5822a; }
.rc-score b.low { color: #c0392b; }
.rc-score b.na { color: var(--color-text-secondary); font-weight: 500; }
.rc-detail { display: flex; flex-direction: column; gap: 12px; padding-top: 8px; border-top: 1px dashed var(--color-border); }
.err-line { color: #c0392b; font-size: 12px; background: #fdecea; padding: 7px 10px; border-radius: 8px; }
.d-block { display: flex; flex-direction: column; gap: 6px; }
.d-title { font-size: 11px; font-weight: 700; color: var(--color-text-secondary); }
.seq-compare { display: flex; gap: 16px; flex-wrap: wrap; }
.seq-col { display: flex; flex-direction: column; gap: 5px; flex: 1; min-width: 200px; }
.seq-l { font-size: 11px; color: var(--color-text-secondary); }
.answer-box { background: #fffdfa; border: 1px solid var(--color-border); border-radius: 8px; padding: 9px 11px; font-size: 12.5px; line-height: 1.6; white-space: pre-wrap; max-height: 200px; overflow-y: auto; }
.reason-list { margin: 0; padding-left: 16px; display: flex; flex-direction: column; gap: 4px; font-size: 12px; line-height: 1.55; }

/* ---- 弹窗 ---- */
.modal-overlay { position: fixed; inset: 0; background: rgba(45,42,36,0.4); display: flex; align-items: center; justify-content: center; z-index: 110; }
.modal-box { width: 440px; max-width: 92vw; background: #fff; border-radius: 16px; padding: 22px 24px; box-shadow: 0 16px 48px rgba(0,0,0,0.2); display: flex; flex-direction: column; gap: 12px; }
.modal-title { font-size: 16px; font-weight: 700; }
.modal-sub { margin: -4px 0 0; font-size: 12px; color: var(--color-text-secondary); line-height: 1.55; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 4px; }

.input { width: 100%; padding: 9px 11px; font-size: 13px; border: 1px solid var(--color-border); border-radius: 9px; background: #fffdfa; color: var(--color-text); outline: none; font-family: inherit; box-sizing: border-box; }
.input:focus { border-color: var(--color-primary); background: #fff; }
.textarea { resize: vertical; line-height: 1.55; }
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
.icon-btn { width: 26px; height: 26px; display: inline-flex; align-items: center; justify-content: center; border: none; background: transparent; border-radius: 7px; cursor: pointer; color: var(--color-text-secondary); font-size: 13px; }
.icon-btn:hover:not(:disabled) { background: #f0e7da; color: var(--color-primary); }
.icon-btn.danger:hover:not(:disabled) { background: #fdecea; color: #c0392b; }
.icon-btn:disabled { opacity: 0.35; cursor: not-allowed; }
</style>