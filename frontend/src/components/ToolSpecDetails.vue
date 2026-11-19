<template>
  <div class="tool-spec">
    <p>{{ spec.description }}</p>
    <p class="limit">{{ spec.limitations }}</p>
    <p>执行器：{{ executorLabel }} · {{ review.requires_admin ? '需要管理员版本审批' : '固定只读连接器' }}</p>
    <ul v-if="spec.sources?.length" class="sources">
      <li v-for="url in spec.sources" :key="url"><a v-if="safeUrl(url)" :href="safeUrl(url)" target="_blank" rel="noopener noreferrer">{{ url }}</a><span v-else>来源地址无效</span></li>
    </ul>
    <details>
      <summary>查看完整配置、输入输出与验收样例</summary>
      <p>添加时不执行安装命令。测试只验证声明样例，不代表每次回答均正确。</p>
      <pre>{{ JSON.stringify(spec, null, 2) }}</pre>
      <button type="button" @click="download">下载声明 JSON</button>
    </details>
  </div>
</template>

<script setup>
import { computed } from 'vue'
const props = defineProps({ spec: { type: Object, required: true }, review: { type: Object, default: () => ({}) } })
const executorLabel = computed(() => ({ connector: '只读连接器', http: '固定 HTTP API', python: '隔离 Python（需环境及审批）' }[props.spec.executor] || '未知'))
function safeUrl(value) {
  try {
    const url = new URL(value)
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : ''
  } catch { return '' }
}
function download() {
  const url = URL.createObjectURL(new Blob([JSON.stringify(props.spec, null, 2)], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url
  link.download = 'personal-tool.json'
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
</script>

<style scoped>
.tool-spec { min-width: 0; font-size: 13px; line-height: 1.7; overflow-wrap: anywhere; }
p { margin: 7px 0; }
.limit { color: #865d36; }
.sources { padding-left: 20px; }
a { color: #80613e; }
details { border-top: 1px solid #e9e1d5; padding-top: 8px; }
summary { cursor: pointer; color: #705437; }
pre { max-height: 360px; overflow: auto; padding: 10px; background: #f5f3ef; white-space: pre; font-size: 12px; }
button { padding: 6px 10px; border: 1px solid #d9cdbb; background: #fffaf1; border-radius: 6px; cursor: pointer; }
</style>
