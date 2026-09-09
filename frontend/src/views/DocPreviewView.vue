<template>
  <div class="doc-preview-page">
    <!-- 顶部工具栏 -->
    <div class="doc-toolbar">
      <div class="doc-toolbar-left">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="20" height="20">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <polyline points="14 2 14 8 20 8"/>
        </svg>
        <span class="doc-toolbar-title">{{ displayTitle }}</span>
        <span v-if="pageNum" class="doc-toolbar-page">第 {{ pageNum }} 页</span>
        <span v-if="isPdfMode" class="doc-toolbar-page">原始文档分页预览</span>
      </div>
      <div class="doc-toolbar-right">
        <a v-if="fileUrl" :href="fileUrl" :download="displayTitle" class="toolbar-btn">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="15" height="15">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
            <polyline points="7 10 12 15 17 10"/>
            <line x1="12" y1="15" x2="12" y2="3"/>
          </svg>
          下载原文
        </a>
      </div>
    </div>

    <!-- 内容区 -->
    <div class="doc-content-wrap">
      <!-- 加载中 -->
      <div v-if="loading" class="doc-state">
        <div class="doc-spinner"></div>
        <span>正在加载 Word 文档...</span>
      </div>
      <!-- 错误 -->
      <div v-else-if="error" class="doc-state doc-error">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="40" height="40">
          <circle cx="12" cy="12" r="10"/>
          <line x1="12" y1="8" x2="12" y2="12"/>
          <line x1="12" y1="16" x2="12.01" y2="16"/>
        </svg>
        <p>文档加载失败：{{ error }}</p>
        <a v-if="fileUrl" :href="fileUrl" :download="displayTitle" class="toolbar-btn">下载原文查看</a>
      </div>
      <!-- 原始文档 PDF 分页预览（Word 原始排版转换 / PDF 原件），浏览器阅读器自带分页导航 -->
      <iframe
        v-else-if="isPdfMode"
        :src="pdfUrl"
        class="doc-pdf-frame"
        :title="displayTitle"
      ></iframe>
      <!-- Word 未生成原始 PDF 预览：不展示解析后的文本，仅提供下载 -->
      <div v-else-if="isWordWithoutPdf" class="doc-state">
        <p>原始 Word 文档尚未生成 PDF 预览，可下载原文查看</p>
        <a v-if="fileUrl" :href="fileUrl" :download="displayTitle" class="toolbar-btn">下载原文</a>
      </div>
      <!-- Word HTML 渲染（旧数据兼容：仅有 pdf 预览模式才走 iframe，其余不展示解析文本） -->
      <div v-else class="doc-paper">
        <!-- 页码导航锚点 -->
        <div v-if="pageNum && pageAnchors.length > 0" class="doc-page-nav">
          <span>已定位到第 {{ pageNum }} 页内容</span>
        </div>
        <div
          ref="htmlContainer"
          class="doc-html-content"
          v-html="docHtml"
        ></div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import mammoth from 'mammoth'
import { getImageUrl as buildImageUrl } from '@/api'

const route = useRoute()
const filename = ref(route.query.file || '')
const pageNum = ref(route.query.page ? parseInt(route.query.page) : null)
// 聊天附件预览会直接传入文件分发 URL（游客可用）；知识库来源仍走 /uploads?token=
const directUrl = ref(route.query.url || '')
// Word 原始排版转换后的 PDF 文件名：存在时以 PDF 分页预览原始文档
const pdfName = ref(route.query.pdf || '')
// 可选显示标题：调用方传入的 source_name 等展示名（与真实磁盘文件名 file 解耦）
const titleName = ref(route.query.title || '')
const isPdfMode = computed(() => !!pdfName.value)
const pdfUrl = computed(() => {
  if (!pdfName.value) return ''
  const base = buildImageUrl(pdfName.value)
  return pageNum.value ? `${base}#page=${pageNum.value}` : base
})

// Word 文件未附带 pdf 参数时，不再展示 mammoth 解析后的文本，只提供下载原文
const isWordWithoutPdf = computed(() => {
  const name = filename.value.toLowerCase()
  return (name.endsWith('.docx') || name.endsWith('.doc')) && !pdfName.value
})

// 工具栏标题：优先用调用方传入的展示名 title，其次真实文件名，最后回退 pdf 名
const displayTitle = computed(() => titleName.value || filename.value || pdfName.value || '')

const loading = ref(true)
const error = ref('')
const docHtml = ref('')
const fileUrl = ref('')
const htmlContainer = ref(null)
const pageAnchors = ref([])

onMounted(async () => {
  // 仅传 pdf 参数（原始文档分页预览）时无需 file，故 filename 与 pdf 均缺失才报错
  if (!filename.value && !isPdfMode.value) {
    error.value = '未指定文件名'
    loading.value = false
    return
  }

  // 构造文件 URL：优先使用直传的分发地址，否则用带 token 的 /uploads 地址
  fileUrl.value = directUrl.value || (filename.value ? buildImageUrl(filename.value) : '')

  // PDF 模式：iframe 直接加载，无需 mammoth 解析
  if (isPdfMode.value) {
    // 仅传 pdf 参数时没有 file，下载原文回退到 pdf 本身的分发地址
    if (!fileUrl.value) fileUrl.value = buildImageUrl(pdfName.value)
    loading.value = false
    return
  }

  // Word 文件未生成 PDF 预览：直接结束加载，模板显示下载提示
  if (isWordWithoutPdf.value) {
    loading.value = false
    return
  }

  try {
    const response = await fetch(fileUrl.value)
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    const arrayBuffer = await response.arrayBuffer()
    const result = await mammoth.convertToHtml({ arrayBuffer })
    docHtml.value = result.value || ''
    loading.value = false

    // 如果有页码，等 DOM 更新后滚动到对应段落
    if (pageNum.value) {
      await nextTick()
      scrollToPage(pageNum.value)
    }
  } catch (err) {
    console.error('Word 预览加载失败:', err)
    error.value = err.message || '未知错误'
    loading.value = false
  }
})

