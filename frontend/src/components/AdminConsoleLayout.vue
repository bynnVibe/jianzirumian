<template>
  <!--
    系统管理控制台布局：与「正常使用界面」彻底分离。
    左侧为控制台专属导航（按角色渲染管理/运维模块），右侧顶栏 + 嵌套子页 router-view。
    admin 可返回正常使用界面；ops 仅能停留在控制台（无正常界面入口）。
    父容器 App.vue .main-content 为 100vh + overflow:hidden，本布局自行承担内部滚动分配。
  -->
  <div class="admin-console">
    <!-- ============ 左侧专属导航 ============ -->
    <aside class="console-nav">
      <div class="console-brand">
        <div class="console-brand-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20">
            <circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>
          </svg>
        </div>
        <div>
          <h1 class="console-brand-title">系统管理</h1>
          <p class="console-brand-subtitle">见字如面 · 管理控制台</p>
        </div>
      </div>

      <nav class="console-menu">
        <!-- 系统设置（仅管理员） -->
        <router-link
          v-if="auth.isAdmin"
          to="/admin/settings"
          class="console-item"
          :class="{ active: route.name === 'AdminSettings' }"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="console-item-icon">
            <circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>
          </svg>
          系统设置
        </router-link>

        <!-- 用户管理（仅管理员） -->
        <router-link
          v-if="auth.isAdmin"
          to="/admin/users"
          class="console-item"
          :class="{ active: route.name === 'UserManagement' }"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="console-item-icon">
            <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>
          </svg>
          用户管理
        </router-link>

        <!-- CI/CD 流水线（仅运维：EDD 评估驱动部署，管理员不可操作） -->
        <router-link
          v-if="auth.isOps"
          to="/admin/pipeline"
          class="console-item"
          :class="{ active: route.name === 'Pipeline' }"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="console-item-icon">
            <circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/>
            <path d="M6 8.5v2a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2v-2"/><path d="M12 12.5v3"/>
          </svg>
          CI/CD 流水线
        </router-link>

        <!-- 模型网关（管理员/运维） -->
        <router-link
          v-if="auth.canOps"
          to="/admin/gateway"
          class="console-item"
          :class="{ active: route.name === 'Gateway' }"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="console-item-icon">
            <rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>
            <path d="M10 6.5h4a2 2 0 0 1 2 2V14"/><path d="M14 17.5h-4a2 2 0 0 1-2-2V10"/>
          </svg>
          模型网关
        </router-link>

        <!-- 回归评测（管理员/运维） -->
        <router-link
          v-if="auth.canOps"
          to="/admin/eval"
          class="console-item"
          :class="{ active: route.name === 'Eval' }"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="console-item-icon">
            <path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>
          </svg>
          回归评测
        </router-link>

        <!-- Agent 可观测性（管理员/运维） -->
        <router-link
          v-if="auth.canOps"
          to="/admin/observability"
          class="console-item"
          :class="{ active: route.name === 'Observability' }"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="console-item-icon">
            <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
          </svg>
          Agent 可观测性
        </router-link>
      </nav>

      <!-- 底部用户条：改资料 + 登出（ops 无正常界面，此处为其唯一账户操作入口） -->
      <div class="console-footer">
        <div class="console-user">
          <div class="console-avatar">{{ auth.username.charAt(0).toUpperCase() }}</div>
          <div class="console-user-meta">
            <span class="console-user-name">{{ auth.username }}</span>
            <span class="console-user-role" :class="roleClass">{{ roleLabel }}</span>
          </div>
        </div>
        <div class="console-footer-actions">
          <button class="console-footer-btn" @click="openProfileModal" title="修改个人信息">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
              <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>
            </svg>
          </button>
          <button class="console-footer-btn console-footer-btn-logout" @click="showLogoutConfirm = true" title="退出登录">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
              <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>
            </svg>
          </button>
        </div>
      </div>
    </aside>

    <!-- ============ 右侧主体 ============ -->
    <section class="console-main">
      <header class="console-topbar">
        <h2 class="console-topbar-title">{{ currentTitle }}</h2>
        <!-- 仅管理员可返回正常使用界面；运维无正常界面入口 -->
        <button v-if="auth.isAdmin" class="console-back-btn" @click="router.push('/chat')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
            <line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>
          </svg>
          返回正常使用
        </button>
      </header>
      <div class="console-body">
        <router-view />
      </div>
    </section>

    <!-- 修改个人信息弹窗 -->
    <div v-if="showProfileModal" class="modal-overlay" @click.self="showProfileModal = false">
      <div class="modal-box">
        <h3 class="modal-title">修改个人信息</h3>
        <div class="form-field">
          <label class="form-label">用户名</label>
          <input class="form-input" v-model.trim="profileForm.username" placeholder="至少 2 个字符" />
        </div>
        <div class="form-field">
          <label class="form-label">联系方式</label>
          <input class="form-input" v-model.trim="profileForm.contact" placeholder="手机号 / 邮箱" />
        </div>
        <div class="form-divider">修改密码（选填）</div>
        <div class="form-field">
          <label class="form-label">原密码</label>
          <input class="form-input" type="password" v-model="profileForm.oldPassword" placeholder="修改密码时必填" />
        </div>
        <div class="form-field">
          <label class="form-label">新密码</label>
          <input class="form-input" type="password" v-model="profileForm.newPassword" placeholder="至少 6 位" />
        </div>
        <div class="form-field">
          <label class="form-label">确认新密码</label>
          <input class="form-input" type="password" v-model="profileForm.confirmPassword" placeholder="再次输入新密码" />
        </div>
        <p v-if="profileError" class="modal-error">{{ profileError }}</p>
        <div class="modal-actions">
          <button class="modal-btn modal-btn-cancel" @click="showProfileModal = false">取消</button>
          <button class="modal-btn modal-btn-confirm" :disabled="profileSaving" @click="saveProfile">
            {{ profileSaving ? '保存中...' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 退出登录确认弹窗 -->
    <div v-if="showLogoutConfirm" class="modal-overlay" @click.self="showLogoutConfirm = false">
      <div class="logout-confirm-box">
        <h3 class="modal-title">退出登录</h3>
        <p class="logout-confirm-desc">确定要退出当前账号吗？</p>
        <div class="modal-actions">
          <button class="modal-btn modal-btn-cancel" @click="showLogoutConfirm = false">取消</button>
          <button class="modal-btn modal-btn-danger" @click="confirmLogout">确认退出</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { updateProfile } from '@/api'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()

const roleLabel = computed(() =>
  auth.isAdmin ? '管理员' : (auth.isOps ? '运维' : '普通用户')
)
const roleClass = computed(() =>
  auth.isAdmin ? 'role-admin' : (auth.isOps ? 'role-ops' : 'role-user')
)

// 顶栏标题跟随当前子路由
const TITLE_MAP = {
  AdminSettings: '系统设置',
  UserManagement: '用户管理',
  Pipeline: 'CI/CD 流水线',
  Gateway: '模型网关',
  Eval: '回归评测',
  Observability: 'Agent 可观测性',
}
const currentTitle = computed(() => TITLE_MAP[route.name] || '系统管理')

// ---- 退出登录 ----
const showLogoutConfirm = ref(false)
async function confirmLogout() {
  showLogoutConfirm.value = false
  await auth.logout()
  router.push('/login')
}

// ---- 修改个人信息 ----
const showProfileModal = ref(false)
const profileSaving = ref(false)
const profileError = ref('')
const profileForm = ref({
  username: '',
  contact: '',
  oldPassword: '',
  newPassword: '',
  confirmPassword: '',
})

function openProfileModal() {
  profileForm.value = {
    username: auth.user?.username || '',
    contact: auth.user?.contact || '',
    oldPassword: '',
    newPassword: '',
    confirmPassword: '',
  }
  profileError.value = ''
  showProfileModal.value = true
}

async function saveProfile() {
  const f = profileForm.value
  profileError.value = ''
  if (f.newPassword || f.oldPassword) {
    if (!f.oldPassword) {
      profileError.value = '请输入原密码'
      return
    }
    if (!f.newPassword || f.newPassword.length < 6) {
      profileError.value = '新密码至少 6 位'
      return
    }
    if (f.newPassword !== f.confirmPassword) {
      profileError.value = '两次输入的新密码不一致'
      return
    }
  }
  const payload = { username: f.username, contact: f.contact }
  if (f.newPassword) {
    payload.old_password = f.oldPassword
    payload.new_password = f.newPassword
  }
  profileSaving.value = true
  try {
    const res = await updateProfile(payload)
    if (res.data.success) {
      auth.user = res.data.user
      localStorage.setItem('auth_user', JSON.stringify(res.data.user))
      showProfileModal.value = false
    }
  } catch (err) {
    profileError.value = err.response?.data?.detail || err.message || '保存失败'
  } finally {
    profileSaving.value = false
  }
}
</script>

<style scoped>
.admin-console {
  display: flex;
  height: 100%;
  width: 100%;
  overflow: hidden;
}

/* ---- 左侧专属导航 ---- */
.console-nav {
  width: 240px;
  flex-shrink: 0;
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--color-sidebar);
  border-right: 1px solid var(--color-border);
}

.console-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 20px 16px;
  border-bottom: 1px solid var(--color-border);
}

