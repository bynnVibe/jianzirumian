<template>
  <div class="kb-eval">
    <!-- 用例区 -->
    <section class="eval-section">
      <div class="section-bar">
        <h3>评估用例 <span class="count">{{ cases.length }}</span></h3>
        <div class="bar-actions">
          <button class="btn-tool" :disabled="generating" @click="generate">{{ generating ? '生成中…' : '自动生成用例' }}</button>
          <button class="btn-tool" @click="openCaseForm">添加用例</button>
          <button class="btn-dark" :disabled="starting || !cases.length || hasRunning" @click="startRun">
            {{ starting ? '启动中…' : hasRunning ? '评估进行中…' : '开始评估' }}
          </button>
        </div>
      </div>
      <p v-if="caseMessage" class="msg" :class="{ error: caseMessageError }">{{ caseMessage }}</p>
      <div v-if="!cases.length" class="empty-hint">暂无用例：可手动添加，或基于知识库内容自动生成</div>
      <div v-else class="case-list">
        <div v-for="c in cases" :key="c.id" class="case-item">
          <div class="case-main">
            <span class="case-q">{{ c.question }}</span>
            <span v-if="c.reference_answer" class="case-ref" :title="c.reference_answer">参考答案：{{ c.reference_answer }}</span>
            <span v-if="c.expected_keywords?.length" class="case-kws">
              <i v-for="k in c.expected_keywords" :key="k">{{ k }}</i>
            </span>
          </div>
          <button class="link-btn danger" @click="removeCase(c)">删除</button>
        </div>
      </div>
    </section>

    <!-- 运行区 -->
    <section class="eval-section">
      <div class="section-bar">
        <h3>评估运行</h3>
        <button class="btn-tool" :disabled="runsLoading" @click="loadRuns">刷新</button>
      </div>
      <div v-if="!runs.length" class="empty-hint">暂无运行记录</div>
      <div v-else class="run-list">
        <div
          v-for="r in runs" :key="r.id"
          class="run-item" :class="{ active: selectedRunId === r.id }"
          @click="openRun(r)"
        >
          <span class="run-time">{{ formatTime(r.started_at) }}</span>
          <span class="status-badge" :class="r.status">{{ statusLabel(r.status) }}</span>
          <template v-if="r.status === 'finished' && r.summary">
            <span class="run-metric">命中 {{ pct(r.summary.retrieval_hit_rate) }}</span>
            <span class="run-metric">忠实度 {{ pct(r.summary.avg_faithfulness) }}</span>
            <span class="run-metric">相关度 {{ pct(r.summary.avg_answer_relevance) }}</span>
            <span class="run-metric">{{ r.summary.total }} 例 / {{ r.summary.error || 0 }} 失败</span>
          </template>
          <span v-else-if="r.status === 'running'" class="run-metric">评估进行中，自动刷新…</span>
        </div>
      </div>

      <!-- 运行详情 -->
      <div v-if="runDetail" class="run-detail">
        <div v-if="runDetail.summary" class="summary-cards">
          <div class="summary-card"><b>{{ pct(runDetail.summary.retrieval_hit_rate) }}</b><span>检索命中率</span></div>
          <div class="summary-card"><b>{{ pct(runDetail.summary.avg_faithfulness) }}</b><span>忠实度</span></div>
          <div class="summary-card"><b>{{ pct(runDetail.summary.avg_answer_relevance) }}</b><span>答案相关性</span></div>
          <div class="summary-card"><b>{{ pct(runDetail.summary.avg_context_precision) }}</b><span>上下文精确率</span></div>
          <div class="summary-card"><b>{{ pct(runDetail.summary.avg_context_recall) }}</b><span>上下文召回率</span></div>
          <div class="summary-card"><b>{{ pct(runDetail.summary.avg_judge_score) }}</b><span>评审得分</span></div>
          <div class="summary-card"><b>{{ Math.round(runDetail.summary.avg_latency_ms || 0) }}ms</b><span>平均耗时</span></div>
        </div>
        <table class="result-table">
          <thead>
            <tr>
              <th>问题</th><th>状态</th><th>命中</th><th>忠实度</th><th>相关性</th><th>评审</th><th>耗时</th><th></th>
            </tr>
          </thead>
          <tbody>
            <template v-for="r in runDetail.results || []" :key="r.id">
              <tr>
                <td class="q-cell" :title="r.question">{{ r.question }}</td>
                <td><span class="status-badge" :class="r.status">{{ r.status === 'ok' ? '成功' : '失败' }}</span></td>
                <td>{{ r.retrieval_hit ? '✓' : '✗' }}</td>
                <td>{{ pct(r.faithfulness) }}</td>
                <td>{{ pct(r.answer_relevance) }}</td>
                <td>{{ pct(r.judge_score) }}</td>
                <td>{{ r.latency_ms }}ms</td>
                <td><button class="link-btn" @click="expandedId = expandedId === r.id ? '' : r.id">{{ expandedId === r.id ? '收起' : '详情' }}</button></td>
              </tr>
              <tr v-if="expandedId === r.id" class="expand-row">
                <td colspan="8">
                  <div v-if="r.error" class="expand-error">{{ r.error }}</div>
                  <div v-if="r.answer" class="expand-block"><b>回答</b><p>{{ r.answer }}</p></div>
                  <div v-if="r.judge_reason" class="expand-block"><b>评审理由</b><p>{{ r.judge_reason }}</p></div>
                  <div v-if="r.contexts?.length" class="expand-block">
                    <b>检索上下文（{{ r.contexts.length }}）</b>
                    <p v-for="(c, i) in r.contexts" :key="i" class="ctx-item">{{ typeof c === 'string' ? c : (c.text || JSON.stringify(c)) }}</p>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
    </section>

    <!-- 添加用例弹窗 -->
    <div v-if="caseFormVisible" class="modal-mask" @click.self="caseFormVisible = false">
      <div class="modal-card">
        <h3>添加评估用例</h3>
        <label class="field-label">问题</label>
        <textarea v-model="caseForm.question" class="field-input" rows="2" placeholder="例如：糖尿病患者的饮食原则是什么？"></textarea>
        <label class="field-label">参考答案（可选，用于召回率/评审）</label>
        <textarea v-model="caseForm.reference" class="field-input" rows="3"></textarea>
        <label class="field-label">期望关键词（可选，逗号分隔，用于命中率）</label>
        <input v-model="caseForm.keywords" class="field-input" placeholder="血糖, 膳食纤维" />
        <p v-if="formError" class="form-error">{{ formError }}</p>
        <div class="modal-actions">
          <button class="btn-ghost" @click="caseFormVisible = false">取消</button>
          <button class="btn-dark" :disabled="saving" @click="submitCase">添加</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  listKbEvalCases, addKbEvalCase, deleteKbEvalCase, generateKbEvalCases,
  startKbEvalRun, listKbEvalRuns, getKbEvalRun,
} from '@/api'

