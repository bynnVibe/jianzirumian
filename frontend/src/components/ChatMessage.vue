<template>
  <div class="message" :class="[role]">
    <div class="avatar">
      <template v-if="role === 'user'">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="avatar-icon">
          <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
          <circle cx="12" cy="7" r="4"/>
        </svg>
      </template>
      <template v-else>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" class="avatar-icon leaf">
          <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2z"/>
          <path d="M12 6v6l4 2" stroke-linecap="round"/>
          <path d="M8 12c0 2.21 1.79 4 4 4" stroke-linecap="round"/>
        </svg>
      </template>
    </div>
    <div class="content-area">
      <div class="sender-name">{{ role === 'user' ? '你' : '见字如面' }}</div>
      <!-- 用户发送的附件（图片/文档）回显：点击在新页面预览 -->
      <div v-if="role === 'user' && attachments && attachments.length" class="message-attachments">
        <template v-for="(att, idx) in attachments" :key="idx">
          <a
            v-if="att.file_id"
            class="message-attachment-chip clickable"
            :href="attachmentPreviewUrl(att)"
            target="_blank"
            :title="'在新页面预览 ' + att.name"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>
            </svg>
            {{ att.name }}
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="11" height="11" class="chip-external-icon">
              <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>
              <polyline points="15 3 21 3 21 9"/>
              <line x1="10" y1="14" x2="21" y2="3"/>
            </svg>
          </a>
          <span v-else class="message-attachment-chip">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>
            </svg>
            {{ att.name }}
          </span>
        </template>
      </div>
      <div class="message-content markdown-content" v-html="renderedContent" @click="onContentClick"></div>

      <!-- 用户消息操作：复制 + 编辑重问 -->
      <div v-if="role === 'user' && content" class="message-actions">
        <button class="btn-action" @click="copyText" :title="copied ? '已复制' : '复制问题'">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13">
            <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
            <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
          </svg>
          {{ copied ? '已复制' : '复制' }}
        </button>
        <button class="btn-action" @click="emit('reask', content)" title="将问题填入输入框编辑后重新提问">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13">
            <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
            <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
          </svg>
          编辑重问
        </button>
      </div>

      <!-- 来源信息：可展开的预览 -->
      <div v-if="sources && sources.length > 0" class="sources-section">
        <div class="sources-title">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
            <polyline points="14 2 14 8 20 8"/>
            <line x1="16" y1="13" x2="8" y2="13"/>
            <line x1="16" y1="17" x2="8" y2="17"/>
          </svg>
          知识来源 ({{ sources.length }})
        </div>
        <div class="sources-list">
          <div
            v-for="source in sources"
            :key="source.index"
            class="source-item"
            :class="{ expanded: expandedSource === source.index }"
            :ref="el => setSourceRef('k-' + source.index, el)"
          >
            <div class="source-header" @click="toggleSource(source.index)">
              <span class="source-badge">
                来源 {{ source.index }}
                <span v-if="source.relevance !== undefined" class="relevance-score" :class="relevanceClass(source.relevance)">
                  {{ source.relevance }}%
                </span>
              </span>
              <span class="source-preview">{{ truncateText(source.text, 80) }}</span>
              <span class="source-toggle">{{ expandedSource === source.index ? '收起' : '展开' }}</span>
            </div>
            <!-- 展开详细内容 -->
            <div v-if="expandedSource === source.index" class="source-detail">
              <div class="source-full-text">{{ source.text }}</div>
              <div class="source-detail-actions">
                <button class="btn-bookmark-sm" @click.stop="openBookmarkModal('knowledge', source.text, '', buildBookmarkSourceInfo(source))" title="收藏此知识片段">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12">
                    <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>
                  </svg>
                  收藏片段
                </button>
                <a
                  v-if="source.page_number && isDocumentSource(source)"
                  :href="isWordSource(source) ? getWordPreviewUrl(source) : getPdfPreviewUrl(source)"
                  target="_blank"
                  class="view-image-link"
                >
                  第 {{ source.page_number }} 页 ↗
                </a>
                <span v-else-if="source.page_number && !isDocumentSource(source)" class="source-page-text">
                  第 {{ source.page_number }} 页
                </span>
                <!-- Word/PDF 文档来源：在线预览入口（跳转新标签页） -->
                <template v-if="isDocumentSource(source)">
                  <a
                    v-if="isWordSource(source)"
                    :href="getWordPreviewUrl(source)"
                    target="_blank"
                    class="view-image-link doc-preview-link"
                  >
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12">
                      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                      <polyline points="14 2 14 8 20 8"/>
                    </svg>
                    {{ source.pdf_preview_path ? '在线预览 Word' : '下载原文' }} {{ source.page_number ? '（第 ' + source.page_number + ' 页）' : '' }} ↗
                  </a>
                  <a
                    v-else
                    :href="getPdfPreviewUrl(source)"
                    target="_blank"
                    class="view-image-link doc-preview-link"
                  >
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12">
                      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                      <polyline points="14 2 14 8 20 8"/>
                    </svg>
                    在线查看 PDF {{ source.page_number ? '（第 ' + source.page_number + ' 页）' : '' }} ↗
                  </a>
                </template>
              </div>
              <!-- 图片类来源才显示原图；Word/PDF 文档不显示图片链接 -->
              <div v-if="source.source_image && !isDocumentSource(source)" class="source-image-box">
                <img
                  :src="getImageUrl(source.source_image)"
                  class="source-image"
                  alt="原始图片"
                  @click="openImage(source.source_image)"
                />
                <a :href="getImageUrl(source.source_image)" target="_blank" class="view-image-link">查看原图</a>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 助手回答的操作：收藏 + 点赞/点踩反馈 -->
      <div v-if="role === 'assistant' && content" class="message-actions">
        <button class="btn-bookmark" @click="openBookmarkModal('answer', content, '', {})" title="收藏此回答">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
            <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>
          </svg>
          收藏回答
        </button>
        <template v-if="messageId && canFeedback">
          <button
            class="btn-feedback"
            :class="{ active: localFeedback?.rating === 'like' }"
            @click="handleLike"
            title="回答有帮助"
          >👍</button>
          <button
            class="btn-feedback"
            :class="{ active: localFeedback?.rating === 'dislike' }"
            @click="handleDislike"
            title="回答需改进"
          >👎</button>
        </template>
      </div>

      <!-- 点踩备注输入 -->
      <div v-if="showCommentBox" class="feedback-comment">
        <textarea
          v-model="commentText"
          class="feedback-comment-input"
          rows="2"
          placeholder="可填写反馈备注，帮助我们改进回答质量（可跳过）"
        ></textarea>
        <div class="feedback-comment-actions">
          <button class="btn-cancel-sm" @click="showCommentBox = false">取消</button>
          <button class="btn-submit-sm" @click="submitDislike(true)">提交反馈</button>
        </div>
      </div>

      <!-- 联网搜索结果：与知识来源一致的可展开预览 -->
      <div v-if="searchResults && searchResults.length > 0" class="search-section">
        <div class="sources-title">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
            <circle cx="11" cy="11" r="8"/>
            <line x1="21" y1="21" x2="16.65" y2="16.65"/>
          </svg>
          联网搜索结果 ({{ searchResults.length }})
        </div>
        <div class="sources-list">
          <div
            v-for="(result, idx) in searchResults"
            :key="idx"
            class="source-item"
            :class="{ expanded: expandedSearch === idx }"
            :ref="el => setSourceRef('w-' + idx, el)"
          >
            <div class="source-header" @click="toggleSearch(idx)">
              <span class="source-badge">网络来源 {{ idx + 1 }}</span>
              <span class="source-preview">{{ result.title }}</span>
              <span class="source-toggle">{{ expandedSearch === idx ? '收起' : '展开' }}</span>
            </div>
            <!-- 展开详细内容 -->
            <div v-if="expandedSearch === idx" class="source-detail">
              <a :href="result.url" target="_blank" class="search-title">{{ result.title }}</a>
              <div class="source-full-text">{{ cleanSnippet(result.snippet) }}</div>
              <a v-if="result.url" :href="result.url" target="_blank" class="view-image-link">访问原网页 ↗</a>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- 收藏模态框 -->
  <Teleport to="body">
    <div v-if="showBookmarkModal" class="bookmark-overlay" @click.self="closeBookmarkModal">
      <div class="bookmark-modal">
        <div class="bookmark-modal-header">
          <h3>收藏知识</h3>
          <button class="modal-close" @click="closeBookmarkModal">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
              <line x1="18" y1="6" x2="6" y2="18"/>
              <line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>
        <div class="bookmark-modal-body">
          <div class="form-group">
            <label class="form-label">收藏标题</label>
            <input
              v-model="bookmarkForm.title"
              type="text"
              class="form-input"
              placeholder="为收藏起个名字"
            />
          </div>
          <div class="form-group">
            <label class="form-label">标签（逗号分隔）</label>
            <input
              v-model="bookmarkForm.tagsInput"
              type="text"
              class="form-input"
              placeholder="如：养生,中医,食疗"
              @keydown.enter.prevent
            />
            <div v-if="bookmarkForm.tags.length > 0" class="tags-display">
              <span
                v-for="(tag, idx) in bookmarkForm.tags"
                :key="idx"
                class="tag-chip"
              >
                {{ tag }}
                <button class="tag-remove" @click="removeTag(idx)">&times;</button>
              </span>
            </div>
          </div>
          <div class="form-group">
            <label class="form-label">内容</label>
            <textarea
              v-model="bookmarkForm.content"
              class="form-textarea"
              rows="6"
              readonly
            ></textarea>
          </div>
          <!-- Word/PDF 文档来源：显示检索跳转链接（无来源图片） -->
          <div class="form-group" v-if="bookmarkForm.sourceType === 'knowledge' && isDocumentSource(bookmarkForm.sourceInfo)">
            <label class="form-label">来源文档</label>
            <div class="bookmark-doc-source">
              <span class="bookmark-doc-name" :title="sourceDisplayName(bookmarkForm.sourceInfo)">{{ sourceDisplayName(bookmarkForm.sourceInfo) }}</span>
              <a
                v-if="isWordSource(bookmarkForm.sourceInfo)"
                :href="getWordPreviewUrl(bookmarkForm.sourceInfo)"
                target="_blank"
                class="view-image-link doc-preview-link"
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                  <polyline points="14 2 14 8 20 8"/>
                </svg>
                {{ bookmarkForm.sourceInfo.pdf_preview_path ? '在线预览 Word' : '下载原文' }}{{ bookmarkForm.sourceInfo.page_number ? '（第 ' + bookmarkForm.sourceInfo.page_number + ' 页）' : '' }} ↗
              </a>
              <a
                v-else
                :href="getPdfPreviewUrl(bookmarkForm.sourceInfo)"
                target="_blank"
                class="view-image-link doc-preview-link"
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                  <polyline points="14 2 14 8 20 8"/>
                </svg>
                在线查看 PDF{{ bookmarkForm.sourceInfo.page_number ? '（第 ' + bookmarkForm.sourceInfo.page_number + ' 页）' : '' }} ↗
              </a>
            </div>
          </div>
          <!-- 图片类来源：显示来源图片 -->
          <div class="form-group" v-else-if="bookmarkForm.sourceType === 'knowledge' && bookmarkForm.sourceInfo.source_image">
            <label class="form-label">来源图片</label>
            <img
              :src="getImageUrl(bookmarkForm.sourceInfo.source_image)"
              class="bookmark-source-image"
              alt="来源图片"
            />
          </div>
        </div>
        <div class="bookmark-modal-footer">
          <button class="btn-cancel" @click="closeBookmarkModal">取消</button>
          <button class="btn-save" @click="saveBookmark" :disabled="savingBookmark">
            {{ savingBookmark ? '保存中...' : '保存收藏' }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>

</template>

<script setup>
import { ref, reactive, computed, watch, nextTick } from 'vue'
import MarkdownIt from 'markdown-it'
import { createBookmark, getImageUrl as buildImageUrl, getAttachmentFileUrl } from '@/api'

const props = defineProps({
  role: { type: String, required: true },
  content: { type: String, default: '' },
  sources: { type: Array, default: () => [] },
  searchResults: { type: Array, default: () => [] },
  attachments: { type: Array, default: () => [] },
  messageId: { type: String, default: '' },
  feedback: { type: Object, default: null },
  canFeedback: { type: Boolean, default: false },
})

const emit = defineEmits(['reask', 'feedback'])

// ---- 用户消息：复制 ----
const copied = ref(false)
async function copyText() {
  try {
    await navigator.clipboard.writeText(props.content)
  } catch {
    // 剪贴板 API 不可用时的降级方案
    const ta = document.createElement('textarea')
    ta.value = props.content
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    document.body.removeChild(ta)
  }
  copied.value = true
  setTimeout(() => (copied.value = false), 1500)
}

// ---- 助手回答：点赞/点踩反馈 ----
const localFeedback = ref(props.feedback ? { ...props.feedback } : null)
watch(
  () => props.feedback,
  (val) => {
    localFeedback.value = val ? { ...val } : null
  }
)

const showCommentBox = ref(false)
const commentText = ref('')

function handleLike() {
  showCommentBox.value = false
  localFeedback.value = { rating: 'like', comment: localFeedback.value?.comment || '' }
  emit('feedback', { rating: 'like', comment: localFeedback.value.comment })
}

function handleDislike() {
  // 点踩弹出备注输入（可跳过提交）
  commentText.value = localFeedback.value?.comment || ''
  showCommentBox.value = true
}

function submitDislike(withComment) {
  showCommentBox.value = false
  const comment = withComment ? commentText.value.trim() : ''
  localFeedback.value = { rating: 'dislike', comment }
  emit('feedback', { rating: 'dislike', comment })
}

const expandedSource = ref(null)
const expandedSearch = ref(null)

// 来源条目 DOM 引用（k-知识库编号 / w-搜索索引），供引用跳转定位
const sourceEls = new Map()
function setSourceRef(key, el) {
  if (el) sourceEls.set(key, el)
  else sourceEls.delete(key)
}

const md = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
})

