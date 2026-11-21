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
      <div v-if="intent && (intent.freshness || intent.scope)" class="process-intent">
        <span v-if="intent.freshness === 'dynamic'" class="process-chip realtime">实时</span>
        <span v-else-if="intent.freshness === 'static'" class="process-chip">静态知识</span>
        <span v-if="intent.scope" class="process-chip">{{ intent.scope === 'public' ? '公共' : '个人' }}</span>
        <span v-if="intent.query" class="process-intent-query" :title="intent.query">检索式：{{ intent.query }}</span>
      </div>
      <div v-if="error" class="process-error" role="alert">
        <div class="process-error-head">失败步骤：{{ stageLabel(error.stage) || '未知阶段' }}<span v-if="error.code" class="process-error-code">{{ error.code }}</span></div>
        <p v-if="error.message" class="process-error-msg">{{ error.message }}</p>
        <small v-if="error.retryable" class="process-error-retry">该错误可重试：请检查配置或稍后重新提问</small>
      </div>
      <ol v-if="v2Steps.length" class="process-stage-list">
        <li v-for="s in v2Steps" :key="s.step_id" :class="'stage-' + s.state">
          <span class="process-stage-name">{{ stageLabel(s.stage) }}</span>
          <span class="process-stage-state">{{ stageStateLabel(s.state) }}</span>
          <small v-if="Number.isFinite(s.elapsed_ms) && s.elapsed_ms > 0" class="process-stage-time">{{ (s.elapsed_ms / 1000).toFixed(1) }} 秒</small>
          <small v-if="s.state === 'skipped' && s.detail" class="process-stage-detail">{{ s.detail }}</small>
          <small v-else-if="s.error" class="process-stage-detail err">{{ s.error }}</small>
        </li>
      </ol>
      <ol v-else-if="steps.length && !tools.length" class="process-plan-list">
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
          <span class="process-tag" :title="t.tool">{{ t.tool }}</span>
          <em v-if="t.personal || t.tool?.startsWith('pt_')" class="process-personal">个人工具</em>
          <span class="process-tool-query">{{ t.query || t.purpose }}</span>
          <span class="process-tool-state">
            <span v-if="t.state === 'start'" class="process-spinner"></span>
            <template v-else>{{ t.summary || (t.failed ? '失败' : '完成') }}</template>
          </span>
          <small v-if="t.execution_status" class="process-result">执行：{{ statusLabel(t.execution_status) }} · 数据：{{ statusLabel(t.validation_status) }} · 需求：{{ statusLabel(t.requirement_status) }}<template v-if="Number.isFinite(t.elapsed_ms)"> · {{ (t.elapsed_ms / 1000).toFixed(1) }} 秒</template></small>
        </div>
      </div>
      <p v-if="!plan.length && !tools.length && !steps.length && !error" class="process-empty">本轮未记录执行过程（旧版本消息或无过程数据）。</p>
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
  // schema_version=2 快照字段：结构化失败归因/意图标签/终态
  error: { type: Object, default: null },
  intent: { type: Object, default: null },
  processStatus: { type: String, default: '' },
})
defineEmits(['toggle'])
const statusLabel = value => ({ completed: '结束', passed: '通过', satisfied: '已核验', unverified: '未验证', unmet: '未满足', failed: '失败', blocked: '被阻止', cancelled: '已取消', cancellation_requested: '请求取消中', timeout: '超时' }[value] || '未知')

