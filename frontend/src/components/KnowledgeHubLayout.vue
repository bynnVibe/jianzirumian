<template>
  <!--
    知识库控制台布局：参考「系统管理」控制台的独立布局逻辑。
    左侧为知识库专属导航（知识库管理/知识收藏/知识百科），右侧顶栏 + 嵌套子页 router-view。
    进入后只承载知识库相关功能界面，与正常使用侧边栏分离（App.vue 对 /kb/* 隐藏主侧边栏）。
  -->
  <div class="kb-console">
    <!-- ============ 左侧专属导航 ============ -->
    <aside class="console-nav">
      <div class="console-brand">
        <div class="console-brand-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20">
            <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
          </svg>
        </div>
        <div>
          <h1 class="console-brand-title">知识库</h1>
          <p class="console-brand-subtitle">见字如面 · 知识中心</p>
        </div>
      </div>

      <nav class="console-menu">
        <!-- 知识库管理 -->
        <router-link to="/kb/management" class="console-item" :class="{ active: route.name === 'KnowledgeBase' }">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="console-item-icon">
            <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
          </svg>
          知识库管理
        </router-link>

        <!-- 知识收藏 -->
        <router-link to="/kb/bookmarks" class="console-item" :class="{ active: route.name === 'Bookmarks' }">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="console-item-icon">
            <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>
          </svg>
          知识收藏
        </router-link>

        <!-- 知识百科（登录用户可见，游客不可见） -->
        <router-link v-if="!auth.isGuest" to="/kb/wiki" class="console-item" :class="{ active: route.name === 'Wiki' }">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="console-item-icon">
            <path d="M12 2l3 6 7 1-5 5-5 5-5-5-5-5 7-1z" transform="translate(0 1) scale(0.92)"/>
          </svg>
          知识百科
        </router-link>

        <!-- 知识库上传（登录用户可见，游客不可见） -->
        <router-link v-if="!auth.isGuest" to="/kb/upload" class="console-item" :class="{ active: route.name === 'KnowledgeUpload' }">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="console-item-icon">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>
          </svg>
          知识库上传
        </router-link>
      </nav>

      <!-- 底部用户条 -->
      <div class="console-footer">
        <div class="console-user">
          <div class="console-avatar">{{ (auth.username || '客').charAt(0).toUpperCase() }}</div>
          <div class="console-user-meta">
            <span class="console-user-name">{{ auth.username || '游客' }}</span>
            <span class="console-user-role" :class="roleClass">{{ roleLabel }}</span>
          </div>
        </div>
      </div>
    </aside>

    <!-- ============ 右侧主体 ============ -->
    <section class="console-main">
      <header class="console-topbar">
        <h2 class="console-topbar-title">{{ currentTitle }}</h2>
        <button class="console-back-btn" @click="router.push('/chat')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
            <line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>
          </svg>
          返回对话
        </button>
      </header>
      <div class="console-body">
        <router-view />
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()

const roleLabel = computed(() =>
  auth.isAdmin ? '管理员' : (auth.isOps ? '运维' : (auth.isGuest ? '游客' : '普通用户'))
)
const roleClass = computed(() =>
  auth.isAdmin ? 'role-admin' : (auth.isOps ? 'role-ops' : 'role-user')
)

// 顶栏标题跟随当前子路由
const TITLE_MAP = {
  KnowledgeBase: '知识库管理',
  Bookmarks: '知识收藏',
  Wiki: '知识百科',
  KnowledgeUpload: '知识库上传',
}
const currentTitle = computed(() => TITLE_MAP[route.name] || '知识库')
</script>

<style scoped>
.kb-console {
  display: flex;
  height: 100%;
  width: 100%;
  overflow: hidden;
}

/* ---- 左侧专属导航 ---- */
.console-nav {
  width: 220px;
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

/* ---- 移动端：导航收窄 ---- */
@media (max-width: 768px) {
  .console-nav {
    width: 180px;
  }
}
</style>
