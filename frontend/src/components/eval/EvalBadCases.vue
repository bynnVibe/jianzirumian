<template>
  <div class="badcases">
    <!-- 过滤条 + 分类统计 -->
    <div class="bc-toolbar">
      <select class="input bc-select" v-model="filters.status" @change="reload(true)">
        <option value="">全部状态</option>
        <option value="open">待处理</option>
        <option value="reproduced">已复现</option>
        <option value="fixed">已修复</option>
        <option value="ignored">已忽略</option>
      </select>
      <select class="input bc-select" v-model="filters.category" @change="reload(true)">
        <option value="">全部分类</option>
        <option v-for="(label, key) in CATEGORY_LABELS" :key="key" :value="key">{{ label }}</option>
      </select>
      <select class="input bc-select" v-model="filters.source_type" @change="reload(true)">
        <option value="">全部来源</option>
        <option v-for="(label, key) in SOURCE_LABELS" :key="key" :value="key">{{ label }}</option>
      </select>
      <button class="btn btn-outline sm" :disabled="loading" @click="reload(true)">刷新</button>
      <span class="bc-note">多源自动收集低分/失败用例，归因后可转评测用例跑回归闭环。文本已脱敏。</span>
    </div>

    <div v-if="hasCounts" class="bc-stats">
      <button
        v-for="(label, key) in CATEGORY_LABELS" :key="key"
        class="bc-stat" :class="{ active: filters.category === key, empty: !categoryCounts[key] }"
        @click="toggleCategory(key)"
      >
        <span class="bc-stat-n">{{ categoryCounts[key] || 0 }}</span>
        <span class="bc-stat-l">{{ label }}</span>
      </button>
    </div>

    <p v-if="errorMsg" class="inline-error">{{ errorMsg }}</p>
    <div v-if="loading && !items.length" class="mini-loading">加载 BadCase 中…</div>
    <div v-else-if="!items.length" class="empty-block">
      <p>暂无 BadCase。离线/在线评测、点踩反馈、空回答与 Agent 低分会被自动收集到这里。</p>
    </div>

    <table v-else class="data-table bc-table">
      <thead>
        <tr>
          <th>时间</th><th>来源</th><th>分类</th><th style="width:34%">问题（脱敏）</th>
          <th>状态</th><th>关联用例</th><th style="width:120px">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in items" :key="row.id" class="bc-row" @click="openDetail(row)">
          <td class="cell-time">{{ row.created_at }}</td>
          <td><span class="chip src">{{ SOURCE_LABELS[row.source_type] || row.source_type }}</span></td>
          <td><span class="chip cat" :class="'cat-' + row.category">{{ CATEGORY_LABELS[row.category] || row.category }}</span></td>
          <td class="q-cell">{{ row.question || '(空)' }}</td>
          <td><span class="status-pill sm" :class="'st-' + row.status">{{ STATUS_LABELS[row.status] || row.status }}</span></td>
          <td>
            <span v-if="row.case_id" class="chip case" title="已转评测用例">已转用例</span>
            <span v-else class="muted">—</span>
          </td>
          <td @click.stop>
            <button class="btn btn-ghost xs" @click="openDetail(row)">详情</button>
            <button v-if="row.status === 'open' || row.status === 'reproduced'" class="btn btn-ghost xs" @click="openToCase(row)">转用例</button>
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="total > items.length || offset > 0" class="bc-pager">
      <button class="btn btn-outline sm" :disabled="offset <= 0 || loading" @click="page(-1)">上一页</button>
      <span class="bc-page-text">{{ offset + 1 }}–{{ Math.min(offset + items.length, total) }} / {{ total }}</span>
      <button class="btn btn-outline sm" :disabled="offset + items.length >= total || loading" @click="page(1)">下一页</button>
    </div>

    <!-- ============ 详情抽屉 ============ -->
    <div v-if="detail" class="drawer-overlay" @click.self="detail = null">
      <aside class="drawer">
        <div class="drawer-head">
          <span class="drawer-title">BadCase 详情</span>
          <span class="chip cat" :class="'cat-' + detail.category">{{ CATEGORY_LABELS[detail.category] || detail.category }}</span>
          <span class="status-pill sm" :class="'st-' + detail.status">{{ STATUS_LABELS[detail.status] || detail.status }}</span>
          <button class="icon-btn" title="关闭" @click="detail = null">✕</button>
        </div>
        <div class="drawer-body">
          <div class="d-block">
            <span class="d-title">问题（脱敏）</span>
            <div class="answer-box">{{ detail.question || '(空)' }}</div>
          </div>
          <div class="d-block">
            <span class="d-title">回答摘要（脱敏）</span>
            <div class="answer-box">{{ detail.answer || '(空)' }}</div>
          </div>

          <div class="d-block">
            <span class="d-title">指标快照</span>
            <div v-if="metricEntries.length" class="metric-snap">
              <div v-for="m in metricEntries" :key="m.key" class="snap-item">
                <span class="snap-v" :class="m.cls">{{ m.text }}</span>
                <span class="snap-l">{{ m.label }}</span>
              </div>
            </div>
            <div v-else class="muted">无指标快照</div>
          </div>

          <div class="d-block">
            <span class="d-title">
              归因分析
              <em v-if="detail.analysis && detail.analysis.degraded" class="degraded-tag">规则模板</em>
              <button class="btn btn-ghost xs" :disabled="analyzing" @click="doAnalyze">
                {{ analyzing ? '分析中…' : 'LLM 增强归因' }}
              </button>
            </span>
            <div class="answer-box cause">{{ (detail.analysis && detail.analysis.cause) || '暂无归因，点击「LLM 增强归因」生成。' }}</div>
            <ul v-if="detail.analysis && detail.analysis.suggestions && detail.analysis.suggestions.length" class="sugg-list">
              <li v-for="(s, i) in detail.analysis.suggestions" :key="i">{{ s }}</li>
            </ul>
          </div>

          <div class="d-block">
            <span class="d-title">关联</span>
            <div class="rel-line">
              <span class="muted">来源：</span>{{ SOURCE_LABELS[detail.source_type] || detail.source_type }}
              <span class="mono-id">{{ detail.source_id }}</span>
            </div>
            <div v-if="detail.run_id" class="rel-line">
              <span class="muted">评测运行：</span>
              <button v-if="canLinkRun" class="link-btn" @click="goRun(detail.run_id)">{{ shortId(detail.run_id) }}</button>
              <span v-else class="mono-id">{{ shortId(detail.run_id) }}</span>
            </div>
            <div v-if="detail.repro_run_id" class="rel-line">
              <span class="muted">复现运行：</span>
              <button v-if="canLinkRun" class="link-btn" @click="goRun(detail.repro_run_id)">{{ shortId(detail.repro_run_id) }}</button>
              <span v-else class="mono-id">{{ shortId(detail.repro_run_id) }}</span>
              <span class="chip case">回归通过</span>
            </div>
            <div v-if="detail.case_id" class="rel-line">
              <span class="muted">已转用例：</span><span class="mono-id">{{ shortId(detail.case_id) }}</span>
            </div>
            <div v-if="detail.note" class="rel-line"><span class="muted">备注：</span>{{ detail.note }}</div>
          </div>
        </div>
        <div class="drawer-foot">
          <button v-if="!detail.case_id" class="btn btn-primary sm" @click="openToCase(detail)">转评测用例</button>
          <button v-if="detail.status !== 'fixed'" class="btn btn-outline sm" @click="doMark('fixed')">标记已修复</button>
          <button v-if="detail.status !== 'ignored'" class="btn btn-outline sm" @click="doMark('ignored')">忽略</button>
          <button v-if="detail.status !== 'open'" class="btn btn-ghost sm" @click="doMark('open')">重新打开</button>
        </div>
      </aside>
    </div>

    <!-- ============ 转用例弹窗 ============ -->
    <div v-if="toCaseTarget" class="modal-overlay" @click.self="toCaseTarget = null">
      <div class="modal-box">
        <h3 class="modal-title">转评测用例</h3>
        <p class="modal-sub">将该 BadCase 沉淀为回归用例（origin=badcase）。跑回归通过后自动标记「已复现/修复」。</p>
        <label class="field">
          <span class="field-label">目标评测集 <em>*</em></span>
          <select class="input" v-model="toCaseForm.datasetId">
            <option value="" disabled>选择评测集</option>
            <option v-for="d in ragDatasets" :key="d.id" :value="d.id">{{ d.name }}（{{ d.case_count }} 用例）</option>
          </select>
        </label>
        <label class="field">
          <span class="field-label">问题</span>
          <textarea class="input textarea" v-model="toCaseForm.question" rows="2"></textarea>
        </label>
        <label class="field">
          <span class="field-label">参考答案（编辑后用于忠实度/召回评判）</span>
          <textarea class="input textarea" v-model="toCaseForm.referenceAnswer" rows="3" placeholder="填写期望的正确回答"></textarea>
        </label>
        <label class="field">
          <span class="field-label">期望关键词（逗号分隔，命中检索片段即算召回）</span>
          <input class="input" v-model="toCaseForm.keywords" placeholder="如：黄芪, 补气" />
        </label>
        <div class="modal-actions">
          <button class="btn btn-outline" @click="toCaseTarget = null">取消</button>
          <button class="btn btn-primary" :disabled="converting || !toCaseForm.datasetId" @click="doToCase">
            {{ converting ? '转换中…' : '确认转换' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import {
  listEvalBadCases, analyzeEvalBadCase, evalBadCaseToCase, markEvalBadCase,
  listEvalDatasets, getEvalRun,
} from '@/api'

const router = useRouter()

const CATEGORY_LABELS = {
  retrieval_miss: '检索未命中', empty_context: '上下文为空', hallucination: '幻觉',
  irrelevant: '答非所问', empty_answer: '空回答', tool_failure: '工具失败', low_quality: '质量低分',
}
const SOURCE_LABELS = {
  offline_eval: '离线评测', online_eval: '在线评测', feedback: '点踩反馈',
  empty_answer: '空回答', agent_eval: 'Agent 离线', agent_online: 'Agent 在线',
}
const STATUS_LABELS = { open: '待处理', reproduced: '已复现', fixed: '已修复', ignored: '已忽略' }
const METRIC_LABELS = {
  faithfulness: '忠实度', answer_relevance: '回答相关', context_precision: '上下文精确',
  context_recall: '上下文召回', hallucination: '幻觉率↓', geval_score: 'GEval',
  retrieval_hit: '检索命中', hit_at_k: 'Hit@K', mrr: 'MRR',
  task_completion: '任务完成', tool_correctness: '工具正确', argument_correctness: '参数正确',
  step_efficiency: '步骤效率', plan_quality: '规划质量', plan_adherence: '规划遵循',
  trajectory_judge: '轨迹评分', trajectory_match: '轨迹匹配',
}
const PAGE_SIZE = 50

const filters = reactive({ status: '', category: '', source_type: '' })
const items = ref([])
const total = ref(0)
const offset = ref(0)
const loading = ref(false)
const errorMsg = ref('')
const categoryCounts = ref({})
const detail = ref(null)
const analyzing = ref(false)
const datasets = ref([])

const ragDatasets = computed(() => datasets.value.filter((d) => (d.kind || 'rag') === 'rag'))
const hasCounts = computed(() => Object.values(categoryCounts.value).some((n) => n > 0))
const canLinkRun = computed(() =>
  detail.value && ['offline_eval', 'online_eval'].includes(detail.value.source_type))

const metricEntries = computed(() => {
  const m = detail.value?.metrics || {}
  const out = []
  for (const [key, label] of Object.entries(METRIC_LABELS)) {
    if (m[key] == null) continue
    const v = m[key]
    if (key === 'retrieval_hit' || key === 'hit_at_k' || key === 'trajectory_match') {
      out.push({ key, label, text: v ? '命中' : '未中', cls: v ? 'good' : 'bad' })
    } else {
      const invert = key === 'hallucination'
      const good = invert ? v <= 0.2 : v >= 0.8
      const mid = invert ? v <= 0.5 : v >= 0.5
      out.push({ key, label, text: (v * 100).toFixed(0) + '%', cls: good ? 'good' : mid ? 'mid' : 'bad' })
    }
  }
  return out
})

onMounted(async () => {
  await reload(true)
  try {
    const res = await listEvalDatasets()
    datasets.value = res.data.datasets || []
  } catch (e) { /* 数据集仅用于转用例下拉，失败不阻断列表 */ }
})

function errMsg(e, fallback) {
  return e?.response?.data?.message || e?.response?.data?.detail || e?.message || fallback
}

async function reload(reset = false) {
  if (reset) offset.value = 0
  loading.value = true
  errorMsg.value = ''
  try {
    const res = await listEvalBadCases({
      status: filters.status, category: filters.category, source_type: filters.source_type,
      limit: PAGE_SIZE, offset: offset.value,
    })
    items.value = res.data.items || []
    total.value = res.data.total || 0
    categoryCounts.value = res.data.category_counts || {}
  } catch (e) {
    errorMsg.value = errMsg(e, '加载 BadCase 失败')
  } finally {
    loading.value = false
  }
}

function page(dir) {
  const next = offset.value + dir * PAGE_SIZE
  if (next < 0 || next >= total.value) return
  offset.value = next
  reload()
}

function toggleCategory(key) {
  filters.category = filters.category === key ? '' : key
  reload(true)
}

function openDetail(row) {
  detail.value = { ...row, analysis: { ...(row.analysis || {}) } }
}

function shortId(id) {
  return id ? String(id).slice(0, 8) : ''
}

async function goRun(runId) {
  try {
    const res = await getEvalRun(runId)
    const dsId = res.data?.run?.dataset_id
    if (dsId) router.push({ name: 'EvalDataset', params: { datasetId: dsId }, query: { run: runId } })
  } catch (e) {
    errorMsg.value = errMsg(e, '无法打开关联运行')
  }
}

async function doAnalyze() {
  if (!detail.value) return
  analyzing.value = true
  try {
    const res = await analyzeEvalBadCase(detail.value.id)
    detail.value.analysis = res.data.analysis || {}
    const row = items.value.find((x) => x.id === detail.value.id)
    if (row) row.analysis = detail.value.analysis
  } catch (e) {
    errorMsg.value = errMsg(e, '归因分析失败')
  } finally {
    analyzing.value = false
  }
}

async function doMark(status) {
  if (!detail.value) return
  try {
    await markEvalBadCase(detail.value.id, status)
    detail.value.status = status
    const row = items.value.find((x) => x.id === detail.value.id)
    if (row) row.status = status
  } catch (e) {
    errorMsg.value = errMsg(e, '标记失败')
  }
}

// ---- 转用例 ----
const toCaseTarget = ref(null)
const converting = ref(false)
const toCaseForm = reactive({ datasetId: '', question: '', referenceAnswer: '', keywords: '' })

function openToCase(row) {
  toCaseTarget.value = row
  toCaseForm.datasetId = ragDatasets.value[0]?.id || ''
  toCaseForm.question = row.question || ''
  toCaseForm.referenceAnswer = row.answer || ''
  toCaseForm.keywords = ''
}

async function doToCase() {
  if (!toCaseTarget.value || !toCaseForm.datasetId) return
  converting.value = true
  errorMsg.value = ''
  try {
    const keywords = toCaseForm.keywords.split(/[,，]/).map((s) => s.trim()).filter(Boolean)
    const res = await evalBadCaseToCase(toCaseTarget.value.id, {
      datasetId: toCaseForm.datasetId,
      question: toCaseForm.question,
      referenceAnswer: toCaseForm.referenceAnswer,
      expectedKeywords: keywords,
    })
    const caseId = res.data.case_id
    const row = items.value.find((x) => x.id === toCaseTarget.value.id)
    if (row) row.case_id = caseId
    if (detail.value && detail.value.id === toCaseTarget.value.id) detail.value.case_id = caseId
    toCaseTarget.value = null
  } catch (e) {
    errorMsg.value = errMsg(e, '转用例失败')
  } finally {
    converting.value = false
  }
}
</script>

<style scoped>
.badcases { display: flex; flex-direction: column; gap: 14px; }
.bc-toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.bc-select { width: 140px; flex: none; }
.bc-note { font-size: 12px; color: var(--color-text-secondary); line-height: 1.5; }
.bc-stats { display: flex; gap: 8px; flex-wrap: wrap; }
.bc-stat {
  display: flex; flex-direction: column; align-items: center; gap: 1px; cursor: pointer;
  background: #faf7f2; border: 1px solid var(--color-border); border-radius: 9px; padding: 7px 14px; min-width: 78px;
}
.bc-stat:hover { border-color: var(--color-primary); }
.bc-stat.active { border-color: var(--color-primary); background: var(--color-primary-light); }
.bc-stat.empty { opacity: 0.5; }
.bc-stat-n { font-size: 16px; font-weight: 700; font-variant-numeric: tabular-nums; }
.bc-stat-l { font-size: 10.5px; color: var(--color-text-secondary); }

.inline-error { background: #fdecea; border: 1px solid #f5c6c0; color: #c0392b; font-size: 12.5px; padding: 9px 13px; border-radius: 10px; }
.mini-loading { padding: 26px; text-align: center; color: var(--color-text-secondary); font-size: 12.5px; }
.empty-block { border: 1px dashed var(--color-border); border-radius: 14px; padding: 40px 16px; text-align: center; color: var(--color-text-secondary); font-size: 12.5px; background: rgba(255,255,255,0.5); }

.data-table { width: 100%; border-collapse: collapse; font-size: 12.5px; background: #fff; border: 1px solid var(--color-border); border-radius: 12px; overflow: hidden; }
.data-table th, .data-table td { padding: 9px 10px; text-align: left; border-bottom: 1px solid var(--color-border); vertical-align: top; }
.data-table th { background: rgba(0,0,0,0.03); font-weight: 600; color: var(--color-text-secondary); white-space: nowrap; }
.data-table tbody tr:last-child td { border-bottom: none; }
.bc-row { cursor: pointer; }
.bc-row:hover { background: #fdfaf5; }
.cell-time { white-space: nowrap; color: var(--color-text-secondary); }
.q-cell { line-height: 1.5; }
.muted { color: #b9ad9e; }

.chip { font-size: 11px; font-weight: 600; padding: 2px 9px; border-radius: 6px; white-space: nowrap; }
.chip.src { background: #f0ece6; color: var(--color-text-secondary); }
.chip.case { background: #e3edfd; color: #1d4ed8; }
.chip.cat { background: #f3e8fa; color: #8a4bb8; }
.chip.cat-retrieval_miss { background: #fdecea; color: #c0392b; }
.chip.cat-empty_context { background: #fdf0d8; color: #b5822a; }
.chip.cat-hallucination { background: #fdecea; color: #c0392b; }
.chip.cat-irrelevant { background: #fdf0d8; color: #b5822a; }
.chip.cat-empty_answer { background: #f0ece6; color: #8a7e72; }
.chip.cat-tool_failure { background: #fdecea; color: #c0392b; }
.chip.cat-low_quality { background: #f3e8fa; color: #8a4bb8; }

.status-pill { font-size: 11px; font-weight: 700; padding: 3px 10px; border-radius: 20px; white-space: nowrap; background: #ede6dc; color: var(--color-text-secondary); }
.status-pill.sm { padding: 2px 8px; }
.status-pill.st-open { background: #fdf0d8; color: #b5822a; }
.status-pill.st-reproduced { background: #e3edfd; color: #1d4ed8; }
.status-pill.st-fixed { background: #e6f4ea; color: #2e7d46; }
.status-pill.st-ignored { background: #f0ece6; color: #8a7e72; }

.bc-pager { display: flex; align-items: center; justify-content: center; gap: 12px; }
.bc-page-text { font-size: 12px; color: var(--color-text-secondary); font-variant-numeric: tabular-nums; }

/* ---- 抽屉 ---- */
.drawer-overlay { position: fixed; inset: 0; background: rgba(45,42,36,0.4); display: flex; justify-content: flex-end; z-index: 100; }
.drawer { width: 520px; max-width: 94vw; height: 100%; background: #fff; display: flex; flex-direction: column; box-shadow: -8px 0 32px rgba(0,0,0,0.16); }
.drawer-head { display: flex; align-items: center; gap: 10px; padding: 16px 20px; border-bottom: 1px solid var(--color-border); }
.drawer-title { font-size: 15px; font-weight: 700; }
.drawer-head .icon-btn { margin-left: auto; }
.drawer-body { flex: 1; overflow-y: auto; padding: 18px 20px; display: flex; flex-direction: column; gap: 16px; }
.drawer-foot { display: flex; gap: 8px; flex-wrap: wrap; padding: 14px 20px; border-top: 1px solid var(--color-border); }
.d-block { display: flex; flex-direction: column; gap: 6px; }
.d-title { font-size: 11.5px; font-weight: 700; color: var(--color-text-secondary); display: flex; align-items: center; gap: 8px; }
.degraded-tag { font-style: normal; font-size: 10.5px; background: #f0ece6; color: #8a7e72; padding: 1px 7px; border-radius: 5px; }
.answer-box { background: #fffdfa; border: 1px solid var(--color-border); border-radius: 8px; padding: 9px 11px; font-size: 12.5px; line-height: 1.6; white-space: pre-wrap; max-height: 200px; overflow-y: auto; }
.answer-box.cause { background: #fbf8f3; }
.metric-snap { display: flex; gap: 8px; flex-wrap: wrap; }
.snap-item { background: #faf7f2; border-radius: 8px; padding: 6px 11px; display: flex; flex-direction: column; align-items: center; gap: 1px; min-width: 66px; }
.snap-v { font-size: 14px; font-weight: 700; font-variant-numeric: tabular-nums; }
.snap-v.good { color: #2e7d46; }
.snap-v.mid { color: #b5822a; }
.snap-v.bad { color: #c0392b; }
.snap-l { font-size: 10px; color: var(--color-text-secondary); }
.sugg-list { margin: 0; padding-left: 18px; display: flex; flex-direction: column; gap: 4px; font-size: 12.5px; line-height: 1.55; color: var(--color-text); }
.rel-line { font-size: 12.5px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.mono-id { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; color: var(--color-text-secondary); }
.link-btn { border: none; background: transparent; color: #1d4ed8; cursor: pointer; font-size: 12px; font-family: ui-monospace, Menlo, monospace; padding: 0; text-decoration: underline; }

/* ---- 弹窗 / 表单 ---- */
.modal-overlay { position: fixed; inset: 0; background: rgba(45,42,36,0.4); display: flex; align-items: center; justify-content: center; z-index: 110; }
.modal-box { width: 480px; max-width: 92vw; background: #fff; border-radius: 16px; padding: 22px 24px; box-shadow: 0 16px 48px rgba(0,0,0,0.2); display: flex; flex-direction: column; gap: 12px; }
.modal-title { font-size: 16px; font-weight: 700; }
.modal-sub { margin: -4px 0 0; font-size: 12px; color: var(--color-text-secondary); line-height: 1.55; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 4px; }
.field { display: flex; flex-direction: column; gap: 5px; }
.field-label { font-size: 11.5px; font-weight: 600; color: var(--color-text-secondary); }
.field-label em { color: #c0392b; font-style: normal; }
.input { width: 100%; padding: 9px 11px; font-size: 13px; border: 1px solid var(--color-border); border-radius: 9px; background: #fffdfa; color: var(--color-text); outline: none; font-family: inherit; box-sizing: border-box; }
.input:focus { border-color: var(--color-primary); background: #fff; }
.textarea { resize: vertical; line-height: 1.55; }
select.input { appearance: auto; background: #fff; }

/* ---- 按钮 ---- */
.btn { display: inline-flex; align-items: center; gap: 6px; border: 1px solid transparent; cursor: pointer; border-radius: 9px; font-weight: 600; font-size: 13px; padding: 9px 16px; transition: all 0.16s; font-family: inherit; }
.btn.sm { padding: 6px 12px; font-size: 12.5px; }
.btn.xs { padding: 3px 8px; font-size: 11.5px; }
.btn:disabled { opacity: 0.55; cursor: not-allowed; }
.btn-primary { background: linear-gradient(135deg, #c98a4b, #d9a76a); color: #fff; }
.btn-primary:hover:not(:disabled) { background: linear-gradient(135deg, #b0763c, #cf9a55); }
.btn-outline { background: #fff; border-color: var(--color-border); color: var(--color-text-secondary); }
.btn-outline:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
.btn-ghost { background: transparent; color: var(--color-text-secondary); }
.btn-ghost:hover:not(:disabled) { background: #f3ece2; color: var(--color-primary); }
.icon-btn { width: 28px; height: 28px; display: inline-flex; align-items: center; justify-content: center; border: none; background: transparent; border-radius: 7px; cursor: pointer; color: var(--color-text-secondary); font-size: 14px; }
.icon-btn:hover { background: #f0e7da; color: var(--color-primary); }
</style>