const renderedContent = computed(() => {
  // 先渲染 Markdown，再注入引用标记样式
  // （md 配置了 html:false，若渲染前注入 <span> 会被转义成源码显示）
  let html = md.render(props.content || '')
  html = html.replace(
    /\[(网络来源|来源)\s*(\d+)\]/g,
    (m, type, num) => {
      const kind = type === '网络来源' ? 'web' : 'knowledge'
      return `<span class="source-citation" data-cite-kind="${kind}" data-cite-num="${num}" title="点击查看对应来源">[${type} ${num}]</span>`
    }
  )
  return html
})

// 点击回答中的引用标记 → 展开并滚动定位到下方对应来源（事件委托，v-html 内容无法直接绑事件）
function onContentClick(e) {
  const cite = e.target.closest('.source-citation')
  if (!cite || !cite.dataset.citeKind) return
  jumpToSource(cite.dataset.citeKind, parseInt(cite.dataset.citeNum, 10))
}

async function jumpToSource(kind, num) {
  let key
  if (kind === 'web') {
    const idx = num - 1
    if (!props.searchResults || idx < 0 || idx >= props.searchResults.length) return
    expandedSearch.value = idx
    key = 'w-' + idx
  } else {
    if (!props.sources?.some(s => s.index === num)) return
    expandedSource.value = num
    key = 'k-' + num
  }
  await nextTick()
  const el = sourceEls.get(key)
  if (!el) return
  el.scrollIntoView({ behavior: 'smooth', block: 'center' })
  // 短暂高亮提示定位到的条目
  el.classList.add('cite-flash')
  setTimeout(() => el.classList.remove('cite-flash'), 1600)
}

