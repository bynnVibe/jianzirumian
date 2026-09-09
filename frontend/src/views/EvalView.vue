<template>
  <div class="eval-view">
    <!-- 顶部标题栏 -->
    <header class="eval-header">
      <div class="header-inner">
        <div class="header-title">
          <span class="header-eyebrow">Regression Suite</span>
          <h1>回归评测</h1>
          <p class="header-sub">对问答主链路跑离线回归，量化检索命中、引用忠实度与答案质量，防止优化引入回退</p>
        </div>
      </div>
    </header>

    <div class="eval-layout">
      <!-- ============ 左栏：评测集 ============ -->
      <aside class="dataset-panel">
        <div class="panel-head">
          <span class="panel-title">评测集</span>
          <button class="icon-btn" title="新建评测集" @click="openCreateDataset">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
              <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
            </svg>
          </button>
        </div>

        <div v-if="loadingDatasets" class="mini-loading">加载中…</div>
        <div v-else-if="!datasets.length" class="empty-block">
          <p>还没有评测集</p>
          <button class="btn btn-ghost sm" @click="openCreateDataset">创建第一个</button>
        </div>
        <ul v-else class="dataset-list">
          <li
            v-for="d in datasets"
            :key="d.id"
            class="dataset-item"
            :class="{ active: d.id === selectedDatasetId }"
            @click="selectDataset(d.id)"
          >
            <div class="dataset-main">
              <span class="dataset-name">{{ d.name }}</span>
              <span class="dataset-meta">{{ d.case_count }} 用例 · {{ runStatusText(d.last_run) }}</span>
            </div>
            <button class="icon-btn danger" title="删除评测集" @click.stop="removeDataset(d)">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                <polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
              </svg>
            </button>
          </li>
        </ul>
      </aside>

      <!-- ============ 右栏：主工作区 ============ -->
      <main class="work-panel">
        <div v-if="!selectedDataset" class="placeholder">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.2" width="46" height="46">
            <path d="M9 11l3 3L22 4" /><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
          </svg>
          <p>选择左侧评测集开始，或新建一个评测集</p>
        </div>

        <template v-else>
          <!-- 子导航 -->
          <nav class="subnav">
            <button class="subnav-btn" :class="{ active: panel === 'cases' }" @click="switchPanel('cases')">
              用例管理 <span class="count-pill">{{ cases.length }}</span>
            </button>
            <button class="subnav-btn" :class="{ active: panel === 'runs' || panel === 'detail' }" @click="switchPanel('runs')">
              运行与历史
              <span v-if="hasRunningRun" class="live-dot"></span>
            </button>
            <div class="subnav-spacer"></div>
            <button class="btn btn-primary" :disabled="startingRun || !cases.length" @click="startRun">
              <span v-if="startingRun" class="spinner-xs"></span>
              {{ startingRun ? '发起中…' : '发起评测' }}
            </button>
          </nav>

          <p v-if="errorMsg" class="inline-error">{{ errorMsg }}</p>

          <!-- ---------- 用例管理 ---------- -->
          <section v-show="panel === 'cases'" class="panel-body">
            <!-- 新增用例表单 -->
            <div class="card">
              <div class="card-head">
                <h3>新增用例</h3>
                <div class="card-head-actions">
                  <button class="btn btn-outline sm" @click="showImport = !showImport">
                    {{ showImport ? '收起批量导入' : 'JSON 批量导入' }}
                  </button>
                  <button class="btn btn-outline sm" :disabled="importingFeedback" @click="importFromFeedback">
                    <span v-if="importingFeedback" class="spinner-xs"></span>
                    从点踩反馈导入
                  </button>
                </div>
              </div>

              <div class="form-grid">
                <label class="field span-2">
                  <span class="field-label">问题 <em>*</em></span>
                  <input class="input" v-model.trim="caseForm.question" placeholder="用户会问的问题" />
                </label>
                <label class="field">
                  <span class="field-label">期望关键词</span>
                  <input class="input" v-model.trim="caseForm.keywords" placeholder="逗号分隔，命中检索片段即算召回" />
                </label>
                <label class="field">
                  <span class="field-label">期望来源</span>
                  <input class="input" v-model.trim="caseForm.expected_source" placeholder="命中标题/文件名即算召回" />
                </label>
                <label class="field span-2">
                  <span class="field-label">参考答案</span>
                  <textarea class="input textarea" v-model.trim="caseForm.reference_answer" rows="3" placeholder="填写后启用 LLM-as-judge 打分（1-5）；留空则不评分"></textarea>
                </label>
              </div>
              <div class="card-foot">
                <button class="btn btn-primary sm" :disabled="addingCase || !caseForm.question" @click="addCase">
                  {{ addingCase ? '添加中…' : '添加用例' }}
                </button>
              </div>

              <!-- JSON 批量导入 -->
              <div v-if="showImport" class="import-box">
                <p class="import-hint">
                  粘贴 JSON 数组，每项形如
                  <code>{"question":"…","expected_keywords":["…"],"reference_answer":"…","expected_source":"…"}</code>
                </p>
                <textarea class="input textarea mono" v-model="importText" rows="6" placeholder='[{"question":"…"}]'></textarea>
                <div class="card-foot">
                  <button class="btn btn-primary sm" :disabled="importing" @click="doImportJson">
                    {{ importing ? '导入中…' : '导入' }}
                  </button>
                </div>
              </div>
            </div>

            <!-- 用例表格 -->
            <div class="card">
              <div class="card-head"><h3>用例列表</h3></div>
              <div v-if="loadingCases" class="mini-loading">加载中…</div>
              <table v-else-if="cases.length" class="data-table">
                <thead>
                  <tr><th style="width:44%">问题</th><th>期望关键词</th><th>期望来源</th><th>参考答案</th><th style="width:48px"></th></tr>
                </thead>
                <tbody>
                  <tr v-for="c in cases" :key="c.id">
                    <td class="q-cell">{{ c.question }}</td>
                    <td>
                      <span v-if="c.expected_keywords && c.expected_keywords.length" class="kw-wrap">
                        <span v-for="(k, i) in c.expected_keywords" :key="i" class="kw-tag">{{ k }}</span>
                      </span>
                      <span v-else class="muted">—</span>
                    </td>
                    <td><span class="muted-clip">{{ c.expected_source || '—' }}</span></td>
                    <td><span class="muted-clip">{{ c.reference_answer ? '已填写' : '—' }}</span></td>
                    <td>
                      <button class="icon-btn danger" title="删除" @click="removeCase(c)">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                          <polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                        </svg>
                      </button>
                    </td>
                  </tr>
                </tbody>
              </table>
              <div v-else class="empty-block"><p>暂无用例，先添加或导入</p></div>
            </div>
          </section>

          <!-- ---------- 运行历史 ---------- -->
          <section v-show="panel === 'runs'" class="panel-body">
            <div class="card">
              <div class="card-head">
                <h3>运行历史</h3>
                <button class="btn btn-ghost sm" @click="loadRuns">刷新</button>
              </div>
              <div v-if="loadingRuns && !runs.length" class="mini-loading">加载中…</div>
              <ul v-else-if="runs.length" class="run-list">
                <li v-for="r in runs" :key="r.id" class="run-item" @click="openRun(r.id)">
                  <span class="status-pill" :class="r.status">{{ statusLabel(r.status) }}</span>
                  <div class="run-info">
                    <span class="run-time">{{ fmtTime(r.started_at) }}</span>
                    <span class="run-prog">
                      <template v-if="r.status === 'running'">进度 {{ r.completed }}/{{ r.total }}</template>
                      <template v-else-if="r.summary && r.summary.total != null">
                        命中 {{ pct(r.summary.retrieval_hit_rate) }} · 忠实 {{ pct(r.summary.faithfulness_pass_rate) }} ·
                        judge {{ r.summary.avg_judge_score ?? '—' }} · {{ Math.round(r.summary.avg_latency_ms || 0) }}ms
                      </template>
                    </span>
                  </div>
                  <div class="run-bar" v-if="r.status === 'running' && r.total">
                    <div class="run-bar-fill" :style="{ width: (r.completed / r.total * 100) + '%' }"></div>
                  </div>
                  <svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><polyline points="9 18 15 12 9 6" /></svg>
                </li>
              </ul>
              <div v-else class="empty-block"><p>还没有运行记录，点击「发起评测」开始</p></div>
            </div>
          </section>

          <!-- ---------- 运行详情 ---------- -->
          <section v-show="panel === 'detail'" class="panel-body">
            <button class="btn btn-ghost sm back-btn" @click="switchPanel('runs')">← 返回运行历史</button>
            <div v-if="loadingDetail && !detail" class="mini-loading">加载中…</div>
            <template v-else-if="detail">
              <!-- 指标卡片 -->
              <div class="metric-grid">
                <div class="metric-card" v-for="m in metricCards" :key="m.key">
                  <span class="metric-label">{{ m.label }}</span>
                  <span class="metric-value">{{ m.value }}</span>
                  <span v-if="m.delta" class="metric-delta" :class="m.delta.cls">{{ m.delta.text }}</span>
                  <span v-else class="metric-delta flat">首次运行</span>
                </div>
              </div>

              <div class="run-status-line">
                <span class="status-pill" :class="detail.run.status">{{ statusLabel(detail.run.status) }}</span>
                <span class="muted">{{ fmtTime(detail.run.started_at) }}<template v-if="detail.run.finished_at"> → {{ fmtTime(detail.run.finished_at) }}</template></span>
                <span v-if="detail.run.summary && detail.run.summary.total != null" class="muted">
                  共 {{ detail.run.summary.total }} 例 · 成功 {{ detail.run.summary.ok }} · 失败 {{ detail.run.summary.error }}
                </span>
              </div>

              <!-- 结果明细表 -->
              <div class="card">
                <div class="card-head"><h3>用例结果明细</h3></div>
                <table class="data-table result-table">
                  <thead>
                    <tr><th style="width:34%">问题</th><th>命中</th><th>忠实</th><th>judge</th><th>延迟</th><th>状态</th></tr>
                  </thead>
                  <tbody>
                    <template v-for="row in detail.results" :key="row.id">
                      <tr class="result-row" @click="toggleExpand(row.id)">
                        <td class="q-cell">
                          <span class="exp-caret" :class="{ open: expanded.has(row.id) }">▸</span>{{ row.question }}
                        </td>
                        <td><span class="bool" :class="row.retrieval_hit ? 'yes' : 'no'">{{ row.retrieval_hit ? '命中' : '未中' }}</span></td>
                        <td><span class="bool" :class="row.faithfulness_pass ? 'yes' : 'no'">{{ row.faithfulness_pass ? '通过' : '未过' }}</span></td>
                        <td>{{ row.judge_score != null ? row.judge_score : '—' }}</td>
                        <td>{{ row.latency_ms }}ms</td>
                        <td><span class="status-pill sm" :class="row.status === 'ok' ? 'finished' : 'failed'">{{ row.status === 'ok' ? '成功' : '错误' }}</span></td>
                      </tr>
                      <tr v-if="expanded.has(row.id)" class="expand-row">
                        <td colspan="6">
                          <div class="expand-inner">
                            <div v-if="row.error" class="err-line">错误：{{ row.error }}</div>
                            <div class="expand-block">
                              <span class="expand-title">模型答案</span>
                              <div class="answer-box">{{ row.answer || '(空)' }}</div>
                            </div>
                            <div class="expand-block" v-if="row.judge_reason">
                              <span class="expand-title">评分理由</span>
                              <div class="reason-box">{{ row.judge_reason }}</div>
                            </div>
                            <div class="expand-block">
                              <span class="expand-title">检索来源（{{ (row.retrieved_sources || []).length }}）</span>
                              <ul class="src-list" v-if="row.retrieved_sources && row.retrieved_sources.length">
                                <li v-for="(s, i) in row.retrieved_sources" :key="i">
                                  <span class="src-idx">[{{ s.index }}]</span>
                                  <span class="src-meta">
                                    {{ s.wiki_title || s.source_image || s.kb_name || '片段' }}
                                    <em v-if="s.relevance != null">相关度 {{ s.relevance }}%</em>
                                  </span>
                                  <span class="src-text">{{ s.text }}</span>
                                </li>
                              </ul>
                              <div v-else class="muted">无检索结果</div>
                            </div>
                          </div>
                        </td>
                      </tr>
                    </template>
                  </tbody>
                </table>
              </div>
            </template>
          </section>
        </template>
      </main>
    </div>

    <!-- 新建评测集弹窗 -->
    <div v-if="showCreateDataset" class="modal-overlay" @click.self="showCreateDataset = false">
      <div class="modal-box">
        <h3 class="modal-title">新建评测集</h3>
        <label class="field">
          <span class="field-label">名称 <em>*</em></span>
          <input class="input" v-model.trim="datasetForm.name" placeholder="如：中医养生核心问答集" />
        </label>
        <label class="field">
          <span class="field-label">描述</span>
          <textarea class="input textarea" v-model.trim="datasetForm.description" rows="3" placeholder="评测集用途说明（选填）"></textarea>
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
  listEvalDatasets, createEvalDataset, deleteEvalDataset,
  listEvalCases, createEvalCase, deleteEvalCase,
  importEvalCases, importEvalCasesFromFeedback,
  startEvalRun, listEvalRuns, getEvalRun,
} from '@/api'

