<template>
  <div class="ctx-badge-wrap">
    <!-- 缩略态：迷你环形水位 + 百分比，点击展开明细弹层 -->
    <button
      class="ctx-badge"
      :class="levelClass"
      :title="badgeTitle"
      @click.stop="toggle"
    >
      <svg viewBox="0 0 36 36" class="ctx-ring" aria-hidden="true">
        <circle cx="18" cy="18" r="14" fill="none" stroke="currentColor" stroke-opacity="0.16" stroke-width="5" />
        <circle
          cx="18" cy="18" r="14" fill="none" stroke="currentColor" stroke-width="5"
          stroke-linecap="round" :stroke-dasharray="dashArray" transform="rotate(-90 18 18)"
        />
      </svg>
      <span class="ctx-pct">{{ pctText }}</span>
    </button>

    <!-- 明细弹层：总占比 + 分段堆叠条 + 各段占窗口比例 -->
    <div v-if="open" class="ctx-pop" @click.stop>
      <div class="ctx-pop-head">
        <span>上下文用量</span>
        <button class="ctx-pop-close" title="关闭" @click="open = false">×</button>
      </div>
      <template v-if="usage.has_data">
        <div class="ctx-pop-main">
          <strong class="ctx-pop-pct">{{ pctText }}</strong>
          <span class="ctx-pop-used">已使用 {{ fmtK(usage.total_tokens) }} / {{ fmtK(usage.context_window) }}</span>
        </div>
        <div class="ctx-pop-bar">
          <i
            v-for="seg in usage.segments"
            :key="seg.segment"
            :style="{ width: barWidth(seg), background: segColor(seg.segment) }"
          />
        </div>
        <div class="ctx-pop-legend">
          <div v-for="seg in usage.segments" :key="seg.segment" class="ctx-legend-row">
            <i class="ctx-dot" :style="{ background: segColor(seg.segment) }" />
            <span class="ctx-legend-label">{{ seg.label }}</span>
            <span class="ctx-legend-pct">{{ windowPct(seg) }}</span>
          </div>
        </div>
        <div class="ctx-pop-foot">{{ Math.round(usage.context_window / 1000) }}K 上下文 · 默认窗口</div>
      </template>
      <p v-else class="ctx-pop-empty">暂无上下文记录<br>发起一次问答后即可看到各段占用占比</p>
    </div>
  </div>
</template>

<script setup>
// 对话窗口上下文用量缩略图：默认浓缩为输入框旁的环形水位徽章，
// 点击展开明细弹层（总占比 / 分段堆叠条 / 各语义段占窗口比例）。
// 数据源：GET /chat/sessions/{id}/context-usage（最近一次真实请求的 segment 构成，
// 与管理员/个人面板同口径：按最终生成调用真实 prompt_tokens 分摊）。
import { ref, computed, watch, onBeforeUnmount } from 'vue'
import { getSessionContextUsage } from '@/api'

const props = defineProps({
  sessionId: { type: String, default: '' },
})

// 语义段配色（与弹层堆叠条/图例圆点共用）
const SEG_COLORS = {
  system: '#6366f1',
  memory: '#8b5cf6',
  memory_profile: '#a78bfa',
  memory_relevant: '#c4b5fd',
  skills: '#f472b6',
  history: '#f59e0b',
  summary: '#fbbf24',
  knowledge: '#10b981',
  search: '#ec4899',
  tool: '#14b8a6',
}

const EMPTY = { has_data: false, segments: [], total_tokens: 0, context_window: 131072, used_pct: 0 }

const usage = ref({ ...EMPTY })
const open = ref(false)

const CIRC = 2 * Math.PI * 14

const pctText = computed(() => (usage.value.has_data ? `${usage.value.used_pct}%` : '--'))
const dashArray = computed(() => {
  const ratio = Math.max(0, Math.min(1, (usage.value.used_pct || 0) / 100))
  return `${CIRC * ratio} ${CIRC}`
})
// 水位分级配色：<60% 常态 / 60-85% 偏紧 / ≥85% 告急
const levelClass = computed(() => {
  if (!usage.value.has_data) return 'idle'
  if (usage.value.used_pct >= 85) return 'danger'
  if (usage.value.used_pct >= 60) return 'warn'
  return 'ok'
})
const badgeTitle = computed(() =>
  usage.value.has_data ? `上下文用量 ${usage.value.used_pct}%（点击看明细）` : '上下文用量（暂无记录）',
)

