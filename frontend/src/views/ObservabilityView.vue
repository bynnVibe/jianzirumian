<template>
  <div class="obs-view">
    <!-- 顶部标题栏 -->
    <header class="obs-header">
      <div class="header-inner">
        <div class="header-title">
          <span class="header-eyebrow">{{ canOps ? 'Observability' : '我的使用概览' }}</span>
          <h1>{{ canOps ? 'Agent 可观测性' : '可观测性' }}</h1>
          <p v-if="canOps" class="header-sub">
            在线监控 Agent 行为：日志记录每一步、指标聚合 Token/延迟/成功率、追踪回放每次请求的完整 prompt 与 response
          </p>
          <p v-else class="header-sub">了解自己的使用情况与服务完成情况，仅展示当前账号的业务统计。</p>
        </div>
        <div class="header-actions">
          <div class="days-switch">
            <button
              v-for="d in [7, 30]"
              :key="d"
              class="day-btn"
              :class="{ active: days === d }"
              :disabled="loading"
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

      <template v-if="!canOps">
        <p v-if="loading && !businessMetrics" class="empty-hint" role="status">正在加载个人使用概览…</p>
        <template v-if="businessMetrics">
          <section class="cards business-cards">
            <div class="metric-card"><span class="metric-num">{{ businessMetrics.overview.total }}</span><span class="metric-label">使用次数</span></div>
            <div class="metric-card"><span class="metric-num">{{ businessMetrics.overview.succeeded }}</span><span class="metric-label">成功完成</span></div>
            <div class="metric-card"><span class="metric-num">{{ completionPercent(businessMetrics.overview.completion_rate) }}</span><span class="metric-label">服务完成率</span></div>
            <div class="metric-card"><span class="metric-num">{{ responseSeconds(businessMetrics.overview.avg_response_seconds) }}</span><span class="metric-label">平均等待时间</span></div>
          </section>
          <section class="panel">
            <div class="panel-head"><span class="panel-title">处理情况</span></div>
            <p class="business-summary">进行中 {{ businessMetrics.overview.processing }} 次 · 已取消 {{ businessMetrics.overview.cancelled }} 次 · 未完成 {{ businessMetrics.overview.failed }} 次</p>
            <p v-if="businessMetrics.overview.failed" class="empty-hint">遇到未完成的操作，可重新尝试；若多次失败，请联系管理员。</p>
            <p v-if="!businessMetrics.overview.total" class="empty-hint">这段时间还没有使用记录。开始一次问答或使用知识助手后，点击刷新即可查看。</p>
          </section>
          <section class="panel">
            <div class="panel-head"><span class="panel-title">每日使用趋势（近{{ days }}天）</span></div>
            <div v-if="businessMetrics.overview.total" class="trend-chart business-trend">
              <div v-for="row in businessMetrics.daily" :key="row.day" class="trend-col"
                @mouseenter="showTip($event, [row.day, `使用 ${row.total} 次`, `成功 ${row.succeeded} 次`, `未完成 ${row.failed} 次`])"
                @mousemove="moveTip" @mouseleave="hideTip">
                <div class="trend-bar-wrap"><div class="trend-bar" :style="{ height: businessBarHeight(row.total) + '%' }"></div></div>
                <span class="trend-label">{{ row.day.slice(5) }}</span>
              </div>
            </div>
            <p v-else class="empty-hint">暂无使用趋势</p>
          </section>
          <section class="panel business-breakdown">
            <div class="panel-head"><span class="panel-title">业务功能使用情况</span></div>
            <table v-if="businessMetrics.by_activity.length" class="obs-table">
              <thead><tr><th>功能</th><th>使用次数</th><th>成功完成</th><th>服务完成率</th><th>平均等待</th></tr></thead>
              <tbody><tr v-for="row in businessMetrics.by_activity" :key="row.activity">
                <td>{{ row.activity }}</td><td>{{ row.total }}</td><td>{{ row.succeeded }}</td>
                <td>{{ completionPercent(row.completion_rate) }}</td><td>{{ responseSeconds(row.avg_response_seconds) }}</td>
              </tr></tbody>
            </table>
            <p v-else class="empty-hint">暂无功能使用记录</p>
          </section>
          <p class="business-note">仅统计当前账号最近 {{ businessMetrics.retention_days }} 天内保留的服务记录，不代表全站用量。一次操作可能触发问答、笔记整理等不同服务，按各项服务分别计数。完成率与平均等待时间不计入进行中和主动取消的操作；完成率不代表答案准确率。</p>
        </template>
      </template>
      <template v-else>
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
          <span class="metric-num" :class="{ warn: metrics.overview.token_overuse }">
            {{ formatNum(metrics.overview.avg_tokens_per_request) }}
          </span>
          <span class="metric-label">平均 Token/请求</span>
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
            @mouseenter="showTip($event, dayTipLines(row))"
            @mousemove="moveTip"
            @mouseleave="hideTip"
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

      <!-- ============ Token 用量分析 ============ -->
      <section class="panel" v-if="metrics">
        <div class="panel-head">
          <span class="panel-title">Token 用量分析</span>
          <span v-if="metrics.overview.token_overuse" class="token-warn-badge">用量偏高</span>
        </div>

        <!-- 历史基线对比：回答“使用量是否下降/优化” -->
        <div class="token-compare">
          <div class="compare-item">
            <span class="compare-num">{{ formatNum(metrics.token_comparison.current_avg) }}</span>
            <span class="compare-label">近期平均 Token/请求</span>
          </div>
          <div class="compare-arrow" :class="metrics.token_comparison.trend">
            <template v-if="metrics.token_comparison.delta_pct != null">
              <span class="delta">{{ metrics.token_comparison.trend === 'down' ? '▼' : (metrics.token_comparison.trend === 'up' ? '▲' : '—') }} {{ Math.abs(metrics.token_comparison.delta_pct) }}%</span>
              <span class="compare-hint">近期 vs 前期（本周期内）</span>
            </template>
            <span v-else class="compare-hint">数据不足（需≥2次）</span>
          </div>
          <div class="compare-item">
            <span class="compare-num">{{ formatNum(metrics.token_comparison.previous_avg) }}</span>
            <span class="compare-label">前期平均 Token/请求</span>
          </div>
        </div>
        <p class="token-note down" v-if="metrics.token_comparison.trend === 'down'">✓ 平均用量较前期下降 {{ Math.abs(metrics.token_comparison.delta_pct) }}%，优化见效。</p>
        <p class="token-note up" v-else-if="metrics.token_comparison.trend === 'up'">↑ 平均用量较前期上升 {{ metrics.token_comparison.delta_pct }}%，建议关注下方各段构成定位增量来源。</p>
        <p class="token-note warn" v-if="metrics.overview.token_overuse">⚠ 单请求平均 Token（{{ formatNum(metrics.overview.avg_tokens_per_request) }}）已超过告警阈值 {{ formatNum(metrics.overview.token_warn_threshold) }}，可收紧历史窗口（SESSION_MAX_HISTORY）或附件上下文预算。</p>

        <!-- segment 级构成：定位 Token 大户 -->
        <div class="segment-title">上下文各段 Token 构成</div>
        <div v-if="metrics.segment_breakdown.length" class="segment-bars">
          <div v-for="seg in metrics.segment_breakdown" :key="seg.segment" class="segment-group">
            <button
              type="button"
              class="segment-row"
              :aria-expanded="expandedSegments.has(seg.segment)"
              @click="toggleSegment(seg.segment)"
              @mouseenter="showTip($event, segmentTipLines(seg))"
              @mousemove="moveTip"
              @mouseleave="hideTip"
            >
              <span class="segment-label">{{ seg.label }}</span>
              <div class="segment-bar-wrap">
                <div class="segment-bar" :style="{ width: (seg.share * 100).toFixed(1) + '%' }"></div>
              </div>
              <span class="segment-val">
                {{ formatNum(seg.tokens) }} · {{ (seg.share * 100).toFixed(0) }}%
                <span v-if="seg.delta_pct != null" class="seg-delta" :class="seg.trend"
                  :title="`较前期 ${seg.delta_pct}%`">{{ seg.trend === 'down' ? '▼' : (seg.trend === 'up' ? '▲' : '—') }}{{ Math.abs(seg.delta_pct) }}%</span>
                <span v-else class="seg-delta flat">—</span>
              </span>
              <span class="segment-arrow" :class="{ open: expandedSegments.has(seg.segment) }">▾</span>
            </button>
            <div v-if="expandedSegments.has(seg.segment)" class="segment-items">
              <div
                v-for="it in seg.items"
                :key="seg.segment + it.trace_id"
                class="segment-item"
                :title="`点击查看该请求全链路追踪：${it.input}`"
                @click="openTrace(it.trace_id)"
              >
                <span class="seg-item-time">{{ formatTime(it.started_at) }}</span>
                <span class="seg-item-user">{{ it.username }}</span>
                <span class="seg-item-input">{{ it.input || '-' }}</span>
                <span class="seg-item-tok">{{ formatNum(it.tokens) }} tok</span>
              </div>
              <p v-if="!seg.items.length" class="empty-hint">该段暂无会话明细</p>
              <p class="seg-items-hint">以上为该段最近 {{ seg.items.length }} 次会话（按时间倒序），点击某行可查看该请求的全链路追踪。</p>
            </div>
          </div>
        </div>
        <p v-else class="empty-hint">暂无 segment 度量数据（发起一次带知识检索的对话后即可看到）</p>

        <!-- 每日平均 token 趋势 -->
        <div class="segment-title">每日平均 Token/请求趋势</div>
        <div class="trend-chart" v-if="metrics.daily.length">
          <div v-for="row in metrics.daily" :key="'tk-' + row.day" class="trend-col"
            @mouseenter="showTip($event, tokenDayTipLines(row))"
            @mousemove="moveTip"
            @mouseleave="hideTip">
            <div class="trend-bar-wrap">
              <div class="trend-bar token-bar" :style="{ height: tokenBarHeight(row.avg_tokens) + '%' }"></div>
            </div>
            <span class="trend-label">{{ row.day.slice(5) }}</span>
          </div>
        </div>
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
              <option value="cancelled">已取消</option>
              <option value="timeout">超时</option>
              <option value="interrupted">重启中断</option>
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
              <td :title="t.user_id">{{ t.username || t.user_id || '-' }}</td>
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
      </template>
    </div>

    <!-- ============ 图表 hover tooltip ============ -->
    <div v-if="tooltip.show" class="chart-tooltip" :style="{ left: tooltip.x + 14 + 'px', top: tooltip.y + 14 + 'px' }" role="tooltip">
      <div v-for="(line, i) in tooltip.lines" :key="i" :class="['tip-line', i === 0 ? 'tip-title' : '']">{{ line }}</div>
    </div>

    <!-- ============ Trace 详情弹窗（span 时间线） ============ -->
    <div v-if="canOps && traceDetail" class="modal-overlay" @click.self="closeTrace">
      <div class="modal-box trace-box">
        <div class="trace-head">
          <h3 class="modal-title">
            全链路追踪 · <span class="agent-badge" :class="'agent-' + traceDetail.agent">{{ traceDetail.agent }}</span>
          </h3>
          <button class="icon-btn" title="关闭" @click="closeTrace">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>
        <div class="trace-meta">
          <span>状态：<b :class="{ 'cell-err': traceDetail.status === 'error' }">{{ statusText(traceDetail.status) }}</b></span>
          <span>延迟：{{ formatMs(traceDetail.latency_ms) }}</span>
          <span>Token：{{ traceDetail.prompt_tokens }} + {{ traceDetail.completion_tokens }}</span>
          <span :title="traceDetail.user_id">用户：{{ traceDetail.username || traceDetail.user_id || '-' }}</span>
          <span>开始：{{ formatTime(traceDetail.started_at) }}</span>
        </div>
        <div class="trace-input">
          <span class="span-kind-badge step">输入</span>
          <pre class="span-text">{{ traceDetail.input || '-' }}</pre>
        </div>
        <p v-if="traceDetail.error" class="inline-error">错误：{{ traceDetail.error }}</p>

        <div class="trace-log-actions">
          <button class="btn btn-ghost" :aria-expanded="showTraceLogs" @click="toggleTraceLogs">
            {{ showTraceLogs ? '返回步骤追踪' : '查看详细日志' }}
          </button>
          <span class="trace-id">请求 ID：{{ traceDetail.id }}</span>
        </div>
        <section v-if="showTraceLogs" class="trace-logs" aria-label="本次请求详细日志" :aria-busy="logsLoading">
          <div class="trace-log-actions">
            <span>已加载 {{ traceLogs.length }} 条记录</span>
            <button class="btn btn-ghost sm" :disabled="logsLoading" @click="loadTraceLogs(false)">刷新日志</button>
          </div>
          <p class="log-notice">按请求 ID 展示已落盘日志（含异常堆栈），常见凭据已脱敏。旧日志仅能关联带 trace ID 的记录；未记录、已清理或独立进程中未关联的日志无法补齐。</p>
          <p v-for="warning in logWarnings" :key="warning" class="log-notice">{{ warning }}</p>
          <p v-if="logsError" class="inline-error" role="alert">{{ logsError }}</p>
          <div v-if="traceLogs.length" class="log-entries">
            <div v-for="entry in traceLogs" :key="entry.id" class="log-entry">
              <span class="span-io-label">{{ entry.source }}<template v-if="entry.truncated"> · 本条过长，已截断</template></span>
              <pre class="span-text trace-log-text">{{ entry.text }}</pre>
            </div>
          </div>
          <p v-else-if="!logsLoading && !logsError" class="empty-hint">暂未找到可关联日志。可返回步骤追踪查看已保存的 prompt、response 与错误信息。</p>
          <p v-if="logsLoading" class="mini-loading" role="status">日志加载中…</p>
          <button v-if="logsCursor" class="btn btn-ghost" :disabled="logsLoading" @click="loadTraceLogs(true)">继续加载日志</button>
        </section>
        <div v-else class="span-list">
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
import { ref, computed, onMounted } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { getMyObservabilityMetrics, getObservabilityMetrics, getObservabilityTraces, getObservabilityTrace, getObservabilityTraceLogs } from '@/api'

