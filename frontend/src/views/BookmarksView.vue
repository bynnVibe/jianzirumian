<template>
  <div class="bookmarks-page">
    <div class="page-header">
      <h2>知识收藏</h2>
      <span class="bookmark-count" v-if="allBookmarks.length">共 {{ allBookmarks.length }} 条</span>
    </div>

    <!-- 搜索和标签筛选 -->
    <div class="toolbar">
      <div class="search-box">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="search-icon" width="16" height="16">
          <circle cx="11" cy="11" r="8"/>
          <line x1="21" y1="21" x2="16.65" y2="16.65"/>
        </svg>
        <input
          v-model="searchQuery"
          type="text"
          class="search-input"
          placeholder="搜索收藏标题或内容..."
        />
        <button v-if="searchQuery" class="search-clear" @click="searchQuery = ''">&times;</button>
      </div>
      <div class="tag-filter" v-if="allTags.length > 0">
        <button
          class="tag-filter-btn"
          :class="{ active: activeTag === '' }"
          @click="activeTag = ''"
        >全部</button>
        <button
          v-for="tag in allTags"
          :key="tag"
          class="tag-filter-btn"
          :class="{ active: activeTag === tag }"
          @click="toggleTag(tag)"
        >{{ tag }}</button>
      </div>
    </div>

    <!-- 加载状态 -->
    <div v-if="loading" class="loading-state">
      <div class="spinner"></div>
      <p>加载中...</p>
    </div>

    <!-- 空状态 -->
    <div v-else-if="allBookmarks.length === 0" class="empty-state">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" class="empty-icon">
        <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>
      </svg>
      <p>暂无收藏</p>
      <p class="empty-hint">在对话中收藏的知识片段将显示在这里</p>
    </div>

    <!-- 无匹配结果 -->
    <div v-else-if="totalFiltered === 0" class="empty-state">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" class="empty-icon">
        <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
      </svg>
      <p>未找到匹配的收藏</p>
      <p class="empty-hint">试试其他关键词或标签</p>
    </div>

    <!-- 收藏网格 -->
    <div v-else class="bookmark-grid">
      <div
        v-for="bookmark in pagedBookmarks"
        :key="bookmark.id"
        class="bookmark-card clickable"
        @click="openDetail(bookmark)"
        :title="'点击查看收藏详情'"
      >
        <!-- 顶部：分类 + 标题 + 日期 -->
        <div class="card-top">
          <span class="source-badge" :class="bookmark.source_type">
            {{ bookmark.source_type === 'answer' ? 'AI回答' : '知识片段' }}
          </span>
          <h3 class="card-title" :title="bookmark.title">{{ bookmark.title }}</h3>
          <span class="card-date">{{ formatDate(bookmark.created_at) }}</span>
        </div>

        <!-- 内容预览（固定高度，多行截断） -->
        <div class="card-body">
          <p class="card-content">{{ bookmark.content }}</p>
          <!-- 图片类来源：缩略图 -->
          <div v-if="!isDocumentSource(bookmark) && bookmark.source_info?.source_image" class="card-thumb">
            <img
              :src="getImageUrl(bookmark.source_info.source_image)"
              class="thumb-img"
              alt="来源图片"
              @click.stop="openImage(bookmark.source_info.source_image)"
            />
          </div>
          <!-- Word/PDF 来源：检索跳转链接 -->
          <div v-else-if="isDocumentSource(bookmark)" class="card-thumb">
            <a :href="docPreviewUrl(bookmark)" target="_blank" class="card-doc-link" @click.stop>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                <polyline points="14 2 14 8 20 8"/>
              </svg>
              {{ docLinkLabel(bookmark) }} ↗
            </a>
          </div>
        </div>

        <!-- 底部：标签 + 删除 -->
        <div class="card-bottom">
          <div class="card-tags" v-if="bookmark.tags && bookmark.tags.length > 0">
            <button
              v-for="tag in bookmark.tags"
              :key="tag"
              class="card-tag"
              :class="{ 'tag-active': activeTag === tag }"
              @click.stop="clickTag(tag)"
            >{{ tag }}</button>
          </div>
          <button class="btn-delete-bookmark" @click.stop="confirmDelete(bookmark.id)" title="删除">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
              <polyline points="3 6 5 6 21 6"/>
              <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
            </svg>
          </button>
        </div>
      </div>
    </div>

    <!-- 分页 -->
    <div v-if="totalPages > 1" class="pagination">
      <button
        class="page-btn"
        :disabled="currentPage <= 1"
        @click="currentPage = currentPage - 1"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
          <polyline points="15 18 9 12 15 6"/>
        </svg>
        上一页
      </button>

      <div class="page-numbers">
        <button
          v-for="p in visiblePages"
          :key="p"
          class="page-num"
          :class="{ active: p === currentPage }"
          @click="currentPage = p"
        >{{ p }}</button>
      </div>

      <button
        class="page-btn"
        :disabled="currentPage >= totalPages"
        @click="currentPage = currentPage + 1"
      >
        下一页
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
          <polyline points="9 18 15 12 9 6"/>
        </svg>
      </button>
    </div>

    <!-- 收藏详情对话框 -->
    <Teleport to="body">
      <div v-if="detailBookmark" class="detail-overlay" @click.self="closeDetail">
        <div class="detail-modal">
          <div class="detail-header">
            <span class="source-badge" :class="detailBookmark.source_type">
              {{ detailBookmark.source_type === 'answer' ? 'AI回答' : '知识片段' }}
            </span>
            <h3 class="detail-title" :title="detailBookmark.title">{{ detailBookmark.title }}</h3>
            <button class="modal-close" @click="closeDetail">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
                <line x1="18" y1="6" x2="6" y2="18"/>
                <line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          </div>
          <div class="detail-body">
            <div class="detail-meta">
              <span class="detail-date">收藏于 {{ formatDate(detailBookmark.created_at) }}</span>
              <div class="detail-tags" v-if="detailBookmark.tags && detailBookmark.tags.length">
                <span v-for="tag in detailBookmark.tags" :key="tag" class="card-tag">{{ tag }}</span>
              </div>
            </div>
            <div class="detail-content markdown-content" v-html="renderedDetailContent"></div>
            <!-- 来源：Word/PDF 检索跳转链接 -->
            <div v-if="detailBookmark.source_type === 'knowledge' && isDocumentSource(detailBookmark)" class="detail-source">
              <div class="detail-source-label">来源文档</div>
              <div class="detail-source-doc">
                <span class="detail-doc-name" :title="sourceDisplayName(detailBookmark)">{{ sourceDisplayName(detailBookmark) }}</span>
                <a :href="docPreviewUrl(detailBookmark)" target="_blank" class="detail-doc-link">{{ docLinkLabel(detailBookmark) }} ↗</a>
              </div>
            </div>
            <!-- 来源：原始图片 -->
            <div v-else-if="detailBookmark.source_type === 'knowledge' && detailBookmark.source_info?.source_image" class="detail-source">
              <div class="detail-source-label">来源图片</div>
              <img
                :src="getImageUrl(detailBookmark.source_info.source_image)"
                class="detail-source-image"
                alt="来源图片"
                @click="openImage(detailBookmark.source_info.source_image)"
              />
              <a :href="getImageUrl(detailBookmark.source_info.source_image)" target="_blank" class="detail-doc-link">查看原图 ↗</a>
            </div>
          </div>
          <div class="detail-footer">
            <button class="btn-cancel" @click="closeDetail">关闭</button>
            <button class="btn-danger" @click="confirmDelete(detailBookmark.id)">删除</button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 删除确认对话框 -->
    <Teleport to="body">
      <div v-if="showDeleteConfirm" class="confirm-overlay" @click.self="showDeleteConfirm = false">
        <div class="confirm-dialog">
          <p>确定要删除这条收藏吗？</p>
          <div class="confirm-actions">
            <button class="btn-cancel" @click="showDeleteConfirm = false">取消</button>
            <button class="btn-danger" @click="handleDelete">删除</button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import MarkdownIt from 'markdown-it'
