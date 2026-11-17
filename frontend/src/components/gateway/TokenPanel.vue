<template>
  <div class="panel">
    <div class="panel-head">
      <div class="panel-intro">
        <h2>Token 用量分析</h2>
        <p>
          两源合并视图：<strong>网关侧</strong>按密钥 / 模型 / 渠道的请求 Token 用量与配额水位，
          <strong>Agent 侧</strong>上下文段构成与历史基线对比（自可观测性迁移）。
        </p>
      </div>
      <div class="head-tools">
        <select v-model.number="days" class="field-input sm" @change="load">
          <option :value="7">近 7 天</option>
          <option :value="14">近 14 天</option>
          <option :value="30">近 30 天</option>
        </select>
        <button class="btn btn-ghost sm" :disabled="loading" @click="load">{{ loading ? '加载中…' : '刷新' }}</button>
      </div>
    </div>

    <p v-if="errorMsg" class="err-tip">{{ errorMsg }}</p>
    <div v-if="loading && !data" class="mini-loading">加载 Token 分析中…</div>

    <template v-if="data">
      <!-- ============ 总览卡 ============ -->
      <div class="stat-grid">
        <div class="stat-card">
          <span class="stat-label">网关 Token 总量（近 {{ days }} 天）</span>
          <span class="stat-value">{{ formatNum(gw.overview.tokens) }}</span>
          <span class="stat-sub">请求 {{ formatNum(gw.overview.total) }} 次 · 命中风险 {{ gw.overview.risky }} 次</span>
        </div>
        <div class="stat-card">
          <span class="stat-label">网关平均 Token/请求</span>
          <span class="stat-value">{{ formatNum(avgPerRequest) }}</span>
          <span class="stat-sub">成功率 {{ ((gw.overview.success_rate || 0) * 100).toFixed(1) }}%</span>
        </div>
        <div class="stat-card">
          <span class="stat-label">Agent 平均 Token/请求</span>
          <span class="stat-value" :class="{ warn: agent.overview.token_overuse }">{{ formatNum(agent.overview.avg_tokens_per_request) }}</span>
          <span class="stat-sub">
            阈值 {{ formatNum(agent.overview.token_warn_threshold) }}
            <span v-if="agent.overview.token_overuse" class="overuse-tag">超用警示</span>
          </span>
        </div>
        <div class="stat-card">
          <span class="stat-label">Agent Token 总量</span>
          <span class="stat-value">{{ formatNum(agent.overview.total_tokens) }}</span>
          <span class="stat-sub">含上下文各语义段累计</span>
        </div>
      </div>

      <!-- ============ 每日 Token 趋势 ============ -->
      <section class="card">
        <h3 class="card-title">每日 Token 消耗趋势（网关请求日志）</h3>
        <div v-if="gw.daily.length" class="trend-chart">
          <div v-for="d in gw.daily" :key="d.day" class="trend-col" :title="`${d.day} · Token ${formatNum(d.tokens)} · 请求 ${d.total}`">
            <div class="trend-bar-wrap"><div class="trend-bar" :style="{ height: tokenBarH(d.tokens) + '%' }"></div></div>
            <span class="trend-day">{{ d.day.slice(5) }}</span>
          </div>
        </div>
        <div v-else class="mini-empty">暂无调用数据</div>
      </section>

      <!-- ============ 按密钥：用量 × 配额水位 ============ -->
      <section class="card">
        <h3 class="card-title">按密钥用量与配额水位</h3>
        <table v-if="mergedKeys.length" class="mini-table">
          <thead>
            <tr>
              <th>密钥</th><th>类型</th>
              <th class="num">窗口请求</th><th class="num">Prompt</th><th class="num">Completion</th><th class="num">窗口 Token</th><th class="num">风险</th>
              <th class="num">累计已用 / 配额</th><th class="quota-cell">配额水位</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="k in mergedKeys" :key="k.id || k.name">
              <td><strong>{{ k.name }}</strong><span v-if="!k.enabled" class="key-off">已停用</span></td>
              <td>{{ k.key_type_label || k.key_type || '—' }}</td>
              <td class="num">{{ formatNum(k.total) }}</td>
              <td class="num">{{ formatNum(k.prompt_tokens) }}</td>
              <td class="num">{{ formatNum(k.completion_tokens) }}</td>
              <td class="num"><strong>{{ formatNum(k.tokens) }}</strong></td>
              <td class="num"><span v-if="k.risky" class="risk-tag risk-high">{{ k.risky }}</span><span v-else>0</span></td>
              <td class="num">{{ formatNum(k.token_used) }} / {{ k.token_quota ? formatNum(k.token_quota) : '不限' }}</td>
              <td class="quota-cell">
                <template v-if="k.token_quota">
                  <div class="quota-bar-wrap">
                    <div class="quota-bar" :class="quotaLevel(k)" :style="{ width: Math.min(100, quotaPct(k)) + '%' }"></div>
                  </div>
                  <span class="quota-text" :class="quotaLevel(k)">
                    {{ quotaPct(k).toFixed(0) }}% · 余 {{ formatNum(Math.max(0, k.token_remaining || 0)) }}
                  </span>
                </template>
                <span v-else class="quota-text">未设配额</span>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else class="mini-empty">暂无密钥用量数据</div>
        <p class="card-note">窗口 Token = 所选时间范围内该密钥请求日志累计；累计已用 = 密钥全生命周期计数（配额扣减口径）。</p>
      </section>

      <!-- ============ 按模型 / 按渠道 ============ -->
      <div class="two-col">
        <section class="card">
          <h3 class="card-title">按模型</h3>
          <table v-if="gw.by_model.length" class="mini-table">
            <thead><tr><th>模型</th><th class="num">请求</th><th class="num">Token</th><th class="num">占比</th></tr></thead>
            <tbody>
              <tr v-for="m in gw.by_model" :key="m.name" class="drill-row"
                  title="点击查看该模型每次请求的 Token 明细" @click="openDrill('model', m.name)">
                <td>{{ m.name }}</td>
                <td class="num">{{ formatNum(m.total) }}</td>
                <td class="num">{{ formatNum(m.tokens) }}</td>
                <td class="num">{{ gw.overview.tokens ? ((m.tokens / gw.overview.tokens) * 100).toFixed(1) : 0 }}%</td>
              </tr>
            </tbody>
          </table>
          <div v-else class="mini-empty">暂无数据</div>
        </section>
        <section class="card">
          <h3 class="card-title">按渠道（路由规则）</h3>
          <table v-if="gw.by_channel.length" class="mini-table">
            <thead><tr><th>渠道</th><th class="num">请求</th><th class="num">Token</th><th class="num">成功率</th></tr></thead>
            <tbody>
              <tr v-for="c in gw.by_channel" :key="c.name" class="drill-row"
                  title="点击查看该渠道每次请求的 Token 明细" @click="openDrill('channel', c.name)">
                <td>{{ c.name }}</td>
                <td class="num">{{ formatNum(c.total) }}</td>
                <td class="num">{{ formatNum(c.tokens) }}</td>
                <td class="num">{{ c.total ? Math.round((c.ok / c.total) * 100) : 0 }}%</td>
              </tr>
            </tbody>
          </table>
          <div v-else class="mini-empty">暂无数据</div>
        </section>
      </div>

      <!-- ============ Agent 侧：历史基线对比 ============ -->
      <section class="card" v-if="agent.token_comparison">
        <h3 class="card-title">Agent 上下文用量 · 历史基线对比</h3>
        <div class="token-compare">
          <div class="compare-item">
            <span class="compare-num">{{ formatNum(agent.token_comparison.current_avg) }}</span>
            <span class="compare-label">近期平均 Token/请求</span>
          </div>
          <div class="compare-arrow" :class="agent.token_comparison.trend">
            <template v-if="agent.token_comparison.delta_pct != null">
              <span class="delta">{{ trendArrow(agent.token_comparison.trend) }} {{ Math.abs(agent.token_comparison.delta_pct) }}%</span>
              <span class="compare-hint">近期 vs 前期（本周期内）</span>
            </template>
            <span v-else class="compare-hint">数据不足（需≥2次）</span>
          </div>
          <div class="compare-item">
            <span class="compare-num">{{ formatNum(agent.token_comparison.previous_avg) }}</span>
            <span class="compare-label">前期平均 Token/请求</span>
          </div>
        </div>
        <p class="token-note down" v-if="agent.token_comparison.trend === 'down'">✓ 平均用量较前期下降 {{ Math.abs(agent.token_comparison.delta_pct) }}%，优化见效。</p>
        <p class="token-note up" v-else-if="agent.token_comparison.trend === 'up'">↑ 平均用量较前期上升 {{ agent.token_comparison.delta_pct }}%，建议关注下方各段构成定位增量来源。</p>
        <p class="token-note warn" v-if="agent.overview.token_overuse">⚠ 单请求平均 Token（{{ formatNum(agent.overview.avg_tokens_per_request) }}）已超过告警阈值 {{ formatNum(agent.overview.token_warn_threshold) }}，可收紧历史窗口（SESSION_MAX_HISTORY）或附件上下文预算。</p>
      </section>

      <!-- ============ Agent 侧：上下文构成（最新对话 / 历史对话 / 窗口聚合） ============ -->
      <section class="card">
        <div class="ctx-head">
          <h3 class="card-title">Agent 上下文构成 · 按语义段</h3>
          <div class="ctx-modes">
            <button class="ctx-mode" :class="{ active: ctxMode === 'latest' }" @click="switchCtxMode('latest')">最新对话</button>
            <button class="ctx-mode" :class="{ active: ctxMode === 'history' }" @click="switchCtxMode('history')">历史对话</button>
            <button class="ctx-mode" :class="{ active: ctxMode === 'window' }" @click="switchCtxMode('window')">窗口聚合</button>
          </div>
        </div>

        <!-- 最新对话 / 历史对话：单次请求的语义段构成 -->
        <template v-if="ctxMode !== 'window'">
          <div v-if="ctxMode === 'history'" class="ctx-selectors">
            <select v-model="histDate" class="field-input sm ctx-date" @change="onHistDateChange">
              <option value="">全部日期…</option>
              <option v-for="d in explorerDates" :key="d" :value="d">{{ d }}</option>
            </select>
            <select v-model="histSessionId" class="field-input sm ctx-session" @change="onHistSessionChange">
              <option value="" disabled>选择会话…</option>
              <option v-for="s in explorerSessions" :key="s.session_id" :value="s.session_id">
                {{ s.title }} · {{ s.username }} · {{ s.last_active }}
              </option>
            </select>
            <select v-model="histTraceId" class="field-input sm" :disabled="!explorerTraces.length" @change="onHistTraceChange">
              <option v-for="t in explorerTraces" :key="t.trace_id" :value="t.trace_id">
                {{ formatTime(t.started_at) }} · {{ t.input || t.agent }} · {{ formatNum(t.prompt_tokens) }} tok
              </option>
            </select>
          </div>
          <p v-if="explorerError" class="err-tip">{{ explorerError }}</p>
          <div v-if="explorerLoading" class="mini-loading">加载上下文构成中…</div>
          <template v-else-if="explorer && explorer.session_id">
            <p class="ctx-meta">
              <strong>{{ explorer.title }}</strong>
              <span>· {{ explorer.username }}</span>
              <span v-if="explorer.selected_at">· {{ formatTime(explorer.selected_at) }}</span>
              <span>· 本次请求 {{ formatNum(explorer.total_tokens) }} Token</span>
              <button class="btn btn-ghost sm" @click="reloadExplorer">刷新</button>
            </p>
            <!-- 对话消耗对比：本对话总耗 vs 上次对话 / 平均对话 -->
            <div v-if="explorerCompare" class="ctx-compare">
              <span class="cmp-chip cur">本对话总耗 <b>{{ formatNum(explorerCompare.session_tokens) }}</b> tok</span>
              <span v-if="explorerCompare.prev" class="cmp-chip">
                上次对话 <b>{{ formatNum(explorerCompare.prev.tokens) }}</b> tok
                <em :class="deltaClass(explorerCompare.session_tokens, explorerCompare.prev.tokens)">
                  {{ deltaText(explorerCompare.session_tokens, explorerCompare.prev.tokens) }}
                </em>
              </span>
              <span v-else class="cmp-chip first">首次对话（无上次对比）</span>
              <span class="cmp-chip">
                平均对话 <b>{{ formatNum(explorerCompare.avg_tokens) }}</b> tok
                <em :class="deltaClass(explorerCompare.session_tokens, explorerCompare.avg_tokens)">
                  {{ deltaText(explorerCompare.session_tokens, explorerCompare.avg_tokens) }}
                </em>
              </span>
            </div>
            <table v-if="explorer.segments.length" class="mini-table">
              <thead><tr><th>上下文段</th><th class="num">Token</th><th class="num">占比</th></tr></thead>
              <tbody>
                <tr v-for="seg in explorer.segments" :key="seg.segment">
                  <td>{{ seg.label }}</td>
                  <td class="num">{{ formatNum(seg.tokens) }}</td>
                  <td class="num">
                    <div class="share-bar"><span :style="{ width: ((seg.share || 0) * 100).toFixed(1) + '%' }"></span></div>
                    {{ ((seg.share || 0) * 100).toFixed(1) }}%
                  </td>
                </tr>
              </tbody>
            </table>
            <div v-else class="mini-empty">该次请求暂无语义段度量数据</div>
          </template>
          <div v-else-if="!explorerLoading" class="mini-empty">暂无对话 trace 数据（发起一次对话后即可查看）</div>
        </template>

        <!-- 窗口聚合：原有 segment_breakdown 表与钻取 -->
        <template v-else>
        <table v-if="segments.length" class="mini-table seg-table">
          <thead>
            <tr><th>上下文段</th><th class="num">累计 Token</th><th class="num">占比</th><th class="num">次数</th><th class="num">平均/次</th><th class="num">较前期</th></tr>
          </thead>
          <tbody>
            <template v-for="seg in segments" :key="seg.segment">
              <tr class="seg-row" @click="toggleSeg(seg.segment)">
                <td><span class="seg-caret" :class="{ open: expandedSegs.has(seg.segment) }">▸</span>{{ seg.label }}</td>
                <td class="num">{{ formatNum(seg.tokens) }}</td>
                <td class="num">
                  <div class="share-bar"><span :style="{ width: ((seg.share || 0) * 100).toFixed(1) + '%' }"></span></div>
                  {{ ((seg.share || 0) * 100).toFixed(1) }}%
                </td>
                <td class="num">{{ seg.count }}</td>
                <td class="num">{{ formatNum(seg.avg_tokens) }}</td>
                <td class="num">
                  <span v-if="seg.delta_pct != null" class="seg-delta" :class="seg.trend"
                    :title="`近期 ${seg.recent_avg} vs 前期 ${seg.prev_avg}`">{{ trendArrow(seg.trend) }}{{ Math.abs(seg.delta_pct) }}%</span>
                  <span v-else class="seg-delta flat">—</span>
                </td>
              </tr>
              <tr v-if="expandedSegs.has(seg.segment)" class="seg-drill-row">
                <td colspan="6">
                  <div class="seg-items">
                    <p class="seg-drill-title">「{{ seg.label }}」最近 {{ (seg.items || []).length }} 次会话</p>
                    <div v-for="it in seg.items || []" :key="seg.segment + it.trace_id" class="seg-item">
                      <span class="seg-item-time">{{ formatTime(it.started_at) }}</span>
                      <span class="seg-item-user">{{ it.username }}</span>
                      <span class="seg-item-input">{{ it.input || '-' }}</span>
                      <span class="seg-item-tok">{{ formatNum(it.tokens) }} tok</span>
                    </div>
                    <p v-if="!(seg.items || []).length" class="mini-empty sm">该段暂无会话明细</p>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
        <div v-else class="mini-empty">暂无 segment 度量数据（发起一次带知识检索的对话后即可看到）</div>
        <p class="card-note">窗口聚合 = 所选时间范围内全部请求的累计；点击行展开最近会话明细。</p>
        </template>
      </section>
    </template>

    <!-- ============ Token 钻取模态：某模型/某渠道每次请求的 Token 明细 ============ -->
    <div v-if="drill.visible" class="drill-overlay" @click.self="closeDrill">
      <div class="drill-panel">
        <div class="drill-head">
          <div>
            <h3 class="drill-title">{{ drill.dim === 'model' ? '模型' : '渠道' }}：{{ drill.name }}</h3>
            <p class="drill-sub">
              近 {{ days }} 天每次请求的 Token 消耗明细 · 共 {{ formatNum(drill.count) }} 次请求 ·
              合计 {{ formatNum(drill.tokens) }} Token
            </p>
          </div>
          <button class="btn btn-ghost sm" @click="closeDrill">关闭</button>
        </div>
        <div v-if="drill.loading" class="mini-loading">加载明细中…</div>
        <p v-else-if="drill.error" class="err-tip">{{ drill.error }}</p>
        <div v-else-if="!drill.requests.length" class="mini-empty">该时间窗内无请求记录</div>
        <div v-else class="drill-body">
          <table class="mini-table">
            <thead>
              <tr>
                <th>时间</th>
                <th v-if="drill.dim === 'channel'">模型</th>
                <th v-else>渠道</th>
                <th>密钥</th>
                <th>角色</th>
                <th>状态</th>
                <th class="num">Prompt</th>
                <th class="num">Completion</th>
                <th class="num">Total</th>
                <th class="num">延迟</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in drill.requests" :key="r.id">
                <td class="nowrap">{{ formatTime(r.created_at) }}</td>
                <td>{{ drill.dim === 'channel' ? (r.model_requested || '-') : (r.channel_name || '-') }}</td>
                <td>{{ r.key_name || '-' }}</td>
                <td>{{ r.role || '-' }}</td>
                <td><span class="drill-status" :class="r.status === 'ok' ? 'ok' : 'err'">{{ r.status }}</span></td>
                <td class="num">{{ formatNum(r.prompt_tokens) }}</td>
                <td class="num">{{ formatNum(r.completion_tokens) }}</td>
                <td class="num strong">{{ formatNum(r.total_tokens) }}</td>
                <td class="num">{{ r.latency_ms }}ms</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { getGatewayTokenAnalysis, getContextExplorer, getGatewayTokenRequests } from '@/api'

