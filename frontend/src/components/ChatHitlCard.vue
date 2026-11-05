<template>
  <section class="hitl-card" aria-live="polite">
    <!-- 联网确认：知识库无结果、未开联网时询问是否联网搜索 -->
    <template v-if="hitl.type === 'need_search'">
      <div class="hitl-head">
        <span class="hitl-badge">需要你的确认</span>
      </div>
      <p class="hitl-question">{{ hitl.message || '知识库中未检索到相关内容，是否联网搜索？' }}</p>
      <div class="hitl-actions">
        <button class="hitl-btn primary" :disabled="busy" @click="emit('search', hitl.query)">联网搜索</button>
        <button class="hitl-btn" :disabled="busy" @click="emit('direct', hitl.query)">直接回答</button>
      </div>
    </template>

    <!-- 意图澄清：证据不足且意图模糊时追问，提供可点选补充方向 -->
    <template v-else-if="hitl.type === 'clarify'">
      <div class="hitl-head">
        <span class="hitl-badge">想更懂你的问题</span>
      </div>
      <p class="hitl-question">{{ hitl.question }}</p>
      <div class="hitl-options" v-if="(hitl.options || []).length">
        <button
          v-for="(opt, oi) in hitl.options"
          :key="oi"
          class="hitl-chip"
          :disabled="busy"
          @click="emit('option', opt)"
        >{{ opt }}</button>
      </div>
    </template>
  </section>
</template>

<script setup>
defineProps({
  hitl: { type: Object, required: true },
  busy: { type: Boolean, default: false },
})
const emit = defineEmits(['search', 'direct', 'option'])
</script>

<style scoped>
.hitl-card {
  margin: 8px 0 20px 44px;
  border: 1px solid #eed9bb;
  border-radius: 10px;
  background: #fffdf9;
  padding: 10px 12px;
}

.hitl-head {
  margin-bottom: 6px;
}

.hitl-badge {
  font-size: 10.5px;
  font-weight: 600;
  color: #fff;
  background: #c98a4b;
  border-radius: 999px;
  padding: 2px 8px;
}

.hitl-question {
  font-size: 13px;
  color: #4a3f35;
  margin: 0 0 10px;
  line-height: 1.6;
}

.hitl-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.hitl-btn {
  padding: 6px 14px;
  font-size: 12.5px;
  color: #6b5a48;
  background: #fff;
  border: 1px solid #d8c3a5;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease;
}

.hitl-btn:hover:not(:disabled) {
  background: #fbf3e8;
  border-color: #c98a4b;
}

.hitl-btn.primary {
  color: #fff;
  background: #c98a4b;
  border-color: #c98a4b;
}

.hitl-btn.primary:hover:not(:disabled) {
  background: #b8783c;
}

.hitl-btn:disabled,
.hitl-chip:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.hitl-options {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.hitl-chip {
  padding: 5px 12px;
  font-size: 12.5px;
  color: #6b5a48;
  background: #fff;
  border: 1px solid #e0cdb2;
  border-radius: 999px;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease;
}

.hitl-chip:hover:not(:disabled) {
  background: #fbf3e8;
  border-color: #c98a4b;
}
</style>
