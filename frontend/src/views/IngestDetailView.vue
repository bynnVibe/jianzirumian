<template>
  <div class="ingest-detail-view">
    <!-- 顶部：返回 + 标题 -->
    <div class="detail-header">
      <button class="back-btn" @click="goBack">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
          <line x1="19" y1="12" x2="5" y2="12" />
          <polyline points="12 19 5 12 12 5" />
        </svg>
        返回入库历史
      </button>
    </div>

    <!-- 加载中 -->
    <div v-if="loading" class="detail-state">
      <div class="detail-spinner"></div>
      <span>正在加载解析文档…</span>
    </div>

    <!-- 错误 -->
    <div v-else-if="error" class="detail-state detail-error">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="40" height="40">
        <circle cx="12" cy="12" r="10" />
        <line x1="12" y1="8" x2="12" y2="12" />
        <line x1="12" y1="16" x2="12.01" y2="16" />
      </svg>
      <p>{{ error }}</p>
    </div>

    <template v-else-if="record">
      <!-- 概要卡片 -->
      <div class="detail-summary">
        <span class="sum-icon" :class="record.source_type">{{ typeIcon(record.source_type) }}</span>
        <div class="sum-main">
          <div class="sum-title" :title="record.title || record.source_name">
            {{ record.title || record.source_name }}
          </div>
          <div class="sum-meta">
            <span>入库时间：{{ formatDateTime(record.ingested_at) }}</span>
            <span class="dot">·</span>
            <span>入库片段：{{ record.entry_count }} 段</span>
            <span class="dot">·</span>
            <span>来源类型：{{ typeLabel(record.source_type) }}</span>
          </div>
          <div
            v-if="record.source_name && record.source_name !== record.title"
            class="sum-srcfile"
            :title="record.source_name"
          >
            源文件：{{ record.source_name }}
          </div>
        </div>
        <button
          v-if="record.source_file_exists"
          class="btn btn-outline btn-sm"
          @click="previewSource"
        >
          查看知识来源
        </button>
        <span v-else class="source-missing">源文件已删除</span>
      </div>

      <!-- 解析文档片段 -->
      <div class="fragments-section">
        <div class="frag-head">
          <span class="frag-head-title">解析文档</span>
          <span class="frag-head-count">解析内容 {{ fragments.length }} 段</span>
        </div>

        <div v-if="!fragments.length" class="detail-state">
          <p>该记录暂无解析文本内容</p>
        </div>

        <div v-else class="frag-list">
          <div v-for="(frag, idx) in fragments" :key="idx" class="frag-card">
            <div class="frag-card-head">
              <span v-if="frag.page_number" class="frag-page">第 {{ frag.page_number }} 页</span>
              <span v-else class="frag-page">片段 {{ idx + 1 }}</span>
              <span class="frag-len">{{ frag.text.length }} 字</span>
            </div>
            <div class="frag-text">{{ frag.text }}</div>
          </div>
        </div>
      </div>
    </template>

    <!-- 原始图片预览弹窗 -->
    <div v-if="previewImageSrc" class="preview-modal" @click.self="previewImageSrc = ''">
      <div class="preview-modal-content">
        <div class="preview-modal-header">
          <h3>知识来源预览</h3>
          <button class="modal-close" @click="previewImageSrc = ''" aria-label="关闭">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>
        <img :src="previewImageSrc" class="preview-img" alt="知识来源" />
      </div>
    </div>

    <!-- 轻量提示 -->
    <transition name="toast-fade">
      <div v-if="toast" class="detail-toast">{{ toast }}</div>
    </transition>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getIngestHistoryContent, getImageUrl as buildImageUrl } from '@/api'

const route = useRoute()
const router = useRouter()

const loading = ref(true)
const error = ref('')
const record = ref(null)
const fragments = ref([])
const previewImageSrc = ref('')

