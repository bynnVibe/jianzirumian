import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'

/**
 * 通用「未保存改动离开拦截」composable —— 复用系统设置页（ManagementView）的三选弹窗交互。
 *
 * 拦截两类离开：
 *  1. 路由离开（onBeforeRouteLeave）：弹三选弹窗（保存后离开 / 不保留直接离开 / 留在本页）；
 *  2. 浏览器级离开（beforeunload）：关签/刷新/地址栏跳转触发原生未保存提示。
 * 另外对外暴露 confirmLeave()，供页内「tab 切换」等 v-else-if 卸载前复用同一弹窗，
 * 避免切换面板时静默丢弃草稿。
 *
 * 脏态判定二选一：
 *  - stateJson()：返回草稿态快照字符串，与内部快照不一致即视为脏（系统设置式）；
 *  - isDirty()：直接返回布尔（网关多源聚合式，dirty = 各注册源 some(isDirty)）。
 *
 * @param {Object}   opts
 * @param {Function} [opts.isActive=()=>true] 是否处于需拦截的上下文（如命中某路由）
 * @param {Function} [opts.stateJson]         草稿态快照序列化函数
 * @param {Function} [opts.isDirty]           直接脏态判定函数（优先于 stateJson）
 * @param {Function} opts.saveAll             保存全部草稿（save 选项调用）
 * @returns {{leavePrompt:import('vue').Ref<boolean>, leaveSaving:import('vue').Ref<boolean>,
 *            dirty:import('vue').ComputedRef<boolean>, resolveLeave:Function,
 *            refreshSnapshot:Function, confirmLeave:Function}}
 */
export function useUnsavedLeaveGuard({ isActive, stateJson, isDirty, saveAll }) {
  const active = typeof isActive === 'function' ? isActive : () => true
  const snapshot = ref(typeof stateJson === 'function' ? stateJson() : '')
  const leavePrompt = ref(false)
  const leaveSaving = ref(false)
  // 单一待决 resolver：保证同一时刻只有一个离开弹窗在等待用户选择，避免重入产生孤儿 promise
  let leaveResolve = null

  function refreshSnapshot() {
    snapshot.value = typeof stateJson === 'function' ? stateJson() : ''
  }

  const dirty = computed(() => {
    if (!active()) return false
    if (typeof isDirty === 'function') return !!isDirty()
    return !!snapshot.value && typeof stateJson === 'function' && stateJson() !== snapshot.value
  })

  function askLeaveChoice() {
    leavePrompt.value = true
    return new Promise((resolve) => { leaveResolve = resolve })
  }

  function resolveLeave(choice) {
    leavePrompt.value = false
    const r = leaveResolve
    leaveResolve = null
    if (r) r(choice)
  }

  /**
   * 统一离开确认：返回 true=放行、false=留下。
   * save→先 saveAll 再放行；leave→直接放行；stay→取消。
   * 已有弹窗在等待选择时取消本次动作，由首个守卫统一放行，避免重入绕过拦截。
   */
  async function confirmLeave() {
    if (!dirty.value) return true
    if (leavePrompt.value) return false
    const choice = await askLeaveChoice()
    if (choice === 'stay') return false
    if (choice === 'save') {
      leaveSaving.value = true
      try {
        if (typeof saveAll === 'function') await saveAll()
        refreshSnapshot()
      } finally {
        leaveSaving.value = false
      }
    }
    return true
  }

  onBeforeRouteLeave(async () => {
    if (!active()) return true
    return confirmLeave()
  })

  function onBeforeUnload(e) {
    if (dirty.value) {
      e.preventDefault()
      e.returnValue = ''
    }
  }
  onMounted(() => window.addEventListener('beforeunload', onBeforeUnload))
  onBeforeUnmount(() => window.removeEventListener('beforeunload', onBeforeUnload))

  return { leavePrompt, leaveSaving, dirty, resolveLeave, refreshSnapshot, confirmLeave }
}
