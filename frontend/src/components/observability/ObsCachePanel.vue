<template>
  <section class="panel cache-panel">
    <div class="panel-head">
      <span class="panel-title">
        语义缓存监控（命中率 / 时延与 Token 对比 / 节省 / 空间占用）
        <span class="cache-state" :class="data?.enabled ? 'on' : 'off'">{{ data?.enabled ? '缓存已启用' : '缓存未启用' }}</span>
      </span>
      <button class="btn btn-ghost sm" :disabled="loading" @click="load(true)">{{ loading ? '加载中…' : '刷新' }}</button>
    </div>
    <p class="cache-sub">
      命中即复用缓存答案（不再消耗上游 Token）；未命中对照为同窗口真实出网成功请求。语义详情已脱敏。
    </p>
    <p v-if="errorMsg" class="inline-error">{{ errorMsg }}</p>
    <div v-if="loading && !data" class="empty-hint">加载缓存监控中…</div>

    <template v-if="data">
      <!-- 总览卡 -->
      <div class="cards cache-cards">
        <div class="metric-card">
          <span class="metric-num" :class="{ good: data.overview.hit_rate >= 0.3 }">{{ (data.overview.hit_rate * 100).toFixed(1) }}%</span>
          <span class="metric-label">命中率（{{ data.overview.hits }} 命中 / {{ data.overview.requests }} 次查找）</span>
        </div>
        <div class="metric-card">
          <span class="metric-num good">{{ fmtDuration(data.overview.saved_time_ms) }}</span>
          <span class="metric-label">节省时间（命中 × 单次提速）</span>
        </div>
        <div class="metric-card">
          <span class="metric-num good">{{ formatNum(data.overview.saved_tokens) }}</span>
          <span class="metric-label">节省 Token（跳过的上游消耗）</span>
        </div>
        <div class="metric-card">
          <span class="metric-num">{{ data.space.mb }} MB</span>
          <span class="metric-label">空间占用（{{ data.space.entries }} 条 · 公共 {{ data.space.public_entries }} / 个人 {{ data.space.personal_entries }} · 过期 {{ data.space.expired_entries }}）</span>
        </div>
      </div>

      <!-- 命中 vs 未命中对比 -->
      <div class="cache-block-title">命中 vs 未命中（近 {{ data.days }} 天）</div>
      <table class="obs-table">
        <thead>
          <tr><th>口径</th><th>平均响应时间</th><th>花费 Token</th><th>样本数</th></tr>
        </thead>
        <tbody>
          <tr>
            <td><span class="hit-tag hit">缓存命中</span></td>
            <td>{{ data.comparison.hit_avg_latency_ms }} ms（查找开销）</td>
            <td>≈ 0（复用缓存答案）</td>
            <td>{{ data.overview.hits }}</td>
          </tr>
          <tr>
            <td><span class="hit-tag miss">未命中 · 真实出网</span></td>
            <td>{{ formatNum(data.comparison.miss_avg_latency_ms) }} ms（端到端）</td>
            <td>{{ formatNum(data.comparison.miss_avg_tokens) }} /请求</td>
            <td>{{ data.comparison.miss_samples }}</td>
          </tr>
        </tbody>
      </table>

      <!-- 每日趋势 -->
      <div class="cache-block-title">每日命中率与节省 Token</div>
      <div class="trend-chart" v-if="data.daily.length">
        <div v-for="row in data.daily" :key="row.day" class="trend-col"
          :title="`${row.day} · 命中率 ${(row.hit_rate * 100).toFixed(1)}%（${row.hits} 命中 / ${row.misses} 未命中）· 节省 ${formatNum(row.saved_tokens)} Token`">
          <div class="trend-bar-wrap">
            <div class="trend-bar cache-bar" :style="{ height: rateBarH(row.hit_rate) + '%' }"></div>
          </div>
          <span class="trend-label">{{ row.day.slice(5) }}</span>
          <span class="trend-rate">{{ (row.hit_rate * 100).toFixed(0) }}%</span>
        </div>
      </div>
      <p v-else class="empty-hint">暂无缓存事件（启用语义缓存并发起问答后开始记录）</p>

      <!-- 语义详情 -->
      <div class="cache-block-title">缓存语义详情（按命中次数 Top {{ data.entries.length }} · 点击展开答案摘要）</div>
      <table v-if="data.entries.length" class="obs-table cache-entries">
        <thead>
          <tr><th>问题（脱敏摘要）</th><th>命中次数</th><th>归属</th><th>TTL 档</th><th>状态</th><th>最近使用</th></tr>
        </thead>
        <tbody>
          <template v-for="e in data.entries" :key="e.id">
            <tr class="entry-row" @click="toggle(e.id)">
              <td class="entry-q"><span class="entry-caret" :class="{ open: expanded.has(e.id) }">▸</span>{{ e.question }}</td>
              <td>{{ e.hit_count }}</td>
              <td>{{ e.scope === 'public' ? '公共' : '个人' }}</td>
              <td>{{ e.ttl_label }}</td>
              <td><span class="entry-state" :class="e.expired ? 'expired' : 'active'">{{ e.expired ? '已过期' : '有效' }}</span></td>
              <td>{{ e.last_used_at || '-' }}</td>
            </tr>
            <tr v-if="expanded.has(e.id)" class="entry-drill">
              <td colspan="6"><p class="entry-answer">{{ e.answer || '（无答案摘要）' }}</p></td>
            </tr>
          </template>
        </tbody>
      </table>
      <p v-else class="empty-hint">暂无缓存条目</p>
    </template>
  </section>
