<template>
  <div class="kb-detail">
    <div v-if="loading" class="full-state">加载中…</div>
    <div v-else-if="error" class="full-state error">
      <p>{{ error }}</p>
      <button class="btn-ghost" @click="goOverview">返回知识库总览</button>
    </div>

    <template v-else-if="kb">
      <!-- 头部：返回 + 名称 + 统计 + 上传 -->
      <header class="detail-header">
        <div class="header-left">
          <button class="back-btn" title="返回知识库总览" @click="goOverview">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
              <line x1="19" y1="12" x2="5" y2="12" /><polyline points="12 19 5 12 12 5" />
            </svg>
          </button>
          <div class="header-text">
            <div class="kb-title-row">
              <h1>{{ kb.name }}</h1>
              <span class="vis-badge" :class="kb.visibility">{{ kb.visibility === 'public' ? '公共' : '个人' }}</span>
              <span class="topic-tag">{{ kb.topic }}</span>
            </div>
            <div class="kb-stats">
              {{ kb.doc_count }} 文档 / {{ kb.entry_count }} 分片 / {{ formatTokens(kb.token_estimate) }} Token
            </div>
          </div>
        </div>
        <button v-if="!auth.isGuest" class="btn-upload" @click="openUpload">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12" />
          </svg>
          上传文档
        </button>
      </header>

      <!-- 选项卡 -->
      <nav class="tab-bar">
        <button
          v-for="t in visibleTabs" :key="t.key"
          class="tab-item" :class="{ active: tab === t.key }"
          @click="setTab(t.key)"
        >{{ t.label }}</button>
      </nav>

      <!-- 选项卡内容 -->
      <div class="tab-body">
        <KbDocumentsPanel
          v-if="tab === 'documents'"
          :kb-id="kbId" :is-guest="auth.isGuest" :focus-source="focusSource"
          @upload="openUpload" @changed="refreshStats"
        />
        <KbRetrievalPanel v-else-if="tab === 'retrieval'" :kb-id="kbId" />
        <KbMindmapPanel v-else-if="tab === 'mindmap'" :kb-id="kbId" @jump-doc="onJumpDoc" />
        <KbGraphPanel v-else-if="tab === 'graph'" :kb-id="kbId" />
        <BookmarksView v-else-if="tab === 'bookmarks'" embedded />
        <WikiView v-else-if="tab === 'wiki'" embedded :kb-id="kbId" />
        <KbEvalPanel v-else-if="tab === 'eval'" :kb-id="kbId" />
      </div>
    </template>

    <!-- 上传文档弹窗（复用知识库上传界面，锁定当前知识库） -->
    <Teleport to="body">
      <div v-if="uploadVisible" class="upload-mask" @click.self="closeUpload">
        <div class="upload-modal">
          <div class="upload-modal-head">
            <h3>上传文档<span v-if="kb" class="upload-kb-name">· {{ kb.name }}</span></h3>
            <button class="modal-close" title="关闭" @click="closeUpload">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
                <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
              </svg>
            </button>
          </div>
          <div class="upload-modal-body">
            <KnowledgeUpload
              ref="uploadRef"
              embedded
              :preset-kb-id="kbId"
              @open-history-detail="ingestDetail = $event"
            />
          </div>
        </div>
      </div>

      <!-- 嵌套：入库详情弹窗 -->
      <div v-if="ingestDetail" class="upload-mask nested" @click.self="ingestDetail = null">
        <div class="upload-modal">
          <IngestDetailView
            embedded
            :record-id="ingestDetail.recordId"
            :src="ingestDetail.src"
            @back="ingestDetail = null"
          />
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { listKnowledgeBases } from '@/api'
import KbDocumentsPanel from '@/components/kb/KbDocumentsPanel.vue'
import KbRetrievalPanel from '@/components/kb/KbRetrievalPanel.vue'
import KbMindmapPanel from '@/components/kb/KbMindmapPanel.vue'
import KbGraphPanel from '@/components/kb/KbGraphPanel.vue'
import KbEvalPanel from '@/components/kb/KbEvalPanel.vue'
import BookmarksView from '@/views/BookmarksView.vue'
import WikiView from '@/views/WikiView.vue'
import KnowledgeUpload from '@/views/KnowledgeUpload.vue'
import IngestDetailView from '@/views/IngestDetailView.vue'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const ALL_TABS = [
  { key: 'documents', label: '文档' },
  { key: 'retrieval', label: '检索测试' },
  { key: 'mindmap', label: '思维导图' },
  { key: 'graph', label: '知识图谱' },
  { key: 'bookmarks', label: '知识收藏' },
  { key: 'wiki', label: '知识百科' },
  { key: 'eval', label: 'RAG评估' },
]
const GUEST_TABS = ['documents', 'retrieval']

const kbId = computed(() => String(route.params.kbId || ''))
const visibleTabs = computed(() =>
  auth.isGuest ? ALL_TABS.filter((t) => GUEST_TABS.includes(t.key)) : ALL_TABS,
)

const kb = ref(null)
const loading = ref(true)
const error = ref('')
const tab = ref('documents')

const uploadVisible = ref(false)
const uploadRef = ref(null)
const ingestDetail = ref(null)
const focusSource = ref('')

// 思维导图节点点击跳转：切到「文档」选项卡并聚焦对应来源文档
async function onJumpDoc(title) {
  if (!title) return
  focusSource.value = ''
  setTab('documents')
  await nextTick()
  focusSource.value = title
}

