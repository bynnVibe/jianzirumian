<template>
  <div v-if="visible" class="assistant-root">
    <!-- 折叠态：右侧悬浮入口（生成中显示“进行中”徽标，切回即见实时流） -->
    <button v-if="!store.isOpen" class="assistant-fab" @click="store.open()" title="知识助手">
      <span class="fab-icon">✦</span>
      <span class="fab-text">知识助手</span>
      <span v-if="store.isGenerating" class="fab-badge">进行中</span>
    </button>

    <!-- 展开态：问答面板 -->
    <transition name="assistant-slide">
      <section v-if="store.isOpen" class="assistant-panel" aria-label="知识助手">
        <header class="panel-head">
          <div class="head-title">
            <span class="head-dot"></span>
            <span>知识助手</span>
            <span class="head-sub">Agent</span>
            <span v-if="store.isGenerating" class="head-live">● 进行中</span>
          </div>
          <div class="head-actions">
            <button class="icon-btn" @click="store.toggleHistoryDrawer()" title="历史记录">🕘</button>
            <button class="icon-btn" @click="store.newSession()" title="新建对话" :disabled="store.isGenerating">⟳</button>
            <button class="icon-btn" @click="store.toggle()" title="收起">✕</button>
          </div>
        </header>

        <!-- 历史记录抽屉 -->
        <div v-if="store.historyDrawerOpen" class="history-drawer">
          <div class="history-head">
            <span class="history-title">历史记录</span>
            <button class="icon-btn" @click="store.historyDrawerOpen = false" title="关闭">✕</button>
          </div>
          <div class="history-actions">
            <button class="hist-new" @click="store.newSession()" :disabled="store.isGenerating">＋ 新建对话</button>
          </div>
          <div class="history-list">
            <p v-if="store.historyLoading" class="history-empty">加载中…</p>
            <p v-else-if="!store.sessions.length" class="history-empty">暂无历史会话</p>
            <div
              v-for="s in store.sessions"
              :key="s.id"
              class="history-item"
              :class="{ active: s.id === store.viewingSessionId || (!store.isViewingHistory && s.id === store.currentSessionId) }"
              @click="store.openSession(s.id)"
            >
              <div class="hist-main">
                <span class="hist-item-title">{{ s.title || '未命名会话' }}</span>
                <span class="hist-time">{{ formatTime(s.updated_at) }}</span>
              </div>
              <button
                class="hist-del"
                title="删除会话"
                @click.stop="onDeleteSession(s.id)"
              >🗑</button>
            </div>
          </div>
        </div>

        <!-- 只读历史回放横幅 -->
        <div v-if="store.isViewingHistory" class="history-banner">
          <span class="hb-text">📖 正在查看历史会话（只读）</span>
          <button class="hb-back" @click="store.backToLive()">返回当前对话</button>
        </div>

        <!-- 当前上下文提示 -->
        <div class="panel-context" v-if="!store.isViewingHistory && store.context.open_doc_title">
          <span class="ctx-tag">正在阅读</span>
          《{{ store.context.open_doc_title }}》
        </div>
        <div class="panel-context" v-else-if="!store.isViewingHistory && store.context.page_title">
          <span class="ctx-tag">当前词条</span>
          《{{ store.context.page_title }}》
        </div>
        <div class="panel-context" v-else-if="!store.isViewingHistory && store.context.route_label">
          <span class="ctx-tag">当前页面</span>
          {{ store.context.route_label }}
        </div>

        <!-- 消息区 -->
        <div class="panel-body" ref="bodyRef">
          <!-- 欢迎态 -->
          <div v-if="displayMessages.length === 0" class="welcome">
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
          <div v-for="(m, i) in displayMessages" :key="i" class="msg" :class="m.role">
            <div v-if="m.role === 'assistant'" class="msg-avatar">✦</div>
            <div class="msg-bubble">
              <!-- 思考过程（累积展示 Agent 每一步推理） -->
              <div v-if="m.thoughts && m.thoughts.length" class="msg-thoughts">
                <div class="thoughts-head" @click="m._collapse = !m._collapse">
                  <span class="thoughts-icon">💭</span>
                  <span>思考过程（{{ m.thoughts.length }} 步）</span>
                  <span class="thoughts-toggle">{{ m._collapse ? '展开' : '收起' }}</span>
                </div>
                <ol v-show="!m._collapse" class="thoughts-list">
                  <li v-for="(t, ti) in m.thoughts" :key="ti" class="thought-item">{{ t }}</li>
                </ol>
              </div>

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
              <div v-if="m.stopped && m.content" class="msg-stopped">⏹ 已停止生成</div>

              <!-- 追问卡片：知识缺口/意图不明时助手请求用户选择下一步动作 -->
              <div v-if="m.clarify" class="clarify-card">
                <div class="clarify-head">
                  <span class="clarify-badge">需要你的确认</span>
                </div>
                <p class="clarify-question">{{ m.clarify.question }}</p>
                <div class="clarify-options" v-if="m.clarify.options.length">
                  <button
                    v-for="(opt, oi) in m.clarify.options"
                    :key="oi"
                    class="chip chip-sm"
                    :disabled="store.isGenerating || store.isViewingHistory"
                    @click="sendQuick(opt)"
                  >{{ opt }}</button>
                </div>
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
          <!-- 只读历史回放：隐藏输入，提示返回当前对话 -->
          <div v-if="store.isViewingHistory" class="readonly-hint">
            历史会话为只读回放。点击右上“返回当前对话”或“新建对话”继续提问。
          </div>
          <template v-else>
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
              <div class="mention-wrap">
                <textarea
                  v-model="draft"
                  ref="inputRef"
                  class="input-box"
                  rows="1"
                  placeholder="向知识助手提问… 输入 @ 可引用当前界面文档/片段"
                  :disabled="store.isGenerating"
                  @keydown="onInputKeydown"
                  @input="autoGrow"
                ></textarea>
                <!-- @ 引用候选：当前界面正在展示的文档/片段标题 -->
                <div v-if="mentionOpen" class="mention-pop" role="listbox">
                  <div class="mention-head">@ 引用当前界面的文档 / 片段</div>
                  <p v-if="!mentionCandidates.length" class="mention-empty">没有匹配的条目</p>
                  <button
                    v-for="(opt, oi) in mentionCandidates"
                    :key="opt.title"
                    type="button"
                    class="mention-item"
                    :class="{ active: oi === mentionIndex }"
                    @mousedown.prevent="selectMention(opt)"
                  >
                    <span class="mention-title">{{ opt.title }}</span>
                    <span class="mention-sub">{{ opt.sub }}</span>
                  </button>
                </div>
              </div>
              <!-- 生成中：停止按钮；否则：发送按钮 -->
              <button
                v-if="store.isGenerating"
                class="send-btn stop"
                @click="store.stop()"
                title="停止生成"
              >
                <span class="stop-square"></span>
              </button>
              <button
                v-else
                class="send-btn"
                :disabled="!draft.trim()"
                @click="onSend"
                title="发送"
              >➤</button>
            </div>
            <div class="status-line" v-if="store.statusText">{{ store.statusText }}</div>
          </template>
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

