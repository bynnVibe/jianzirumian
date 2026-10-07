<template>
  <div class="panel">
    <div class="panel-head">
      <div class="panel-intro">
        <h2>请求日志与审计</h2>
        <p>每次调用的状态码、Token 消耗、上游路由、工具调用、请求参数、风险明细与<strong>可视化调用链路</strong>全部入库，可搜索、筛选、展开查看。</p>
      </div>
      <div class="head-actions">
        <button class="btn btn-ghost sm" @click="load">刷新</button>
        <button class="btn btn-danger sm" @click="onPurge">清理过期日志</button>
      </div>
    </div>

    <!-- 搜索筛选栏 -->
    <div class="filter-bar">
      <input v-model.trim="filters.keyword" class="field-input" placeholder="关键词（请求体/响应/密钥名）" @keyup.enter="reload" />
      <select v-model="filters.status" class="field-input">
        <option value="">全部状态</option>
        <option value="ok">成功</option>
        <option value="error">失败</option>
        <option value="blocked">阻断</option>
      </select>
      <select v-model="filters.risk_level" class="field-input">
        <option value="">全部风险</option>
        <option value="none">无风险</option>
        <option value="low">低</option>
        <option value="medium">中</option>
        <option value="high">高</option>
        <option value="critical">严重</option>
      </select>
      <input v-model.trim="filters.model" class="field-input" placeholder="模型名" @keyup.enter="reload" />
      <select v-model="filters.key_id" class="field-input">
        <option value="">全部密钥</option>
        <option v-for="k in keyOptions" :key="k.id" :value="k.id">{{ k.name }}</option>
      </select>
      <select v-model="filters.channel_id" class="field-input">
        <option value="">全部渠道</option>
        <option v-for="c in channelOptions" :key="c.id" :value="c.id">{{ c.name }}</option>
      </select>
      <button class="btn btn-primary sm" @click="reload">搜索</button>
      <button class="btn btn-ghost sm" @click="resetFilters">重置</button>
    </div>

    <p v-if="errorMsg" class="err-tip">{{ errorMsg }}</p>
    <div v-if="loading" class="mini-loading">加载日志中…</div>
    <div v-else-if="!logs.length" class="mini-empty">没有符合条件的日志记录。</div>

    <div v-else class="log-list">
      <div v-for="row in logs" :key="row.id" class="log-item">
        <div class="log-row" @click="toggleExpand(row)">
          <span class="risk-tag" :class="`risk-${row.risk_level}`">{{ riskLabel(row.risk_level) }}</span>
          <span class="status-tag" :class="`st-${row.status}`">{{ statusLabel(row.status) }}</span>
          <span class="log-code mono">{{ row.status_code || '-' }}</span>
          <span class="log-key">{{ row.key_name || '匿名' }}</span>
          <span class="log-model mono">{{ row.model_requested }}<template v-if="row.model_mapped && row.model_mapped !== row.model_requested"> → {{ row.model_mapped }}</template></span>
          <span class="log-channel">{{ row.channel_name || '-' }}</span>
          <span class="log-tokens">{{ formatNum(row.total_tokens) }} tok</span>
          <span class="log-latency">{{ row.latency_ms }}ms</span>
          <span v-if="row.retries" class="retry-pill">重试 {{ row.retries }}</span>
          <span v-if="row.audit_action" class="audit-pill">{{ auditLabel(row.audit_action) }}</span>
          <span class="log-time">{{ formatTime(row.created_at) }}</span>
          <span class="expand-caret">{{ expanded === row.id ? '▾' : '▸' }}</span>
        </div>

        <!-- 展开明细 -->
        <div v-if="expanded === row.id" class="log-detail">
          <div v-if="detailLoading" class="mini-loading">加载明细中…</div>
          <template v-else-if="detail">
            <!-- 可视化调用链路 -->
            <div class="detail-block">
              <h4 class="detail-title">🔗 可视化调用链路</h4>
              <TraceFlow :steps="detail.trace_steps || []" />
            </div>

            <!-- 风险详情面板 -->
            <div v-if="detail.risks?.length" class="detail-block risk-block">
              <h4 class="detail-title">
                ⚠️ 安全审计命中
                <span class="risk-tag" :class="`risk-${detail.risk_level}`">{{ riskLabel(detail.risk_level) }} · {{ detail.risk_score }} 分</span>
                <span v-if="detail.audit_action" class="audit-pill">{{ auditLabel(detail.audit_action) }}</span>
              </h4>
              <div class="risk-findings">
                <div v-for="(f, i) in detail.risks" :key="i" class="finding" :class="`sev-${f.severity}`">
                  <div class="finding-head">
                    <span class="sev-dot"></span>
                    <strong>{{ f.rule_name }}</strong>
                    <span class="finding-cat">{{ f.category_label }}</span>
                    <span class="finding-sev">{{ sevLabel(f.severity) }}</span>
                  </div>
                  <div class="finding-match mono">{{ f.matched }}</div>
                </div>
              </div>
            </div>

            <!-- 上游路由 + 工具调用 -->
            <div class="detail-grid">
              <div class="detail-block">
                <h4 class="detail-title">上游路由</h4>
                <div class="kv"><span>渠道</span><strong>{{ detail.channel_name || '-' }}</strong></div>
                <div class="kv"><span>URL</span><code class="mono">{{ detail.upstream_url || '-' }}</code></div>
                <div class="kv"><span>流式</span><strong>{{ detail.stream ? '是' : '否' }}</strong></div>
                <div class="kv"><span>客户端 IP</span><strong>{{ detail.client_ip || '-' }}</strong></div>
                <div class="kv"><span>Token</span><strong>{{ detail.prompt_tokens }} + {{ detail.completion_tokens }} = {{ detail.total_tokens }}</strong></div>
                <div v-if="detail.error" class="kv err"><span>错误</span><strong>{{ detail.error }}</strong></div>
              </div>
              <div class="detail-block">
                <h4 class="detail-title">工具调用</h4>
                <div v-if="detail.tool_calls?.length" class="tool-list">
                  <span v-for="(t, i) in detail.tool_calls" :key="i" class="tool-chip mono">{{ typeof t === 'string' ? t : (t.name || JSON.stringify(t)) }}</span>
                </div>
                <div v-else class="detail-none">本次调用未触发工具</div>
              </div>
            </div>

            <!-- 请求参数 -->
            <div class="detail-block">
              <h4 class="detail-title">请求参数</h4>
              <pre class="code-block">{{ prettyBody(detail.request_body) }}</pre>
            </div>
            <!-- 响应预览 -->
            <div class="detail-block">
              <h4 class="detail-title">响应预览</h4>
              <pre class="code-block">{{ prettyBody(detail.response_preview) }}</pre>
            </div>
          </template>
        </div>
      </div>

      <!-- 分页 -->
      <div class="pager">
        <button class="btn btn-ghost sm" :disabled="page <= 1" @click="goPage(page - 1)">上一页</button>
        <span class="pager-info">第 {{ page }} / {{ totalPages }} 页 · 共 {{ total }} 条</span>
        <button class="btn btn-ghost sm" :disabled="page >= totalPages" @click="goPage(page + 1)">下一页</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import {
  queryGatewayLogs, getGatewayLog, purgeGatewayLogs,
  listGatewayKeys, listGatewayChannels,
} from '@/api'
import TraceFlow from './TraceFlow.vue'

