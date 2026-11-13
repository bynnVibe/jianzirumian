<template>
  <div class="kb-documents">
    <div class="doc-toolbar">
      <div class="search-box">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="15" height="15">
          <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
        </svg>
        <input v-model="query" placeholder="搜索文档名称…" @keyup.enter="load" />
        <button v-if="query" class="clear-btn" @click="query = ''; load()">&times;</button>
      </div>
      <button class="btn-refresh" :disabled="loading" @click="load">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
          <path d="M23 4v6h-6M1 20v-6h6" />
          <path d="M3.5 9a9 9 0 0 1 14.9-3.4L23 10M1 14l4.6 4.4A9 9 0 0 0 20.5 15" />
        </svg>
        刷新
      </button>
      <button v-if="!isGuest" class="btn-upload" @click="$emit('upload')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12" />
        </svg>
        上传文档
      </button>
    </div>

    <p v-if="isGuest" class="guest-hint">游客模式仅展示前 3 篇文档，登录后可查看全部并上传</p>

    <div v-if="loading" class="state-box">加载中…</div>
    <div v-else-if="error" class="state-box error">{{ error }}</div>
    <div v-else-if="!documents.length" class="state-box">
      暂无文档{{ query ? '（无匹配结果）' : '，点击右上角「上传文档」开始入库' }}
    </div>

    <table v-else class="doc-table">
      <thead>
        <tr>
          <th class="col-name">名称</th>
          <th>大小</th>
          <th>分块</th>
          <th>状态</th>
          <th>更新时间</th>
          <th class="col-actions">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="doc in documents" :key="doc.source_image" :class="{ 'row-focus': doc.title === highlighted }">
          <td class="col-name">
            <div class="doc-name-cell">
              <span class="doc-type-tag" :class="doc.source_type">{{ typeLabel(doc.source_type) }}</span>
              <div class="doc-name-text">
                <span class="doc-title" :title="doc.title">{{ doc.title }}</span>
                <span class="doc-filename" :title="doc.filename">{{ doc.filename }}</span>
              </div>
            </div>
          </td>
          <td>{{ formatSize(doc.size_bytes) }}</td>
          <td>{{ doc.chunk_count }}</td>
          <td><span class="status-badge">已完成</span></td>
          <td class="time-cell">{{ formatTime(doc.updated_at) }}</td>
          <td class="col-actions">
            <button class="link-btn" @click="preview(doc)">预览</button>
            <button v-if="!isGuest" class="link-btn danger" @click="removeDoc(doc)">删除</button>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup>
import { nextTick, onMounted, ref, watch } from 'vue'
import { listKbDocuments, deleteKnowledgeEntriesBySource, getImageUrl } from '@/api'

const props = defineProps({
  kbId: { type: String, required: true },
  isGuest: { type: Boolean, default: false },
  focusSource: { type: String, default: '' },
})
const emit = defineEmits(['upload', 'changed'])

const documents = ref([])
const loading = ref(true)
const error = ref('')
const query = ref('')
const highlighted = ref('')
let _focusApplied = ''

// 思维导图节点跳转：按来源标题定位文档并自动预览 + 高亮
async function applyFocus() {
  const title = (props.focusSource || '').trim()
  if (!title || title === _focusApplied) return
  _focusApplied = title
  highlighted.value = title
  await nextTick()
  const doc = documents.value.find((d) => d.title === title)
    || documents.value.find((d) => (d.title || '').includes(title) || title.includes(d.title || ''))
  if (doc) {
    preview(doc)
    const el = document.querySelector('.kb-documents .row-focus')
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }
}

watch(() => props.focusSource, () => { _focusApplied = ''; applyFocus() })

const TYPE_LABELS = { image: '图片', pdf: 'PDF', word: 'Word', url: '网页', wiki: '百科', markdown: 'Markdown' }
function typeLabel(t) { return TYPE_LABELS[t] || t || '文件' }

function formatSize(bytes) {
  if (!bytes) return '-'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function formatTime(t) {
  if (!t) return '-'
  return String(t).replace('T', ' ').slice(0, 16)
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await listKbDocuments(props.kbId, query.value.trim())
    documents.value = res.data.documents || []
    applyFocus()
  } catch (e) {
    error.value = e.response?.data?.detail || '加载文档列表失败'
  } finally {
    loading.value = false
  }
}

