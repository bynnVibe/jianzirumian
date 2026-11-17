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
      <div
        v-for="(r, i) in results"
        :key="i"
        class="result-card"
        :class="{ 'high-relevance': scoreClass(r.relevance) === 'high' }"
        title="点击查看检索片段全文"
        @click="openDetail(r, i)"
      >
        <div class="result-head">
          <span class="rank">#{{ i + 1 }}</span>
          <span class="result-title">{{ r.is_wiki_page ? (r.wiki_title || r.title) : r.title }}</span>
          <span v-if="r.is_wiki_page" class="wiki-badge">百科</span>
          <span v-if="r.page_number" class="page-badge">第 {{ r.page_number }} 页</span>
          <span class="score" :class="scoreClass(r.relevance)">
            <template v-if="scoreClass(r.relevance) === 'high'">★ {{ r.relevance }}% 高相关</template>
            <template v-else>{{ r.relevance }}% 相关</template>
          </span>
        </div>
        <p class="result-text">
          <span v-for="(p, j) in highlightParts(r.text)" :key="j" :class="{ 'hl-mark': p.hl }">{{ p.t }}</span>
        </p>
        <div class="result-foot">
          <span class="result-source">{{ r.filename }}</span>
          <button class="link-btn" @click.stop="openSource(r)">查看来源</button>
        </div>
      </div>
    </div>

    <!-- 检索片段全文弹层 -->
    <div v-if="detail" class="detail-mask" @click.self="detail = null">
      <div class="detail-modal">
        <div class="detail-head">
          <span class="rank">#{{ detail.rank }}</span>
          <strong class="detail-title">{{ detail.is_wiki_page ? (detail.wiki_title || detail.title) : detail.title }}</strong>
          <span class="score" :class="scoreClass(detail.relevance)">{{ detail.relevance }}% 相关</span>
          <button class="detail-close" @click="detail = null">×</button>
        </div>
        <div class="detail-meta">
          <span v-if="detail.is_wiki_page" class="wiki-badge">百科</span>
          <span v-if="detail.page_number" class="page-badge">第 {{ detail.page_number }} 页</span>
          <span class="result-source">来源：{{ detail.filename }}</span>
        </div>
        <div class="detail-body">
          <span v-for="(p, j) in highlightParts(detail.text)" :key="j" :class="{ 'hl-mark': p.hl }">{{ p.t }}</span>
        </div>
        <div class="detail-foot">
          <button class="link-btn" @click="openSource(detail)">查看来源原文</button>
          <button class="btn-search sm" @click="detail = null">关闭</button>
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
const detail = ref(null)

function openDetail(r, i) {
  detail.value = { ...r, rank: i + 1 }
}

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

// ---- 句子级高亮：按句末标点切句，命中查询词的句子才高亮，不整段框选 ----
function queryTerms(q) {
  return (q || '').trim().split(/[\s,，、;；]+/).filter(Boolean)
}
function termChars(t) {
  return [...new Set(t.match(/[\u4e00-\u9fa5A-Za-z0-9]/g) || [])]
}
function sentenceHit(sentence, terms) {
  return terms.some((t) => {
    if (sentence.includes(t)) return true
    // 字符全覆盖兜底：如查「零税率」可命中「税率为零…」这类同义句式
    const chars = termChars(t)
    return chars.length >= 2 && chars.every(c => sentence.includes(c))
  })
}
function highlightParts(text) {
  const sentences = String(text || '').match(/[^。！？；\n]+[。！？；\n]?/g) || []
  const terms = queryTerms(query.value)
  if (!terms.length) return sentences.map(t => ({ t, hl: false }))
  return sentences.map(t => ({ t, hl: sentenceHit(t, terms) }))
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

.result-list { display: flex; flex-direction: column; gap: 12px; align-items: stretch; }
.result-card {
  background: #fff; border: 1px solid var(--color-border); border-radius: 10px;
  padding: 14px 16px; cursor: pointer; transition: box-shadow 0.15s, border-color 0.15s;
}
.result-card:hover { border-color: var(--color-primary); box-shadow: 0 2px 8px rgba(120, 90, 50, 0.10); }
.result-card.high-relevance {
  border-left: 4px solid #2e7d32;
  box-shadow: 0 1px 6px rgba(46, 125, 50, 0.12);
}
.result-card.high-relevance:hover { border-color: #2e7d32; }
.result-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }
.rank { font-size: 12px; font-weight: 700; color: var(--color-primary); }
.result-title { font-size: 14px; font-weight: 600; color: var(--color-text); }
.wiki-badge { font-size: 11px; background: var(--color-primary-light); color: var(--color-primary); padding: 1px 7px; border-radius: 5px; }
.page-badge { font-size: 11px; background: #f3efe9; color: var(--color-text-secondary); padding: 1px 7px; border-radius: 5px; }
.score { margin-left: auto; font-size: 12px; font-weight: 600; padding: 2px 9px; border-radius: 6px; }
.score.high { background: #e8f5e9; color: #2e7d32; }
.result-card.high-relevance .score.high {
  font-size: 13px; font-weight: 800; padding: 3px 12px;
  background: #2e7d32; color: #fff; letter-spacing: 0.3px;
}
.score.mid { background: #fff8e1; color: #b8860b; }
.score.low { background: #fdecea; color: #c0392b; }

.result-text {
  margin: 0 0 10px; font-size: 13px; line-height: 1.7; color: #5a4f42;
  white-space: pre-wrap; word-break: break-word;
}
/* 命中查询词的句子级高亮 */
.hl-mark { background: #ffe9a8; color: #6b4a00; border-radius: 3px; padding: 0 1px; }
.result-foot { display: flex; align-items: center; justify-content: space-between; }
.result-source { font-size: 12px; color: var(--color-text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.link-btn { border: none; background: none; color: var(--color-primary); font-size: 12px; cursor: pointer; flex-shrink: 0; }
.link-btn:hover { text-decoration: underline; }

/* 全文弹层 */
.detail-mask {
  position: fixed; inset: 0; background: rgba(60, 45, 30, 0.45);
  display: flex; align-items: center; justify-content: center; z-index: 1000; padding: 24px;
}
.detail-modal {
  background: #fffdfb; border-radius: 12px; max-width: 760px; width: 100%;
  max-height: 82vh; display: flex; flex-direction: column; padding: 18px 22px;
  box-shadow: 0 8px 32px rgba(60, 45, 30, 0.25);
}
.detail-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.detail-title { font-size: 15px; color: var(--color-text); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.detail-close { border: none; background: none; font-size: 22px; line-height: 1; cursor: pointer; color: var(--color-text-secondary); padding: 0 2px; }
.detail-close:hover { color: var(--color-text); }
.detail-meta { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.detail-body {
  flex: 1; overflow-y: auto; font-size: 13.5px; line-height: 1.8; color: #5a4f42;
  white-space: pre-wrap; word-break: break-word;
  background: #faf8f5; border: 1px solid var(--color-border); border-radius: 8px; padding: 14px 16px;
}
.detail-foot { display: flex; align-items: center; justify-content: space-between; margin-top: 12px; }
.btn-search.sm { padding: 7px 18px; font-size: 12.5px; }
</style>