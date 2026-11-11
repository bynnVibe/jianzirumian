<template>
  <div class="chat-view">
    <!-- 空状态 - 没有消息时 -->
    <div v-if="messages.length === 0 && !store.isGenerating" class="welcome-screen">
      <div class="welcome-content">
        <div class="welcome-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.2" width="48" height="48">
            <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2z"/>
            <path d="M12 6v6l4 2" stroke-linecap="round"/>
            <path d="M8 12c0 2.21 1.79 4 4 4" stroke-linecap="round"/>
          </svg>
        </div>
        <h1 class="welcome-title">见字如面</h1>
        <p class="welcome-subtitle">将手写笔记转化为智慧，用知识滋养身心</p>
        <div class="suggestion-cards">
          <div class="suggestion-card" @click="askSuggestion('中医有哪些调理脾胃的方法？')">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
              <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
              <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
            </svg>
            中医调理脾胃
          </div>
          <div class="suggestion-card" @click="askSuggestion('手写笔记中的养生方法有哪些？')">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
              <path d="M12 20h9"/>
              <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>
            </svg>
            养生方法汇总
          </div>
          <div class="suggestion-card" @click="askSuggestion('丰隆穴的功效与按摩方法？')">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
              <circle cx="12" cy="12" r="10"/>
              <path d="M12 6v6l4 2"/>
            </svg>
            穴位按摩指南
          </div>
          <div class="suggestion-card" @click="askSuggestion('高血压患者的日常注意事项？')">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
              <path d="M22 12h-4l-3 9L9 3l-3 9H2"/>
            </svg>
            健康管理
          </div>
        </div>
      </div>
    </div>

    <!-- 消息列表 -->
    <div v-else ref="messagesRef" class="messages-container">
      <div class="messages-list">
        <!-- 历史消息 -->
        <div v-for="(msg, idx) in messages" :key="messageAnchor(store.currentSessionId, msg, idx)"
                  :id="messageAnchor(store.currentSessionId, msg, idx)" class="message-anchor"
                  :class="{ 'located-turn': active === messageAnchor(store.currentSessionId, msg, idx) && !following }">
          <ChatMessage
            v-if="!msg.hitl"
            :role="msg.role"
            :content="msg.content"
            :sources="msg.sources"
            :search-results="msg.search_results"
            :attachments="msg.attachments"
            :message-id="msg.message_id"
            :feedback="msg.feedback"
            :can-feedback="!auth.isGuest && !msg.interrupted"
            :cache-hit="!!msg.cache_hit"
            :cache-score="msg.cache_score || 0"
            @reask="handleReask"
            @feedback="(fb) => handleFeedback(msg, fb)"
          />
          <!-- HITL 卡片：联网确认 / 意图澄清（后端暂停本轮，等待用户响应后触发新请求） -->
          <ChatHitlCard
            v-else
            :hitl="msg.hitl"
            :busy="store.isGenerating"
            @search="handleHitlSearch"
            @direct="handleHitlDirect"
            @option="handleHitlOption"
          />
          <ChatActionCard v-for="action in msg.actions || []" :key="action.id" :action="action" @update="store.updateAction" />
          <!-- 能力缺口卡片（需求5）：系统无合适工具时给出 CLI 候选方案，用户选择后沙箱执行 -->
          <CapabilityGapCard v-if="msg.capability_gap" :gap="msg.capability_gap" />
        </div>
        <!-- 正在生成的流式响应 -->
        <ChatMessage
          v-if="store.isGenerating && currentResponse"
          role="assistant"
          :content="currentResponse"
          :sources="store.sources"
          :search-results="store.searchResults"
        />
        <!-- 流水线进度指示器：逐步展示处理阶段，替代干等 -->
        <div v-if="store.isGenerating && pipelineSteps.length" class="pipeline-indicator">
          <div class="pipeline-steps">
            <template v-for="(step, idx) in pipelineSteps" :key="step.key">
              <div class="pipeline-step" :class="step.state">
                <span class="step-dot">
                  <svg v-if="step.state === 'done'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" width="9" height="9">
                    <polyline points="20 6 9 17 4 12"/>
                  </svg>
                  <span v-else-if="step.state === 'active'" class="step-spinner"></span>
                </span>
                <span class="step-label">{{ step.label }}</span>
              </div>
              <span
                v-if="idx < pipelineSteps.length - 1"
                class="step-line"
                :class="{ done: step.state === 'done' }"
              ></span>
            </template>
          </div>
          <div v-if="phaseMessage" class="pipeline-message">
            <div class="status-spinner"></div>
            <span>{{ phaseMessage }}</span>
          </div>
        </div>
        <!-- Agent（复杂度分流）执行过程：思考链 / 解题计划 / 工具调用 -->
        <div v-if="store.isGenerating && hasAgentActivity" class="agent-panel">
          <div v-if="store.agentPlan.length" class="agent-block">
            <div class="agent-block-title">解题计划</div>
            <ol class="agent-plan-list">
              <li v-for="(s, i) in store.agentPlan" :key="i">
                <span class="agent-tag">{{ s.tool }}</span>
                <span class="agent-plan-text">{{ s.purpose || s.query }}</span>
              </li>
            </ol>
          </div>
          <div v-if="store.agentThoughts.length" class="agent-block">
            <div class="agent-block-title">思考</div>
            <p v-for="(t, i) in store.agentThoughts" :key="i" class="agent-thought">{{ t }}</p>
          </div>
          <div v-if="store.agentTools.length" class="agent-block">
            <div class="agent-block-title">工具调用</div>
            <div v-for="(t, i) in store.agentTools" :key="i" class="agent-tool-row" :class="{ failed: t.failed }">
              <span class="agent-tag">{{ t.tool }}</span>
              <span class="agent-tool-query">{{ t.query }}</span>
              <span class="agent-tool-state">
                <span v-if="t.state === 'start'" class="step-spinner"></span>
                <template v-else>{{ t.failed ? '失败' : (t.summary || '完成') }}</template>
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <ConversationNavigator :entries="entries" :active="active" :session-id="store.currentSessionId || ''" @navigate="navigate" />

    <!-- 输入区域 -->
    <div class="input-section">
      <button v-if="messages.length && !following" class="back-to-latest" @click="scrollToBottom">↓ 回到最新</button>
      <!-- 游客提示 -->
      <div v-if="auth.isGuest" class="guest-query-banner">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="16" height="16">
          <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
        </svg>
        <span>游客最多可查询 {{ guestQueryLimit }} 次（含知识库检索和联网搜索），超出需登录</span>
        <router-link to="/login" class="guest-login-link">去登录</router-link>
      </div>
      <button v-if="store.isGenerating" class="stop-generation" @click="store.stopGeneration()">停止生成</button>
      <ChatInput
        ref="chatInputRef"
        :disabled="store.isGenerating"
        @send="handleSend"
      />
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useChatStore } from '@/stores/chat'
import { useAuthStore } from '@/stores/auth'
import { submitFeedback, getPublicConfig } from '@/api'
import ChatMessage from '@/components/ChatMessage.vue'
import ChatInput from '@/components/ChatInput.vue'
import ChatActionCard from '@/components/ChatActionCard.vue'
import ChatHitlCard from '@/components/ChatHitlCard.vue'
import CapabilityGapCard from '@/components/CapabilityGapCard.vue'
import ConversationNavigator from '@/components/ConversationNavigator.vue'
import { messageAnchor } from '@/utils/conversationNavigation'
import { useConversationNavigation } from '@/utils/useConversationNavigation'