function preview(doc) {
  const filename = (doc.source_image || '').split('/').pop()
  // Markdown：在线渲染预览（/doc-preview 拉取原文渲染），不再直接打开原始文件触发下载
  if (doc.source_type === 'markdown' || /\.(md|markdown)$/i.test(filename)) {
    const title = encodeURIComponent(doc.title || filename)
    window.open(`/doc-preview?file=${encodeURIComponent(filename)}&title=${title}`, '_blank')
    return
  }
  if (doc.source_type === 'word' && doc.pdf_preview_path) {
    const pdf = encodeURIComponent(doc.pdf_preview_path.split('/').pop())
    window.open(`/doc-preview?file=${encodeURIComponent(filename)}&pdf=${pdf}`, '_blank')
    return
  }
  window.open(getImageUrl(filename), '_blank')
}

async function removeDoc(doc) {
  if (!window.confirm(`确定删除文档「${doc.title}」？其全部分片与向量将一并删除。`)) return
  try {
    await deleteKnowledgeEntriesBySource(doc.source_image)
    await load()
    emit('changed')
  } catch (e) {
    window.alert(e.response?.data?.detail || '删除失败')
  }
}

onMounted(load)
defineExpose({ reload: load })
</script>

<style scoped>
.kb-documents { padding: 20px 24px; height: 100%; overflow-y: auto; box-sizing: border-box; }

.doc-toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; }

.search-box {
  display: flex; align-items: center; gap: 8px;
  background: #fff; border: 1px solid var(--color-border); border-radius: 8px;
  padding: 8px 12px; width: 280px; color: var(--color-text-secondary);
}
.search-box:focus-within { border-color: var(--color-primary); }
.search-box input { border: none; outline: none; flex: 1; font-size: 13px; background: transparent; color: var(--color-text); }
.clear-btn { border: none; background: none; font-size: 16px; cursor: pointer; color: var(--color-text-secondary); line-height: 1; }

.btn-refresh {
  display: inline-flex; align-items: center; gap: 5px;
  border: 1px solid var(--color-border); background: #fff; border-radius: 8px;
  padding: 8px 14px; font-size: 13px; color: var(--color-text); cursor: pointer;
}
.btn-refresh:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
.btn-refresh:disabled { opacity: 0.5; cursor: not-allowed; }

.btn-upload {
  margin-left: auto;
  display: inline-flex; align-items: center; gap: 6px;
  background: var(--color-text); color: #fff; border: none; border-radius: 8px;
  padding: 9px 18px; font-size: 13px; font-weight: 500; cursor: pointer;
}
.btn-upload:hover { opacity: 0.85; }

.guest-hint { font-size: 12px; color: var(--color-text-secondary); margin: 0 0 12px; }

.state-box { padding: 60px 0; text-align: center; color: var(--color-text-secondary); font-size: 14px; }
.state-box.error { color: #c0392b; }

.doc-table { width: 100%; border-collapse: collapse; background: #fff; border: 1px solid var(--color-border); border-radius: 10px; overflow: hidden; }
.doc-table th {
  text-align: left; font-size: 12px; font-weight: 600; color: var(--color-text-secondary);
  background: var(--color-sidebar); padding: 10px 14px; border-bottom: 1px solid var(--color-border);
}
.doc-table td { padding: 12px 14px; font-size: 13px; color: var(--color-text); border-bottom: 1px solid #f3efe9; }
.doc-table tbody tr:last-child td { border-bottom: none; }
.doc-table tbody tr:hover { background: #fdfaf5; }
.doc-table tbody tr.row-focus { background: var(--color-primary-light); }
.doc-table tbody tr.row-focus td { border-bottom-color: #e6d3bd; }

.col-name { width: 38%; }
.col-actions { width: 110px; }

.doc-name-cell { display: flex; align-items: center; gap: 10px; min-width: 0; }
.doc-type-tag {
  flex-shrink: 0; font-size: 11px; padding: 2px 7px; border-radius: 5px;
  background: var(--color-primary-light); color: var(--color-primary);
}
.doc-type-tag.pdf { background: #fdecea; color: #c0392b; }
.doc-type-tag.word { background: #e8f0fe; color: #3b6ecc; }
.doc-type-tag.url { background: #e8f5e9; color: #2e7d32; }
.doc-name-text { display: flex; flex-direction: column; min-width: 0; }
.doc-title { font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.doc-filename { font-size: 11px; color: var(--color-text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.status-badge { font-size: 12px; color: #2e7d32; background: #e8f5e9; padding: 2px 8px; border-radius: 6px; }
.time-cell { color: var(--color-text-secondary); font-size: 12px; white-space: nowrap; }

.link-btn { border: none; background: none; color: var(--color-primary); font-size: 13px; cursor: pointer; padding: 2px 6px; }
.link-btn:hover { text-decoration: underline; }
.link-btn.danger { color: #c0392b; }
</style>