const PAGE_SIZE = 30
const logs = ref([])
const total = ref(0)
const page = ref(1)
const loading = ref(true)
const errorMsg = ref('')
const expanded = ref(null)
const detail = ref(null)
const detailLoading = ref(false)
const keyOptions = ref([])
const channelOptions = ref([])

const filters = reactive({ keyword: '', status: '', risk_level: '', model: '', key_id: '', channel_id: '' })
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))

const RISK_LABELS = { none: '无风险', low: '低', medium: '中', high: '高', critical: '严重' }
const STATUS_LABELS = { ok: '成功', error: '失败', blocked: '阻断' }
const AUDIT_LABELS = { warn: '警告', mask: '已脱敏', block: '已阻断', '': '审计' }
const SEV_LABELS = { low: '低危', medium: '中危', high: '高危', critical: '严重' }
function riskLabel(l) { return RISK_LABELS[l] || l }
function statusLabel(s) { return STATUS_LABELS[s] || s }
function auditLabel(a) { return AUDIT_LABELS[a] || a }
function sevLabel(s) { return SEV_LABELS[s] || s }
function formatNum(n) { return (n || 0).toLocaleString('en-US') }
function formatTime(iso) {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`
}
function prettyBody(raw) {
  if (!raw) return '（空）'
  try { return JSON.stringify(JSON.parse(raw), null, 2) } catch { return raw }
}

async function load() {
  loading.value = true
  errorMsg.value = ''
  try {
    const res = await queryGatewayLogs({
      ...filters, limit: PAGE_SIZE, offset: (page.value - 1) * PAGE_SIZE,
    })
    logs.value = res.data.items || []
    total.value = res.data.total || 0
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '加载日志失败'
  } finally {
    loading.value = false
  }
}
function reload() { page.value = 1; expanded.value = null; detail.value = null; load() }
function resetFilters() {
  Object.assign(filters, { keyword: '', status: '', risk_level: '', model: '', key_id: '', channel_id: '' })
  reload()
}
function goPage(p) { page.value = p; expanded.value = null; detail.value = null; load() }

async function toggleExpand(row) {
  if (expanded.value === row.id) { expanded.value = null; detail.value = null; return }
  expanded.value = row.id
  detail.value = null
  detailLoading.value = true
  try {
    const res = await getGatewayLog(row.id)
    detail.value = res.data.log
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '加载明细失败'
  } finally {
    detailLoading.value = false
  }
}

async function onPurge() {
  if (!confirm('确认按保留天数清理过期日志？此操作不可撤销。')) return
  try {
    const res = await purgeGatewayLogs()
    alert(`已清理 ${res.data.deleted || 0} 条过期日志`)
    await load()
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '清理失败'
  }
}

async function loadOptions() {
  try {
    const [kRes, cRes] = await Promise.all([listGatewayKeys(), listGatewayChannels()])
    keyOptions.value = kRes.data.keys || []
    channelOptions.value = cRes.data.channels || []
  } catch (e) { /* 选项加载失败不阻断日志展示 */ }
}

onMounted(async () => { await Promise.all([loadOptions(), load()]) })
</script>

<style scoped>
.panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px; flex-wrap: wrap; }
.panel-intro h2 { font-size: 16px; font-weight: 700; margin-bottom: 4px; }
.panel-intro p { font-size: 12.5px; color: var(--color-text-secondary); line-height: 1.6; max-width: 760px; }
.head-actions { display: flex; gap: 8px; }
.mono { font-family: 'SFMono-Regular', Consolas, monospace; }

.filter-bar { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; align-items: center; }
.filter-bar .field-input { padding: 7px 10px; font-size: 12.5px; }
.filter-bar input.field-input { min-width: 180px; flex: 1; }
.field-input { border: 1px solid var(--color-border); border-radius: 8px; background: #fff; color: var(--color-text); outline: none; }
.field-input:focus { border-color: var(--color-primary); }

.log-list { display: flex; flex-direction: column; gap: 8px; }
.log-item { background: #fffdfb; border: 1px solid var(--color-border); border-radius: 10px; overflow: hidden; }
.log-row { display: flex; align-items: center; gap: 10px; padding: 11px 14px; cursor: pointer; font-size: 12.5px; flex-wrap: wrap; }
.log-row:hover { background: #faf6f0; }
.risk-tag { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 8px; white-space: nowrap; }
.risk-none { background: #eee9e2; color: #8a7e72; }
.risk-low { background: #e8f0fb; color: #3a6ea5; }
.risk-medium { background: #fdf0d9; color: #b8802a; }
.risk-high { background: #fbe7d9; color: #c1622a; }
.risk-critical { background: #fbe4e3; color: #c0392b; }
.status-tag { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 8px; }
.st-ok { background: #e4f4e8; color: #3d8b52; }
.st-error { background: #fbe4e3; color: #c0392b; }
.st-blocked { background: #f3e6f7; color: #8e44ad; }
.log-code { color: var(--color-text-secondary); font-size: 11.5px; min-width: 26px; }
.log-key { font-weight: 600; }
.log-model { color: var(--color-text-secondary); font-size: 11.5px; }
.log-channel { color: var(--color-text-secondary); font-size: 11.5px; }
.log-tokens, .log-latency { color: var(--color-text-secondary); font-size: 11.5px; }
.retry-pill { font-size: 10.5px; font-weight: 700; padding: 1px 7px; border-radius: 8px; background: #fdf0d9; color: #b8802a; }
.audit-pill { font-size: 10.5px; font-weight: 700; padding: 1px 7px; border-radius: 8px; background: #f4ece0; color: #b8802a; }
.log-time { margin-left: auto; color: var(--color-text-secondary); font-size: 11px; white-space: nowrap; }
.expand-caret { color: var(--color-text-secondary); font-size: 12px; }

.log-detail { border-top: 1px solid var(--color-border); padding: 16px 18px; background: #fdfbf7; display: flex; flex-direction: column; gap: 16px; }
.detail-block { }
.detail-title { font-size: 12.5px; font-weight: 700; margin-bottom: 8px; display: flex; align-items: center; gap: 8px; }
.detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 760px) { .detail-grid { grid-template-columns: 1fr; } }
.kv { display: flex; gap: 8px; font-size: 12px; margin-bottom: 5px; }
.kv span { color: var(--color-text-secondary); min-width: 68px; }
.kv code { word-break: break-all; color: var(--color-text); }
.kv.err strong { color: #c0392b; }
.detail-none { font-size: 12px; color: var(--color-text-secondary); font-style: italic; }
.tool-list { display: flex; gap: 6px; flex-wrap: wrap; }
.tool-chip { background: #eef2f7; color: #4a6fa5; padding: 2px 8px; border-radius: 7px; font-size: 11px; }

.risk-block { background: #fdf6f2; border: 1px solid #f0dcd0; border-radius: 10px; padding: 12px 14px; }
.risk-findings { display: flex; flex-direction: column; gap: 8px; }
.finding { border-left: 3px solid var(--color-border); padding: 6px 10px; background: #fff; border-radius: 0 6px 6px 0; }
.finding.sev-low { border-left-color: #6a9fd8; }
.finding.sev-medium { border-left-color: #e0a92e; }
.finding.sev-high { border-left-color: #d97b3f; }
.finding.sev-critical { border-left-color: #d9534f; }
.finding-head { display: flex; align-items: center; gap: 8px; font-size: 12px; flex-wrap: wrap; }
.sev-dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.finding-cat { font-size: 10.5px; color: var(--color-text-secondary); background: #f4efe8; padding: 1px 7px; border-radius: 7px; }
.finding-sev { font-size: 10.5px; font-weight: 700; margin-left: auto; }
.finding-match { margin-top: 5px; font-size: 11.5px; color: #a83226; word-break: break-all; background: #fbeceb; padding: 4px 8px; border-radius: 5px; }

.code-block { margin: 0; padding: 10px 12px; background: #2d2a24; color: #e8e0d4; font-family: 'SFMono-Regular', Consolas, monospace; font-size: 11.5px; line-height: 1.6; max-height: 260px; overflow: auto; white-space: pre-wrap; word-break: break-all; border-radius: 8px; }

.pager { display: flex; align-items: center; justify-content: center; gap: 16px; margin-top: 16px; }
.pager-info { font-size: 12.5px; color: var(--color-text-secondary); }

.btn { padding: 8px 16px; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer; border: 1px solid transparent; transition: all 0.15s; white-space: nowrap; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn.sm { padding: 5px 12px; font-size: 12px; }
.btn-primary { background: var(--color-primary); color: #fff; }
.btn-primary:hover:not(:disabled) { background: #b87a3f; }
.btn-danger { background: #fbe4e3; color: #c0392b; border-color: #f0c9c6; }
.btn-danger:hover:not(:disabled) { background: #f6d3d0; }
.btn-ghost { background: transparent; border-color: var(--color-border); color: var(--color-text); }
.btn-ghost:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
.err-tip { color: #d9534f; font-size: 12.5px; margin-bottom: 10px; }
.mini-loading, .mini-empty { color: var(--color-text-secondary); font-size: 13px; padding: 32px; text-align: center; }
</style>
