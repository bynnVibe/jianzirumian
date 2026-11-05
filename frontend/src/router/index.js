import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import AdminConsoleLayout from '@/components/AdminConsoleLayout.vue'
import KnowledgeHubLayout from '@/components/KnowledgeHubLayout.vue'

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
    path: '/forgot-password',
    name: 'ForgotPassword',
    component: () => import('@/views/ForgotPasswordView.vue'),
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
    path: '/skills',
    name: 'Skills',
    component: () => import('@/views/SkillsView.vue'),
    meta: { requiresAuth: true, allowGuest: false },
  },
  {
    path: '/ingest-detail/:recordId',
    name: 'IngestDetail',
    component: () => import('@/views/IngestDetailView.vue'),
    meta: { requiresAuth: true },
  },
  // 旧路径兼容重定向（系统管理已迁移到 /admin 控制台）
  {
    path: '/settings',
    redirect: '/admin/settings',
  },
  {
    path: '/management',
    redirect: '/admin/settings',
  },
  // ============ 知识库控制台（独立布局 + 专属左侧导航，参考系统管理控制台逻辑） ============
  // 收纳知识库管理/知识收藏/知识百科三个功能界面；进入后只展示知识库相关功能。
  {
    path: '/kb',
    component: KnowledgeHubLayout,
    meta: { requiresAuth: true, allowGuest: true },
    redirect: { name: 'KnowledgeBase' },
    children: [
      {
        path: 'management',
        name: 'KnowledgeBase',
        component: () => import('@/views/ManagementView.vue'),
        meta: { requiresAuth: true, allowGuest: true },
      },
      {
        path: 'bookmarks',
        name: 'Bookmarks',
        component: () => import('@/views/BookmarksView.vue'),
        meta: { requiresAuth: true, allowGuest: true },
      },
      {
        path: 'wiki',
        name: 'Wiki',
        component: () => import('@/views/WikiView.vue'),
        meta: { requiresAuth: true, allowGuest: false },
      },
      {
        path: 'upload',
        name: 'KnowledgeUpload',
        component: () => import('@/views/KnowledgeUpload.vue'),
        meta: { requiresAuth: true, allowGuest: false },
      },
    ],
  },
  // 旧扁平路径兼容重定向到知识库控制台
  { path: '/knowledge', redirect: '/kb/management' },
  { path: '/bookmarks', redirect: '/kb/bookmarks' },
  { path: '/wiki', redirect: '/kb/wiki' },
  { path: '/upload', redirect: '/kb/upload' },
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
  // （/bookmarks 与 /wiki 已并入 /kb 控制台，见上方重定向）
  // ============ 系统管理控制台（独立布局 + 专属左侧导航） ============
  // 收纳所有管理/运维模块；admin 与 ops 均可进（requiresConsole=canOps），
  // 各子页再按 requiresAdmin / requiresOps / requiresOpsOnly 细分权限。
  {
    path: '/admin',
    component: AdminConsoleLayout,
    meta: { requiresAuth: true, requiresConsole: true },
    redirect: () => (useAuthStore().isOps ? { name: 'Pipeline' } : { name: 'AdminSettings' }),
    children: [
      {
        path: 'settings',
        name: 'AdminSettings',
        component: () => import('@/views/ManagementView.vue'),
        meta: { requiresAuth: true, requiresAdmin: true },
      },
      {
        path: 'users',
        name: 'UserManagement',
        component: () => import('@/views/UserManagementView.vue'),
        meta: { requiresAuth: true, requiresAdmin: true },
      },
      {
        path: 'pipeline',
        name: 'Pipeline',
        component: () => import('@/views/PipelineView.vue'),
        meta: { requiresAuth: true, requiresOpsOnly: true },
      },
      {
        path: 'gateway',
        name: 'Gateway',
        component: () => import('@/views/GatewayView.vue'),
        meta: { requiresAuth: true, requiresOps: true },
      },
      {
        path: 'eval',
        name: 'Eval',
        component: () => import('@/views/EvalView.vue'),
        meta: { requiresAuth: true, requiresOps: true },
      },
      {
        path: 'observability',
        name: 'Observability',
        component: () => import('@/views/ObservabilityView.vue'),
        meta: { requiresAuth: true, requiresOps: true },
      },
    ],
  },
  // 旧运维/管理扁平路径兼容重定向
  { path: '/pipeline', redirect: '/admin/pipeline' },
  { path: '/gateway', redirect: '/admin/gateway' },
  { path: '/eval', redirect: '/admin/eval' },
  {
    path: '/observability',
    name: 'MyObservability',
    component: () => import('@/views/ObservabilityView.vue'),
    meta: { requiresAuth: true },
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

// 运维角色（role==='ops'）可访问的路由白名单：
// CI/CD 流水线 + 模型网关 + 回归评测 + 可观测性 + 认证相关页（登录/注册/忘记密码/指南）
const OPS_ALLOWED_ROUTES = [
  'Pipeline', 'Gateway', 'Eval', 'Observability',
  'Login', 'Register', 'ForgotPassword', 'Guide',
]

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

  // 普通用户使用独立业务视图，管理员/运维仍进入技术控制台。
  if (to.name === 'MyObservability' && auth.canOps) {
    next({ name: 'Observability' })
    return
  }

  // 需要进入系统管理控制台（管理员或运维角色）
  if (to.meta.requiresConsole && !auth.canOps) {
    next({ name: 'Chat' })
    return
  }

  // 需要管理员角色
  if (to.meta.requiresAdmin && !auth.isAdmin) {
    next({ name: 'Chat' })
    return
  }

  // 需要运维权限（管理员或运维角色）
  if (to.meta.requiresOps && !auth.canOps) {
    next({ name: 'Chat' })
    return
  }

  // 仅限运维角色（CI/CD 流水线）：管理员同样不可访问
  if (to.meta.requiresOpsOnly && !auth.isOps) {
    next({ name: 'Chat' })
    return
  }

  // 运维角色白名单：ops 仅可访问 CI/CD、回归评测、可观测性与认证页，
  // 其余（知识库/上传/百科/对话等）一律重定向到流水线首页
  if (auth.isOps && !OPS_ALLOWED_ROUTES.includes(to.name)) {
    next({ name: 'Pipeline' })
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