// 轻量提示（无外部 toast 组件，页面内自用）
const toast = ref('')
let toastTimer = null
function showToast(msg) {
  toast.value = msg
  if (toastTimer) clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toast.value = '' }, 3000)
}

// 详情页需要原始文件路径以复用现有预览端点
let sourcePath = ''
let pdfPreviewPath = ''

function typeIcon(type) {
  return type === 'image' ? '图' : type === 'word' ? 'W' : type === 'pdf' ? 'P' : '文'
}
function typeLabel(type) {
  return type === 'image' ? '图片' : type === 'word' ? 'Word 文档' : type === 'pdf' ? 'PDF 文档' : type
}
function formatDateTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function goBack() {
  router.push({ name: 'KnowledgeUpload', query: { tab: 'history' } })
}

function previewSource() {
  if (!record.value || !record.value.source_file_exists) return
  if (record.value.source_type === 'image') {
    previewImageSrc.value = buildImageUrl(sourcePath)
    return
  }
  // Word 记录必须有转换后的原始排版 PDF；缺失时不能让 PDF 阅读器加载 .docx
  if (record.value.source_type === 'word' && !pdfPreviewPath) {
    showToast('该记录未生成原始排版预览')
    return
  }
  // pdf 参数：Word 用预览 PDF，PDF 直接用源文件本身
  const pdfPath = record.value.source_type === 'word' ? pdfPreviewPath : sourcePath
  const pdfName = (pdfPath || '').split('/').pop()
  if (!pdfName) return
  // file 为磁盘真实文件名（下载原文用，Word 指向原始 docx），title 为展示名
  const diskName = (sourcePath || '').split('/').pop()
  const title = record.value.source_name || record.value.title || ''
  const fileParam = diskName ? `&file=${encodeURIComponent(diskName)}` : ''
  const titleParam = title ? `&title=${encodeURIComponent(title)}` : ''
  window.open(`/doc-preview?pdf=${encodeURIComponent(pdfName)}${fileParam}${titleParam}`, '_blank')
}

