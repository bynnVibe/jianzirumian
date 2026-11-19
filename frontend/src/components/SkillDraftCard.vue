<template>
  <section class="draft-card" :class="draft.status" aria-live="polite">
    <header class="draft-header">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="16" height="16">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
        <polyline points="14 2 14 8 20 8"/>
        <line x1="9" y1="13" x2="15" y2="13"/><line x1="9" y1="17" x2="13" y2="17"/>
      </svg>
      <span class="draft-title">Skill 草稿 · {{ draft.name }}</span>
      <span class="draft-badge" :class="draft.status">{{ labels[draft.status] || draft.status }}</span>
    </header>

    <div class="draft-meta">
      <div class="draft-meta-row"><span class="draft-key">名称</span><span class="draft-val">{{ draft.name }}</span></div>
      <div v-if="draft.description" class="draft-meta-row"><span class="draft-key">描述</span><span class="draft-val">{{ draft.description }}</span></div>
      <div class="draft-meta-row"><span class="draft-key">类型</span><span class="draft-val">{{ isPackage ? 'agent_skill · 按需工作流' : 'prompt · 对话提示' }}</span></div>
      <div class="draft-meta-row"><span class="draft-key">归属</span><span class="draft-val">个人技能（仅自己可见可用）</span></div>
      <div v-if="review.status" class="draft-meta-row"><span class="draft-key">评审</span><span class="draft-val">{{ reviewLabels[review.status] || review.status }}</span></div>
      <div v-if="review.dependencies?.length" class="draft-meta-row"><span class="draft-key">依赖</span><span class="draft-val">{{ review.dependencies.join('、') }}</span></div>
      <div v-if="isPackage" class="draft-meta-row"><span class="draft-key">来源</span><span class="draft-val">{{ provenance.origin === 'model_generated' ? '模型生成（未验证部分见下方）' : '来源适配' }}</span></div>
      <div v-for="link in sourceLinks" :key="link" class="draft-meta-row"><span class="draft-key">链接</span><a class="draft-val draft-link" :href="link" target="_blank" rel="noopener noreferrer">{{ link }}</a></div>
      <div v-if="provenance.author" class="draft-meta-row"><span class="draft-key">作者</span><span class="draft-val">{{ provenance.author }}</span></div>
      <div v-if="provenance.commit" class="draft-meta-row"><span class="draft-key">版本</span><code class="draft-val">{{ provenance.commit }}</code></div>
      <div v-if="provenance.fetched_at" class="draft-meta-row"><span class="draft-key">获取</span><span class="draft-val">{{ provenance.fetched_at }}</span></div>
      <div v-if="provenance.adaptation" class="draft-meta-row"><span class="draft-key">适配</span><span class="draft-val">{{ provenance.adaptation }}</span></div>
    </div>

    <ul v-if="review.reasons?.length" class="draft-hint"><li v-for="reason in review.reasons" :key="reason">{{ reason }}</li></ul>
    <p v-if="provenance.unverified" class="draft-hint">未验证：{{ Array.isArray(provenance.unverified) ? provenance.unverified.join('；') : provenance.unverified }}</p>
    <p v-if="isPackage" class="draft-hint">包声明不是工具授权；安装不会执行脚本、hooks 或依赖安装命令。</p>
    <details v-if="draft.dependency_links?.length" class="draft-files" open>
      <summary>关联工具依赖 · {{ draft.dependency_links.length }} 项</summary>
      <p class="draft-hint">选择只绑定所展示的个人工具版本；工具须另行确认、配置与测试。全部依赖就绪并重新确认后才能启用 Skill，不代表实际需求已验收。</p>
      <button v-if="canDownload" class="resource-button" :disabled="busy" @click="loadTools">刷新可选个人工具</button>
      <button v-if="canDownload" class="resource-button" :disabled="busy" @click="refresh">刷新依赖状态</button>
      <div v-for="link in draft.dependency_links" :key="link.requirement" class="dependency-item">
        <strong>{{ link.requirement }}</strong>
        <p v-if="link.tool_id" class="draft-hash">选定 {{ link.tool_id }} · 版本 {{ link.version }} · {{ link.ready ? '工具样例已通过' : '尚未就绪或版本已变更' }}</p>
        <p v-if="link.error" class="draft-hint">{{ link.error }}</p>
        <div v-if="canDownload" class="dependency-actions">
          <select v-model="toolChoices[link.requirement]" :disabled="busy" :aria-label="`为 ${link.requirement} 选择个人工具`">
            <option value="">选择个人工具版本</option>
            <option v-for="tool in availableTools" :key="tool.id" :value="tool.id">{{ tool.name }} · {{ tool.status }} · {{ tool.version.slice(0, 12) }}</option>
          </select>
          <button :disabled="busy || !toolChoices[link.requirement]" @click="bindDependency(link, availableTools.find(t => t.id === toolChoices[link.requirement]))">选定此版本</button>
          <button v-if="link.tool_id" :disabled="busy" @click="bindDependency(link, null)">移除选择</button>
          <button v-if="(!link.draft || link.draft.tool_missing || ['cancelled', 'expired', 'failed'].includes(link.draft.status)) && !link.tool_id" :disabled="busy || link.status === 'discovering'" @click="discoverDependency(link)">{{ link.status === 'discovering' ? '正在发现关联工具…' : '联网查找并生成工具草稿' }}</button>
        </div>
        <ToolDraftCard v-if="link.draft" :draft="link.draft" :readonly="!canDownload" @update="refresh" />
        <button v-if="canDownload && link.draft?.tool && !link.tool_id" class="resource-button" :disabled="busy" @click="bindDependency(link, link.draft.tool)">选定关联工具的此版本</button>
      </div>
      <button v-if="dependencyController" class="resource-button" @click="dependencyController.abort()">取消依赖发现</button>
      <p class="draft-hash">依赖确认指纹：{{ draft.dependency_hash }}</p>
      <button v-if="draft.status === 'imported'" class="resource-button" :disabled="busy" @click="submit(true)">确认依赖并重新评审（就绪则启用）</button>
    </details>
    <details class="draft-files">
      <summary>{{ isPackage ? '展开完整 SKILL.md 与资源清单' : '展开完整指令内容' }}</summary>
      <pre class="draft-content">{{ draft.content }}</pre>
      <ul v-if="draft.files?.length" class="resource-list">
        <li v-for="file in draft.files" :key="file.path">
          <button type="button" class="resource-button" :disabled="loadingFile || !canDownload" @click="previewResource(file.path)">{{ file.path }}</button>
          <small>{{ file.size }} B · SHA256 {{ file.hash }}</small>
        </li>
      </ul>
      <details v-if="resource" open><summary>{{ resource.path }}</summary><pre class="draft-content">{{ resource.content ?? '二进制资源不在线渲染，请下载完整包查看。' }}</pre></details>
      <p v-if="draft.content_hash" class="draft-hash">确认指纹：{{ draft.content_hash }}</p>
      <button v-if="isPackage && canDownload" class="draft-cancel download-button" type="button" :disabled="downloading" @click="download">{{ downloading ? '准备下载…' : '下载完整包' }}</button>
    </details>

    <p v-if="draft.status === 'pending'" class="draft-hint">
      尚未导入。点击「确认导入」后系统会自动写入你的个人 Skill 配置，无需手动操作；
      <small>有效期至 {{ expiresText }}</small>
    </p>
    <p v-if="draft.status === 'importing'" class="draft-hint">正在自动导入个人 Skill 配置…</p>
    <p v-if="draft.status === 'imported'" class="draft-success">
      已保存个人 Skill《{{ draft.name }}》。{{ review.status === 'needs_config' ? '依赖尚未就绪，未启用。' : '可在配置页查看当前启用状态。' }}
      <router-link to="/skills" class="draft-link">前往 Skill 配置查看</router-link>
    </p>
    <p v-if="draft.status === 'cancelled'" class="draft-muted">已取消，未写入任何配置。如需沉淀可重新发起。</p>
    <p v-if="draft.status === 'expired'" class="draft-muted">草稿已过期（24 小时有效），请重新发起沉淀。</p>
    <p v-if="draft.status === 'failed' || error" class="draft-error" role="alert">{{ error || draft.error }}</p>

    <label v-if="draft.status === 'pending' && draft.same_name_skills?.length" class="draft-hint duplicate-confirm">
      <input v-model="allowSameName" type="checkbox">已有同名技能，保留原技能并另存一份（不会覆盖）
    </label>
    <div v-if="draft.status === 'pending'" class="draft-buttons">
      <button class="draft-confirm" :disabled="busy || (draft.same_name_skills?.length && !allowSameName)" @click="submit(true)">
        {{ busy ? '导入中…' : '确认导入' }}
      </button>
      <button class="draft-cancel" :disabled="busy" @click="submit(false)">取消</button>
    </div>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { cancelSkillDraft, confirmSkillDraft, getSkillDraft, downloadSkillDraft, getSkillDraftResource,
  listPersonalTools, bindSkillDependency, discoverSkillDependency } from '@/api'
