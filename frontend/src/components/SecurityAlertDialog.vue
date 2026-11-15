<template>
  <!-- 全局安全风险弹窗：任意页面轮询未确认且达阈值的风险事件（仅 admin/ops） -->
  <div v-if="auth.canOps && auth.isAuthenticated && alertQueue.length" class="sec-modal-mask">
    <div class="sec-modal" :class="`sev-${alertCurrent.risk_level}`">
      <div class="sec-modal-head">
        <h3 class="sec-modal-title">⚠️ 安全风险告警</h3>
        <span class="sec-alert-count">{{ alertIndex + 1 }} / {{ alertQueue.length }}</span>
      </div>
      <div class="sec-alert-top">
        <span class="risk-tag" :class="`risk-${alertCurrent.risk_level}`">
          {{ riskLabel(alertCurrent.risk_level) }} · {{ alertCurrent.risk_score }} 分
        </span>
        <span class="sec-alert-meta">
          {{ alertCurrent.finding_count }} 项命中 · {{ alertCurrent.role || '—' }} · {{ alertCurrent.model || '—' }}
        </span>
        <span class="sec-alert-time">{{ formatTime(alertCurrent.created_at) }}</span>
      </div>
      <div class="sec-alert-findings">
        <div v-for="(f, i) in alertCurrent.findings" :key="i" class="finding" :class="`sev-${f.severity}`">
          <div class="finding-head">
            <strong>{{ f.rule_name }}</strong>
            <span class="finding-cat">{{ f.category_label }}</span>
            <span class="finding-sev" :class="`txt-${f.severity}`">{{ sevLabel(f.severity) }}</span>
          </div>
          <div class="finding-match mono">{{ f.matched }}</div>
        </div>
      </div>
      <!-- 原因分析（风险研判）+ 优化措施 + 应对建议：记录事件时已后台自动生成 -->
      <div class="sec-suggest">
        <div class="sec-suggest-head">
          <strong>原因分析 · 优化措施 · 应对建议</strong>
          <button class="btn btn-ghost sm" :disabled="suggesting" @click="onSuggest">
            {{ suggesting ? '生成中…' : (alertCurrent.suggestion ? '重新生成' : '立即生成') }}
          </button>
        </div>
        <p v-if="alertCurrent.suggestion" class="sec-suggest-text">{{ alertCurrent.suggestion }}</p>
        <p v-else class="sec-suggest-empty">建议生成中（模型不可用时自动降级规则化建议），也可点「立即生成」。</p>
      </div>
      <div class="sec-modal-actions">
        <button class="btn btn-ghost" @click="onDismiss">确认</button>
        <button class="btn btn-primary" @click="onHandle">去处理</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import {
  listGatewaySecurityEvents, ackGatewaySecurityEvents, suggestGatewaySecurityEvent,
} from '@/api'

const auth = useAuthStore()
const router = useRouter()

const alertQueue = ref([])
const alertIndex = ref(0)
const alertCurrent = computed(() => alertQueue.value[alertIndex.value] || {})
const suggesting = ref(false)
let timer = null

