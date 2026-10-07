<template>
  <div class="gateway-view">
    <!-- 顶部标题栏 -->
    <header class="gw-header">
      <div class="header-inner">
        <div class="header-title">
          <span class="header-eyebrow">LLM Local Gateway · 模型网关</span>
          <h1>模型网关</h1>
          <p class="header-sub">
            对下游统一暴露 OpenAI 兼容协议（<code>/v1/chat/completions</code>），凭本地密钥鉴权，屏蔽真实上游 Key。
            内置密钥配额、优先级+权重负载均衡与故障切换、全量请求日志审计，以及风险检测安全审计中心。
          </p>
        </div>
      </div>
      <!-- Tab 导航 -->
      <nav class="gw-tabs">
        <button
          v-for="t in tabs"
          :key="t.key"
          class="gw-tab"
          :class="{ active: activeTab === t.key }"
          @click="activeTab = t.key"
        >
          {{ t.label }}
        </button>
      </nav>
    </header>

    <div class="gw-body">
      <!-- ============ 仪表盘 ============ -->
      <section v-if="activeTab === 'dashboard'" class="dash">
        <div v-if="dashLoading" class="mini-loading">加载总览中…</div>
        <template v-else>
          <!-- 概览卡片 -->
          <div class="stat-grid">
            <div class="stat-card">
              <span class="stat-label">上游渠道</span>
              <span class="stat-value">{{ overview.channel_count || 0 }}</span>
              <span class="stat-sub">启用 {{ overview.channel_enabled || 0 }} 个</span>
            </div>
            <div class="stat-card">
              <span class="stat-label">本地密钥</span>
              <span class="stat-value">{{ overview.key_count || 0 }}</span>
              <span class="stat-sub">启用 {{ overview.key_enabled || 0 }} 个</span>
            </div>
            <div class="stat-card">
              <span class="stat-label">近 {{ statDays }} 天调用</span>
              <span class="stat-value">{{ stats.overview?.total || 0 }}</span>
              <span class="stat-sub">
                成功 {{ stats.overview?.ok || 0 }} · 失败 {{ stats.overview?.errors || 0 }} · 阻断 {{ stats.overview?.blocked || 0 }}
              </span>
            </div>
            <div class="stat-card">
              <span class="stat-label">成功率</span>
              <span class="stat-value">{{ ((stats.overview?.success_rate || 0) * 100).toFixed(1) }}%</span>
              <span class="stat-sub">平均延迟 {{ stats.overview?.avg_latency_ms || 0 }} ms</span>
            </div>
            <div class="stat-card">
              <span class="stat-label">Token 消耗</span>
              <span class="stat-value">{{ formatNum(stats.overview?.tokens || 0) }}</span>
              <span class="stat-sub">命中风险 {{ stats.overview?.risky || 0 }} 次</span>
            </div>
            <div class="stat-card">
              <span class="stat-label">渠道健康</span>
              <span class="stat-value health-mini">
                <i class="h-dot h-healthy" :title="`健康 ${overview.channel_health?.healthy || 0}`"></i>
                <i class="h-dot h-degraded" :title="`降级 ${overview.channel_health?.degraded || 0}`"></i>
                <i class="h-dot h-down" :title="`熔断 ${overview.channel_health?.down || 0}`"></i>
              </span>
              <span class="stat-sub">
                健康 {{ overview.channel_health?.healthy || 0 }} · 降级 {{ overview.channel_health?.degraded || 0 }} · 熔断 {{ overview.channel_health?.down || 0 }}
              </span>
            </div>
          </div>

          <div class="dash-cols">
            <!-- 每日趋势 -->
            <div class="card">
              <div class="card-head">
                <h3 class="card-title">每日调用趋势</h3>
                <select v-model.number="statDays" class="field-input sm" @change="loadStats">
                  <option :value="7">近 7 天</option>
                  <option :value="14">近 14 天</option>
                  <option :value="30">近 30 天</option>
                </select>
              </div>
              <div v-if="stats.daily?.length" class="trend-chart">
                <div v-for="d in stats.daily" :key="d.day" class="trend-col">
                  <div class="trend-bars">
                    <div class="bar bar-ok" :style="{ height: barH(d.ok) + 'px' }" :title="`成功 ${d.ok}`"></div>
                    <div class="bar bar-err" :style="{ height: barH(d.errors) + 'px' }" :title="`失败 ${d.errors}`"></div>
                    <div class="bar bar-blk" :style="{ height: barH(d.blocked) + 'px' }" :title="`阻断 ${d.blocked}`"></div>
                  </div>
                  <span class="trend-day">{{ d.day.slice(5) }}</span>
                </div>
              </div>
              <div v-else class="mini-empty">暂无调用数据</div>
            </div>

            <!-- 风险分布 -->
            <div class="card">
              <h3 class="card-title">风险等级分布</h3>
              <div v-if="stats.by_risk?.length" class="risk-dist">
                <div v-for="r in stats.by_risk" :key="r.level" class="risk-row">
                  <span class="risk-tag" :class="`risk-${r.level}`">{{ riskLabel(r.level) }}</span>
                  <div class="risk-bar-wrap">
                    <div class="risk-bar" :class="`risk-${r.level}`" :style="{ width: riskPct(r.total) + '%' }"></div>
                  </div>
                  <span class="risk-count">{{ r.total }}</span>
                </div>
              </div>
              <div v-else class="mini-empty">暂无风险记录</div>
            </div>
          </div>

          <div class="dash-cols">
            <!-- 渠道调用排行 -->
            <div class="card">
              <h3 class="card-title">渠道调用分布</h3>
              <table v-if="stats.by_channel?.length" class="mini-table">
                <thead>
                  <tr><th>渠道</th><th class="num">调用</th><th class="num">成功率</th><th class="num">Token</th><th class="num">均延迟</th></tr>
                </thead>
                <tbody>
                  <tr v-for="c in stats.by_channel" :key="c.name">
                    <td>{{ c.name }}</td>
                    <td class="num">{{ c.total }}</td>
                    <td class="num">{{ c.total ? Math.round((c.ok / c.total) * 100) : 0 }}%</td>
                    <td class="num">{{ formatNum(c.tokens) }}</td>
                    <td class="num">{{ Math.round(c.avg_latency) }}ms</td>
                  </tr>
                </tbody>
              </table>
              <div v-else class="mini-empty">暂无数据</div>
            </div>

            <!-- Top 密钥 -->
            <div class="card">
              <h3 class="card-title">Top 调用密钥</h3>
              <table v-if="stats.top_keys?.length" class="mini-table">
                <thead>
                  <tr><th>密钥</th><th class="num">调用</th><th class="num">Token</th><th class="num">风险</th></tr>
                </thead>
                <tbody>
                  <tr v-for="k in stats.top_keys" :key="k.name">
                    <td>{{ k.name }}</td>
                    <td class="num">{{ k.total }}</td>
                    <td class="num">{{ formatNum(k.tokens) }}</td>
                    <td class="num"><span v-if="k.risky" class="risk-tag risk-high">{{ k.risky }}</span><span v-else>0</span></td>
                  </tr>
                </tbody>
              </table>
              <div v-else class="mini-empty">暂无数据</div>
            </div>
          </div>

          <!-- 最近风险日志 -->
          <div class="card">
            <div class="card-head">
              <h3 class="card-title">最近风险调用</h3>
              <button class="btn btn-ghost sm" @click="activeTab = 'logs'">查看全部日志 →</button>
            </div>
            <div v-if="overview.recent_risks?.length" class="risk-list">
              <div v-for="r in overview.recent_risks" :key="r.id" class="risk-item">
                <span class="risk-tag" :class="`risk-${r.risk_level}`">{{ riskLabel(r.risk_level) }}</span>
                <span class="risk-item-main">
                  <strong>{{ r.key_name || '匿名' }}</strong> · {{ r.model_requested }}
                  <span v-if="r.audit_action" class="audit-pill">{{ auditLabel(r.audit_action) }}</span>
                </span>
                <span class="risk-item-time">{{ formatTime(r.created_at) }}</span>
              </div>
            </div>
            <div v-else class="mini-empty">近期无风险调用 🎉</div>
          </div>
        </template>
      </section>

      <!-- ============ 渠道 ============ -->
      <ChannelsPanel v-else-if="activeTab === 'channels'" />

      <!-- ============ 密钥 ============ -->
      <KeysPanel v-else-if="activeTab === 'keys'" />

      <!-- ============ 日志 ============ -->
      <LogsPanel v-else-if="activeTab === 'logs'" />

      <!-- ============ 安全审计 ============ -->
      <SecurityPanel v-else-if="activeTab === 'security'" />
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { getGatewayOverview, getGatewayStats } from '@/api'
import ChannelsPanel from '@/components/gateway/ChannelsPanel.vue'
import KeysPanel from '@/components/gateway/KeysPanel.vue'
import LogsPanel from '@/components/gateway/LogsPanel.vue'
import SecurityPanel from '@/components/gateway/SecurityPanel.vue'

