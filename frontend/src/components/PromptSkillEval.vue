<template>
  <div class="pse">
    <div class="pse-layout">
      <!-- ============ 左栏：评测目标 ============ -->
      <aside class="pse-targets">
        <div class="panel-head">
          <span class="panel-title">评测目标</span>
          <button class="icon-btn" title="刷新" :disabled="loading" @click="loadAll">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="15" height="15">
              <polyline points="23 4 23 10 17 10" /><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
            </svg>
          </button>
        </div>
        <p class="pse-hint">对系统提示词与用户技能做专项评测：静态断言 + Rubric 多维打分 + A/B 行为对比，并据结果一键优化上线。</p>
        <div v-if="loadingTargets" class="mini-loading">加载中…</div>
        <ul v-else class="target-list">
          <li
            v-for="t in targets"
            :key="t.target_type + ':' + t.target_id"
            class="target-item"
            :class="{ active: isSelected(t) }"
            @click="selectTarget(t)"
          >
            <div class="target-main">
              <span class="target-name">{{ t.name }}</span>
              <span class="target-meta">
                <em class="tag" :class="t.target_type">{{ t.target_type === 'prompt' ? '提示词' : (t.kind === 'playbook' ? 'Skill·策略' : 'Skill·提示') }}</em>
                <em v-if="t.builtin" class="tag builtin">内置</em>
                {{ t.scope === 'chat' ? '对话' : (t.scope === 'url_import' ? '链接解析' : (t.scope === 'polish' ? '正文清洗' : t.scope)) }}
              </span>
            </div>
          </li>
        </ul>
      </aside>

      <!-- ============ 右栏：评测工作区 ============ -->
      <main class="pse-work">
        <div v-if="!selectedTarget" class="placeholder">
          <p>选择左侧一个评测目标开始</p>
        </div>
        <template v-else>
          <div class="work-head">
            <div>
              <h2>{{ selectedTarget.name }}</h2>
              <p class="work-sub">当前文本 {{ (selectedTarget.text || '').length }} 字</p>
            </div>
            <button class="btn btn-primary" :disabled="startingEval || busy" @click="startEval">
              <span v-if="startingEval" class="spinner-xs"></span>
              {{ startingEval ? '评测中…' : '发起评测' }}
            </button>
          </div>

          <p v-if="errorMsg" class="inline-error">{{ errorMsg }}</p>
          <p v-if="okMsg" class="inline-ok">{{ okMsg }}</p>

          <!-- 当前文本预览 -->
          <details class="card pse-collapse">
            <summary>查看当前文本</summary>
            <pre class="text-box">{{ selectedTarget.text }}</pre>
          </details>

          <!-- 历史评测 -->
          <div class="card" v-if="evals.length">
            <div class="card-head"><h3>评测历史</h3></div>
            <ul class="eval-history">
              <li
                v-for="e in evals"
                :key="e.id"
                :class="{ active: selectedEval && selectedEval.id === e.id }"
                @click="openEval(e.id)"
              >
                <span class="eh-score" :class="scoreTone(e.overall_score)">{{ e.overall_score != null ? e.overall_score.toFixed(2) : '—' }}</span>
                <span class="eh-status" :class="e.status">{{ statusText(e.status) }}</span>
                <span class="eh-time">{{ fmtTime(e.created_at) }}</span>
                <em v-if="e.published" class="tag published">已上线</em>
              </li>
            </ul>
          </div>

          <!-- 评测详情 -->
          <template v-if="selectedEval">
            <div v-if="selectedEval.status === 'running'" class="card running-card">
              <span class="spinner-xs"></span> 评测执行中，正在打分与生成优化建议…
            </div>
            <template v-else>
              <!-- 总分 + 断言 -->
              <div class="card">
                <div class="card-head">
                  <h3>质量评估</h3>
                  <span class="overall" :class="scoreTone(selectedEval.overall_score)">
                    总分 {{ selectedEval.overall_score != null ? selectedEval.overall_score.toFixed(2) : '—' }} / 5
                  </span>
                </div>
                <div class="assert-grid">
                  <div v-for="a in selectedEval.assertions" :key="a.key" class="assert-item" :class="a.passed ? 'pass' : 'fail'">
                    <span class="assert-mark">{{ a.passed ? '✓' : '✕' }}</span>
                    <span class="assert-label">{{ a.label }}</span>
                    <span class="assert-detail">{{ a.detail }}</span>
                  </div>
                </div>
                <div class="dim-list">
                  <div v-for="d in selectedEval.rubric.dimensions" :key="d.key" class="dim-row">
                    <span class="dim-label">{{ d.label }}</span>
                    <div class="dim-bar"><span :class="scoreTone(d.score)" :style="{ width: dimWidth(d.score) }"></span></div>
                    <span class="dim-score">{{ d.score != null ? d.score.toFixed(1) : '—' }}</span>
                    <span class="dim-reason">{{ d.reason }}</span>
                  </div>
                </div>
              </div>

              <!-- 优化建议 -->
              <div class="card">
                <div class="card-head"><h3>优化建议</h3>
                  <em v-if="selectedEval.suggestion.changed === false" class="tag builtin">无需优化</em>
                </div>
                <p class="rationale">{{ selectedEval.suggestion.rationale }}</p>
                <ul v-if="selectedEval.suggestion.gains && selectedEval.suggestion.gains.length" class="gains">
                  <li v-for="(g, i) in selectedEval.suggestion.gains" :key="i">
                    <strong>{{ dimLabel(g.dimension) }}</strong>：{{ g.note }}
                  </li>
                </ul>
                <label class="edit-label">优化后文本（可二次编辑后上线）</label>
                <textarea v-model="editingSuggestion" class="text-input" rows="8" :disabled="busy"></textarea>
              </div>

              <!-- A/B 行为对比 -->
              <div class="card">
                <div class="card-head"><h3>A/B 行为对比</h3></div>
                <p class="pse-hint">在真实问答链路上，用评测集用例对比「当前版本(A)」与「优化候选(B)」，pairwise 裁判断定胜率。</p>
                <div class="ab-controls">
                  <select v-model="abDatasetId" class="text-input ab-select" :disabled="busy">
                    <option value="">选择评测集…</option>
                    <option v-for="d in datasets" :key="d.id" :value="d.id">{{ d.name }}（{{ d.case_count }} 用例）</option>
                  </select>
                  <button class="btn btn-outline" :disabled="!abDatasetId || busy || startingAB" @click="runAB">
                    <span v-if="startingAB" class="spinner-xs"></span>
                    {{ startingAB ? '对比中…' : '运行 A/B' }}
                  </button>
                </div>
                <div v-if="selectedEval.ab && selectedEval.ab.skipped" class="ab-note">已跳过：{{ selectedEval.ab.reason }}</div>
                <div v-else-if="selectedEval.ab && selectedEval.ab.error" class="ab-note err">A/B 失败：{{ selectedEval.ab.error }}</div>
                <div v-else-if="selectedEval.ab && selectedEval.ab.cases" class="ab-result">
                  <div class="ab-stats">
                    <span class="ab-win">胜 {{ selectedEval.ab.wins }}</span>
                    <span class="ab-tie">平 {{ selectedEval.ab.ties }}</span>
                    <span class="ab-loss">负 {{ selectedEval.ab.losses }}</span>
                    <span class="ab-rate">胜率 {{ (selectedEval.ab.win_rate * 100).toFixed(1) }}%</span>
                  </div>
                  <p class="ab-reco" :class="{ good: selectedEval.ab.recommend_publish }">
                    {{ selectedEval.ab.recommend_publish ? '✓ 对比结果支持上线该优化' : '⚠ 对比结果不建议上线，请谨慎评估' }}
                  </p>
                </div>
              </div>

              <!-- 上线 -->
              <div class="card publish-card">
                <div class="card-head"><h3>上线</h3>
                  <em v-if="selectedEval.published" class="tag published">已上线</em>
                </div>
                <p class="pse-hint">
                  {{ selectedTarget.target_type === 'prompt'
                    ? '上线将发布为新的系统提示词版本，chat 主流程即刻采用（保留历史版本）。'
                    : '上线将更新该技能内容，并自动重跑安全预审；预审不通过则拒绝上线。' }}
                </p>
                <button class="btn btn-primary" :disabled="busy || publishing || !editingSuggestion.trim()" @click="publish">
                  <span v-if="publishing" class="spinner-xs"></span>
                  {{ publishing ? '上线中…' : '确认上线' }}
                </button>
              </div>
            </template>
          </template>

          <!-- 系统提示词版本历史 -->
          <div class="card" v-if="selectedTarget.target_type === 'prompt' && versions.length">
            <div class="card-head"><h3>系统提示词版本</h3></div>
            <ul class="version-list">
              <li v-for="v in versions" :key="v.id" :class="v.status">
                <em class="tag" :class="v.status">{{ v.status === 'published' ? '生效中' : (v.status === 'archived' ? '已归档' : '草稿') }}</em>
                <span class="v-name">{{ v.name || '（未命名）' }}</span>
                <span class="v-time">{{ fmtTime(v.updated_at) }}</span>
              </li>
            </ul>
          </div>
        </template>
      </main>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import {
  listPromptSkillTargets, getPromptVersions, startPromptSkillEval,
  listPromptSkillEvals, getPromptSkillEval, startPromptSkillAB, publishPromptSkill,
  listEvalDatasets,
} from '@/api'