const auth = useAuthStore()
const canOps = computed(() => auth.canOps)
const businessMetrics = ref(null)

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
const showTraceLogs = ref(false)
const traceLogs = ref([])
const logsLoading = ref(false)
const logsLoaded = ref(false)
const logsCursor = ref(null)
const logsError = ref('')
const logWarnings = ref([])
let traceRequestId = 0
let logsRequestId = 0

onMounted(loadAll)

async function loadAll() {
  loading.value = true
  errorMsg.value = ''
  try {
    if (canOps.value) {
      await Promise.all([loadMetrics(), loadTraces()])
    } else {
      businessMetrics.value = null
      const res = await getMyObservabilityMetrics(days.value)
      businessMetrics.value = res.data
    }
  } catch (err) {
    errorMsg.value = canOps.value ? (err.response?.data?.detail || err.message || '加载失败') : '个人使用概览加载失败，请稍后刷新重试。'
  } finally {
    loading.value = false
  }
}

async function loadMetrics() {
  if (!canOps.value) return
  const res = await getObservabilityMetrics(days.value)
  metrics.value = res.data
}

async function loadTraces() {
  if (!canOps.value) return
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
  if (loading.value) return
  days.value = d
  loadAll()
}

function page(dir) {
  offset.value = Math.max(0, offset.value + dir * 30)
  loadTraces()
}