import ToolDraftCard from './ToolDraftCard.vue'

const props = defineProps({ draft: { type: Object, required: true } })
const emit = defineEmits(['update'])
const busy = ref(false)
const error = ref('')
const allowSameName = ref(false)
const downloading = ref(false)
const loadingFile = ref(false)
const resource = ref(null)
const availableTools = ref([])
const toolChoices = ref({})
const dependencyController = ref(null)
const isPackage = computed(() => props.draft.kind === 'agent_skill')
const review = computed(() => props.draft.review || {})
const provenance = computed(() => props.draft.provenance || {})
const canDownload = computed(() => ['pending', 'imported'].includes(props.draft.status))
const reviewLabels = { ready: '基础检查通过', needs_config: '缺少依赖或配置，保持未启用', unsupported: '暂不支持', rejected: '安全拒绝' }
const sourceLinks = computed(() => {
  const source = provenance.value
  return [...new Set([source.url, ...(source.sources || [])].map(x => typeof x === 'string' ? x : x?.url).filter(x => {
    try { const u = new URL(x); return ['https:', 'http:'].includes(u.protocol) && !u.username && !u.password } catch { return false }
  }))]
})
watch(() => props.draft.content_hash, () => { resource.value = null; allowSameName.value = false })
const labels = {
  pending: '待确认', importing: '导入中', imported: '已导入',
  cancelled: '已取消', expired: '已过期', failed: '导入失败',
}
const expiresText = computed(() =>
  props.draft.expires_at ? new Date(props.draft.expires_at).toLocaleString() : '24 小时')

