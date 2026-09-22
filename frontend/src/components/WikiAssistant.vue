<template>
  <div v-if="visible" class="assistant-root">
    <!-- 折叠态：右侧悬浮入口 -->
    <button v-if="!store.isOpen" class="assistant-fab" @click="store.open()" title="知识助手">
      <span class="fab-icon">✦</span>
      <span class="fab-text">知识助手</span>
    </button>

    <!-- 展开态：问答面板 -->
    <transition name="assistant-slide">
      <section v-if="store.isOpen" class="assistant-panel" aria-label="知识助手">
        <header class="panel-head">
          <div class="head-title">
            <span class="head-dot"></span>
            <span>知识助手</span>
            <span class="head-sub">Agent</span>
          </div>
          <div class="head-actions">
            <button class="icon-btn" @click="store.reset()" title="清空对话" :disabled="store.isGenerating">⟳</button>
            <button class="icon-btn" @click="store.toggle()" title="收起">✕</button>
          </div>
        </header>

        <!-- 当前上下文提示 -->
        <div class="panel-context" v-if="store.context.page_title">
          <span class="ctx-tag">当前词条</span>
          《{{ store.context.page_title }}》
        </div>
        <div class="panel-context" v-else-if="store.context.route_label">
          <span class="ctx-tag">当前页面</span>
          {{ store.context.route_label }}
        </div>

        <!-- 消息区 -->
        <div class="panel-body" ref="bodyRef">
          <!-- 欢迎态 -->
          <div v-if="store.messages.length === 0" class="welcome">
            <p class="welcome-title">你好，我是知识助手 👋</p>
            <p class="welcome-desc">
              我可以基于当前知识页面做主题分析、解释你的疑惑、总结整体知识文档，
              也能在你确认后帮你订正词条内容。试试下面的快捷提问：
            </p>
            <div class="chips">
              <button v-for="c in quickChips" :key="c" class="chip" @click="sendQuick(c)">{{ c }}</button>
            </div>
          </div>

          <!-- 消息列表 -->
          <div v-for="(m, i) in store.messages" :key="i" class="msg" :class="m.role">
            <div v-if="m.role === 'assistant'" class="msg-avatar">✦</div>
            <div class="msg-bubble">
              <!-- 思考 -->
              <div v-if="m.thought" class="msg-thought">💭 {{ m.thought }}</div>

              <!-- 工具调用进度 -->
              <div v-if="m.tools && m.tools.length" class="msg-tools">
                <div v-for="(t, ti) in m.tools" :key="ti" class="tool-line">
                  <span v-if="t.state === 'running'" class="tool-spinner"></span>
                  <span v-else class="tool-check">✓</span>
                  <span class="tool-name">{{ toolLabel(t.tool) }}</span>
                  <span class="tool-summary" v-if="t.summary">{{ t.summary }}</span>
                </div>
              </div>

              <!-- 答案正文 -->
              <div
                v-if="m.content"
                class="msg-content markdown-content"
                :class="{ error: m.error }"
                v-html="render(m.content)"
              ></div>
              <div v-else-if="m.streaming" class="typing">
                <span></span><span></span><span></span>
              </div>

              <!-- 编辑确认卡片 -->
              <div v-if="m.edit" class="edit-card" :class="m.edit.status">
                <div class="edit-head">
                  <span class="edit-badge">修改建议</span>
                  <span class="edit-title">《{{ m.edit.page_title }}》</span>
                </div>
                <p class="edit-reason" v-if="m.edit.reason">理由：{{ m.edit.reason }}</p>
                <div class="edit-diff">
                  <div class="diff-col">
                    <div class="diff-label old">原文</div>
                    <pre class="diff-pre old">{{ m.edit.old_content }}</pre>
                  </div>
                  <div class="diff-col">
                    <div class="diff-label new">修改后</div>
                    <pre class="diff-pre new">{{ m.edit.new_content }}</pre>
                  </div>
                </div>
                <div class="edit-actions" v-if="m.edit.status === 'pending'">
                  <button class="btn-reject" :disabled="editBusy === m.edit.id" @click="onReject(m.edit.id)">驳回</button>
                  <button class="btn-approve" :disabled="editBusy === m.edit.id" @click="onApprove(m.edit.id)">
                    {{ editBusy === m.edit.id ? '应用中…' : '应用修改' }}
                  </button>
                </div>
                <div class="edit-done" v-else>
                  {{ m.edit.status === 'approved' ? '✓ 已应用到知识库' : '已驳回' }}
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- 输入区 -->
        <div class="panel-input">
          <div class="chips chips-inline" v-if="store.messages.length">
            <button
              v-for="c in quickChips"
              :key="c"
              class="chip chip-sm"
              :disabled="store.isGenerating"
              @click="sendQuick(c)"
            >{{ c }}</button>
          </div>
          <div class="input-row">
            <textarea
              v-model="draft"
              ref="inputRef"
              class="input-box"
              rows="1"
              placeholder="向知识助手提问…"
              :disabled="store.isGenerating"
              @keydown.enter.exact.prevent="onSend"
              @input="autoGrow"
            ></textarea>
            <button class="send-btn" :disabled="store.isGenerating || !draft.trim()" @click="onSend" title="发送">
              <span v-if="store.isGenerating" class="send-spinner"></span>
              <span v-else>➤</span>
            </button>
          </div>
          <div class="status-line" v-if="store.statusText">{{ store.statusText }}</div>
        </div>
      </section>
    </transition>
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import MarkdownIt from 'markdown-it'
import { useAssistantStore } from '@/stores/assistant'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const store = useAssistantStore()
const auth = useAuthStore()
const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

