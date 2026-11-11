<template>
  <div class="input-area">
    <!-- 配额友好提示：公共额度用尽→联系管理员；专属配额用尽→已降级公共密钥（均不阻断使用） -->
    <div v-if="quotaMessage" class="quota-banner" :class="quota.exhausted ? 'warn' : 'info'">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
        <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
      </svg>
      <span>{{ quotaMessage }}</span>
      <button class="quota-close" type="button" title="收起" @click="quotaDismissed = true">×</button>
    </div>
    <!-- 模式切换 (DeepSeek 风格) -->
    <div class="mode-bar">
      <label class="mode-toggle">
        <span class="mode-label" :class="{ active: useKnowledgeLocal }">
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
        <span class="mode-label" :class="{ active: useSearchLocal }">
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
      <ContextUsageBadge
        ref="ctxBadgeRef"
        class="ctx-badge-slot"
        :session-id="store.currentSessionId || ''"
      />
      <span class="mode-hint" v-if="modeHint">{{ modeHint }}</span>
      <label v-if="models.length" class="channel-select" title="选择出网模型（仅业务规则内模型；留空自动均衡）">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
          <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/>
        </svg>
        <select v-model="modelLocal" :disabled="disabled">
          <option value="">{{ defaultModelLabel }}</option>
          <option v-for="m in models" :key="m" :value="m">{{ m }}</option>
        </select>
      </label>
    </div>

    <!-- 附件 chips （拖入/选择/粘贴但尚未发送） -->
    <div v-if="attachments.length" class="attachment-chips">
      <div v-for="att in attachments" :key="att.id" class="attachment-chip">
        <svg v-if="att.kind === 'image'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
          <rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/>
        </svg>
        <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>
        </svg>
        <span class="attachment-name" :title="att.name">{{ att.name }}</span>
        <span v-if="att.pasted" class="attachment-tag">截图检索</span>
        <button class="attachment-remove" type="button" @click="removeAttachment(att.id)">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12">
            <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
          </svg>
        </button>
      </div>
    </div>
    <p v-if="hasPastedImage" class="attachment-hint">
      <template v-if="store.useKnowledge">已粘贴截图：发送后自动识别图中文字，并按识别内容检索知识库</template>
      <template v-else>已粘贴截图：发送后自动识别图中文字作答（未开启「知识检索」，不查知识库）</template>
      <template v-if="!inputText.trim()">（可直接发送，也可补一句你想问的）</template>
    </p>

    <!-- 输入框 (参考 DeepSeek) -->
    <div
      class="input-wrapper"
      :class="{ focused: isFocused, 'drag-over': isDragOver }"
      @dragover.prevent="isDragOver = true"
      @dragleave.prevent="isDragOver = false"
      @drop.prevent="handleDrop"
      @paste="handlePaste"
    >
      <button class="btn-attach" type="button" :disabled="disabled" @click="triggerFilePicker" title="添加图片/文档附件（也可直接粘贴截图）">
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
import { ref, computed, watch, nextTick, onMounted } from 'vue'
import { useChatStore } from '@/stores/chat'
import { getMyChatModels, getMyGatewayQuota } from '@/api'
import ContextUsageBadge from '@/components/ContextUsageBadge.vue'

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
  if (hasPastedImage.value) return '可直接发送：将识别截图文字并检索知识库；也可补一句你想问的...'
  if (attachments.value.length) return '给附件补充一句说明，如“帮我识别”“上传到知识库”“总结要点”...'
  return '输入你的问题，关于健康、养生、中医...（可直接粘贴截图检索）'
})

const canSend = computed(() => !!inputText.value.trim() || attachments.value.length > 0)

// 是否含粘贴进来的截图（决定是否展示「截图检索」提示与占位文案）
const hasPastedImage = computed(() => attachments.value.some((a) => a.pasted && a.kind === 'image'))

function detectKind(filename, mime) {
  const name = filename || ''
  const idx = name.lastIndexOf('.')
  const ext = idx === -1 ? '' : name.slice(idx).toLowerCase()
  if (ACCEPTED_IMAGE_EXTS.includes(ext)) return 'image'
  if (ACCEPTED_DOC_EXTS[ext]) return ACCEPTED_DOC_EXTS[ext]
  // 剪贴板截图可能没有扩展名（不同浏览器/系统命名不一），按 MIME 兜底判定
  if ((mime || '').startsWith('image/')) return 'image'
  return null
}

/** 截图命名：剪贴板图片常为无名或统一 image.png，改成可读名字便于附件回显与历史查看 */
function pasteImageName(file) {
  const mime = file.type || 'image/png'
  const ext = mime === 'image/jpeg' ? '.jpg' : mime === 'image/webp' ? '.webp'
    : mime === 'image/bmp' ? '.bmp' : '.png'
  const now = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  return `截图_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}${ext}`
}

/** File.name 只读：需重命名时重建 File（保留内容与 MIME，失败则沿用原对象） */
function withFileName(file, name) {
  try {
    return new File([file], name, { type: file.type, lastModified: file.lastModified })
  } catch (e) {
    return file
  }
}

