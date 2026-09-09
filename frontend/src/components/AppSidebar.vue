<template>
  <aside class="sidebar" :class="{ 'sidebar-open': open }">
    <!-- 移动端关闭按钮 -->
    <button class="sidebar-close-btn" @click="$emit('close')" aria-label="关闭菜单">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20">
        <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
      </svg>
    </button>

    <!-- 品牌区域 -->
    <div class="brand">
      <div class="brand-icon">
        <svg class="leaf-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 19l7-7 3 3-7 7-3-3z"/>
          <path d="M18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5z"/>
          <path d="M2 2l7.586 7.586"/>
          <circle cx="11" cy="11" r="2"/>
        </svg>
      </div>
      <div>
        <h1 class="brand-title">见字如面</h1>
        <p class="brand-subtitle">手写笔记知识库</p>
      </div>
    </div>

    <!-- 操作按钮 -->
    <div class="actions">
      <button class="btn-new-chat" @click="handleNewChat">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="btn-icon">
          <line x1="12" y1="5" x2="12" y2="19"/>
          <line x1="5" y1="12" x2="19" y2="12"/>
        </svg>
        新对话
      </button>

      <router-link v-if="!auth.isGuest" to="/upload" class="btn-upload" :class="{ active: isUploadPage }" @click="handleNavClick">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="btn-icon">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
          <polyline points="17 8 12 3 7 8"/>
          <line x1="12" y1="3" x2="12" y2="15"/>
        </svg>
        知识库上传
      </router-link>
    </div>

    <!-- 配置与管理的入口 -->
    <div class="actions config-actions">
      <!-- 知识库管理（所有用户可见） -->
      <router-link to="/knowledge" class="btn-upload" :class="{ active: isKnowledgePage }" @click="handleNavClick">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="btn-icon">
          <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
        </svg>
        知识库管理
      </router-link>

      <!-- 知识收藏（所有用户可见） -->
      <router-link to="/bookmarks" class="btn-upload" :class="{ active: isBookmarksPage }" @click="handleNavClick">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="btn-icon">
          <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>
        </svg>
        知识收藏
      </router-link>

      <!-- 知识百科（登录用户可见：llm-wiki 编译词条，含个人知识库词条） -->
      <router-link v-if="!auth.isGuest" to="/wiki" class="btn-upload" :class="{ active: isWikiPage }" @click="handleNavClick">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="btn-icon">
          <path d="M12 2l3 6 7 1-5 5 1 7-6-3-6 3 1-7-5-5 7-1z"/>
        </svg>
        知识百科
      </router-link>

      <!-- 系统管理（仅管理员可见：系统设置 + 用户管理） -->
      <router-link
        v-if="auth.isAdmin"
        to="/management"
        class="btn-upload"
        :class="{ active: isManagementPage }"
        @click="handleNavClick"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="btn-icon">
          <circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>
        </svg>
        系统管理
      </router-link>

      <!-- 回归评测（仅管理员可见：评测集 / 一键回归 / 运行对比） -->
      <router-link
        v-if="auth.isAdmin"
        to="/eval"
        class="btn-upload"
        :class="{ active: isEvalPage }"
        @click="handleNavClick"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="btn-icon">
          <path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>
        </svg>
        回归评测
      </router-link>
    </div>

    <!-- 聊天历史 -->
    <div class="history-section">
      <div class="section-header">
        <span class="section-title">对话历史</span>
        <span class="session-count" v-if="sessions.length">{{ sessions.length }}</span>
      </div>

      <div class="history-list" v-if="sessions.length > 0">
        <div
          v-for="group in groupedSessions"
          :key="group.key"
          class="history-group"
        >
          <div class="history-group-header" @click="toggleGroup(group.key)">
            <span class="group-title">{{ group.label }}</span>
            <span class="group-count">{{ group.sessions.length }}</span>
            <span class="group-arrow" :class="{ collapsed: collapsedGroups.has(group.key) }">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                <polyline points="6 9 12 15 18 9"/>
              </svg>
            </span>
          </div>
          <div v-if="!collapsedGroups.has(group.key)" class="history-group-items">
            <div
              v-for="session in group.sessions"
              :key="session.session_id"
              class="history-item"
              :class="{ active: session.session_id === currentSessionId }"
              @click="handleSwitchSession(session.session_id)"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="chat-icon">
                <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
              </svg>
              <span class="item-title">{{ session.title }}</span>
              <button
                class="btn-delete"
                @click.stop="handleDeleteSession(session.session_id)"
                title="删除对话"
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                  <polyline points="3 6 5 6 21 6"/>
                  <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                </svg>
              </button>
            </div>
          </div>
        </div>
      </div>

      <div class="empty-history" v-else>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" class="empty-icon">
          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
        </svg>
        <p>暂无对话记录</p>
        <p class="empty-hint">开始新对话吧</p>
      </div>
    </div>

    <!-- 底部：用户信息 + 登出 -->
    <div class="sidebar-footer">
      <div class="user-info" v-if="auth.isAuthenticated">
        <div class="user-avatar">{{ auth.username.charAt(0).toUpperCase() }}</div>
        <div class="user-meta user-meta-clickable" @click="toggleUserMenu" title="个人中心">
          <span class="user-name">{{ auth.username }}</span>
          <span class="user-role">{{ auth.isAdmin ? '管理员' : '普通用户' }}</span>
        </div>
        <button class="btn-logout" @click="handleLogout" title="退出登录">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
            <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
            <polyline points="16 17 21 12 16 7"/>
            <line x1="21" y1="12" x2="9" y2="12"/>
          </svg>
        </button>
      </div>

      <!-- 游客模式提示 -->
      <div class="guest-banner" v-if="auth.isGuest">
        <div class="guest-banner-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="20" height="20">
            <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
            <circle cx="12" cy="7" r="4"/>
          </svg>
        </div>
        <div class="guest-banner-text">
          <span class="guest-banner-title">游客模式</span>
          <span class="guest-banner-hint">登录解锁更多功能</span>
        </div>
        <button class="btn-guest-login" @click="handleGuestLogin">登录</button>
      </div>

      <!-- 用户悬浮菜单 -->
      <div v-if="showUserMenu" class="user-menu">
        <button class="user-menu-item" @click="openProfileModal">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="15" height="15">
            <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
            <circle cx="12" cy="7" r="4"/>
          </svg>
          修改个人信息
        </button>
        <button class="user-menu-item" @click="openUsageModal">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="15" height="15">
            <line x1="18" y1="20" x2="18" y2="10"/>
            <line x1="12" y1="20" x2="12" y2="4"/>
            <line x1="6" y1="20" x2="6" y2="14"/>
          </svg>
          查看使用情况
        </button>
      </div>
    </div>

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

    <!-- 使用看板弹窗 -->
    <div v-if="showUsageModal" class="modal-overlay" @click.self="showUsageModal = false">
      <div class="modal-box usage-box">
        <div class="usage-header">
          <h3 class="modal-title">使用看板</h3>
          <div class="usage-days">
            <button
              v-for="d in [7, 30, 90]"
              :key="d"
              class="usage-day-btn"
              :class="{ active: usageDays === d }"
              @click="loadUsage(d)"
            >近{{ d }}天</button>
          </div>
        </div>

        <div v-if="usageLoading" class="usage-loading">加载中...</div>
        <p v-else-if="usageError" class="modal-error">{{ usageError }}</p>
        <template v-else-if="usageData">
          <div class="usage-scroll">
            <!-- 总量卡片 -->
            <div class="usage-cards">
              <div class="usage-card">
                <span class="usage-card-num">{{ usageData.total_query_count }}</span>
                <span class="usage-card-label">提问次数</span>
              </div>
              <div class="usage-card">
                <span class="usage-card-num">{{ usageData.total_knowledge_count }}</span>
                <span class="usage-card-label">知识库检索</span>
              </div>
              <div class="usage-card">
                <span class="usage-card-num">{{ usageData.total_web_search_count }}</span>
                <span class="usage-card-label">联网搜索</span>
              </div>
              <div class="usage-card">
                <span class="usage-card-num">{{ usageData.total_upload_count }}</span>
                <span class="usage-card-label">知识库上传</span>
              </div>
              <div class="usage-card">
                <span class="usage-card-num">{{ usageData.total_tokens }}</span>
                <span class="usage-card-label">Token 消耗</span>
              </div>
              <div class="usage-card">
                <span class="usage-card-num">{{ usageData.active_days }}</span>
                <span class="usage-card-label">活跃天数</span>
              </div>
              <div class="usage-card">
                <span class="usage-card-num">{{ usageData.session_count }}</span>
                <span class="usage-card-label">对话会话</span>
              </div>
              <div class="usage-card">
                <span class="usage-card-num">{{ usageData.bookmark_count }}</span>
                <span class="usage-card-label">知识收藏</span>
              </div>
              <div class="usage-card">
                <span class="usage-card-num">{{ usageData.kb_count }}</span>
                <span class="usage-card-label">个人知识库</span>
              </div>
              <div class="usage-card">
                <span class="usage-card-num">{{ usageData.like_count }} / {{ usageData.dislike_count }}</span>
                <span class="usage-card-label">点赞 / 点踩</span>
              </div>
            </div>

            <!-- 每日提问频率趋势 -->
            <div class="usage-section-title">每日提问频率（近{{ usageDays }}天）</div>
            <div class="usage-chart" v-if="usageData.daily.length">
              <div
                v-for="(row, idx) in usageData.daily"
                :key="row.day"
                class="chart-col"
                :title="chartTooltip(row)"
              >
                <div class="chart-bar" :style="{ height: barHeight(row.query_count) + '%' }"></div>
                <span v-if="showChartLabel(idx)" class="chart-label">{{ formatChartDay(row.day) }}</span>
              </div>
            </div>
            <div v-else class="chart-empty">暂无使用记录</div>

            <!-- 每日明细 -->
            <div class="usage-section-title" v-if="usageData.daily.length">每日明细</div>
            <table class="usage-table" v-if="usageData.daily.length">
              <thead>
                <tr>
                  <th>日期</th>
                  <th>提问</th>
                  <th>知识库检索</th>
                  <th>联网搜索</th>
                  <th>上传</th>
                  <th>Token</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in reversedDaily" :key="row.day">
                  <td>{{ formatChartDay(row.day) }}</td>
                  <td>{{ row.query_count }}</td>
                  <td>{{ row.knowledge_count }}</td>
                  <td>{{ row.web_search_count }}</td>
                  <td>{{ row.upload_count }}</td>
                  <td>{{ row.tokens }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
      </div>
    </div>

    <!-- 退出登录确认弹窗 -->
    <div v-if="showLogoutConfirm" class="logout-confirm-overlay" @click.self="showLogoutConfirm = false">
      <div class="logout-confirm-box">
        <h3 class="logout-confirm-title">退出登录</h3>
        <p class="logout-confirm-desc">确定要退出当前账号吗？</p>
        <div class="logout-confirm-actions">
          <button class="logout-btn logout-btn-cancel" @click="showLogoutConfirm = false">取消</button>
          <button class="logout-btn logout-btn-confirm" @click="confirmLogout">确认退出</button>
        </div>
      </div>
    </div>
  </aside>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useChatStore } from '@/stores/chat'
