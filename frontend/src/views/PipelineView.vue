<template>
  <div class="pipeline-view">
    <!-- 顶部标题栏 -->
    <header class="pl-header">
      <div class="header-inner">
        <div class="header-title">
          <span class="header-eyebrow">CI/CD · Evaluation-Driven Deployment</span>
          <h1>发布流水线</h1>
          <p class="header-sub">
            面向 Agent 系统的评估驱动部署：代码提交 → 单元测试 → 评估回归 → 构建镜像 → 灰度发布 → 全量上线。
            评估回归阶段以 Ragas 四指标均值作为质量分，低于阈值将自动阻断后续发布。
          </p>
        </div>
      </div>
    </header>

    <div class="pl-body">
      <!-- ============ 操作区 ============ -->
      <section class="card run-bar">
        <div class="run-bar-left">
          <label class="field">
            <span class="field-label">Git 引用（分支 / Tag / Commit，可留空）</span>
            <input
              v-model.trim="gitRef"
              class="field-input"
              placeholder="如 main 或 v1.2.0"
              :disabled="isRunning"
            />
          </label>
        </div>
        <div class="run-bar-right">
          <button class="btn btn-primary" :disabled="isRunning || starting" @click="onStart">
            {{ starting ? '发起中…' : (isRunning ? '运行中…' : '发起流水线') }}
          </button>
          <button
            v-if="isRunning"
            class="btn btn-danger"
            :disabled="aborting"
            @click="onAbort"
          >
            {{ aborting ? '中止中…' : '中止运行' }}
          </button>
          <button class="btn btn-ghost" @click="showSettings = !showSettings">
            {{ showSettings ? '收起配置' : 'EDD 配置' }}
          </button>
        </div>
      </section>

      <p v-if="errorMsg" class="err-tip">{{ errorMsg }}</p>

      <!-- ============ EDD 配置面板 ============ -->
      <section v-if="showSettings" class="card settings-card">
        <h3 class="card-title">流水线配置</h3>
        <div class="settings-grid">
          <label class="field">
            <span class="field-label">EDD 质量阈值（0~1）</span>
            <input
              v-model="settingsForm.edd_threshold"
              type="number"
              step="0.01"
              min="0"
              max="1"
              class="field-input"
            />
          </label>
          <label class="field">
            <span class="field-label">评估回归使用的评测集</span>
            <select v-model="settingsForm.edd_dataset_id" class="field-input">
              <option value="">（默认首个评测集）</option>
              <option v-for="d in datasets" :key="d.id" :value="d.id">
                {{ d.name }}（{{ d.case_count }} 用例）
              </option>
            </select>
          </label>
          <label class="field checkbox">
            <input type="checkbox" v-model="settingsForm.run_unit_test" />
            <span>启用单元测试阶段（pytest）</span>
          </label>
          <label class="field checkbox">
            <input type="checkbox" v-model="settingsForm.run_build_image" />
            <span>启用构建镜像阶段（docker build）</span>
          </label>
          <label class="field span-2">
            <span class="field-label">构建镜像命令（留空则用默认 docker build）</span>
            <input v-model.trim="settingsForm.build_cmd" class="field-input mono" placeholder="docker build -f Dockerfile.backend -t app:latest ." />
          </label>
          <label class="field span-2">
            <span class="field-label">灰度发布命令（留空则跳过该阶段）</span>
            <input v-model.trim="settingsForm.canary_cmd" class="field-input mono" placeholder="./scripts/deploy-aliyun.sh canary" />
          </label>
          <label class="field span-2">
            <span class="field-label">全量上线命令（留空则跳过该阶段）</span>
            <input v-model.trim="settingsForm.full_cmd" class="field-input mono" placeholder="./scripts/deploy-aliyun.sh prod" />
          </label>
        </div>
        <div class="settings-actions">
          <button class="btn btn-primary" :disabled="savingSettings" @click="onSaveSettings">
            {{ savingSettings ? '保存中…' : '保存配置' }}
          </button>
          <span v-if="settingsSavedTip" class="ok-tip">已保存</span>
        </div>
      </section>

      <!-- ============ 当前运行 ============ -->
      <section v-if="currentRun" class="card">
        <div class="card-head">
          <h3 class="card-title">
            当前运行
            <span class="status-pill" :class="runStatusClass(currentRun.status)">{{ runStatusText(currentRun.status) }}</span>
          </h3>
          <span class="card-meta">
            {{ currentRun.git_ref ? `ref: ${currentRun.git_ref} · ` : '' }}触发: {{ currentRun.trigger }} · {{ formatTime(currentRun.started_at) }}
          </span>
        </div>

        <!-- 六阶段流程图 -->
        <div class="stage-flow">
          <template v-for="(st, idx) in currentRun.stages" :key="st.name">
            <div
              class="stage-node"
              :class="[`st-${st.status}`, { active: expandedStage === st.name }]"
              @click="toggleStage(st.name)"
            >
              <div class="stage-dot">
                <span v-if="st.status === 'running'" class="dot-spinner"></span>
                <span v-else-if="st.status === 'passed'">✓</span>
                <span v-else-if="st.status === 'failed'">✕</span>
                <span v-else-if="st.status === 'skipped'">↷</span>
                <span v-else>·</span>
              </div>
              <div class="stage-name">{{ st.label }}</div>
              <div class="stage-status-text">{{ stageStatusText(st.status) }}</div>
            </div>
            <div v-if="idx < currentRun.stages.length - 1" class="stage-arrow" :class="`ar-${st.status}`">→</div>
          </template>
        </div>

        <!-- EDD 分数展示 -->
        <div v-if="eddInfo" class="edd-banner" :class="eddInfo.passed ? 'edd-ok' : 'edd-bad'">
          <span class="edd-icon">{{ eddInfo.passed ? '🛡️' : '⛔' }}</span>
          <div class="edd-text">
            <strong>评估回归质量分：{{ eddInfo.score }}</strong>
            <span>阈值 {{ eddInfo.threshold }} · {{ eddInfo.passed ? '通过，允许发布' : '未达标，已阻断发布' }}</span>
          </div>
        </div>

        <!-- 阶段日志查看器 -->
        <div v-if="expandedStage" class="log-viewer">
          <div class="log-head">
            <span>{{ stageLabel(expandedStage) }} · 日志</span>
            <button class="icon-btn" @click="expandedStage = ''">收起</button>
          </div>
          <pre class="log-body">{{ stageLog(expandedStage) || '（暂无日志输出）' }}</pre>
        </div>
      </section>

      <div v-else-if="!loadingRun" class="card empty-card">
        <p>暂无进行中的运行。发起一次流水线，或从下方历史中选择查看。</p>
      </div>

      <!-- ============ 运行历史 ============ -->
      <section class="card">
        <div class="card-head">
          <h3 class="card-title">运行历史</h3>
          <button class="btn btn-ghost sm" @click="loadRuns">刷新</button>
        </div>
        <div v-if="loadingRuns && !runs.length" class="mini-loading">加载中…</div>
        <div v-else-if="!runs.length" class="mini-empty">还没有运行记录</div>
        <table v-else class="run-table">
          <thead>
            <tr>
              <th>状态</th>
              <th>触发</th>
              <th>Git 引用</th>
              <th>EDD 分数</th>
              <th>阶段</th>
              <th>发起人</th>
              <th>开始时间</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="r in runs"
              :key="r.id"
              :class="{ 'row-active': currentRun && currentRun.id === r.id }"
              @click="selectRun(r.id)"
            >
              <td><span class="status-pill" :class="runStatusClass(r.status)">{{ runStatusText(r.status) }}</span></td>
              <td>{{ r.trigger }}</td>
              <td class="mono">{{ r.git_ref || '—' }}</td>
              <td>{{ r.edd_score != null ? r.edd_score : '—' }}</td>
              <td class="mini-stages">
                <span
                  v-for="s in r.stages"
                  :key="s.name"
                  class="mini-dot"
                  :class="`st-${s.status}`"
                  :title="`${s.label}: ${stageStatusText(s.status)}`"
                ></span>
              </td>
              <td>{{ r.created_by || '—' }}</td>
              <td>{{ formatTime(r.started_at) }}</td>
              <td><span class="link">查看</span></td>
            </tr>
          </tbody>
        </table>
      </section>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount } from 'vue'
