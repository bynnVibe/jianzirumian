<template>
  <section class="personal-tools">
    <header><h2>我的工具 <small>{{ tools.length }}</small></h2><button :disabled="busy" @click="load">刷新</button></header>
    <p>在聊天输入 <code>@tool 功能描述或公开 API 文档链接</code> 添加。工具只属于当前用户；仅已启用且通过样例测试的版本可被调用。</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="!tools.length && !busy">暂无个人工具。</p>
    <article v-for="tool in tools" :key="tool.id">
      <div class="row"><strong>{{ tool.name }}</strong><span>{{ state(tool.status) }}</span>
        <button :disabled="busy" @click="open(tool)">{{ selected?.id === tool.id ? '收起详情' : '详情与配置' }}</button>
        <button :disabled="busy || (!tool.enabled && !['ready', 'disabled'].includes(tool.status))" @click="configure(tool, { enabled: !tool.enabled })">{{ tool.enabled ? '停用' : '启用' }}</button>
      </div>
      <template v-if="selected?.id === tool.id">
        <ToolSpecDetails :spec="selected.spec" :review="selected.review" />
        <p v-if="selected.blockers?.length" class="warning">待处理：{{ selected.blockers.map(blocker).join('；') }}</p>
        <label v-if="selected.review?.requires_terms"><input v-model="terms" type="checkbox" :disabled="busy" />仅用于非商业用途，遵守 Open-Meteo 免费条款、CC-BY 4.0 署名和限额</label>
        <button v-if="selected.review?.requires_terms" :disabled="busy" @click="configure(selected, { accept_terms: terms })">保存条款选择</button>
        <form v-if="selected.spec.config?.auth" @submit.prevent="saveCredential">
          <label>API 凭据（仅加密保存，不放入聊天）<input v-model="credential" type="password" autocomplete="new-password" maxlength="8192" :disabled="busy" /></label>
          <button :disabled="busy || !credential">保存凭据并要求重新测试</button>
          <span>{{ selected.credential_configured ? '已配置，无法回读原文' : '尚未配置' }}</span>
        </form>
        <div class="row"><button :disabled="busy || !!testing" @click="test(selected)">{{ testing === selected.id ? '测试中，可刷新记录并取消' : '执行只读样例测试' }}</button><button :disabled="busy" @click="details(selected.id)">刷新详情与执行状态</button></div>
        <details><summary>版本记录与测试结果</summary>
          <ul><li v-for="version in selected.versions || []" :key="version.version"><code>{{ version.version }}</code> · {{ version.created_at }}</li></ul>
          <ul><li v-for="test in selected.tests || []" :key="test.id">{{ state(test.status) }} · {{ test.created_at }}<pre>{{ test.report_json }}</pre></li></ul>
        </details>
        <details><summary>最近执行记录</summary>
          <ul><li v-for="run in executions" :key="run.id">{{ state(run.execution_status) }} · 数据 {{ run.validation_status }} · 需求 {{ run.requirement_status }} · {{ run.created_at }}
            <button v-if="['running', 'cancellation_requested', 'cleanup_failed'].includes(run.execution_status)" :disabled="busy" @click="cancelRun(run)">取消／核验终止</button>
          </li></ul>
        </details>
      </template>
    </article>
    <details v-if="auth.isAdmin" @toggle="loadReviews($event)">
      <summary>管理员：待审工具版本</summary>
      <p>须核对真实文档、固定目标与参数权限。脚本还需独立隔离 Worker；审批不代表样例通过。</p>
      <article v-for="item in reviews" :key="item.tool_id + item.version">
        <p>属主 {{ item.owner_id }} · 版本 <code>{{ item.version }}</code></p>
        <ToolSpecDetails :spec="item.spec" :review="item.review" />
        <details v-if="item.spec.executor === 'python'"><summary>本次审批绑定的隔离运行配置（变化后须重新审批）</summary><pre>{{ JSON.stringify(item.runtime_profile, null, 2) }}</pre><p v-if="!item.profile_hash">尚未配置固定镜像，审批后仍不能运行。</p></details>
        <label><input v-model="approvals[item.tool_id]" type="checkbox" :disabled="busy" />已核验上述版本只读无外部副作用；不执行文档安装指令或脚本</label>
        <button :disabled="busy || !approvals[item.tool_id]" @click="approve(item)">批准此版本（仍需属主测试）</button>
      </article>
    </details>
  </section>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { listPersonalTools, getPersonalTool, configurePersonalTool, setPersonalToolCredential, testPersonalTool,
  listPersonalToolExecutions, cancelPersonalToolExecution, listPersonalToolReviews, approvePersonalTool } from '@/api'