const props = defineProps({
  kbId: { type: String, required: true },
})

const cases = ref([])
const runs = ref([])
const runsLoading = ref(false)
const generating = ref(false)
const starting = ref(false)
const caseMessage = ref('')
const caseMessageError = ref(false)
const selectedRunId = ref('')
const runDetail = ref(null)
const expandedId = ref('')

const hasRunning = computed(() => runs.value.some((r) => r.status === 'running'))

function pct(v) {
  if (v == null) return '-'
  return `${Math.round(v * 100)}%`
}

function formatTime(t) {
  return t ? String(t).replace('T', ' ').slice(0, 16) : ''
}

function statusLabel(s) {
  return { running: '进行中', finished: '已完成', failed: '失败' }[s] || s
}

async function loadCases() {
  try {
    const res = await listKbEvalCases(props.kbId)
    cases.value = res.data.cases || []
  } catch (e) {
    caseMessage.value = e.response?.data?.detail || '加载用例失败'
    caseMessageError.value = true
  }
}

async function loadRuns() {
  runsLoading.value = true
  try {
    const res = await listKbEvalRuns(props.kbId)
    runs.value = res.data.runs || []
    if (selectedRunId.value) {
      const sel = runs.value.find((r) => r.id === selectedRunId.value)
      if (sel && sel.status !== 'running') await openRun(sel, true)
    }
  } catch (e) {
    console.error('加载评估运行失败:', e)
  } finally {
    runsLoading.value = false
  }
}

