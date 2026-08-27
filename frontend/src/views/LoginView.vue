<template>
  <div class="auth-page">
    <div class="auth-card">
      <div class="auth-header">
        <div class="auth-logo">
          <svg class="leaf-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 19l7-7 3 3-7 7-3-3z"/>
            <path d="M18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5z"/>
            <path d="M2 2l7.586 7.586"/>
            <circle cx="11" cy="11" r="2"/>
          </svg>
        </div>
        <h1 class="auth-title">见字如面</h1>
        <p class="auth-subtitle">登录以继续使用</p>
      </div>

      <form class="auth-form" @submit.prevent="handleLogin">
        <div class="form-group">
          <label class="form-label">用户名</label>
          <input
            v-model="form.username"
            type="text"
            class="form-input"
            placeholder="请输入用户名"
            autocomplete="username"
            required
          />
        </div>

        <div class="form-group">
          <label class="form-label">密码</label>
          <input
            v-model="form.password"
            type="password"
            class="form-input"
            placeholder="请输入密码"
            autocomplete="current-password"
            required
          />
        </div>

        <div v-if="errorMsg" class="form-error">{{ errorMsg }}</div>

        <button type="submit" class="btn-submit" :disabled="auth.loading">
          <span v-if="auth.loading" class="spinner-xs"></span>
          {{ auth.loading ? '登录中...' : '登 录' }}
        </button>
      </form>

      <div class="auth-footer">
        还没有账号？
        <router-link to="/register" class="auth-link">立即注册</router-link>
      </div>

      <div class="guest-divider">
        <span>或</span>
      </div>

      <button class="btn-guest" @click="handleGuest">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" class="guest-icon">
          <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
          <circle cx="12" cy="7" r="4"/>
        </svg>
        以游客身份体验
      </button>

      <router-link to="/guide" class="btn-guide">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" class="guest-icon">
          <circle cx="12" cy="12" r="10"/>
          <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>
          <line x1="12" y1="17" x2="12.01" y2="17"/>
        </svg>
        系统使用指导
      </router-link>
    </div>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useChatStore } from '@/stores/chat'

const router = useRouter()
const auth = useAuthStore()
const chatStore = useChatStore()

const form = reactive({
  username: '',
  password: '',
})
const errorMsg = ref('')

async function handleLogin() {
  errorMsg.value = ''
  if (!form.username.trim() || !form.password) {
    errorMsg.value = '请填写用户名和密码'
    return
  }

  const result = await auth.login(form.username.trim(), form.password)
  if (result.success) {
    // 登录成功，清空旧的会话数据（如游客会话），加载新用户的会话
    chatStore.clearAllSessionData()
    router.push('/chat')
  } else {
    errorMsg.value = result.message
  }
}

function handleGuest() {
  // 清空之前用户的会话数据，再进入游客模式
  chatStore.clearAllSessionData()
  auth.enterAsGuest()
  router.push('/chat')
}
</script>

<style scoped>
.auth-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: linear-gradient(135deg, #f5f0ea 0%, #ede6dc 50%, #e6ddd2 100%);
  padding: 20px;
}

.auth-card {
  background: #fff;
  border-radius: 20px;
  padding: 40px 36px;
  width: 100%;
  max-width: 400px;
  box-shadow: 0 8px 40px rgba(0, 0, 0, 0.08);
}

.auth-header {
  text-align: center;
  margin-bottom: 32px;
}

.auth-logo {
  width: 56px;
  height: 56px;
  background: linear-gradient(135deg, #c98a4b, #e9bc7d);
  border-radius: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 0 auto 16px;
  color: white;
}

.leaf-icon {
  width: 30px;
  height: 30px;
}

.auth-title {
  font-size: 24px;
  font-weight: 700;
  color: #3a2e24;
  margin-bottom: 6px;
}

.auth-subtitle {
  font-size: 14px;
  color: #8c7e6e;
}

.auth-form {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.form-label {
  font-size: 13px;
  font-weight: 600;
  color: #5a4a3a;
}

.form-input {
  padding: 11px 14px;
  border: 1px solid #ddd5cb;
  border-radius: 10px;
  font-size: 14px;
  color: #3a2e24;
  background: #faf8f5;
  transition: all 0.2s;
  outline: none;
}

.form-input:focus {
  border-color: #c98a4b;
  box-shadow: 0 0 0 3px rgba(201, 138, 75, 0.12);
  background: white;
}

.form-input::placeholder {
  color: #bbb0a2;
}

.form-error {
  font-size: 13px;
  color: #c62828;
  background: #fce4ec;
  padding: 8px 12px;
  border-radius: 8px;
  text-align: center;
}

.btn-submit {
  padding: 12px;
  background: linear-gradient(135deg, #c98a4b, #d9a76a);
  color: white;
  border: none;
  border-radius: 12px;
  font-size: 15px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  margin-top: 4px;
}

.btn-submit:hover:not(:disabled) {
  background: linear-gradient(135deg, #b0763c, #cf9a55);
  transform: translateY(-1px);
  box-shadow: 0 4px 12px rgba(201, 138, 75, 0.3);
}

.btn-submit:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.spinner-xs {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid rgba(255, 255, 255, 0.3);
  border-top-color: white;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.auth-footer {
  text-align: center;
  margin-top: 24px;
  font-size: 14px;
  color: #8c7e6e;
}

.auth-link {
  color: #c98a4b;
  text-decoration: none;
  font-weight: 600;
}

.auth-link:hover {
  text-decoration: underline;
}

.guest-divider {
  display: flex;
  align-items: center;
  margin: 20px 0;
  color: #bbb0a2;
  font-size: 13px;
}

.guest-divider::before,
.guest-divider::after {
  content: '';
  flex: 1;
  height: 1px;
  background: #e8e0d6;
}

.guest-divider span {
  padding: 0 12px;
}

.btn-guest {
  width: 100%;
  padding: 11px;
  background: transparent;
  border: 1.5px solid #ddd5cb;
  border-radius: 12px;
  font-size: 14px;
  font-weight: 500;
  color: #8c7e6e;
  cursor: pointer;
  transition: all 0.2s;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
}

.btn-guest:hover {
  border-color: #c98a4b;
  color: #c98a4b;
  background: #faf8f5;
}

.guest-icon {
  width: 18px;
  height: 18px;
}

.btn-guide {
  margin-top: 10px;
  width: 100%;
  padding: 11px;
  background: transparent;
  border: none;
  font-size: 13px;
  font-weight: 500;
  color: #8c7e6e;
  cursor: pointer;
  transition: color 0.2s;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  text-decoration: none;
  box-sizing: border-box;
}

.btn-guide:hover {
  color: #c98a4b;
}
</style>
