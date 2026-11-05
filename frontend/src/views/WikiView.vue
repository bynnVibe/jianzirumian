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

    <!-- 对话沉淀（llm-wiki 路径A：高频主题 → 私人综合词条） -->
    <div class="conv-card" v-if="conv.enabled">
      <div class="conv-head">
        <div class="conv-head-text">
          <h3 class="conv-title">对话沉淀</h3>
          <p class="conv-desc">系统周期性归纳你反复问及的高频主题，自动综合成仅你可见的私人百科词条，提问时检索会优先命中</p>
        </div>
        <button class="conv-compile-btn" :disabled="convCompiling" @click="compileTopics">
          {{ convCompiling ? '沉淀中...' : '立即沉淀' }}
        </button>
      </div>
      <div class="conv-body">
        <div class="conv-section" v-if="conv.candidates.length">
          <span class="conv-label">高频主题</span>
          <div class="conv-topics">
            <span v-for="c in conv.candidates" :key="c.topic" class="conv-topic" :class="{ done: c.digested }">
              {{ c.topic }}<em>×{{ c.hits }}</em>
              <i v-if="c.digested" class="conv-done-flag">已沉淀</i>
            </span>
          </div>
        </div>
        <div class="conv-section" v-if="conv.digested.length">
          <span class="conv-label">已沉淀私人词条</span>
          <div class="conv-topics">
            <button v-for="d in conv.digested" :key="d.page_id || d.topic" class="conv-digest-link" @click="openPageById(d.page_id)">
              {{ d.topic }}
            </button>
          </div>
        </div>
        <p v-if="!conv.candidates.length && !conv.digested.length" class="conv-empty">
          近期提问中出现被问及 {{ conv.min_hits }} 次以上的高频主题后，可在此沉淀为私人词条
        </p>
      </div>
      <p v-if="convMessage" class="conv-message" :class="convMessageType">{{ convMessage }}</p>
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
import { getWikiPages, getWikiPage, deleteWikiPage, listKnowledgeBases, getConversationTopics, compileConversationTopics } from '@/api'
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

// 对话沉淀（llm-wiki 路径A）
const conv = ref({ enabled: false, min_hits: 3, candidates: [], digested: [] })
const convCompiling = ref(false)
const convMessage = ref('')
const convMessageType = ref('info')

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
    assistant.pushAction(`打开百科词条《${(detailPage.value.title || '').slice(0, 40)}》`)
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
    await loadConversation(false)
  } catch (e) {
    console.error('删除词条失败:', e)
  } finally {
    deleting.value = false
  }
}

/** 加载对话沉淀预览；refresh=true 时后端调 LLM 归纳高频主题 */
async function loadConversation(refresh = true) {
  try {
    const res = await getConversationTopics(refresh)
    conv.value = {
      enabled: !!res.data.enabled,
      min_hits: res.data.min_hits || 3,
      candidates: res.data.candidates || [],
      digested: res.data.digested || [],
    }
  } catch (e) {
    console.error('加载对话沉淀预览失败:', e)
  }
}

/** 手动触发沉淀：把高频主题综合成私人词条 */
async function compileTopics() {
  convCompiling.value = true
  convMessage.value = ''
  try {
    const res = await compileConversationTopics()
    const n = res.data.generated || 0
    convMessageType.value = n > 0 ? 'success' : 'info'
    convMessage.value = res.data.message || (n > 0 ? `已沉淀 ${n} 条私人词条` : '暂无新的高频主题可沉淀')
    await Promise.all([loadConversation(true), loadPages()])
  } catch (e) {
    convMessageType.value = 'error'
    convMessage.value = '沉淀失败：' + (e.response?.data?.detail || e.message || '未知错误')
  } finally {
    convCompiling.value = false
  }
}

/** 从已沉淀日志打开词条详情 */
async function openPageById(pageId) {
  if (!pageId) return
  try {
    const res = await getWikiPage(pageId)
    if (res.data.page) {
      detailPage.value = res.data.page
      assistant.pushAction(`打开百科词条《${(res.data.page.title || '').slice(0, 40)}》`)
    }
  } catch (e) {
    console.error('加载词条详情失败:', e)
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
  await loadConversation(true)
  window.addEventListener('wiki:page-updated', onWikiPageUpdated)
})