function truncateText(text, maxLen) {
  if (!text) return ''
  if (text.length <= maxLen) return text
  return text.slice(0, maxLen) + '...'
}

/**
 * 清洗搜索摘要中的原始 Markdown 噪声（图片/链接/标题符号等）
 * 兼容历史会话中后端未清洗过的旧数据
 */
function cleanSnippet(text) {
  if (!text) return ''
  return text
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')       // 图片
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')    // 链接→保留文本
    .replace(/#{1,6}\s*/g, '')                   // 标题符号
    .replace(/\*{1,3}|`+/g, '')                  // 加粗/斜体/代码符号
    .replace(/\s+/g, ' ')
    .trim()
}

function toggleSource(index) {
  expandedSource.value = expandedSource.value === index ? null : index
}

function toggleSearch(idx) {
  expandedSearch.value = expandedSearch.value === idx ? null : idx
}

function getImageUrl(imagePath) {
  // 图片接口需鉴权，统一走 api 层 helper（自动附带 token）
  return buildImageUrl(imagePath)
}

/**
 * 附件点击预览（新页面打开）：
 *  - image → /attachment-preview 图片预览页
 *  - word  → 复用 /doc-preview（mammoth 渲染，url 参数指向附件分发接口）
 *  - pdf   → 浏览器内置 PDF 阅读器
 */
function attachmentPreviewUrl(att) {
  if (!att.file_id) return '#'
  const fileUrl = getAttachmentFileUrl(att.file_id)
  const name = encodeURIComponent(att.name || att.filename || '')
  if (att.kind === 'word') {
    return `/doc-preview?file=${name}&url=${encodeURIComponent(fileUrl)}`
  }
  if (att.kind === 'pdf') return fileUrl
  return `/attachment-preview?id=${encodeURIComponent(att.file_id)}&name=${name}`
}

// ---- 文档来源类型判断（兼容旧数据：优先 source_type，其次按扩展名） ----
function getSourceType(source) {
  if (source.source_type) return source.source_type
  const name = (source.source_image || '').toLowerCase()
  if (name.endsWith('.docx') || name.endsWith('.doc')) return 'word'
  if (name.endsWith('.pdf')) return 'pdf'
  return 'image'
}

function isDocumentSource(source) {
  const t = getSourceType(source)
  return t === 'word' || t === 'pdf'
}

function isWordSource(source) {
  return getSourceType(source) === 'word'
}

/**
 * Word 文档来源 URL：
 * - 若后端已生成原始排版 PDF，跳转到 /doc-preview 分页预览原始文档并定位到对应页；
 * - 否则直接下载原始 docx，不展示解析后的文本。
 */
function getWordPreviewUrl(source) {
  const path = source.source_image
  if (!path) return '#'
  const filename = path.split('/').pop()
  if (!source.pdf_preview_path) {
    return buildImageUrl(filename)
  }
  const page = source.page_number ? `&page=${source.page_number}` : ''
  const pdf = `&pdf=${encodeURIComponent(source.pdf_preview_path.split('/').pop())}`
  return `/doc-preview?file=${encodeURIComponent(filename)}${page}${pdf}`
}

/**
 * PDF 在线查看 URL：直接在浏览器内置 PDF 阅读器打开 + 页码锚点
 */
function getPdfPreviewUrl(source) {
  const path = source.pdf_preview_path || source.source_image
  if (!path) return '#'
  const filename = path.split('/').pop()
  const baseUrl = buildImageUrl(filename)
  return source.page_number ? `${baseUrl}#page=${source.page_number}` : baseUrl
}


function openImage(imagePath) {
  window.open(getImageUrl(imagePath), '_blank')
}

function relevanceClass(score) {
  if (score >= 80) return 'score-high'
  if (score >= 60) return 'score-medium'
  return 'score-low'
}

// ---- 收藏模态框 ----
const showBookmarkModal = ref(false)
const savingBookmark = ref(false)

const bookmarkForm = reactive({
  title: '',
  tagsInput: '',
  tags: [],
  content: '',
  sourceType: 'knowledge',
  sourceInfo: {},
})

function openBookmarkModal(sourceType, content, title, sourceInfo) {
  bookmarkForm.sourceType = sourceType
  bookmarkForm.content = content
  bookmarkForm.sourceInfo = sourceInfo || {}
  bookmarkForm.tagsInput = ''
  bookmarkForm.tags = []
  // 自动生成标题：取内容前 30 个字
  bookmarkForm.title = title || content.replace(/\s+/g, ' ').trim().slice(0, 30) + (content.length > 30 ? '...' : '')
  showBookmarkModal.value = true
}

function closeBookmarkModal() {
  showBookmarkModal.value = false
}

function removeTag(idx) {
  bookmarkForm.tags.splice(idx, 1)
}

// 监听 tagsInput 变化，逗号分隔自动生成标签
watch(() => bookmarkForm.tagsInput, (val) => {
  if (val.includes(',')) {
    const parts = val.split(',')
    const newTag = parts[0].trim()
    if (newTag && !bookmarkForm.tags.includes(newTag)) {
      bookmarkForm.tags.push(newTag)
    }
    bookmarkForm.tagsInput = parts.slice(1).join(',').trim()
  }
})

// 收藏片段时构建来源信息（保留文档类型/页码/预览路径，供收藏页跳转原文）
function buildBookmarkSourceInfo(source) {
  return {
    source_image: source.source_image || '',
    source_type: source.source_type,
    page_number: source.page_number,
    pdf_preview_path: source.pdf_preview_path,
    kb_name: source.kb_name || '',
    index: source.index,
  }
}

// 来源文档展示名（优先知识库名，回退文件名）
function sourceDisplayName(info) {
  const name = (info.source_image || '').split('/').pop()
  return info.kb_name || name || '未知文档'
}

async function saveBookmark() {
  if (!bookmarkForm.title.trim() || !bookmarkForm.content.trim()) return
  savingBookmark.value = true
  try {
    // 处理 tagsInput 中可能剩余的标签
    const remaining = bookmarkForm.tagsInput.trim()
    const allTags = [...bookmarkForm.tags]
    if (remaining && !allTags.includes(remaining)) {
      allTags.push(remaining)
    }
    await createBookmark({
      title: bookmarkForm.title.trim(),
      tags: allTags,
      content: bookmarkForm.content,
      sourceType: bookmarkForm.sourceType,
      sourceInfo: bookmarkForm.sourceInfo,
    })
    closeBookmarkModal()
  } catch (err) {
    alert('收藏失败: ' + (err.response?.data?.detail || err.message))
  } finally {
    savingBookmark.value = false
  }
}
</script>

<style scoped>
.message {
  display: flex;
  gap: 12px;
  padding: 20px 24px;
  transition: background 0.2s;
}

.message.user {
  background: transparent;
}

.message.assistant {
  background: #fefcf9;
  border-bottom: 1px solid #f9eddb;
}

.avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  margin-top: 2px;
}

.message.user .avatar {
  background: #e8e2d9;
  color: var(--color-text);
}

.message.assistant .avatar {
  background: linear-gradient(135deg, #c98a4b, #e9bc7d);
  color: white;
}

.avatar-icon {
  width: 16px;
  height: 16px;
}

.avatar-icon.leaf {
  width: 18px;
  height: 18px;
}

.content-area {
  flex: 1;
  min-width: 0;
}

.sender-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: 6px;
}

.message-content {
  font-size: 15px;
  line-height: 1.75;
  color: #3d352a;
}

/* 用户附件回显 */
.message-attachments {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 6px;
}

.message-attachment-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 8px;
  background: rgba(255,255,255,0.25);
  border: 1px solid rgba(255,255,255,0.4);
  border-radius: 8px;
  font-size: 12px;
}