import { listBookmarks, deleteBookmark, getImageUrl as buildImageUrl } from '@/api'

const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

const PAGE_SIZE = 8

const allBookmarks = ref([])
const loading = ref(true)
const searchQuery = ref('')
const activeTag = ref('')
const currentPage = ref(1)
const showDeleteConfirm = ref(false)
const deletingId = ref(null)

// 所有标签（去重）
const allTags = computed(() => {
  const tagSet = new Set()
  allBookmarks.value.forEach((b) => {
    ;(b.tags || []).forEach((t) => tagSet.add(t))
  })
  return [...tagSet].sort()
})

// 搜索 + 标签筛选
const filteredBookmarks = computed(() => {
  let result = allBookmarks.value
  if (searchQuery.value.trim()) {
    const q = searchQuery.value.trim().toLowerCase()
    result = result.filter(
      (b) =>
        b.title.toLowerCase().includes(q) ||
        b.content.toLowerCase().includes(q)
    )
  }
  if (activeTag.value) {
    result = result.filter((b) => (b.tags || []).includes(activeTag.value))
  }
  return result
})

const totalFiltered = computed(() => filteredBookmarks.value.length)
const totalPages = computed(() => Math.max(1, Math.ceil(totalFiltered.value / PAGE_SIZE)))

// 当前页数据
const pagedBookmarks = computed(() => {
  const start = (currentPage.value - 1) * PAGE_SIZE
  return filteredBookmarks.value.slice(start, start + PAGE_SIZE)
})

