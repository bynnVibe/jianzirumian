<template>
  <div class="input-area">
    <!-- 模式切换 (DeepSeek 风格) -->
    <div class="mode-bar">
      <label class="mode-toggle">
        <span class="mode-label" :class="{ active: useKnowledge }">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
            <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
            <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
          </svg>
          知识检索
        </span>
        <div class="toggle-switch">
          <input type="checkbox" v-model="useKnowledgeLocal" />
          <span class="toggle-slider"></span>
        </div>
      </label>
      <label class="mode-toggle">
        <span class="mode-label" :class="{ active: useSearch }">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
            <circle cx="11" cy="11" r="8"/>
            <line x1="21" y1="21" x2="16.65" y2="16.65"/>
          </svg>
          联网搜索
        </span>
        <div class="toggle-switch">
          <input type="checkbox" v-model="useSearchLocal" />
          <span class="toggle-slider"></span>
        </div>
      </label>
      <span class="mode-hint" v-if="modeHint">{{ modeHint }}</span>
    </div>

    <!-- 附件 chips （拖入/选择但尚未发送） -->
    <div v-if="attachments.length" class="attachment-chips">
      <div v-for="att in attachments" :key="att.id" class="attachment-chip">
        <svg v-if="att.kind === 'image'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
          <rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/>
        </svg>
        <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>
        </svg>
        <span class="attachment-name" :title="att.name">{{ att.name }}</span>
        <button class="attachment-remove" type="button" @click="removeAttachment(att.id)">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12">
            <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
          </svg>
        </button>
      </div>
    </div>

    <!-- 输入框 (参考 DeepSeek) -->
    <div
      class="input-wrapper"
      :class="{ focused: isFocused, 'drag-over': isDragOver }"
      @dragover.prevent="isDragOver = true"
      @dragleave.prevent="isDragOver = false"
      @drop.prevent="handleDrop"
    >
      <button class="btn-attach" type="button" :disabled="disabled" @click="triggerFilePicker" title="添加图片/文档附件">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
          <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>
        </svg>
      </button>
      <input
        ref="fileInputRef"
        type="file"
        class="hidden-file-input"
        multiple
        accept=".jpg,.jpeg,.png,.bmp,.tiff,.webp,.docx,.pdf"
        @change="handleFilePicked"
      />
      <textarea
        ref="textareaRef"
        v-model="inputText"
        class="chat-textarea"
        :placeholder="placeholder"
        :disabled="disabled"
        rows="1"
        @input="autoResize"
        @focus="isFocused = true"
        @blur="isFocused = false"
        @keydown="handleKeydown"
        @paste="handlePaste"
      ></textarea>
      <button
        class="btn-send"
        :class="{ active: canSend }"
        :disabled="disabled || !canSend"
        @click="handleSend"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
          <line x1="22" y1="2" x2="11" y2="13"/>
          <polygon points="22 2 15 22 11 13 2 9 22 2"/>
        </svg>
      </button>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick } from 'vue'
import { useChatStore } from '@/stores/chat'

const store = useChatStore()
const emit = defineEmits(['send'])

const inputText = ref('')
const isFocused = ref(false)
const isDragOver = ref(false)
const textareaRef = ref(null)
const fileInputRef = ref(null)
const attachments = ref([]) // [{ id, file, name, kind }]

const ACCEPTED_IMAGE_EXTS = ['.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp']
const ACCEPTED_DOC_EXTS = { '.docx': 'word', '.pdf': 'pdf' }
let attachmentSeq = 0

const props = defineProps({
  disabled: { type: Boolean, default: false },
})

const placeholder = computed(() => {
  if (store.isGenerating) return '正在生成回答...'
  if (attachments.value.length) return '给附件补充一句说明，如“帮我识别”“上传到知识库”“总结要点”...'
  return '输入你的问题，关于健康、养生、中医...'
})

const canSend = computed(() => !!inputText.value.trim() || attachments.value.length > 0)

function detectKind(filename) {
  const idx = filename.lastIndexOf('.')
  if (idx === -1) return null
  const ext = filename.slice(idx).toLowerCase()
  if (ACCEPTED_IMAGE_EXTS.includes(ext)) return 'image'
  return ACCEPTED_DOC_EXTS[ext] || null
}

function addFiles(fileList) {
  const files = Array.from(fileList || [])
  for (const file of files) {
    const kind = detectKind(file.name)
    if (!kind) continue // 忽略不支持的类型，避免上传后端报错
    attachments.value.push({ id: `att-${++attachmentSeq}`, file, name: file.name, kind })
  }
}

function removeAttachment(id) {
  attachments.value = attachments.value.filter((a) => a.id !== id)
}

function triggerFilePicker() {
  fileInputRef.value?.click()
}

