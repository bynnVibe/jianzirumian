<template>
  <div class="wiki-page">
    <div class="page-header">
      <div>
        <h2>知识百科</h2>
        <p class="page-desc">笔记入库后由系统自动编译的结构化词条；提问时检索会优先命中这些百科卡片</p>
      </div>
      <span class="wiki-count" v-if="wikiPages.length">共 {{ wikiPages.length }} 条</span>
    </div>

    <!-- 筛选工具栏 -->
    <div class="toolbar">
      <select v-model="typeFilter" class="filter-select" @change="loadPages">
        <option value="">全部类型</option>
        <option value="source">来源卡片</option>
        <option value="digest">综合词条</option>
      </select>
      <select v-model="kbFilter" class="filter-select" @change="loadPages">
        <option value="">全部知识库</option>
        <option v-for="kb in kbList" :key="kb.id" :value="kb.id">{{ kb.name }}</option>
      </select>
      <button class="refresh-btn" @click="loadPages" :disabled="loading">刷新</button>
    </div>

    <!-- 加载状态 -->
    <div v-if="loading" class="loading-state">
      <div class="spinner"></div>
      <p>加载百科列表...</p>
    </div>

    <!-- 空状态 -->
    <div v-else-if="wikiPages.length === 0" class="empty-state">
      <p>暂无百科词条</p>
      <p class="empty-hint">在「知识库上传」入库后，系统会在后台自动编译生成百科词条</p>
    </div>

    <!-- 词条列表 -->
    <div v-else class="wiki-list">
      <div v-for="p in wikiPages" :key="p.id" class="wiki-item">
        <div class="wiki-item-head">
          <span class="wiki-type-tag" :class="p.page_type">
            {{ p.page_type === 'digest' ? '综合词条' : '来源卡片' }}
          </span>
          <button class="wiki-title" @click="openPage(p)">{{ p.title }}</button>
          <span v-if="p.visibility === 'private'" class="wiki-private-badge">私人</span>
          <span class="wiki-kb-name">{{ kbName(p.kb_id) }}</span>
        </div>
        <p class="wiki-excerpt">{{ p.excerpt }}</p>
        <div class="wiki-item-foot">
          <span class="wiki-time">{{ formatTime(p.updated_at) }}</span>
          <div class="wiki-actions">
            <button class="btn-link" @click="openPage(p)">查看</button>
            <button v-if="canDelete(p)" class="btn-link danger" @click="deleteTarget = p">删除</button>
          </div>
        </div>
      </div>
    </div>

    <!-- 词条详情弹窗 -->
    <div v-if="detailPage" class="modal-overlay" @click.self="detailPage = null">
      <div class="modal-box wiki-detail-box">
        <div class="modal-head">
          <h3>{{ detailPage.title }}</h3>
          <button class="modal-close" @click="detailPage = null">&times;</button>
        </div>
        <div class="modal-body markdown-content" v-html="renderedDetail"></div>
      </div>
    </div>

    <!-- 删除确认弹窗 -->
    <div v-if="deleteTarget" class="modal-overlay" @click.self="deleteTarget = null">
      <div class="modal-box modal-sm">
        <div class="modal-head">
          <h3>删除百科词条</h3>
          <button class="modal-close" @click="deleteTarget = null">&times;</button>
        </div>
        <div class="modal-body">
          <p>确定删除「{{ deleteTarget.title }}」吗？其向量条目也会一并删除。</p>
        </div>
        <div class="modal-foot">
          <button class="btn-cancel" @click="deleteTarget = null">取消</button>
          <button class="btn-confirm-danger" :disabled="deleting" @click="doDelete">
            {{ deleting ? '删除中...' : '删除' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import MarkdownIt from 'markdown-it'
import { getWikiPages, getWikiPage, deleteWikiPage, listKnowledgeBases } from '@/api'
import { useAuthStore } from '@/stores/auth'
import { useAssistantStore } from '@/stores/assistant'

const authStore = useAuthStore()
const assistant = useAssistantStore()
const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

const wikiPages = ref([])
const kbList = ref([])
const loading = ref(false)
const typeFilter = ref('')
const kbFilter = ref('')
const detailPage = ref(null)
const deleteTarget = ref(null)
const deleting = ref(false)

const renderedDetail = computed(() => (detailPage.value ? md.render(detailPage.value.content || '') : ''))

function kbName(id) {
  const kb = kbList.value.find((k) => k.id === id)
  return kb ? kb.name : ''
}

function formatTime(t) {
  if (!t) return ''
  return String(t).replace('T', ' ').slice(0, 16)
}

/** 与后端删除权限一致：管理员全权；私人词条属主可删；公共综合词条登录用户可删 */
function canDelete(p) {
  if (authStore.isAdmin) return true
  if (p.visibility === 'private') return p.owner_id === authStore.user?.id
  return p.page_type === 'digest'
}

async function loadPages() {
  loading.value = true
  try {
    const params = {}
    if (typeFilter.value) params.page_type = typeFilter.value
    if (kbFilter.value) params.kb_id = kbFilter.value
    const res = await getWikiPages(params)
    wikiPages.value = res.data.pages || []
  } catch (e) {
    console.error('加载百科列表失败:', e)
  } finally {
    loading.value = false
  }
}

async function openPage(p) {
  try {
    const res = await getWikiPage(p.id)
    detailPage.value = res.data.page || p
  } catch (e) {
    console.error('加载词条详情失败:', e)
  }
}

async function doDelete() {
  if (!deleteTarget.value) return
  deleting.value = true
  try {
    await deleteWikiPage(deleteTarget.value.id)
    deleteTarget.value = null
    await loadPages()
  } catch (e) {
    console.error('删除词条失败:', e)
  } finally {
    deleting.value = false
  }
}

onMounted(async () => {
  try {
    const res = await listKnowledgeBases()
    kbList.value = res.data.bases || []
  } catch (e) {
    console.error('加载知识库列表失败:', e)
  }
  await loadPages()
  window.addEventListener('wiki:page-updated', onWikiPageUpdated)
})

onUnmounted(() => {
  window.removeEventListener('wiki:page-updated', onWikiPageUpdated)
  assistant.clearPageContext()
  assistant.clearOpenDoc()
})

// 打开/关闭词条详情时同步知识助手的「当前页面」上下文
watch(detailPage, (p) => {
  if (p) {
    assistant.setContext({ page_id: p.id, page_title: p.title })
    // 直接注入正在阅读的词条正文，助手无需再调用工具即可结合内容作答
    assistant.setOpenDoc({ title: p.title || '', content: p.content || '' })
  } else {
    assistant.clearPageContext()
    assistant.clearOpenDoc()
  }
})

// 知识助手应用编辑后刷新列表与已打开的详情
function onWikiPageUpdated() {
  loadPages()
  if (detailPage.value) {
    getWikiPage(detailPage.value.id)
      .then((res) => {
        if (res.data.page) detailPage.value = res.data.page
      })
      .catch(() => {})
  }
}

</script>

<style scoped>
.wiki-page {
  padding: 24px 28px;
  height: 100%;
  overflow-y: auto;
}

.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 16px;
}

.page-header h2 {
  font-size: 20px;
  font-weight: 700;
  color: #4a3f35;
  margin: 0 0 4px;
}

.page-desc {
  font-size: 12.5px;
  color: #8d7f6f;
  margin: 0;
}

.wiki-count {
  font-size: 12px;
  color: #8d7f6f;
  background: #f0e9df;
  border-radius: 999px;
  padding: 4px 12px;
  white-space: nowrap;
}

.toolbar {
  display: flex;
  gap: 10px;
  margin-bottom: 16px;
}

.filter-select {
  padding: 7px 10px;
  border: 1px solid #e0d8cc;
  border-radius: 8px;
  background: #fff;
  color: #4a3f35;
  font-size: 13px;
  outline: none;
}

.refresh-btn {
  padding: 7px 14px;
  border: 1px solid #e0d8cc;
  border-radius: 8px;
  background: #fff;
  color: #6b5d4f;
  font-size: 13px;
  cursor: pointer;
}

.refresh-btn:hover:not(:disabled) {
  border-color: #c98a4b;
  color: #c98a4b;
}

.loading-state,
.empty-state {
  text-align: center;
  padding: 60px 0;
  color: #8d7f6f;
}

.spinner {
  width: 28px;
  height: 28px;
  margin: 0 auto 10px;
  border: 3px solid #e8dfd2;
  border-top-color: #c98a4b;
  border-radius: 50%;
  animation: wiki-spin 0.8s linear infinite;
}

@keyframes wiki-spin {
  to { transform: rotate(360deg); }
}

.empty-hint {
  font-size: 12.5px;
  color: #a89a89;
  margin-top: 6px;
}

.wiki-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.wiki-item {
  background: #fff;
  border: 1px solid #e8dfd2;
  border-radius: 12px;
  padding: 14px 16px;
}

.wiki-item-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.wiki-type-tag {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 999px;
  background: #f0e9df;
  color: #8d6a3f;
  white-space: nowrap;
}

.wiki-type-tag.digest {
  background: #e8f0e8;
  color: #4f7a4f;
}

.wiki-title {
  border: none;
  background: none;
  padding: 0;
  font-size: 14.5px;
  font-weight: 600;
  color: #4a3f35;
  cursor: pointer;
  text-align: left;
}

.wiki-title:hover {
  color: #c98a4b;
}

.wiki-private-badge {
  font-size: 10.5px;
  padding: 1px 7px;
  border-radius: 999px;
  background: #f5e3e3;
  color: #a05555;
}

.wiki-kb-name {
  font-size: 11.5px;
  color: #a89a89;
}

.wiki-excerpt {
  margin: 8px 0;
  font-size: 12.5px;
  color: #6b5d4f;
  line-height: 1.6;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.wiki-item-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.wiki-time {
  font-size: 11.5px;
  color: #a89a89;
}

.wiki-actions {
  display: flex;
  gap: 12px;
}

.btn-link {
  border: none;
  background: none;
  padding: 0;
  font-size: 12.5px;
  color: #c98a4b;
  cursor: pointer;
}

.btn-link.danger {
  color: #b05555;
}

/* ---- 弹窗 ---- */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(60, 48, 36, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}

.modal-box {
  background: #fdfbf7;
  border-radius: 14px;
  width: min(760px, 92vw);
  max-height: 84vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 12px 40px rgba(60, 48, 36, 0.25);
}

.modal-sm {
  width: min(420px, 92vw);
}

.modal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px;
  border-bottom: 1px solid #e8dfd2;
}

.modal-head h3 {
  margin: 0;
  font-size: 15.5px;
  color: #4a3f35;
}

.modal-close {
  border: none;
  background: none;
  font-size: 20px;
  color: #a89a89;
  cursor: pointer;
  line-height: 1;
}

.modal-body {
  padding: 16px 20px;
  overflow-y: auto;
  font-size: 13.5px;
  color: #4a3f35;
  line-height: 1.7;
}

.modal-foot {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 12px 20px;
  border-top: 1px solid #e8dfd2;
}

.btn-cancel {
  padding: 7px 16px;
  border: 1px solid #e0d8cc;
  border-radius: 8px;
  background: #fff;
  color: #6b5d4f;
  font-size: 13px;
  cursor: pointer;
}

.btn-confirm-danger {
  padding: 7px 16px;
  border: none;
  border-radius: 8px;
  background: #b05555;
  color: #fff;
  font-size: 13px;
  cursor: pointer;
}

.btn-confirm-danger:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
</style>