// ---- 评测集 ----
const datasets = ref([])
const loadingDatasets = ref(false)
const selectedDatasetId = ref('')
const selectedDataset = computed(() => datasets.value.find((d) => d.id === selectedDatasetId.value) || null)

// ---- 面板 ----
const panel = ref('cases') // cases | runs | detail
const errorMsg = ref('')

// ---- 用例 ----
const cases = ref([])
const loadingCases = ref(false)
const addingCase = ref(false)
const caseForm = reactive({ question: '', keywords: '', reference_answer: '', expected_source: '' })
const showImport = ref(false)
const importText = ref('')
const importing = ref(false)
const importingFeedback = ref(false)

// ---- 运行 ----
const runs = ref([])
const loadingRuns = ref(false)
const startingRun = ref(false)
const detail = ref(null)
const loadingDetail = ref(false)
const expanded = ref(new Set())
let pollTimer = null

const hasRunningRun = computed(() => runs.value.some((r) => r.status === 'running'))

onMounted(loadDatasets)
onBeforeUnmount(stopPolling)

function errMsg(e, fallback) {
  return e?.response?.data?.message || e?.response?.data?.detail || e?.message || fallback
}

// ============================================
// 评测集
// ============================================
async function loadDatasets() {
  loadingDatasets.value = true
  try {
    const res = await listEvalDatasets()
    datasets.value = res.data.datasets || []
    if (!selectedDatasetId.value && datasets.value.length) {
      await selectDataset(datasets.value[0].id)
    }
  } catch (e) {
    errorMsg.value = errMsg(e, '加载评测集失败')
  } finally {
    loadingDatasets.value = false
  }
}

