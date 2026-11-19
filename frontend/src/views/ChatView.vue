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
            :tool-sources="msg.tool_sources || []"
            :attachments="msg.attachments"
            :message-id="msg.message_id"
            :feedback="msg.feedback"
            :can-feedback="!auth.isGuest && !msg.interrupted"
            :cache-hit="!!msg.cache_hit"
            :cache-score="msg.cache_score || 0"
            :cache-info="msg.cache_info || {}"
            :can-refresh="canRefresh(msg, idx)"
            :busy="store.isGenerating"
            @refresh="refreshAnswer(idx)"
            @reask="handleReask"
            @feedback="(fb) => handleFeedback(msg, fb)"
          >
            <template #process>
              <AgentProcessPanel v-if="msg.agent_process" :plan="msg.agent_process.plan" :tools="msg.agent_process.tools"
                :collapsed="msg.process_collapsed !== false" @toggle="msg.process_collapsed = !(msg.process_collapsed !== false)" />
            </template>
            <template #cards>
              <ChatActionCard v-for="action in msg.actions || []" :key="action.id" :action="action" @update="store.updateAction" />
              <CapabilityGapCard v-if="msg.capability_gap" :gap="msg.capability_gap" />
              <SkillDraftCard v-for="d in msg.skill_drafts || []" :key="d.id" :draft="d" @update="store.updateSkillDraft" />
              <ToolDraftCard v-for="d in msg.tool_drafts || []" :key="d.id" :draft="d" @update="store.updateToolDraft" />
            </template>
          </ChatMessage>
          <!-- HITL 卡片：联网确认 / 意图澄清（后端暂停本轮，等待用户响应后触发新请求） -->
          <ChatHitlCard
            v-else
            :hitl="msg.hitl"
            :busy="store.isGenerating"
            @search="handleHitlSearch"
            @direct="handleHitlDirect"
            @option="handleHitlOption"
          />

        </div>
        <!-- 正在生成的流式响应 -->
        <ChatMessage v-if="store.isGenerating" role="assistant" :content="currentResponse"
          :sources="store.sources" :search-results="store.searchResults" :tool-sources="store.toolSources"
          :cache-hit="!!store.cacheHitInfo?.hit" :cache-info="store.cacheHitInfo || {}">
          <template #process>
            <AgentProcessPanel :plan="store.agentPlan" :tools="store.agentTools"
              :steps="pipelineSteps" :status-text="phaseMessage" :collapsed="processCollapsed" live
              @toggle="toggleProcess" />
          </template>
          <template #cards>
            <SkillDraftCard v-if="store.skillDraft" :draft="store.skillDraft" @update="store.updateSkillDraft" />
            <ToolDraftCard v-if="store.toolDraft" :draft="store.toolDraft" @update="store.updateToolDraft" />
          </template>
        </ChatMessage>
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
      <!-- 中途停止任务 checkpoint：是否继续 -->
      <div v-if="store.pendingCheckpoint && !store.isGenerating" class="checkpoint-bar">
        <div class="checkpoint-text">
          <span class="checkpoint-title">检测到中途停止的任务，是否继续？</span>
          <span class="checkpoint-query">{{ store.pendingCheckpoint.query }}</span>
          <span class="checkpoint-hint">继续将在前序进度基础上续答；不继续则软删除（保留 48 小时，可后续检索）</span>
        </div>
        <div class="checkpoint-actions">
          <button class="checkpoint-btn primary" @click="store.resumeCheckpoint()">继续任务</button>
          <button class="checkpoint-btn ghost" @click="store.abandonPendingCheckpoint()">不继续</button>
        </div>
      </div>
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
import AgentProcessPanel from '@/components/AgentProcessPanel.vue'
import SkillDraftCard from '@/components/SkillDraftCard.vue'
import ToolDraftCard from '@/components/ToolDraftCard.vue'
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
    case 'capability_discovery': return '本次能力添加将联网查找资料，不改变普通聊天的联网开关'
    case 'skill_loading': return '正在匹配个人 Skill 并核验资源…'
    case 'tool_selecting': return '正在匹配已授权个人工具…'
    case 'native_tools': return '正在执行原生工具调用；个人 API 使用独立网络授权'
    case 'tool_results': return '工具结果已返回，正在核验数据并生成回答'
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
    case 'cache_hit': return '已核对上下文与证据，复用有效答案'
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

// 灰色过程区收缩控制：新一轮生成展开；最终答案开始流式呈现时自动收缩，
// 答案与收缩后的过程区同屏，用户可点头部随时展开回看。
const processCollapsed = ref(false)
const processTouched = ref(false)
function toggleProcess() {
  processTouched.value = true
  processCollapsed.value = !processCollapsed.value
}
watch(() => store.isGenerating, (gen) => {
  if (gen) { processCollapsed.value = false; processTouched.value = false }
})
watch(currentResponse, (text, old) => {
  if (text && !old && store.isGenerating && !processTouched.value) processCollapsed.value = true
})

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
async function handleSend(text, files, options = {}) {
  scrollToBottom()
  await store.sendMessage(text, files, options)
}

function canRefresh(msg, idx) {
  const question = messages.value[idx - 1]
  return msg.role === 'assistant' && question?.role === 'user' && !question.attachments?.length &&
    !msg.skill_drafts?.length && !msg.tool_drafts?.length && !msg.actions?.length && !msg.capability_gap &&
    !/^@(skills|tools?)\b/i.test(question.content || '')
}
function refreshAnswer(idx) {
  if (!canRefresh(messages.value[idx], idx) || store.isGenerating) return
  const question = messages.value[idx - 1]
  store.useKnowledge = question.use_knowledge ?? store.useKnowledge
  store.useSearch = question.use_search ?? store.useSearch
  handleSend(question.content, [], { forceRefresh: true })
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
  // 返回对话页时恢复待处理的中断任务提示
  if (!auth.isGuest) store.refreshCheckpoints()

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
.checkpoint-bar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 0 auto 10px; max-width: 760px; width: 100%; padding: 10px 14px; background: #fdf6ec; border: 1px solid #e8d5b5; border-radius: 10px; }
.checkpoint-text { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.checkpoint-title { font-size: 13px; font-weight: 700; color: var(--color-text); }
.checkpoint-query { font-size: 12px; color: var(--color-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.checkpoint-hint { font-size: 11px; color: var(--color-text-secondary); }
.checkpoint-actions { display: flex; gap: 8px; flex-shrink: 0; }
.checkpoint-btn { padding: 6px 16px; border-radius: 8px; font-size: 13px; cursor: pointer; border: 1px solid var(--color-border); background: #fff; color: var(--color-text); }
.checkpoint-btn.primary { background: var(--color-primary, #c98a4b); border-color: var(--color-primary, #c98a4b); color: #fff; }
.checkpoint-btn.primary:hover { opacity: .9; }
.checkpoint-btn.ghost:hover { border-color: #c0392b; color: #c0392b; }
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

/* Agent 过程区已抽为 AgentProcessPanel 组件（灰色可折叠，答案呈现时自动收缩） */

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