// 知识助手仅在「知识库界面」浮现：知识类页面白名单（/kb 总览/详情/上传 + 入库详情）。
// 其余页面（含 Skill 配置、对话、登录、管理控制台、系统管理等）一律不浮出助手。
const ASSISTANT_ROUTES = ['KnowledgeOverview', 'KnowledgeDetail', 'KnowledgeUpload', 'IngestDetail']
const ROUTE_LABELS = {
  KnowledgeOverview: '知识库',
  KnowledgeDetail: '知识库详情',
  Management: '系统管理',
  KnowledgeUpload: '知识库上传',
  IngestDetail: '入库详情',
  UserManagement: '用户管理',
  Eval: '回归评测',
}

const visible = computed(() => auth.isAuthenticated && ASSISTANT_ROUTES.includes(route.name))

const quickChips = computed(() => {
  if (store.context.open_doc_title) {
    return ['分析这篇文档的主要内容', '总结这篇文档的关键要点', '这篇文档有哪些值得注意的地方？', '结合知识库解释这篇文档']
  }
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
  web_search: '联网搜索',
  propose_page_edit: '生成修改建议',
}

function toolLabel(name) {
  return TOOL_LABELS[name] || name || '工具'
}

// 当前展示的消息：回看只读历史时用 historyMessages，否则用活动 messages（保留在途流）
const displayMessages = computed(() =>
  store.isViewingHistory ? store.historyMessages : store.messages,
)

// 会话时间格式化（仅月-日 时:分）
function formatTime(iso) {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return ''
    const p = (n) => String(n).padStart(2, '0')
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
  } catch (e) {
    return ''
  }
}