// 可见页码（最多显示 7 个，带省略）
const visiblePages = computed(() => {
  const total = totalPages.value
  const current = currentPage.value
  if (total <= 7) {
    return Array.from({ length: total }, (_, i) => i + 1)
  }
  const pages = []
  // 始终显示首页
  pages.push(1)
  let start = Math.max(2, current - 2)
  let end = Math.min(total - 1, current + 2)
  if (current <= 3) {
    start = 2
    end = Math.min(5, total - 1)
  }
  if (current >= total - 2) {
    start = Math.max(total - 4, 2)
    end = total - 1
  }
  if (start > 2) pages.push('...')
  for (let i = start; i <= end; i++) pages.push(i)
  if (end < total - 1) pages.push('...')
  pages.push(total)
  return pages
})

onMounted(async () => {
  await loadBookmarks()
})

async function loadBookmarks() {
  loading.value = true
  try {
    const res = await listBookmarks()
    allBookmarks.value = res.data.bookmarks || []
  } catch (err) {
    console.error('加载收藏失败:', err)
  } finally {
    loading.value = false
  }
}

function toggleTag(tag) {
  activeTag.value = activeTag.value === tag ? '' : tag
  currentPage.value = 1
}

function clickTag(tag) {
  activeTag.value = tag
  currentPage.value = 1
}