async function loadDetail() {
  loading.value = true
  error.value = ''
  try {
    const res = await getIngestHistoryContent(route.params.recordId)
    record.value = res.data.record || null
    fragments.value = res.data.fragments || []
    // 来源预览复用现有端点：优先用接口返回的源路径，回退到列表页传入参数
    sourcePath = res.data.record?.source_path || route.query.src || ''
    pdfPreviewPath = res.data.record?.pdf_preview_path || ''
  } catch (e) {
    error.value = e.response?.data?.detail || e.message || '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(loadDetail)
</script>

<style scoped>
.ingest-detail-view {
  max-width: 900px;
  margin: 0 auto;
  padding: 24px;
  /* 父容器 .main-content 为 100vh + overflow:hidden，页面需自身承担滚动 */
  height: 100%;
  overflow-y: auto;
}

/* ---------- 顶部返回 ---------- */
.detail-header { margin-bottom: 18px; }
.back-btn {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 7px 16px; border-radius: 10px; font-size: 13.5px; font-weight: 600;
  border: 1px solid #e0d5c5; background: #fff; color: #6b6156; cursor: pointer;
  transition: all 0.15s;
}
.back-btn:hover { border-color: #c98a4b; color: #c98a4b; }

/* ---------- 状态 ---------- */
.detail-state {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 14px; padding: 60px 20px; color: #b3a798; font-size: 14px;
}
.detail-error { color: #c0392b; }
.detail-spinner {
  width: 34px; height: 34px; border: 3px solid #ede6dc;
  border-top-color: #c98a4b; border-radius: 50%; animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ---------- 概要卡片 ---------- */
.detail-summary {
  display: flex; align-items: center; gap: 14px;
  background: #fff; border: 1px solid #e8dfd2; border-radius: 14px; padding: 18px 20px;
  margin-bottom: 18px;
}
.sum-icon {
  flex-shrink: 0; width: 42px; height: 42px; border-radius: 10px;
  background: #f9eddb; color: #84431f; font-size: 16px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
}
.sum-icon.word { background: #e8eef7; color: #4a6fa5; }
.sum-icon.pdf { background: #fbe6e2; color: #b5543f; }
.sum-main { flex: 1; min-width: 0; }
.sum-title {
  font-size: 17px; font-weight: 700; color: #2d2a24;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sum-meta {
  margin-top: 6px; font-size: 12.5px; color: #8a7e72;
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
}
.sum-meta .dot { color: #d3c7b6; }
.sum-srcfile {
  margin-top: 4px; font-size: 12px; color: #a2937f;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.source-missing { font-size: 12.5px; color: #b3a798; }

/* ---------- 片段 ---------- */
.fragments-section {
  background: #fff; border: 1px solid #e8dfd2; border-radius: 14px; padding: 18px 20px;
}
.frag-head {
  display: flex; align-items: baseline; gap: 10px; margin-bottom: 14px;
  padding-bottom: 12px; border-bottom: 1px dashed #e8dfd2;
}
.frag-head-title { font-size: 16px; font-weight: 700; color: #2d2a24; }
.frag-head-count { font-size: 12.5px; color: #8a7e72; }
.frag-list { display: flex; flex-direction: column; gap: 14px; }
.frag-card {
  border: 1px solid #ede4d6; border-radius: 12px; overflow: hidden; background: #fdfbf7;
}
.frag-card-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 14px; background: #f7f1e7; border-bottom: 1px solid #ede4d6;
}
.frag-page { font-size: 12.5px; font-weight: 700; color: #84431f; }
.frag-len { font-size: 11.5px; color: #a99a86; }
.frag-text {
  padding: 14px 16px; font-size: 14px; line-height: 1.9; color: #3d352a;
  white-space: pre-wrap; word-break: break-word;
}

/* ---------- 按钮 ---------- */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  padding: 8px 16px; border-radius: 8px; font-size: 13px; cursor: pointer;
  border: none; transition: all 0.15s;
}
.btn-sm { padding: 5px 12px; font-size: 12px; border-radius: 6px; }
.btn-outline { background: #fff; border: 1px solid #e0d5c5; color: #6b6156; }
.btn-outline:hover { border-color: #c98a4b; color: #c98a4b; }

/* ---------- 图片预览弹窗 ---------- */
.preview-modal {
  position: fixed; inset: 0; z-index: 1000; background: rgba(0, 0, 0, 0.7);
  display: flex; align-items: center; justify-content: center; padding: 24px;
}
.preview-modal-content {
  background: #fff; border-radius: 14px; max-width: 90vw; max-height: 90vh;
  display: flex; flex-direction: column; overflow: hidden;
}
.preview-modal-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 18px; border-bottom: 1px solid #ede4d6;
}
.preview-modal-header h3 { font-size: 15px; font-weight: 700; color: #2d2a24; }
.modal-close {
  background: none; border: none; cursor: pointer; color: #8a7e72;
  display: flex; align-items: center; padding: 4px; border-radius: 6px;
}
.modal-close:hover { background: #f3ece2; color: #c98a4b; }
.preview-img { max-width: 86vw; max-height: 78vh; object-fit: contain; display: block; }

/* ---------- 轻量提示 ---------- */
.detail-toast {
  position: fixed; left: 50%; bottom: 40px; transform: translateX(-50%);
  z-index: 1100; padding: 10px 20px; border-radius: 10px;
  background: rgba(45, 42, 36, 0.92); color: #fdfbf7; font-size: 13.5px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.18); pointer-events: none;
}
.toast-fade-enter-active, .toast-fade-leave-active { transition: opacity 0.25s, transform 0.25s; }
.toast-fade-enter-from, .toast-fade-leave-to {
  opacity: 0; transform: translateX(-50%) translateY(8px);
}
</style>