async function onDeleteSession(id) {
  if (!window.confirm('确定删除该历史会话？此操作不可恢复。')) return
  try {
    await store.removeSession(id)
  } catch (e) {
    console.error('删除会话失败:', e)
    alert(e?.response?.data?.detail || e?.message || '删除会话失败')
  }
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
  const mentions = extractMentions(text)
  draft.value = ''
  nextTick(() => {
    if (inputRef.value) inputRef.value.style.height = 'auto'
  })
  store.send(text, mentions)
}

// ---- @ 引用当前界面文档/片段 ----
// 候选来自视图写入的界面实时快照（page_state）：知识库管理页的 documents、
// 收藏/百科页的 items，即用户此刻屏幕上正在展示的文档/片段标题。
const mentionIndex = ref(0)
const mentionSuppressed = ref(false) // Esc 关闭后，直到 @ 查询串变化才重新弹出

const mentionMatch = computed(() => {
  const m = draft.value.match(/@([^@\n《》]*)$/)
  return m ? m[1] : null
})
watch(mentionMatch, () => {
  mentionSuppressed.value = false
  mentionIndex.value = 0
})

const mentionOptions = computed(() => {
  const ps = store.context.page_state
  if (!ps) return []
  const seen = new Set()
  const out = []
  for (const d of (ps.documents || [])) {
    const t = ((d && d.title) || '').trim()
    if (!t || seen.has(t)) continue
    seen.add(t)
    out.push({ title: t, sub: d.kb_name ? `文档 · ${d.kb_name}` : '文档' })
  }
  for (const it of (ps.items || [])) {
    const t = ((it && it.title) || '').trim()
    if (!t || seen.has(t)) continue
    seen.add(t)
    out.push({ title: t, sub: it.type ? `片段 · ${it.type}` : '片段' })
  }
  return out
})

const mentionCandidates = computed(() => {
  const q = (mentionMatch.value || '').trim().toLowerCase()
  const list = mentionOptions.value
  const filtered = q ? list.filter((o) => o.title.toLowerCase().includes(q)) : list
  return filtered.slice(0, 8)
})

const mentionOpen = computed(() =>
  mentionMatch.value !== null && !mentionSuppressed.value && !store.isGenerating && mentionOptions.value.length > 0,
)

function selectMention(opt) {
  if (!opt) return
  // 用 @《标题》 替换尾部的 @查询串，作为可读的引用标记
  draft.value = draft.value.replace(/@([^@\n《》]*)$/, `@《${opt.title}》 `)
  mentionSuppressed.value = true
  nextTick(() => inputRef.value && inputRef.value.focus())
}

// 从发送文本中提取 @《标题》 引用（去重保序），随请求传给后端做联合分析
function extractMentions(text) {
  const titles = []
  const re = /@《([^》]+)》/g
  let m
  while ((m = re.exec(text))) {
    const t = m[1].trim()
    if (t && !titles.includes(t)) titles.push(t)
  }
  return titles.map((t) => ({ title: t }))
}

