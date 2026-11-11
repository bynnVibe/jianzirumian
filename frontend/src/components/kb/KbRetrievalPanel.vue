<template>
  <div class="kb-retrieval">
    <div class="retrieval-bar">
      <input
        v-model="query"
        class="query-input"
        placeholder="输入问题或关键词，测试当前知识库的检索效果…"
        @keyup.enter="runSearch"
      />
      <select v-model.number="topK" class="topk-select">
        <option :value="3">Top 3</option>
        <option :value="5">Top 5</option>
        <option :value="10">Top 10</option>
      </select>
      <button class="btn-search" :disabled="searching || !query.trim()" @click="runSearch">
        {{ searching ? '检索中…' : '检索测试' }}
      </button>
    </div>

    <p v-if="message" class="msg" :class="{ error: !success }">{{ message }}</p>

    <div v-if="!searched && !searching" class="state-box">
      输入查询内容，查看当前知识库命中的分片与相关度
    </div>
    <div v-else-if="searching" class="state-box">检索中…</div>
    <div v-else-if="!results.length" class="state-box">未命中任何分片</div>

    <div v-else class="result-list">
      <div v-for="(r, i) in results" :key="i" class="result-card">
        <div class="result-head">
          <span class="rank">#{{ i + 1 }}</span>
          <span class="result-title">{{ r.is_wiki_page ? (r.wiki_title || r.title) : r.title }}</span>
          <span v-if="r.is_wiki_page" class="wiki-badge">百科</span>
          <span v-if="r.page_number" class="page-badge">第 {{ r.page_number }} 页</span>
          <span class="score" :class="scoreClass(r.relevance)">{{ r.relevance }}% 相关</span>
        </div>
        <p class="result-text">{{ r.text }}</p>
        <div class="result-foot">
          <span class="result-source">{{ r.filename }}</span>
          <button class="link-btn" @click="openSource(r)">查看来源</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { searchKnowledge, getImageUrl } from '@/api'

const props = defineProps({
  kbId: { type: String, required: true },
})

const query = ref('')
const topK = ref(5)
const searching = ref(false)
const searched = ref(false)
const results = ref([])
const message = ref('')
const success = ref(true)

async function runSearch() {
  const q = query.value.trim()
  if (!q || searching.value) return
  searching.value = true
  message.value = ''
  try {
    const res = await searchKnowledge(q, topK.value, props.kbId)
    success.value = !!res.data.success
    results.value = res.data.results || []
    message.value = res.data.message || ''
    searched.value = true
  } catch (e) {
    success.value = false
    message.value = e.response?.data?.detail || e.response?.data?.message || '检索失败'
    results.value = []
    searched.value = true
  } finally {
    searching.value = false
  }
}

function scoreClass(pct) {
  if (pct >= 70) return 'high'
  if (pct >= 40) return 'mid'
  return 'low'
}

function openSource(r) {
  const filename = (r.source_image || '').split('/').pop()
  if (!filename) return
  if (r.source_type === 'word' && r.pdf_preview_path) {
    const pdf = encodeURIComponent(r.pdf_preview_path.split('/').pop())
    const page = r.page_number ? `&page=${r.page_number}` : ''
    window.open(`/doc-preview?file=${encodeURIComponent(filename)}${page}&pdf=${pdf}`, '_blank')
    return
  }
  const url = getImageUrl(filename)
  window.open(r.page_number ? `${url}#page=${r.page_number}` : url, '_blank')
}
</script>

<style scoped>
.kb-retrieval { padding: 20px 24px; height: 100%; overflow-y: auto; box-sizing: border-box; }

.retrieval-bar { display: flex; gap: 10px; margin-bottom: 14px; }
.query-input {
  flex: 1; border: 1px solid var(--color-border); border-radius: 8px;
  padding: 10px 14px; font-size: 14px; outline: none; background: #fff; color: var(--color-text);
}
.query-input:focus { border-color: var(--color-primary); }
.topk-select {
  border: 1px solid var(--color-border); border-radius: 8px; padding: 0 10px;
  font-size: 13px; background: #fff; color: var(--color-text); outline: none;
}
.btn-search {
  background: var(--color-text); color: #fff; border: none; border-radius: 8px;
  padding: 10px 20px; font-size: 13px; cursor: pointer; white-space: nowrap;
}
.btn-search:hover:not(:disabled) { opacity: 0.85; }
.btn-search:disabled { opacity: 0.5; cursor: not-allowed; }

.msg { font-size: 13px; color: var(--color-text-secondary); margin: 0 0 12px; }
.msg.error { color: #c0392b; }

.state-box { padding: 60px 0; text-align: center; color: var(--color-text-secondary); font-size: 14px; }

.result-list { display: flex; flex-direction: column; gap: 12px; }
.result-card { background: #fff; border: 1px solid var(--color-border); border-radius: 10px; padding: 14px 16px; }
.result-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }
.rank { font-size: 12px; font-weight: 700; color: var(--color-primary); }
.result-title { font-size: 14px; font-weight: 600; color: var(--color-text); }
.wiki-badge { font-size: 11px; background: var(--color-primary-light); color: var(--color-primary); padding: 1px 7px; border-radius: 5px; }
.page-badge { font-size: 11px; background: #f3efe9; color: var(--color-text-secondary); padding: 1px 7px; border-radius: 5px; }
.score { margin-left: auto; font-size: 12px; font-weight: 600; padding: 2px 9px; border-radius: 6px; }
.score.high { background: #e8f5e9; color: #2e7d32; }
.score.mid { background: #fff8e1; color: #b8860b; }
.score.low { background: #fdecea; color: #c0392b; }

.result-text {
  margin: 0 0 10px; font-size: 13px; line-height: 1.7; color: #5a4f42;
  white-space: pre-wrap; word-break: break-word;
  display: -webkit-box; -webkit-line-clamp: 6; -webkit-box-orient: vertical; overflow: hidden;
}
.result-foot { display: flex; align-items: center; justify-content: space-between; }
.result-source { font-size: 12px; color: var(--color-text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.link-btn { border: none; background: none; color: var(--color-primary); font-size: 12px; cursor: pointer; flex-shrink: 0; }
.link-btn:hover { text-decoration: underline; }
</style>