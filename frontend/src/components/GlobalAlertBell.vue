<template>
  <div v-if="auth.canOps && auth.isAuthenticated" class="alert-bell-wrap"
       :style="{ top: bellTop + 'px', right: bellRight + 'px' }">
    <!-- 新 critical 告警 toast -->
    <transition name="bell-toast">
      <div v-if="toast" class="bell-toast" @click="goAlerts">
        <strong>⚠ 严重告警</strong>
        <span>{{ toast }}</span>
        <em>点击查看 →</em>
      </div>
    </transition>
    <!-- 铃铛：未确认告警红点；支持上下拖拽移位（避开侧边面板按钮），点击进告警页 -->
    <button class="alert-bell" :class="{ ringing: unacked > 0, dragging: bellDragging }"
            :title="(unacked ? `${unacked} 条未确认告警` : '暂无未确认告警') + '·可上下拖拽移动位置'"
            @pointerdown="onBellPointerDown" @click="onBellClick">
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
        <path d="M13.73 21a2 2 0 0 1-3.46 0" />
      </svg>
      <span v-if="unacked" class="bell-dot">{{ unacked > 99 ? '99+' : unacked }}</span>
    </button>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useAssistantStore } from '@/stores/assistant'
import { getObsAlerts } from '@/api'

const auth = useAuthStore()
const assistant = useAssistantStore()
const router = useRouter()

const unacked = ref(0)
const toast = ref('')
let timer = null
let toastTimer = null
// 已提示过的 critical 告警 id（同一告警只 toast 一次）
const notified = new Set()
let firstLoad = true

// ---- 铃铛位置：上下可拖拽（localStorage 持久化）；知识助手展开时左移避让面板头部按钮 ----
const BELL_TOP_KEY = 'jzrm.alertBellTop'
const _savedTop = parseInt(localStorage.getItem(BELL_TOP_KEY) || '', 10)
const bellTop = ref(Number.isFinite(_savedTop) ? Math.max(8, Math.min(_savedTop, 4000)) : 14)
const bellDragging = ref(false)
let bellSuppressClick = false

const bellRight = computed(() => {
  if (!assistant.isOpen) return 18
  // 小屏下避让宽度收敛，避免铃铛被推出屏幕外
  return Math.min(assistant.panelWidth + 18, Math.max(18, window.innerWidth - 56))
})

function onBellPointerDown(e) {
  const startY = e.clientY
  const startTop = bellTop.value
  let moved = false
  const onMove = (ev) => {
    const dy = ev.clientY - startY
    if (!moved && Math.abs(dy) < 4) return
    moved = true
    bellDragging.value = true
    bellTop.value = Math.max(8, Math.min(window.innerHeight - 70, startTop + dy))
  }
  const onUp = () => {
    window.removeEventListener('pointermove', onMove)
    window.removeEventListener('pointerup', onUp)
    bellDragging.value = false
    if (moved) {
      bellSuppressClick = true
      setTimeout(() => { bellSuppressClick = false }, 0)
      try { localStorage.setItem(BELL_TOP_KEY, String(bellTop.value)) } catch (err) { /* 忽略 */ }
    }
  }
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', onUp)
}

function onBellClick() {
  if (bellSuppressClick) return   // 拖拽结束触发的 click 不导航
  goAlerts()
}

const METRIC_LABELS = {
  error_rate: '请求错误率', p95_latency: 'P95 延迟', token_overuse: '平均 Token/请求',
  eval_regression: '回归评测均分', dislike_rate: '点踩占比', online_quality: '在线评测均分',
}

async function poll() {
  if (!auth.canOps || !auth.isAuthenticated) return
  try {
    const res = await getObsAlerts({ status: 'open', limit: 50 })
    const list = res.data.alerts || []
    unacked.value = res.data.unacked || 0
    // 新出现的 critical 告警 → toast（首轮只记录不提示，避免登录即轰炸）
    for (const a of list) {
      if (a.severity === 'critical' && !notified.has(a.id)) {
        notified.add(a.id)
        if (!firstLoad) {
          toast.value = `${METRIC_LABELS[a.metric] || a.metric} 越限（当前 ${a.value} / 阈值 ${a.threshold}）`
          if (toastTimer) clearTimeout(toastTimer)
          toastTimer = setTimeout(() => { toast.value = '' }, 8000)
        }
      }
    }
    firstLoad = false
  } catch (e) {
    // 轮询失败静默（未登录/无权限时不打扰）
  }
}

function goAlerts() {
  toast.value = ''
  router.push({ name: 'Observability', query: { tab: 'metrics', focus: 'alerts' } })
}

watch(() => auth.canOps, (v) => { if (v) { firstLoad = true; poll() } })

onMounted(() => {
  poll()
  timer = setInterval(poll, 15000)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
  if (toastTimer) clearTimeout(toastTimer)
})
</script>

<style scoped>
.alert-bell-wrap { position: fixed; top: 14px; right: 18px; z-index: 120; display: flex; flex-direction: column; align-items: flex-end; gap: 8px; transition: right 0.25s; }
.alert-bell {
  position: relative; width: 38px; height: 38px; border-radius: 50%;
  border: 1px solid var(--color-border); background: #fffdfb; color: var(--color-text-secondary);
  cursor: grab; display: flex; align-items: center; justify-content: center;
  box-shadow: 0 1px 4px rgba(120,90,50,0.10); transition: all 0.15s; touch-action: none;
}
.alert-bell.dragging { cursor: grabbing; box-shadow: 0 4px 14px rgba(120,90,50,0.22); }
.alert-bell:hover { color: var(--color-primary); border-color: var(--color-primary); }
.alert-bell.ringing { color: #c0392b; border-color: #f0c4c2; }
.bell-dot {
  position: absolute; top: -5px; right: -6px; min-width: 17px; height: 17px; padding: 0 4px;
  border-radius: 9px; background: #d9534f; color: #fff; font-size: 10px; font-weight: 700;
  display: flex; align-items: center; justify-content: center; box-sizing: border-box;
}
.bell-toast {
  display: flex; flex-direction: column; gap: 3px; max-width: 300px;
  background: #fffdfb; border: 1px solid #f0c4c2; border-left: 3px solid #d9534f;
  border-radius: 10px; padding: 10px 14px; cursor: pointer;
  box-shadow: 0 4px 16px rgba(120,50,40,0.16); font-size: 12px; color: var(--color-text);
}
.bell-toast strong { color: #c0392b; font-size: 12.5px; }
.bell-toast em { font-style: normal; font-size: 11px; color: var(--color-primary); }
.bell-toast-enter-active, .bell-toast-leave-active { transition: all 0.25s ease; }
.bell-toast-enter-from, .bell-toast-leave-to { opacity: 0; transform: translateY(-8px); }
@media (max-width: 768px) { .alert-bell-wrap { top: 12px; right: 12px; } }
</style>