.message-attachment-chip.clickable {
  cursor: pointer;
  text-decoration: none;
  color: inherit;
  transition: all 0.2s;
}

.message-attachment-chip.clickable:hover {
  border-color: var(--color-primary);
  color: var(--color-primary);
  background: var(--color-primary-light);
}

.chip-external-icon {
  opacity: 0.6;
}

/* Sources section */
.sources-section,
.search-section {
  margin-top: 16px;
  padding: 12px 14px;
  background: #faf6f0;
  border-radius: 10px;
  border: 1px solid #ede6dc;
}

.sources-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-secondary);
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
}

.sources-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.source-item {
  background: white;
  border-radius: 8px;
  border: 1px solid #ede6dc;
  overflow: hidden;
  transition: border-color 0.2s;
}

.source-item.expanded {
  border-color: var(--color-primary);
}

/* 可点击的头部预览 */
.source-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  cursor: pointer;
  transition: background 0.15s;
}

.source-header:hover {
  background: #fefcf9;
}

.source-badge {
  flex-shrink: 0;
  background: var(--color-primary-light);
  color: var(--color-primary);
  font-size: 11px;
  font-weight: 500;
  padding: 1px 6px;
  border-radius: 4px;
}

.source-preview {
  flex: 1;
  font-size: 13px;
  color: var(--color-text-secondary);
  line-height: 1.5;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}

