<template>
  <section class="url-review">
    <div class="source-meta">
      <a v-if="safeArticleUrl(item.source_url)" :href="safeArticleUrl(item.source_url)" target="_blank" rel="noopener noreferrer">打开原网页 ↗</a>
      <span>{{ item.source_site || '公开网页' }}</span>
      <span>{{ item.extraction_method === 'browser' ? '浏览器提取' : item.fetched_at ? '网页提取' : '等待解析／手动补充' }}</span>
      <span v-if="item.fetched_at">{{ new Date(item.fetched_at).toLocaleString() }}</span>
      <span>{{ urlStatusLabel(item) }}</span>
    </div>
    <p v-if="item.error" class="notice" role="status">{{ item.error }}</p>
    <p v-for="warning in item.warnings || []" :key="warning" class="notice">{{ warning }}</p>
    <p v-if="!item.original_text && editable" class="notice">可粘贴你有权使用的正文；保存后将标记为“用户补充”，不会视为抓取成功。</p>
    <label>入库标题<input v-model="title" :disabled="!editable || busy" maxlength="300" placeholder="文章标题" /></label>
    <div class="editor-toolbar"><span>{{ text.length }} 字 · 版本 {{ item.revision }}{{ dirty ? ' · 未保存' : ' · 已保存' }}</span>
      <button type="button" @click="preview = !preview">{{ preview ? '编辑正文' : '预览正文' }}</button></div>
    <div v-if="preview" class="article-preview" v-html="renderArticle(text)"></div>
    <textarea v-else v-model="text" :disabled="!editable || busy" maxlength="500000" rows="16" aria-label="链接正文" placeholder="解析后的 Markdown 正文；可检查并修改后确认入库"></textarea>
    <details v-if="item.original_text"><summary>查看最初提取正文（不随编辑改变）</summary><pre>{{ item.original_text }}</pre></details>
    <div v-if="editable" class="actions">
      <button :disabled="busy || !dirty" @click="save">{{ busy ? '处理中…' : '保存草稿' }}</button>
      <button :disabled="busy || !text.trim() || !canWrite" class="confirm" @click="confirmDraft">确认当前正文并入库</button>
      <span v-if="!canWrite">请选择你有写权限的知识库，或新建个人知识库。</span>
    </div>
    <p v-else-if="item.status === 'ingesting'" role="status">正在入库，请勿重复提交；本页面将自动更新结果。</p>
    <p v-else-if="item.status === 'succeeded'" class="success" role="status">已成功入库 {{ item.result?.chunk_count }} 个片段。{{ item.result?.wiki_warning || '百科编译在后台进行。' }}</p>
    <p v-if="message" class="notice" role="alert">{{ message }}</p>
  </section>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { saveUrlImport, confirmUrlImport } from '@/api'
import { renderArticle, safeArticleUrl, urlDraftFingerprint, urlStatusLabel } from '@/utils/urlArticles'
const props = defineProps({ item: { type: Object, required: true }, kbId: String, canWrite: Boolean })
const emit = defineEmits(['updated', 'dirty', 'submitted', 'busy'])
const title = ref(props.item.title || '')
const text = ref(props.item.parsed_text || '')
const saved = ref(urlDraftFingerprint(title.value, text.value))
const preview = ref(false)
const busy = ref(false)
const message = ref('')
const editable = computed(() => props.item.phase === 'parse' && ['done', 'error'].includes(props.item.status))
const dirty = computed(() => editable.value && saved.value !== urlDraftFingerprint(title.value, text.value))
watch(dirty, (value) => emit('dirty', value), { immediate: true })
watch(busy, (value) => emit('busy', value), { immediate: true })
async function persist() {
  if (!dirty.value) return props.item
  const res = await saveUrlImport(props.item.id, { expected_revision: props.item.revision, title: title.value, parsed_text: text.value })
  title.value = res.data.item.title
  text.value = res.data.item.parsed_text
  saved.value = urlDraftFingerprint(title.value, text.value)
  emit('dirty', false)
  emit('updated', res.data.item)
  return res.data.item
}
async function save() {
  if (busy.value) return
  busy.value = true; message.value = ''
  try { await persist(); message.value = '草稿已保存，尚未入库。' }
  catch (error) { message.value = error.response?.data?.detail || '保存失败，本地编辑已保留，请重试。' }
  finally { busy.value = false }
}
async function confirmDraft() {
  if (busy.value || !editable.value || !props.canWrite || !text.value.trim()) return
  busy.value = true; message.value = ''
  try {
    const draft = await persist()
    const res = await confirmUrlImport(draft.id, draft.revision, props.kbId)
    emit('updated', res.data.item)
    emit('submitted')
  } catch (error) { message.value = error.response?.data?.detail || '确认请求失败，请刷新任务状态后核对；不要重新创建重复任务。' }
  finally { busy.value = false }
}
</script>

<style scoped>
.url-review { padding: 12px 0; }
.source-meta, .editor-toolbar, .actions { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-bottom: 14px; font-size: 12px; }
.source-meta { color: var(--color-text-secondary); }
a { color: var(--color-primary); }
label { display: block; font-size: 13px; }
input, textarea { display: block; width: 100%; border: 1px solid var(--color-border); border-radius: 8px; padding: 12px; margin: 8px 0 14px; background: #fffdf9; color: var(--color-text); font: inherit; }
textarea { font-family: monospace; resize: vertical; line-height: 1.7; }
button { border: 1px solid var(--color-border); background: #fff; color: var(--color-text); border-radius: 7px; padding: 7px 12px; cursor: pointer; }
button:disabled { opacity: .5; cursor: not-allowed; }
.confirm { background: var(--color-primary); color: #fff; }
.notice { color: #8b5e22; background: #fff7e7; padding: 10px; border-radius: 7px; font-size: 13px; white-space: pre-wrap; }
.success { color: #32804e; }
details { font-size: 12px; margin-bottom: 16px; }
pre { max-height: 260px; overflow: auto; white-space: pre-wrap; }
.article-preview { border: 1px solid var(--color-border); padding: 16px; border-radius: 8px; line-height: 1.8; overflow-wrap: anywhere; max-height: 650px; overflow: auto; }
.article-preview :deep(pre) { overflow: auto; background: #f5f1eb; padding: 12px; }
.article-preview :deep(table) { border-collapse: collapse; display: block; overflow-x: auto; }
.article-preview :deep(td), .article-preview :deep(th) { border: 1px solid #ddd; padding: 6px; }
</style>