let disposed = false
let stateRevision = 0
const controller = new AbortController()

/** 挂载时回读服务端状态（刷新/重进会话后确认卡与后端保持一致） */
async function refresh() {
  if (disposed || ['cancelled', 'expired'].includes(props.draft.status)) return
  const revision = ++stateRevision
  try {
    const { data } = await getSkillDraft(props.draft.id, controller.signal)
    if (!disposed && revision === stateRevision) { emit('update', data); error.value = '' }
  } catch (err) {
    if (!disposed && revision === stateRevision && err.response?.status !== 404) {
      error.value = err.response?.data?.detail || '状态读取失败，可重试'
    }
  }
}

async function submit(confirmed) {
  if (busy.value || !canDownload.value) return
  ++stateRevision
  busy.value = true
  error.value = ''
  try {
    const { data } = confirmed
      ? await confirmSkillDraft(props.draft.id, props.draft.content_hash, allowSameName.value,
        props.draft.dependency_links?.length ? props.draft.dependency_hash : '')
      : await cancelSkillDraft(props.draft.id)
    if (!disposed) { ++stateRevision; emit('update', data) }
  } catch (err) {
    if (!disposed) {
      const message = err.response?.data?.detail || '请求失败，请刷新后重试'
      // 导入失败（如安全预审拦截）时回读最新状态，避免卡片停留在可点击态
      await refresh()
      error.value = message
    }
  } finally {
    busy.value = false
  }
}

async function loadTools() {
  try {
    const { data } = await listPersonalTools()
    if (!disposed) availableTools.value = data.tools || []
  } catch { if (!disposed) error.value = '个人工具列表读取失败' }
}

async function bindDependency(link, tool) {
  if (busy.value || !canDownload.value) return
  ++stateRevision
  busy.value = true
  error.value = ''
  try {
    const { data } = await bindSkillDependency(props.draft.id, {
      expected_hash: props.draft.content_hash, requirement: link.requirement,
      tool_id: tool?.id || '', version: tool?.version || '',
    })
    if (!disposed) { ++stateRevision; emit('update', data) }
  } catch (err) {
    if (!disposed) error.value = err.response?.data?.detail || '依赖版本选择失败，请刷新后重试'
  } finally { busy.value = false }
}

async function discoverDependency(link) {
  if (busy.value || !canDownload.value) return
  ++stateRevision
  busy.value = true
  error.value = ''
  dependencyController.value = new AbortController()
  const signal = dependencyController.value.signal
  try {
    const { data } = await discoverSkillDependency(props.draft.id, {
      expected_hash: props.draft.content_hash, requirement: link.requirement,
    }, signal)
    if (!disposed) { ++stateRevision; emit('update', data) }
  } catch (err) {
    if (!disposed) {
      await refresh()
      error.value = signal.aborted ? '已取消依赖发现，未自动安装工具' : err.response?.data?.detail || '依赖发现失败，未安装工具'
    }
  } finally { dependencyController.value = null; busy.value = false }
}

async function previewResource(path) {
  loadingFile.value = true
  const hash = props.draft.content_hash
  try {
    const { data } = await getSkillDraftResource(props.draft.id, path, hash, controller.signal)
    if (!disposed && hash === props.draft.content_hash) resource.value = data
  } catch (err) {
    if (!disposed) error.value = err.response?.data?.detail || '资源读取失败'
  } finally { loadingFile.value = false }
}