const route = useRoute()
const router = useRouter()
const store = useChatStore()
const auth = useAuthStore()
// 切走页面（组件卸载）不中断回答：应答流生命周期在 store（全局）层，
// 用户临时切到系统设置等页面时本轮回答继续执行，回来即可看到完整结果；
// 仅「停止生成」按钮、切换/新建会话才取消。
const messagesRef = ref(null)
const chatInputRef = ref(null)

// 游客免费次数（管理员可配置，动态拉取）
const guestQueryLimit = ref(3)

const messages = computed(() => store.messages)
const currentResponse = computed(() => store.currentResponse)
const { entries, active, following, navigate, scrollToBottom } = useConversationNavigation(
  messagesRef, messages, computed(() => store.currentSessionId), currentResponse,
)

const phaseMessage = computed(() => {
  switch (store.generationPhase) {
    case 'tool_start':
    case 'tool_processing': return '正在处理附件...'
    case 'tool_done': return toolCallsSummary.value || '附件处理完成'
    case 'ocr_retrieval': return '已识别图片文字，正在按图中内容检索知识库...'
    case 'rewriting': return '正在理解你的问题...'
    case 'retrieving': return '正在检索知识库...'
    case 'reranking': return '正在对检索结果重排序...'
    case 'gap_checking': return '正在评估证据是否充分...'
    case 'rechecking': return '证据不足，正在改写查询重新检索...'
    case 'retrieved': return `已检索到相关知识`
    case 'not_found': return '知识库中未找到相关内容'
    case 'searching': return '正在联网搜索...'
    case 'searched': return '联网搜索完成'
    case 'search_empty': return '联网搜索无结果'
    case 'search_error': return '联网搜索出错'
    case 'generating': return '正在生成回答...'
    case 'auto_resolving': return '🔧 正在自动调用命令行能力获取实时数据以解决你的问题，请耐心等待…'
    case 'planning': return '正在规划解题步骤...'
    case 'executing': return '正在按计划执行检索...'
    case 'thinking': return '正在思考...'
    case 'fallback': return '当前模型不可用，已切换免费模型重试...'
    case 'citations_checked': return '已核对引用编号'
    case 'cache_hit': return '命中语义缓存，直接回复历史回答'
    default: return ''
  }
})

