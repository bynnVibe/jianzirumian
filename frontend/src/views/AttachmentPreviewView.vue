<template>
  <div class="att-preview-page">
    <!-- 顶部工具栏 -->
    <div class="att-toolbar">
      <div class="att-toolbar-left">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="20" height="20">
          <rect x="3" y="3" width="18" height="18" rx="2"/>
          <circle cx="8.5" cy="8.5" r="1.5"/>
          <path d="M21 15l-5-5L5 21"/>
        </svg>
        <span class="att-toolbar-title">{{ filename }}</span>
      </div>
      <div class="att-toolbar-right">
        <a v-if="fileUrl" :href="fileUrl" :download="filename" class="toolbar-btn">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="15" height="15">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
            <polyline points="7 10 12 15 17 10"/>
            <line x1="12" y1="15" x2="12" y2="3"/>
          </svg>
          下载原文件
        </a>
      </div>
    </div>

    <!-- 内容区 -->
    <div class="att-content-wrap">
      <!-- 加载中 -->
      <div v-if="loading" class="att-state">
        <div class="att-spinner"></div>
        <span>正在加载预览...</span>
      </div>
      <!-- 错误 -->
      <div v-else-if="error" class="att-state att-error">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="40" height="40">
          <circle cx="12" cy="12" r="10"/>
          <line x1="12" y1="8" x2="12" y2="12"/>
          <line x1="12" y1="16" x2="12.01" y2="16"/>
        </svg>
        <p>预览加载失败：{{ error }}</p>
        <a v-if="fileUrl" :href="fileUrl" :download="filename" class="toolbar-btn">下载原文件查看</a>
      </div>
      <!-- PDF：浏览器内嵌阅读器 -->
      <iframe v-else-if="kind === 'pdf'" :src="fileUrl" class="att-pdf-frame" :title="filename"></iframe>
      <!-- 图片：居中原图（v-show 保证 img 始终在 DOM 中，才能触发 load/error 事件） -->
      <div v-show="!loading && !error && kind !== 'pdf'" class="att-image-box">
        <img :src="fileUrl" :alt="filename" class="att-image" @load="loading = false" @error="onImageError" />
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useRoute } from 'vue-router'
import { getAttachmentFileUrl } from '@/api'

const route = useRoute()
const fileId = ref(route.query.id || '')
const filename = ref(route.query.name || '附件')

// 类型：优先 query 指定，其次按扩展名推断
const kind = computed(() => {
  if (route.query.kind) return route.query.kind
  const n = filename.value.toLowerCase()
  if (n.endsWith('.pdf')) return 'pdf'
  return 'image'
})

const loading = ref(true)
const error = ref('')
// 附件分发接口游客可用，无需 token
const fileUrl = computed(() => getAttachmentFileUrl(fileId.value))

if (!fileId.value) {
  error.value = '未指定附件'
  loading.value = false
} else if (kind.value === 'pdf') {
  // iframe 自行加载，无需等待
  loading.value = false
}

function onImageError() {
  error.value = '图片加载失败或文件不存在'
  loading.value = false
}
</script>

<style scoped>
.att-preview-page {
  height: 100vh;
  background: #f5f1eb;
  display: flex;
  flex-direction: column;
}

/* 顶部工具栏 */
.att-toolbar {
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

.att-toolbar-left {
  display: flex;
  align-items: center;
  gap: 10px;
  color: #c98a4b;
  min-width: 0;
}

.att-toolbar-title {
  font-size: 14px;
  font-weight: 600;
  color: #3d352a;
  max-width: 60vw;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.att-toolbar-right {
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
.att-content-wrap {
  flex: 1;
  min-height: 0;
  overflow: auto;
  display: flex;
  flex-direction: column;
}

.att-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 16px;
  min-height: 300px;
  color: #888;
  font-size: 14px;
}

.att-error { color: #c0392b; }

.att-spinner {
  width: 36px;
  height: 36px;
  border: 3px solid #ede6dc;
  border-top-color: #c98a4b;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin { to { transform: rotate(360deg); } }

/* 图片居中展示 */
.att-image-box {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.att-image {
  max-width: 100%;
  max-height: calc(100vh - 120px);
  border-radius: 8px;
  box-shadow: 0 4px 24px rgba(0,0,0,0.12);
  background: white;
}

/* PDF 内嵌阅读器 */
.att-pdf-frame {
  flex: 1;
  width: 100%;
  border: none;
}
</style>