.console-brand-icon {
  width: 36px;
  height: 36px;
  background: linear-gradient(135deg, #c98a4b, #e9bc7d);
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: white;
  flex-shrink: 0;
}

.console-brand-title {
  font-size: 16px;
  font-weight: 700;
  color: var(--color-text);
  line-height: 1.2;
}

.console-brand-subtitle {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.console-menu {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.console-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border-radius: 10px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s;
  text-decoration: none;
  color: var(--color-text);
  border: 1px solid var(--color-border);
  background: white;
}

.console-item:hover {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
}

.console-item.active {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.console-item-icon {
  width: 16px;
  height: 16px;
  flex-shrink: 0;
}

/* ---- 底部用户条 ---- */
.console-footer {
  border-top: 1px solid var(--color-border);
  padding: 12px 16px;
  flex-shrink: 0;
}

.console-user {
  display: flex;
  align-items: center;
  gap: 10px;
}

.console-avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: linear-gradient(135deg, #c98a4b, #e9bc7d);
  color: white;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  font-weight: 700;
  flex-shrink: 0;
}

.console-user-meta {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.console-user-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.console-user-role {
  font-size: 11px;
  align-self: flex-start;
  padding: 1px 7px;
  border-radius: 8px;
}

.console-user-role.role-admin {
  background: #fdecea;
  color: #c62828;
}

.console-user-role.role-ops {
  background: #e3f2fd;
  color: #1565c0;
}

.console-user-role.role-user {
  background: #ede6dc;
  color: #8a7e72;
}

.console-footer-actions {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}

.console-footer-btn {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  height: 32px;
  border: 1px solid var(--color-border);
  background: white;
  border-radius: 8px;
  cursor: pointer;
  color: var(--color-text-secondary);
  transition: all 0.2s;
}

.console-footer-btn:hover {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.console-footer-btn-logout:hover {
  background: #fce4ec;
  border-color: #ef9a9a;
  color: #c62828;
}

/* ---- 右侧主体 ---- */
.console-main {
  flex: 1;
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-width: 0;
}

.console-topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 24px;
  border-bottom: 1px solid var(--color-border);
  background: var(--color-bg);
  flex-shrink: 0;
}

.console-topbar-title {
  font-size: 17px;
  font-weight: 700;
  color: var(--color-text);
}

.console-back-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 7px 14px;
  border: 1px solid var(--color-border);
  background: white;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text);
  cursor: pointer;
  transition: all 0.2s;
}

.console-back-btn:hover {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
  color: var(--color-primary);
}

/* 子页容器：子视图各自设 height:100% + overflow-y:auto 承担滚动 */
.console-body {
  flex: 1;
  overflow: hidden;
  min-height: 0;
}

/* ---- 弹窗（复用暖色主题） ---- */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}

.modal-box {
  width: 400px;
  max-width: 92vw;
  max-height: 86vh;
  overflow-y: auto;
  background: var(--color-bg, #fff);
  border-radius: 14px;
  padding: 22px 24px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.18);
}

.logout-confirm-box {
  width: 320px;
  max-width: 90vw;
  background: var(--color-bg, #fff);
  border-radius: 14px;
  padding: 22px 24px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.18);
}

.logout-confirm-desc {
  margin: 0 0 18px;
  font-size: 13px;
  color: var(--color-text-secondary, #888);
}

.modal-title {
  margin: 0 0 16px;
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text, #333);
}

.form-field {
  margin-bottom: 12px;
}

.form-label {
  display: block;
  font-size: 12px;
  color: var(--color-text-secondary, #888);
  margin-bottom: 4px;
}

.form-input {
  width: 100%;
  padding: 8px 10px;
  font-size: 13px;
  border: 1px solid var(--color-border, #e0d8cc);
  border-radius: 8px;
  background: #fff;
  color: var(--color-text, #333);
  outline: none;
  transition: border-color 0.15s;
  box-sizing: border-box;
}

.form-input:focus {
  border-color: var(--color-primary, #c98a4b);
}

.form-divider {
  font-size: 12px;
  color: var(--color-text-secondary, #999);
  border-top: 1px dashed var(--color-border, #e0d8cc);
  padding-top: 10px;
  margin: 14px 0 10px;
}

.modal-error {
  color: #c62828;
  font-size: 12px;
  margin: 0 0 10px;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 4px;
}

.modal-btn {
  padding: 7px 16px;
  font-size: 13px;
  border-radius: 8px;
  cursor: pointer;
  border: 1px solid transparent;
  transition: all 0.15s;
}

.modal-btn-cancel {
  background: transparent;
  border-color: var(--color-border, #e0d8cc);
  color: var(--color-text-secondary, #666);
}

.modal-btn-cancel:hover {
  background: rgba(0, 0, 0, 0.04);
}

.modal-btn-confirm {
  background: linear-gradient(135deg, #c98a4b, #d9a76a);
  color: #fff;
}

.modal-btn-confirm:hover {
  background: linear-gradient(135deg, #b0763c, #cf9a55);
}

.modal-btn-confirm:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.modal-btn-danger {
  background: #c62828;
  color: #fff;
}

.modal-btn-danger:hover {
  background: #b71c1c;
}

/* ---- 移动端：导航收窄 ---- */
@media (max-width: 768px) {
  .console-nav {
    width: 200px;
  }
}
</style>