function handleFilePicked(e) {
  addFiles(e.target.files)
  e.target.value = '' // 允许重复选择同一文件
}

function handleDrop(e) {
  isDragOver.value = false
  addFiles(e.dataTransfer?.files)
}

function handlePaste(e) {
  const items = e.clipboardData?.items
  if (!items) return
  const files = []
  for (const item of items) {
    if (item.kind === 'file') {
      const file = item.getAsFile()
      if (file) files.push(file)
    }
  }
  if (files.length) addFiles(files)
}

// 本地 toggle 状态
const useKnowledgeLocal = ref(store.useKnowledge)
const useSearchLocal = ref(store.useSearch)

watch(useKnowledgeLocal, (val) => {
  store.useKnowledge = val
})

watch(useSearchLocal, (val) => {
  store.useSearch = val
})

// 消费再编辑草稿：历史问题回填输入框并聚焦
watch(
  () => store.editingDraft,
  async (draft) => {
    if (!draft) return
    inputText.value = draft.text
    store.editingDraft = null
    await nextTick()
    autoResize()
    textareaRef.value?.focus()
    // 光标移到末尾
    const el = textareaRef.value
    if (el) el.setSelectionRange(el.value.length, el.value.length)
  },
  { immediate: true }
)

const modeHint = computed(() => {
  if (useKnowledgeLocal.value && useSearchLocal.value) {
    return '将从知识库和网络中综合检索'
  }
  if (useKnowledgeLocal.value) {
    return '仅从知识库中检索'
  }
  if (useSearchLocal.value) {
    return '仅从网络中检索'
  }
  return '将基于模型自身知识回答'
})

function autoResize() {
  const el = textareaRef.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 200) + 'px'
}

function handleKeydown(e) {
  // Enter 发送 (Shift+Enter 换行)
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    handleSend()
  }
}

function handleSend() {
  const text = inputText.value.trim()
  if (!canSend.value || props.disabled) return
  const files = attachments.value.map((a) => a.file)
  emit('send', text, files)
  inputText.value = ''
  attachments.value = []
  autoResize()
}

// 暴露 focus 方法
function focus() {
  textareaRef.value?.focus()
}

defineExpose({ focus })
</script>

<style scoped>
.input-area {
  padding: 12px 24px 20px;
  background: linear-gradient(transparent, var(--color-bg) 30%);
}

.mode-bar {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 0 4px 8px;
  flex-wrap: wrap;
}

.mode-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  user-select: none;
}

.mode-label {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 13px;
  color: var(--color-text-secondary);
  transition: color 0.2s;
}

.mode-label.active {
  color: var(--color-text);
  font-weight: 500;
}

.mode-hint {
  font-size: 12px;
  color: var(--color-text-secondary);
  opacity: 0.7;
  margin-left: auto;
}

.input-wrapper {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  padding: 12px 16px;
  background: white;
  border: 1.5px solid var(--color-border);
  border-radius: 16px;
  transition: all 0.2s;
  box-shadow: 0 1px 4px rgba(0,0,0,0.04);
}

.input-wrapper.focused {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px rgba(212, 133, 46, 0.1);
}

.input-wrapper.drag-over {
  border-color: var(--color-primary);
  background: var(--color-primary-light, #fdf3e7);
}

.attachment-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 0 4px 8px;
}

.attachment-chip {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 8px;
  background: #f5efe6;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  font-size: 12px;
  color: var(--color-text);
  max-width: 220px;
}

.attachment-chip svg {
  flex-shrink: 0;
  color: var(--color-primary);
}

.attachment-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.attachment-remove {
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  padding: 0;
  flex-shrink: 0;
}

.attachment-remove:hover {
  color: var(--color-primary);
}

.btn-attach {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  border: none;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  background: transparent;
  color: var(--color-text-secondary);
  transition: all 0.2s;
  flex-shrink: 0;
}

.btn-attach:hover {
  background: #ede6dc;
  color: var(--color-primary);
}

.btn-attach:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.hidden-file-input {
  display: none;
}

.chat-textarea {
  flex: 1;
  border: none;
  outline: none;
  resize: none;
  font-family: inherit;
  font-size: 15px;
  line-height: 1.6;
  color: var(--color-text);
  background: transparent;
  max-height: 200px;
  min-height: 24px;
}

.chat-textarea::placeholder {
  color: #bfb1a1;
}

.btn-send {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  border: none;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  background: #ede6dc;
  color: #bfb1a1;
  transition: all 0.2s;
  flex-shrink: 0;
}

.btn-send.active {
  background: var(--color-primary);
  color: white;
}

.btn-send.active:hover {
  background: #c56d23;
}

.btn-send:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}
</style>