import ToolSpecDetails from './ToolSpecDetails.vue'
const auth = useAuthStore()
const tools = ref([]), selected = ref(null), executions = ref([]), reviews = ref([]), approvals = ref({})
const busy = ref(false), error = ref(''), notice = ref(''), terms = ref(false), credential = ref(''), testing = ref('')
const state = value => ({ ready: '已就绪', needs_config: '待配置或测试', testing: '测试中', disabled: '已停用', passed: '通过', failed: '失败', blocked: '被阻止', cancelled: '已取消', interrupted: '已中断', running: '执行中', completed: '执行结束', timeout: '超时', cancellation_requested: '取消待确认', cleanup_failed: '隔离清理未确认' }[value] || value)
const blocker = value => ({ admin_review_required: '管理员版本审批', noncommercial_terms_required: '确认非商业条款', credentials_required: '配置服务端主密钥及 API 凭据', sandbox_unavailable: '隔离 Worker 尚不可用，禁止主机执行', version_changed: '版本变化，需重新确认', invalid_stored_spec: '已存工具声明损坏，已阻止执行，请重新创建并确认' }[value] || value)
async function act(fn) {
  busy.value = true; error.value = ''; notice.value = ''
  try { await fn() }
  catch (err) { error.value = typeof err.response?.data?.detail === 'string' ? err.response.data.detail : '请求未完成，请刷新状态核验，不要假定操作已成功。' }
  finally { busy.value = false }
}
async function load() { await act(async () => { tools.value = (await listPersonalTools()).data.tools || [] }) }
async function readDetails(id) {
  selected.value = (await getPersonalTool(id)).data
  terms.value = !!selected.value.terms_accepted
  executions.value = (await listPersonalToolExecutions(id)).data.executions || []
  const idx = tools.value.findIndex(t => t.id === id)
  if (idx >= 0) tools.value[idx] = selected.value
}
async function details(id) { await act(() => readDetails(id)) }
async function open(tool) {
  credential.value = ''
  if (selected.value?.id === tool.id) { selected.value = null; return }
  await details(tool.id)
}
async function configure(tool, body) { await act(async () => { await configurePersonalTool(tool.id, body); await readDetails(tool.id) }) }
async function saveCredential() {
  await act(async () => {
    try { await setPersonalToolCredential(selected.value.id, credential.value) }
    finally { credential.value = '' }
    await readDetails(selected.value.id)
    notice.value = '凭据已加密保存，请重新运行本版本样例测试。'
  })
}
async function test(tool) {
  if (testing.value) return
  testing.value = tool.id; error.value = ''; notice.value = '测试进行中，可刷新执行记录并取消正在运行的任务。'
  try {
    await testPersonalTool(tool.id)
    if (selected.value?.id === tool.id) await readDetails(tool.id)
    notice.value = '测试请求已结束，请以版本测试结果为准。'
  } catch (err) {
    error.value = typeof err.response?.data?.detail === 'string' ? err.response.data.detail : '测试连接结束，请刷新记录核验最终状态。'
  } finally { testing.value = '' }
}
async function cancelRun(run) {
  await act(async () => { await cancelPersonalToolExecution(run.id); notice.value = '已请求取消；请刷新执行状态确认终止。'; await readDetails(selected.value.id) })
}
async function fetchReviews() {
  const rows = (await listPersonalToolReviews()).data.reviews || []
  reviews.value = rows.map(r => ({ ...r, spec: JSON.parse(r.spec_json), review: JSON.parse(r.review_json) }))
  approvals.value = {}
}
async function loadReviews(event) { if (event.target.open) await act(fetchReviews) }
async function approve(item) { await act(async () => { await approvePersonalTool(item.tool_id, item.owner_id, item.version, item.profile_hash); await fetchReviews(); notice.value = '版本已批准，仍须属主完成配置和只读样例测试。' }) }
onMounted(load)
</script>

<style scoped>
.personal-tools { margin: 24px 0; padding: 20px; background: #fffaf3; border: 1px solid #e0d6c7; border-radius: 12px; color: #53493c; font-size: 13px; line-height: 1.7; min-width: 0; }
header, .row { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; }
header { justify-content: space-between; }
h2 { font-size: 17px; margin: 0; } small { color: #91816b; }
article { padding: 12px 0; border-top: 1px solid #e8dfd1; overflow-wrap: anywhere; }
.row { margin: 8px 0; }
button { padding: 6px 10px; border-radius: 7px; background: #fffefb; border: 1px solid #d6c5aa; color: #705335; cursor: pointer; }
button:disabled { opacity: .55; cursor: not-allowed; }
label { display: flex; align-items: flex-start; gap: 7px; margin: 10px 0; }
form { margin: 12px 0; padding: 10px; background: #f4f2ed; } form label { display: block; }
input[type=password] { display: block; max-width: 100%; width: 320px; padding: 7px; border: 1px solid #d6c5aa; border-radius: 5px; }
summary { cursor: pointer; margin: 10px 0; } pre { max-width: 100%; overflow: auto; }
.error { color: #a7352b; } .warning { color: #95611f; }
@media (max-width: 600px) { .personal-tools { padding: 12px; } button { min-height: 38px; } }
</style>