async function selectDataset(id) {
  selectedDatasetId.value = id
  panel.value = 'cases'
  detail.value = null
  expanded.value = new Set()
  errorMsg.value = ''
  stopPolling()
  await Promise.all([loadCases(), loadRuns()])
}

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
  try {
    const res = await createEvalDataset({ name: datasetForm.name, description: datasetForm.description })
    showCreateDataset.value = false
    datasets.value.unshift(res.data.dataset)
    await selectDataset(res.data.dataset.id)
  } catch (e) {
    errorMsg.value = errMsg(e, '创建评测集失败')
  } finally {
    creatingDataset.value = false
  }
}

async function removeDataset(d) {
  if (!window.confirm(`确认删除评测集「${d.name}」？其用例与运行历史将一并删除。`)) return
  try {
    await deleteEvalDataset(d.id)
    datasets.value = datasets.value.filter((x) => x.id !== d.id)
    if (selectedDatasetId.value === d.id) {
      selectedDatasetId.value = ''
      detail.value = null
      cases.value = []
      runs.value = []
      stopPolling()
      if (datasets.value.length) await selectDataset(datasets.value[0].id)
    }
  } catch (e) {
    errorMsg.value = errMsg(e, '删除评测集失败')
  }
}

// ============================================
// 用例
// ============================================
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

