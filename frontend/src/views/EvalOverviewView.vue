<template>
  <div class="eval-overview">
    <!-- ============ 顶部：标题 + 分段页签 + 工具条（图1 式） ============ -->
    <header class="eval-header">
      <div class="header-inner">
        <div class="title-row">
          <h1 class="page-title">评测</h1>
          <nav class="seg-tabs" role="tablist" aria-label="评测分类">
            <button class="seg-tab" :class="{ active: tab === 'datasets' }" role="tab" :aria-selected="tab === 'datasets'" @click="tab = 'datasets'">
              评测集
            </button>
            <button class="seg-tab" :class="{ active: tab === 'evaluators' }" role="tab" :aria-selected="tab === 'evaluators'" @click="tab = 'evaluators'">
              评估器
            </button>
            <button class="seg-tab" :class="{ active: tab === 'experiments' }" role="tab" :aria-selected="tab === 'experiments'" @click="tab = 'experiments'">
              实验
            </button>
          </nav>
        </div>
        <div class="toolbar">
          <input
            v-model.trim="keyword"
            class="search-input"
            type="search"
            :placeholder="tab === 'datasets' ? '搜索评测集名称…' : tab === 'evaluators' ? '搜索评估器名称…' : '搜索评测集 / 发起人…'"
          />
          <button class="tool-btn" title="刷新" :disabled="loadingDatasets || loadingEvals" @click="reloadAll">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="15" height="15">
              <polyline points="23 4 23 10 17 10" /><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
            </svg>
            刷新
          </button>
          <button v-if="tab === 'datasets'" class="btn-primary-btn" @click="openCreateDataset">＋ 新建评测集</button>
          <button v-else-if="tab === 'evaluators'" class="btn-primary-btn" @click="openCreateEvaluator">＋ 新建评估器</button>
          <button v-else class="btn-primary-btn" @click="openCreateRun">＋ 新建实验</button>
        </div>
      </div>
      <p class="header-sub">
        {{ tab === 'datasets'
          ? '问答回归 · Ragas：按评测集对问答主链路跑离线回归，量化忠实度 / 回答相关性 / 上下文精确率 / 召回率，防止优化引入回退。点击卡片进入详情。'
          : tab === 'evaluators'
            ? '评估器：基于已有评测集构建评测工作流，一键运行并在页内实时查看评测进度与逐用例结果；Prompt / Skill 专项评测工作台已整合在本页下方。'
            : '实验：一次评测运行即一个实验。选择评测集发起实验，点击行进入该运行的逐用例结果与环比分析。' }}
      </p>
    </header>

    <div class="overview-body">
      <p v-if="errorMsg" class="inline-error">{{ errorMsg }}</p>

      <!-- ============ 评测集卡片格 ============ -->
      <template v-if="tab === 'datasets'">
        <div v-if="loadingDatasets" class="mini-loading">加载中…</div>
        <div v-else-if="!filteredDatasets.length" class="empty-block">
          <p>{{ datasets.length ? '没有匹配「' + keyword + '」的评测集' : '还没有评测集，创建第一个开始问答回归' }}</p>
          <button class="outline-btn" @click="openCreateDataset">创建评测集</button>
        </div>
        <div v-else class="card-grid">
          <div v-for="d in filteredDatasets" :key="d.id" class="entry-card" :class="{ focused: d.id === focusedId }" @click="goDataset(d.id)">
            <div class="entry-card-head">
              <span class="entry-icon ds">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
                  <path d="M9 11l3 3L22 4" /><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
                </svg>
              </span>
              <span class="entry-name">{{ d.name }}</span>
              <span class="card-actions">
                <button class="icon-btn danger" title="删除评测集" @click.stop="removeDataset(d)">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                    <polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                  </svg>
                </button>
              </span>
            </div>
            <p class="entry-desc">{{ d.description || '暂无描述' }}</p>
            <div class="entry-tags">
              <span class="tag-chip">{{ d.case_count }} 用例</span>
              <span class="tag-chip" :class="runChipCls(d.last_run)">{{ runStatusText(d.last_run) }}</span>
            </div>
          </div>
        </div>
      </template>

      <!-- ============ 评估器（基于评测集的评测工作流） ============ -->
      <template v-else-if="tab === 'evaluators'">
        <!-- ---- 页内运行监控（点击运行后在本页查看进度与结果） ---- -->
        <section v-if="activeRun" class="run-monitor">
          <div class="monitor-head">
            <span class="monitor-title">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
                <polygon points="5 3 19 12 5 21 5 3" />
              </svg>
              {{ activeRun.evaluator.name }}
            </span>
            <span class="tag-chip muted">{{ activeRun.evaluator.dataset_name }} · {{ activeRun.evaluator.case_count }} 用例</span>
            <span v-if="runDetail" class="status-pill" :class="runStatusCls(runDetail.run.status)">{{ statusLabel(runDetail.run.status) }}</span>
            <span class="monitor-progress-text">{{ progressText }}</span>
            <span class="monitor-actions">
              <button v-if="!runDetail || runDetail.run.status !== 'running'" class="run-btn sm" :disabled="startingRun" @click="runEvaluator(activeRun.evaluator)">
                {{ startingRun ? '发起中…' : '重新运行' }}
              </button>
              <button class="icon-btn" title="收起监控" @click="closeRun">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                  <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            </span>
          </div>
          <div class="progress-track"><div class="progress-fill" :class="{ done: runDetail && runDetail.run.status === 'finished' }" :style="{ width: progressPct }"></div></div>
          <div v-if="runSummaryCards.length" class="monitor-metrics">
            <div v-for="m in runSummaryCards" :key="m.label" class="monitor-metric">
              <span class="mm-value" :class="m.cls">{{ m.value }}</span>
              <span class="mm-label">{{ m.label }}</span>
            </div>
          </div>
          <div v-if="loadingRun && !runDetail" class="mini-loading">加载运行详情…</div>
          <table v-else-if="runDetail && runDetail.results.length" class="monitor-table">
            <thead>
              <tr><th style="width:34%">问题</th><th>忠实</th><th>相关</th><th>精确</th><th>召回</th><th>延迟</th><th>状态</th></tr>
            </thead>
            <tbody>
              <template v-for="row in runDetail.results" :key="row.id">
                <tr class="result-row" @click="toggleExpand(row.id)">
                  <td class="q-cell"><span class="exp-caret" :class="{ open: expanded.has(row.id) }">▸</span>{{ row.question }}</td>
                  <td><span class="score" :class="scoreCls(row.faithfulness)">{{ pct(row.faithfulness) }}</span></td>
                  <td><span class="score" :class="scoreCls(row.answer_relevance)">{{ pct(row.answer_relevance) }}</span></td>
                  <td><span class="score" :class="scoreCls(row.context_precision)">{{ pct(row.context_precision) }}</span></td>
                  <td><span class="score" :class="scoreCls(row.context_recall)">{{ pct(row.context_recall) }}</span></td>
                  <td>{{ row.latency_ms }}ms</td>
                  <td><span class="status-pill sm" :class="row.status === 'ok' ? 'finished' : 'failed'">{{ row.status === 'ok' ? '成功' : '错误' }}</span></td>
                </tr>
                <tr v-if="expanded.has(row.id)" class="expand-row">
                  <td colspan="7">
                    <div class="expand-inner">
                      <div v-if="row.error" class="err-line">错误：{{ row.error }}</div>
                      <div class="expand-block">
                        <span class="expand-title">模型答案</span>
                        <div class="answer-box">{{ row.answer || '(空)' }}</div>
                      </div>
                      <div v-if="row.judge_reason" class="expand-block">
                        <span class="expand-title">评分理由</span>
                        <div class="answer-box">{{ row.judge_reason }}</div>
                      </div>
                    </div>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
          <p v-else-if="runDetail" class="empty-hint">暂无用例结果，运行产出结果后将自动刷新。</p>
          <p v-else class="empty-hint">该评估器还没有运行记录，点击「重新运行」在页内发起评测。</p>
        </section>

        <!-- ---- 评估器卡片格 ---- -->
        <div v-if="loadingEvals" class="mini-loading">加载中…</div>
        <div v-else-if="!filteredEvals.length" class="empty-block">
          <p>{{ evaluators.length ? '没有匹配「' + keyword + '」的评估器' : '还没有评估器，基于已有评测集构建评测工作流' }}</p>
          <button class="outline-btn" @click="openCreateEvaluator">新建评估器</button>
        </div>
        <div v-else class="card-grid">
          <div v-for="ev in filteredEvals" :key="ev.id" class="entry-card" @click="openRun(ev)">
            <div class="entry-card-head">
              <span class="entry-icon ev">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
                  <circle cx="5" cy="6" r="2.2" /><circle cx="19" cy="6" r="2.2" /><circle cx="12" cy="18" r="2.2" />
                  <path d="M7 7.5l3.5 8M17 7.5l-3.5 8M7.2 6h9.6" />
                </svg>
              </span>
              <span class="entry-name">{{ ev.name }}</span>
              <span class="card-actions">
                <button class="icon-btn" title="编辑评估器" @click.stop="openEditEvaluator(ev)">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                    <path d="M17 3a2.83 2.83 0 0 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
                  </svg>
                </button>
                <button class="icon-btn danger" title="删除评估器" @click.stop="removeEvaluator(ev)">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                    <polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                  </svg>
                </button>
              </span>
            </div>
            <p class="entry-desc">{{ ev.description || `基于评测集「${ev.dataset_name}」的评测工作流` }}</p>
            <div class="entry-tags">
              <span class="tag-chip ds-chip">{{ ev.dataset_name }}</span>
              <span class="tag-chip">{{ ev.case_count }} 用例</span>
              <span class="tag-chip muted">{{ ev.run_count }} 次运行</span>
              <span class="tag-chip" :class="runChipCls(ev.last_run)">{{ runStatusText(ev.last_run) }}</span>
            </div>
            <div class="entry-foot">
              <button class="run-btn" :disabled="startingRun || !ev.dataset_exists" @click.stop="runEvaluator(ev)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13">
                  <polygon points="5 3 19 12 5 21 5 3" />
                </svg>
                运行
              </button>
              <button v-if="ev.last_run" class="outline-btn sm" @click.stop="openRun(ev)">查看结果</button>
            </div>
          </div>
        </div>

        <!-- ---- 页内整合的 Prompt / Skill 专项评测工作台 ---- -->
        <section class="workbench-section">
          <div class="workbench-head">
            <div>
              <span class="workbench-title">Prompt / Skill 专项评测工作台</span>
              <span class="workbench-sub">静态断言 + Rubric 多维打分 + A/B 行为对比，已整合在本页，无需跳转</span>
            </div>
            <button class="tool-btn" @click="goPromptSkill">独立页面打开</button>
          </div>
          <PromptSkillEval />
        </section>
      </template>

      <!-- ============ 实验（跨评测集的运行历史） ============ -->
      <template v-else>
        <div v-if="loadingRuns" class="mini-loading">加载中…</div>
        <div v-else-if="!filteredRuns.length" class="empty-block">
          <p>{{ allRuns.length ? '没有匹配「' + keyword + '」的实验' : '还没有实验，选择评测集发起第一次运行' }}</p>
          <button class="outline-btn" @click="openCreateRun">新建实验</button>
        </div>
        <table v-else class="exp-table">
          <thead>
            <tr>
              <th>开始时间</th><th>评测集</th><th>状态</th><th>进度</th>
              <th>忠实</th><th>相关</th><th>精确</th><th>召回</th><th>延迟</th><th>发起人</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in filteredRuns" :key="r.id" class="exp-row" @click="goRun(r)">
              <td class="cell-time">{{ fmtTime(r.started_at) }}</td>
              <td class="exp-ds">{{ r.dataset_name }}</td>
              <td><span class="tag-chip" :class="runChipCls(r)">{{ statusLabel(r.status) }}</span></td>
              <td>{{ r.completed }}/{{ r.total }}</td>
              <td><span class="score" :class="scoreCls(r.summary?.avg_faithfulness)">{{ pct(r.summary?.avg_faithfulness) }}</span></td>
              <td><span class="score" :class="scoreCls(r.summary?.avg_answer_relevance)">{{ pct(r.summary?.avg_answer_relevance) }}</span></td>
              <td><span class="score" :class="scoreCls(r.summary?.avg_context_precision)">{{ pct(r.summary?.avg_context_precision) }}</span></td>
              <td><span class="score" :class="scoreCls(r.summary?.avg_context_recall)">{{ pct(r.summary?.avg_context_recall) }}</span></td>
              <td>{{ r.summary?.avg_latency_ms != null ? Math.round(r.summary.avg_latency_ms) + 'ms' : '-' }}</td>
              <td>{{ r.created_by || '-' }}</td>
            </tr>
          </tbody>
        </table>
      </template>
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
          <button class="outline-btn" @click="showCreateDataset = false">取消</button>
          <button class="btn-primary-btn" :disabled="creatingDataset || !datasetForm.name" @click="createDataset">
            {{ creatingDataset ? '创建中…' : '创建' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 新建/编辑评估器弹窗：基于已有评测集构建工作流 -->
    <div v-if="showCreateEvaluator" class="modal-overlay" @click.self="showCreateEvaluator = false">
      <div class="modal-box">
        <h3 class="modal-title">{{ editingEvaluatorId ? '编辑评估器' : '新建评估器' }}</h3>
        <p v-if="!editingEvaluatorId" class="modal-sub">选择一个已有评测集作为数据源构建评测工作流；创建后可在页内一键运行并查看结果。</p>
        <p v-if="!editingEvaluatorId && !datasets.length" class="modal-sub warn">还没有评测集，请先在「评测集」页签创建评测集并添加用例。</p>
        <label class="field">
          <span class="field-label">评估器名称 <em>*</em></span>
          <input class="input" v-model.trim="evaluatorForm.name" placeholder="如：问答主链路回归工作流" />
        </label>
        <label class="field">
          <span class="field-label">绑定评测集 <em>*</em></span>
          <select class="input" v-model="evaluatorForm.dataset_id" :disabled="!!editingEvaluatorId">
            <option value="" disabled>选择评测集</option>
            <option v-for="d in datasets" :key="d.id" :value="d.id">{{ d.name }}（{{ d.case_count }} 用例）</option>
          </select>
        </label>
        <label class="field">
          <span class="field-label">描述</span>
          <textarea class="input textarea" v-model.trim="evaluatorForm.description" rows="2" placeholder="工作流用途说明（选填）"></textarea>
        </label>
        <div class="modal-actions">
          <button class="outline-btn" @click="showCreateEvaluator = false">取消</button>
          <button class="btn-primary-btn" :disabled="savingEvaluator || !evaluatorForm.name || !evaluatorForm.dataset_id" @click="saveEvaluator">
            {{ savingEvaluator ? '保存中…' : editingEvaluatorId ? '保存' : '创建' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 新建实验弹窗：选择评测集发起一次运行 -->
    <div v-if="showCreateRun" class="modal-overlay" @click.self="showCreateRun = false">
      <div class="modal-box">
        <h3 class="modal-title">新建实验</h3>
        <p class="modal-sub">选择评测集发起一次评测运行；发起后在「实验」页签查看进度，点击行进入逐用例结果。</p>
        <p v-if="!datasets.length" class="modal-sub warn">还没有评测集，请先在「评测集」页签创建评测集并添加用例。</p>
        <label class="field">
          <span class="field-label">评测集 <em>*</em></span>
          <select class="input" v-model="runFormDatasetId">
            <option value="" disabled>选择评测集</option>
            <option v-for="d in datasets" :key="d.id" :value="d.id">{{ d.name }}（{{ d.case_count }} 用例）</option>
          </select>
        </label>
        <div class="modal-actions">
          <button class="outline-btn" @click="showCreateRun = false">取消</button>
          <button class="btn-primary-btn" :disabled="creatingRun || !runFormDatasetId" @click="createRun">
            {{ creatingRun ? '发起中…' : '发起实验' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import PromptSkillEval from '@/components/PromptSkillEval.vue'
import {
  listEvalDatasets, createEvalDataset, deleteEvalDataset,
  listEvalEvaluators, createEvalEvaluator, updateEvalEvaluator, deleteEvalEvaluator,
  startEvalRun, getEvalRun, listAllEvalRuns,
} from '@/api'

const router = useRouter()

// ---- 分段页签：评测集 | 评估器（基于评测集的评测工作流） ----
const tab = ref('datasets')
const keyword = ref('')
const focusedId = ref('')

const datasets = ref([])
const loadingDatasets = ref(false)
const evaluators = ref([])
const loadingEvals = ref(false)
const errorMsg = ref('')

onMounted(reloadAll)
onUnmounted(() => {
  stopPolling()
  stopRunsPolling()
})

function errMsg(e, fallback) {
  return e?.response?.data?.message || e?.response?.data?.detail || e?.message || fallback
}

function reloadAll() {
  loadDatasets()
  loadEvaluators()
  if (tab.value === 'experiments') loadRunsAll()
}

async function loadDatasets() {
  loadingDatasets.value = true
  try {
    const res = await listEvalDatasets()
    datasets.value = res.data.datasets || []
  } catch (e) {
    errorMsg.value = errMsg(e, '加载评测集失败')
  } finally {
    loadingDatasets.value = false
  }
}

async function loadEvaluators() {
  loadingEvals.value = true
  try {
    const res = await listEvalEvaluators()
    evaluators.value = res.data.evaluators || []
    // 运行结束后同步监控面板中的评估器快照（用例数/最近运行）
    if (activeRun.value) {
      const fresh = evaluators.value.find((x) => x.id === activeRun.value.evaluator.id)
      if (fresh) activeRun.value = { ...activeRun.value, evaluator: fresh }
    }
  } catch (e) {
    errorMsg.value = errMsg(e, '加载评估器失败')
  } finally {
    loadingEvals.value = false
  }
}

const filteredDatasets = computed(() => {
  if (!keyword.value) return datasets.value
  return datasets.value.filter((d) => (d.name || '').toLowerCase().includes(keyword.value.toLowerCase()))
})
const filteredEvals = computed(() => {
  if (!keyword.value) return evaluators.value
  const kw = keyword.value.toLowerCase()
  return evaluators.value.filter((e) =>
    (e.name || '').toLowerCase().includes(kw) || (e.dataset_name || '').toLowerCase().includes(kw))
})

function goDataset(id) {
  focusedId.value = id
  router.push({ name: 'EvalDataset', params: { datasetId: id } })
}

function goPromptSkill() {
  router.push({ name: 'EvalPromptSkill' })
}

// ---- 新建评测集 ----
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
    goDataset(res.data.dataset.id)
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
  } catch (e) {
    errorMsg.value = errMsg(e, '删除评测集失败')
  }
}

// ---- 新建/编辑评估器 ----
const showCreateEvaluator = ref(false)
const savingEvaluator = ref(false)
const editingEvaluatorId = ref('')
const evaluatorForm = reactive({ name: '', dataset_id: '', description: '' })

function openCreateEvaluator() {
  editingEvaluatorId.value = ''
  evaluatorForm.name = ''
  evaluatorForm.dataset_id = ''
  evaluatorForm.description = ''
  if (!datasets.value.length) loadDatasets()
  showCreateEvaluator.value = true
}

function openEditEvaluator(ev) {
  editingEvaluatorId.value = ev.id
  evaluatorForm.name = ev.name
  evaluatorForm.dataset_id = ev.dataset_id
  evaluatorForm.description = ev.description || ''
  showCreateEvaluator.value = true
}

async function saveEvaluator() {
  savingEvaluator.value = true
  try {
    if (editingEvaluatorId.value) {
      await updateEvalEvaluator(editingEvaluatorId.value, {
        name: evaluatorForm.name,
        description: evaluatorForm.description,
      })
    } else {
      await createEvalEvaluator({
        name: evaluatorForm.name,
        datasetId: evaluatorForm.dataset_id,
        description: evaluatorForm.description,
      })
    }
    showCreateEvaluator.value = false
    await loadEvaluators()
  } catch (e) {
    errorMsg.value = errMsg(e, editingEvaluatorId.value ? '保存评估器失败' : '创建评估器失败')
  } finally {
    savingEvaluator.value = false
  }
}

async function removeEvaluator(ev) {
  if (!window.confirm(`确认删除评估器「${ev.name}」？评测集与运行历史不受影响。`)) return
  try {
    await deleteEvalEvaluator(ev.id)
    evaluators.value = evaluators.value.filter((x) => x.id !== ev.id)
    if (activeRun.value?.evaluator.id === ev.id) closeRun()
  } catch (e) {
    errorMsg.value = errMsg(e, '删除评估器失败')
  }
}

// ---- 页内运行监控 ----
const activeRun = ref(null)      // { evaluator, runId }
const runDetail = ref(null)      // getEvalRun 响应
const loadingRun = ref(false)
const startingRun = ref(false)
const expanded = ref(new Set())
let pollTimer = null

async function runEvaluator(ev) {
  if (!ev.dataset_exists) {
    errorMsg.value = '该评估器绑定的评测集已删除，无法运行'
    return
  }
  startingRun.value = true
  errorMsg.value = ''
  try {
    const res = await startEvalRun(ev.dataset_id)
    activeRun.value = { evaluator: ev, runId: res.data.run_id }
    runDetail.value = null
    expanded.value = new Set()
    await loadRunDetail(true)
    startPolling()
  } catch (e) {
    if (e?.response?.status === 409) {
      errorMsg.value = e.response.data?.message || '已有评测任务在执行中，请等待其结束后再试'
    } else {
      errorMsg.value = errMsg(e, '发起评测失败')
    }
  } finally {
    startingRun.value = false
  }
}

function openRun(ev) {
  if (!ev.last_run) {
    runEvaluator(ev)
    return
  }
  activeRun.value = { evaluator: ev, runId: ev.last_run.id }
  expanded.value = new Set()
  loadRunDetail()
  if (ev.last_run.status === 'running') startPolling()
}

function closeRun() {
  stopPolling()
  activeRun.value = null
  runDetail.value = null
}

function startPolling() {
  stopPolling()
  pollTimer = setInterval(() => loadRunDetail(true), 2500)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function loadRunDetail(silent = false) {
  if (!activeRun.value?.runId) return
  if (!silent) loadingRun.value = true
  try {
    const res = await getEvalRun(activeRun.value.runId)
    runDetail.value = res.data
    if (res.data.run.status !== 'running') {
      stopPolling()
      loadEvaluators() // 刷新卡片上的最近运行状态
    }
  } catch (e) {
    if (!silent) errorMsg.value = errMsg(e, '加载运行详情失败')
  } finally {
    if (!silent) loadingRun.value = false
  }
}

function toggleExpand(id) {
  const next = new Set(expanded.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expanded.value = next
}

const progressText = computed(() => {
  if (!runDetail.value) return ''
  const done = runDetail.value.results?.length ?? 0
  const total = activeRun.value?.evaluator?.case_count ?? 0
  return total ? `${done} / ${total} 用例` : `${done} 用例`
})
const progressPct = computed(() => {
  const total = activeRun.value?.evaluator?.case_count ?? 0
  const done = runDetail.value?.results?.length ?? 0
  if (!total) return runDetail.value?.run.status === 'finished' ? '100%' : '0%'
  return Math.min(100, Math.round((done / total) * 100)) + '%'
})
const runSummaryCards = computed(() => {
  const s = runDetail.value?.run?.summary || {}
  if (s.total == null) return []
  return [
    { label: '用例', value: s.total },
    { label: '成功', value: s.ok ?? 0 },
    { label: '失败', value: s.error ?? 0, cls: s.error ? 'bad' : '' },
    { label: '忠实度', value: pct(s.avg_faithfulness) },
    { label: '答案相关', value: pct(s.avg_answer_relevance) },
    { label: '上下文精确', value: pct(s.avg_context_precision) },
    { label: '上下文召回', value: pct(s.avg_context_recall) },
    { label: '平均延迟', value: `${Math.round(s.avg_latency_ms || 0)}ms` },
  ]
})

// ---- 实验（跨评测集运行历史） ----
const allRuns = ref([])
const loadingRuns = ref(false)
const showCreateRun = ref(false)
const runFormDatasetId = ref('')
const creatingRun = ref(false)
let runsPollTimer = null

const filteredRuns = computed(() => {
  if (!keyword.value) return allRuns.value
  const kw = keyword.value.toLowerCase()
  return allRuns.value.filter((r) =>
    (r.dataset_name || '').toLowerCase().includes(kw) || (r.created_by || '').toLowerCase().includes(kw))
})

watch(tab, (t) => {
  if (t === 'experiments') loadRunsAll()
  else stopRunsPolling()
})

async function loadRunsAll() {
  if (!allRuns.value.length) loadingRuns.value = true
  try {
    const res = await listAllEvalRuns()
    allRuns.value = res.data.runs || []
    manageRunsPolling()
  } catch (e) {
    errorMsg.value = errMsg(e, '加载实验失败')
  } finally {
    loadingRuns.value = false
  }
}

function manageRunsPolling() {
  const running = allRuns.value.some((r) => r.status === 'running')
  if (running && !runsPollTimer) runsPollTimer = setInterval(loadRunsAll, 3000)
  else if (!running && runsPollTimer) stopRunsPolling()
}

function stopRunsPolling() {
  if (runsPollTimer) {
    clearInterval(runsPollTimer)
    runsPollTimer = null
  }
}

function openCreateRun() {
  runFormDatasetId.value = ''
  if (!datasets.value.length) loadDatasets()
  showCreateRun.value = true
}

async function createRun() {
  creatingRun.value = true
  errorMsg.value = ''
  try {
    await startEvalRun(runFormDatasetId.value)
    showCreateRun.value = false
    tab.value = 'experiments'
    await loadRunsAll()
  } catch (e) {
    errorMsg.value = e?.response?.status === 409
      ? (e.response.data?.message || '已有评测任务在执行中，请等待其结束后再试')
      : errMsg(e, '发起实验失败')
  } finally {
    creatingRun.value = false
  }
}

function goRun(r) {
  router.push({ name: 'EvalDataset', params: { datasetId: r.dataset_id }, query: { run: r.id } })
}

// ---- 格式化 ----
function fmtTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}
function pct(v) {
  return v == null ? '-' : (v * 100).toFixed(0) + '%'
}
function scoreCls(v) {
  if (v == null) return 'na'
  if (v >= 0.8) return 'good'
  if (v >= 0.5) return 'mid'
  return 'low'
}
function runStatusText(lastRun) {
  if (!lastRun) return '未运行'
  return `最近${statusLabel(lastRun.status)}`
}
function runChipCls(lastRun) {
  if (!lastRun) return 'muted'
  return { running: 'running', finished: 'ok', failed: 'bad', interrupted: 'muted' }[lastRun.status] || 'muted'
}
function runStatusCls(st) {
  return { running: 'running', finished: 'finished', failed: 'failed', interrupted: '' }[st] || ''
}
function statusLabel(st) {
  return { running: '执行中', finished: '已完成', failed: '失败', interrupted: '已中断' }[st] || st
}
</script>

<style scoped>
.eval-overview {
  height: 100vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  background:
    radial-gradient(1200px 400px at 100% -10%, #fbf1e2 0%, transparent 60%),
    var(--color-bg);
}

/* ---- 顶部（图1：标题 + 分段页签 + 搜索/刷新/新建） ---- */
.eval-header {
  border-bottom: 1px solid var(--color-border);
  background: rgba(254, 252, 249, 0.86);
  backdrop-filter: blur(8px);
  position: sticky;
  top: 0;
  z-index: 20;
  pointer-events: none; /* 头部按钮均显式恢复 pointer-events，避免吸顶吞点击 */
}
.header-inner {
  padding: 18px 32px 0;
  max-width: 1400px;
  width: 100%;
  margin: 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  flex-wrap: wrap;
}
.title-row { display: flex; align-items: center; gap: 20px; }
.page-title { font-size: 20px; font-weight: 700; margin: 0; letter-spacing: -0.3px; }
.seg-tabs {
  display: flex; gap: 2px; background: #f0ece6; border-radius: 9px; padding: 3px;
  pointer-events: auto;
}
.seg-tab {
  border: none; background: transparent; cursor: pointer; font-size: 13px; font-weight: 600;
  color: var(--color-text-secondary); padding: 6px 18px; border-radius: 7px; transition: all .15s;
}
.seg-tab:hover { color: var(--color-primary); }
.seg-tab.active { background: #cfe3fb; color: #1d4ed8; }
.toolbar { display: flex; align-items: center; gap: 8px; pointer-events: auto; }
.search-input {
  border: 1px solid var(--color-border); background: #fff; border-radius: 9px;
  padding: 7px 12px; font-size: 12.5px; width: 200px; color: var(--color-text); outline: none;
}
.search-input:focus { border-color: var(--color-primary); }
.tool-btn {
  display: inline-flex; align-items: center; gap: 5px; border: 1px solid var(--color-border);
  background: #fff; border-radius: 9px; padding: 7px 12px; font-size: 12.5px; font-weight: 600;
  color: var(--color-text-secondary); cursor: pointer;
}
.tool-btn:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
.tool-btn:disabled { opacity: 0.55; cursor: not-allowed; }
.btn-primary-btn {
  border: none; background: #17171a; color: #fff; border-radius: 9px; padding: 8px 16px;
  font-size: 12.5px; font-weight: 600; cursor: pointer;
}
.btn-primary-btn:hover:not(:disabled) { background: #2c2c31; }
.btn-primary-btn:disabled { opacity: 0.55; cursor: not-allowed; }
.header-sub {
  margin: 10px auto 14px; padding: 0 32px; max-width: 1400px;
  font-size: 12px; color: var(--color-text-secondary); line-height: 1.5;
}

.overview-body {
  flex: 1; padding: 20px 32px 48px; max-width: 1400px; width: 100%; margin: 0 auto;
  box-sizing: border-box;
}
.inline-error {
  background: #fdecea; border: 1px solid #f5c6c0; color: #c0392b; font-size: 12.5px;
  padding: 9px 13px; border-radius: 10px; margin-bottom: 12px;
}

/* ---- 卡片格（图1） ---- */
.card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }
.entry-card {
  background: #fff; border: 1px solid var(--color-border); border-radius: 12px;
  padding: 18px 20px; cursor: pointer; transition: all 0.16s;
  display: flex; flex-direction: column; gap: 10px;
}
.entry-card:hover { border-color: var(--color-primary); box-shadow: 0 4px 16px rgba(201,138,75,0.12); }
.entry-card.focused { border-color: #7fb3f2; box-shadow: 0 0 0 1px #7fb3f2; }
.entry-card-head { display: flex; align-items: center; gap: 10px; }
.entry-icon {
  width: 36px; height: 36px; border-radius: 9px; flex-shrink: 0;
  display: inline-flex; align-items: center; justify-content: center; color: #fff;
}
.entry-icon.ds { background: #f0762b; }
.entry-icon.ev { background: #2563eb; }
.entry-name {
  flex: 1; min-width: 0; font-size: 14.5px; font-weight: 700;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.card-actions { display: flex; gap: 2px; }
.icon-btn {
  width: 26px; height: 26px; display: inline-flex; align-items: center; justify-content: center;
  border: none; background: transparent; border-radius: 7px; cursor: pointer;
  color: var(--color-text-secondary); transition: all 0.15s;
}
.icon-btn:hover { background: #f0e7da; color: var(--color-primary); }
.icon-btn.danger:hover { background: #fdecea; color: #c0392b; }
.entry-desc {
  margin: 0; font-size: 12.5px; color: var(--color-text-secondary); line-height: 1.55;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.entry-tags { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.entry-foot { display: flex; align-items: center; gap: 8px; margin-top: 2px; }
.tag-chip {
  background: #f3e8fa; color: #8a4bb8; font-size: 11px; font-weight: 600;
  padding: 2px 9px; border-radius: 6px;
}
.tag-chip.muted { background: #f0ece6; color: var(--color-text-secondary); font-weight: 500; }
.tag-chip.ok { background: #e6f4ea; color: #2e7d46; }
.tag-chip.bad { background: #fdecea; color: #c0392b; }
.tag-chip.running { background: #fdf0d8; color: #b5822a; }
.tag-chip.ds-chip { background: #e3edfd; color: #1d4ed8; max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.status-pill {
  font-size: 11px; font-weight: 700; padding: 3px 10px; border-radius: 20px; white-space: nowrap;
  background: #ede6dc; color: var(--color-text-secondary);
}
.status-pill.sm { padding: 2px 8px; }
.status-pill.running { background: #fdf0d8; color: #b5822a; }
.status-pill.finished { background: #e6f4ea; color: #2e7d46; }
.status-pill.failed { background: #fdecea; color: #c0392b; }

/* ---- 运行按钮 ---- */
.run-btn {
  display: inline-flex; align-items: center; gap: 5px; border: none; cursor: pointer;
  background: #17171a; color: #fff; border-radius: 8px; padding: 6px 14px;
  font-size: 12px; font-weight: 600; transition: background 0.15s;
}
.run-btn:hover:not(:disabled) { background: #2c2c31; }
.run-btn:disabled { opacity: 0.55; cursor: not-allowed; }
.run-btn.sm { padding: 4px 10px; font-size: 11.5px; }

/* ---- 页内运行监控 ---- */
.run-monitor {
  background: #fff; border: 1px solid var(--color-border); border-radius: 14px;
  padding: 16px 18px; margin-bottom: 16px;
  border-left: 3px solid #2563eb;
}
.monitor-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }
.monitor-title {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 14px; font-weight: 700; color: #1d4ed8;
}
.monitor-progress-text { font-size: 12px; color: var(--color-text-secondary); font-variant-numeric: tabular-nums; }
.monitor-actions { margin-left: auto; display: inline-flex; align-items: center; gap: 6px; }
.progress-track { height: 6px; background: #f0ece6; border-radius: 4px; overflow: hidden; }
.progress-fill { height: 100%; background: linear-gradient(90deg, #4f8ef7, #2563eb); border-radius: 4px; transition: width 0.4s; }
.progress-fill.done { background: #2e9e5b; }
.monitor-metrics { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
.monitor-metric {
  background: #faf7f2; border-radius: 9px; padding: 8px 12px; min-width: 74px;
  display: flex; flex-direction: column; align-items: center; gap: 1px;
}
.mm-value { font-size: 15px; font-weight: 700; font-variant-numeric: tabular-nums; }
.mm-value.bad { color: #c0392b; }
.mm-label { font-size: 10.5px; color: var(--color-text-secondary); }
.monitor-table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 12px; }
.monitor-table th, .monitor-table td {
  padding: 7px 8px; text-align: left; border-bottom: 1px solid var(--color-border);
  color: var(--color-text); vertical-align: top;
}
.monitor-table th { color: var(--color-text-secondary); font-weight: 600; white-space: nowrap; }
.monitor-table .result-row { cursor: pointer; }
.monitor-table .result-row:hover { background: var(--color-primary-light, #fbf1e2); }
.q-cell { line-height: 1.5; }
.exp-caret { display: inline-block; margin-right: 5px; color: var(--color-text-secondary); transition: transform 0.15s; }
.exp-caret.open { transform: rotate(90deg); }
.score { font-weight: 700; font-variant-numeric: tabular-nums; }
.score.good { color: #2e7d46; }
.score.mid { color: #b5822a; }
.score.low { color: #c0392b; }
.score.na { color: var(--color-text-secondary); font-weight: 500; }
.expand-row td { background: #fbf8f3; }
.expand-inner { display: flex; flex-direction: column; gap: 8px; padding: 4px 2px; }
.err-line { color: #c0392b; font-size: 12px; }
.expand-block { display: flex; flex-direction: column; gap: 4px; }
.expand-title { font-size: 11px; font-weight: 700; color: var(--color-text-secondary); }
.answer-box {
  background: #fff; border: 1px solid var(--color-border); border-radius: 8px;
  padding: 8px 10px; font-size: 12px; line-height: 1.6; white-space: pre-wrap;
  max-height: 180px; overflow-y: auto;
}
.empty-hint { font-size: 12px; color: var(--color-text-secondary); margin: 10px 0 0; }

/* ---- 页内整合工作台 ---- */
.workbench-section {
  margin-top: 26px; border-top: 1px dashed var(--color-border); padding-top: 18px;
}
.workbench-head {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  margin-bottom: 12px; flex-wrap: wrap;
}
.workbench-title { font-size: 14px; font-weight: 700; display: block; }
.workbench-sub { font-size: 11.5px; color: var(--color-text-secondary); display: block; margin-top: 2px; }

/* ---- 实验表格 ---- */
.exp-table {
  width: 100%; border-collapse: collapse; font-size: 12.5px;
  background: #fff; border: 1px solid var(--color-border); border-radius: 12px; overflow: hidden;
}
.exp-table th, .exp-table td { padding: 9px 10px; text-align: left; border-bottom: 1px solid var(--color-border); }
.exp-table th { background: rgba(0,0,0,0.03); font-weight: 600; color: var(--color-text-secondary); white-space: nowrap; }
.exp-table tbody tr:last-child td { border-bottom: none; }
.exp-row { cursor: pointer; }
.exp-row:hover { background: #fdfaf5; }
.exp-ds { font-weight: 600; }
.cell-time { white-space: nowrap; color: var(--color-text-secondary); }

/* ---- 空/加载 ---- */
.mini-loading { padding: 26px; text-align: center; color: var(--color-text-secondary); font-size: 12.5px; }
.empty-block {
  border: 1px dashed var(--color-border); border-radius: 14px; padding: 40px 16px;
  text-align: center; color: var(--color-text-secondary); font-size: 12.5px;
  display: flex; flex-direction: column; gap: 12px; align-items: center; background: rgba(255,255,255,0.5);
}
.outline-btn {
  border: 1px solid var(--color-border); background: #fff; color: var(--color-text-secondary);
  border-radius: 9px; padding: 7px 14px; font-size: 12.5px; font-weight: 600; cursor: pointer;
}
.outline-btn.sm { padding: 5px 11px; font-size: 12px; }
.outline-btn:hover { border-color: var(--color-primary); color: var(--color-primary); }

/* ---- 弹窗 / 表单 ---- */
.modal-overlay { position: fixed; inset: 0; background: rgba(45,42,36,0.4); display: flex; align-items: center; justify-content: center; z-index: 100; }
.modal-box { width: 440px; max-width: 92vw; background: #fff; border-radius: 16px; padding: 22px 24px; box-shadow: 0 16px 48px rgba(0,0,0,0.2); display: flex; flex-direction: column; gap: 12px; }
.modal-title { font-size: 16px; font-weight: 700; margin-bottom: 2px; }
.modal-sub { margin: -4px 0 0; font-size: 12px; color: var(--color-text-secondary); line-height: 1.55; }
.modal-sub.warn { color: #c0392b; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 4px; }
.field { display: flex; flex-direction: column; gap: 5px; }
.field-label { font-size: 11.5px; font-weight: 600; color: var(--color-text-secondary); }
.field-label em { color: #c0392b; font-style: normal; }
.input {
  width: 100%; padding: 9px 11px; font-size: 13px; border: 1px solid var(--color-border);
  border-radius: 9px; background: #fffdfa; color: var(--color-text); outline: none; transition: border-color 0.15s;
  font-family: inherit; box-sizing: border-box;
}
.input:focus { border-color: var(--color-primary); background: #fff; }
.textarea { resize: vertical; line-height: 1.55; }
select.input { appearance: auto; background: #fff; }

/* ---- 响应式 ---- */
@media (max-width: 768px) {
  .eval-header { padding-top: 44px; }
  .header-inner { padding: 16px 16px 0; }
  .header-sub { padding: 0 16px; }
  .overview-body { padding: 16px 16px 40px; }
  .toolbar { width: 100%; }
  .search-input { flex: 1; width: auto; }
}
</style>