import {
  getPipelineStages,
  getPipelineSettings,
  savePipelineSettings,
  startPipelineRun,
  listPipelineRuns,
  getPipelineRun,
  abortPipelineRun,
  listEvalDatasets,
} from '@/api'

const stages = ref([])
const datasets = ref([])
const runs = ref([])
const currentRun = ref(null)

const gitRef = ref('')
const starting = ref(false)
const aborting = ref(false)
const loadingRuns = ref(false)
const loadingRun = ref(false)
const errorMsg = ref('')

const showSettings = ref(false)
const savingSettings = ref(false)
const settingsSavedTip = ref(false)
const settingsForm = reactive({
  edd_threshold: 0.85,
  edd_dataset_id: '',
  run_unit_test: true,
  run_build_image: false,
  build_cmd: '',
  canary_cmd: '',
  full_cmd: '',
})

const expandedStage = ref('')

let pollTimer = null

const isRunning = computed(() => currentRun.value && currentRun.value.status === 'running')

// 评估回归门控信息：优先取运行 summary，其次取 eval_regression 阶段 detail
const eddInfo = computed(() => {
  const run = currentRun.value
  if (!run) return null
  const threshold = run.edd_threshold != null ? run.edd_threshold : 0.85
  let score = run.edd_score
  if (score == null) {
    const st = (run.stages || []).find((s) => s.name === 'eval_regression')
    if (st && st.detail && st.detail.score != null) score = st.detail.score
  }
  if (score == null) return null
  return { score: Number(score).toFixed(4), threshold, passed: Number(score) >= Number(threshold) }
})