async function addCase() {
  addingCase.value = true
  errorMsg.value = ''
  try {
    const keywords = caseForm.keywords
      .split(/[,，]/)
      .map((s) => s.trim())
      .filter(Boolean)
    const res = await createEvalCase(selectedDatasetId.value, {
      question: caseForm.question,
      expected_keywords: keywords,
      reference_answer: caseForm.reference_answer,
      expected_source: caseForm.expected_source,
    })
    cases.value.push(res.data.case)
    caseForm.question = ''
    caseForm.keywords = ''
    caseForm.reference_answer = ''
    caseForm.expected_source = ''
    refreshDatasetCounts()
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
    refreshDatasetCounts()
  } catch (e) {
    errorMsg.value = errMsg(e, '删除用例失败')
  }
}

async function doImportJson() {
  errorMsg.value = ''
  let parsed
  try {
    parsed = JSON.parse(importText.value)
  } catch (e) {
    errorMsg.value = 'JSON 解析失败，请检查格式'
    return
  }
  const arr = Array.isArray(parsed) ? parsed : parsed.cases
  if (!Array.isArray(arr) || !arr.length) {
    errorMsg.value = '请提供非空的 JSON 数组'
    return
  }
  importing.value = true
  try {
    await importEvalCases(selectedDatasetId.value, arr)
    importText.value = ''
    showImport.value = false
    await loadCases()
    refreshDatasetCounts()
  } catch (e) {
    errorMsg.value = errMsg(e, '导入失败')
  } finally {
    importing.value = false
  }
}