async function openRun(r, keepSelection = false) {
  if (!keepSelection) {
    selectedRunId.value = r.id
    runDetail.value = null
    expandedId.value = ''
  }
  if (r.status === 'running') return
  try {
    const res = await getKbEvalRun(props.kbId, r.id)
    if (selectedRunId.value === r.id) runDetail.value = res.data.run || null
  } catch (e) {
    console.error('加载运行详情失败:', e)
  }
}

async function generate() {
  generating.value = true
  caseMessage.value = ''
  try {
    const res = await generateKbEvalCases(props.kbId)
    caseMessage.value = `已生成 ${res.data.cases?.length || 0} 条用例`
    caseMessageError.value = false
    await loadCases()
  } catch (e) {
    caseMessage.value = e.response?.data?.detail || '生成失败'
    caseMessageError.value = true
  } finally {
    generating.value = false
  }
}

async function startRun() {
  starting.value = true
  caseMessage.value = ''
  try {
    await startKbEvalRun(props.kbId)
    caseMessage.value = '评估已启动'
    caseMessageError.value = false
    await loadRuns()
  } catch (e) {
    caseMessage.value = e.response?.data?.detail || '启动失败'
    caseMessageError.value = true
  } finally {
    starting.value = false
  }
}

async function removeCase(c) {
  if (!window.confirm(`确定删除用例「${c.question.slice(0, 30)}」？`)) return
  try {
    await deleteKbEvalCase(props.kbId, c.id)
    await loadCases()
  } catch (e) {
    caseMessage.value = e.response?.data?.detail || '删除失败'
    caseMessageError.value = true
  }
}

const caseFormVisible = ref(false)
const saving = ref(false)
const formError = ref('')
const caseForm = ref({ question: '', reference: '', keywords: '' })

function openCaseForm() {
  caseForm.value = { question: '', reference: '', keywords: '' }
  formError.value = ''
  caseFormVisible.value = true
}

async function submitCase() {
  if (!caseForm.value.question.trim()) { formError.value = '请输入问题'; return }
  saving.value = true
  formError.value = ''
  try {
    await addKbEvalCase(props.kbId, {
      question: caseForm.value.question.trim(),
      referenceAnswer: caseForm.value.reference.trim(),
      expectedKeywords: caseForm.value.keywords.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
    })
    caseFormVisible.value = false
    await loadCases()
  } catch (e) {
    formError.value = e.response?.data?.detail || '添加失败'
  } finally {
    saving.value = false
  }
}

let pollTimer = null
onMounted(async () => {
  await Promise.all([loadCases(), loadRuns()])
  pollTimer = setInterval(() => { if (hasRunning.value) loadRuns() }, 3000)
})
onUnmounted(() => { if (pollTimer) clearInterval(pollTimer) })
</script>

<style scoped>
.kb-eval { padding: 20px 24px; height: 100%; overflow-y: auto; box-sizing: border-box; }

.eval-section { margin-bottom: 26px; }
.section-bar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; gap: 10px; flex-wrap: wrap; }
.section-bar h3 { margin: 0; font-size: 15px; font-weight: 600; color: var(--color-text); }
.count { font-size: 12px; color: var(--color-text-secondary); font-weight: 400; }
.bar-actions { display: flex; gap: 8px; }

