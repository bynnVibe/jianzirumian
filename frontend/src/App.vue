<template>
  <div class="app-layout">
    <!-- 移动端：汉堡菜单按钮 -->
    <button
      v-if="showSidebar"
      class="hamburger-btn"
      :class="{ open: sidebarOpen }"
      @click="sidebarOpen = !sidebarOpen"
      aria-label="菜单"
    >
      <span></span>
      <span></span>
      <span></span>
    </button>

    <!-- 移动端：遮罩层 -->
    <div
      v-if="showSidebar && sidebarOpen && isMobile"
      class="sidebar-overlay"
      @click="sidebarOpen = false"
    ></div>

    <AppSidebar
      v-if="showSidebar"
      :open="sidebarOpen"
      @close="sidebarOpen = false"
    />
    <main class="main-content" :class="{ 'no-sidebar': !showSidebar, 'sidebar-open': sidebarOpen }">
      <router-view />
    </main>

    <!-- 知识助手：非对话页面右侧可折叠问答栏（组件内部按路由/登录态自行决定显隐） -->
    <WikiAssistant />
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import AppSidebar from '@/components/AppSidebar.vue'
import WikiAssistant from '@/components/WikiAssistant.vue'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const auth = useAuthStore()

const sidebarOpen = ref(false)

// 是否移动端
const isMobile = ref(window.innerWidth < 768)

function handleResize() {
  isMobile.value = window.innerWidth < 768
  if (!isMobile.value) {
    sidebarOpen.value = false
  }
}

// 登录/注册/使用指南页不显示侧边栏（指南页免登录，不能有触发 401 的请求）
// 独立新页打开的预览页（文档/附件）同样不显示侧边栏，保证沉浸式阅读
const showSidebar = computed(() => {
  return !['Login', 'Register', 'Guide', 'DocPreview', 'AttachmentPreview'].includes(route.name)
})

// ---- 5 分钟无操作自动登出 ----
let inactivityTimer = null
const INACTIVITY_LIMIT = 5 * 60 * 1000

function resetInactivityTimer() {
  if (inactivityTimer) {
    clearTimeout(inactivityTimer)
  }
  if (auth.isAuthenticated) {
    inactivityTimer = setTimeout(() => {
      auth.logout()
      window.location.href = '/login'
    }, INACTIVITY_LIMIT)
  }
}

function handleActivity() {
  resetInactivityTimer()
}

onMounted(() => {
  // 同步恢复登录态（从 localStorage），不阻塞 UI 渲染
  auth.init()

  if (auth.isAuthenticated) {
    resetInactivityTimer()
  }

  window.addEventListener('resize', handleResize)
  document.addEventListener('click', handleActivity)
  document.addEventListener('keydown', handleActivity)
  document.addEventListener('mousemove', handleActivity)
  document.addEventListener('scroll', handleActivity, { passive: true })
})

onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
  if (inactivityTimer) {
    clearTimeout(inactivityTimer)
  }
  document.removeEventListener('click', handleActivity)
  document.removeEventListener('keydown', handleActivity)
  document.removeEventListener('mousemove', handleActivity)
  document.removeEventListener('scroll', handleActivity)
})
</script>

<style>
:root {
  --sidebar-width: 260px;
  --color-bg: #fefcf9;
  --color-sidebar: #faf6f0;
  --color-primary: #c98a4b;
  --color-primary-light: #f9eddb;
  --color-text: #2d2a24;
  --color-text-secondary: #8a7e72;
  --color-border: #ede6dc;
}

* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  background: var(--color-bg);
  color: var(--color-text);
  -webkit-font-smoothing: antialiased;
}

.app-layout {
  display: flex;
  height: 100vh;
  width: 100vw;
}

.main-content {
  flex: 1;
  margin-left: var(--sidebar-width);
  height: 100vh;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.main-content.no-sidebar {
  margin-left: 0;
}

/* ---- 汉堡菜单按钮 ---- */
.hamburger-btn {
  display: none;
  position: fixed;
  top: 12px;
  left: 12px;
  z-index: 30;
  width: 36px;
  height: 36px;
  border: none;
  border-radius: 8px;
  background: white;
  box-shadow: 0 1px 4px rgba(0,0,0,0.12);
  cursor: pointer;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 5px;
  padding: 8px;
  transition: background 0.2s;
}

.hamburger-btn:hover {
  background: var(--color-primary-light);
}

.hamburger-btn span {
  display: block;
  width: 20px;
  height: 2px;
  background: var(--color-text);
  border-radius: 2px;
  transition: all 0.3s ease;
  transform-origin: center;
}

.hamburger-btn.open span:nth-child(1) {
  transform: translateY(7px) rotate(45deg);
}

.hamburger-btn.open span:nth-child(2) {
  opacity: 0;
}

.hamburger-btn.open span:nth-child(3) {
  transform: translateY(-7px) rotate(-45deg);
}

/* ---- 移动端遮罩层 ---- */
.sidebar-overlay {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.35);
  z-index: 8;
  animation: fadeIn 0.25s ease;
}

@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}

/* ---- 移动端适配 ---- */
@media (max-width: 768px) {
  .hamburger-btn {
    display: flex;
  }

  .sidebar-overlay {
    display: block;
  }

  .app-layout {
    position: relative;
  }

  .main-content {
    margin-left: 0;
    width: 100vw;
  }

  .main-content.sidebar-open {
    /* 内容区不滑动，由侧边栏抽屉动画处理 */
  }
}
</style>