.source-toggle {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--color-primary);
  font-weight: 500;
}

/* 展开的详细内容 */
.source-detail {
  border-top: 1px solid #ede6dc;
  padding: 10px;
}

.source-full-text {
  font-size: 13px;
  line-height: 1.7;
  color: var(--color-text);
  white-space: pre-wrap;
  word-wrap: break-word;
  margin-bottom: 10px;
  max-height: 300px;
  overflow-y: auto;
}

.source-image-box {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
}

.source-image {
  max-width: 100%;
  max-height: 300px;
  border-radius: 6px;
  border: 1px solid #ede6dc;
  cursor: zoom-in;
  transition: opacity 0.2s;
}

.source-image:hover {
  opacity: 0.9;
}

.view-image-link {
  color: var(--color-primary);
  font-size: 12px;
  font-weight: 500;
  text-decoration: none;
}

.view-image-link:hover {
  text-decoration: underline;
}

/* Search section */
.search-title {
  display: block;
  margin-bottom: 6px;
  color: var(--color-primary);
  font-size: 13px;
  font-weight: 500;
  text-decoration: none;
}

.search-title:hover {
  text-decoration: underline;
}

.source-text {
  font-size: 13px;
  color: var(--color-text-secondary);
  line-height: 1.5;
  margin-top: 4px;
}