const targets = ref([])
const selectedTarget = ref(null)
const loadingTargets = ref(false)
const loading = ref(false)

const datasets = ref([])
const versions = ref([])
const evals = ref([])
const selectedEval = ref(null)
const editingSuggestion = ref('')
const abDatasetId = ref('')

const startingEval = ref(false)
const startingAB = ref(false)
const publishing = ref(false)
const errorMsg = ref('')
const okMsg = ref('')
let pollTimer = null

const busy = computed(() => startingEval.value || startingAB.value || publishing.value)

onMounted(loadAll)
onBeforeUnmount(stopPoll)

function isSelected(t) {
  return selectedTarget.value
    && selectedTarget.value.target_type === t.target_type
    && selectedTarget.value.target_id === t.target_id
}

async function loadAll() {
  loading.value = true
  loadingTargets.value = true
  errorMsg.value = ''
  try {
    const [tRes, dRes] = await Promise.all([listPromptSkillTargets(), listEvalDatasets()])
    targets.value = tRes.data.targets || []
    datasets.value = dRes.data.datasets || []
    if (selectedTarget.value) {
      const match = targets.value.find((t) => isSelected(t))
      if (match) selectedTarget.value = match
    }
    await loadVersions()
  } catch (err) {
    errorMsg.value = err.response?.data?.detail || err.message || '加载失败'
  } finally {
    loading.value = false
    loadingTargets.value = false
  }
}