.btn-tool { border: 1px solid var(--color-border); background: #fff; border-radius: 8px; padding: 6px 13px; font-size: 12.5px; color: var(--color-text); cursor: pointer; }
.btn-tool:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
.btn-tool:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-dark { background: var(--color-text); color: #fff; border: none; border-radius: 8px; padding: 6px 16px; font-size: 12.5px; cursor: pointer; }
.btn-dark:hover:not(:disabled) { opacity: 0.85; }
.btn-dark:disabled { opacity: 0.5; cursor: not-allowed; }

.msg { font-size: 12.5px; color: var(--color-text-secondary); margin: 0 0 10px; }
.msg.error { color: #c0392b; }
.empty-hint { padding: 18px; border: 1px dashed var(--color-border); border-radius: 10px; text-align: center; color: var(--color-text-secondary); font-size: 12.5px; }

.case-list { display: flex; flex-direction: column; gap: 8px; }
.case-item { display: flex; align-items: center; gap: 10px; background: #fff; border: 1px solid var(--color-border); border-radius: 8px; padding: 10px 14px; }
.case-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.case-q { font-size: 13px; font-weight: 500; color: var(--color-text); }
.case-ref { font-size: 11.5px; color: var(--color-text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.case-kws { display: flex; gap: 4px; flex-wrap: wrap; }
.case-kws i { font-style: normal; font-size: 10.5px; background: var(--color-primary-light); color: var(--color-primary); padding: 1px 6px; border-radius: 4px; }
.link-btn { border: none; background: none; color: var(--color-primary); font-size: 12px; cursor: pointer; flex-shrink: 0; }
.link-btn.danger { color: #c0392b; }
.link-btn:hover { text-decoration: underline; }

.run-list { display: flex; flex-direction: column; gap: 6px; }
.run-item {
  display: flex; align-items: center; gap: 12px; background: #fff;
  border: 1px solid var(--color-border); border-radius: 8px; padding: 9px 14px;
  cursor: pointer; font-size: 12.5px; flex-wrap: wrap;
}
.run-item:hover { border-color: var(--color-primary); }
.run-item.active { border-color: var(--color-primary); background: #fdf8f1; }
.run-time { color: var(--color-text); font-weight: 500; }
.run-metric { color: var(--color-text-secondary); font-size: 12px; }

.status-badge { font-size: 11px; padding: 2px 8px; border-radius: 6px; background: #f3efe9; color: var(--color-text-secondary); }
.status-badge.running { background: #fff8e1; color: #b8860b; }
.status-badge.finished, .status-badge.ok { background: #e8f5e9; color: #2e7d32; }
.status-badge.failed, .status-badge.error { background: #fdecea; color: #c0392b; }

.run-detail { margin-top: 14px; }
.summary-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 8px; margin-bottom: 14px; }
.summary-card { background: #fff; border: 1px solid var(--color-border); border-radius: 8px; padding: 10px; text-align: center; display: flex; flex-direction: column; gap: 3px; }
.summary-card b { font-size: 16px; color: var(--color-primary); }
.summary-card span { font-size: 11px; color: var(--color-text-secondary); }

.result-table { width: 100%; border-collapse: collapse; background: #fff; border: 1px solid var(--color-border); border-radius: 8px; overflow: hidden; }
.result-table th { text-align: left; font-size: 11.5px; color: var(--color-text-secondary); background: var(--color-sidebar); padding: 8px 10px; border-bottom: 1px solid var(--color-border); }
.result-table td { padding: 8px 10px; font-size: 12px; color: var(--color-text); border-bottom: 1px solid #f3efe9; }
.q-cell { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.expand-row td { background: #fdfaf5; }
.expand-block { margin-bottom: 8px; }
.expand-block b { font-size: 11.5px; color: var(--color-text-secondary); }
.expand-block p { margin: 4px 0 0; font-size: 12px; line-height: 1.6; color: #5a4f42; white-space: pre-wrap; word-break: break-word; }
.ctx-item { border-left: 2px solid var(--color-border); padding-left: 8px; margin-top: 6px; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.expand-error { color: #c0392b; font-size: 12px; margin-bottom: 8px; }

.modal-mask { position: fixed; inset: 0; background: rgba(45, 42, 36, 0.4); display: flex; align-items: center; justify-content: center; z-index: 100; }
.modal-card { background: #fff; border-radius: 14px; padding: 22px 24px; width: 440px; max-width: calc(100vw - 40px); box-shadow: 0 12px 40px rgba(0, 0, 0, 0.15); }
.modal-card h3 { margin: 0 0 12px; font-size: 16px; }
.field-label { display: block; font-size: 12.5px; color: var(--color-text-secondary); margin: 10px 0 5px; }
.field-input { width: 100%; box-sizing: border-box; border: 1px solid var(--color-border); border-radius: 8px; padding: 8px 11px; font-size: 13px; outline: none; font-family: inherit; color: var(--color-text); }
.field-input:focus { border-color: var(--color-primary); }
.form-error { color: #c0392b; font-size: 12.5px; margin: 10px 0 0; }
.modal-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 18px; }
.btn-ghost { background: transparent; border: 1px solid var(--color-border); border-radius: 8px; padding: 7px 16px; font-size: 13px; cursor: pointer; color: var(--color-text); }
</style>