function formatDate(timestamp) {
  if (!timestamp) return ''
  const d = new Date(timestamp * 1000)
  return d.toLocaleDateString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function getImageUrl(imagePath) {
  // 图片接口需鉴权，统一走 api 层 helper（自动附带 token）
  return buildImageUrl(imagePath)
}

function openImage(imagePath) {
  window.open(getImageUrl(imagePath), '_blank')
}

// ---- 收藏详情 ----
const detailBookmark = ref(null)

function openDetail(bookmark) {
  detailBookmark.value = bookmark
}

function closeDetail() {
  detailBookmark.value = null
}

const renderedDetailContent = computed(() => {
  if (!detailBookmark.value) return ''
  return md.render(detailBookmark.value.content || '')
})

// ---- 来源类型判断（兼容旧数据：优先 source_type，其次按扩展名） ----
function getSourceType(bookmark) {
  const info = bookmark.source_info || {}
  if (info.source_type) return info.source_type
  const name = (info.source_image || '').toLowerCase()
  if (name.endsWith('.docx') || name.endsWith('.doc')) return 'word'
  if (name.endsWith('.pdf')) return 'pdf'
  return 'image'
}

function isDocumentSource(bookmark) {
  const t = getSourceType(bookmark)
  return t === 'word' || t === 'pdf'
}

// Word 文档跳转链接：已生成排版 PDF 则分页预览，否则下载原文
function getWordPreviewUrl(info) {
  const path = info.source_image
  if (!path) return '#'
  const filename = path.split('/').pop()
  if (!info.pdf_preview_path) return buildImageUrl(filename)
  const page = info.page_number ? `&page=${info.page_number}` : ''
  const pdf = `&pdf=${encodeURIComponent(info.pdf_preview_path.split('/').pop())}`
  return `/doc-preview?file=${encodeURIComponent(filename)}${page}${pdf}`
}

// PDF 跳转链接：浏览器内置阅读器 + 页码锚点
function getPdfPreviewUrl(info) {
  const path = info.pdf_preview_path || info.source_image
  if (!path) return '#'
  const filename = path.split('/').pop()
  const baseUrl = buildImageUrl(filename)
  return info.page_number ? `${baseUrl}#page=${info.page_number}` : baseUrl
}

function docPreviewUrl(bookmark) {
  const info = bookmark.source_info || {}
  return getSourceType(bookmark) === 'word' ? getWordPreviewUrl(info) : getPdfPreviewUrl(info)
}

function docLinkLabel(bookmark) {
  const info = bookmark.source_info || {}
  const kind = getSourceType(bookmark) === 'word'
    ? (info.pdf_preview_path ? '在线预览 Word' : '下载原文')
    : '在线查看 PDF'
  return info.page_number ? `${kind}（第 ${info.page_number} 页）` : kind
}

function sourceDisplayName(bookmark) {
  const info = bookmark.source_info || {}
  const name = (info.source_image || '').split('/').pop()
  return info.kb_name || name || '未知来源'
}

function confirmDelete(id) {
  deletingId.value = id
  showDeleteConfirm.value = true
}

async function handleDelete() {
  if (!deletingId.value) return
  const id = deletingId.value
  try {
    await deleteBookmark(id)
    allBookmarks.value = allBookmarks.value.filter((b) => b.id !== id)
    // 删除的是当前详情中的收藏时，同步关闭详情
    if (detailBookmark.value?.id === id) detailBookmark.value = null
    // 如果当前页没有数据了，回退一页
    if (pagedBookmarks.value.length === 0 && currentPage.value > 1) {
      currentPage.value--
    }
  } catch (err) {
    console.error('删除收藏失败:', err)
  } finally {
    showDeleteConfirm.value = false
    deletingId.value = null
  }
}
</script>

<style scoped>
.bookmarks-page {
  padding: 28px 32px;
  max-width: 1100px;
  margin: 0 auto;
}

.page-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 20px;
}

.page-header h2 {
  font-size: 20px;
  font-weight: 700;
  color: var(--color-text);
  margin: 0;
}

.bookmark-count {
  font-size: 12px;
  color: var(--color-text-secondary);
  background: #ede6dc;
  padding: 2px 8px;
  border-radius: 8px;
}

/* ---- 工具栏 ---- */
.toolbar {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-bottom: 20px;
}

.search-box {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  background: white;
  border: 1px solid #ede6dc;
  border-radius: 10px;
  transition: border-color 0.2s;
}

.search-box:focus-within {
  border-color: var(--color-primary);
}

.search-icon {
  color: var(--color-text-secondary);
  flex-shrink: 0;
}

.search-input {
  border: none;
  outline: none;
  font-size: 13px;
  flex: 1;
  background: transparent;
  color: var(--color-text);
}

.search-clear {
  border: none;
  background: transparent;
  font-size: 18px;
  color: var(--color-text-secondary);
  cursor: pointer;
  padding: 0 4px;
  line-height: 1;
}

.search-clear:hover {
  color: var(--color-text);
}

