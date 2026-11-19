<template>
  <section class="tool-draft" aria-label="个人工具草稿">
    <header><strong>{{ draft.spec?.name || '个人工具' }}</strong><span>{{ statusText }}</span></header>
    <ToolSpecDetails v-if="draft.spec" :spec="draft.spec" :review="draft.review" />
    <p v-if="readonly" class="hint">原 Skill 草稿已失效，关联工具不能从此卡片继续确认。</p>
    <template v-if="draft.status === 'awaiting_confirmation' && !readonly">
      <p class="hint">确认将添加个人配置并执行只读样例测试；缺少审批、凭据或隔离环境时保持关闭。</p>
      <label v-if="draft.review?.requires_terms"><input v-model="acceptTerms" type="checkbox" :disabled="busy" />我确认仅用于非商业用途，并遵守 Open-Meteo 免费接口条款、CC-BY 4.0 署名与调用限额</label>
      <label v-if="sameNameConflict"><input v-model="allowSameName" type="checkbox" :disabled="busy" />已有同名工具，另存一份（不覆盖旧工具）</label>
      <div class="actions">
        <button :disabled="busy" @click="confirm">{{ busy ? '处理中…' : '确认添加并测试' }}</button>
        <button :disabled="busy" @click="cancel">取消草稿</button>
      </div>
    </template>
    <template v-else-if="draft.status === 'installed'">
      <p>{{ draft.tool?.status === 'ready' ? '本版本样例测试通过，可在聊天中按需调用。' : '配置已保存，工具尚未就绪或已关闭。请到个人工具管理查看测试与配置要求。' }}</p>
      <router-link to="/skills">打开个人能力管理 →</router-link>
    </template>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <button class="refresh" :disabled="busy" @click="refresh">刷新状态</button>
  </section>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { confirmToolDraft, cancelToolDraft, getToolDraft } from '@/api'
import ToolSpecDetails from './ToolSpecDetails.vue'
const props = defineProps({ draft: { type: Object, required: true }, readonly: { type: Boolean, default: false } })
const emit = defineEmits(['update'])
const busy = ref(false)
const error = ref('')
const acceptTerms = ref(false)
const allowSameName = ref(false)
const sameNameConflict = ref(false)
watch(() => props.draft.content_hash, () => { acceptTerms.value = false; allowSameName.value = false; sameNameConflict.value = false })
const statusText = computed(() => ({ awaiting_confirmation: '待确认', installed: '已保存配置', cancelled: '已取消', expired: '已过期', testing: '测试中' }[props.draft.status] || '状态待核验'))
async function perform(action) {
  busy.value = true
  error.value = ''
  try { emit('update', (await action()).data) }
  catch (err) {
    const detail = err.response?.data?.detail
    error.value = typeof detail === 'string' ? detail : '请求未完成，请刷新核验状态后重试。'
    sameNameConflict.value = error.value.includes('同名')
  } finally { busy.value = false }
}
function confirm() { if (!props.readonly && !busy.value) return perform(() => confirmToolDraft(props.draft.id, props.draft.content_hash, acceptTerms.value, allowSameName.value)) }
function cancel() { if (!props.readonly && !busy.value) return perform(() => cancelToolDraft(props.draft.id)) }
function refresh() { return perform(() => getToolDraft(props.draft.id)) }
</script>

<style scoped>
.tool-draft { margin: 14px 0; padding: 16px; background: #fffaf1; border: 1px solid #dfd3be; border-radius: 12px; min-width: 0; color: #51483e; font-size: 13px; }
header { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; }
header span, .hint { color: #82725c; }
label { display: flex; align-items: flex-start; gap: 7px; margin: 12px 0; line-height: 1.6; }
input { margin-top: 4px; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0; }
button { border: 1px solid #cfbea1; background: #fffefb; padding: 7px 12px; border-radius: 7px; cursor: pointer; color: #6b5031; }
button:disabled { opacity: .55; cursor: wait; }
.refresh { margin-top: 10px; }
a { color: #80613e; }
.error { color: #a43a2e; }
</style>