/**
 * 尝试滚动到指定页内容。
 * Word 文档里没有原生"页"概念，按以下策略估算：
 *  - 把所有段落按页估算：约每 N 个段落为一页（N = 总段落/总页数）
 *  - 如果元数据里没有总页数，则直接滚动到占比位置
 */
function scrollToPage(page) {
  if (!htmlContainer.value) return
  const paras = htmlContainer.value.querySelectorAll('p, h1, h2, h3, h4, table, li')
  if (!paras.length) return
  const totalParas = paras.length
  // 估算：文档中每页约 10 个段落（可调整）
  const parasPerPage = Math.max(1, Math.floor(totalParas / Math.max(1, page + 2)))
  const targetIdx = Math.min((page - 1) * parasPerPage, totalParas - 1)
  const targetEl = paras[targetIdx]
  if (targetEl) {
    // 延迟一点让页面完全渲染后再滚动
    setTimeout(() => {
      targetEl.scrollIntoView({ behavior: 'smooth', block: 'start' })
      // 高亮目标段落
      targetEl.classList.add('page-highlight')
      setTimeout(() => targetEl.classList.remove('page-highlight'), 2000)
    }, 100)
  }
}
</script>

<style scoped>
.doc-preview-page {
  min-height: 100vh;
  background: #f5f1eb;
  display: flex;
  flex-direction: column;
}

/* 顶部工具栏 */
.doc-toolbar {
  position: sticky;
  top: 0;
  z-index: 10;
  background: white;
  border-bottom: 1px solid #e5ddd4;
  padding: 10px 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  box-shadow: 0 1px 4px rgba(0,0,0,0.06);
}

.doc-toolbar-left {
  display: flex;
  align-items: center;
  gap: 10px;
  color: #c98a4b;
}

.doc-toolbar-title {
  font-size: 14px;
  font-weight: 600;
  color: #3d352a;
  max-width: 500px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.doc-toolbar-page {
  font-size: 12px;
  background: #f3efe9;
  color: #c98a4b;
  padding: 2px 10px;
  border-radius: 20px;
  font-weight: 500;
}

.doc-toolbar-right {
  display: flex;
  gap: 10px;
}

.toolbar-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 6px 14px;
  border-radius: 8px;
  background: #f3efe9;
  color: #c98a4b;
  font-size: 13px;
  font-weight: 500;
  text-decoration: none;
  transition: background 0.2s;
}
.toolbar-btn:hover { background: #ede6dc; }

/* 内容区 */
.doc-content-wrap {
  flex: 1;
  padding: 32px 20px;
  overflow-y: auto;
}

.doc-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 16px;
  min-height: 300px;
  color: #888;
  font-size: 14px;
}

.doc-error { color: #c0392b; }

.doc-spinner {
  width: 36px;
  height: 36px;
  border: 3px solid #ede6dc;
  border-top-color: #c98a4b;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin { to { transform: rotate(360deg); } }

/* 文档纸张效果 */
.doc-paper {
  max-width: 860px;
  margin: 0 auto;
  background: white;
  border-radius: 8px;
  box-shadow: 0 2px 12px rgba(0,0,0,0.08);
  padding: 48px 56px;
  min-height: 600px;
}

.doc-page-nav {
  font-size: 12px;
  color: #c98a4b;
  background: #faf8f5;
  border: 1px solid #ede6dc;
  border-radius: 6px;
  padding: 6px 14px;
  margin-bottom: 24px;
}

/* mammoth 渲染的文档内容样式 */
.doc-html-content {
  font-size: 15px;
  line-height: 1.85;
  color: #2c2c2c;
}

.doc-html-content :deep(p) {
  margin: 8px 0;
}

.doc-html-content :deep(h1) {
  font-size: 20px;
  font-weight: 700;
  margin: 20px 0 10px;
  color: #1a1a1a;
}

.doc-html-content :deep(h2) {
  font-size: 17px;
  font-weight: 700;
  margin: 16px 0 8px;
  color: #1a1a1a;
}

.doc-html-content :deep(h3) {
  font-size: 15px;
  font-weight: 600;
  margin: 14px 0 6px;
}

/* 表格 */
.doc-html-content :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: 16px 0;
  font-size: 14px;
}

.doc-html-content :deep(td),
.doc-html-content :deep(th) {
  border: 1px solid #d0c8be;
  padding: 8px 12px;
  vertical-align: top;
  line-height: 1.6;
}

.doc-html-content :deep(th) {
  background: #f3efe9;
  font-weight: 600;
}

/* 页码定位高亮 */
.doc-html-content :deep(.page-highlight) {
  background: #fff8e1;
  border-left: 3px solid #c98a4b;
  padding-left: 8px;
  transition: background 1.5s ease;
}

/* 原始文档 PDF 分页预览 */
.doc-pdf-frame {
  width: 100%;
  height: calc(100vh - 57px);
  border: none;
  background: white;
}
</style>