const days = ref(7)
const loading = ref(false)
const errorMsg = ref('')
const data = ref(null)
const expandedSegs = ref(new Set())

// Token 钻取：点击「按模型 / 按渠道」表格行 → 拉取该维度每次请求的 Token 明细
const drill = reactive({
  visible: false, dim: 'model', name: '', loading: false, error: '',
  requests: [], count: 0, tokens: 0,
})

// 上下文构成三模式：latest（默认最新对话）/ history（历史对话选择器）/ window（窗口聚合）
const ctxMode = ref('latest')
const explorer = ref(null)
const explorerLoading = ref(false)
const explorerError = ref('')
const histSessionId = ref('')
const histTraceId = ref('')
const histDate = ref('')
const explorerSessions = computed(() => explorer.value?.sessions || [])
const explorerTraces = computed(() => explorer.value?.traces || [])
const explorerDates = computed(() => explorer.value?.dates || [])
const explorerCompare = computed(() => explorer.value?.compare || null)

const gw = computed(() => data.value?.gateway || { overview: {}, daily: [], by_key: [], by_model: [], by_channel: [] })
const agent = computed(() => data.value?.agent || { overview: {}, token_comparison: null, segment_breakdown: [] })
const segments = computed(() => agent.value.segment_breakdown || [])
const avgPerRequest = computed(() => {
  const o = gw.value.overview || {}
  return o.total ? Math.round((o.tokens || 0) / o.total) : 0
})

