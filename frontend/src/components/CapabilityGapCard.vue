<template>
  <section class="gap-card" aria-live="polite">
    <div class="gap-head">
      <span class="gap-badge">能力缺口 · 需要你的选择</span>
      <span class="gap-scenario">{{ gap.scenario_label || gap.scenario }}</span>
    </div>
    <p class="gap-message">{{ gap.message || '当前系统没有合适的工具解决这个问题，可选择以下命令行方案执行：' }}</p>
    <p v-if="gap.query" class="gap-query" :title="gap.query">问题：{{ gap.query }}</p>

    <!-- 方案列表（human-in-the-loop：用户选择想怎么实现；未解决时展示 Agent 判定的下一步） -->
    <div v-if="isNextRound" class="next-round-hint">
      执行结果尚未完全解决你的问题，Agent 判定需要继续下一步，请选择：
    </div>
    <div v-if="activeSolutions.length" class="gap-solutions">
      <div v-for="sol in activeSolutions" :key="sol.id" class="gap-solution"
           :class="{ chosen: chosenId === sol.id }">
        <div class="sol-main">
          <span class="sol-label">{{ sol.label }}</span>
          <span class="sol-risk" :class="'risk-' + sol.risk_level">风险：{{ riskLabel(sol.risk_level) }}</span>
        </div>
        <p v-if="sol.note" class="sol-note">{{ sol.note }}</p>
        <code class="sol-argv">{{ (sol.argv || []).join(' ') }}</code>
        <!-- 需要 URL 的方案：让用户提供访问目标 -->
        <div v-if="needUrl(sol)" class="sol-url-row">
          <input v-model="urlInput" class="sol-url" type="text" placeholder="输入要访问的公开网页/接口 URL"
                 :disabled="busy" maxlength="500" />
        </div>
        <div class="sol-actions">
          <button class="gap-btn primary" :disabled="busy || (needUrl(sol) && !validUrl)"
                  @click="choose(sol)">
            {{ busy && chosenId === sol.id ? '执行中…' : '选择并执行' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 风险二次确认（high/critical：停下来让用户确认后才在沙箱执行） -->
    <div v-if="pendingConfirm" class="gap-confirm">
      <p class="confirm-text">
        ⚠ 该方案风险等级为「{{ riskLabel(pendingConfirm.risk_level) }}」，将在沙箱中执行：
        <code>{{ (pendingConfirm.argv || []).join(' ') }}</code>
      </p>
      <div class="sol-actions">
        <button class="gap-btn danger" :disabled="busy" @click="run(pendingConfirm, true)">确认执行</button>
        <button class="gap-btn" :disabled="busy" @click="pendingConfirm = null">取消</button>
      </div>
    </div>

    <!-- 执行结果（沙箱 stdout / 错误） -->
    <div v-if="result" class="gap-result" :class="{ failed: !result.success }">
      <div class="result-head">
        <span class="result-badge" :class="result.success ? 'ok' : 'err'">
          {{ result.success ? '沙箱执行成功' : '执行未成功' }}
        </span>
        <span class="result-meta">exit={{ result.exit_code }} · sandbox</span>
      </div>
      <pre v-if="result.stdout" class="result-text">{{ result.stdout }}</pre>
      <pre v-if="result.stderr" class="result-text">{{ result.stderr }}</pre>
      <p v-if="result.error" class="result-error">{{ result.error }}</p>
    </div>

    <!-- Agent 闭环：已解决则展示回喂 LLM 后的最终答复 -->
    <div v-if="result && result.resolved && result.followup_answer" class="gap-followup">
      <div class="followup-head">
        <span class="followup-badge">Agent 已解决你的问题</span>
      </div>
      <p class="followup-text">{{ result.followup_answer }}</p>
    </div>
    <!-- 未解决且无候选下一步：展示 Agent 的文字建议 -->
    <p v-else-if="result && !result.resolved && result.next_hint && !hasNextSolutions" class="next-hint">
      {{ result.next_hint }}
    </p>
    <p v-if="errorMsg" class="result-error">{{ errorMsg }}</p>
  </section>
</template>

<script setup>
import { ref, computed } from 'vue'
import { executeCapabilityGap } from '@/api'

const props = defineProps({
  gap: { type: Object, required: true },
})

const busy = ref(false)
const chosenId = ref('')
const urlInput = ref('')
const pendingConfirm = ref(null)
const result = ref(null)
const errorMsg = ref('')

const validUrl = computed(() => /^https?:\/\/\S+$/i.test(urlInput.value.trim()))

// Claude Code 式闭环：执行后回喂 LLM 判定；未解决则用 next_solutions 开启下一轮 HITL
const hasNextSolutions = computed(() =>
  !!(result.value && result.value.next_solutions && result.value.next_solutions.length))
const isNextRound = computed(() => hasNextSolutions.value && !result.value.resolved)
const activeSolutions = computed(() => {
  const r = result.value
  if (r && r.resolved) return []
  if (hasNextSolutions.value) return r.next_solutions
  return props.gap.solutions || []
})

function riskLabel(risk) {
  return { low: '低', medium: '中', high: '高', critical: '极高' }[risk] || risk || '-'
}

function needUrl(sol) {
  return (sol.argv || []).some((a) => String(a).includes('{url}')) || /需提供 URL/.test(sol.label || '')
}

/** HITL 第一步：用户选定方案；high/critical 风险先弹确认，其余直接沙箱执行 */
function choose(sol) {
  errorMsg.value = ''
  result.value = null
  urlInput.value = ''
  chosenId.value = sol.id
  pendingConfirm.value = null
  if (sol.risk_level === 'high' || sol.risk_level === 'critical') {
    pendingConfirm.value = sol
    return
  }
  run(sol, false)
}

/** HITL 第二步：调用后端在沙箱（CliRunner）中执行选定方案 */
async function run(sol, confirmed) {
  busy.value = true
  errorMsg.value = ''
  try {
    const res = await executeCapabilityGap({
      gap_id: props.gap.gap_id,
      solution_id: sol.id,
      confirmed: !!confirmed,
      url: urlInput.value.trim(),
    })
    const data = res.data || {}
    if (data.needs_confirmation) {
      // 后端要求二次确认（风险操作停下来等用户）
      pendingConfirm.value = sol
      result.value = null
    } else {
      pendingConfirm.value = null
      result.value = data
    }
  } catch (err) {
    errorMsg.value = err.response?.data?.detail || err.message || '方案执行失败'
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.gap-card {
  margin: 8px 0 20px 44px;
  border: 1px solid #eed9bb;
  border-radius: 10px;
  background: #fffdf9;
  padding: 10px 12px;
}

.gap-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  flex-wrap: wrap;
}

.gap-badge {
  font-size: 10.5px;
  font-weight: 600;
  color: #fff;
  background: #c98a4b;
  border-radius: 999px;
  padding: 2px 8px;
}

.gap-scenario {
  font-size: 11.5px;
  color: #8a6d4d;
  background: #f7ecd9;
  border-radius: 999px;
  padding: 2px 8px;
}

.gap-message {
  font-size: 13px;
  color: #4a3f35;
  margin: 0 0 4px;
  line-height: 1.6;
}

.gap-query {
  font-size: 12px;
  color: #8a7a68;
  margin: 0 0 8px;
}

.gap-solutions {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.next-round-hint {
  font-size: 12.5px;
  color: #7c5a2e;
  background: #f7ecd9;
  border-radius: 8px;
  padding: 7px 10px;
  margin-bottom: 8px;
  line-height: 1.6;
}

.gap-followup {
  margin-top: 8px;
  border: 1px solid #cfdcc6;
  border-radius: 8px;
  background: #f4f8ef;
  padding: 8px 10px;
}

.followup-head { display: flex; align-items: center; gap: 8px; }

.followup-badge {
  font-size: 10.5px;
  font-weight: 600;
  color: #fff;
  background: #6a8f5b;
  border-radius: 999px;
  padding: 2px 8px;
}

.followup-text {
  margin: 6px 0 0;
  font-size: 13px;
  color: #40483c;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}

.next-hint {
  margin: 8px 0 0;
  font-size: 12.5px;
  color: #8a6d4d;
  line-height: 1.6;
}

.gap-solution {
  border: 1px solid #e7d7bd;
  border-radius: 8px;
  background: #fff;
  padding: 8px 10px;
}

.gap-solution.chosen {
  border-color: #c98a4b;
  background: #fdf6ea;
}

.sol-main {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  align-items: baseline;
}

.sol-label {
  font-size: 12.5px;
  color: #4a3f35;
  font-weight: 600;
}

.sol-risk {
  font-size: 10.5px;
  border-radius: 999px;
  padding: 1px 7px;
  white-space: nowrap;
}

.sol-risk.risk-low { color: #4c7a4c; background: #e9f2e6; }
.sol-risk.risk-medium { color: #8a6d1f; background: #f7eed3; }
.sol-risk.risk-high { color: #a34d2e; background: #f8e3d7; }
.sol-risk.risk-critical { color: #fff; background: #b04a3a; }

.sol-note {
  font-size: 11.5px;
  color: #8a7a68;
  margin: 4px 0 0;
}

.sol-argv {
  display: block;
  margin-top: 5px;
  font-size: 11px;
  color: #6b5a48;
  background: #f7f1e6;
  border-radius: 6px;
  padding: 4px 8px;
  word-break: break-all;
}

.sol-url-row {
  margin-top: 6px;
}

.sol-url {
  width: 100%;
  box-sizing: border-box;
  font-size: 12px;
  border: 1px solid #d8c3a5;
  border-radius: 6px;
  padding: 5px 8px;
  background: #fffdf9;
  color: #4a3f35;
}

.sol-actions {
  margin-top: 7px;
  display: flex;
  gap: 8px;
}

.gap-btn {
  padding: 5px 13px;
  font-size: 12px;
  color: #6b5a48;
  background: #fff;
  border: 1px solid #d8c3a5;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease;
}

.gap-btn:hover:not(:disabled) {
  background: #fbf3e8;
  border-color: #c98a4b;
}

.gap-btn.primary {
  color: #fff;
  background: #c98a4b;
  border-color: #c98a4b;
}

.gap-btn.primary:hover:not(:disabled) { background: #b8783c; }

.gap-btn.danger {
  color: #fff;
  background: #b04a3a;
  border-color: #b04a3a;
}

.gap-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.gap-confirm {
  margin-top: 8px;
  border: 1px solid #e3b8a6;
  border-radius: 8px;
  background: #fbeee7;
  padding: 8px 10px;
}

.confirm-text {
  font-size: 12.5px;
  color: #7c4433;
  margin: 0 0 6px;
  line-height: 1.6;
}

.confirm-text code {
  background: #f7e6dc;
  border-radius: 4px;
  padding: 1px 5px;
  word-break: break-all;
}

.gap-result {
  margin-top: 8px;
  border: 1px solid #cfdcc6;
  border-radius: 8px;
  background: #f4f8ef;
  padding: 8px 10px;
}

.gap-result.failed {
  border-color: #e3b8a6;
  background: #fdf1ec;
}

.result-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.result-badge {
  font-size: 10.5px;
  font-weight: 600;
  border-radius: 999px;
  padding: 2px 8px;
}

.result-badge.ok { color: #fff; background: #6a8f5b; }
.result-badge.err { color: #fff; background: #b04a3a; }

.result-meta {
  font-size: 11px;
  color: #8a7a68;
}

.result-text {
  margin: 6px 0 0;
  font-size: 11.5px;
  color: #40483c;
  background: #fff;
  border: 1px solid #dde5d4;
  border-radius: 6px;
  padding: 6px 8px;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 220px;
  overflow-y: auto;
}

.result-error {
  margin: 6px 0 0;
  font-size: 12px;
  color: #a34d2e;
}
</style>