function stageStatusText(s) {
  return { pending: '等待中', running: '执行中', passed: '通过', failed: '失败', skipped: '已跳过', aborted: '已中止' }[s] || s
}
function runStatusText(s) {
  return { running: '运行中', finished: '成功', failed: '失败', aborted: '已中止' }[s] || s
}
function runStatusClass(s) {
  return { running: 'pill-running', finished: 'pill-ok', failed: 'pill-fail', aborted: 'pill-skip' }[s] || ''
}
function stageLabel(name) {
  const st = (currentRun.value?.stages || []).find((s) => s.name === name)
  return st ? st.label : name
}
function stageLog(name) {
  const st = (currentRun.value?.stages || []).find((s) => s.name === name)
  return st ? st.log : ''
}
function toggleStage(name) {
  expandedStage.value = expandedStage.value === name ? '' : name
}
function formatTime(iso) {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    return d.toLocaleString('zh-CN', { hour12: false })
  } catch (e) {
    return iso
  }
}

async function loadStages() {
  try {
    const res = await getPipelineStages()
    stages.value = res.data.stages || []
  } catch (e) {
    console.error('加载阶段元信息失败', e)
  }
}

async function loadDatasets() {
  try {
    const res = await listEvalDatasets()
    datasets.value = res.data.datasets || []
  } catch (e) {
    console.error('加载评测集失败', e)
  }
}