.tag-filter {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.tag-filter-btn {
  padding: 4px 12px;
  border: 1px solid #ede6dc;
  border-radius: 14px;
  background: white;
  font-size: 12px;
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all 0.2s;
}

.tag-filter-btn:hover {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.tag-filter-btn.active {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
  color: var(--color-primary);
}

/* ---- 加载/空状态 ---- */
.loading-state,
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 80px 20px;
  color: var(--color-text-secondary);
}

.empty-icon {
  width: 40px;
  height: 40px;
  margin-bottom: 12px;
  opacity: 0.4;
}

.empty-state p {
  font-size: 14px;
}

.empty-hint {
  font-size: 12px !important;
  margin-top: 4px;
  opacity: 0.6;
}

.spinner {
  width: 24px;
  height: 24px;
  border: 2px solid #ede6dc;
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  margin-bottom: 10px;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* ---- 收藏网格（统一卡片高宽） ---- */
.bookmark-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 14px;
}

.bookmark-card {
  background: white;
  border: 1px solid #ede6dc;
  border-radius: 12px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  transition: box-shadow 0.2s, border-color 0.2s;
  min-height: 220px;
}

.bookmark-card:hover {
  box-shadow: 0 3px 12px rgba(0, 0, 0, 0.07);
  border-color: #dcd5cb;
}

.bookmark-card.clickable {
  cursor: pointer;
}

.bookmark-card.clickable:hover {
  border-color: var(--color-primary);
}

/* Word/PDF 来源跳转链接（卡片内） */
.card-doc-link {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 10px;
  background: var(--color-primary-light);
  border: 1px solid #eadfce;
  border-radius: 6px;
  color: var(--color-primary);
  font-size: 12px;
  font-weight: 500;
  text-decoration: none;
  transition: all 0.2s;
}

.card-doc-link:hover {
  border-color: var(--color-primary);
  background: #f0dcc0;
}

/* 顶部行：分类 + 标题 + 日期 */
.card-top {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 14px 16px 0;
}

.source-badge {
  flex-shrink: 0;
  font-size: 11px;
  font-weight: 500;
  padding: 2px 8px;
  border-radius: 4px;
  white-space: nowrap;
}

.source-badge.answer {
  background: #e8f5e9;
  color: #2e7d32;
}

.source-badge.knowledge {
  background: var(--color-primary-light);
  color: var(--color-primary);
}

.card-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text);
  margin: 0;
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.card-date {
  font-size: 11px;
  color: var(--color-text-secondary);
  white-space: nowrap;
  flex-shrink: 0;
}

/* 内容区（flex-grow 撑满，固定行数截断） */
.card-body {
  flex: 1;
  padding: 10px 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 0;
}

.card-content {
  font-size: 13px;
  line-height: 1.6;
  color: #5a4f42;
  margin: 0;
  display: -webkit-box;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-word;
}

.card-thumb {
  flex-shrink: 0;
}

.thumb-img {
  max-width: 100%;
  max-height: 80px;
  border-radius: 6px;
  border: 1px solid #ede6dc;
  cursor: zoom-in;
  transition: opacity 0.2s;
}

.thumb-img:hover {
  opacity: 0.85;
}

/* 底部：标签 + 删除 */
.card-bottom {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px 12px;
  gap: 8px;
  border-top: 1px solid #f3efe9;
  margin-top: auto;
}

.card-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  flex: 1;
  min-width: 0;
}

.card-tag {
  font-size: 11px;
  color: var(--color-primary);
  background: var(--color-primary-light);
  padding: 2px 8px;
  border-radius: 4px;
  border: none;
  cursor: pointer;
  transition: all 0.15s;
  white-space: nowrap;
}

.card-tag:hover {
  background: #f0dcc0;
}

.card-tag.tag-active {
  background: var(--color-primary);
  color: white;
}

.btn-delete-bookmark {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: #bdb4a8;
  cursor: pointer;
  transition: all 0.2s;
}

.btn-delete-bookmark:hover {
  background: #ffebee;
  color: #c62828;
  border-color: #f5c6cb;
}

/* ---- 分页 ---- */
.pagination {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  margin-top: 28px;
  padding-bottom: 12px;
}

.page-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 7px 14px;
  border: 1px solid #ede6dc;
  border-radius: 8px;
  background: white;
  font-size: 13px;
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all 0.2s;
}