async function loadVersions() {
  try {
    const res = await getPromptVersions()
    versions.value = res.data.versions || []
  } catch {
    versions.value = []
  }
}

async function selectTarget(t) {
  selectedTarget.value = t
  selectedEval.value = null
  editingSuggestion.value = ''
  errorMsg.value = ''
  okMsg.value = ''
  stopPoll()
  await loadEvals()
  // 默认打开最近一次评测详情
  if (evals.value.length) await openEval(evals.value[0].id)
}

async function loadEvals() {
  if (!selectedTarget.value) return
  try {
    const res = await listPromptSkillEvals({
      target_type: selectedTarget.value.target_type,
      target_id: selectedTarget.value.target_id,
    })
    evals.value = res.data.evals || []
  } catch {
    evals.value = []
  }
}

async function startEval() {
  if (!selectedTarget.value) return
  startingEval.value = true
  errorMsg.value = ''
  okMsg.value = ''
  try {
    const res = await startPromptSkillEval(selectedTarget.value.target_type, selectedTarget.value.target_id)
    await loadEvals()
    await openEval(res.data.eval_id)
    startPoll()
  } catch (err) {
    errorMsg.value = err.response?.data?.detail || err.response?.data?.message || err.message || '发起评测失败'
  } finally {
    startingEval.value = false
  }
}

async function openEval(evalId) {
  try {
    const res = await getPromptSkillEval(evalId)
    selectedEval.value = res.data.eval
    editingSuggestion.value = (selectedEval.value.suggestion && selectedEval.value.suggestion.optimized) || ''
    if (selectedEval.value.status === 'running') startPoll()
  } catch (err) {
    errorMsg.value = err.response?.data?.detail || '加载评测详情失败'
  }
}

function startPoll() {
  stopPoll()
  pollTimer = setInterval(async () => {
    if (!selectedEval.value) return stopPoll()
    try {
      const res = await getPromptSkillEval(selectedEval.value.id)
      selectedEval.value = res.data.eval
      if (selectedEval.value.status !== 'running') {
        editingSuggestion.value = (selectedEval.value.suggestion && selectedEval.value.suggestion.optimized) || editingSuggestion.value
        stopPoll()
        await loadEvals()
        await loadVersions()
      }
    } catch { /* 轮询失败静默，下次重试 */ }
  }, 2500)
}

function stopPoll() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

async function runAB() {
  if (!selectedEval.value || !abDatasetId.value) return
  startingAB.value = true
  errorMsg.value = ''
  okMsg.value = ''
  try {
    await startPromptSkillAB(selectedEval.value.id, abDatasetId.value)
    startPoll()
  } catch (err) {
    errorMsg.value = err.response?.data?.detail || err.response?.data?.message || '发起 A/B 失败'
  } finally {
    startingAB.value = false
  }
}