const draft = ref('')
const bodyRef = ref(null)
const inputRef = ref(null)
const editBusy = ref('')

// 对话/登录/指南/独立预览页不显示助手（与侧边栏隐藏规则一致，且助手需登录）
const HIDDEN_ROUTES = ['Login', 'Register', 'Guide', 'Chat', 'ChatWithSession', 'DocPreview', 'AttachmentPreview']
const ROUTE_LABELS = {
  Wiki: '知识百科',
  KnowledgeBase: '知识库管理',
  Management: '系统管理',
  KnowledgeUpload: '知识库上传',
  IngestDetail: '入库详情',
  Bookmarks: '我的收藏',
  UserManagement: '用户管理',
  Eval: '回归评测',
}

const visible = computed(() => auth.isAuthenticated && !HIDDEN_ROUTES.includes(route.name))

const quickChips = computed(() => {
  if (store.context.page_id) {
    return ['分析当前页面的主题', '解释这个词条的关键要点', '这个主题还有哪些相关资料？', '帮我订正当前词条的表述']
  }
  return ['总结整体知识文档', '知识库里有哪些主题？', '帮我梳理最近的资料', '检索一个我关心的概念']
})

const TOOL_LABELS = {
  read_current_page: '读取当前页面',
  search_knowledge: '检索知识库',
  list_wiki_pages: '浏览百科目录',
  read_wiki_page: '读取百科词条',
  get_source_text: '读取原始资料',
  propose_page_edit: '生成修改建议',
}

function toolLabel(name) {
  return TOOL_LABELS[name] || name || '工具'
}

function render(text) {
  try {
    return md.render(text || '')
  } catch (e) {
    return text
  }
}

function onSend() {
  const text = draft.value.trim()
  if (!text || store.isGenerating) return
  draft.value = ''
  nextTick(() => {
    if (inputRef.value) inputRef.value.style.height = 'auto'
  })
  store.send(text)
}

function sendQuick(text) {
  if (store.isGenerating) return
  store.send(text)
}

async function onApprove(editId) {
  editBusy.value = editId
  try {
    await store.approveEdit(editId)
  } catch (e) {
    console.error('应用修改失败:', e)
    alert(e?.response?.data?.detail || e?.message || '应用修改失败')
  } finally {
    editBusy.value = ''
  }
}

async function onReject(editId) {
  editBusy.value = editId
  try {
    await store.rejectEdit(editId)
  } catch (e) {
    console.error('驳回失败:', e)
    alert(e?.response?.data?.detail || e?.message || '驳回失败')
  } finally {
    editBusy.value = ''
  }
}

function autoGrow(e) {
  const el = e.target
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 120) + 'px'
}

