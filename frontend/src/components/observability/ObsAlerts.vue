<template>
  <section class="panel alerts-panel" id="obs-alerts">
    <div class="panel-head">
      <span class="panel-title">
        实时告警分析（质量 / 性能越限 → 归因 + 原因分析 + 优化建议）
        <span v-if="unacked" class="alert-badge">{{ unacked }} 未确认</span>
      </span>
      <div class="alert-tools">
        <select v-model="statusFilter" class="filter-select" @change="load">
          <option value="">全部状态</option>
          <option value="open">待确认</option>
          <option value="acked">已确认</option>
          <option value="resolved">已恢复/处置</option>
        </select>
        <button class="btn btn-ghost sm" :disabled="scanning" @click="onScan">
          {{ scanning ? '扫描中…' : '立即扫描' }}
        </button>
        <button class="btn btn-ghost sm" :disabled="loading" @click="load">刷新</button>
      </div>
    </div>
    <p class="alert-sub">
      后台周期扫描错误率 / P95 延迟 / Token 超用 / 回归均分 / 点踩占比 / 在线评测均分；越限自动归因并给出优化建议，指标回落后自动恢复。
    </p>
    <p v-if="errorMsg" class="inline-error">{{ errorMsg }}</p>
    <div v-if="loading && !alerts.length" class="empty-hint">加载告警中…</div>
    <p v-else-if="!alerts.length" class="empty-hint">暂无告警事件 🎉（指标越限时将自动在此呈现归因与建议）</p>
    <div v-else class="alert-list">
      <article v-for="a in alerts" :key="a.id" class="alert-card" :class="[`sev-${a.severity}`, `st-${a.status}`]">
        <div class="alert-head" @click="toggle(a.id)">
          <span class="sev-tag" :class="`sev-${a.severity}`">{{ a.severity === 'critical' ? '严重' : '警告' }}</span>
          <span class="type-tag">{{ a.type === 'quality' ? '质量' : '性能' }}</span>
          <strong class="alert-metric">{{ metricLabel(a.metric) }}</strong>
          <span class="alert-value">
            当前 <b>{{ fmtValue(a) }}</b> / 阈值 {{ fmtThreshold(a) }}
          </span>
          <span class="status-tag" :class="`st-${a.status}`">{{ statusLabel(a.status) }}</span>
          <span class="alert-time">{{ formatTime(a.created_at) }}</span>
          <span class="caret">{{ expanded.has(a.id) ? '▾' : '▸' }}</span>
        </div>
        <div v-if="expanded.has(a.id)" class="alert-body">
          <!-- 原因分析 + 优化建议 -->
          <div class="analysis-box">
            <div class="analysis-head">
              <strong>原因分析与优化建议</strong>
              <span v-if="a.analysis?.degraded" class="degraded-tag">规则化降级</span>
              <button class="btn btn-ghost sm" :disabled="reanalyzingId === a.id" @click="onReanalyze(a)">
                {{ reanalyzingId === a.id ? '分析中…' : '重新分析' }}
              </button>
            </div>
            <p v-if="a.analysis?.cause" class="analysis-cause">{{ a.analysis.cause }}</p>
            <p v-else class="empty-hint sm">尚未生成分析，点「重新分析」由模型基于证据研判。</p>
            <ul v-if="a.analysis?.suggestions?.length" class="suggestion-list">
              <li v-for="(s, i) in a.analysis.suggestions" :key="i">{{ s }}</li>
            </ul>
            <p v-if="a.analyzed_at" class="analysis-time">分析时间 {{ formatTime(a.analyzed_at) }}</p>
          </div>
          <!-- 归因证据 -->
          <div class="evidence-box">
            <strong class="evidence-title">归因证据（真实运行数据）</strong>
            <pre class="evidence-json">{{ prettyEvidence(a.evidence) }}</pre>
          </div>
          <!-- 生命周期操作 -->
          <div class="alert-actions">
            <span v-if="a.resolved_by" class="resolved-meta">
              处置：{{ a.resolved_by === 'system' ? '指标回落自动恢复' : a.resolved_by }}
              <template v-if="a.resolved_at"> · {{ formatTime(a.resolved_at) }}</template>
            </span>
            <button v-if="a.status === 'open'" class="btn btn-ghost sm" @click="onAck(a)">确认</button>
            <button v-if="a.status !== 'resolved'" class="btn btn-primary sm" @click="onResolve(a)">处置完成</button>
          </div>
        </div>
      </article>
    </div>
  </section>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { getObsAlerts, ackObsAlert, resolveObsAlert, reanalyzeObsAlert, scanObsAlerts } from '@/api'