// 流水线步骤：根据当前阶段与阶段历史推导每步状态（done/active/pending）
const pipelineSteps = computed(() => {
  const phase = store.generationPhase
  const history = store.phaseHistory || []
  if (!phase && !history.length) return []

  const steps = []
  const hasTool = history.some((p) =>
    ['attachments_saved', 'tool_start', 'tool_processing', 'tool_done', 'ocr_retrieval'].includes(p)
  )
  if (hasTool) {
    steps.push({ key: 'tool', label: '处理附件', phases: ['attachments_saved', 'tool_start', 'tool_processing', 'tool_done', 'ocr_retrieval'] })
  }
  steps.push({ key: 'rewrite', label: '理解问题', phases: ['rewriting'] })
  steps.push({
    key: 'retrieve',
    label: '知识检索',
    phases: ['retrieving', 'reranking', 'gap_checking', 'rechecking', 'retrieved', 'not_found'],
  })
  const hasSearch = store.useSearch || history.some((p) => p.startsWith('search'))
  if (hasSearch) {
    steps.push({ key: 'search', label: '联网搜索', phases: ['searching', 'searched', 'search_empty', 'search_error'] })
  }
  steps.push({ key: 'generate', label: '生成回答', phases: ['generating', 'fallback', 'citations_checked', 'auto_resolving'] })

  const activeIdx = steps.findIndex((s) => s.phases.includes(phase))
  return steps.map((s, i) => ({
    ...s,
    state:
      i < activeIdx
        ? 'done'
        : i === activeIdx
          ? 'active'
          : history.some((p) => s.phases.includes(p))
            ? 'done'
            : 'pending',
  }))
})

const toolCallsSummary = computed(() => {
  const calls = store.toolCalls || []
  if (!calls.length) return ''
  const ok = calls.filter((c) => c.success).length
  return `附件已处理 ${ok}/${calls.length} 个`
})

// Agent 过程面板是否可见（思考链/计划/工具任一非空）
const hasAgentActivity = computed(() =>
  (store.agentThoughts && store.agentThoughts.length) ||
  (store.agentPlan && store.agentPlan.length) ||
  (store.agentTools && store.agentTools.length)
)

