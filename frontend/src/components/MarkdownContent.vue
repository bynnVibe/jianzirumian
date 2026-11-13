<template>
  <div class="md-content" v-html="html"></div>
</template>

<script setup>
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'
import { safeArticleUrl } from '@/utils/urlArticles'

const props = defineProps({
  content: { type: String, default: '' },
})

// 与全站约定一致：html:false 作为 XSS 防线；链接/图片仅放行安全 http(s) 地址
const md = new MarkdownIt({ html: false, linkify: true, breaks: true })
md.validateLink = (url) => !!safeArticleUrl(url)
md.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
  tokens[index].attrSet('target', '_blank')
  tokens[index].attrSet('rel', 'noopener noreferrer')
  return renderer.renderToken(tokens, index, options)
}

const html = computed(() => md.render(props.content || ''))
</script>

<style scoped>
.md-content {
  font-size: 15px;
  line-height: 1.85;
  color: #2c2c2c;
  word-break: break-word;
}

.md-content :deep(h1),
.md-content :deep(h2),
.md-content :deep(h3),
.md-content :deep(h4),
.md-content :deep(h5),
.md-content :deep(h6) {
  color: #1a1a1a;
  font-weight: 700;
  margin: 22px 0 10px;
  line-height: 1.4;
}
.md-content :deep(h1) { font-size: 24px; padding-bottom: 8px; border-bottom: 1px solid #ede6dc; }
.md-content :deep(h2) { font-size: 20px; padding-bottom: 6px; border-bottom: 1px solid #f3efe9; }
.md-content :deep(h3) { font-size: 17px; }
.md-content :deep(h4) { font-size: 15px; }
.md-content :deep(h5),
.md-content :deep(h6) { font-size: 14px; color: #555; }
.md-content :deep(h1:first-child),
.md-content :deep(h2:first-child),
.md-content :deep(h3:first-child) { margin-top: 0; }

.md-content :deep(p) { margin: 8px 0; }

.md-content :deep(a) { color: #c98a4b; text-decoration: none; }
.md-content :deep(a:hover) { text-decoration: underline; }

.md-content :deep(ul),
.md-content :deep(ol) { margin: 8px 0; padding-left: 26px; }
.md-content :deep(li) { margin: 4px 0; }
.md-content :deep(li > ul),
.md-content :deep(li > ol) { margin: 2px 0; }

.md-content :deep(blockquote) {
  margin: 12px 0;
  padding: 8px 16px;
  border-left: 3px solid #c98a4b;
  background: #faf8f5;
  color: #6b6257;
  border-radius: 0 6px 6px 0;
}
.md-content :deep(blockquote p) { margin: 4px 0; }

.md-content :deep(code) {
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 13px;
  background: #f3efe9;
  color: #a05a2c;
  padding: 2px 6px;
  border-radius: 4px;
}
.md-content :deep(pre) {
  margin: 12px 0;
  padding: 14px 16px;
  background: #2f2a24;
  border-radius: 8px;
  overflow-x: auto;
  line-height: 1.6;
}
.md-content :deep(pre code) {
  background: none;
  color: #f0e8dd;
  padding: 0;
  font-size: 13px;
}

.md-content :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: 16px 0;
  font-size: 14px;
  display: block;
  overflow-x: auto;
}
.md-content :deep(td),
.md-content :deep(th) {
  border: 1px solid #d0c8be;
  padding: 8px 12px;
  vertical-align: top;
  line-height: 1.6;
}
.md-content :deep(th) { background: #f3efe9; font-weight: 600; }
.md-content :deep(tr:nth-child(even) td) { background: #fdfaf5; }

.md-content :deep(hr) {
  border: none;
  border-top: 1px solid #ede6dc;
  margin: 24px 0;
}

.md-content :deep(img) {
  max-width: 100%;
  border-radius: 6px;
  margin: 8px 0;
}
</style>