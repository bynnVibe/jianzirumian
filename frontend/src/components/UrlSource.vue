<template>
  <span class="url-source" @click.stop>
    <a v-if="url" :href="url" target="_blank" rel="noopener noreferrer">网页原文 ↗</a>
    <button v-if="path" type="button" @click="load">查看入库正文</button>
    <span v-if="source.fetched_at" class="fetched">{{ new Date(source.fetched_at).toLocaleDateString() }} 提取</span>
    <Teleport to="body">
      <div v-if="open" class="url-modal" @click.self="open = false" @keydown.esc="open = false">
        <section role="dialog" aria-modal="true" aria-label="已确认的入库正文" tabindex="-1">
          <header><strong>{{ source.document_title || source.title || '已确认的入库正文' }}</strong><button @click="open = false" autofocus>关闭</button></header>
          <p v-if="error" role="alert">{{ error }}</p><p v-else-if="loading">加载中…</p>
          <article v-else v-html="renderArticle(text)"></article>
        </section>
      </div>
    </Teleport>
  </span>
</template>
<script setup>
import { computed, ref } from 'vue'
import { getUrlSnapshot } from '@/api'
import { renderArticle, safeArticleUrl } from '@/utils/urlArticles'
const props = defineProps({ source: { type: Object, required: true } })
const url = computed(() => safeArticleUrl(props.source.source_url || props.source.final_url))
const path = computed(() => props.source.source_image || props.source.source_path || props.source.image_path || props.source.file_path)
const open = ref(false), loading = ref(false), error = ref(''), text = ref('')
async function load() {
  open.value = true; loading.value = true; error.value = ''
  try { text.value = (await getUrlSnapshot(path.value)).data }
  catch { error.value = '正文快照不可用或无访问权限。' }
  finally { loading.value = false }
}
</script>
<style scoped>
.url-source { display: inline-flex; flex-wrap: wrap; align-items: center; gap: 10px; font-size: 12px; }
a, button { color: var(--color-primary, #95734d); }
button { border: 1px solid #ded5c9; background: #fffdf9; border-radius: 6px; padding: 4px 8px; cursor: pointer; }
.fetched { color: #938677; }
.url-modal { position: fixed; inset: 0; z-index: 2000; background: #0006; display: flex; justify-content: center; align-items: center; padding: 20px; }
section { width: min(820px, 100%); max-height: 85vh; overflow: auto; background: #fffdf9; color: #463d33; border-radius: 12px; padding: 24px; }
header { display: flex; justify-content: space-between; align-items: center; gap: 20px; }
article { line-height: 1.8; overflow-wrap: anywhere; }
article :deep(pre) { overflow: auto; background: #f1ece6; padding: 12px; }
article :deep(table) { border-collapse: collapse; display: block; overflow: auto; }
article :deep(td), article :deep(th) { padding: 6px; border: 1px solid #ddd; }
</style>