/* 引用跳转定位时的短暂高亮 */
.source-item.cite-flash {
  animation: cite-flash 1.6s ease;
}

@keyframes cite-flash {
  0%, 40% {
    border-color: var(--color-primary);
    box-shadow: 0 0 0 3px var(--color-primary-light);
  }
  100% {
    box-shadow: 0 0 0 3px transparent;
  }
}

/* Relevance score colors */
.score-high {
  background: #d4edda;
  color: #155724;
}

.score-medium {
  background: #fff3cd;
  color: #856404;
}

.score-low {
  background: #f8d7da;
  color: #721c24;
}

/* ---- 消息操作按钮 ---- */
.message-actions {
  margin-top: 12px;
  display: flex;
  gap: 8px;
  align-items: center;
}

.btn-action {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 10px;
  border: 1px solid #ede6dc;
  border-radius: 6px;
  background: white;
  color: var(--color-text-secondary);
  font-size: 12px;
  cursor: pointer;
  transition: all 0.2s;
}

.btn-action:hover {
  border-color: var(--color-primary);
  color: var(--color-primary);
  background: var(--color-primary-light);
}

/* ---- 反馈按钮 ---- */
.btn-feedback {
  width: 30px;
  height: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid #ede6dc;
  border-radius: 6px;
  background: white;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.2s;
  opacity: 0.7;
}

