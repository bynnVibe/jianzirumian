import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const routes = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/views/LoginView.vue'),
    meta: { guest: true },
  },
  {
    path: '/register',
    name: 'Register',
    component: () => import('@/views/RegisterView.vue'),
    meta: { guest: true },
  },
  {
    path: '/guide',
    name: 'Guide',
    component: () => import('@/views/GuideView.vue'),
  },
  {
    path: '/',
    redirect: '/chat',
  },
  {
    path: '/chat',
    name: 'Chat',
    component: () => import('@/views/ChatView.vue'),
    meta: { requiresAuth: true, allowGuest: true },
  },
  {
    path: '/chat/:sessionId',
    name: 'ChatWithSession',
    component: () => import('@/views/ChatView.vue'),
    meta: { requiresAuth: true, allowGuest: true },
  },
  {
    path: '/upload',
    name: 'KnowledgeUpload',
    component: () => import('@/views/KnowledgeUpload.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/ingest-detail/:recordId',
    name: 'IngestDetail',
    component: () => import('@/views/IngestDetailView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/settings',
    redirect: '/management',
  },
  {
    path: '/management',
    name: 'Management',
    component: () => import('@/views/ManagementView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/knowledge',
    name: 'KnowledgeBase',
    component: () => import('@/views/ManagementView.vue'),
    meta: { requiresAuth: true, allowGuest: true },
  },
  {
    path: '/doc-preview',
    name: 'DocPreview',
    component: () => import('@/views/DocPreviewView.vue'),
    meta: { requiresAuth: true, allowGuest: true },
  },
  {
    path: '/attachment-preview',
    name: 'AttachmentPreview',
    component: () => import('@/views/AttachmentPreviewView.vue'),
    meta: { requiresAuth: true, allowGuest: true },
  },
  {
    path: '/bookmarks',
    name: 'Bookmarks',
    component: () => import('@/views/BookmarksView.vue'),
    meta: { requiresAuth: true, allowGuest: true },
  },
  {
    path: '/wiki',
    name: 'Wiki',
    component: () => import('@/views/WikiView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/admin/users',
    name: 'UserManagement',
    component: () => import('@/views/UserManagementView.vue'),
    meta: { requiresAuth: true, requiresAdmin: true },
  },
  {
    path: '/eval',
    name: 'Eval',
    component: () => import('@/views/EvalView.vue'),
    meta: { requiresAuth: true, requiresAdmin: true },
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

// ---- 路由守卫 ----
router.beforeEach(async (to, from, next) => {
  const auth = useAuthStore()

  // 需要登录的路由：如果 store 未初始化，从 localStorage 恢复并向后端验证
  if (to.meta.requiresAuth && !auth.isAuthenticated) {
    // 游客模式下允许访问带有 allowGuest 标记的路由
    if (auth.isGuest && to.meta.allowGuest) {
      next()
      return
    }

    if (localStorage.getItem('auth_token')) {
      await auth.init()
    }
    if (!auth.isAuthenticated) {
      // 游客访问受限页面，引导到登录页
      if (auth.isGuest && !to.meta.allowGuest) {
        next({ name: 'Login' })
        return
      }
      next({ name: 'Login', query: { redirect: to.fullPath } })
      return
    }
  }

  // 需要管理员角色
  if (to.meta.requiresAdmin && !auth.isAdmin) {
    next({ name: 'Chat' })
    return
  }

  // 游客页面（登录/注册）：已登录则跳到首页
  if (to.meta.guest && auth.isAuthenticated) {
    next({ name: 'Chat' })
    return
  }

  next()
})

export default router