function onInputKeydown(e) {
  if (mentionOpen.value && mentionCandidates.value.length) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      mentionIndex.value = (mentionIndex.value + 1) % mentionCandidates.value.length
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      mentionIndex.value = (mentionIndex.value - 1 + mentionCandidates.value.length) % mentionCandidates.value.length
      return
    }
    if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault()
      selectMention(mentionCandidates.value[mentionIndex.value])
      return
    }
    if (e.key === 'Escape') {
      e.preventDefault()
      mentionSuppressed.value = true
      return
    }
  }
  // 非引用选择态：Enter（不含 Shift 等修饰键）发送
  if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey) {
    e.preventDefault()
    onSend()
  }
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
  (name, oldName) => {
    store.setContext({ route_name: name || '', route_label: ROUTE_LABELS[name] || '' })
    if (name !== 'KnowledgeDetail') store.clearPageContext()
    // 离开知识库详情页时清除界面实时快照（视图未及清理时的兜底）
    if (name !== 'KnowledgeDetail') store.clearPageState()
    // 切换页面时清除文档预览上下文，并记录一条行文动作
    if (name !== oldName) {
      if (store.context.open_doc_title) store.clearOpenDoc()
      if (name) store.pushAction(`进入${ROUTE_LABELS[name] || name}页面`)
    }
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
// 打开只读历史会话时滚动到底部
watch(() => store.historyMessages.length, () => { if (store.isViewingHistory) scrollToBottom() })

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

/* ---- 思考过程（可折叠，累积多步） ---- */
.msg-thoughts {
  margin-bottom: 8px;
  border: 1px dashed #e6dccb;
  border-radius: 8px;
  background: #fcf9f3;
  overflow: hidden;
}

.thoughts-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 9px;
  font-size: 11.5px;
  color: #a08a6d;
  cursor: pointer;
  user-select: none;
}

.thoughts-head:hover {
  background: #f7f0e4;
}

.thoughts-icon {
  font-size: 12px;
}

.thoughts-toggle {
  margin-left: auto;
  font-size: 10.5px;
  color: #c0a882;
}

.thoughts-list {
  margin: 0;
  padding: 4px 12px 8px 26px;
  border-top: 1px dashed #eee4d4;
}

.thought-item {
  font-size: 11.5px;
  line-height: 1.6;
  color: #8a7e72;
  font-style: italic;
  margin: 3px 0;
}

.msg-stopped {
  margin-top: 6px;
  font-size: 11px;
  color: #b08a5a;
  background: #faf3e6;
  border-radius: 6px;
  padding: 3px 8px;
  display: inline-block;
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

/* ---- 历史抽屉 / 徒标 / 只读横幅 ---- */
.fab-badge {
  margin-top: 4px;
  font-size: 9.5px;
  font-weight: 700;
  color: #fff;
  background: #c0563f;
  border-radius: 999px;
  padding: 1px 5px;
  letter-spacing: 0.5px;
  animation: assistant-pulse 1.4s ease-in-out infinite;
}

@keyframes assistant-pulse {
  0%, 100% { opacity: 0.7; }
  50% { opacity: 1; }
}

.head-live {
  font-size: 10px;
  font-weight: 700;
  color: #c0563f;
  letter-spacing: 0.3px;
}

.history-drawer {
  position: absolute;
  top: 0;
  right: 0;
  width: 100%;
  height: 100%;
  background: #fdfbf7;
  z-index: 5;
  display: flex;
  flex-direction: column;
}

.history-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px;
  border-bottom: 1px solid #ede6dc;
  background: linear-gradient(135deg, #faf3e8, #fdfbf7);
  flex-shrink: 0;
}

.history-title {
  font-size: 14px;
  font-weight: 700;
  color: #4a3f35;
}

.history-actions {
  padding: 10px 14px;
  flex-shrink: 0;
}

.hist-new {
  width: 100%;
  border: 1px solid #e0c9a6;
  background: #fff;
  color: #b5793c;
  font-size: 12.5px;
  font-weight: 600;
  padding: 8px 12px;
  border-radius: 10px;
  cursor: pointer;
  transition: all 0.15s;
}

.hist-new:hover:not(:disabled) {
  background: #faf3e8;
  border-color: #c98a4b;
}

.hist-new:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.history-list {
  flex: 1;
  overflow-y: auto;
  padding: 0 10px 14px;
}

.history-empty {
  text-align: center;
  color: #a89a89;
  font-size: 12.5px;
  padding: 24px 0;
}

.history-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 10px;
  border-radius: 10px;
  cursor: pointer;
  transition: background 0.15s;
  border: 1px solid transparent;
}

