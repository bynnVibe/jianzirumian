<template>
  <section class="gap-card" aria-live="polite">
    <div class="gap-head">
      <span class="gap-badge">实时能力受限</span>
      <span v-if="gap.scenario_label || gap.scenario" class="gap-scenario">{{ gap.scenario_label || gap.scenario }}</span>
    </div>
    <p class="gap-message">{{ message }}</p>
    <p v-if="gap.query" class="gap-query" :title="gap.query">问题：{{ gap.query }}</p>

    <!-- DeepSeek 式降级：只给应用内动作建议，绝不要求用户执行命令 -->
    <div v-if="suggestions.length" class="gap-actions">
      <button v-for="s in suggestions" :key="s.id" class="gap-btn"
              :class="{ primary: s.id === 'enable_search' }"
              :disabled="store.isGenerating" @click="act(s)">
        {{ s.label }}
      </button>
    </div>
  </section>
</template>

<script setup>
import { computed } from 'vue'
import { useChatStore } from '@/stores/chat'

const props = defineProps({
  gap: { type: Object, required: true },
})
const emit = defineEmits(['enable-search', 'retry'])
const store = useChatStore()

const DEFAULT_MESSAGE = '抱歉，当前能力不足以直接回答这类实时问题。开启「联网搜索」后重新提问，我可以结合网络信息作答。'

const message = computed(() => props.gap.message || DEFAULT_MESSAGE)
const suggestions = computed(() => {
  const list = Array.isArray(props.gap.suggestions) ? props.gap.suggestions : []
  if (list.length) return list
  // 存量历史消息（旧版 CLI 方案卡）统一降级为应用内建议，不再暴露命令
  return [{ id: 'enable_search', label: '开启联网搜索并重新提问' }]
})

function act(s) {
  if (s.id === 'enable_search') emit('enable-search', props.gap.query || '')
  else if (s.id === 'retry') emit('retry', props.gap.query || '')
}
</script>

<style scoped>
.gap-card {
  margin: 8px 0 20px 44px;
  border: 1px solid #eed9bb;
  border-radius: 10px;
  background: #fffdf9;
  padding: 10px 12px;
}

.gap-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  flex-wrap: wrap;
}

.gap-badge {
  font-size: 10.5px;
  font-weight: 600;
  color: #fff;
  background: #c98a4b;
  border-radius: 999px;
  padding: 2px 8px;
}

.gap-scenario {
  font-size: 11.5px;
  color: #8a6d4d;
  background: #f7ecd9;
  border-radius: 999px;
  padding: 2px 8px;
}

.gap-message {
  font-size: 13px;
  color: #4a3f35;
  margin: 0 0 4px;
  line-height: 1.6;
}

.gap-query {
  font-size: 12px;
  color: #8a7a68;
  margin: 0 0 8px;
}

.gap-actions {
  margin-top: 7px;
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.gap-btn {
  padding: 5px 13px;
  font-size: 12px;
  color: #6b5a48;
  background: #fff;
  border: 1px solid #d8c3a5;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease;
}

.gap-btn:hover:not(:disabled) {
  background: #fbf3e8;
  border-color: #c98a4b;
}

.gap-btn.primary {
  color: #fff;
  background: #c98a4b;
  border-color: #c98a4b;
}

.gap-btn.primary:hover:not(:disabled) { background: #b8783c; }

.gap-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