async function publish() {
  if (!selectedEval.value) return
  publishing.value = true
  errorMsg.value = ''
  okMsg.value = ''
  try {
    await publishPromptSkill(selectedEval.value.id, editingSuggestion.value)
    okMsg.value = '已上线，优化版本即刻生效。'
    await openEval(selectedEval.value.id)
    await Promise.all([loadEvals(), loadVersions(), loadAll()])
  } catch (err) {
    const data = err.response?.data || {}
    errorMsg.value = data.error || data.detail || (data.review && data.review.reasons && data.review.reasons.join('；')) || '上线失败'
  } finally {
    publishing.value = false
  }
}

// ---- 展示辅助 ----
function statusText(s) {
  return { running: '执行中', finished: '已完成', failed: '失败' }[s] || s
}
function scoreTone(score) {
  if (score == null) return 'na'
  if (score >= 4) return 'good'
  if (score >= 3) return 'mid'
  return 'bad'
}
function dimWidth(score) {
  if (score == null) return '0%'
  return ((score / 5) * 100).toFixed(1) + '%'
}
function dimLabel(key) {
  const all = [...(selectedEval.value?.rubric?.dimensions || [])]
  const hit = all.find((d) => d.key === key)
  return hit ? hit.label : (key || '综合')
}
function fmtTime(iso) {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (x) => String(x).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}
</script>

<style scoped>
.pse { padding: 0; }
.pse-layout { display: grid; grid-template-columns: 260px 1fr; gap: 16px; align-items: start; }
@media (max-width: 900px) { .pse-layout { grid-template-columns: 1fr; } }

.panel-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.panel-title { font-size: 13px; font-weight: 700; color: var(--color-text); }
.pse-hint { margin: 4px 0 10px; font-size: 12px; line-height: 1.7; color: var(--color-text-secondary); }
.mini-loading { font-size: 12px; color: var(--color-text-secondary); padding: 8px 0; }
.placeholder { padding: 60px 0; text-align: center; color: var(--color-text-secondary); }