const METRIC_LABELS = {
  error_rate: '请求错误率',
  p95_latency: 'P95 延迟',
  token_overuse: '平均 Token/请求',
  eval_regression: '回归评测均分',
  dislike_rate: '点踩占比',
  online_quality: '在线评测均分',
}

const alerts = ref([])
const unacked = ref(0)
const loading = ref(false)
const scanning = ref(false)
const errorMsg = ref('')
const statusFilter = ref('')
const expanded = ref(new Set())
const reanalyzingId = ref('')

function metricLabel(m) { return METRIC_LABELS[m] || m }
function statusLabel(s) { return { open: '待确认', acked: '已确认', resolved: '已恢复' }[s] || s }
function fmtValue(a) {
  if (a.metric === 'p95_latency') return `${Math.round(a.value)}ms`
  if (['error_rate', 'dislike_rate'].includes(a.metric)) return `${(a.value * 100).toFixed(1)}%`
  return Number(a.value).toFixed(a.value < 10 ? 2 : 0)
}
function fmtThreshold(a) {
  if (a.metric === 'p95_latency') return `${Math.round(a.threshold)}ms`
  if (['error_rate', 'dislike_rate'].includes(a.metric)) return `${(a.threshold * 100).toFixed(0)}%`
  return a.threshold
}
function formatTime(iso) {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
function prettyEvidence(ev) {
  if (!ev || !Object.keys(ev).length) return '（无证据明细）'
  try { return JSON.stringify(ev, null, 2) } catch { return String(ev) }
}
function toggle(id) {
  const s = new Set(expanded.value)
  if (s.has(id)) s.delete(id); else s.add(id)
  expanded.value = s
}

async function load() {
  loading.value = true
  errorMsg.value = ''
  try {
    const res = await getObsAlerts({ status: statusFilter.value, limit: 50 })
    alerts.value = res.data.alerts || []
    unacked.value = res.data.unacked || 0
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '加载告警失败'
  } finally {
    loading.value = false
  }
}

async function onScan() {
  scanning.value = true
  try {
    await scanObsAlerts()
    await load()
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '扫描失败'
  } finally {
    scanning.value = false
  }
}

async function onAck(a) {
  try {
    await ackObsAlert(a.id)
    await load()
  } catch (e) { errorMsg.value = e.response?.data?.detail || '确认失败' }
}

async function onResolve(a) {
  try {
    await resolveObsAlert(a.id)
    await load()
  } catch (e) { errorMsg.value = e.response?.data?.detail || '处置失败' }
}

async function onReanalyze(a) {
  reanalyzingId.value = a.id
  try {
    const res = await reanalyzeObsAlert(a.id)
    if (res.data.alert) Object.assign(a, res.data.alert)
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '重新分析失败'
  } finally {
    reanalyzingId.value = ''
  }
}

onMounted(load)
defineExpose({ load })
</script>

<style scoped>
.panel { background: #fffdfb; border: 1px solid var(--color-border); border-radius: 12px; padding: 18px 20px; box-shadow: 0 1px 3px rgba(120,90,50,0.04); margin-bottom: 18px; }
.panel-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 8px; }
.panel-title { font-size: 14.5px; font-weight: 700; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.alert-badge { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 8px; background: #fbe4e3; color: #c0392b; }
.alert-tools { display: flex; gap: 8px; align-items: center; }
.alert-sub { font-size: 11.5px; color: var(--color-text-secondary); line-height: 1.6; margin: 0 0 12px; }
.filter-select { padding: 5px 9px; border: 1px solid var(--color-border); border-radius: 8px; font-size: 12px; background: #fff; color: var(--color-text); outline: none; }
.inline-error { color: #d9534f; font-size: 12.5px; margin: 6px 0; }
.empty-hint { color: var(--color-text-secondary); font-size: 12.5px; padding: 14px 0; text-align: center; }
.empty-hint.sm { padding: 4px 0; text-align: left; }

.alert-list { display: flex; flex-direction: column; gap: 8px; }
.alert-card { border: 1px solid var(--color-border); border-left: 3px solid #e0a92e; border-radius: 8px; background: #fffdfb; overflow: hidden; }
.alert-card.sev-critical { border-left-color: #d9534f; }
.alert-card.st-resolved { opacity: 0.72; }
.alert-head { display: flex; align-items: center; gap: 10px; padding: 10px 12px; cursor: pointer; flex-wrap: wrap; }
.alert-head:hover { background: #faf6f0; }
.sev-tag { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 8px; }
.sev-tag.sev-warning { background: #fdf0d9; color: #b8802a; }
.sev-tag.sev-critical { background: #fbe4e3; color: #c0392b; }
.type-tag { font-size: 10.5px; color: var(--color-text-secondary); background: #f4efe8; padding: 1px 8px; border-radius: 7px; }
.alert-metric { font-size: 13px; }
.alert-value { font-size: 12px; color: var(--color-text-secondary); }
.alert-value b { color: #c0392b; }
.status-tag { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 8px; }
.status-tag.st-open { background: #fbe4e3; color: #c0392b; }
.status-tag.st-acked { background: #fdf0d9; color: #b8802a; }
.status-tag.st-resolved { background: #e4f4e8; color: #3d8b52; }
.alert-time { font-size: 11px; color: var(--color-text-secondary); margin-left: auto; }
.caret { font-size: 11px; color: var(--color-text-secondary); }
.alert-body { padding: 4px 12px 14px; border-top: 1px solid #f4efe8; }

.analysis-box { background: #f7f2ea; border-radius: 8px; padding: 10px 12px; margin: 12px 0; }
.analysis-head { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
.analysis-head strong { font-size: 12.5px; }
.analysis-head .btn { margin-left: auto; }
.degraded-tag { font-size: 10px; color: #a99c8c; background: #eee9e2; padding: 1px 6px; border-radius: 6px; }
.analysis-cause { font-size: 12px; line-height: 1.7; margin: 0 0 6px; white-space: pre-wrap; }
.suggestion-list { margin: 0; padding-left: 18px; font-size: 12px; line-height: 1.8; }
.analysis-time { font-size: 10.5px; color: var(--color-text-secondary); margin: 6px 0 0; }

.evidence-box { margin-bottom: 12px; }
.evidence-title { font-size: 12px; display: block; margin-bottom: 6px; }
.evidence-json { background: #faf6f0; border: 1px solid #f4efe8; border-radius: 8px; padding: 10px 12px; font-size: 11px; line-height: 1.6; max-height: 260px; overflow: auto; margin: 0; font-family: 'SFMono-Regular', Consolas, monospace; white-space: pre-wrap; word-break: break-all; }

.alert-actions { display: flex; align-items: center; gap: 8px; justify-content: flex-end; flex-wrap: wrap; }
.resolved-meta { font-size: 11.5px; color: #3d8b52; margin-right: auto; }

.btn { padding: 8px 16px; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer; border: 1px solid transparent; transition: all 0.15s; white-space: nowrap; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn.sm { padding: 5px 12px; font-size: 12px; }
.btn-primary { background: var(--color-primary); color: #fff; }
.btn-primary:hover:not(:disabled) { background: #b87a3f; }
.btn-ghost { background: transparent; border-color: var(--color-border); color: var(--color-text); }
.btn-ghost:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
</style>