function segColor(segment) {
  return SEG_COLORS[segment] || '#9ca3af'
}
function fmtK(t) {
  return t >= 1000 ? `${(t / 1000).toFixed(1)}K` : `${t}`
}
function windowPct(seg) {
  const w = usage.value.context_window || 1
  return `${((seg.tokens / w) * 100).toFixed(1)}%`
}
function barWidth(seg) {
  const w = usage.value.context_window || 1
  return `${Math.min(100, (seg.tokens / w) * 100)}%`
}

async function load() {
  if (!props.sessionId) {
    usage.value = { ...EMPTY }
    return
  }
  try {
    const res = await getSessionContextUsage(props.sessionId)
    usage.value = { ...EMPTY, ...(res.data || {}) }
  } catch (e) {
    // 用量展示属辅助信息：拉取失败保持空概览，不打断输入区
    usage.value = { ...EMPTY }
  }
}

function toggle() {
  open.value = !open.value
}
function onDocClick() {
  open.value = false
}

watch(() => props.sessionId, () => {
  open.value = false
  load()
}, { immediate: true })
watch(open, (v) => {
  if (v) document.addEventListener('click', onDocClick)
  else document.removeEventListener('click', onDocClick)
})
onBeforeUnmount(() => document.removeEventListener('click', onDocClick))

defineExpose({ refresh: load })
</script>

<style scoped>
.ctx-badge-wrap {
  position: relative;
  display: inline-flex;
}
.ctx-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border: 1px solid var(--color-border);
  background: #fff;
  border-radius: 999px;
  padding: 3px 10px 3px 5px;
  cursor: pointer;
  color: var(--color-primary);
  transition: box-shadow 0.15s, border-color 0.15s;
}
.ctx-badge:hover {
  border-color: var(--color-primary);
  box-shadow: 0 2px 8px rgba(201, 138, 75, 0.15);
}
.ctx-badge.warn { color: #b4620a; }
.ctx-badge.danger { color: #c0392b; }
.ctx-badge.idle { color: var(--color-text-secondary); }
.ctx-ring { width: 18px; height: 18px; display: block; }
.ctx-pct {
  font-size: 12px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.ctx-pop {
  position: absolute;
  bottom: calc(100% + 8px);
  right: 0;
  width: 300px;
  background: #fff;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  box-shadow: 0 8px 28px rgba(45, 42, 36, 0.12);
  padding: 14px 16px 12px;
  z-index: 40;
}
.ctx-pop-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: 10px;
}
.ctx-pop-close {
  border: none;
  background: none;
  cursor: pointer;
  color: var(--color-text-secondary);
  font-size: 16px;
  line-height: 1;
  padding: 2px 6px;
  border-radius: 4px;
}
.ctx-pop-close:hover {
  background: var(--color-primary-light);
  color: var(--color-primary);
}
.ctx-pop-main {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 8px;
}
.ctx-pop-pct {
  font-size: 24px;
  font-weight: 700;
  letter-spacing: -0.01em;
}
.ctx-pop-used {
  font-size: 12px;
  color: var(--color-text-secondary);
}
.ctx-pop-bar {
  display: flex;
  height: 8px;
  border-radius: 4px;
  background: #ede6dc;
  overflow: hidden;
  margin-bottom: 12px;
}
.ctx-pop-bar i { display: block; height: 100%; }
.ctx-pop-legend {
  display: flex;
  flex-direction: column;
  gap: 7px;
  margin-bottom: 12px;
}
.ctx-legend-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
}
.ctx-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex: none;
}
.ctx-legend-label {
  color: var(--color-text);
  flex: 1;
}
.ctx-legend-pct {
  color: var(--color-text-secondary);
  font-variant-numeric: tabular-nums;
}
.ctx-pop-foot {
  border-top: 1px dashed var(--color-border);
  padding-top: 9px;
  text-align: center;
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text);
}
.ctx-pop-empty {
  font-size: 12.5px;
  color: var(--color-text-secondary);
  text-align: center;
  line-height: 1.7;
  margin: 4px 0 8px;
}
</style>