import { useAuthStore } from '@/stores/auth'
import { healthCheck, updateProfile, getMyUsageStats } from '@/api'

const props = defineProps({
  open: { type: Boolean, default: false },
})
const emit = defineEmits(['close'])

const router = useRouter()
const route = useRoute()
const store = useChatStore()
const auth = useAuthStore()
const isConnected = ref(false)

const sessions = computed(() => store.sessions)
const currentSessionId = computed(() => store.currentSessionId)
const collapsedGroups = ref(new Set())

// 按日期分组：今天 / 昨天 / 具体日期（在同一年内省略年份）
const groupedSessions = computed(() => {
  const groups = []
  const map = new Map()

  for (const session of sessions.value) {
    const d = new Date(session.updated_at || session.created_at)
    if (Number.isNaN(d.getTime())) continue
    const key = formatDateKey(d)
    if (!map.has(key)) {
      map.set(key, {
        key,
        label: formatGroupLabel(d),
        sessions: [],
        date: d,
      })
      groups.push(map.get(key))
    }
    map.get(key).sessions.push(session)
  }

  // 日期降序
  groups.sort((a, b) => b.date - a.date)
  return groups
})

// 监听会话变化，默认折叠超过 10 天的日期组
watch(
  sessions,
  (list) => {
    const now = new Date()
    now.setHours(0, 0, 0, 0)
    const next = new Set(collapsedGroups.value)
    for (const session of list) {
      const d = new Date(session.updated_at || session.created_at)
      if (Number.isNaN(d.getTime())) continue
      const dayStart = new Date(d)
      dayStart.setHours(0, 0, 0, 0)
      const diffDays = Math.floor((now - dayStart) / (1000 * 60 * 60 * 24))
      const key = formatDateKey(d)
      if (diffDays > 10) {
        next.add(key)
      }
    }
    collapsedGroups.value = next
  },
  { immediate: true }
)