async function openTrace(id) {
  if (!canOps.value) return
  const requestId = ++traceRequestId
  resetTraceLogs()
  errorMsg.value = ''
  try {
    const res = await getObservabilityTrace(id)
    if (requestId !== traceRequestId) return
    if (!res.data.success) throw new Error(res.data.detail || 'trace 不存在')
    traceDetail.value = res.data.trace
    expandedSpans.value = new Set()
  } catch (err) {
    if (requestId === traceRequestId) errorMsg.value = err.response?.data?.detail || err.message || '加载 trace 详情失败'
  }
}

function resetTraceLogs() {
  ++logsRequestId
  showTraceLogs.value = false
  traceLogs.value = []
  logsLoading.value = false
  logsLoaded.value = false
  logsCursor.value = null
  logsError.value = ''
  logWarnings.value = []
}

function closeTrace() {
  ++traceRequestId
  traceDetail.value = null
  resetTraceLogs()
}

function toggleTraceLogs() {
  showTraceLogs.value = !showTraceLogs.value
  if (showTraceLogs.value && !logsLoaded.value) loadTraceLogs()
}

async function loadTraceLogs(append = false) {
  if (!canOps.value || !traceDetail.value || logsLoading.value) return
  const id = traceDetail.value.id
  const requestId = ++logsRequestId
  const cursor = append ? logsCursor.value : ''
  logsLoading.value = true
  logsError.value = ''
  if (!append) {
    traceLogs.value = []
    logsCursor.value = null
    logWarnings.value = []
  }
  try {
    const res = await getObservabilityTraceLogs(id, { cursor: cursor || '', limit: 100 })
    if (requestId !== logsRequestId || traceDetail.value?.id !== id) return
    if (!res.data.success) throw new Error(res.data.detail || '日志读取失败')
    traceLogs.value = append ? [...traceLogs.value, ...res.data.entries] : res.data.entries
    logsCursor.value = res.data.next_cursor
    logWarnings.value = [...new Set([...logWarnings.value, ...(res.data.warnings || [])])]
    logsLoaded.value = true
  } catch (err) {
    if (requestId === logsRequestId) logsError.value = err.response?.data?.detail || err.message || '日志读取失败'
  } finally {
    if (requestId === logsRequestId) logsLoading.value = false
  }
}

