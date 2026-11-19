<template>
  <section class="process-panel" :class="{ collapsed, live }">
    <button type="button" class="process-header" :aria-expanded="!collapsed" @click="$emit('toggle')">
      <svg class="process-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="14" height="14">
        <circle cx="12" cy="12" r="3"/>
        <path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M19.1 4.9L17 7M7 17l-2.1 2.1"/>
      </svg>
      <span class="process-title">处理过程</span>
      <span class="process-summary">{{ summaryText }}</span>
      <span v-if="live && !collapsed" class="process-live-dot" aria-hidden="true"></span>
      <svg class="process-chevron" :class="{ open: !collapsed }" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13">
        <polyline points="6 9 12 15 18 9"/>
      </svg>
    </button>
    <div v-show="!collapsed" class="process-body">
      <p v-if="statusText" class="process-thought" role="status">{{ statusText }}</p>
      <ol v-if="steps.length && !tools.length" class="process-plan-list">
        <li v-for="step in steps.filter(s => s.state !== 'pending')" :key="step.key">{{ step.label }} · {{ step.state === 'done' ? '已完成' : '处理中' }}</li>
      </ol>
      <div v-if="plan.length" class="process-block">
        <div class="process-block-title">解题计划</div>
        <ol class="process-plan-list">
          <li v-for="(s, i) in plan" :key="i">
            <span class="process-tag">{{ s.tool }}</span>
            <span class="process-plan-text">{{ s.purpose || s.query }}</span>
          </li>
        </ol>
      </div>

      <div v-if="tools.length" class="process-block">
        <div class="process-block-title">工具调用</div>
        <div v-for="(t, i) in tools" :key="i" class="process-tool-row" :class="{ failed: t.failed }">
          <span class="process-tag" :title="t.tool">{{ t.tool?.startsWith('pt_') ? '个人工具' : t.tool }}</span>
          <span class="process-tool-query">{{ t.query || t.purpose }}</span>
          <span class="process-tool-state">
            <span v-if="t.state === 'start'" class="process-spinner"></span>
            <template v-else>{{ t.summary || (t.failed ? '失败' : '完成') }}</template>
          </span>
          <small v-if="t.execution_status" class="process-result">执行：{{ statusLabel(t.execution_status) }} · 数据：{{ statusLabel(t.validation_status) }} · 需求：{{ statusLabel(t.requirement_status) }}<template v-if="Number.isFinite(t.elapsed_ms)"> · {{ (t.elapsed_ms / 1000).toFixed(1) }} 秒</template></small>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  plan: { type: Array, default: () => [] },
  thoughts: { type: Array, default: () => [] },
  tools: { type: Array, default: () => [] },
  collapsed: { type: Boolean, default: false },
  live: { type: Boolean, default: false },
  steps: { type: Array, default: () => [] },
  statusText: { type: String, default: '' },
})
defineEmits(['toggle'])
const statusLabel = value => ({ completed: '结束', passed: '通过', satisfied: '已核验', unverified: '未验证', unmet: '未满足', failed: '失败', blocked: '被阻止', cancelled: '已取消', cancellation_requested: '请求取消中', timeout: '超时' }[value] || '未知')

const summaryText = computed(() => {
  const parts = []
  if (props.plan.length) parts.push(`${props.plan.length} 步计划`)
  if (props.tools.length) {
    const failed = props.tools.filter((t) => t.failed).length
    parts.push(`${props.tools.length} 次工具调用${failed ? `（${failed} 失败）` : ''}`)
  }
  if (props.steps.length && !props.tools.length) parts.push(`${props.steps.filter(s => s.state === 'done').length} 个阶段已完成`)
  return parts.join(' · ') || (props.live ? '处理中' : '查看执行摘要')
})
</script>

<style scoped>
/* 灰色过程区：与暖色正文区隔，答案呈现后默认收缩，可随时展开回看 */
.process-panel {
  margin: 6px 0 14px;
  border: 1px solid #e2e2de;
  border-radius: 10px;
  background: #f6f6f4;
  font-size: 12.5px;
  color: #6f6f68;
  overflow: hidden;
}
.process-header {
  display: flex;
  align-items: center;
  gap: 7px;
  width: 100%;
  padding: 8px 12px;
  border: none;
  background: transparent;
  color: #7a7a72;
  font-size: 12px;
  cursor: pointer;
  text-align: left;
}
.process-header:hover { background: #efefec; }
.process-icon { flex-shrink: 0; color: #9a9a90; }
.process-title { font-weight: 600; letter-spacing: 0.3px; }
.process-summary {
  color: #9a9a90;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.process-live-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #a3a398;
  animation: process-pulse 1.2s ease-in-out infinite;
  flex-shrink: 0;
}
@keyframes process-pulse {
  0%, 100% { opacity: 0.35; }
  50% { opacity: 1; }
}
.process-chevron {
  margin-left: auto;
  flex-shrink: 0;
  color: #9a9a90;
  transition: transform 0.2s ease;
  transform: rotate(-90deg);
}
.process-chevron.open { transform: rotate(0deg); }
.process-body {
  padding: 2px 12px 10px;
  border-top: 1px solid #eaeae6;
}
.process-block + .process-block {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed #e4e4e0;
}
.process-block-title {
  font-size: 11px;
  font-weight: 600;
  color: #8f8f86;
  margin-bottom: 4px;
  letter-spacing: 0.5px;
}
.process-plan-list { margin: 0; padding-left: 18px; }
.process-plan-list li { margin: 2px 0; line-height: 1.6; }
.process-plan-text { margin-left: 6px; }
.process-thought { margin: 2px 0; line-height: 1.6; color: #7d7d74; }
.process-tag {
  display: inline-block;
  padding: 1px 7px;
  font-size: 10.5px;
  color: #fff;
  background: #a3a398;
  border-radius: 999px;
}
.process-result { flex-basis: 100%; color: #716d65; white-space: normal; overflow-wrap: anywhere; }
.process-tool-row {
  flex-wrap: wrap;
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 3px 0;
}
.process-tool-row.failed .process-tag { background: #b08a8a; }
.process-tool-row.failed .process-tool-state { color: #a06a6a; }
.process-tool-query {
  color: #8a8a80;
  max-width: 45%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.process-tool-state {
  margin-left: auto;
  color: #9a9a90;
  display: flex;
  align-items: center;
  gap: 4px;
  max-width: 46%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.process-spinner {
  width: 8px;
  height: 8px;
  border: 1.5px solid #d8d8d2;
  border-top-color: #8f8f86;
  border-radius: 50%;
  animation: process-spin 0.8s linear infinite;
}
@keyframes process-spin { to { transform: rotate(360deg); } }

@media (max-width: 768px) {
  .process-panel { margin-left: 0; }
  .process-tool-query { max-width: 34%; }
}
</style>