async function importFromFeedback() {
  errorMsg.value = ''
  importingFeedback.value = true
  try {
    const res = await importEvalCasesFromFeedback(selectedDatasetId.value)
    await loadCases()
    refreshDatasetCounts()
    if (!res.data.imported) errorMsg.value = '没有可导入的点踩反馈（或均已存在）'
  } catch (e) {
    errorMsg.value = errMsg(e, '从反馈导入失败')
  } finally {
    importingFeedback.value = false
  }
}

function refreshDatasetCounts() {
  const d = datasets.value.find((x) => x.id === selectedDatasetId.value)
  if (d) d.case_count = cases.value.length
}

// ============================================
// 运行
// ============================================
async function loadRuns() {
  if (!selectedDatasetId.value) return
  loadingRuns.value = true
  try {
    const res = await listEvalRuns(selectedDatasetId.value)
    runs.value = res.data.runs || []
    managePolling()
  } catch (e) {
    errorMsg.value = errMsg(e, '加载运行历史失败')
  } finally {
    loadingRuns.value = false
  }
}

async function startRun() {
  errorMsg.value = ''
  startingRun.value = true
  try {
    await startEvalRun(selectedDatasetId.value)
    panel.value = 'runs'
    await loadRuns()
  } catch (e) {
    errorMsg.value = errMsg(e, '发起评测失败')
  } finally {
    startingRun.value = false
  }
}

async function openRun(runId) {
  panel.value = 'detail'
  loadingDetail.value = true
  expanded.value = new Set()
  try {
    const res = await getEvalRun(runId)
    detail.value = res.data
    // 详情内若仍在运行，也进入轮询
    managePolling()
  } catch (e) {
    errorMsg.value = errMsg(e, '加载运行详情失败')
  } finally {
    loadingDetail.value = false
  }
}