// v2 快照步骤（带 stage 字段）与 live 流水线步骤（key/label）区分渲染
const v2Steps = computed(() => (props.steps || []).filter(s => s && s.stage !== undefined))
const STAGE_LABELS = {
  intent_rewrite: '意图识别', execution_decision: '执行决策', tool_routing: '来源路由',
  semantic_cache_probe: '语义缓存探针', knowledge_retrieve: '知识检索', rerank: '重排序',
  evidence_check: '证据校验', ocr_retrieval: '图片识别', tool_selection: '工具匹配',
  tool_model_selection: '决策模型', tool_model_fallback: '模型回退', skill_identity_check: 'Skill 依赖核验',
  direct_tool_plan: '直调计划', direct_answer: '直调答案', answer: '最终回复',
  answer_generation: '回答生成', web_search: '联网搜索', search_knowledge: '知识检索',
  chat_agent: 'Agent 分流', skill_distill_route: 'Skill 沉淀路由', plan: '规划',
  model_selection: '模型选择', tool_execution: '工具执行', agent: 'Agent 轮次',
}
const stageLabel = name => {
  if (!name) return ''
  if (STAGE_LABELS[name]) return STAGE_LABELS[name]
  if (String(name).startsWith('pt_')) return '个人工具'
  if (String(name).startsWith('decision')) return '工具决策'
  return name
}
const stageStateLabel = state => ({ ok: '完成', skipped: '已跳过', error: '失败', blocked: '被阻止', running: '处理中', cancelled: '已取消', timeout: '超时' }[state] || state || '完成')

const summaryText = computed(() => {
  const parts = []
  if (props.error) parts.push(`失败：${stageLabel(props.error.stage) || props.error.code || '未知步骤'}`)
  if (props.plan.length) parts.push(`${props.plan.length} 步计划`)
  if (props.tools.length) {
    const failed = props.tools.filter((t) => t.failed).length
    parts.push(`${props.tools.length} 次工具调用${failed ? `（${failed} 失败）` : ''}`)
  }
  if (v2Steps.value.length) {
    const skipped = v2Steps.value.filter(s => s.state === 'skipped').length
    parts.push(`${v2Steps.value.length} 个阶段${skipped ? `（${skipped} 跳过）` : ''}`)
  } else if (props.steps.length && !props.tools.length) {
    parts.push(`${props.steps.filter(s => s.state === 'done').length} 个阶段已完成`)
  }
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
.process-personal { font-style: normal; font-size: 10.5px; padding: 1px 7px; border-radius: 999px; background: #e3ecdd; color: #3f6230; flex: none; }
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

/* v2 过程快照：意图标签/结构化错误/阶段清单/空态 */
.process-intent { display: flex; align-items: center; gap: 6px; margin: 4px 0; flex-wrap: wrap; }
.process-chip {
  padding: 1px 8px; font-size: 10.5px; border-radius: 999px;
  background: #e8e8e2; color: #7a7a70;
}
.process-chip.realtime { background: #dcead9; color: #4c7a45; font-weight: 600; }
.process-intent-query { color: #9a9a90; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 60%; }
.process-error {
  margin: 6px 0; padding: 7px 10px; border-radius: 8px;
  background: #f3e6e4; border: 1px solid #e3cdc9; color: #8a5a52;
}
.process-error-head { font-weight: 600; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.process-error-code {
  font-family: ui-monospace, monospace; font-size: 10.5px; font-weight: 400;
  padding: 0 6px; border-radius: 999px; background: #e7d3cf; color: #7c4f47;
}
.process-error-msg { margin: 4px 0 0; line-height: 1.6; overflow-wrap: anywhere; }
.process-error-retry { display: block; margin-top: 3px; color: #a08078; }
.process-stage-list { margin: 4px 0; padding-left: 18px; }
.process-stage-list li { margin: 2px 0; line-height: 1.7; display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap; }
.process-stage-name { font-weight: 600; color: #77776e; }
.process-stage-state { color: #9a9a90; }
.process-stage-list li.stage-error .process-stage-state { color: #a06a6a; font-weight: 600; }
.process-stage-list li.stage-skipped .process-stage-state { color: #a8a89c; }
.process-stage-time { color: #b0b0a6; }
.process-stage-detail { flex-basis: 100%; color: #9a9a90; overflow-wrap: anywhere; }
.process-stage-detail.err { color: #a06a6a; }
.process-empty { margin: 6px 0 2px; color: #a8a89c; }

@media (max-width: 768px) {
  .process-panel { margin-left: 0; }
  .process-tool-query { max-width: 34%; }
}
</style>