async function loadSettings() {
  try {
    const res = await getPipelineSettings()
    const s = res.data.settings || {}
    settingsForm.edd_threshold = s.edd_threshold != null ? s.edd_threshold : 0.85
    settingsForm.edd_dataset_id = s.edd_dataset_id || ''
    settingsForm.run_unit_test = !!Number(s.run_unit_test)
    settingsForm.run_build_image = !!Number(s.run_build_image)
    settingsForm.build_cmd = s.build_cmd || ''
    settingsForm.canary_cmd = s.canary_cmd || ''
    settingsForm.full_cmd = s.full_cmd || ''
  } catch (e) {
    console.error('加载配置失败', e)
  }
}

async function onSaveSettings() {
  savingSettings.value = true
  settingsSavedTip.value = false
  errorMsg.value = ''
  try {
    await savePipelineSettings({
      edd_threshold: Number(settingsForm.edd_threshold),
      edd_dataset_id: settingsForm.edd_dataset_id,
      run_unit_test: settingsForm.run_unit_test,
      run_build_image: settingsForm.run_build_image,
      build_cmd: settingsForm.build_cmd,
      canary_cmd: settingsForm.canary_cmd,
      full_cmd: settingsForm.full_cmd,
    })
    settingsSavedTip.value = true
    setTimeout(() => (settingsSavedTip.value = false), 2000)
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '保存配置失败'
  } finally {
    savingSettings.value = false
  }
}

async function loadRuns() {
  loadingRuns.value = true
  try {
    const res = await listPipelineRuns(20)
    runs.value = res.data.runs || []
  } catch (e) {
    console.error('加载运行历史失败', e)
  } finally {
    loadingRuns.value = false
  }
}

async function selectRun(runId) {
  loadingRun.value = true
  expandedStage.value = ''
  try {
    const res = await getPipelineRun(runId)
    currentRun.value = res.data.run
    schedulePoll()
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '加载运行详情失败'
  } finally {
    loadingRun.value = false
  }
}

async function refreshCurrentRun() {
  if (!currentRun.value) return
  try {
    const res = await getPipelineRun(currentRun.value.id)
    currentRun.value = res.data.run
    // 运行结束后刷新历史列表
    if (res.data.run.status !== 'running') {
      await loadRuns()
    }
  } catch (e) {
    console.error('刷新运行详情失败', e)
  }
}

function schedulePoll() {
  clearTimeout(pollTimer)
  if (currentRun.value && currentRun.value.status === 'running') {
    pollTimer = setTimeout(async () => {
      await refreshCurrentRun()
      schedulePoll()
    }, 2000)
  }
}

async function onStart() {
  starting.value = true
  errorMsg.value = ''
  try {
    const res = await startPipelineRun(gitRef.value, 'manual')
    await loadRuns()
    await selectRun(res.data.run_id)
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '发起流水线失败'
  } finally {
    starting.value = false
  }
}

async function onAbort() {
  if (!currentRun.value) return
  aborting.value = true
  errorMsg.value = ''
  try {
    await abortPipelineRun(currentRun.value.id)
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '中止失败'
  } finally {
    aborting.value = false
  }
}

onMounted(async () => {
  await Promise.all([loadStages(), loadDatasets(), loadSettings(), loadRuns()])
  // 若历史中存在运行中的记录，自动加载其详情并轮询
  const running = runs.value.find((r) => r.status === 'running')
  if (running) {
    await selectRun(running.id)
  }
})

onBeforeUnmount(() => {
  clearTimeout(pollTimer)
})
</script>