const tabs = [
  { key: 'dashboard', label: '仪表盘' },
  { key: 'channels', label: '渠道 · 负载均衡' },
  { key: 'keys', label: '密钥 · 配额' },
  { key: 'logs', label: '日志 · 审计' },
  { key: 'security', label: '安全审计中心' },
]
const activeTab = ref('dashboard')
const dashLoading = ref(true)
const statDays = ref(7)
const overview = reactive({ channel_health: {}, recent_risks: [] })
const stats = reactive({ overview: {}, daily: [], by_channel: [], by_model: [], by_risk: [], top_keys: [] })

const RISK_LABELS = { none: '无风险', low: '低', medium: '中', high: '高', critical: '严重' }
const AUDIT_LABELS = { warn: '警告', mask: '已脱敏', block: '已阻断', '': '审计' }
function riskLabel(l) { return RISK_LABELS[l] || l }
function auditLabel(a) { return AUDIT_LABELS[a] || a }
function formatNum(n) { return (n || 0).toLocaleString('en-US') }
function formatTime(iso) {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
function maxDaily() {
  return Math.max(1, ...(stats.daily || []).map((d) => (d.ok || 0) + (d.errors || 0) + (d.blocked || 0)))
}
function barH(v) { return Math.round(((v || 0) / maxDaily()) * 90) }
function maxRisk() { return Math.max(1, ...(stats.by_risk || []).map((r) => r.total || 0)) }
function riskPct(v) { return Math.round(((v || 0) / maxRisk()) * 100) }

async function loadOverview() {
  try {
    const res = await getGatewayOverview()
    Object.assign(overview, res.data)
  } catch (e) {
    console.error('加载网关总览失败', e)
  }
}
async function loadStats() {
  try {
    const res = await getGatewayStats(statDays.value)
    Object.assign(stats, res.data)
  } catch (e) {
    console.error('加载网关统计失败', e)
  }
}

onMounted(async () => {
  dashLoading.value = true
  await Promise.all([loadOverview(), loadStats()])
  dashLoading.value = false
})
</script>

<style scoped>
.gateway-view {
  height: 100vh;
  overflow-y: auto;
  background:
    radial-gradient(1200px 400px at 100% -10%, #fbf1e2 0%, transparent 60%),
    var(--color-bg);
}

/* ---- 顶部 ---- */
.gw-header {
  border-bottom: 1px solid var(--color-border);
  background: rgba(254, 252, 249, 0.9);
  backdrop-filter: blur(8px);
  position: sticky; top: 0; z-index: 20;
}
.header-inner { padding: 20px 32px 8px; max-width: 1240px; margin: 0 auto; }
.header-eyebrow {
  font-size: 11px; letter-spacing: 2.5px; text-transform: uppercase;
  color: var(--color-primary); font-weight: 700;
}
.header-title h1 { font-size: 22px; font-weight: 700; margin: 2px 0 4px; letter-spacing: -0.3px; }
.header-sub { font-size: 12.5px; color: var(--color-text-secondary); max-width: 880px; line-height: 1.6; }
.header-sub code { background: #f4ece0; padding: 1px 5px; border-radius: 4px; font-size: 11.5px; }

.gw-tabs { max-width: 1240px; margin: 0 auto; padding: 12px 32px 0; display: flex; gap: 4px; flex-wrap: wrap; }
.gw-tab {
  padding: 9px 16px; font-size: 13px; font-weight: 600; cursor: pointer;
  background: transparent; border: none; border-bottom: 2px solid transparent;
  color: var(--color-text-secondary); transition: all 0.15s; border-radius: 6px 6px 0 0;
}
.gw-tab:hover { color: var(--color-primary); background: rgba(201, 138, 75, 0.06); }
.gw-tab.active { color: var(--color-primary); border-bottom-color: var(--color-primary); }

.gw-body { max-width: 1240px; margin: 0 auto; padding: 20px 32px 56px; }

/* ---- 概览卡片 ---- */
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; margin-bottom: 18px; }
.stat-card {
  background: #fffdfb; border: 1px solid var(--color-border); border-radius: 12px;
  padding: 16px 18px; display: flex; flex-direction: column; gap: 4px;
  box-shadow: 0 1px 3px rgba(120, 90, 50, 0.04);
}
.stat-label { font-size: 11.5px; color: var(--color-text-secondary); font-weight: 600; }
.stat-value { font-size: 26px; font-weight: 700; color: var(--color-text); line-height: 1.2; }
.stat-sub { font-size: 11.5px; color: var(--color-text-secondary); }
.health-mini { display: flex; gap: 8px; align-items: center; height: 32px; }
.h-dot { width: 14px; height: 14px; border-radius: 50%; display: inline-block; }
.h-healthy { background: #4a9d5f; }
.h-degraded { background: #e0a92e; }
.h-down { background: #d9534f; }

.dash-cols { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-bottom: 18px; }
@media (max-width: 900px) { .dash-cols { grid-template-columns: 1fr; } }

/* ---- 卡片 ---- */
.card {
  background: #fffdfb; border: 1px solid var(--color-border); border-radius: 12px;
  padding: 18px 20px; box-shadow: 0 1px 3px rgba(120, 90, 50, 0.04);
}
.card-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; gap: 10px; }
.card-title { font-size: 14.5px; font-weight: 700; margin-bottom: 10px; }
.card-head .card-title { margin-bottom: 0; }

/* ---- 趋势图 ---- */
.trend-chart { display: flex; align-items: flex-end; gap: 8px; height: 130px; padding-top: 8px; overflow-x: auto; }
.trend-col { display: flex; flex-direction: column; align-items: center; gap: 6px; flex: 1; min-width: 34px; }
.trend-bars { display: flex; align-items: flex-end; gap: 2px; height: 92px; }
.bar { width: 8px; border-radius: 3px 3px 0 0; min-height: 2px; }
.bar-ok { background: #4a9d5f; }
.bar-err { background: #e0a92e; }
.bar-blk { background: #d9534f; }
.trend-day { font-size: 10px; color: var(--color-text-secondary); white-space: nowrap; }

/* ---- 风险分布 ---- */
.risk-dist { display: flex; flex-direction: column; gap: 10px; }
.risk-row { display: flex; align-items: center; gap: 10px; }
.risk-bar-wrap { flex: 1; height: 10px; background: #f4efe8; border-radius: 5px; overflow: hidden; }
.risk-bar { height: 100%; border-radius: 5px; }
.risk-count { font-size: 12px; color: var(--color-text-secondary); min-width: 28px; text-align: right; }

/* ---- 风险标签 ---- */
.risk-tag { font-size: 11px; font-weight: 700; padding: 2px 9px; border-radius: 10px; white-space: nowrap; }
.risk-none { background: #eee9e2; color: #8a7e72; }
.risk-low { background: #e8f0fb; color: #3a6ea5; }
.risk-medium { background: #fdf0d9; color: #b8802a; }
.risk-high { background: #fbe7d9; color: #c1622a; }
.risk-critical { background: #fbe4e3; color: #c0392b; }
.risk-bar.risk-none { background: #c9c0b4; }
.risk-bar.risk-low { background: #6a9fd8; }
.risk-bar.risk-medium { background: #e0a92e; }
.risk-bar.risk-high { background: #d97b3f; }
.risk-bar.risk-critical { background: #d9534f; }

.audit-pill { font-size: 10.5px; font-weight: 700; padding: 1px 7px; border-radius: 8px; background: #f4ece0; color: #b8802a; margin-left: 6px; }

/* ---- 迷你表格 ---- */
.mini-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.mini-table th { text-align: left; padding: 7px 8px; color: var(--color-text-secondary); font-weight: 600; border-bottom: 1px solid var(--color-border); font-size: 11.5px; }
.mini-table td { padding: 8px; border-bottom: 1px solid #f4efe8; }
.mini-table .num { text-align: right; }
.mini-table th.num { text-align: right; }

/* ---- 最近风险列表 ---- */
.risk-list { display: flex; flex-direction: column; gap: 8px; }
.risk-item { display: flex; align-items: center; gap: 12px; padding: 9px 12px; border-radius: 8px; background: #faf6f0; }
.risk-item-main { flex: 1; font-size: 12.5px; color: var(--color-text); }
.risk-item-time { font-size: 11.5px; color: var(--color-text-secondary); }

/* ---- 通用 ---- */
.field-input { padding: 8px 11px; border: 1px solid var(--color-border); border-radius: 8px; font-size: 13px; background: #fff; color: var(--color-text); outline: none; }
.field-input:focus { border-color: var(--color-primary); }
.field-input.sm { padding: 5px 9px; font-size: 12px; }
.btn { padding: 8px 16px; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer; border: 1px solid transparent; transition: all 0.15s; white-space: nowrap; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn.sm { padding: 5px 12px; font-size: 12px; }
.btn-ghost { background: transparent; border-color: var(--color-border); color: var(--color-text); }
.btn-ghost:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
.mini-loading, .mini-empty { color: var(--color-text-secondary); font-size: 13px; padding: 24px 0; text-align: center; }
</style>