onUnmounted(() => {
  window.removeEventListener('wiki:page-updated', onWikiPageUpdated)
  assistant.clearPageContext()
  assistant.clearOpenDoc()
  assistant.clearPageState()
})

// 知识百科列表的界面实时快照（词条清单 + 筛选），使助手无需重检即可感知“当前百科页”
const WIKI_TYPE_LABELS = { source: '来源卡片', digest: '综合词条' }
const wikiPageState = computed(() => {
  const items = wikiPages.value.slice(0, 30).map((p) => ({
    title: p.title || '未命名',
    type: WIKI_TYPE_LABELS[p.page_type] || p.page_type || '',
    link: '',
  }))
  return {
    view: '知识百科' + (typeFilter.value ? `（筛选：${WIKI_TYPE_LABELS[typeFilter.value] || typeFilter.value}）` : ''),
    kb_filter: kbFilter.value ? kbName(kbFilter.value) : '',
    stats: { shown_count: wikiPages.value.length },
    items,
    items_truncated: wikiPages.value.length > items.length,
  }
})

// 列表/筛选变化时同步快照
watch(wikiPageState, (state) => {
  if (state) assistant.setPageState(state)
  else assistant.clearPageState()
})

// 筛选变化记一条行文动作
watch([typeFilter, kbFilter], ([t, k], [ot, ok]) => {
  if (t !== ot) assistant.pushAction(`筛选百科类型：${WIKI_TYPE_LABELS[t] || '全部类型'}`)
  if (k !== ok) assistant.pushAction(`筛选百科知识库：${k ? kbName(k) : '全部知识库'}`)
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

/* ---- 对话沉淀卡 ---- */
.conv-card {
  background: #fdf8f0;
  border: 1px solid #ecdcc4;
  border-radius: 12px;
  padding: 14px 16px;
  margin-bottom: 16px;
}

.conv-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.conv-title {
  font-size: 14.5px;
  font-weight: 700;
  color: #8a5a2b;
  margin: 0 0 4px;
}

.conv-desc {
  font-size: 12px;
  color: #9a8874;
  margin: 0;
  line-height: 1.6;
}

.conv-compile-btn {
  flex-shrink: 0;
  padding: 7px 16px;
  border: 1px solid #d8b783;
  border-radius: 8px;
  background: #f6e7cf;
  color: #8a5a2b;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
}

.conv-compile-btn:hover:not(:disabled) {
  background: #efdcc0;
  border-color: #c98a4b;
}

.conv-compile-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.conv-body {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.conv-section {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
}

.conv-label {
  font-size: 11.5px;
  color: #a08b6f;
  flex-shrink: 0;
  min-width: 84px;
}

.conv-topics {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.conv-topic {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12.5px;
  color: #6b5d4f;
  background: #fff;
  border: 1px solid #e8dfd2;
  border-radius: 999px;
  padding: 3px 10px;
}

.conv-topic em {
  font-style: normal;
  color: #b08a5a;
  font-size: 11px;
}

.conv-topic.done {
  border-color: #cfe0cf;
  background: #f2f7f2;
  color: #5a7a5a;
}

.conv-done-flag {
  font-style: normal;
  font-size: 10px;
  color: #4f7a4f;
  background: #e2efe2;
  border-radius: 999px;
  padding: 0 6px;
  margin-left: 2px;
}

.conv-digest-link {
  border: none;
  background: none;
  padding: 0;
  font-size: 12.5px;
  color: #c98a4b;
  cursor: pointer;
  text-decoration: underline;
  text-underline-offset: 2px;
}

.conv-empty {
  font-size: 12px;
  color: #a89a89;
  margin: 0;
}

.conv-message {
  margin: 10px 0 0;
  font-size: 12px;
  padding: 6px 10px;
  border-radius: 8px;
}

.conv-message.success {
  background: #e8f2e8;
  color: #4f7a4f;
}

.conv-message.info {
  background: #f0e9df;
  color: #8d7f6f;
}

.conv-message.error {
  background: #f7e6e6;
  color: #a05555;
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