function formatTokens(n) {
  if (!n) return '0'
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`
  return String(n)
}

function goOverview() {
  router.push('/kb')
}

function setTab(key) {
  tab.value = key
  router.replace({ query: { ...route.query, tab: key } })
}

async function loadKb() {
  loading.value = true
  error.value = ''
  try {
    const res = await listKnowledgeBases()
    const found = (res.data.bases || []).find((b) => b.id === kbId.value)
    if (!found) {
      kb.value = null
      error.value = '知识库不存在或你没有访问权限'
      return
    }
    kb.value = found
    const q = String(route.query.tab || '')
    tab.value = visibleTabs.value.some((t) => t.key === q) ? q : 'documents'
  } catch (e) {
    error.value = e.response?.data?.detail || '加载知识库失败'
  } finally {
    loading.value = false
  }
}

async function refreshStats() {
  try {
    const res = await listKnowledgeBases()
    const found = (res.data.bases || []).find((b) => b.id === kbId.value)
    if (found) kb.value = found
  } catch (e) {
    console.error('刷新知识库统计失败:', e)
  }
}

function openUpload() {
  uploadVisible.value = true
}

function closeUpload() {
  if (uploadRef.value && !uploadRef.value.allowLeave()) return
  uploadVisible.value = false
  ingestDetail.value = null
  refreshStats()
}

watch(kbId, (id, old) => {
  if (id && id !== old) {
    tab.value = 'documents'
    loadKb()
  }
}, { immediate: true })

watch(() => route.query.tab, (q) => {
  const key = String(q || '')
  if (visibleTabs.value.some((t) => t.key === key)) tab.value = key
})
</script>

<style scoped>
.kb-detail { display: flex; flex-direction: column; height: 100%; background: var(--color-bg); }

.full-state {
  flex: 1; display: flex; flex-direction: column; gap: 14px;
  align-items: center; justify-content: center;
  color: var(--color-text-secondary); font-size: 14px;
}
.full-state.error { color: #c0392b; }

.detail-header {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  padding: 20px 28px 14px;
}
.header-left { display: flex; align-items: center; gap: 14px; min-width: 0; }
.back-btn {
  width: 36px; height: 36px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  border: 1px solid var(--color-border); border-radius: 9px; background: #fff;
  color: var(--color-text); cursor: pointer;
}
.back-btn:hover { border-color: var(--color-primary); color: var(--color-primary); }
.header-text { min-width: 0; }
.kb-title-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.kb-title-row h1 {
  margin: 0; font-size: 20px; font-weight: 700; color: var(--color-text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 420px;
}
.vis-badge { font-size: 11px; padding: 2px 8px; border-radius: 6px; background: #e8f5e9; color: #2e7d32; }
.vis-badge.private { background: var(--color-primary-light); color: var(--color-primary); }
.topic-tag { font-size: 11px; padding: 2px 8px; border-radius: 6px; background: #f3efe9; color: var(--color-text-secondary); }
.kb-stats { margin-top: 4px; font-size: 12.5px; color: var(--color-text-secondary); }

.btn-upload {
  display: inline-flex; align-items: center; gap: 6px; flex-shrink: 0;
  background: var(--color-text); color: #fff; border: none; border-radius: 8px;
  padding: 9px 18px; font-size: 13px; font-weight: 500; cursor: pointer;
  white-space: nowrap;
}
.btn-upload:hover { opacity: 0.85; }

.tab-bar {
  display: flex; gap: 4px; padding: 0 28px;
  border-bottom: 1px solid var(--color-border); flex-shrink: 0;
}
.tab-item {
  border: none; background: none; cursor: pointer;
  padding: 10px 16px; font-size: 13.5px; color: var(--color-text-secondary);
  border-bottom: 2px solid transparent; margin-bottom: -1px;
}
.tab-item:hover { color: var(--color-text); }
.tab-item.active { color: var(--color-primary); border-bottom-color: var(--color-primary); font-weight: 600; }

.tab-body { flex: 1; min-height: 0; }

.btn-ghost {
  background: transparent; border: 1px solid var(--color-border); border-radius: 8px;
  padding: 8px 16px; font-size: 13px; color: var(--color-text); cursor: pointer;
}

.upload-mask {
  position: fixed; inset: 0; z-index: 200;
  background: rgba(45, 42, 36, 0.45);
  display: flex; align-items: center; justify-content: center; padding: 24px;
}
.upload-mask.nested { z-index: 210; background: rgba(45, 42, 36, 0.35); }
.upload-modal {
  width: min(1080px, 96vw); height: 88vh;
  background: var(--color-bg); border-radius: 14px; overflow: hidden;
  display: flex; flex-direction: column;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.25);
}
.upload-modal-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 20px; border-bottom: 1px solid var(--color-border); flex-shrink: 0;
}
.upload-modal-head h3 { margin: 0; font-size: 15px; color: var(--color-text); }
.upload-kb-name { color: var(--color-text-secondary); font-weight: 400; font-size: 13px; margin-left: 6px; }
.modal-close {
  width: 30px; height: 30px; display: flex; align-items: center; justify-content: center;
  border: none; background: transparent; border-radius: 8px; cursor: pointer; color: var(--color-text-secondary);
}
.modal-close:hover { background: #f3efe9; color: var(--color-text); }
.upload-modal-body { flex: 1; min-height: 0; }
</style>