async function download() {
  downloading.value = true
  try {
    const { data } = await downloadSkillDraft(props.draft.id, props.draft.content_hash)
    if (disposed) return
    const url = URL.createObjectURL(data)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `${props.draft.slug || 'skill'}.zip`
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch { if (!disposed) error.value = '下载失败，请重新获取草稿后重试' }
  finally { downloading.value = false }
}

onMounted(refresh)
onBeforeUnmount(() => { disposed = true; controller.abort(); dependencyController.value?.abort() })
</script>

<style scoped>
.draft-card {
  margin: 10px 0 20px;
  padding: 14px 16px;
  border: 1px solid #e0cdb2;
  border-radius: 12px;
  background: #fffdf9;
  font-size: 13.5px;
  color: var(--color-text, #4a3f35);
  box-shadow: 0 1px 4px rgba(139, 115, 85, 0.08);
}
.draft-card.imported { border-color: #b5c9a8; background: #fbfdf8; }
.draft-card.cancelled, .draft-card.expired { opacity: 0.75; }
.draft-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  color: #8b7355;
}
.draft-title { letter-spacing: 0.3px; }
.draft-badge {
  margin-left: auto;
  padding: 2px 10px;
  font-size: 11px;
  font-weight: 600;
  border-radius: 999px;
  background: #f3e8d8;
  color: #a0763f;
}
.draft-badge.imported { background: #e4efdc; color: #5a7a4a; }
.draft-badge.failed { background: #f5e0e0; color: #a05555; }
.draft-badge.cancelled, .draft-badge.expired { background: #eceae6; color: #8a8a80; }
.draft-meta { margin: 10px 0 4px; }
.draft-meta-row { display: flex; gap: 8px; margin: 4px 0; line-height: 1.55; }
.draft-key {
  flex-shrink: 0;
  width: 40px;
  color: #a0937f;
  font-size: 12px;
}
.draft-val { word-break: break-word; min-width: 0; }
.draft-files { margin-top: 12px; }
.draft-files summary { cursor: pointer; color: #8b7355; margin-bottom: 8px; }
.resource-list { list-style: none; padding: 0; }
.resource-list li { margin: 8px 0; }
.resource-list small, .draft-hash { display: block; overflow-wrap: anywhere; font-size: 11px; color: #807363; }
.resource-button { border: 0; padding: 4px 0; background: none; color: #8b7355; cursor: pointer; overflow-wrap: anywhere; text-align: left; }
.duplicate-confirm { display: flex; align-items: center; gap: 8px; }
.dependency-item { padding: 10px 0; border-top: 1px solid #eadfce; overflow-wrap: anywhere; }
.dependency-actions { display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0; }
.dependency-actions select { min-width: 0; max-width: 100%; flex: 1 1 200px; }
.dependency-actions button, .dependency-actions select { border: 1px solid #d8c6a8; border-radius: 6px; background: #fffdf9; padding: 6px; color: #6b5031; }
.download-button { border-radius: 8px; padding: 6px 12px; margin-top: 8px; cursor: pointer; }
.draft-content-label {
  margin: 10px 0 4px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.5px;
  color: #a0937f;
}
.draft-content {
  margin: 0;
  padding: 10px 12px;
  max-height: 280px;
  overflow-y: auto;
  border: 1px solid #eadfce;
  border-radius: 8px;
  background: #faf6ee;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 12px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  color: #5c5145;
  scrollbar-width: thin;
  scrollbar-color: #d8c6a8 #faf6ee;
}
.draft-content::-webkit-scrollbar { width: 8px; }
.draft-content::-webkit-scrollbar-track { background: #faf6ee; border-radius: 8px; }
.draft-content::-webkit-scrollbar-thumb { background: #d8c6a8; border-radius: 8px; }
.draft-content::-webkit-scrollbar-thumb:hover { background: #c9b28c; }
.draft-hint { margin: 10px 0 0; font-size: 12.5px; color: #8a7a63; line-height: 1.6; }
.draft-hint small { color: #a0937f; }
.draft-success { margin: 10px 0 0; color: #5a7a4a; line-height: 1.6; }
.draft-link { color: #8b7355; font-weight: 600; text-decoration: none; margin-left: 6px; }
.draft-link:hover { text-decoration: underline; }
.draft-muted { margin: 10px 0 0; color: #8a8a80; }
.draft-error { margin: 10px 0 0; color: #b05555; }
.draft-buttons { display: flex; gap: 10px; margin-top: 12px; }
.draft-buttons button {
  padding: 7px 18px;
  border-radius: 8px;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.15s;
}
.draft-confirm {
  border: 1px solid #8b7355;
  background: #8b7355;
  color: #fff;
  font-weight: 600;
}
.draft-confirm:hover:not(:disabled) { background: #7a6448; }
.draft-cancel {
  border: 1px solid #d8c6a8;
  background: transparent;
  color: #8b7355;
}
.draft-cancel:hover:not(:disabled) { background: #f3e8d8; }
.draft-buttons button:disabled { opacity: 0.55; cursor: wait; }

@media (max-width: 768px) {
  .draft-card { margin-left: 0; padding: 12px; }
  .draft-content { max-height: 220px; }
}
</style>