function toggleSpan(id) {
  const next = new Set(expandedSpans.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expandedSpans.value = next
}

function completionPercent(value) {
  return value == null ? '—' : `${(value * 100).toFixed(1)}%`
}

function responseSeconds(value) {
  return value == null ? '—' : `${Number(value).toFixed(1)} 秒`
}

function businessBarHeight(count) {
  const max = Math.max(1, ...(businessMetrics.value?.daily || []).map((row) => row.total))
  return count ? Math.max(6, Math.round(count / max * 100)) : 0
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
  return { ok: '成功', error: '失败', running: '进行中', cancelled: '已取消', timeout: '超时', interrupted: '重启中断' }[status] || status
}

function barHeight(count) {
  const max = Math.max(1, ...(metrics.value?.daily || []).map((r) => r.total))
  if (!count) return 3
  return Math.max(6, Math.round((count / max) * 100))
}

function tokenBarHeight(avg) {
  const max = Math.max(1, ...(metrics.value?.daily || []).map((r) => r.avg_tokens || 0))
  if (!avg) return 3
  return Math.max(6, Math.round((avg / max) * 100))
}

// ---- 图表 hover tooltip（鼠标悬停即时显示对应数字）----
const tooltip = ref({ show: false, x: 0, y: 0, lines: [] })

function showTip(event, lines) {
  tooltip.value = { show: true, x: event.clientX, y: event.clientY, lines }
}

function moveTip(event) {
  if (!tooltip.value.show) return
  tooltip.value.x = event.clientX
  tooltip.value.y = event.clientY
}

function hideTip() {
  tooltip.value.show = false
}

function dayTipLines(row) {
  return [
    row.day,
    `请求 ${row.total} · 失败 ${row.errors}`,
    `Token 总量 ${formatNum(row.tokens)}`,
    `平均 ${row.avg_tokens ?? 0} token/请求`,
    `平均延迟 ${formatMs(row.avg_latency_ms)}`,
  ]
}

function tokenDayTipLines(row) {
  return [
    row.day,
    `平均 ${row.avg_tokens ?? 0} token/请求`,
    `Token 总量 ${formatNum(row.tokens)}`,
    `请求 ${row.total}`,
  ]
}

function segmentTipLines(seg) {
  const lines = [
    seg.label,
    `总 ${formatNum(seg.tokens)} token · 占比 ${(seg.share * 100).toFixed(1)}%`,
    `平均 ${seg.avg_tokens} token/次 · ${seg.count} 次`,
  ]
  if (seg.delta_pct != null) {
    lines.push(`近期 ${seg.recent_avg} vs 前期 ${seg.prev_avg}：${seg.delta_pct > 0 ? '+' : ''}${seg.delta_pct}%（${seg.trend === 'down' ? '下降=优化' : seg.trend === 'up' ? '上升' : '持平'}）`)
  } else {
    lines.push('数据不足（需≥2次）')
  }
  lines.push('点击可展开查看对应会话')
  return lines
}

// ---- segment drill-down：展开查看该段对应哪几次会话 ----
const expandedSegments = ref(new Set())

function toggleSegment(seg) {
  const next = new Set(expandedSegments.value)
  if (next.has(seg)) next.delete(seg)
  else next.add(seg)
  expandedSegments.value = next
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

.cards.business-cards { grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
.business-summary { margin: 0; line-height: 1.8; }
.business-note { margin: 0; font-size: 12px; line-height: 1.8; color: var(--color-text-secondary); }
.business-breakdown { overflow-x: auto; }
.business-trend { overflow-x: auto; }
.business-trend .trend-col { min-width: 26px; }

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

/* ---- Token 用量分析 ---- */
.token-bar {
  background: linear-gradient(180deg, #7f9bd1, #4f6bab);
}

.token-warn-badge {
  padding: 2px 10px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 700;
  background: #fdecea;
  color: #c62828;
}

.token-compare {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 14px;
  background: #faf8f5;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  margin-bottom: 10px;
}

.compare-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.compare-num {
  font-size: 20px;
  font-weight: 700;
  color: var(--color-text);
}

.compare-label {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.compare-arrow {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
}

.compare-arrow .delta {
  font-size: 15px;
  font-weight: 700;
}

.compare-arrow.down .delta { color: #2e7d32; }
.compare-arrow.up .delta { color: #c62828; }
.compare-arrow.flat .delta { color: var(--color-text-secondary); }

.compare-hint {
  font-size: 10px;
  color: var(--color-text-secondary);
}

.token-note {
  margin: 0 0 8px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--color-text-secondary);
}

.token-note.down { color: #2e7d32; }
.token-note.up { color: #b26a00; }
.token-note.warn { color: #c62828; }

.segment-title {
  font-size: 12px;
  font-weight: 700;
  color: var(--color-text);
  margin: 14px 0 8px;
}

.segment-bars {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.segment-group {
  display: flex;
  flex-direction: column;
}

.segment-row {
  width: 100%;
  display: grid;
  grid-template-columns: 88px 1fr 168px 18px;
  align-items: center;
  gap: 10px;
  padding: 4px 0;
  background: none;
  border: none;
  cursor: pointer;
  text-align: left;
}

.segment-row:hover .segment-label {
  color: var(--color-primary);
}

.segment-label {
  font-size: 12px;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.segment-bar-wrap {
  height: 14px;
  background: #f0ebe3;
  border-radius: 7px;
  overflow: hidden;
}

.segment-bar {
  height: 100%;
  background: linear-gradient(90deg, #d9a76a, #c98a4b);
  border-radius: 7px;
  min-width: 2px;
  transition: width 0.3s;
}

.segment-val {
  font-size: 11px;
  color: var(--color-text-secondary);
  text-align: right;
  white-space: nowrap;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 6px;
}

.seg-delta {
  font-weight: 700;
}

.seg-delta.down { color: #2e7d32; }
.seg-delta.up { color: #c62828; }
.seg-delta.flat { color: var(--color-text-secondary); }

.segment-arrow {
  color: var(--color-text-secondary);
  font-size: 10px;
  transition: transform 0.2s;
  text-align: center;
}

.segment-arrow.open {
  transform: rotate(180deg);
}

.segment-items {
  margin: 4px 0 8px 98px;
  border-left: 2px solid var(--color-border);
  padding-left: 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.segment-item {
  display: grid;
  grid-template-columns: 92px 72px 1fr 76px;
  gap: 10px;
  align-items: center;
  font-size: 11px;
  color: var(--color-text);
  padding: 3px 6px;
  border-radius: 6px;
  cursor: pointer;
}

.segment-item:hover {
  background: var(--color-primary-light);
}

.seg-item-time,
.seg-item-user {
  color: var(--color-text-secondary);
  white-space: nowrap;
}

.seg-item-input {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.seg-item-tok {
  text-align: right;
  white-space: nowrap;
  font-weight: 600;
  color: var(--color-primary);
}

.seg-items-hint {
  margin: 4px 0 0;
  font-size: 10px;
  color: var(--color-text-secondary);
}

/* ---- 图表 hover tooltip ---- */
.chart-tooltip {
  position: fixed;
  z-index: 200;
  pointer-events: none;
  background: rgba(43, 36, 28, 0.94);
  color: #fff;
  border-radius: 8px;
  padding: 8px 10px;
  font-size: 11px;
  line-height: 1.6;
  max-width: 280px;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.25);
}

.tip-line {
  white-space: nowrap;
}

.tip-title {
  font-weight: 700;
  margin-bottom: 2px;
  color: #f0d9b8;
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

.status-dot.cancelled, .status-dot.interrupted { background: #637080; }
.status-dot.timeout { background: #c28725; }
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

.trace-log-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 12px;
  font-size: 12px;
}

.trace-id, .log-notice {
  font-size: 12px;
  color: var(--color-text-secondary);
  overflow-wrap: anywhere;
}

.log-notice {
  line-height: 1.6;
  margin: 0 0 10px;
}

.log-entries {
  max-height: 50vh;
  overflow-y: auto;
  margin-bottom: 12px;
}

.log-entry .trace-log-text {
  max-height: none;
  overflow-y: visible;
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