// 按密钥合并：配额表（全量密钥）× 窗口用量（by_key），按窗口 Token 降序
const mergedKeys = computed(() => {
  const byName = new Map((gw.value.by_key || []).map((k) => [k.name, k]))
  const rows = (data.value?.keys_quota || []).map((k) => {
    const u = byName.get(k.name) || {}
    return {
      ...k,
      total: u.total || 0, prompt_tokens: u.prompt_tokens || 0,
      completion_tokens: u.completion_tokens || 0, tokens: u.tokens || 0, risky: u.risky || 0,
    }
  })
  return rows.sort((a, b) => (b.tokens - a.tokens) || (b.token_used - a.token_used))
})

function formatNum(n) { return Math.round(n || 0).toLocaleString('en-US') }
function trendArrow(t) { return t === 'down' ? '▼' : (t === 'up' ? '▲' : '—') }
function formatTime(iso) {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
function tokenBarH(v) {
  const max = Math.max(1, ...(gw.value.daily || []).map((d) => d.tokens || 0))
  if (!v) return 3
  return Math.max(6, Math.round((v / max) * 100))
}
function quotaPct(k) { return k.token_quota ? ((k.token_used || 0) / k.token_quota) * 100 : 0 }
function quotaLevel(k) {
  const p = quotaPct(k)
  return p >= 100 ? 'danger' : (p >= 80 ? 'warn' : 'ok')
}
function toggleSeg(seg) {
  const s = new Set(expandedSegs.value)
  if (s.has(seg)) s.delete(seg); else s.add(seg)
  expandedSegs.value = s
}

async function load() {
  loading.value = true
  errorMsg.value = ''
  try {
    const res = await getGatewayTokenAnalysis(days.value, 'days')
    data.value = res.data
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '加载 Token 分析失败'
  } finally {
    loading.value = false
  }
}

async function openDrill(dim, name) {
  drill.dim = dim
  drill.name = name
  drill.visible = true
  drill.loading = true
  drill.error = ''
  drill.requests = []
  drill.count = 0
  drill.tokens = 0
  try {
    const params = { days: days.value }
    if (dim === 'model') params.model = name
    else params.channel = name
    const res = await getGatewayTokenRequests(params)
    drill.requests = res.data.requests || []
    drill.count = res.data.count || 0
    drill.tokens = res.data.tokens || 0
  } catch (e) {
    drill.error = e.response?.data?.detail || '加载 Token 明细失败'
  } finally {
    drill.loading = false
  }
}

function closeDrill() {
  drill.visible = false
}

async function loadExplorer({ date = '', sessionId = '', traceId = '' } = {}) {
  explorerLoading.value = true
  explorerError.value = ''
  try {
    const res = await getContextExplorer({ date, sessionId, traceId })
    explorer.value = res.data
    histDate.value = date
    histSessionId.value = res.data.session_id || ''
    histTraceId.value = res.data.selected_trace_id || ''
  } catch (e) {
    explorerError.value = e.response?.data?.detail || '加载上下文构成失败'
  } finally {
    explorerLoading.value = false
  }
}

function switchCtxMode(mode) {
  ctxMode.value = mode
  if (mode === 'latest') loadExplorer()
  else if (mode === 'history' && !explorer.value) loadExplorer()
}

function reloadExplorer() {
  if (ctxMode.value === 'history') loadExplorer({ date: histDate.value, sessionId: histSessionId.value, traceId: histTraceId.value })
  else loadExplorer()
}

function onHistDateChange() {
  // 日期筛选：重载该日期会话列表，后端缺省选中该日期最新会话
  loadExplorer({ date: histDate.value })
}

function onHistSessionChange() {
  // 切换会话：trace 默认取该会话最新一次
  loadExplorer({ date: histDate.value, sessionId: histSessionId.value })
}

function onHistTraceChange() {
  loadExplorer({ date: histDate.value, sessionId: histSessionId.value, traceId: histTraceId.value })
}

// 对比变化：相对基准（上次/平均）的 Δ 百分比
function deltaText(cur, base) {
  if (!base) return '—'
  const pct = ((cur - base) / base) * 100
  const sign = pct > 0 ? '+' : ''
  return `${sign}${pct.toFixed(0)}%`
}
function deltaClass(cur, base) {
  if (!base || cur === base) return 'flat'
  return cur > base ? 'up' : 'down'
}

onMounted(() => {
  load()
  loadExplorer()
})
</script>

<style scoped>
.panel { display: flex; flex-direction: column; gap: 18px; }
.panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.panel-intro h2 { font-size: 16px; font-weight: 700; margin-bottom: 4px; }
.panel-intro p { font-size: 12.5px; color: var(--color-text-secondary); line-height: 1.6; max-width: 780px; }
.head-tools { display: flex; gap: 8px; align-items: center; }

.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; }
.stat-card { background: #fffdfb; border: 1px solid var(--color-border); border-radius: 12px; padding: 16px 18px; display: flex; flex-direction: column; gap: 4px; box-shadow: 0 1px 3px rgba(120,90,50,0.04); }
.stat-label { font-size: 11.5px; color: var(--color-text-secondary); font-weight: 600; }
.stat-value { font-size: 26px; font-weight: 700; line-height: 1.2; }
.stat-value.warn { color: #c62828; }
.stat-sub { font-size: 11.5px; color: var(--color-text-secondary); display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.overuse-tag { font-size: 10px; font-weight: 700; padding: 1px 7px; border-radius: 8px; background: #fdecea; color: #c62828; }

.card { background: #fffdfb; border: 1px solid var(--color-border); border-radius: 12px; padding: 18px 20px; box-shadow: 0 1px 3px rgba(120,90,50,0.04); }
.card-title { font-size: 14.5px; font-weight: 700; margin-bottom: 12px; }
.card-note { font-size: 11px; color: var(--color-text-secondary); margin: 10px 0 0; line-height: 1.6; }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
@media (max-width: 900px) { .two-col { grid-template-columns: 1fr; } }

/* 每日趋势 */
.trend-chart { display: flex; align-items: flex-end; gap: 8px; height: 130px; padding-top: 8px; overflow-x: auto; }
.trend-col { display: flex; flex-direction: column; align-items: center; gap: 6px; flex: 1; min-width: 34px; height: 100%; justify-content: flex-end; }
.trend-bar-wrap { width: 100%; height: 92px; display: flex; align-items: flex-end; justify-content: center; }
.trend-bar { width: 60%; max-width: 26px; background: linear-gradient(180deg, #7f9bd1, #4f6bab); border-radius: 4px 4px 0 0; transition: height 0.3s; }
.trend-day { font-size: 10px; color: var(--color-text-secondary); white-space: nowrap; }

/* 表格 */
.mini-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.mini-table th { text-align: left; padding: 7px 8px; color: var(--color-text-secondary); font-weight: 600; border-bottom: 1px solid var(--color-border); font-size: 11.5px; white-space: nowrap; }
.mini-table td { padding: 8px; border-bottom: 1px solid #f4efe8; }
.mini-table .num { text-align: right; }
.mini-table th.num { text-align: right; }
.key-off { font-size: 10px; color: #a99c8c; background: #eee9e2; padding: 1px 6px; border-radius: 6px; margin-left: 6px; }

/* Token 钻取：可点击行 + 明细模态 */
.drill-row { cursor: pointer; transition: background 0.15s; }
.drill-row:hover { background: #faf5ee; }
.drill-overlay { position: fixed; inset: 0; background: rgba(60,45,30,0.35); display: flex; align-items: center; justify-content: center; z-index: 1000; padding: 24px; }
.drill-panel { background: #fffdfb; border: 1px solid var(--color-border); border-radius: 14px; width: min(960px, 100%); max-height: 84vh; display: flex; flex-direction: column; box-shadow: 0 12px 40px rgba(80,60,30,0.22); }
.drill-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 18px 22px; border-bottom: 1px solid var(--color-border); }
.drill-title { font-size: 15px; font-weight: 700; }
.drill-sub { font-size: 12px; color: var(--color-text-secondary); margin-top: 4px; }
.drill-body { padding: 12px 22px 20px; overflow: auto; }
.drill-status { font-size: 11px; font-weight: 600; padding: 1px 8px; border-radius: 8px; }
.drill-status.ok { background: #e8f5e9; color: #2e7d32; }
.drill-status.err { background: #fdecea; color: #c62828; }
.nowrap { white-space: nowrap; }
.mini-table td.strong { font-weight: 700; }
.risk-tag { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 8px; }
.risk-high { background: #fbe7d9; color: #c1622a; }

/* 配额水位 */
.quota-cell { min-width: 150px; }
.quota-bar-wrap { height: 8px; background: #f4efe8; border-radius: 4px; overflow: hidden; margin-bottom: 4px; }
.quota-bar { height: 100%; border-radius: 4px; transition: width 0.3s; }
.quota-bar.ok { background: #4a9d5f; }
.quota-bar.warn { background: #e0a92e; }
.quota-bar.danger { background: #d9534f; }
.quota-text { font-size: 10.5px; color: var(--color-text-secondary); white-space: nowrap; }
.quota-text.warn { color: #b8802a; font-weight: 700; }
.quota-text.danger { color: #c0392b; font-weight: 700; }

/* 历史基线对比 */
.token-compare { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 14px; background: #faf8f5; border: 1px solid var(--color-border); border-radius: 10px; margin-bottom: 10px; }
.compare-item { display: flex; flex-direction: column; gap: 2px; }
.compare-num { font-size: 20px; font-weight: 700; }
.compare-label { font-size: 11px; color: var(--color-text-secondary); }
.compare-arrow { display: flex; flex-direction: column; align-items: center; gap: 2px; }
.compare-arrow .delta { font-size: 15px; font-weight: 700; }
.compare-arrow.down .delta { color: #2e7d32; }
.compare-arrow.up .delta { color: #c62828; }
.compare-arrow.flat .delta { color: var(--color-text-secondary); }
.compare-hint { font-size: 10px; color: var(--color-text-secondary); }
.token-note { margin: 0 0 8px; font-size: 12px; line-height: 1.7; color: var(--color-text-secondary); }
.token-note.down { color: #2e7d32; }
.token-note.up { color: #b26a00; }
.token-note.warn { color: #c62828; }

/* 段构成 */
.ctx-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
.ctx-head .card-title { margin-bottom: 0; }
.ctx-modes { display: flex; gap: 6px; }
.ctx-mode { border: 1px solid var(--color-border); background: #fff; border-radius: 8px; padding: 4px 12px; font-size: 12px; color: var(--color-text-secondary); cursor: pointer; }
.ctx-mode:hover { border-color: var(--color-primary); color: var(--color-primary); }
.ctx-mode.active { background: var(--color-primary); border-color: var(--color-primary); color: #fff; font-weight: 600; }
.ctx-selectors { display: flex; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }
.ctx-session { min-width: 280px; max-width: 460px; }
.ctx-meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 12px; color: var(--color-text-secondary); margin: 0 0 10px; }
.ctx-meta strong { color: var(--color-text); }
.ctx-compare { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin: 0 0 12px; }
.cmp-chip { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; padding: 4px 10px; border-radius: 14px; background: #f6f2ea; border: 1px solid #ece4d6; color: var(--color-text-secondary); }
.cmp-chip b { color: var(--color-text); font-weight: 700; }
.cmp-chip.cur { background: var(--color-primary-light); border-color: var(--color-primary); }
.cmp-chip.first { font-style: italic; }
.cmp-chip em { font-style: normal; font-weight: 700; }
.cmp-chip em.up { color: #c62828; }
.cmp-chip em.down { color: #2e7d32; }
.cmp-chip em.flat { color: var(--color-text-secondary); }
.seg-table .seg-row { cursor: pointer; }
.seg-table .seg-row:hover { background: var(--color-primary-light); }
.seg-caret { display: inline-block; color: var(--color-primary); font-size: 11px; margin-right: 5px; transition: transform 0.15s ease; }
.seg-caret.open { transform: rotate(90deg); }
.share-bar { display: inline-block; vertical-align: middle; width: 60px; height: 6px; margin-right: 8px; background: #f0ebe3; border-radius: 3px; overflow: hidden; }
.share-bar span { display: block; height: 100%; background: var(--color-primary); border-radius: 3px; }
.seg-delta { font-weight: 700; }
.seg-delta.down { color: #2e7d32; }
.seg-delta.up { color: #c62828; }
.seg-delta.flat { color: var(--color-text-secondary); }
.seg-drill-row > td { background: #fbf7ef; padding: 6px 10px; }
.seg-items { border-left: 2px solid var(--color-border); padding-left: 12px; display: flex; flex-direction: column; gap: 4px; }
.seg-drill-title { margin: 4px 0; font-size: 12px; font-weight: 600; color: var(--color-text-secondary); }
.seg-item { display: grid; grid-template-columns: 92px 72px 1fr 76px; gap: 10px; align-items: center; font-size: 11px; padding: 3px 6px; border-radius: 6px; }
.seg-item-time, .seg-item-user { color: var(--color-text-secondary); white-space: nowrap; }
.seg-item-input { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.seg-item-tok { text-align: right; white-space: nowrap; font-weight: 600; color: var(--color-primary); }

/* 通用 */
.field-input { padding: 8px 11px; border: 1px solid var(--color-border); border-radius: 8px; font-size: 13px; background: #fff; color: var(--color-text); outline: none; }
.field-input:focus { border-color: var(--color-primary); }
.field-input.sm { padding: 5px 9px; font-size: 12px; }
.btn { padding: 8px 16px; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer; border: 1px solid transparent; transition: all 0.15s; white-space: nowrap; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn.sm { padding: 5px 12px; font-size: 12px; }
.btn-ghost { background: transparent; border-color: var(--color-border); color: var(--color-text); }
.btn-ghost:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
.err-tip { color: #d9534f; font-size: 12.5px; }
.mini-loading, .mini-empty { color: var(--color-text-secondary); font-size: 13px; padding: 24px 0; text-align: center; }
.mini-empty.sm { padding: 6px 0; font-size: 11.5px; text-align: left; }
</style>