<template>
  <section class="action-card" :aria-busy="busy" aria-live="polite">
    <header>附件入库确认 · {{ labels[action.status] || action.status }}</header>
    <p>{{ action.filename }} · {{ action.tool_name === 'image_upload' ? '图片识别入库' : '文档解析入库' }}</p>
    <label v-if="action.status === 'pending'">目标知识库
      <select v-model="target" :disabled="busy">
        <option v-if="target && !(action.targets || []).some(k => k.id === target)" :value="target" disabled>原目标已不可写，请重新选择</option>
        <option value="">确认后创建个人库（仅自己可见）</option>
        <option v-for="kb in action.targets || []" :key="kb.id" :value="kb.id">
          {{ kb.name }}（{{ kb.visibility === 'public' ? '公共可见' : '仅自己可见' }}）
        </option>
      </select>
    </label>
    <p v-else>目标知识库：{{ action.target_name }}（{{ action.kb_id || '未创建' }}）</p>
    <p>可见性：{{ visibility === 'public' ? '公共知识库，其他用户可检索' : '个人知识库，仅自己可见' }}</p>
    <small>有效期至 {{ new Date(action.expires_at).toLocaleString() }}</small>
    <p v-if="action.status === 'pending'">尚未入库。确认后才会执行，取消不会删除聊天附件。</p>
    <p v-if="action.status === 'running'">正在执行，不能撤销；请勿重复上传。</p>
    <p v-if="action.status === 'succeeded'">入库完成：{{ action.result?.chunk_count || 0 }} 个知识片段。</p>
    <p v-if="['failed', 'interrupted'].includes(action.status)">可能部分落库，请先核对知识库；不会自动重试。</p>
    <p v-if="error || action.error" class="action-error" role="alert">{{ error || action.error }}</p>
    <div v-if="action.status === 'pending'" class="action-buttons">
      <button :disabled="busy" @click="submit(true)">确认入库</button>
      <button :disabled="busy" @click="submit(false)">取消提案</button>
    </div>
    <button v-if="error" :disabled="busy" @click="refresh">刷新状态</button>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { cancelChatAction, confirmChatAction, getChatAction } from '@/api'
const props = defineProps({ action: { type: Object, required: true } })
const emit = defineEmits(['update'])
const target = ref(props.action.kb_id || '')
const busy = ref(false)
const error = ref('')
const labels = { pending: '待确认', running: '执行中', succeeded: '已完成', failed: '失败', cancelled: '已取消', expired: '已过期', interrupted: '已中断' }
const visibility = computed(() => props.action.status === 'pending'
  ? (props.action.targets || []).find(k => k.id === target.value)?.visibility || 'private'
  : props.action.visibility)
let timer, disposed = false
const controller = new AbortController()
function poll() {
  clearTimeout(timer)
  if (!disposed && ['pending', 'running'].includes(props.action.status)) {
    timer = setTimeout(refresh, props.action.status === 'running' ? 2000 : 30000)
  }
}
async function refresh() {
  if (disposed) return
  try {
    const { data } = await getChatAction(props.action.id, controller.signal)
    if (!disposed) { emit('update', data); error.value = '' }
  } catch (err) {
    if (!disposed) error.value = err.response?.data?.detail || '状态读取失败，可手动刷新'
  } finally { poll() }
}
async function submit(confirmed) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    const { data } = confirmed ? await confirmChatAction(props.action.id, target.value) : await cancelChatAction(props.action.id)
    if (!disposed) emit('update', data)
  } catch (err) {
    if (!disposed) error.value = err.response?.data?.detail || '请求失败，请刷新状态后再操作'
  } finally { busy.value = false; poll() }
}
watch(() => props.action.status, poll)
onMounted(refresh)
onBeforeUnmount(() => { disposed = true; clearTimeout(timer); controller.abort() })
</script>

<style scoped>
.action-card { margin: 8px 0 20px 44px; padding: 16px; border: 1px solid var(--border-color, #ddd); border-radius: 12px; background: var(--bg-secondary, #f7f8fa); font-size: 14px; }
.action-card header { font-weight: 600; }
.action-card p { margin: 8px 0; }
.action-card select { display: block; width: 100%; padding: 8px; margin: 6px 0; color: inherit; background: var(--bg-primary, white); border: 1px solid #aaa; border-radius: 6px; }
.action-buttons { display: flex; gap: 10px; margin-top: 12px; }
.action-card button { padding: 7px 14px; border: 1px solid #8b7355; border-radius: 6px; cursor: pointer; }
.action-card button:disabled { opacity: .5; cursor: wait; }
.action-error { color: #b33; }
</style>
