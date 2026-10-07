<template>
  <div class="trace-flow">
    <template v-for="(step, idx) in steps" :key="idx">
      <div class="trace-node" :class="`st-${step.status}`" @click="toggle(idx)">
        <div class="trace-dot">
          <span v-if="step.status === 'ok'">✓</span>
          <span v-else-if="step.status === 'error' || step.status === 'fail'">✕</span>
          <span v-else-if="step.status === 'warn'">!</span>
          <span v-else>{{ step.seq }}</span>
        </div>
        <div class="trace-meta">
          <span class="trace-phase">{{ phaseLabel(step.phase) }}</span>
          <span class="trace-name">{{ step.name }}</span>
          <span class="trace-elapsed">{{ step.elapsed_ms }}ms</span>
        </div>
      </div>
      <span v-if="idx < steps.length - 1" class="trace-arrow" :class="`ar-${step.status}`">→</span>
    </template>
    <!-- 展开的明细 -->
    <div v-if="expanded != null && steps[expanded]" class="trace-detail">
      <div class="trace-detail-head">
        <strong>{{ phaseLabel(steps[expanded].phase) }}</strong> · {{ steps[expanded].name }}
        <span class="trace-detail-status" :class="`st-${steps[expanded].status}`">{{ statusLabel(steps[expanded].status) }}</span>
        <span class="trace-detail-time">{{ steps[expanded].elapsed_ms }}ms · {{ formatTime(steps[expanded].at) }}</span>
      </div>
      <pre v-if="steps[expanded].detail" class="trace-detail-body">{{ steps[expanded].detail }}</pre>
      <div v-else class="trace-detail-empty">无附加明细</div>
    </div>
    <div v-if="!steps.length" class="trace-empty">无链路数据</div>
  </div>
</template>

<script setup>
import { ref } from 'vue'

defineProps({ steps: { type: Array, default: () => [] } })

const expanded = ref(null)
function toggle(idx) { expanded.value = expanded.value === idx ? null : idx }

const PHASE_LABELS = {
  auth: '① 鉴权', quota: '② 配额', security: '③ 安全审计', route: '④ 选路',
  upstream: '⑤ 上游调用', response: '⑥ 响应处理', log: '⑦ 日志入库',
}
function phaseLabel(p) { return PHASE_LABELS[p] || p }
const STATUS_LABELS = { ok: '成功', error: '失败', fail: '失败', warn: '警告', skip: '跳过' }
function statusLabel(s) { return STATUS_LABELS[s] || s }
function formatTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`
}
</script>

<style scoped>
.trace-flow { display: flex; align-items: flex-start; gap: 4px; flex-wrap: wrap; padding: 8px 0; }
.trace-node {
  display: flex; flex-direction: column; align-items: center; gap: 6px;
  padding: 10px 8px; border-radius: 10px; cursor: pointer; border: 1px solid transparent;
  transition: all 0.15s; text-align: center; min-width: 82px;
}
.trace-node:hover { background: #faf6f0; }
.trace-dot {
  width: 30px; height: 30px; border-radius: 50%; display: flex; align-items: center; justify-content: center;
  font-size: 13px; font-weight: 700; border: 2px solid var(--color-border); background: #fff; color: var(--color-text-secondary);
}
.trace-meta { display: flex; flex-direction: column; gap: 1px; }
.trace-phase { font-size: 10.5px; color: var(--color-text-secondary); font-weight: 600; }
.trace-name { font-size: 11.5px; font-weight: 600; max-width: 90px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.trace-elapsed { font-size: 10px; color: var(--color-text-secondary); }
.trace-arrow { align-self: center; font-size: 16px; color: var(--color-border); padding-top: 12px; }

.st-ok .trace-dot { border-color: #4a9d5f; background: #e4f4e8; color: #3d8b52; }
.st-error .trace-dot, .st-fail .trace-dot { border-color: #d9534f; background: #fbe4e3; color: #c0392b; }
.st-warn .trace-dot { border-color: #e0a92e; background: #fdf0d9; color: #b8802a; }
.st-skip .trace-dot { border-color: #d8cfc2; background: #f2ede6; color: #a99c8c; }
.ar-error, .ar-fail { color: #d9534f; }
.ar-ok { color: #4a9d5f; }

.trace-detail { width: 100%; margin-top: 10px; border: 1px solid var(--color-border); border-radius: 10px; overflow: hidden; }
.trace-detail-head { display: flex; align-items: center; gap: 8px; padding: 8px 12px; background: #faf6f0; font-size: 12px; flex-wrap: wrap; }
.trace-detail-status { font-size: 10.5px; font-weight: 700; padding: 1px 8px; border-radius: 8px; background: #eee9e2; color: #8a7e72; }
.trace-detail-status.st-ok { background: #e4f4e8; color: #3d8b52; }
.trace-detail-status.st-error, .trace-detail-status.st-fail { background: #fbe4e3; color: #c0392b; }
.trace-detail-status.st-warn { background: #fdf0d9; color: #b8802a; }
.trace-detail-time { margin-left: auto; font-size: 11px; color: var(--color-text-secondary); }
.trace-detail-body {
  margin: 0; padding: 10px 12px; background: #2d2a24; color: #e8e0d4;
  font-family: 'SFMono-Regular', Consolas, monospace; font-size: 11.5px; line-height: 1.6;
  max-height: 200px; overflow: auto; white-space: pre-wrap; word-break: break-all;
}
.trace-detail-empty { padding: 10px 12px; font-size: 12px; color: var(--color-text-secondary); }
.trace-empty { width: 100%; text-align: center; color: var(--color-text-secondary); font-size: 12.5px; padding: 12px; }
</style>