.btn-feedback:hover {
  opacity: 1;
  border-color: var(--color-primary);
}

.btn-feedback.active {
  opacity: 1;
  border-color: var(--color-primary);
  background: var(--color-primary-light);
  box-shadow: 0 0 0 1px var(--color-primary-light);
}

.feedback-comment {
  margin-top: 8px;
  padding: 10px 12px;
  background: #faf6f0;
  border: 1px solid #ede6dc;
  border-radius: 8px;
}

.feedback-comment-input {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid #ede6dc;
  border-radius: 6px;
  font-size: 13px;
  line-height: 1.5;
  font-family: inherit;
  resize: vertical;
  outline: none;
  box-sizing: border-box;
  background: white;
  color: var(--color-text);
}

.feedback-comment-input:focus {
  border-color: var(--color-primary);
}

.feedback-comment-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 8px;
}

.btn-cancel-sm,
.btn-submit-sm {
  padding: 5px 14px;
  border-radius: 6px;
  font-size: 12px;
  cursor: pointer;
  transition: all 0.2s;
}

.btn-cancel-sm {
  border: 1px solid #ede6dc;
  background: white;
  color: var(--color-text-secondary);
}

.btn-cancel-sm:hover {
  background: #f3efe9;
}

.btn-submit-sm {
  border: none;
  background: var(--color-primary);
  color: white;
}