.pse-targets {
  background: #fff; border: 1px solid var(--color-border); border-radius: 12px; padding: 14px 16px;
}
.target-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.target-item {
  border: 1px solid var(--color-border); border-radius: 10px; padding: 8px 10px; cursor: pointer;
  transition: all .15s;
}
.target-item:hover { border-color: var(--color-primary); }
.target-item.active { background: #faf5ee; border-color: var(--color-primary); }
.target-main { display: flex; flex-direction: column; gap: 3px; }
.target-name { font-size: 13px; font-weight: 600; color: var(--color-text); }
.target-meta { font-size: 11px; color: var(--color-text-secondary); display: flex; align-items: center; gap: 6px; }

.tag { font-style: normal; font-size: 10px; padding: 1px 6px; border-radius: 6px; background: #f0ebe3; color: var(--color-text-secondary); }
.tag.prompt { background: #e8f0e8; color: #3a6b3a; }
.tag.skill { background: #eaeef5; color: #3a4f7a; }
.tag.builtin { background: #f3ecdf; color: #8a6b2f; }
.tag.published { background: #e3f0e6; color: #2e7d32; }
.tag.archived { background: #f0ebe3; }

.pse-work { display: flex; flex-direction: column; gap: 14px; }
.work-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.work-head h2 { margin: 0; font-size: 18px; color: var(--color-text); }
.work-sub { margin: 4px 0 0; font-size: 12px; color: var(--color-text-secondary); }

.card { background: #fff; border: 1px solid var(--color-border); border-radius: 12px; padding: 14px 16px; }
.card-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.card-head h3 { margin: 0; font-size: 14px; color: var(--color-text); }
.pse-collapse summary { cursor: pointer; font-size: 13px; font-weight: 600; color: var(--color-text); }

.text-box {
  margin: 10px 0 0; padding: 10px; background: #faf8f5; border: 1px solid var(--color-border);
  border-radius: 8px; font-size: 12px; line-height: 1.6; white-space: pre-wrap; word-break: break-word;
  max-height: 260px; overflow-y: auto;
}
.text-input {
  width: 100%; box-sizing: border-box; padding: 8px 10px; border: 1px solid var(--color-border);
  border-radius: 8px; font-size: 12px; line-height: 1.6; font-family: inherit; resize: vertical;
  background: #fff; color: var(--color-text);
}
.edit-label { display: block; font-size: 12px; color: var(--color-text-secondary); margin: 10px 0 6px; }

.inline-error { margin: 0; font-size: 12px; color: #c62828; }
.inline-ok { margin: 0; font-size: 12px; color: #2e7d32; }

.eval-history { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.eval-history li {
  display: flex; align-items: center; gap: 10px; padding: 6px 8px; border-radius: 8px;
  cursor: pointer; font-size: 12px; border: 1px solid transparent;
}
.eval-history li:hover { background: #faf8f5; }
.eval-history li.active { border-color: var(--color-primary); background: #faf5ee; }
.eh-score { font-weight: 700; min-width: 38px; }
.eh-status { color: var(--color-text-secondary); }
.eh-status.finished { color: #2e7d32; }
.eh-status.running { color: #b26a00; }
.eh-status.failed { color: #c62828; }
.eh-time { margin-left: auto; color: var(--color-text-secondary); }

.overall { font-size: 13px; font-weight: 700; }
.overall.good, .eh-score.good, .dim-score.good { color: #2e7d32; }
.overall.mid, .eh-score.mid, .dim-score.mid { color: #b26a00; }
.overall.bad, .eh-score.bad, .dim-score.bad { color: #c62828; }
.na { color: var(--color-text-secondary); }

.assert-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 8px; margin-bottom: 12px; }
.assert-item { display: flex; align-items: baseline; gap: 6px; font-size: 12px; padding: 6px 8px; border-radius: 8px; }
.assert-item.pass { background: #eef6ef; }
.assert-item.fail { background: #fbeeee; }
.assert-mark { font-weight: 700; }
.assert-item.pass .assert-mark { color: #2e7d32; }
.assert-item.fail .assert-mark { color: #c62828; }
.assert-label { font-weight: 600; color: var(--color-text); }
.assert-detail { color: var(--color-text-secondary); }

.dim-list { display: flex; flex-direction: column; gap: 8px; }
.dim-row { display: grid; grid-template-columns: 92px 1fr 34px 1fr; align-items: center; gap: 8px; font-size: 12px; }
.dim-label { color: var(--color-text); font-weight: 600; }
.dim-bar { height: 8px; background: #f0ebe3; border-radius: 4px; overflow: hidden; }
.dim-bar span { display: block; height: 100%; border-radius: 4px; }
.dim-bar span.good { background: #4caf50; }
.dim-bar span.mid { background: #e0a020; }
.dim-bar span.bad { background: #d9534f; }
.dim-bar span.na { background: #ccc; }
.dim-score { text-align: right; font-weight: 700; }
.dim-reason { color: var(--color-text-secondary); }

.rationale { margin: 0 0 8px; font-size: 12px; line-height: 1.7; color: var(--color-text); }
.gains { margin: 0 0 8px; padding-left: 18px; font-size: 12px; line-height: 1.8; color: var(--color-text-secondary); }

.ab-controls { display: flex; gap: 10px; align-items: center; margin-top: 6px; }
.ab-select { flex: 1; }
.ab-note { margin-top: 10px; font-size: 12px; color: var(--color-text-secondary); }
.ab-note.err { color: #c62828; }
.ab-result { margin-top: 10px; }
.ab-stats { display: flex; gap: 14px; font-size: 13px; font-weight: 600; }
.ab-win { color: #2e7d32; } .ab-tie { color: #b26a00; } .ab-loss { color: #c62828; }
.ab-rate { color: var(--color-text); }
.ab-reco { margin: 8px 0 0; font-size: 12px; color: #b26a00; }
.ab-reco.good { color: #2e7d32; }

.publish-card { border-color: var(--color-primary); }
.version-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.version-list li { display: flex; align-items: center; gap: 10px; font-size: 12px; padding: 5px 0; }
.v-name { color: var(--color-text); }
.v-time { margin-left: auto; color: var(--color-text-secondary); }

.running-card { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--color-text-secondary); }
.spinner-xs {
  display: inline-block; width: 12px; height: 12px; border: 2px solid var(--color-border);
  border-top-color: var(--color-primary); border-radius: 50%; animation: pse-spin .7s linear infinite;
}
@keyframes pse-spin { to { transform: rotate(360deg); } }

.icon-btn {
  background: none; border: none; cursor: pointer; color: var(--color-text-secondary);
  padding: 4px; border-radius: 6px; display: inline-flex;
}
.icon-btn:hover { background: #f0ebe3; color: var(--color-primary); }
.btn { cursor: pointer; border-radius: 8px; font-size: 13px; padding: 7px 14px; border: 1px solid transparent; }
.btn:disabled { opacity: .5; cursor: not-allowed; }
.btn-primary { background: var(--color-primary); color: #fff; }
.btn-outline { background: #fff; border-color: var(--color-border); color: var(--color-text); }
</style>