function scrollToBottom() {
  nextTick(() => {
    const el = bodyRef.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

// 路由变化：同步路由上下文；离开词条页清除页面上下文
watch(
  () => route.name,
  (name) => {
    store.setContext({ route_name: name || '', route_label: ROUTE_LABELS[name] || '' })
    if (name !== 'Wiki') store.clearPageContext()
  },
  { immediate: true },
)

// 新消息 / 流式内容增长时自动滚动到底部
watch(() => store.messages.length, scrollToBottom)
watch(
  () => {
    const last = store.messages[store.messages.length - 1]
    return last ? last.content.length + (last.tools ? last.tools.length : 0) : 0
  },
  () => { if (store.isOpen) scrollToBottom() },
)

// 首次打开面板时聚焦输入框
watch(() => store.isOpen, (open) => {
  if (open) nextTick(() => inputRef.value && inputRef.value.focus())
})

onMounted(() => {
  store.setContext({ route_name: route.name || '', route_label: ROUTE_LABELS[route.name] || '' })
})
</script>

<style scoped>
.assistant-root {
  position: fixed;
  top: 0;
  right: 0;
  height: 100vh;
  /* 高于页面级模态框(100)，使查看词条详情时助手仍可用；低于图片灯箱/Toast(1000+) */
  z-index: 110;
  pointer-events: none;
}

.assistant-root > * {
  pointer-events: auto;
}

/* ---- 折叠态悬浮入口 ---- */
.assistant-fab {
  position: absolute;
  right: 0;
  top: 50%;
  transform: translateY(-50%);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 14px 8px;
  border: 1px solid #e8dfd2;
  border-right: none;
  border-radius: 12px 0 0 12px;
  background: linear-gradient(160deg, #fff, #faf3e8);
  color: #b5793c;
  cursor: pointer;
  box-shadow: -2px 2px 12px rgba(120, 90, 50, 0.12);
  transition: all 0.2s;
}

.assistant-fab:hover {
  background: linear-gradient(160deg, #fff, #f6e7d2);
  box-shadow: -3px 3px 16px rgba(120, 90, 50, 0.2);
  padding-right: 12px;
}

.fab-icon {
  font-size: 18px;
  line-height: 1;
}

.fab-text {
  writing-mode: vertical-rl;
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 3px;
}

/* ---- 展开面板 ---- */
.assistant-panel {
  position: absolute;
  right: 0;
  top: 0;
  width: 384px;
  max-width: 92vw;
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: #fdfbf7;
  border-left: 1px solid #ede6dc;
  box-shadow: -6px 0 28px rgba(80, 60, 35, 0.14);
}

.assistant-slide-enter-active,
.assistant-slide-leave-active {
  transition: transform 0.28s cubic-bezier(0.4, 0, 0.2, 1), opacity 0.28s;
}

.assistant-slide-enter-from,
.assistant-slide-leave-to {
  transform: translateX(100%);
  opacity: 0.4;
}

.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px;
  border-bottom: 1px solid #ede6dc;
  background: linear-gradient(135deg, #faf3e8, #fdfbf7);
  flex-shrink: 0;
}

.head-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 15px;
  font-weight: 700;
  color: #4a3f35;
}

.head-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #c98a4b;
  box-shadow: 0 0 0 3px rgba(201, 138, 75, 0.18);
}

.head-sub {
  font-size: 10.5px;
  font-weight: 600;
  color: #b5793c;
  background: #f6e7d2;
  border-radius: 999px;
  padding: 1px 8px;
  letter-spacing: 0.5px;
}

.head-actions {
  display: flex;
  gap: 4px;
}

.icon-btn {
  width: 28px;
  height: 28px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: #8a7e72;
  font-size: 15px;
  cursor: pointer;
  transition: all 0.15s;
}

.icon-btn:hover:not(:disabled) {
  background: #f0e9df;
  color: #c98a4b;
}

.icon-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.panel-context {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 7px 14px;
  font-size: 12px;
  color: #6b5d4f;
  background: #faf6f0;
  border-bottom: 1px solid #f0e9df;
  flex-shrink: 0;
}

.ctx-tag {
  font-size: 10.5px;
  color: #b5793c;
  background: #f6e7d2;
  border-radius: 999px;
  padding: 1px 8px;
  white-space: nowrap;
}

/* ---- 消息区 ---- */
.panel-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px 14px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.welcome {
  margin: auto 4px;
  text-align: center;
}

.welcome-title {
  font-size: 15px;
  font-weight: 700;
  color: #4a3f35;
  margin-bottom: 8px;
}

.welcome-desc {
  font-size: 12.5px;
  color: #8a7e72;
  line-height: 1.7;
  margin-bottom: 16px;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: center;
}

.chip {
  border: 1px solid #e8dfd2;
  background: #fff;
  color: #8d6a3f;
  font-size: 12px;
  padding: 6px 12px;
  border-radius: 999px;
  cursor: pointer;
  transition: all 0.15s;
}

.chip:hover:not(:disabled) {
  border-color: #c98a4b;
  color: #c98a4b;
  background: #faf3e8;
}

.chip:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.msg {
  display: flex;
  gap: 8px;
  align-items: flex-start;
}

.msg.user {
  flex-direction: row-reverse;
}

.msg-avatar {
  width: 26px;
  height: 26px;
  flex-shrink: 0;
  border-radius: 8px;
  background: linear-gradient(135deg, #e8c79e, #c98a4b);
  color: #fff;
  font-size: 13px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.msg-bubble {
  max-width: calc(100% - 40px);
  background: #fff;
  border: 1px solid #ede6dc;
  border-radius: 12px;
  padding: 9px 12px;
  font-size: 13px;
  line-height: 1.65;
  color: #3d362e;
}

.msg.user .msg-bubble {
  background: #f6e7d2;
  border-color: #eed9bb;
  color: #4a3f35;
}

.msg-thought {
  font-size: 11.5px;
  color: #a89a89;
  font-style: italic;
  margin-bottom: 6px;
  padding-bottom: 6px;
  border-bottom: 1px dashed #ede6dc;
}

.msg-tools {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 8px;
}

.tool-line {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  color: #8d6a3f;
  background: #faf6f0;
  border-radius: 8px;
  padding: 4px 8px;
}

.tool-name {
  font-weight: 600;
  white-space: nowrap;
}

.tool-summary {
  color: #a89a89;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tool-check {
  color: #6f9a6f;
  font-size: 11px;
}

.tool-spinner {
  width: 11px;
  height: 11px;
  border: 2px solid #e8dfd2;
  border-top-color: #c98a4b;
  border-radius: 50%;
  animation: assistant-spin 0.7s linear infinite;
  flex-shrink: 0;
}

@keyframes assistant-spin {
  to { transform: rotate(360deg); }
}

.msg-content {
  word-break: break-word;
}

.msg-content.error {
  color: #b05555;
}

.msg-content :deep(p) {
  margin: 0 0 8px;
}

.msg-content :deep(p:last-child) {
  margin-bottom: 0;
}

.msg-content :deep(ul),
.msg-content :deep(ol) {
  margin: 4px 0 8px;
  padding-left: 20px;
}

.msg-content :deep(li) {
  margin-bottom: 3px;
}

.msg-content :deep(h1),
.msg-content :deep(h2),
.msg-content :deep(h3) {
  font-size: 13.5px;
  margin: 10px 0 6px;
  color: #4a3f35;
}

.msg-content :deep(code) {
  background: #f6f1e8;
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 12px;
}

.msg-content :deep(pre) {
  background: #f6f1e8;
  padding: 8px 10px;
  border-radius: 8px;
  overflow-x: auto;
  margin: 6px 0;
}

.msg-content :deep(blockquote) {
  border-left: 3px solid #e0c9a6;
  padding-left: 10px;
  color: #8a7e72;
  margin: 6px 0;
}

.msg-content :deep(a) {
  color: #c98a4b;
}

/* 打字动画 */
.typing {
  display: flex;
  gap: 4px;
  padding: 4px 0;
}

.typing span {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #c98a4b;
  opacity: 0.4;
  animation: assistant-blink 1.2s infinite;
}

.typing span:nth-child(2) { animation-delay: 0.2s; }
.typing span:nth-child(3) { animation-delay: 0.4s; }

@keyframes assistant-blink {
  0%, 60%, 100% { opacity: 0.3; transform: translateY(0); }
  30% { opacity: 1; transform: translateY(-3px); }
}

/* ---- 编辑确认卡片 ---- */
.edit-card {
  margin-top: 10px;
  border: 1px solid #eed9bb;
  border-radius: 10px;
  background: #fffdf9;
  overflow: hidden;
}

.edit-card.approved {
  border-color: #cfe3cf;
  background: #f7fbf7;
}

.edit-card.rejected {
  border-color: #e8d8d8;
  background: #fbf7f7;
  opacity: 0.85;
}

.edit-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  background: #faf3e8;
  border-bottom: 1px solid #f0e4d2;
}

.edit-badge {
  font-size: 10.5px;
  font-weight: 600;
  color: #fff;
  background: #c98a4b;
  border-radius: 999px;
  padding: 2px 8px;
  white-space: nowrap;
}

.edit-title {
  font-size: 12.5px;
  font-weight: 600;
  color: #4a3f35;
}

.edit-reason {
  font-size: 11.5px;
  color: #8a7e72;
  padding: 7px 10px 0;
  margin: 0;
}

.edit-diff {
  display: flex;
  gap: 1px;
  padding: 8px 10px;
}

.diff-col {
  flex: 1;
  min-width: 0;
}

.diff-label {
  font-size: 10.5px;
  font-weight: 600;
  margin-bottom: 4px;
  text-align: center;
  border-radius: 4px;
  padding: 1px 0;
}

.diff-label.old {
  color: #a05555;
  background: #f7ecec;
}

.diff-label.new {
  color: #4f7a4f;
  background: #eef5ee;
}

.diff-pre {
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  font-size: 10.5px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 160px;
  overflow-y: auto;
  margin: 0;
  padding: 6px 8px;
  border-radius: 6px;
}

.diff-pre.old {
  background: #fbf4f4;
  border: 1px solid #f0e0e0;
  color: #7a5c5c;
}

.diff-pre.new {
  background: #f4faf4;
  border: 1px solid #e0f0e0;
  color: #4f6b4f;
}

.edit-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 8px 10px;
  border-top: 1px solid #f0e9df;
}

.btn-reject,
.btn-approve {
  font-size: 12px;
  padding: 6px 14px;
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.15s;
}

.btn-reject {
  border: 1px solid #e0d8cc;
  background: #fff;
  color: #6b5d4f;
}

.btn-reject:hover:not(:disabled) {
  border-color: #b05555;
  color: #b05555;
}

.btn-approve {
  border: none;
  background: #c98a4b;
  color: #fff;
  font-weight: 600;
}

.btn-approve:hover:not(:disabled) {
  background: #b5793c;
}

.btn-reject:disabled,
.btn-approve:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.edit-done {
  padding: 8px 10px;
  font-size: 12px;
  font-weight: 600;
  color: #6f9a6f;
  border-top: 1px solid #f0e9df;
}

.edit-card.rejected .edit-done {
  color: #a08080;
}

/* ---- 输入区 ---- */
.panel-input {
  flex-shrink: 0;
  border-top: 1px solid #ede6dc;
  background: #faf6f0;
  padding: 8px 12px 12px;
}

.chips-inline {
  justify-content: flex-start;
  gap: 6px;
  margin-bottom: 8px;
  max-height: 60px;
  overflow-y: auto;
}

.chip-sm {
  font-size: 11px;
  padding: 4px 10px;
}

.input-row {
  display: flex;
  gap: 8px;
  align-items: flex-end;
}

.input-box {
  flex: 1;
  resize: none;
  border: 1px solid #e0d8cc;
  border-radius: 10px;
  background: #fff;
  padding: 9px 12px;
  font-size: 13px;
  font-family: inherit;
  color: #3d362e;
  line-height: 1.5;
  outline: none;
  max-height: 120px;
  transition: border-color 0.15s;
}

.input-box:focus {
  border-color: #c98a4b;
}

.input-box:disabled {
  background: #f6f1e8;
  cursor: not-allowed;
}

.send-btn {
  width: 38px;
  height: 38px;
  flex-shrink: 0;
  border: none;
  border-radius: 10px;
  background: #c98a4b;
  color: #fff;
  font-size: 15px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.15s;
}

.send-btn:hover:not(:disabled) {
  background: #b5793c;
}

.send-btn:disabled {
  background: #e0d0b8;
  cursor: not-allowed;
}

.send-spinner {
  width: 15px;
  height: 15px;
  border: 2px solid rgba(255, 255, 255, 0.4);
  border-top-color: #fff;
  border-radius: 50%;
  animation: assistant-spin 0.7s linear infinite;
}

.status-line {
  font-size: 11px;
  color: #a89a89;
  margin-top: 6px;
  text-align: center;
}

/* ---- 移动端：全屏面板 ---- */
@media (max-width: 768px) {
  .assistant-panel {
    width: 100vw;
    max-width: 100vw;
  }
}
</style>