.btn-submit-sm:hover {
  opacity: 0.9;
}

.btn-bookmark {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 5px 12px;
  border: 1px solid #ede6dc;
  border-radius: 6px;
  background: white;
  color: var(--color-text-secondary);
  font-size: 12px;
  cursor: pointer;
  transition: all 0.2s;
}

.btn-bookmark:hover {
  border-color: var(--color-primary);
  color: var(--color-primary);
  background: var(--color-primary-light);
}

/* ---- 来源详情操作按钮 ---- */
.source-detail-actions {
  margin-bottom: 8px;
}

.btn-bookmark-sm {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 10px;
  border: 1px solid #ede6dc;
  border-radius: 4px;
  background: white;
  color: var(--color-text-secondary);
  font-size: 11px;
  cursor: pointer;
  transition: all 0.2s;
}

.btn-bookmark-sm:hover {
  border-color: var(--color-primary);
  color: var(--color-primary);
  background: var(--color-primary-light);
}

/* ---- 收藏模态框 ---- */
.bookmark-overlay {
  position: fixed;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.bookmark-modal {
  background: white;
  border-radius: 14px;
  width: 520px;
  max-width: 90vw;
  max-height: 85vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.2);
  overflow: hidden;
}

.bookmark-modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 18px 22px;
  border-bottom: 1px solid #ede6dc;
}

.bookmark-modal-header h3 {
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
  margin: 0;
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
}

.modal-close:hover {
  background: #f3efe9;
  color: var(--color-text);
}

.bookmark-modal-body {
  padding: 20px 22px;
  overflow-y: auto;
  flex: 1;
}

.bookmark-modal-body .form-group {
  margin-bottom: 16px;
}

.bookmark-modal-body .form-label {
  display: block;
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-secondary);
  margin-bottom: 6px;
}

.bookmark-modal-body .form-input {
  width: 100%;
  padding: 9px 12px;
  border: 1px solid #ede6dc;
  border-radius: 8px;
  font-size: 13px;
  outline: none;
  transition: border-color 0.2s;
  box-sizing: border-box;
}

.bookmark-modal-body .form-input:focus {
  border-color: var(--color-primary);
}

.bookmark-modal-body .form-textarea {
  width: 100%;
  padding: 9px 12px;
  border: 1px solid #ede6dc;
  border-radius: 8px;
  font-size: 13px;
  line-height: 1.6;
  outline: none;
  resize: vertical;
  min-height: 100px;
  background: #faf6f0;
  color: var(--color-text);
  box-sizing: border-box;
  font-family: inherit;
}

.tags-display {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}

.tag-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: var(--color-primary-light);
  color: var(--color-primary);
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 4px;
}

.tag-remove {
  border: none;
  background: transparent;
  color: inherit;
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
  padding: 0;
  opacity: 0.6;
}

.tag-remove:hover {
  opacity: 1;
}

.bookmark-source-image {
  max-width: 100%;
  max-height: 200px;
  border-radius: 6px;
  border: 1px solid #ede6dc;
}

.bookmark-doc-source {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  background: #faf6f0;
  border: 1px solid #ede6dc;
  border-radius: 8px;
}

.bookmark-doc-name {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.doc-preview-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}

.bookmark-modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 14px 22px;
  border-top: 1px solid #ede6dc;
}

.btn-cancel {
  padding: 8px 18px;
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

.btn-save {
  padding: 8px 18px;
  border: none;
  border-radius: 8px;
  background: var(--color-primary);
  color: white;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s;
}

.btn-save:hover {
  opacity: 0.9;
}

.btn-save:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