</template>

<script setup>
import { ref, watch, onMounted } from 'vue'
import { getObsCacheMonitor } from '@/api'

const props = defineProps({ days: { type: Number, default: 7 } })

const data = ref(null)
const loading = ref(false)
const errorMsg = ref('')
const expanded = ref(new Set())

function formatNum(n) { return Math.round(n || 0).toLocaleString('en-US') }
function fmtDuration(ms) {
  const s = Math.round((ms || 0) / 1000)
  if (s < 60) return `${s} 秒`
  if (s < 3600) return `${(s / 60).toFixed(1)} 分钟`
  return `${(s / 3600).toFixed(1)} 小时`
}
function rateBarH(rate) { return Math.max(4, Math.round((rate || 0) * 100)) }
function toggle(id) {
  const s = new Set(expanded.value)
  if (s.has(id)) s.delete(id); else s.add(id)
  expanded.value = s
}

async function load(refresh = false) {
  loading.value = true
  errorMsg.value = ''
  try {
    const res = await getObsCacheMonitor(props.days, refresh ? 1 : 0)
    data.value = res.data
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '加载缓存监控失败'
  } finally {
    loading.value = false
  }
}

watch(() => props.days, () => load())
onMounted(() => load())
</script>

<style scoped>
.panel { background: #fffdfb; border: 1px solid var(--color-border); border-radius: 12px; padding: 18px 20px; box-shadow: 0 1px 3px rgba(120,90,50,0.04); margin-bottom: 18px; }
.panel-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 8px; }
.panel-title { font-size: 14.5px; font-weight: 700; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.cache-state { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 8px; }
.cache-state.on { background: #e4f4e8; color: #3d8b52; }
.cache-state.off { background: #eee9e2; color: #8a7e72; }
.cache-sub { font-size: 11.5px; color: var(--color-text-secondary); line-height: 1.6; margin: 0 0 12px; }
.inline-error { color: #d9534f; font-size: 12.5px; margin: 6px 0; }
.empty-hint { color: var(--color-text-secondary); font-size: 12.5px; padding: 14px 0; text-align: center; }

.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; margin-bottom: 14px; }
.metric-card { background: #faf6f0; border: 1px solid var(--color-border); border-radius: 10px; padding: 14px 16px; display: flex; flex-direction: column; gap: 4px; }
.metric-num { font-size: 22px; font-weight: 700; }
.metric-num.good { color: #3d8b52; }
.metric-label { font-size: 11px; color: var(--color-text-secondary); line-height: 1.5; }

.cache-block-title { font-size: 12px; font-weight: 700; margin: 14px 0 8px; }
.obs-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.obs-table th, .obs-table td { padding: 7px 8px; text-align: left; border-bottom: 1px solid var(--color-border); white-space: nowrap; }
.obs-table th { color: var(--color-text-secondary); font-weight: 600; }
.hit-tag { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 8px; }
.hit-tag.hit { background: #e4f4e8; color: #3d8b52; }
.hit-tag.miss { background: #fdf0d9; color: #b8802a; }

.trend-chart { display: flex; align-items: flex-end; gap: 8px; height: 130px; padding-top: 8px; overflow-x: auto; }
.trend-col { display: flex; flex-direction: column; align-items: center; gap: 4px; flex: 1; min-width: 34px; height: 100%; justify-content: flex-end; }
.trend-bar-wrap { width: 100%; height: 84px; display: flex; align-items: flex-end; justify-content: center; }
.trend-bar { width: 60%; max-width: 26px; border-radius: 4px 4px 0 0; transition: height 0.3s; }
.cache-bar { background: linear-gradient(180deg, #7fc98f, #4a9d5f); }
.trend-label { font-size: 10px; color: var(--color-text-secondary); }
.trend-rate { font-size: 10px; font-weight: 700; color: #3d8b52; }

.cache-entries .entry-row { cursor: pointer; }
.cache-entries .entry-row:hover { background: var(--color-primary-light); }
.entry-q { max-width: 420px; overflow: hidden; text-overflow: ellipsis; }
.entry-caret { display: inline-block; color: var(--color-primary); font-size: 11px; margin-right: 5px; transition: transform 0.15s ease; }
.entry-caret.open { transform: rotate(90deg); }
.entry-state { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 8px; }
.entry-state.active { background: #e4f4e8; color: #3d8b52; }
.entry-state.expired { background: #eee9e2; color: #8a7e72; }
.entry-drill > td { background: #fbf7ef; padding: 8px 12px; white-space: normal; }
.entry-answer { margin: 0; font-size: 11.5px; line-height: 1.7; color: var(--color-text); word-break: break-all; }

.btn { padding: 8px 16px; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer; border: 1px solid transparent; transition: all 0.15s; white-space: nowrap; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn.sm { padding: 5px 12px; font-size: 12px; }
.btn-ghost { background: transparent; border-color: var(--color-border); color: var(--color-text); }
.btn-ghost:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
</style>