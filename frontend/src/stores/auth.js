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
  const username = computed(() => user.value?.username || '')
  const isGuest = computed(() => isGuestMode.value && !token.value && !user.value)

  // ---- 初始化（仅从 localStorage 同步恢复，不阻塞 UI） ----
  function init() {
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
  }

  async function verifyToken() {
    verifying.value = true
    try {
      const res = await getCurrentUser()
      if (res.data.success) {
        user.value = res.data.user
        localStorage.setItem('auth_user', JSON.stringify(res.data.user))
      }
    } catch {
      // token 无效，清除缓存
      token.value = ''
      user.value = null
      localStorage.removeItem('auth_token')
      localStorage.removeItem('auth_user')
    } finally {
      verifying.value = false
    }
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
  }

  return {
    user,
    token,
    loading,
    verifying,
    isGuestMode,
    isAuthenticated,
    isAdmin,
    isGuest,
    username,
    init,
    login,
    register,
    logout,
    enterAsGuest,
  }
})