.page-btn:hover:not(:disabled) {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.page-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.page-numbers {
  display: flex;
  align-items: center;
  gap: 4px;
}

.page-num {
  min-width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  font-size: 13px;
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all 0.2s;
  padding: 0 4px;
}

.page-num:hover {
  background: #f3efe9;
}

.page-num.active {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
  color: var(--color-primary);
  font-weight: 600;
}

/* ---- 删除确认对话框 ---- */
.confirm-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1100;
}

.confirm-dialog {
  background: white;
  border-radius: 12px;
  padding: 24px 28px;
  width: 340px;
  max-width: 90vw;
  text-align: center;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.2);
}

.confirm-dialog p {
  font-size: 14px;
  color: var(--color-text);
  margin: 0 0 20px;
}

.confirm-actions {
  display: flex;
  justify-content: center;
  gap: 10px;
}

.btn-cancel {
  padding: 8px 20px;
  border: 1px solid #ede6dc;
  border-radius: 8px;
  background: white;
  color: var(--color-text-secondary);
  font-size: 13px;
  cursor: pointer;
  transition: all 0.2s;
}

.btn-cancel:hover {
  background: #f3efe9;
}

.btn-danger {
  padding: 8px 20px;
  border: none;
  border-radius: 8px;
  background: #c62828;
  color: white;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s;
}

.btn-danger:hover {
  background: #b71c1c;
}

/* ---- 收藏详情对话框 ---- */
.detail-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: 20px;
}

.detail-modal {
  background: white;
  border-radius: 14px;
  width: 720px;
  max-width: 94vw;
  max-height: 86vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.2);
  overflow: hidden;
}

.detail-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 16px 22px;
  border-bottom: 1px solid #ede6dc;
}

.detail-title {
  flex: 1;
  min-width: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--color-text);
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.modal-close {
  width: 32px;
  height: 32px;
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

.modal-close:hover {
  background: #f3efe9;
  color: var(--color-text);
}

.detail-body {
  padding: 18px 22px;
  overflow-y: auto;
  flex: 1;
}

.detail-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

.detail-date {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.detail-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.detail-content {
  font-size: 14px;
  line-height: 1.75;
  color: #3d352a;
  word-break: break-word;
}

.detail-content table {
  border-collapse: collapse;
  margin: 8px 0;
  font-size: 13px;
}

.detail-content th,
.detail-content td {
  border: 1px solid #ede6dc;
  padding: 4px 10px;
}

.detail-source {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px dashed #ede6dc;
}

.detail-source-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-secondary);
  margin-bottom: 8px;
}

.detail-source-doc {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  background: #faf6f0;
  border: 1px solid #ede6dc;
  border-radius: 8px;
}

.detail-doc-name {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.detail-doc-link {
  flex-shrink: 0;
  color: var(--color-primary);
  font-size: 12px;
  font-weight: 500;
  text-decoration: none;
}

.detail-doc-link:hover {
  text-decoration: underline;
}

.detail-source-image {
  max-width: 100%;
  max-height: 260px;
  border-radius: 6px;
  border: 1px solid #ede6dc;
  cursor: zoom-in;
  display: block;
  margin-bottom: 6px;
}

.detail-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 14px 22px;
  border-top: 1px solid #ede6dc;
}

/* ---- 移动端适配 ---- */
@media (max-width: 768px) {
  .bookmarks-page {
    padding: 16px;
  }

  .page-header h2 {
    font-size: 18px;
  }

  .bookmark-grid {
    grid-template-columns: 1fr;
    gap: 10px;
  }

  .bookmark-card {
    min-height: 180px;
  }

  .card-top {
    padding: 10px 12px 0;
  }

  .card-title {
    font-size: 13px;
  }

  .card-body {
    padding: 8px 12px;
  }

  .card-content {
    -webkit-line-clamp: 3;
    font-size: 12px;
  }

  .card-bottom {
    padding: 6px 12px 10px;
  }

  .pagination {
    flex-wrap: wrap;
    gap: 6px;
  }

  .page-btn {
    padding: 5px 10px;
    font-size: 12px;
  }

  .page-num {
    min-width: 28px;
    height: 28px;
    font-size: 12px;
  }

  .tag-filter-btn {
    font-size: 11px;
    padding: 3px 10px;
  }
}
</style>