function toggleExpand(id) {
  const next = new Set(expanded.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expanded.value = next
}

// 轮询：运行历史或详情中存在 running 时，每 3s 刷新
function managePolling() {
  const running = hasRunningRun.value || detail.value?.run?.status === 'running'
  if (running && !pollTimer) {
    pollTimer = setInterval(pollTick, 3000)
  } else if (!running && pollTimer) {
    stopPolling()
  }
}

async function pollTick() {
  try {
    if (panel.value === 'detail' && detail.value) {
      const res = await getEvalRun(detail.value.run.id)
      detail.value = res.data
      await loadRuns()
    } else {
      await loadRuns()
    }
  } catch (e) {
    /* 轮询失败静默，下次重试 */
  }
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function switchPanel(p) {
  panel.value = p
  if (p !== 'detail') detail.value = null
  managePolling()
}

// ============================================
// 指标卡片 + 环比
// ============================================
function delta(cur, prev, { invert = false, suffix = '', scale = 1, digits = 0 } = {}) {
  if (prev == null || cur == null) return null
  const diff = (cur - prev) * scale
  if (Math.abs(diff) < 1e-9) return { text: '持平', cls: 'flat' }
  const up = diff > 0
  const good = invert ? !up : up
  const arrow = up ? '▲' : '▼'
  const val = digits ? Math.abs(diff).toFixed(digits) : Math.round(Math.abs(diff))
  return { text: `${arrow} ${val}${suffix}`, cls: good ? 'good' : 'bad' }
}

const metricCards = computed(() => {
  const s = detail.value?.run?.summary || {}
  const p = detail.value?.prev_summary || {}
  return [
    {
      key: 'hit',
      label: '检索命中率',
      value: pct(s.retrieval_hit_rate),
      delta: delta(s.retrieval_hit_rate, p.retrieval_hit_rate, { scale: 100, suffix: 'pp' }),
    },
    {
      key: 'faith',
      label: '忠实度通过率',
      value: pct(s.faithfulness_pass_rate),
      delta: delta(s.faithfulness_pass_rate, p.faithfulness_pass_rate, { scale: 100, suffix: 'pp' }),
    },
    {
      key: 'judge',
      label: '平均 judge 分',
      value: s.avg_judge_score != null ? Number(s.avg_judge_score).toFixed(2) : '—',
      delta: delta(s.avg_judge_score, p.avg_judge_score, { digits: 2 }),
    },
    {
      key: 'latency',
      label: '平均延迟',
      value: s.avg_latency_ms != null ? `${Math.round(s.avg_latency_ms)}ms` : '—',
      delta: delta(s.avg_latency_ms, p.avg_latency_ms, { invert: true, suffix: 'ms' }),
    },
  ]
})

// ============================================
// 格式化
// ============================================
function pct(v) {
  if (v == null) return '—'
  return `${(v * 100).toFixed(1)}%`
}

function statusLabel(st) {
  return { running: '执行中', finished: '已完成', failed: '失败', interrupted: '已中断' }[st] || st
}

function runStatusText(lastRun) {
  if (!lastRun) return '未运行'
  return `最近${statusLabel(lastRun.status)}`
}

function fmtTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}
</script>

<style scoped>
.eval-view {
  height: 100vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  background:
    radial-gradient(1200px 400px at 100% -10%, #fbf1e2 0%, transparent 60%),
    var(--color-bg);
}

/* ---- 顶部 ---- */
.eval-header {
  border-bottom: 1px solid var(--color-border);
  background: rgba(254, 252, 249, 0.86);
  backdrop-filter: blur(8px);
  position: sticky;
  top: 0;
  z-index: 20;
}
.header-inner { padding: 22px 32px 18px; max-width: 1400px; }
.header-eyebrow {
  font-size: 11px; letter-spacing: 2.5px; text-transform: uppercase;
  color: var(--color-primary); font-weight: 700;
}
.header-title h1 { font-size: 22px; font-weight: 700; margin: 2px 0 4px; letter-spacing: -0.3px; }
.header-sub { font-size: 12.5px; color: var(--color-text-secondary); max-width: 720px; line-height: 1.5; }

/* ---- 布局 ---- */
.eval-layout {
  flex: 1; display: grid; grid-template-columns: 264px 1fr;
  gap: 20px; padding: 20px 32px 40px; max-width: 1400px; width: 100%; margin: 0 auto;
  align-items: start;
}

/* ---- 左栏 ---- */
.dataset-panel {
  background: #fff; border: 1px solid var(--color-border); border-radius: 16px;
  padding: 14px; position: sticky; top: 96px; max-height: calc(100vh - 130px);
  display: flex; flex-direction: column;
}
.panel-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.panel-title { font-size: 12px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; color: var(--color-text-secondary); }
.dataset-list { list-style: none; overflow-y: auto; display: flex; flex-direction: column; gap: 4px; }
.dataset-item {
  display: flex; align-items: center; gap: 8px; padding: 10px 12px; border-radius: 10px;
  cursor: pointer; border: 1px solid transparent; transition: all 0.16s;
}
.dataset-item:hover { background: #faf5ee; }
.dataset-item.active { background: var(--color-primary-light); border-color: #e9bc7d; }
.dataset-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.dataset-name { font-size: 13.5px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dataset-meta { font-size: 11px; color: var(--color-text-secondary); }

/* ---- 右栏 ---- */
.work-panel { min-width: 0; display: flex; flex-direction: column; gap: 16px; }
.placeholder {
  border: 1px dashed var(--color-border); border-radius: 16px; padding: 80px 20px;
  text-align: center; color: var(--color-text-secondary); background: rgba(255,255,255,0.5);
}
.placeholder svg { opacity: 0.35; margin-bottom: 12px; }
.placeholder p { font-size: 13.5px; }

.subnav { display: flex; align-items: center; gap: 8px; }
.subnav-btn {
  display: inline-flex; align-items: center; gap: 7px; padding: 8px 16px; border-radius: 10px;
  border: 1px solid var(--color-border); background: #fff; cursor: pointer; font-size: 13px;
  font-weight: 600; color: var(--color-text-secondary); transition: all 0.16s;
}
.subnav-btn:hover { border-color: var(--color-primary); color: var(--color-primary); }
.subnav-btn.active { background: var(--color-primary-light); border-color: var(--color-primary); color: var(--color-primary); }
.subnav-spacer { flex: 1; }
.count-pill { font-size: 11px; background: #ede6dc; color: var(--color-text-secondary); padding: 0 7px; border-radius: 8px; }
.subnav-btn.active .count-pill { background: #f0dcc0; color: var(--color-primary); }
.live-dot { width: 7px; height: 7px; border-radius: 50%; background: #e0a038; animation: pulse 1.2s infinite; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }

.panel-body { display: flex; flex-direction: column; gap: 16px; }
.inline-error {
  background: #fdecea; border: 1px solid #f5c6c0; color: #c0392b; font-size: 12.5px;
  padding: 9px 13px; border-radius: 10px;
}

/* ---- 卡片 ---- */
.card { background: #fff; border: 1px solid var(--color-border); border-radius: 16px; padding: 18px 20px; }
.card-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
.card-head h3 { font-size: 14.5px; font-weight: 700; }
.card-head-actions { display: flex; gap: 8px; }
.card-foot { display: flex; justify-content: flex-end; margin-top: 12px; }

/* ---- 表单 ---- */
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.field { display: flex; flex-direction: column; gap: 5px; }
.field.span-2 { grid-column: span 2; }
.field-label { font-size: 11.5px; font-weight: 600; color: var(--color-text-secondary); }
.field-label em { color: #c0392b; font-style: normal; }
.input {
  width: 100%; padding: 9px 11px; font-size: 13px; border: 1px solid var(--color-border);
  border-radius: 9px; background: #fffdfa; color: var(--color-text); outline: none; transition: border-color 0.15s;
  font-family: inherit; box-sizing: border-box;
}
.input:focus { border-color: var(--color-primary); background: #fff; }
.textarea { resize: vertical; line-height: 1.55; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }

.import-box { margin-top: 14px; padding-top: 14px; border-top: 1px dashed var(--color-border); }
.import-hint { font-size: 11.5px; color: var(--color-text-secondary); margin-bottom: 8px; line-height: 1.6; }
.import-hint code { background: #f3efe9; padding: 1px 5px; border-radius: 4px; font-size: 11px; }

/* ---- 按钮 ---- */
.btn {
  display: inline-flex; align-items: center; gap: 6px; border: 1px solid transparent; cursor: pointer;
  border-radius: 9px; font-weight: 600; font-size: 13px; padding: 9px 16px; transition: all 0.16s; font-family: inherit;
}
.btn.sm { padding: 6px 12px; font-size: 12.5px; }
.btn:disabled { opacity: 0.55; cursor: not-allowed; }
.btn-primary { background: linear-gradient(135deg, #c98a4b, #d9a76a); color: #fff; box-shadow: 0 2px 8px rgba(201,138,75,0.25); }
.btn-primary:hover:not(:disabled) { background: linear-gradient(135deg, #b0763c, #cf9a55); }
.btn-outline { background: #fff; border-color: var(--color-border); color: var(--color-text-secondary); }
.btn-outline:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
.btn-ghost { background: transparent; color: var(--color-text-secondary); }
.btn-ghost:hover { background: #f3ece2; color: var(--color-primary); }
.icon-btn {
  width: 26px; height: 26px; display: inline-flex; align-items: center; justify-content: center;
  border: none; background: transparent; border-radius: 7px; cursor: pointer; color: var(--color-text-secondary); transition: all 0.15s;
}
.icon-btn:hover { background: #f0e7da; color: var(--color-primary); }
.icon-btn.danger:hover { background: #fdecea; color: #c0392b; }
.spinner-xs { width: 12px; height: 12px; border: 2px solid rgba(255,255,255,0.4); border-top-color: #fff; border-radius: 50%; animation: spin 0.7s linear infinite; }
.btn-outline .spinner-xs, .btn-ghost .spinner-xs { border-color: rgba(201,138,75,0.3); border-top-color: var(--color-primary); }
@keyframes spin { to { transform: rotate(360deg); } }
.back-btn { align-self: flex-start; }

/* ---- 表格 ---- */
.data-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.data-table th {
  text-align: left; font-size: 11px; font-weight: 700; color: var(--color-text-secondary);
  text-transform: uppercase; letter-spacing: 0.5px; padding: 8px 10px; border-bottom: 1px solid var(--color-border);
}
.data-table td { padding: 10px; border-bottom: 1px solid #f4efe7; vertical-align: top; color: var(--color-text); }
.data-table tbody tr:hover { background: #fdfaf5; }
.q-cell { font-weight: 500; line-height: 1.45; }
.muted { color: #b9ad9e; }
.muted-clip { color: var(--color-text-secondary); display: inline-block; max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.kw-wrap { display: flex; flex-wrap: wrap; gap: 4px; }
.kw-tag { background: var(--color-primary-light); color: #a9713a; font-size: 11px; padding: 1px 8px; border-radius: 20px; }

/* ---- 运行列表 ---- */
.run-list { list-style: none; display: flex; flex-direction: column; gap: 8px; }
.run-item {
  display: flex; align-items: center; gap: 12px; padding: 12px 14px; border: 1px solid var(--color-border);
  border-radius: 12px; cursor: pointer; transition: all 0.16s; position: relative;
}
.run-item:hover { border-color: var(--color-primary); background: #fdfaf5; }
.run-info { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.run-time { font-size: 13px; font-weight: 600; }
.run-prog { font-size: 11.5px; color: var(--color-text-secondary); }
.run-bar { position: absolute; left: 0; bottom: 0; height: 3px; width: 100%; background: #f0e7da; border-radius: 0 0 12px 12px; overflow: hidden; }
.run-bar-fill { height: 100%; background: linear-gradient(90deg, #e0a038, #c98a4b); transition: width 0.4s; }
.chev { color: #cbbfae; flex-shrink: 0; }

.status-pill {
  font-size: 11px; font-weight: 700; padding: 3px 10px; border-radius: 20px; white-space: nowrap;
  background: #ede6dc; color: var(--color-text-secondary);
}
.status-pill.sm { font-size: 10.5px; padding: 2px 8px; }
.status-pill.running { background: #fdf0d8; color: #b5822a; }
.status-pill.finished { background: #e6f4ea; color: #2e7d46; }
.status-pill.failed { background: #fdecea; color: #c0392b; }
.status-pill.interrupted { background: #f0ece6; color: #8a7e72; }

/* ---- 指标卡片 ---- */
.metric-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }
.metric-card {
  background: linear-gradient(160deg, #fff, #fdf8f1); border: 1px solid var(--color-border);
  border-radius: 14px; padding: 16px 18px; display: flex; flex-direction: column; gap: 6px;
}
.metric-label { font-size: 11.5px; font-weight: 600; color: var(--color-text-secondary); }
.metric-value { font-size: 28px; font-weight: 700; color: var(--color-primary); letter-spacing: -0.5px; line-height: 1; font-variant-numeric: tabular-nums; }
.metric-delta { font-size: 11.5px; font-weight: 700; }
.metric-delta.good { color: #2e7d46; }
.metric-delta.bad { color: #c0392b; }
.metric-delta.flat { color: #b9ad9e; font-weight: 500; }
.run-status-line { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; font-size: 12px; }

/* ---- 结果明细 ---- */
.result-table tbody tr.result-row { cursor: pointer; }
.exp-caret { display: inline-block; margin-right: 6px; color: var(--color-primary); transition: transform 0.15s; font-size: 11px; }
.exp-caret.open { transform: rotate(90deg); }
.bool { font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 6px; }
.bool.yes { background: #e6f4ea; color: #2e7d46; }
.bool.no { background: #f3ece2; color: #a99a86; }
.expand-row td { background: #fdfaf5; padding: 0; }
.expand-inner { padding: 14px 16px; display: flex; flex-direction: column; gap: 12px; }
.err-line { color: #c0392b; font-size: 12px; background: #fdecea; padding: 7px 10px; border-radius: 8px; }
.expand-block { display: flex; flex-direction: column; gap: 5px; }
.expand-title { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; color: var(--color-text-secondary); }
.answer-box, .reason-box { font-size: 12.5px; line-height: 1.65; white-space: pre-wrap; background: #fff; border: 1px solid var(--color-border); border-radius: 9px; padding: 10px 12px; }
.reason-box { color: var(--color-text-secondary); }
.src-list { list-style: none; display: flex; flex-direction: column; gap: 6px; }
.src-list li { display: flex; gap: 8px; align-items: baseline; font-size: 12px; background: #fff; border: 1px solid var(--color-border); border-radius: 8px; padding: 7px 10px; }
.src-idx { color: var(--color-primary); font-weight: 700; flex-shrink: 0; }
.src-meta { color: var(--color-text-secondary); flex-shrink: 0; }
.src-meta em { font-style: normal; margin-left: 6px; color: #b5822a; }
.src-text { color: var(--color-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }

/* ---- 空/加载 ---- */
.mini-loading { padding: 26px; text-align: center; color: var(--color-text-secondary); font-size: 12.5px; }
.empty-block { padding: 30px 16px; text-align: center; color: var(--color-text-secondary); font-size: 12.5px; display: flex; flex-direction: column; gap: 10px; align-items: center; }

/* ---- 弹窗 ---- */
.modal-overlay { position: fixed; inset: 0; background: rgba(45,42,36,0.4); display: flex; align-items: center; justify-content: center; z-index: 100; }
.modal-box { width: 440px; max-width: 92vw; background: #fff; border-radius: 16px; padding: 22px 24px; box-shadow: 0 16px 48px rgba(0,0,0,0.2); display: flex; flex-direction: column; gap: 12px; }
.modal-title { font-size: 16px; font-weight: 700; margin-bottom: 2px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 4px; }

/* ---- 响应式 ---- */
@media (max-width: 980px) {
  .metric-grid { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 768px) {
  .eval-header { padding-top: 44px; }
  .header-inner { padding: 16px; }
  .eval-layout { grid-template-columns: 1fr; padding: 16px; }
  .dataset-panel { position: static; max-height: none; }
  .form-grid { grid-template-columns: 1fr; }
  .field.span-2 { grid-column: span 1; }
  .metric-grid { grid-template-columns: 1fr 1fr; }
  .subnav { flex-wrap: wrap; }
}
</style>