.history-item:hover {
  background: #f6f1e8;
}

.history-item.active {
  background: #faf3e8;
  border-color: #eed9bb;
}

.hist-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.hist-item-title {
  font-size: 12.5px;
  color: #4a3f35;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.hist-time {
  font-size: 10.5px;
  color: #b3a695;
}

.hist-del {
  flex-shrink: 0;
  width: 26px;
  height: 26px;
  border: none;
  border-radius: 7px;
  background: transparent;
  color: #b3a695;
  font-size: 13px;
  cursor: pointer;
  opacity: 0;
  transition: all 0.15s;
}

.history-item:hover .hist-del {
  opacity: 1;
}

.hist-del:hover {
  background: #f7ecec;
  color: #b05555;
}

.history-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 7px 14px;
  font-size: 11.5px;
  color: #8d6a3f;
  background: #f6e7d2;
  border-bottom: 1px solid #eed9bb;
  flex-shrink: 0;
}

.hb-text {
  font-weight: 600;
}

.hb-back {
  border: 1px solid #d8b98c;
  background: #fff;
  color: #b5793c;
  font-size: 11px;
  font-weight: 600;
  padding: 3px 10px;
  border-radius: 999px;
  cursor: pointer;
  white-space: nowrap;
  transition: all 0.15s;
}

.hb-back:hover {
  background: #faf3e8;
  border-color: #c98a4b;
}

.readonly-hint {
  font-size: 11.5px;
  color: #a89a89;
  text-align: center;
  line-height: 1.6;
  padding: 6px 4px;
}

/* ---- 追问卡片 ---- */
.clarify-card {
  margin-top: 8px;
  border: 1px solid #eed9bb;
  border-radius: 10px;
  background: #fffdf9;
  padding: 8px 10px;
}

.clarify-head {
  margin-bottom: 5px;
}

.clarify-badge {
  font-size: 10.5px;
  font-weight: 600;
  color: #fff;
  background: #c98a4b;
  border-radius: 999px;
  padding: 2px 8px;
}

.clarify-question {
  font-size: 12.5px;
  color: #4a3f35;
  margin: 0 0 8px;
  line-height: 1.6;
}

.clarify-options {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
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
  width: 100%;
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

/* ---- @ 引用当前界面文档/片段 ---- */
.mention-wrap {
  position: relative;
  flex: 1;
  min-width: 0;
}

.mention-pop {
  position: absolute;
  bottom: calc(100% + 6px);
  left: 0;
  right: 0;
  max-height: 240px;
  overflow-y: auto;
  background: #fff;
  border: 1px solid #e8dfd2;
  border-radius: 10px;
  box-shadow: 0 6px 22px rgba(80, 60, 35, 0.16);
  padding: 4px;
  z-index: 20;
}

.mention-head {
  font-size: 10.5px;
  font-weight: 600;
  color: #b5793c;
  padding: 5px 8px 6px;
  border-bottom: 1px dashed #eee4d4;
  margin-bottom: 3px;
}

.mention-empty {
  font-size: 12px;
  color: #a89a89;
  text-align: center;
  padding: 10px 4px;
  margin: 0;
}

.mention-item {
  display: flex;
  flex-direction: column;
  gap: 1px;
  width: 100%;
  border: none;
  background: transparent;
  text-align: left;
  padding: 6px 8px;
  border-radius: 7px;
  cursor: pointer;
  transition: background 0.12s;
}

.mention-item:hover,
.mention-item.active {
  background: #faf3e8;
}

.mention-title {
  font-size: 12.5px;
  color: #4a3f35;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mention-sub {
  font-size: 10.5px;
  color: #b3a695;
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

.send-btn.stop {
  background: #b05555;
}

.send-btn.stop:hover {
  background: #984747;
}

.stop-square {
  width: 13px;
  height: 13px;
  border-radius: 3px;
  background: #fff;
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
