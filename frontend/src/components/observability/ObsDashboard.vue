<template>
  <div class="obs-dash">
    <div class="dash-head">
      <span class="dash-title">DashBoard · 整体观测指标（{{ rangeLabel }}）</span>
      <button class="btn btn-ghost sm" @click="openEdit">编辑指标</button>
    </div>

    <p v-if="loading && !metrics" class="empty-hint" role="status">正在加载 DashBoard…</p>
    <template v-else>
      <!-- ============ 指标卡（可编辑呈现集合，图3 图标卡样式） ============ -->
      <section class="stat-cards">
        <div
          v-for="c in visibleCards"
          :key="c.id"
          class="stat-card"
          :class="{ clickable: !!c.drill }"
          :title="`${c.label}：${c.value}${c.hint ? ' · ' + c.hint : ''}${c.drill ? '，点击查看详情' : ''}`"
          @click="c.drill && emit('drill', c.drill, c.label)"
        >
          <span class="stat-icon" :class="GROUP_CLS[c.group]">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20">
              <template v-if="c.group === '性能'">
                <polyline points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
              </template>
              <template v-else-if="c.group === '质量'">
                <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" /><polyline points="22 4 12 14.01 9 11.01" />
              </template>
              <template v-else-if="c.group === '安全'">
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
              </template>
              <template v-else>
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" />
              </template>
            </svg>
          </span>
          <span class="stat-body">
            <span class="stat-num" :class="{ warn: c.warn }">{{ c.value }}</span>
            <span class="stat-label">{{ c.label }}</span>
          </span>
        </div>
        <p v-if="!visibleCards.length" class="empty-hint">
          尚未选择指标卡，点击右上角「编辑指标」添加要呈现的观测指标。
        </p>
      </section>

      <!-- ============ 对话趋势（图3 柱状图 + 区间切换） ============ -->
      <section class="panel trend-panel">
        <div class="panel-head">
          <div class="panel-title-wrap">
            <span class="panel-title">对话趋势</span>
            <span class="panel-sub">每日查询数</span>
          </div>
          <div class="range-switch" role="group" aria-label="趋势区间">
            <button v-for="r in TREND_RANGES" :key="r" class="range-btn" :class="{ active: trendDays === r }" @click="loadTrend(r)">
              {{ r }}天
            </button>
          </div>
        </div>
        <p v-if="trendLoading" class="empty-hint" role="status">趋势加载中…</p>
        <div v-else-if="trend.length" class="trend-chart">
          <div v-for="p in trend" :key="p.day" class="trend-col" :title="`${p.day} · ${p.queries} 次查询`">
            <span class="trend-val">{{ p.queries || '' }}</span>
            <div class="trend-bar-wrap">
              <div class="trend-bar" :style="{ height: barHeight(p.queries) }"></div>
            </div>
            <span class="trend-day">{{ shortDay(p.day) }}</span>
          </div>
        </div>
        <p v-else class="empty-hint">区间内暂无对话数据。</p>
        <div class="trend-foot">
          <span>区间查询：<b>{{ trendTotal }}</b></span>
          <span>区间 Token：<b>{{ trendTokens }}</b></span>
          <span class="trend-range-label">{{ trendRangeLabel }}</span>
        </div>
      </section>

      <!-- ============ 概况面板（图3 底部三栏） ============ -->
      <section class="overview-grid">
        <div class="panel">
          <div class="panel-head">
            <span class="panel-title">业务概况</span>
            <span class="panel-sub">{{ businessRows.length }} 项</span>
          </div>
          <ul class="kv-list">
            <li
              v-for="r in businessRows"
              :key="r.label"
              :class="{ drillable: !!r.drill }"
              :title="r.drill ? `${r.label}：${r.value}，点击查看详情` : `${r.label}：${r.value}`"
              @click="r.drill && emit('drill', r.drill, r.label)"
            >
              <span class="kv-label">{{ r.label }}</span>
              <span class="kv-value">{{ r.value }}</span>
            </li>
          </ul>
        </div>
        <div class="panel">
          <div class="panel-head">
            <span class="panel-title">质量与安全概况</span>
            <span class="panel-sub">{{ qualityRows.length }} 项</span>
          </div>
          <ul class="kv-list">
            <li
              v-for="r in qualityRows"
              :key="r.label"
              :class="{ drillable: !!r.drill }"
              :title="r.drill ? `${r.label}：${r.value}，点击查看详情` : `${r.label}：${r.value}`"
              @click="r.drill && emit('drill', r.drill, r.label)"
            >
              <span class="kv-label">{{ r.label }}</span>
              <span class="kv-value" :class="{ warn: r.warn }">{{ r.value }}</span>
            </li>
          </ul>
        </div>
        <div class="panel">
          <div class="panel-head">
            <span class="panel-title">模型与调用资源</span>
            <span class="panel-sub">{{ rangeLabel }}</span>
          </div>
          <div class="mini-grid">
            <div
              v-for="r in resourceRows"
              :key="r.label"
              class="mini-cell"
              :class="{ drillable: !!r.drill }"
              :title="r.drill ? `${r.label}：${r.value}，点击查看详情` : `${r.label}：${r.value}`"
              @click="r.drill && emit('drill', r.drill, r.label)"
            >
              <span class="mini-num">{{ r.value }}</span>
              <span class="mini-label">{{ r.label }}</span>
            </div>
          </div>
        </div>
      </section>
    </template>

    <!-- ============ 编辑呈现指标弹窗 ============ -->
    <div v-if="editing" class="modal-overlay" @click.self="editing = false">
      <div class="modal-box dash-edit" role="dialog" aria-label="编辑 DashBoard 呈现指标">
        <h3 class="modal-title">编辑 DashBoard 呈现指标</h3>
        <p class="edit-hint">勾选需要在 DashBoard 呈现的指标卡，保存后按当前账号持久化。</p>
        <div v-for="g in GROUPS" :key="g" class="edit-group">
          <div class="edit-group-title">{{ g }}</div>
          <label v-for="c in catalogOf(g)" :key="c.id" class="edit-item">
            <input type="checkbox" :value="c.id" v-model="draft" />
            <span>{{ c.label }}</span>
          </label>
        </div>
        <div class="edit-actions">
          <button class="btn btn-ghost" @click="editing = false">取消</button>
          <button class="btn btn-primary" :disabled="saving" @click="save">
            {{ saving ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { getObsDashboard, saveObsDashboard, getObsMonitor } from '@/api'

const props = defineProps({
  metrics: { type: Object, default: null },
  monitor: { type: Object, default: null },
  loading: { type: Boolean, default: false },
  rangeLabel: { type: String, default: '近7天' },
})

// 指标数字钻取：父组件打开明细弹窗
const emit = defineEmits(['drill'])

const GROUPS = ['性能', '质量', '安全', '业务']
const GROUP_CLS = { 性能: 'g-perf', 质量: 'g-quality', 安全: 'g-sec', 业务: 'g-biz' }
const TREND_RANGES = [7, 14, 30]
const DEFAULT_CARDS = ['total', 'success_rate', 'avg_latency', 'tokens', 'likes', 'sec_events', 'sessions', 'active_users']

function formatMs(ms) {
  if (!ms) return '0ms'
  return ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : Math.round(ms) + 'ms'
}
function formatNum(n) {
  if (!n) return '0'
  return n >= 10000 ? (n / 1000).toFixed(1) + 'K' : String(n)
}
function pct(v) {
  return v == null ? '-' : (v * 100).toFixed(1) + '%'
}

// 指标卡目录：value 从 /metrics（性能）与 /monitor（质量/安全/业务）取值；drill 为点击钻取明细类型
const CATALOG = [
  { id: 'total', group: '性能', label: '请求总数', drill: 'traces', hint: '统计区间内 Agent 请求总数', get: (d) => String(d.m?.overview?.total ?? 0) },
  { id: 'success_rate', group: '性能', label: '成功率', drill: 'traces', hint: '成功请求占比（error/timeout 计为失败）', warnIf: (d) => (d.m?.overview?.success_rate ?? 1) < 0.95, get: (d) => pct(d.m?.overview?.success_rate) },
  { id: 'errors', group: '性能', label: '失败请求', drill: 'traces', hint: '状态为 error/timeout 的请求', warnIf: (d) => d.m?.overview?.errors > 0, get: (d) => String(d.m?.overview?.errors ?? 0) },
  { id: 'avg_latency', group: '性能', label: '平均延迟', drill: 'traces', hint: '请求端到端平均耗时', get: (d) => formatMs(d.m?.overview?.avg_latency_ms) },
  { id: 'p95_latency', group: '性能', label: 'P95 延迟', drill: 'traces', hint: '95 分位请求耗时', get: (d) => formatMs(d.m?.overview?.p95_latency_ms) },
  { id: 'tokens', group: '性能', label: 'Token 消耗', drill: 'llm_spans', hint: 'prompt + completion Token 总量', get: (d) => formatNum((d.m?.overview?.prompt_tokens ?? 0) + (d.m?.overview?.completion_tokens ?? 0)) },
  { id: 'avg_tokens', group: '性能', label: '平均 Token/请求', drill: 'traces', hint: '含上下文历史的单请求均值', warnIf: (d) => d.m?.overview?.token_overuse, get: (d) => formatNum(d.m?.overview?.avg_tokens_per_request) },
  { id: 'llm_calls', group: '性能', label: 'LLM 调用', drill: 'llm_spans', hint: '区间内 LLM span 调用次数', get: (d) => String(d.m?.overview?.llm_calls ?? 0) },
  { id: 'tool_calls', group: '性能', label: '工具调用', drill: 'tool_spans', hint: '区间内工具路由调用次数', get: (d) => String(d.m?.overview?.tool_calls ?? 0) },
  { id: 'likes', group: '质量', label: '点赞', drill: 'feedbacks', hint: '用户点赞反馈数', get: (d) => String(d.mon?.quality?.likes ?? 0) },
  { id: 'dislikes', group: '质量', label: '点踩', drill: 'feedbacks', hint: '用户点踩反馈数', warnIf: (d) => d.mon?.quality?.dislikes > 0, get: (d) => String(d.mon?.quality?.dislikes ?? 0) },
  { id: 'eval_relevance', group: '质量', label: '最近回归·答案相关度', drill: 'eval_runs', hint: '最近一次回归评测的答案相关度均值', get: (d) => { const r = d.mon?.quality?.eval_runs?.[0]; return r?.avg?.total ? pct(r.avg.answer_relevance) : '-' } },
  { id: 'skill_high', group: '质量', label: 'Skill 预审高危', drill: 'skill_reviews', hint: 'Skill 静态预审高危计数', warnIf: (d) => d.mon?.quality?.skill_reviews?.high > 0, get: (d) => String(d.mon?.quality?.skill_reviews?.high ?? 0) },
  { id: 'sec_events', group: '安全', label: '网关安全事件', drill: 'sec_events', hint: '网关安全审计事件总数', warnIf: (d) => d.mon?.security?.events_total > 0, get: (d) => String(d.mon?.security?.events_total ?? 0) },
  { id: 'sec_pending', group: '安全', label: '安全事件待处置', drill: 'sec_events', hint: '状态为 pending 的安全事件', warnIf: (d) => d.mon?.security?.pending > 0, get: (d) => String(d.mon?.security?.pending ?? 0) },
  { id: 'sec_high', group: '安全', label: '高危/极高事件', drill: 'sec_events', hint: 'risk_level 为 high/critical 的事件', warnIf: (d) => (d.mon?.security?.high ?? 0) + (d.mon?.security?.critical ?? 0) > 0, get: (d) => String((d.mon?.security?.high ?? 0) + (d.mon?.security?.critical ?? 0)) },
  { id: 'tool_blocked', group: '安全', label: '工具约束拦截', drill: 'tool_blocked', hint: '风险/权限/限流拦截的工具调用', warnIf: (d) => d.mon?.security?.tool_blocked > 0, get: (d) => String(d.mon?.security?.tool_blocked ?? 0) },
  { id: 'sessions', group: '业务', label: '会话总数', drill: 'sessions', hint: '累计对话会话数', get: (d) => String(d.mon?.business?.counts?.sessions ?? 0) },
  { id: 'messages', group: '业务', label: '消息总数', drill: 'messages', hint: '累计用户+助手消息数', get: (d) => String(d.mon?.business?.counts?.messages ?? 0) },
  { id: 'active_users', group: '业务', label: '活跃用户', drill: 'active_users', hint: '产生过消息的用户数', get: (d) => String(d.mon?.business?.counts?.active_users ?? 0) },
  { id: 'uploads', group: '业务', label: '上传记录', drill: 'uploads', hint: '图片/文档上传入库记录', get: (d) => String(d.mon?.business?.counts?.uploads ?? 0) },
  { id: 'kbs', group: '业务', label: '知识库', drill: 'kbs', hint: '知识库总数', get: (d) => String(d.mon?.business?.counts?.knowledge_bases ?? 0) },
  { id: 'wiki', group: '业务', label: '百科页面', drill: 'wiki', hint: 'llm-wiki 编译页面数', get: (d) => String(d.mon?.business?.counts?.wiki_pages ?? 0) },
  { id: 'memories', group: '业务', label: '记忆条数', drill: 'memories', hint: '跨会话用户记忆条数', get: (d) => String(d.mon?.business?.counts?.memories ?? 0) },
]

const prefs = ref(null) // null=未加载；[]=用户清空
const editing = ref(false)
const saving = ref(false)
const draft = ref([])

// ---- 对话趋势（独立按区间拉取） ----
const trendDays = ref(7)
const trend = ref([])
const trendLoading = ref(false)

onMounted(() => {
  loadPrefs()
  loadTrend(trendDays.value)
})

async function loadPrefs() {
  try {
    const res = await getObsDashboard()
    prefs.value = Array.isArray(res.data.cards) && res.data.cards.length ? res.data.cards : DEFAULT_CARDS
  } catch {
    prefs.value = DEFAULT_CARDS
  }
}

async function loadTrend(d) {
  trendDays.value = d
  trendLoading.value = true
  try {
    const res = await getObsMonitor(d, 'days')
    const rows = res.data?.business?.usage_daily || []
    trend.value = [...rows].sort((a, b) => (a.day < b.day ? -1 : 1)).slice(-d)
  } catch {
    trend.value = []
  } finally {
    trendLoading.value = false
  }
}

const trendMax = computed(() => Math.max(1, ...trend.value.map((p) => p.queries || 0)))
const trendTotal = computed(() => trend.value.reduce((s, p) => s + (p.queries || 0), 0))
const trendTokens = computed(() =>
  formatNum(trend.value.reduce((s, p) => s + (p.prompt_chars || 0) + (p.completion_chars || 0), 0)))
const trendRangeLabel = computed(() =>
  trend.value.length ? `${trend.value[0].day} ~ ${trend.value[trend.value.length - 1].day}` : '')

function barHeight(v) {
  return Math.max(2, Math.round(((v || 0) / trendMax.value) * 100)) + '%'
}
function shortDay(day) {
  return String(day).slice(5).replace('-', '/')
}

// ---- 指标卡呈现集合 ----
const selected = computed(() => prefs.value || DEFAULT_CARDS)
const visibleCards = computed(() => {
  const data = { m: props.metrics, mon: props.monitor }
  return CATALOG.filter((c) => selected.value.includes(c.id)).map((c) => ({
    id: c.id,
    group: c.group,
    label: c.label,
    drill: c.drill || '',
    hint: c.hint || '',
    value: props.metrics || props.monitor ? c.get(data) : '-',
    warn: Boolean(c.warnIf && (props.metrics || props.monitor) && c.warnIf(data)),
  }))
})

// ---- 概况面板 ----
const businessRows = computed(() => {
  const c = props.monitor?.business?.counts || {}
  return [
    { label: '会话总数', value: c.sessions ?? 0, drill: 'sessions' },
    { label: '消息总数', value: c.messages ?? 0, drill: 'messages' },
    { label: '活跃用户', value: c.active_users ?? 0, drill: 'active_users' },
    { label: '知识库', value: c.knowledge_bases ?? 0, drill: 'kbs' },
    { label: '百科页面', value: c.wiki_pages ?? 0, drill: 'wiki' },
    { label: '记忆条数', value: c.memories ?? 0, drill: 'memories' },
    { label: '上传记录', value: c.uploads ?? 0, drill: 'uploads' },
  ]
})
const qualityRows = computed(() => {
  const q = props.monitor?.quality || {}
  const s = props.monitor?.security || {}
  return [
    { label: '点赞 / 点踩', value: `${q.likes ?? 0} / ${q.dislikes ?? 0}`, warn: (q.dislikes ?? 0) > 0, drill: 'feedbacks' },
    { label: '回归评测运行', value: q.eval_runs?.length ?? 0, drill: 'eval_runs' },
    { label: 'Skill 预审（高危）', value: `${q.skill_reviews?.total ?? 0}（${q.skill_reviews?.high ?? 0}）`, warn: (q.skill_reviews?.high ?? 0) > 0, drill: 'skill_reviews' },
    { label: '网关安全事件', value: s.events_total ?? 0, warn: (s.events_total ?? 0) > 0, drill: 'sec_events' },
    { label: '待处置 / 高危', value: `${s.pending ?? 0} / ${(s.high ?? 0) + (s.critical ?? 0)}`, warn: (s.pending ?? 0) > 0, drill: 'sec_events' },
    { label: '工具约束拦截', value: s.tool_blocked ?? 0, warn: (s.tool_blocked ?? 0) > 0, drill: 'tool_blocked' },
  ]
})
const resourceRows = computed(() => {
  const o = props.metrics?.overview || {}
  return [
    { label: 'LLM 调用', value: o.llm_calls ?? 0, drill: 'llm_spans' },
    { label: '工具调用', value: o.tool_calls ?? 0, drill: 'tool_spans' },
    { label: '平均延迟', value: formatMs(o.avg_latency_ms), drill: 'traces' },
    { label: 'Token 消耗', value: formatNum((o.prompt_tokens ?? 0) + (o.completion_tokens ?? 0)), drill: 'llm_spans' },
  ]
})

// ---- 编辑 ----
function catalogOf(group) {
  return CATALOG.filter((c) => c.group === group)
}
function openEdit() {
  draft.value = [...selected.value]
  editing.value = true
}
async function save() {
  saving.value = true
  try {
    const res = await saveObsDashboard(draft.value)
    prefs.value = res.data.cards
    editing.value = false
  } catch {
    // 保存失败保留弹窗，用户可重试
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.dash-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 12px;
}
.dash-title { font-size: 13px; font-weight: 600; color: var(--color-text-secondary); }

/* ---- 指标卡（图3 图标卡） ---- */
.stat-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.stat-card {
  background: #fff;
  border: 1px solid var(--color-border);
  border-radius: 14px;
  padding: 16px 18px;
  display: flex;
  align-items: center;
  gap: 14px;
}
.stat-card.clickable { cursor: pointer; transition: border-color 0.15s, box-shadow 0.15s; }
.stat-card.clickable:hover { border-color: var(--color-primary, #c98a4b); box-shadow: 0 4px 14px rgba(201, 138, 75, 0.14); }
.stat-card.clickable:hover .stat-num { color: var(--color-primary, #b4652a); }
.stat-icon {
  width: 42px; height: 42px; border-radius: 11px; flex-shrink: 0;
  display: inline-flex; align-items: center; justify-content: center;
}
.stat-icon.g-perf { background: #e3edfd; color: #2563eb; }
.stat-icon.g-quality { background: #e6f4ea; color: #2e7d46; }
.stat-icon.g-sec { background: #efe6fd; color: #7c3aed; }
.stat-icon.g-biz { background: #fdf0d8; color: #e08a00; }
.stat-body { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.stat-num { font-size: 22px; font-weight: 700; color: var(--color-text); line-height: 1.15; font-variant-numeric: tabular-nums; }
.stat-num.warn { color: #c62828; }
.stat-label { font-size: 12px; color: var(--color-text-secondary); }

/* ---- 面板通用 ---- */
.panel {
  background: #fff;
  border: 1px solid var(--color-border);
  border-radius: 14px;
  padding: 16px 18px;
}
.panel-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 12px; }
.panel-title-wrap { display: flex; align-items: baseline; gap: 8px; }
.panel-title { font-size: 14px; font-weight: 700; }
.panel-sub { font-size: 11.5px; color: var(--color-text-secondary); }

/* ---- 对话趋势 ---- */
.range-switch { display: flex; gap: 4px; background: #f3efe9; border-radius: 8px; padding: 3px; }
.range-btn {
  border: none; background: transparent; cursor: pointer; font-size: 12px; font-weight: 600;
  color: var(--color-text-secondary); padding: 4px 10px; border-radius: 6px;
}
.range-btn.active { background: #fff; color: var(--color-primary); box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
.trend-chart { display: flex; gap: 6px; align-items: stretch; height: 168px; }
.trend-col { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: center; gap: 4px; }
.trend-val { font-size: 10.5px; color: var(--color-text-secondary); height: 14px; }
.trend-bar-wrap {
  flex: 1; width: 100%; max-width: 34px; background: #f0ece6; border-radius: 4px;
  display: flex; align-items: flex-end; overflow: hidden;
}
.trend-bar { width: 100%; background: linear-gradient(180deg, #4f8ef7, #2563eb); border-radius: 4px 4px 0 0; }
.trend-day { font-size: 10px; color: var(--color-text-secondary); white-space: nowrap; }
.trend-foot {
  display: flex; align-items: center; gap: 18px; margin-top: 12px; padding-top: 10px;
  border-top: 1px solid var(--color-border); font-size: 12px; color: var(--color-text-secondary);
}
.trend-foot b { color: var(--color-text); }
.trend-range-label { margin-left: auto; }

/* ---- 概况三栏 ---- */
.overview-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-top: 16px; }
.kv-list { list-style: none; display: flex; flex-direction: column; }
.kv-list li {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  padding: 7px 2px; border-bottom: 1px dashed var(--color-border); font-size: 12.5px;
}
.kv-list li:last-child { border-bottom: none; }
.kv-label { color: var(--color-text-secondary); }
.kv-value { font-weight: 600; font-variant-numeric: tabular-nums; }
.kv-value.warn { color: #c62828; }
.kv-list li.drillable { cursor: pointer; }
.kv-list li.drillable:hover { background: rgba(201, 138, 75, 0.07); }
.kv-list li.drillable:hover .kv-value { color: var(--color-primary, #b4652a); text-decoration: underline; }
.mini-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.mini-cell {
  background: #faf7f2; border-radius: 10px; padding: 12px 10px;
  display: flex; flex-direction: column; align-items: center; gap: 2px;
}
.mini-num { font-size: 18px; font-weight: 700; color: var(--color-text); font-variant-numeric: tabular-nums; }
.mini-label { font-size: 11px; color: var(--color-text-secondary); }
.mini-cell.drillable { cursor: pointer; transition: border-color 0.15s; border: 1px solid transparent; }
.mini-cell.drillable:hover { border-color: var(--color-primary, #c98a4b); background: #fdf6ea; }

/* ---- 通用控件 ---- */
.empty-hint { font-size: 12px; color: var(--color-text-secondary); }
.btn {
  border: 1px solid var(--color-border);
  background: #fff;
  border-radius: 8px;
  cursor: pointer;
  font-size: 13px;
  padding: 7px 14px;
  color: var(--color-text);
}
.btn-ghost:hover { background: rgba(0, 0, 0, 0.04); }
.btn-primary { background: var(--color-text); color: #fff; border-color: var(--color-text); }
.btn-primary:disabled { opacity: 0.6; cursor: not-allowed; }
.sm { padding: 5px 10px; font-size: 12px; }

/* ---- 编辑弹窗 ---- */
.modal-overlay {
  position: fixed; inset: 0; background: rgba(0, 0, 0, 0.4);
  display: flex; align-items: center; justify-content: center; z-index: 60;
}
.modal-box {
  background: var(--color-bg, #fff); border: 1px solid var(--color-border); border-radius: 14px;
  padding: 20px 22px; width: min(560px, 92vw); max-height: 82vh; overflow-y: auto;
}
.modal-title { margin: 0 0 6px; font-size: 16px; }
.edit-hint { margin: 0 0 12px; font-size: 12px; color: var(--color-text-secondary); }
.edit-group { margin-bottom: 12px; }
.edit-group-title { font-size: 12px; font-weight: 600; color: var(--color-text-secondary); margin-bottom: 6px; }
.edit-item { display: inline-flex; align-items: center; gap: 5px; margin: 0 14px 6px 0; font-size: 13px; cursor: pointer; }
.edit-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 6px; }

@media (max-width: 980px) {
  .overview-grid { grid-template-columns: 1fr; }
  .trend-chart { height: 130px; }
}
</style>
