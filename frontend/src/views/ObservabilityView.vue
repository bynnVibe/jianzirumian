<template>
  <div class="obs-view">
    <!-- 顶部标题栏 -->
    <header class="obs-header">
      <div class="header-inner">
        <div class="header-title">
          <span class="header-eyebrow">Observability</span>
          <h1>Agent 可观测性</h1>
          <p class="header-sub">
            在线监控 Agent 行为：日志记录每一步、指标聚合 Token/延迟/成功率、追踪回放每次请求的完整 prompt 与 response
          </p>
        </div>
        <div class="header-actions">
          <div class="days-switch">
            <button
              v-for="d in [7, 30]"
              :key="d"
              class="day-btn"
              :class="{ active: days === d }"
              @click="changeDays(d)"
            >近{{ d }}天</button>
          </div>
          <button class="btn btn-ghost" :disabled="loading" @click="loadAll">
            {{ loading ? '加载中…' : '刷新' }}
          </button>
        </div>
      </div>
    </header>

    <div class="obs-body">
      <p v-if="errorMsg" class="inline-error">{{ errorMsg }}</p>

      <!-- ============ 指标总览 ============ -->
      <section class="cards" v-if="metrics">
        <div class="metric-card">
          <span class="metric-num">{{ metrics.overview.total }}</span>
          <span class="metric-label">请求总数</span>
        </div>
        <div class="metric-card">
          <span class="metric-num" :class="{ warn: metrics.overview.success_rate < 0.95 && metrics.overview.total }">
            {{ (metrics.overview.success_rate * 100).toFixed(1) }}%
          </span>
          <span class="metric-label">成功率</span>
        </div>
        <div class="metric-card">
          <span class="metric-num">{{ metrics.overview.errors }}</span>
          <span class="metric-label">失败请求</span>
        </div>
        <div class="metric-card">
          <span class="metric-num">{{ formatMs(metrics.overview.avg_latency_ms) }}</span>
          <span class="metric-label">平均延迟</span>
        </div>
        <div class="metric-card">
          <span class="metric-num">{{ formatMs(metrics.overview.p95_latency_ms) }}</span>
          <span class="metric-label">P95 延迟</span>
        </div>
        <div class="metric-card">
          <span class="metric-num">{{ formatNum(metrics.overview.prompt_tokens + metrics.overview.completion_tokens) }}</span>
          <span class="metric-label">Token 消耗</span>
        </div>
        <div class="metric-card">
          <span class="metric-num">{{ metrics.overview.llm_calls }}</span>
          <span class="metric-label">LLM 调用</span>
        </div>
        <div class="metric-card">
          <span class="metric-num">{{ metrics.overview.tool_calls }}</span>
          <span class="metric-label">工具调用</span>
        </div>
      </section>

      <!-- ============ 每日趋势 ============ -->
      <section class="panel" v-if="metrics">
        <div class="panel-head"><span class="panel-title">每日请求趋势（近{{ days }}天）</span></div>
        <div class="trend-chart" v-if="metrics.daily.length">
          <div
            v-for="row in metrics.daily"
            :key="row.day"
            class="trend-col"
            :title="`${row.day}：请求 ${row.total}、失败 ${row.errors}、Token ${row.tokens}、平均 ${formatMs(row.avg_latency_ms)}`"
          >
            <div class="trend-bar-wrap">
              <div class="trend-bar" :style="{ height: barHeight(row.total) + '%' }">
                <div
                  v-if="row.errors"
                  class="trend-bar-err"
                  :style="{ height: Math.min(100, (row.errors / Math.max(1, row.total)) * 100) + '%' }"
                ></div>
              </div>
            </div>
            <span class="trend-label">{{ row.day.slice(5) }}</span>
          </div>
        </div>
        <p v-else class="empty-hint">暂无数据</p>
      </section>

      <!-- ============ 按 Agent / 按工具 ============ -->
      <section class="two-col" v-if="metrics">
        <div class="panel">
          <div class="panel-head"><span class="panel-title">按 Agent 统计</span></div>
          <table class="obs-table" v-if="metrics.by_agent.length">
            <thead>
              <tr><th>Agent</th><th>请求</th><th>失败</th><th>平均延迟</th><th>LLM</th><th>工具</th><th>Token</th></tr>
            </thead>
            <tbody>
              <tr v-for="row in metrics.by_agent" :key="row.agent">
                <td>{{ row.agent }}</td>
                <td>{{ row.total }}</td>
                <td :class="{ 'cell-err': row.errors }">{{ row.errors }}</td>
                <td>{{ formatMs(row.avg_latency_ms) }}</td>
                <td>{{ row.llm_calls }}</td>
                <td>{{ row.tool_calls }}</td>
                <td>{{ formatNum(row.tokens) }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="empty-hint">暂无数据</p>
        </div>
        <div class="panel">
          <div class="panel-head"><span class="panel-title">工具调用次数</span></div>
          <table class="obs-table" v-if="metrics.by_tool.length">
            <thead>
              <tr><th>工具</th><th>调用</th><th>失败</th><th>平均延迟</th></tr>
            </thead>
            <tbody>
              <tr v-for="row in metrics.by_tool" :key="row.tool">
                <td>{{ row.tool }}</td>
                <td>{{ row.total }}</td>
                <td :class="{ 'cell-err': row.errors }">{{ row.errors }}</td>
                <td>{{ formatMs(row.avg_latency_ms) }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="empty-hint">暂无工具调用</p>
        </div>
      </section>

      <!-- ============ Trace 列表（全链路追踪） ============ -->
      <section class="panel">
        <div class="panel-head">
          <span class="panel-title">请求追踪（Traces）</span>
          <div class="trace-filters">
            <select v-model="filterAgent" class="filter-select" @change="loadTraces">
              <option value="">全部 Agent</option>
              <option value="chat">chat 对话</option>
              <option value="wiki_assistant">知识助手</option>
              <option value="wiki_compile">知识编译</option>
              <option value="wiki_synthesize">综合报告</option>
            </select>
            <select v-model="filterStatus" class="filter-select" @change="loadTraces">
              <option value="">全部状态</option>
              <option value="ok">成功</option>
              <option value="error">失败</option>
              <option value="running">进行中</option>
            </select>
          </div>
        </div>
        <div v-if="loadingTraces" class="mini-loading">加载中…</div>
        <table class="obs-table trace-table" v-else-if="traces.length">
          <thead>
            <tr><th>时间</th><th>Agent</th><th>用户</th><th>输入</th><th>状态</th><th>延迟</th><th>LLM/工具</th><th>Token</th></tr>
          </thead>
          <tbody>
            <tr v-for="t in traces" :key="t.id" class="trace-row" @click="openTrace(t.id)">
              <td class="cell-time">{{ formatTime(t.started_at) }}</td>
              <td><span class="agent-badge" :class="'agent-' + t.agent">{{ t.agent }}</span></td>
              <td>{{ t.user_id || '-' }}</td>
              <td class="cell-input" :title="t.input">{{ t.input || '-' }}</td>
              <td><span class="status-dot" :class="t.status"></span>{{ statusText(t.status) }}</td>
              <td>{{ formatMs(t.latency_ms) }}</td>
              <td>{{ t.llm_calls }} / {{ t.tool_calls }}</td>
              <td>{{ formatNum(t.prompt_tokens + t.completion_tokens) }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="empty-hint">暂无 trace 记录（发起一次对话或知识助手提问后即可看到）</p>
        <div class="pager" v-if="total > traces.length || offset > 0">
          <button class="btn btn-ghost sm" :disabled="offset === 0" @click="page(-1)">上一页</button>
          <span class="pager-info">{{ offset + 1 }}-{{ offset + traces.length }} / {{ total }}</span>
          <button class="btn btn-ghost sm" :disabled="offset + traces.length >= total" @click="page(1)">下一页</button>
        </div>
      </section>
    </div>

    <!-- ============ Trace 详情弹窗（span 时间线） ============ -->
    <div v-if="traceDetail" class="modal-overlay" @click.self="traceDetail = null">
      <div class="modal-box trace-box">
        <div class="trace-head">
          <h3 class="modal-title">
            全链路追踪 · <span class="agent-badge" :class="'agent-' + traceDetail.agent">{{ traceDetail.agent }}</span>
          </h3>
          <button class="icon-btn" title="关闭" @click="traceDetail = null">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>
        <div class="trace-meta">
          <span>状态：<b :class="{ 'cell-err': traceDetail.status === 'error' }">{{ statusText(traceDetail.status) }}</b></span>
          <span>延迟：{{ formatMs(traceDetail.latency_ms) }}</span>
          <span>Token：{{ traceDetail.prompt_tokens }} + {{ traceDetail.completion_tokens }}</span>
          <span>用户：{{ traceDetail.user_id || '-' }}</span>
          <span>开始：{{ formatTime(traceDetail.started_at) }}</span>
        </div>
        <div class="trace-input">
          <span class="span-kind-badge step">输入</span>
          <pre class="span-text">{{ traceDetail.input || '-' }}</pre>
        </div>
        <p v-if="traceDetail.error" class="inline-error">错误：{{ traceDetail.error }}</p>

        <div class="span-list">
          <div v-for="span in traceDetail.spans" :key="span.id" class="span-item" :class="'kind-' + span.kind">
            <button class="span-head" @click="toggleSpan(span.id)">
              <span class="span-seq">#{{ span.seq }}</span>
              <span class="span-kind-badge" :class="span.kind">{{ span.kind }}</span>
              <span class="span-name">{{ span.name }}</span>
              <span class="status-dot" :class="span.status"></span>
              <span class="span-meta">{{ formatMs(span.latency_ms) }}<template v-if="span.prompt_tokens || span.completion_tokens"> · {{ span.prompt_tokens }}+{{ span.completion_tokens }} tok</template></span>
              <span class="span-arrow" :class="{ open: expandedSpans.has(span.id) }">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                  <polyline points="6 9 12 15 18 9" />
                </svg>
              </span>
            </button>
            <div v-if="expandedSpans.has(span.id)" class="span-body">
              <p v-if="span.error" class="inline-error">{{ span.error }}</p>
              <template v-if="span.input">
                <span class="span-io-label">PROMPT / 入参</span>
                <pre class="span-text">{{ span.input }}</pre>
              </template>
              <template v-if="span.output">
                <span class="span-io-label">RESPONSE / 结果</span>
                <pre class="span-text">{{ span.output }}</pre>
              </template>
              <p v-if="!span.input && !span.output && !span.error" class="empty-hint">（无附加内容）</p>
            </div>
          </div>
          <p v-if="!traceDetail.spans.length" class="empty-hint">该 trace 暂无 span</p>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { getObservabilityMetrics, getObservabilityTraces, getObservabilityTrace } from '@/api'

const days = ref(7)
const loading = ref(false)
const loadingTraces = ref(false)
const errorMsg = ref('')
const metrics = ref(null)
const traces = ref([])
const total = ref(0)
const offset = ref(0)
const filterAgent = ref('')
const filterStatus = ref('')
const traceDetail = ref(null)
const expandedSpans = ref(new Set())

onMounted(loadAll)

async function loadAll() {
  loading.value = true
  errorMsg.value = ''
  try {
    await Promise.all([loadMetrics(), loadTraces()])
  } catch (err) {
    errorMsg.value = err.response?.data?.detail || err.message || '加载失败'
  } finally {
    loading.value = false
  }
}

async function loadMetrics() {
  const res = await getObservabilityMetrics(days.value)
  metrics.value = res.data
}

async function loadTraces() {
  loadingTraces.value = true
  try {
    const res = await getObservabilityTraces({
      agent: filterAgent.value,
      status: filterStatus.value,
      limit: 30,
      offset: offset.value,
    })
    traces.value = res.data.items || []
    total.value = res.data.total || 0
  } finally {
    loadingTraces.value = false
  }
}

function changeDays(d) {
  days.value = d
  loadMetrics().catch(() => {})
}

function page(dir) {
  offset.value = Math.max(0, offset.value + dir * 30)
  loadTraces()
}

async function openTrace(id) {
  try {
    const res = await getObservabilityTrace(id)
    if (res.data.success) {
      traceDetail.value = res.data.trace
      expandedSpans.value = new Set()
    }
  } catch (err) {
    errorMsg.value = err.response?.data?.detail || '加载 trace 详情失败'
  }
}

function toggleSpan(id) {
  const next = new Set(expandedSpans.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expandedSpans.value = next
}

// ---- 展示辅助 ----
function formatMs(ms) {
  const n = Number(ms || 0)
  if (n < 1000) return `${n}ms`
  return `${(n / 1000).toFixed(1)}s`
}

function formatNum(n) {
  const v = Number(n || 0)
  if (v >= 10000) return `${(v / 1000).toFixed(1)}k`
  return String(v)
}

function formatTime(iso) {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (x) => String(x).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

function statusText(status) {
  return { ok: '成功', error: '失败', running: '进行中' }[status] || status
}

function barHeight(count) {
  const max = Math.max(1, ...(metrics.value?.daily || []).map((r) => r.total))
  if (!count) return 3
  return Math.max(6, Math.round((count / max) * 100))
}
</script>

<style scoped>
.obs-view {
  /* 父容器 .main-content 为 100vh + overflow:hidden，页面需自身承担滚动 */
  height: 100%;
  overflow-y: auto;
  background: var(--color-bg);
}

.obs-header {
  background: linear-gradient(135deg, #faf8f5, #f3efe9);
  border-bottom: 1px solid var(--color-border);
  padding: 28px 32px 22px;
}

.header-inner {
  max-width: 1200px;
  margin: 0 auto;
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.header-eyebrow {
  font-size: 11px;
  letter-spacing: 1.5px;
  text-transform: uppercase;
  color: var(--color-primary);
  font-weight: 700;
}

.header-title h1 {
  margin: 2px 0 6px;
  font-size: 24px;
  color: var(--color-text);
}

.header-sub {
  margin: 0;
  font-size: 13px;
  color: var(--color-text-secondary);
  max-width: 640px;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.days-switch {
  display: flex;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  overflow: hidden;
}

.day-btn {
  padding: 6px 12px;
  font-size: 12px;
  border: none;
  background: #fff;
  color: var(--color-text-secondary);
  cursor: pointer;
}

.day-btn.active {
  background: var(--color-primary-light);
  color: var(--color-primary);
  font-weight: 600;
}

.obs-body {
  max-width: 1200px;
  margin: 0 auto;
  padding: 20px 32px 48px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* ---- 指标卡片 ---- */
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(128px, 1fr));
  gap: 10px;
}

.metric-card {
  background: #fff;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.metric-num {
  font-size: 20px;
  font-weight: 700;
  color: var(--color-primary);
}

.metric-num.warn {
  color: #c62828;
}

.metric-label {
  font-size: 11px;
  color: var(--color-text-secondary);
}

/* ---- 面板 ---- */
.panel {
  background: #fff;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 14px 16px 16px;
}

.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}

.panel-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--color-text);
}

.two-col {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

@media (max-width: 900px) {
  .two-col {
    grid-template-columns: 1fr;
  }
}

/* ---- 趋势图 ---- */
.trend-chart {
  display: flex;
  align-items: flex-end;
  gap: 6px;
  height: 150px;
  padding: 8px 4px 0;
}

.trend-col {
  flex: 1;
  min-width: 14px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  height: 100%;
}

.trend-bar-wrap {
  flex: 1;
  width: 100%;
  display: flex;
  align-items: flex-end;
  justify-content: center;
}

.trend-bar {
  width: 60%;
  max-width: 26px;
  background: linear-gradient(180deg, #d9a76a, #c98a4b);
  border-radius: 4px 4px 0 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  transition: height 0.3s;
}

.trend-bar-err {
  width: 100%;
  background: #c62828;
}

.trend-label {
  font-size: 10px;
  color: var(--color-text-secondary);
}

/* ---- 表格 ---- */
.obs-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.obs-table th,
.obs-table td {
  padding: 7px 8px;
  text-align: left;
  border-bottom: 1px solid var(--color-border);
  color: var(--color-text);
  white-space: nowrap;
}

.obs-table th {
  color: var(--color-text-secondary);
  font-weight: 600;
}

.trace-table .trace-row {
  cursor: pointer;
  transition: background 0.15s;
}

.trace-table .trace-row:hover {
  background: var(--color-primary-light);
}

.cell-input {
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.cell-time {
  color: var(--color-text-secondary);
}

.cell-err {
  color: #c62828;
  font-weight: 600;
}

.agent-badge {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 11px;
  background: #ede6dc;
  color: #6b5b45;
}

.agent-badge.agent-chat {
  background: #e3f0e6;
  color: #2e6b3e;
}

.agent-badge.agent-wiki_assistant {
  background: #e8ecf7;
  color: #3b4f8f;
}

.agent-badge.agent-wiki_compile,
.agent-badge.agent-wiki_synthesize {
  background: #f7ece0;
  color: #96602a;
}

.status-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 6px;
  background: #9e9e9e;
}

.status-dot.ok {
  background: #43a047;
}

.status-dot.error {
  background: #c62828;
}

.status-dot.running {
  background: #f9a825;
}

.filter-select {
  padding: 5px 8px;
  font-size: 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: #fff;
  color: var(--color-text);
  outline: none;
}

.trace-filters {
  display: flex;
  gap: 8px;
}

.pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  margin-top: 12px;
}

.pager-info {
  font-size: 12px;
  color: var(--color-text-secondary);
}

/* ---- 通用小组件 ---- */
.btn {
  padding: 7px 14px;
  font-size: 13px;
  border-radius: 8px;
  cursor: pointer;
  border: 1px solid transparent;
}

.btn.sm {
  padding: 4px 10px;
  font-size: 12px;
}

.btn-ghost {
  background: #fff;
  border-color: var(--color-border);
  color: var(--color-text);
}

.btn-ghost:hover:not(:disabled) {
  background: var(--color-primary-light);
}

.btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.icon-btn {
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  border-radius: 8px;
  cursor: pointer;
  color: var(--color-text-secondary);
}

.icon-btn:hover {
  background: #ede6dc;
}

.mini-loading,
.empty-hint {
  padding: 18px 0;
  text-align: center;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.inline-error {
  color: #c62828;
  font-size: 12px;
  margin: 0 0 8px;
}

/* ---- Trace 详情弹窗 ---- */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}

.modal-box {
  background: var(--color-bg, #fff);
  border-radius: 14px;
  padding: 20px 24px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.18);
}

.trace-box {
  width: 860px;
  max-width: 94vw;
  max-height: 88vh;
  overflow-y: auto;
}

.trace-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.trace-head .modal-title {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 8px;
}

.trace-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  margin: 10px 0 12px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.trace-meta b {
  color: var(--color-text);
}

.trace-input {
  background: #faf8f5;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  padding: 8px 10px;
  margin-bottom: 12px;
}

.span-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.span-item {
  border: 1px solid var(--color-border);
  border-radius: 10px;
  overflow: hidden;
}

.span-head {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border: none;
  background: #faf8f5;
  cursor: pointer;
  font-size: 12px;
  color: var(--color-text);
}

.span-head:hover {
  background: var(--color-primary-light);
}

.span-seq {
  color: var(--color-text-secondary);
  font-weight: 600;
}

.span-kind-badge {
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
}

.span-kind-badge.llm {
  background: #e8ecf7;
  color: #3b4f8f;
}

.span-kind-badge.tool {
  background: #e3f0e6;
  color: #2e6b3e;
}

.span-kind-badge.step {
  background: #ede6dc;
  color: #6b5b45;
}

.span-name {
  font-weight: 600;
  flex: 1;
  text-align: left;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.span-meta {
  color: var(--color-text-secondary);
  white-space: nowrap;
}

.span-arrow {
  display: flex;
  color: var(--color-text-secondary);
  transition: transform 0.2s;
}

.span-arrow.open {
  transform: rotate(180deg);
}

.span-body {
  padding: 10px 12px;
  border-top: 1px solid var(--color-border);
  background: #fff;
}

.span-io-label {
  display: block;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 1px;
  color: var(--color-text-secondary);
  margin: 6px 0 4px;
}

.span-text {
  margin: 0;
  padding: 8px 10px;
  background: #faf8f5;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  font-size: 11px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 280px;
  overflow-y: auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
</style>
