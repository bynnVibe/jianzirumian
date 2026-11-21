import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import {
  loginUser,
  registerUser,
  logoutUser,
  getCurrentUser,
} from '@/api'

export const useAuthStore = defineStore('auth', () => {
  // ---- 状态 ----
  const user = ref(null)
  const token = ref('')
  const loading = ref(false)
  const verifying = ref(false)   // 后台验证 Token 是否进行中
  const isGuestMode = ref(false) // 游客模式标识

  // ---- 计算属性 ----
  const isAuthenticated = computed(() => !!token.value && !!user.value)
  const isAdmin = computed(() => user.value?.role === 'admin')
  const isOps = computed(() => user.value?.role === 'ops')
  // 运维权限：管理员或运维角色（可访问 CI/CD、可观测性、回归评测）
  const canOps = computed(() => user.value?.role === 'admin' || user.value?.role === 'ops')
  const username = computed(() => user.value?.username || '')
  const isGuest = computed(() => isGuestMode.value && !token.value && !user.value)

  // ---- 初始化（仅从 localStorage 同步恢复，不阻塞 UI） ----
  let initialized = false
  function init() {
    if (initialized) return
    initialized = true
    const savedToken = localStorage.getItem('auth_token')
    const savedUser = localStorage.getItem('auth_user')
    const savedGuest = localStorage.getItem('guest_mode')
    if (savedToken) {
      token.value = savedToken
    }
    if (savedUser) {
      try {
        user.value = JSON.parse(savedUser)
      } catch {
        user.value = null
      }
    }
    if (savedGuest === 'true' && !savedToken) {
      isGuestMode.value = true
    }
    // 后台异步验证 token（不 await，不阻塞 UI）
    if (savedToken) {
      verifyToken()
    }
    startHeartbeat()
  }

  async function verifyToken() {
    verifying.value = true
    try {
      const res = await getCurrentUser()
      if (res.data.success) {
        user.value = res.data.user
        localStorage.setItem('auth_user', JSON.stringify(res.data.user))
      }
    } catch (err) {
      // 仅 401（登录态真实过期）才清除；网络抖动/5xx 等瞬态失败不误登出
      if (err?.response?.status === 401) {
        token.value = ''
        user.value = null
        localStorage.removeItem('auth_token')
        localStorage.removeItem('auth_user')
      }
    } finally {
      verifying.value = false
    }
  }

  // ---- 登录态滑动续期：用户有操作时周期心跳调 /auth/me（后端鉴权即续期），
  // 窗口内无任何操作则不续期，会话自然过期退出 ----
  const HEARTBEAT_MS = 5 * 60 * 1000
  let lastActivityAt = Date.now()
  let heartbeatTimer = null
  function markActivity() { lastActivityAt = Date.now() }
  function startHeartbeat() {
    if (heartbeatTimer) return
    ;['pointerdown', 'keydown', 'scroll', 'touchstart'].forEach((e) => {
      window.addEventListener(e, markActivity, { passive: true, capture: true })
    })
    heartbeatTimer = setInterval(async () => {
      if (!token.value || !user.value) return
      if (Date.now() - lastActivityAt > HEARTBEAT_MS) return // 闲置不续期
      try { await getCurrentUser() } catch { /* 401 由响应拦截器统一处理 */ }
    }, HEARTBEAT_MS)
  }

  // ---- 方法 ----
  async function login(username, password) {
    loading.value = true
    try {
      const res = await loginUser(username, password)
      if (res.data.success) {
        token.value = res.data.token
        user.value = res.data.user
        localStorage.setItem('auth_token', res.data.token)
        localStorage.setItem('auth_user', JSON.stringify(res.data.user))
        // 登录成功，退出游客模式
        isGuestMode.value = false
        localStorage.removeItem('guest_mode')
        return { success: true }
      }
      return { success: false, message: '登录失败' }
    } catch (err) {
      const msg = err.response?.data?.detail || err.message || '登录失败'
      return { success: false, message: msg }
    } finally {
      loading.value = false
    }
  }

  async function register(username, password, contact) {
    loading.value = true
    try {
      const res = await registerUser(username, password, contact)
      if (res.data.success) {
        return { success: true, user: res.data.user }
      }
      return { success: false, message: '注册失败' }
    } catch (err) {
      const msg = err.response?.data?.detail || err.message || '注册失败'
      return { success: false, message: msg }
    } finally {
      loading.value = false
    }
  }

  function enterAsGuest() {
    // 以游客身份进入系统
    isGuestMode.value = true
    localStorage.setItem('guest_mode', 'true')
  }

  async function logout() {
    const { useChatStore } = await import('./chat')
    useChatStore().clearAllSessionData()
    // 注销即清理查询缓存（含公共持久层）并关闭变更同步长连接
    const { useQueryCacheStore } = await import('./queryCache')
    useQueryCacheStore().clearAll()
    const { stopSync } = await import('@/utils/syncClient')
    stopSync()
    try {
      await logoutUser()
    } catch {
      // 忽略登出请求错误
    }
    token.value = ''
    user.value = null
    isGuestMode.value = false
    localStorage.removeItem('auth_token')
    localStorage.removeItem('auth_user')
    localStorage.removeItem('guest_mode')
    // 重置安全告警一次性弹窗标记：下次登录若仍有未确认事件再提示一次
    sessionStorage.removeItem('sec_alert_shown_session')
  }

  return {
    user,
    token,
    loading,
    verifying,
    isGuestMode,
    isAuthenticated,
    isAdmin,
    isOps,
    canOps,
    isGuest,
    username,
    init,
    login,
    register,
    logout,
    enterAsGuest,
  }
})