function formatDateKey(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

function formatGroupLabel(d) {
  const now = new Date()
  now.setHours(0, 0, 0, 0)
  const dayStart = new Date(d)
  dayStart.setHours(0, 0, 0, 0)
  const diff = Math.floor((now - dayStart) / (1000 * 60 * 60 * 24))
  if (diff === 0) return '今天'
  if (diff === 1) return '昨天'
  if (diff < 7) return `前 ${diff} 天`
  if (d.getFullYear() === now.getFullYear()) {
    return `${String(d.getMonth() + 1).padStart(2, '0')}月${String(d.getDate()).padStart(2, '0')}日`
  }
  return `${d.getFullYear()}年${String(d.getMonth() + 1).padStart(2, '0')}月${String(d.getDate()).padStart(2, '0')}日`
}

function toggleGroup(key) {
  const next = new Set(collapsedGroups.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  collapsedGroups.value = next
}

const isUploadPage = computed(() => route.path === '/upload')
const isManagementPage = computed(() => route.path.startsWith('/management'))
const isKnowledgePage = computed(() => route.path === '/knowledge')
const isBookmarksPage = computed(() => route.path === '/bookmarks')
const isWikiPage = computed(() => route.path === '/wiki')
const isAdminUsersPage = computed(() => route.path === '/admin/users')
const isEvalPage = computed(() => route.path === '/eval')

onMounted(async () => {
  // 仅已登录用户加载会话列表（游客/未登录不发起请求，避免 401 触发跳转）
  if (auth.isAuthenticated) {
    await store.loadSessions()
  }
  try {
    await healthCheck()
    isConnected.value = true
  } catch {
    isConnected.value = false
  }
})

async function handleNewChat() {
  emit('close')
  await store.newSession()
  router.push('/chat')
}

function handleNavClick() {
  emit('close')
}

function handleSwitchSession(sessionId) {
  emit('close')
  store.switchToSession(sessionId)
  router.push(`/chat/${sessionId}`)
}

async function handleDeleteSession(sessionId) {
  emit('close')
  await store.removeSession(sessionId)
  if (store.currentSessionId) {
    router.push(`/chat/${store.currentSessionId}`)
  } else {
    router.push('/chat')
  }
}

const showLogoutConfirm = ref(false)

function handleLogout() {
  emit('close')
  showLogoutConfirm.value = true
}

async function confirmLogout() {
  showLogoutConfirm.value = false
  await auth.logout()
  router.push('/login')
}

function handleGuestLogin() {
  emit('close')
  router.push('/login')
}

// ============================================
// 用户悬浮菜单
// ============================================
const showUserMenu = ref(false)

function toggleUserMenu() {
  showUserMenu.value = !showUserMenu.value
}

function onGlobalClick(e) {
  // 点击菜单外部时关闭
  if (showUserMenu.value && !e.target.closest('.user-menu') && !e.target.closest('.user-meta-clickable')) {
    showUserMenu.value = false
  }
}

onMounted(() => {
  document.addEventListener('click', onGlobalClick)
})

onBeforeUnmount(() => {
  document.removeEventListener('click', onGlobalClick)
})

// ============================================
// 修改个人信息
// ============================================
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
  showUserMenu.value = false
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
      // 同步更新本地用户信息
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

// ============================================
// 使用看板
// ============================================
const showUsageModal = ref(false)
const usageLoading = ref(false)
const usageError = ref('')
const usageData = ref(null)
const usageDays = ref(30)

const reversedDaily = computed(() => [...(usageData.value?.daily || [])].reverse())

async function loadUsage(days) {
  usageDays.value = days
  usageLoading.value = true
  usageError.value = ''
  try {
    const res = await getMyUsageStats(days)
    usageData.value = res.data
  } catch (err) {
    usageError.value = err.response?.data?.detail || '加载失败，请稍后重试'
  } finally {
    usageLoading.value = false
  }
}

function openUsageModal() {
  showUserMenu.value = false
  showUsageModal.value = true
  loadUsage(usageDays.value)
}

function formatChartDay(day) {
  // 'YYYY-MM-DD' -> 'MM-DD'
  return day ? day.slice(5) : ''
}

function showChartLabel(idx) {
  // 天数较多时隔段显示日期标签，避免重叠
  if (usageDays.value <= 14) return true
  if (usageDays.value <= 31) return idx % 3 === 0
  return idx % 7 === 0
}

function barHeight(count) {
  const max = Math.max(1, ...(usageData.value?.daily || []).map((r) => r.query_count))
  if (!count) return 3
  return Math.max(6, Math.round((count / max) * 100))
}

function chartTooltip(row) {
  return `${row.day}：提问 ${row.query_count} 次、知识库检索 ${row.knowledge_count} 次、联网搜索 ${row.web_search_count} 次、上传 ${row.upload_count} 次`
}
</script>

<style scoped>
.sidebar {
  position: fixed;
  left: 0;
  top: 0;
  width: var(--sidebar-width);
  height: 100vh;
  background: var(--color-sidebar);
  border-right: 1px solid var(--color-border);
  display: flex;
  flex-direction: column;
  z-index: 10;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 20px 16px;
  border-bottom: 1px solid var(--color-border);
}

.brand-icon {
  width: 36px;
  height: 36px;
  background: linear-gradient(135deg, #c98a4b, #e9bc7d);
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: white;
}

.leaf-icon {
  width: 22px;
  height: 22px;
}

.brand-title {
  font-size: 16px;
  font-weight: 700;
  color: var(--color-text);
  line-height: 1.2;
}

.brand-subtitle {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.actions {
  padding: 12px 12px 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.config-actions {
  border-bottom: 1px solid var(--color-border);
  padding-bottom: 12px;
  margin-bottom: 0;
}

.btn-new-chat,
.btn-upload {
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

.btn-new-chat:hover,
.btn-upload:hover {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
}

.btn-upload.active {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.btn-icon {
  width: 16px;
  height: 16px;
}

.history-section {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  padding: 8px 0;
}

.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px;
}

.section-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.session-count {
  font-size: 11px;
  color: var(--color-text-secondary);
  background: #ede6dc;
  padding: 1px 6px;
  border-radius: 8px;
}

.history-list {
  flex: 1;
  overflow-y: auto;
  padding: 0 8px;
}

.history-group {
  margin-bottom: 6px;
}

.history-group-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.15s;
  user-select: none;
}

.history-group-header:hover {
  background: #ede6dc;
}

.group-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-secondary);
  flex: 1;
}

.group-count {
  font-size: 11px;
  color: var(--color-text-secondary);
  background: #ede6dc;
  padding: 1px 6px;
  border-radius: 8px;
}

.group-arrow {
  display: flex;
  align-items: center;
  color: var(--color-text-secondary);
  transition: transform 0.2s;
}

.group-arrow.collapsed {
  transform: rotate(-90deg);
}

.history-group-items {
  padding-left: 4px;
}

.history-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 10px;
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.2s;
  margin-bottom: 2px;
  position: relative;
}

.history-item:hover {
  background: #ede6dc;
}

.history-item.active {
  background: var(--color-primary-light);
  border: 1px solid #e9bc7d;
}

.chat-icon {
  width: 16px;
  height: 16px;
  color: var(--color-text-secondary);
  flex-shrink: 0;
}

.item-title {
  font-size: 13px;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}

.btn-delete {
  opacity: 0;
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  border: none;
  background: transparent;
  cursor: pointer;
  color: var(--color-text-secondary);
  flex-shrink: 0;
  transition: all 0.2s;
}

.history-item:hover .btn-delete {
  opacity: 1;
}

.btn-delete:hover {
  background: #f9eddb;
  color: #c98a4b;
}

.empty-history {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px 20px;
  color: var(--color-text-secondary);
}

.empty-icon {
  width: 32px;
  height: 32px;
  margin-bottom: 8px;
  opacity: 0.4;
}

.empty-history p {
  font-size: 13px;
}

.empty-hint {
  font-size: 12px !important;
  margin-top: 4px;
  opacity: 0.6;
}

/* ---- 底部用户信息 ---- */
.sidebar-footer {
  border-top: 1px solid var(--color-border);
  flex-shrink: 0;
  position: relative;
}

.user-info {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
}

.user-avatar {
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

.user-meta {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.user-meta-clickable {
  cursor: pointer;
  border-radius: 8px;
  padding: 2px 6px;
  margin-left: -6px;
  transition: background 0.15s;
}

.user-meta-clickable:hover {
  background: #ede6dc;
}

.user-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.user-role {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.btn-logout {
  width: 30px;
  height: 30px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  border-radius: 8px;
  cursor: pointer;
  color: var(--color-text-secondary);
  transition: all 0.2s;
  flex-shrink: 0;
}

.btn-logout:hover {
  background: #fce4ec;
  color: #c62828;
}

/* ---- 游客模式提示 ---- */
.guest-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  background: linear-gradient(135deg, #faf8f5, #f3efe9);
  border-top: 1px solid var(--color-border);
}

.guest-banner-icon {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: #ede6dc;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #8c7e6e;
  flex-shrink: 0;
}

.guest-banner-text {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.guest-banner-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
}

.guest-banner-hint {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.btn-guest-login {
  padding: 6px 12px;
  background: linear-gradient(135deg, #c98a4b, #d9a76a);
  color: white;
  border: none;
  border-radius: 8px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s;
  flex-shrink: 0;
}

.btn-guest-login:hover {
  background: linear-gradient(135deg, #b0763c, #cf9a55);
}

/* ---- 移动端关闭按钮 ---- */
.sidebar-close-btn {
  display: none;
  position: absolute;
  top: 12px;
  right: 12px;
  width: 32px;
  height: 32px;
  border: none;
  border-radius: 8px;
  background: transparent;
  cursor: pointer;
  color: var(--color-text-secondary);
  align-items: center;
  justify-content: center;
  transition: all 0.2s;
  z-index: 1;
}

.sidebar-close-btn:hover {
  background: #ede6dc;
  color: var(--color-text);
}

/* ---- 移动端抽屉动画 ---- */
@media (max-width: 768px) {
  .sidebar {
    transform: translateX(-100%);
    transition: transform 0.28s cubic-bezier(0.4, 0, 0.2, 1);
    z-index: 20;
  }

  .sidebar.sidebar-open {
    transform: translateX(0);
  }

  .sidebar-close-btn {
    display: flex;
  }
}
/* ---- 退出登录确认弹窗 ---- */
.logout-confirm-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}

.logout-confirm-box {
  width: 320px;
  max-width: 90vw;
  background: var(--color-bg, #fff);
  border-radius: 14px;
  padding: 22px 24px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.18);
}

.logout-confirm-title {
  margin: 0 0 8px;
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text-primary, #333);
}

.logout-confirm-desc {
  margin: 0 0 18px;
  font-size: 13px;
  color: var(--color-text-secondary, #888);
}

.logout-confirm-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.logout-btn {
  padding: 7px 16px;
  font-size: 13px;
  border-radius: 8px;
  cursor: pointer;
  border: 1px solid transparent;
  transition: all 0.15s;
}

.logout-btn-cancel {
  background: transparent;
  border-color: var(--color-border, #e0d8cc);
  color: var(--color-text-secondary, #666);
}

.logout-btn-cancel:hover {
  background: rgba(0, 0, 0, 0.04);
}

.logout-btn-confirm {
  background: #c62828;
  color: #fff;
}

.logout-btn-confirm:hover {
  background: #b71c1c;
}

/* ---- 用户悬浮菜单 ---- */
.user-menu {
  position: absolute;
  left: 12px;
  right: 12px;
  bottom: calc(100% + 6px);
  background: #fff;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
  padding: 4px;
  z-index: 30;
}

.user-menu-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 9px 12px;
  border: none;
  background: transparent;
  border-radius: 8px;
  font-size: 13px;
  color: var(--color-text);
  cursor: pointer;
  transition: background 0.15s;
}

.user-menu-item:hover {
  background: var(--color-primary-light, #faf3e8);
  color: var(--color-primary, #c56d23);
}

/* ---- 通用弹窗 ---- */
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

/* ---- 使用看板 ---- */
.usage-box {
  width: 640px;
}

.usage-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.usage-header .modal-title {
  margin-bottom: 0;
}

.usage-days {
  display: flex;
  gap: 4px;
}

.usage-day-btn {
  padding: 4px 10px;
  font-size: 12px;
  border: 1px solid var(--color-border, #e0d8cc);
  background: #fff;
  border-radius: 6px;
  cursor: pointer;
  color: var(--color-text-secondary, #666);
  transition: all 0.15s;
}

.usage-day-btn.active {
  background: var(--color-primary-light, #faf3e8);
  border-color: var(--color-primary, #c98a4b);
  color: var(--color-primary, #c56d23);
}

.usage-loading {
  padding: 40px 0;
  text-align: center;
  color: var(--color-text-secondary, #888);
  font-size: 13px;
}

.usage-scroll {
  margin-top: 16px;
}

.usage-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(105px, 1fr));
  gap: 8px;
}

.usage-card {
  background: linear-gradient(135deg, #faf8f5, #f3efe9);
  border: 1px solid var(--color-border, #eee);
  border-radius: 10px;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.usage-card-num {
  font-size: 18px;
  font-weight: 700;
  color: var(--color-primary, #c56d23);
}

.usage-card-label {
  font-size: 11px;
  color: var(--color-text-secondary, #888);
}

.usage-section-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-secondary, #888);
  margin: 16px 0 8px;
}

.usage-chart {
  display: flex;
  align-items: flex-end;
  gap: 4px;
  height: 140px;
  padding: 8px 4px 20px;
  border-bottom: 1px solid var(--color-border, #e0d8cc);
  margin-bottom: 16px;
}

.chart-col {
  flex: 1;
  min-width: 10px;
  height: 100%;
  position: relative;
  display: flex;
  align-items: flex-end;
  justify-content: center;
}

.chart-bar {
  width: 70%;
  max-width: 18px;
  background: linear-gradient(180deg, #d9a76a, #c98a4b);
  border-radius: 3px 3px 0 0;
  transition: height 0.3s;
}

.chart-label {
  font-size: 9px;
  color: var(--color-text-secondary, #999);
  position: absolute;
  bottom: -16px;
  left: 50%;
  transform: translateX(-50%);
  white-space: nowrap;
}

.chart-empty {
  padding: 24px 0;
  text-align: center;
  font-size: 12px;
  color: var(--color-text-secondary, #999);
}

.usage-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.usage-table th,
.usage-table td {
  padding: 6px 8px;
  text-align: center;
  border-bottom: 1px solid var(--color-border, #eee);
  color: var(--color-text, #333);
}

.usage-table th {
  color: var(--color-text-secondary, #888);
  font-weight: 600;
}

@media (max-width: 768px) {
  .usage-box {
    width: 92vw;
  }
}
</style>