<style scoped>
.pipeline-view {
  height: 100vh;
  overflow-y: auto;
  background:
    radial-gradient(1200px 400px at 100% -10%, #fbf1e2 0%, transparent 60%),
    var(--color-bg);
}

/* ---- 顶部 ---- */
.pl-header {
  border-bottom: 1px solid var(--color-border);
  background: rgba(254, 252, 249, 0.86);
  backdrop-filter: blur(8px);
  position: sticky;
  top: 0;
  z-index: 20;
}
.header-inner { padding: 22px 32px 18px; max-width: 1200px; margin: 0 auto; }
.header-eyebrow {
  font-size: 11px; letter-spacing: 2.5px; text-transform: uppercase;
  color: var(--color-primary); font-weight: 700;
}
.header-title h1 { font-size: 22px; font-weight: 700; margin: 2px 0 4px; letter-spacing: -0.3px; }
.header-sub { font-size: 12.5px; color: var(--color-text-secondary); max-width: 820px; line-height: 1.6; }

.pl-body { max-width: 1200px; margin: 0 auto; padding: 20px 32px 48px; display: flex; flex-direction: column; gap: 18px; }

/* ---- 卡片 ---- */
.card {
  background: #fffdfb;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 18px 20px;
  box-shadow: 0 1px 3px rgba(120, 90, 50, 0.04);
}
.card-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
.card-title { font-size: 15px; font-weight: 700; display: flex; align-items: center; gap: 10px; }
.card-meta { font-size: 12px; color: var(--color-text-secondary); }

/* ---- 操作区 ---- */
.run-bar { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; flex-wrap: wrap; }
.run-bar-left { flex: 1; min-width: 260px; }
.run-bar-right { display: flex; gap: 10px; flex-wrap: wrap; }

.field { display: flex; flex-direction: column; gap: 6px; }
.field-label { font-size: 12px; color: var(--color-text-secondary); font-weight: 600; }
.field-input {
  padding: 8px 11px; border: 1px solid var(--color-border); border-radius: 8px;
  font-size: 13px; background: #fff; color: var(--color-text); outline: none;
  transition: border-color 0.15s;
}
.field-input:focus { border-color: var(--color-primary); }
.field-input.mono, .mono { font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', monospace; font-size: 12px; }
.field.checkbox { flex-direction: row; align-items: center; gap: 8px; font-size: 13px; }

/* ---- 按钮 ---- */
.btn {
  padding: 8px 16px; border-radius: 8px; font-size: 13px; font-weight: 600;
  cursor: pointer; border: 1px solid transparent; transition: all 0.15s; white-space: nowrap;
}
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn.sm { padding: 5px 12px; font-size: 12px; }
.btn-primary { background: var(--color-primary); color: #fff; }
.btn-primary:hover:not(:disabled) { background: #b87a3f; }
.btn-danger { background: #d9534f; color: #fff; }
.btn-danger:hover:not(:disabled) { background: #c9302c; }
.btn-ghost { background: transparent; border-color: var(--color-border); color: var(--color-text); }
.btn-ghost:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
.icon-btn { background: transparent; border: none; color: var(--color-text-secondary); cursor: pointer; font-size: 12px; }
.icon-btn:hover { color: var(--color-primary); }

.err-tip { color: #d9534f; font-size: 12.5px; padding: 0 4px; }
.ok-tip { color: #4a9d5f; font-size: 12.5px; align-self: center; }

/* ---- 配置面板 ---- */
.settings-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px 20px; }
.settings-grid .span-2 { grid-column: span 2; }
.settings-actions { display: flex; align-items: center; gap: 12px; margin-top: 16px; }

/* ---- 状态胶囊 ---- */
.status-pill { font-size: 11px; font-weight: 700; padding: 2px 9px; border-radius: 10px; }
.pill-running { background: #fdf0d9; color: #b8802a; }
.pill-ok { background: #e4f4e8; color: #3d8b52; }
.pill-fail { background: #fbe4e3; color: #c0392b; }
.pill-skip { background: #eee9e2; color: #8a7e72; }

/* ---- 阶段流程图 ---- */
.stage-flow { display: flex; align-items: flex-start; gap: 4px; overflow-x: auto; padding: 8px 0 4px; }
.stage-node {
  flex: 1; min-width: 96px; display: flex; flex-direction: column; align-items: center; gap: 6px;
  padding: 12px 6px; border-radius: 10px; cursor: pointer; border: 1px solid transparent;
  transition: all 0.15s; text-align: center;
}
.stage-node:hover { background: #faf6f0; }
.stage-node.active { border-color: var(--color-primary); background: var(--color-primary-light); }
.stage-dot {
  width: 34px; height: 34px; border-radius: 50%; display: flex; align-items: center; justify-content: center;
  font-size: 15px; font-weight: 700; border: 2px solid var(--color-border); background: #fff; color: var(--color-text-secondary);
}
.stage-name { font-size: 12.5px; font-weight: 600; }
.stage-status-text { font-size: 11px; color: var(--color-text-secondary); }
.stage-arrow { align-self: center; font-size: 18px; color: var(--color-border); padding-top: 14px; }

.st-pending .stage-dot { border-color: var(--color-border); color: var(--color-text-secondary); }
.st-running .stage-dot { border-color: var(--color-primary); color: var(--color-primary); }
.st-passed .stage-dot { border-color: #4a9d5f; background: #e4f4e8; color: #3d8b52; }
.st-failed .stage-dot { border-color: #d9534f; background: #fbe4e3; color: #c0392b; }
.st-skipped .stage-dot { border-color: #d8cfc2; background: #f2ede6; color: #a99c8c; }
.st-aborted .stage-dot { border-color: #d8cfc2; background: #f2ede6; color: #a99c8c; }
.ar-passed { color: #4a9d5f; }
.dot-spinner {
  width: 15px; height: 15px; border: 2px solid var(--color-primary-light);
  border-top-color: var(--color-primary); border-radius: 50%; animation: spin 0.7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ---- EDD banner ---- */
.edd-banner {
  display: flex; align-items: center; gap: 12px; margin-top: 16px;
  padding: 12px 16px; border-radius: 10px; font-size: 13px;
}
.edd-banner .edd-icon { font-size: 20px; }
.edd-text { display: flex; flex-direction: column; gap: 2px; }
.edd-text strong { font-size: 14px; }
.edd-text span { font-size: 12px; opacity: 0.85; }
.edd-ok { background: #e4f4e8; color: #2f6b40; }
.edd-bad { background: #fbe4e3; color: #a83226; }

/* ---- 日志查看器 ---- */
.log-viewer { margin-top: 16px; border: 1px solid var(--color-border); border-radius: 10px; overflow: hidden; }
.log-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 12px; background: #faf6f0; font-size: 12.5px; font-weight: 600;
}
.log-body {
  margin: 0; padding: 12px 14px; background: #2d2a24; color: #e8e0d4;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', monospace; font-size: 12px;
  line-height: 1.6; max-height: 340px; overflow: auto; white-space: pre-wrap; word-break: break-all;
}

/* ---- 空态 / 加载 ---- */
.empty-card { text-align: center; color: var(--color-text-secondary); font-size: 13px; padding: 32px; }
.mini-loading, .mini-empty { color: var(--color-text-secondary); font-size: 13px; padding: 16px 0; text-align: center; }

/* ---- 运行历史表 ---- */
.run-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.run-table th {
  text-align: left; padding: 8px 10px; color: var(--color-text-secondary);
  font-weight: 600; border-bottom: 1px solid var(--color-border); font-size: 11.5px;
}
.run-table td { padding: 10px; border-bottom: 1px solid #f4efe8; }
.run-table tbody tr { cursor: pointer; transition: background 0.12s; }
.run-table tbody tr:hover { background: #faf6f0; }
.run-table tbody tr.row-active { background: var(--color-primary-light); }
.mini-stages { display: flex; gap: 4px; }
.mini-dot { width: 10px; height: 10px; border-radius: 50%; background: var(--color-border); display: inline-block; }
.mini-dot.st-running { background: var(--color-primary); }
.mini-dot.st-passed { background: #4a9d5f; }
.mini-dot.st-failed { background: #d9534f; }
.mini-dot.st-skipped, .mini-dot.st-aborted { background: #d8cfc2; }
.link { color: var(--color-primary); font-weight: 600; }
</style>