function addFiles(fileList, fromPaste = false) {
  const files = Array.from(fileList || [])
  for (const raw of files) {
    const kind = detectKind(raw.name, raw.type)
    if (!kind) continue // 忽略不支持的类型，避免上传后端报错
    // 粘贴的截图重命名为「截图_时分秒.png」；选择/拖入的文件保留原名
    const isShot = fromPaste && kind === 'image'
    const name = isShot ? pasteImageName(raw) : raw.name
    const file = isShot ? withFileName(raw, name) : raw
    attachments.value.push({ id: `att-${++attachmentSeq}`, file, name, kind, pasted: isShot })
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

/**
 * 粘贴截图检索：剪贴板中的图片转为附件（发送后由后端 OCR 并并入检索查询）。
 * 监听挂在输入区容器上，覆盖 textarea 与附件区；纯文本粘贴不受影响。
 */
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
  if (!files.length) return
  // 阻止默认粘贴，避免文件名/二进制占位被当作文本插入输入框
  e.preventDefault()
  addFiles(files, true)
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

// 出网模型下拉：仅列匹配路由规则（chat_answer 绑定 → 默认规则）的上游模型，
// 选不了其他规则的模型；选中即按该模型出网，留空由网关在规则内解析。
// 下拉只展示模型本身，不展示规则名（规则属运维概念，不对普通用户暴露）。
const models = ref([])
const pinnedModel = ref('')
const modelLocal = ref(store.selectedModel)
watch(modelLocal, (val) => {
  store.selectedModel = val
})
const defaultModelLabel = computed(() => {
  if (pinnedModel.value) return `默认（${pinnedModel.value}）`
  return '默认（自动均衡）'
})
onMounted(async () => {
  loadQuota()
  try {
    const res = await getMyChatModels()
    models.value = res.data.models || []
    pinnedModel.value = res.data.pinned_model || ''
    // 规则模型清单变更后，已选模型可能不再可选 → 回落默认
    if (modelLocal.value && !models.value.includes(modelLocal.value)) {
      modelLocal.value = ''
    }
  } catch (e) {
    models.value = []
  }
})

// 上下文用量缩略图：每轮回答结束后刷新当前窗口水位（切换会话由组件自行 watch prop）
const ctxBadgeRef = ref(null)
watch(() => store.isGenerating, (v) => {
  if (!v) ctxBadgeRef.value?.refresh()
})

// 我的出网配额：友好提示（公共额度用尽→联系管理员；专属配额用尽→已降级），非阻断
const quota = ref({})
const quotaDismissed = ref(false)
const quotaMessage = computed(() => {
  if (quotaDismissed.value) return ''
  const q = quota.value || {}
  if (q.exhausted || q.degraded) return q.message || ''
  return ''
})
async function loadQuota() {
  try {
    const res = await getMyGatewayQuota()
    const next = res.data.quota || {}
    // 状态变化（如从降级恢复）时重新展示提示
    if ((next.message || '') !== (quota.value.message || '')) quotaDismissed.value = false
    quota.value = next
  } catch (e) {
    quota.value = {}
  }
}
// 每轮回答结束后刷新配额（用量在此时累加）
watch(() => store.isGenerating, (v) => { if (!v) loadQuota() })


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

/* 配额友好提示条（非阻断） */
.quota-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 4px 10px;
  padding: 8px 12px;
  border-radius: 10px;
  font-size: 12.5px;
  line-height: 1.5;
}
.quota-banner svg { flex-shrink: 0; }
.quota-banner span { flex: 1; }
.quota-banner.warn { background: #fbe4e3; color: #a8322a; border: 1px solid #f0c9c6; }
.quota-banner.info { background: #fdf0d9; color: #96691f; border: 1px solid #f0dcb4; }
.quota-close {
  border: none; background: transparent; cursor: pointer;
  font-size: 18px; line-height: 1; color: inherit; opacity: 0.6; padding: 0 2px;
}
.quota-close:hover { opacity: 1; }


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
}

/* 上下文用量缩略图槽位：领衔右侧组（auto 外边距把剩余空间留在它前面） */
.ctx-badge-slot {
  margin-left: auto;
}

.channel-select {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 12.5px;
  color: var(--color-text-secondary);
}
.channel-select svg { color: var(--color-primary); flex-shrink: 0; }
.channel-select select {
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 3px 8px;
  font-size: 12.5px;
  background: #fff;
  color: var(--color-text);
  outline: none;
  cursor: pointer;
  max-width: 150px;
}
.channel-select select:focus { border-color: var(--color-primary); }
.channel-select select:disabled { opacity: 0.5; cursor: not-allowed; }

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

/* 粘贴截图标记：提示该附件会走「OCR → 检索」链路 */
.attachment-tag {
  flex-shrink: 0;
  padding: 1px 6px;
  border-radius: 999px;
  background: #f3e6d5;
  color: var(--color-primary);
  font-size: 10.5px;
  font-weight: 600;
  white-space: nowrap;
}

.attachment-hint {
  padding: 0 4px 8px;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--color-text-secondary);
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
