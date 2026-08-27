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
        <h1 class="auth-title">创建账号</h1>
        <p class="auth-subtitle">注册后即可使用知识库问答系统</p>
      </div>

      <form class="auth-form" @submit.prevent="handleRegister">
        <div class="form-group">
          <label class="form-label">用户名</label>
          <input
            v-model="form.username"
            type="text"
            class="form-input"
            placeholder="至少 2 个字符"
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
            placeholder="至少 6 个字符"
            autocomplete="new-password"
            required
          />
        </div>

        <div class="form-group">
          <label class="form-label">确认密码</label>
          <input
            v-model="form.confirmPassword"
            type="password"
            class="form-input"
            placeholder="再次输入密码"
            autocomplete="new-password"
            required
          />
        </div>

        <div class="form-group">
          <label class="form-label">联系方式</label>
          <input
            v-model="form.contact"
            type="text"
            class="form-input"
            placeholder="手机号或邮箱"
            required
          />
          <span class="form-hint">用于找回密码和账号通知</span>
        </div>

        <div v-if="errorMsg" class="form-error">{{ errorMsg }}</div>

        <button type="submit" class="btn-submit" :disabled="auth.loading">
          <span v-if="auth.loading" class="spinner-xs"></span>
          {{ auth.loading ? '注册中...' : '注 册' }}
        </button>
      </form>

      <div class="auth-footer">
        已有账号？
        <router-link to="/login" class="auth-link">返回登录</router-link>
      </div>
    </div>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()

const form = reactive({
  username: '',
  password: '',
  confirmPassword: '',
  contact: '',
})
const errorMsg = ref('')

async function handleRegister() {
  errorMsg.value = ''

  const username = form.username.trim()
  const password = form.password
  const confirmPassword = form.confirmPassword
  const contact = form.contact.trim()

  // 前端校验
  if (username.length < 2) {
    errorMsg.value = '用户名至少 2 个字符'
    return
  }
  if (password.length < 6) {
    errorMsg.value = '密码至少 6 个字符'
    return
  }
  if (password !== confirmPassword) {
    errorMsg.value = '两次输入的密码不一致'
    return
  }

  // 联系方式校验：手机号或邮箱
  const isPhone = /^1\d{10}$/.test(contact)
  const isEmail = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(contact)
  if (!isPhone && !isEmail) {
    errorMsg.value = '请输入有效的手机号（11位）或邮箱地址'
    return
  }

  const result = await auth.register(username, password, contact)
  if (result.success) {
    // 注册成功后提示并跳转到登录页
    const isAdmin = result.user.role === 'admin'
    alert(isAdmin ? '管理员账号注册成功！请登录' : '注册成功！请登录')
    router.push('/login')
  } else {
    errorMsg.value = result.message
  }
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
  max-width: 420px;
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

.form-hint {
  font-size: 12px;
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
</style>