// HITL 卡片交互：联网搜索 / 直接回答 / 澄清选项
function handleHitlSearch(query) {
  store.resendWithSearch(query)
}
function handleHitlDirect(query) {
  store.answerWithoutSearch(query)
}
function handleHitlOption(text) {
  store.sendClarifyOption(text)
}

// 路由变化时加载对应会话
watch(() => route.params.sessionId, async (newId) => {
  if (newId && newId !== store.currentSessionId) {
    await store.switchToSession(newId)
  }
}, { immediate: true })

// 新提问恢复跟随；流式结束后不强行覆盖用户的历史定位。
async function handleSend(text, files) {
  scrollToBottom()
  await store.sendMessage(text, files)
}

function askSuggestion(text) {
  handleSend(text)
}

/** 历史问题再编辑：填入输入框聚焦，发送即新一轮提问 */
function handleReask(text) {
  store.startReask(text)
  chatInputRef.value?.focus()
}

/** 点赞/点踩反馈落库，并更新本地消息状态 */
async function handleFeedback(msg, { rating, comment }) {
  if (!msg.message_id || !store.currentSessionId) return
  try {
    await submitFeedback({
      sessionId: store.currentSessionId,
      messageId: msg.message_id,
      rating,
      comment,
    })
    msg.feedback = { rating, comment }
  } catch (err) {
    alert('反馈提交失败: ' + (err.response?.data?.detail || err.message))
  }
}

// ---- 语义缓存面板已收拢至左下角用户菜单（AppSidebar），本页不再展示 ----

onMounted(() => {
  chatInputRef.value?.focus()

  // 游客模式下拉取管理员配置的最新免费次数
  if (auth.isGuest) {
    getPublicConfig()
      .then((res) => {
        if (res.data?.success && Number.isFinite(res.data.guest_query_limit)) {
          guestQueryLimit.value = res.data.guest_query_limit
        }
      })
      .catch(() => {
        // 拉取失败保持默认值
      })
  }
})
</script>