const SEV_LABELS = { low: '低危', medium: '中危', high: '高危', critical: '严重' }
const RISK_LABELS = { none: '无风险', low: '低', medium: '中', high: '高', critical: '严重' }
function sevLabel(s) { return SEV_LABELS[s] || s }
function riskLabel(l) { return RISK_LABELS[l] || l }
function formatTime(iso) {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

// 轮询未确认告警：达到阈值即弹窗（确认后不再打扰；全局唯一弹窗口）
async function pollAlerts() {
  if (!auth.canOps || !auth.isAuthenticated) return
  if (alertQueue.value.length) return // 已有弹窗在处理，跳过本轮
  try {
    const res = await listGatewaySecurityEvents({ alert_only: true, limit: 20 })
    const list = res.data.events || []
    if (list.length) { alertQueue.value = list; alertIndex.value = 0 }
  } catch (e) {
    // 轮询失败静默
  }
}

async function ackCurrent() {
  const cur = alertCurrent.value
  if (cur?.id) { try { await ackGatewaySecurityEvents([cur.id]) } catch (e) { /* 忽略 */ } }
}

async function onDismiss() {
  await ackCurrent()
  if (alertIndex.value < alertQueue.value.length - 1) {
    alertIndex.value += 1
  } else {
    alertQueue.value = []; alertIndex.value = 0
  }
}

async function onHandle() {
  const cur = alertCurrent.value
  alertQueue.value = []; alertIndex.value = 0
  await ackCurrent()
  if (cur?.id) {
    router.push({ name: 'Gateway', query: { tab: 'security', event: cur.id } })
  }
}

async function onSuggest() {
  const cur = alertCurrent.value
  if (!cur?.id) return
  suggesting.value = true
  try {
    const res = await suggestGatewaySecurityEvent(cur.id)
    if (res.data.event) Object.assign(cur, res.data.event)
  } catch (e) {
    // 建议生成失败不阻断弹窗
  } finally {
    suggesting.value = false
  }
}

watch(() => auth.canOps, (v) => { if (v) pollAlerts() })

onMounted(() => {
  pollAlerts()
  timer = setInterval(pollAlerts, 15000)
})
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<style scoped>
.sec-modal-mask { position: fixed; inset: 0; background: rgba(45,42,36,0.4); display: flex; align-items: center; justify-content: center; z-index: 210; padding: 20px; }
.sec-modal { background: #fffdfb; border-radius: 14px; padding: 22px 24px; width: 100%; max-width: 580px; max-height: 86vh; overflow-y: auto; box-shadow: 0 8px 30px rgba(0,0,0,0.22); border-top: 4px solid #d9534f; }
.sec-modal.sev-medium { border-top-color: #e0a92e; }
.sec-modal.sev-high { border-top-color: #d97b3f; }
.sec-modal.sev-low { border-top-color: #6a9fd8; }
.sec-modal-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
.sec-modal-title { font-size: 16px; font-weight: 700; margin: 0; }
.sec-alert-count { font-size: 11.5px; color: var(--color-text-secondary); }
.sec-alert-top { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
.sec-alert-meta { font-size: 12px; color: var(--color-text-secondary); }
.sec-alert-time { font-size: 11px; color: var(--color-text-secondary); margin-left: auto; }
.sec-alert-findings { display: flex; flex-direction: column; gap: 8px; }
.finding { border-left: 3px solid var(--color-border); padding: 6px 10px; background: #faf6f0; border-radius: 0 6px 6px 0; }
.finding.sev-low { border-left-color: #6a9fd8; }
.finding.sev-medium { border-left-color: #e0a92e; }
.finding.sev-high { border-left-color: #d97b3f; }
.finding.sev-critical { border-left-color: #d9534f; }
.finding-head { display: flex; align-items: center; gap: 8px; font-size: 12px; flex-wrap: wrap; }
.finding-cat { font-size: 10.5px; color: var(--color-text-secondary); background: #f4efe8; padding: 1px 7px; border-radius: 7px; }
.finding-sev { font-size: 10.5px; font-weight: 700; margin-left: auto; }
.finding-match { margin-top: 5px; font-size: 11.5px; color: #a83226; word-break: break-all; }
.mono { font-family: 'SFMono-Regular', Consolas, monospace; }
.txt-low { color: #3a6ea5; }
.txt-medium { color: #b8802a; }
.txt-high { color: #c1622a; }
.txt-critical { color: #c0392b; }
.risk-tag { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 8px; }
.risk-none { background: #eee9e2; color: #8a7e72; }
.risk-low { background: #e8f0fb; color: #3a6ea5; }
.risk-medium { background: #fdf0d9; color: #b8802a; }
.risk-high { background: #fbe7d9; color: #c1622a; }
.risk-critical { background: #fbe4e3; color: #c0392b; }

.sec-suggest { background: #f7f2ea; border-radius: 8px; padding: 10px 12px; margin-top: 12px; }
.sec-suggest-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 6px; }
.sec-suggest-head strong { font-size: 12.5px; }
.sec-suggest-text { font-size: 12px; line-height: 1.7; color: var(--color-text); white-space: pre-wrap; margin: 0; }
.sec-suggest-empty { font-size: 11.5px; color: var(--color-text-secondary); margin: 0; }
.sec-modal-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 18px; }

.btn { padding: 8px 16px; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer; border: 1px solid transparent; transition: all 0.15s; white-space: nowrap; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn.sm { padding: 5px 12px; font-size: 12px; }
.btn-primary { background: var(--color-primary); color: #fff; }
.btn-primary:hover:not(:disabled) { background: #b87a3f; }
.btn-ghost { background: transparent; border-color: var(--color-border); color: var(--color-text); }
.btn-ghost:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
</style>