<style scoped>
.stop-generation { display: block; margin: 0 auto 10px; padding: 8px 20px; border: 1px solid #8b7355; border-radius: 8px; background: transparent; color: inherit; cursor: pointer; }
.back-to-latest { display: block; margin: 0 auto 8px; background: #fffdf9; border: 1px solid var(--color-border); border-radius: 18px; padding: 6px 16px; font-size: 12px; color: var(--color-primary); cursor: pointer; }

.message-anchor { border-radius: 10px; }
.located-turn { box-shadow: inset 3px 0 var(--color-primary); }
.chat-view {
  position: relative;
  min-height: 0;
  display: flex;
  flex-direction: column;
  height: 100vh;
}

/* Welcome Screen */
.welcome-screen {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px 24px;
}

.welcome-content {
  text-align: center;
  max-width: 520px;
}

.welcome-icon {
  color: var(--color-primary);
  opacity: 0.8;
  margin-bottom: 16px;
}

.welcome-title {
  font-size: 28px;
  font-weight: 700;
  color: var(--color-text);
  margin-bottom: 8px;
}

.welcome-subtitle {
  font-size: 15px;
  color: var(--color-text-secondary);
  margin-bottom: 32px;
  line-height: 1.6;
}

.suggestion-cards {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.suggestion-card {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 14px 16px;
  background: #fff;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  cursor: pointer;
  transition: all 0.2s;
  font-size: 14px;
  color: var(--color-text);
  text-align: left;
}

.suggestion-card:hover {
  border-color: var(--color-primary);
  background: var(--color-primary-light);
  transform: translateY(-1px);
}

/* Messages */
.messages-container {
  flex: 1;
  overflow-y: auto;
  overflow-anchor: none;
  min-height: 0;
  padding: 0 40px 8px 0;
}

.messages-list {
  max-width: 800px;
  margin: 0 auto;
}

/* Pipeline progress indicator */
.pipeline-indicator {
  padding: 12px 24px;
  max-width: 800px;
  margin: 0 auto;
  width: 100%;
}

.pipeline-steps {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.pipeline-step {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--color-text-secondary);
  transition: color 0.2s;
}

.pipeline-step .step-dot {
  width: 14px;
  height: 14px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1.5px solid var(--color-border);
  background: transparent;
  color: #fff;
  flex-shrink: 0;
}

.pipeline-step.done {
  color: var(--color-text-primary);
}

.pipeline-step.done .step-dot {
  background: var(--color-primary);
  border-color: var(--color-primary);
}

.pipeline-step.active {
  color: var(--color-primary);
  font-weight: 600;
}

.pipeline-step.active .step-dot {
  border-color: var(--color-primary);
}

.pipeline-step.pending {
  opacity: 0.55;
}

.step-spinner {
  width: 8px;
  height: 8px;
  border: 1.5px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

.step-line {
  width: 18px;
  height: 1.5px;
  background: var(--color-border);
  flex-shrink: 0;
}

.step-line.done {
  background: var(--color-primary);
}

.pipeline-message {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
  font-size: 13px;
  color: var(--color-text-secondary);
}

.status-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* Agent（复杂度分流）执行过程面板 */
.agent-panel {
  margin: 4px 0 16px 44px;
  padding: 10px 12px;
  border: 1px dashed #e0cdb2;
  border-radius: 10px;
  background: #fffdf9;
  font-size: 12.5px;
  color: #4a3f35;
}
.agent-block + .agent-block {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid #f0e4d2;
}
.agent-block-title {
  font-size: 11px;
  font-weight: 600;
  color: #c98a4b;
  margin-bottom: 4px;
  letter-spacing: 0.5px;
}
.agent-plan-list {
  margin: 0;
  padding-left: 18px;
}
.agent-plan-list li {
  margin: 2px 0;
  line-height: 1.6;
}
.agent-plan-text {
  margin-left: 6px;
}
.agent-thought {
  margin: 2px 0;
  line-height: 1.6;
  color: #5c5145;
}
.agent-tag {
  display: inline-block;
  padding: 1px 7px;
  font-size: 10.5px;
  color: #fff;
  background: #c98a4b;
  border-radius: 999px;
}
.agent-tool-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 3px 0;
}
.agent-tool-row.failed .agent-tag {
  background: #b05555;
}
.agent-tool-query {
  color: #6b5a48;
  max-width: 45%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.agent-tool-state {
  margin-left: auto;
  color: var(--color-text-secondary);
  display: flex;
  align-items: center;
  gap: 4px;
}

/* Input section */
.input-section {
  flex-shrink: 0;
  max-width: 800px;
  margin: 0 auto;
  width: 100%;
}

/* Guest query banner */
.guest-query-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  margin-bottom: 6px;
  background: linear-gradient(135deg, #fff8e1, #fff3cd);
  border: 1px solid #ffe082;
  border-radius: 10px;
  font-size: 12px;
  color: #8d6e00;
}

.guest-query-banner svg {
  flex-shrink: 0;
  color: #f9a825;
}

.guest-query-banner span {
  flex: 1;
}

.guest-login-link {
  flex-shrink: 0;
  color: #c98a4b;
  text-decoration: none;
  font-weight: 600;
  font-size: 12px;
}

.guest-login-link:hover {
  text-decoration: underline;
}

/* ---- 移动端适配 ---- */
@media (max-width: 768px) {
  .welcome-screen {
    padding: 24px 16px;
  }

  .welcome-title {
    font-size: 22px;
  }

  .welcome-subtitle {
    font-size: 13px;
    margin-bottom: 24px;
  }

  .suggestion-cards {
    grid-template-columns: 1fr;
    gap: 8px;
  }

  .suggestion-card {
    padding: 12px 14px;
    font-size: 13px;
  }

  .messages-container { padding: 40px 0 8px; }
  .messages-list {
    padding: 0 12px;
  }

  .pipeline-indicator {
    padding: 8px 16px;
  }

  .pipeline-message {
    font-size: 12px;
  }

  .input-section {
    padding: 0 12px 8px;
  }
}
</style>
