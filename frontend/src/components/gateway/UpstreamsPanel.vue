<template>
  <div class="panel">
    <div class="panel-head">
      <div class="panel-intro">
        <h2>上游管理 · 模型服务端点</h2>
        <p>
          上游 = 一个可出网的模型服务端点，自身持有<strong>协议 / Base URL / API Key / 成本层级 /
          模型清单 / 权重 / 健康度 / 延迟</strong>。像添加普通模型一样把模型加进上游即可，所有配置都在具体上游里完成。
          <code>免费</code>层级且留空 Base URL/Key 时自动回退系统 OpenRouter 配置。
        </p>
      </div>
      <div class="head-actions">
        <button class="btn btn-ghost" @click="openImport">从 OpenRouter 导入免费模型</button>
        <button class="btn btn-primary" @click="openCreate">+ 添加上游</button>
      </div>
    </div>

    <p v-if="errorMsg" class="err-tip">{{ errorMsg }}</p>
    <div v-if="loading" class="mini-loading">加载上游中…</div>

    <div v-else-if="!upstreams.length" class="mini-empty">
      还没有上游。添加一个上游并填写模型列表后，即可在「路由规则」中被选用对外提供网关服务。
    </div>

    <template v-else>
      <div class="count-line">共 {{ upstreams.length }} 个上游</div>
      <table class="up-table">
        <thead>
          <tr>
            <th>名称</th><th>层级</th><th>状态</th><th class="num">权重</th>
            <th class="num">延迟</th><th class="num">模型数</th><th class="act">操作</th>
          </tr>
        </thead>
        <tbody v-for="u in upstreams" :key="u.id">
          <tr :class="{ 'row-disabled': !u.enabled }">
            <td>
              <div class="up-name">{{ u.name }}</div>
              <div class="up-url mono">{{ u.base_url || (u.tier === 'free' ? '系统 OpenRouter 回退' : '—') }}</div>
              <div v-if="u.last_error" class="up-err" :title="u.last_error">最近错误：{{ u.last_error }}</div>
            </td>
            <td><span class="tier-pill" :class="`t-${u.tier}`">{{ tierLabel(u.tier) }}</span></td>
            <td>
              <span class="health-pill" :class="`h-${u.health}`">{{ healthLabel(u.health) }}</span>
              <span v-if="!u.enabled" class="disabled-pill">已禁用</span>
            </td>
            <td class="num">
              <input class="weight-input" type="number" min="1" :value="u.weight"
                     :disabled="testing === u.id" @change="onWeightChange(u, $event)" />
            </td>
            <td class="num">{{ u.latency_ms ? u.latency_ms + 'ms' : '—' }}</td>
            <td class="num">
              <span class="model-count" :title="(u.models || []).join(', ')">{{ u.model_count || 0 }}</span>
            </td>
            <td class="act">
              <button class="btn btn-ghost sm" @click="onToggleEnabled(u)">{{ u.enabled ? '禁用' : '启用' }}</button>
              <button class="btn btn-ghost sm" @click="openEdit(u)">编辑</button>
              <button class="btn btn-ghost sm" @click="onClone(u)">克隆</button>
              <button class="btn btn-ghost sm" :disabled="testing === u.id" @click="onTest(u)">
                {{ testing === u.id ? '测试中…' : '测试' }}
              </button>
              <button v-if="u.health !== 'healthy'" class="btn btn-ghost sm" @click="onResetHealth(u)">重置健康</button>
              <button class="btn btn-danger sm" @click="onDelete(u)">删除</button>
            </td>
          </tr>
          <tr v-if="testResults[u.id]" class="test-row">
            <td colspan="7">
              <div class="test-banner" :class="testResults[u.id].success ? 'ok' : 'fail'">
                <div class="test-summary">{{ testResults[u.id].success ? '✓' : '✕' }} {{ testResults[u.id].message }}</div>
                <ul v-if="testResults[u.id].results && testResults[u.id].results.length" class="test-models">
                  <li v-for="r in testResults[u.id].results" :key="r.model" :class="r.success ? 'ok' : 'fail'">
                    <span class="t-mark">{{ r.success ? '✓' : '✕' }}</span>
                    <span class="t-model mono">{{ r.model }}</span>
                    <span class="t-msg">{{ r.message }}</span>
                  </li>
                </ul>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </template>

    <!-- 添加/编辑上游弹窗（图2） -->
    <div v-if="showModal" class="modal-mask">
      <div class="modal">
        <div class="modal-head">
          <h3 class="modal-title">{{ editing ? '编辑上游' : '添加上游' }}</h3>
          <button class="modal-close" title="关闭" @click="showModal = false">×</button>
        </div>
        <div class="form-grid">
          <label class="field span-2">
            <span class="field-label">名称 *</span>
            <input v-model.trim="form.name" class="field-input" placeholder="如 OpenRouter 免费池" />
          </label>
          <label class="field span-2">
            <span class="field-label">Base URL</span>
            <input v-model.trim="form.base_url" class="field-input"
                   placeholder="需带完整版本前缀，如 https://api.xxx.com/v1" />
          </label>
          <label class="field span-2">
            <span class="field-label">API Key</span>
            <input v-model.trim="form.api_key" type="password" class="field-input"
                   :placeholder="editing && form.has_api_key ? `已配置（${form.api_key_masked}），留空不修改` : '上游厂商密钥'" />
          </label>
          <label class="field">
            <span class="field-label">类型（协议）</span>
            <select v-model="form.protocol" class="field-input">
              <option v-for="p in protocols" :key="p" :value="p">{{ p }}</option>
            </select>
          </label>
          <label class="field">
            <span class="field-label">成本层级</span>
            <select v-model="form.tier" class="field-input">
              <option v-for="t in tiers" :key="t" :value="t">{{ tierLabel(t) }}</option>
            </select>
          </label>
          <label class="field">
            <span class="field-label">权重（负载均衡用）</span>
            <input v-model.number="form.weight" type="number" min="1" class="field-input" />
          </label>
          <label class="field checkbox">
            <input type="checkbox" v-model="form.enabled" />
            <span>启用该上游</span>
          </label>
          <div class="field span-2">
            <span class="field-label">模型列表（逗号分隔，如 glm4:9b, qwen2.5:7b）</span>
            <textarea v-model="form.modelsText" class="field-input" rows="3"
                      placeholder="glm4:9b, qwen2.5:7b"></textarea>
          </div>
          <label class="field span-2">
            <span class="field-label">备注</span>
            <input v-model.trim="form.remark" class="field-input" placeholder="可选" />
          </label>
        </div>
        <p v-if="formError" class="err-tip">{{ formError }}</p>
        <div class="modal-actions">
          <button class="btn btn-ghost" @click="showModal = false">取消</button>
          <button class="btn btn-primary" :disabled="saving" @click="onSave">{{ saving ? '保存中…' : '保存' }}</button>
        </div>
      </div>
    </div>

    <!-- OpenRouter 免费模型导入弹窗 -->
    <div v-if="showImport" class="modal-mask">
      <div class="modal">
        <div class="modal-head">
          <h3 class="modal-title">从 OpenRouter 导入免费模型</h3>
          <button class="modal-close" title="关闭" @click="showImport = false">×</button>
        </div>
        <div class="import-head">
          <input v-model.trim="importName" class="field-input" placeholder="新上游名称" style="flex:1" />
          <button class="btn btn-ghost sm" :disabled="importLoading" @click="loadFree">
            {{ importLoading ? '拉取中…' : '刷新列表' }}
          </button>
        </div>
        <p class="reg-hint">
          勾选的免费模型将并入一个 <strong>免费层级</strong> 上游；Base URL/Key 留空即回退系统 OpenRouter 配置。
        </p>
        <div v-if="!freeModels.length" class="mini-empty">暂无可导入的免费模型，点击「刷新列表」重试。</div>
        <div v-else class="check-list import-list">
          <label v-for="m in freeModels" :key="m" class="check-item">
            <input type="checkbox" :value="m" v-model="importSelected" />
            <span class="mono">{{ m }}</span>
          </label>
        </div>
        <p v-if="formError" class="err-tip">{{ formError }}</p>
        <div class="modal-actions">
          <button class="btn btn-ghost" @click="showImport = false">取消</button>
          <button class="btn btn-primary" :disabled="saving" @click="onImport">
            {{ saving ? '导入中…' : `导入 ${importSelected.length} 个模型` }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import {
  listGatewayUpstreams, createGatewayUpstream, updateGatewayUpstream,
  deleteGatewayUpstream, cloneGatewayUpstream, testGatewayUpstream,
  resetGatewayUpstreamHealth, listOpenRouterFreeModels,
} from '@/api'

const upstreams = ref([])
const protocols = ref(['openai'])
const tiers = ref(['paid', 'free', 'subscription'])
const loading = ref(true)
const errorMsg = ref('')
const testing = ref('')
const testResults = ref({})

const showModal = ref(false)
const editing = ref(null)
const saving = ref(false)
const formError = ref('')
const form = reactive({
  name: '', protocol: 'openai', base_url: '', api_key: '', tier: 'paid',
  modelsText: '', weight: 100, enabled: true, remark: '',
  has_api_key: false, api_key_masked: '',
})

// OpenRouter 导入
const showImport = ref(false)
const importLoading = ref(false)
const importName = ref('OpenRouter 免费模型')
const freeModels = ref([])
const importSelected = ref([])

function tierLabel(t) { return { paid: '付费', free: '免费', subscription: '订阅' }[t] || t }
function healthLabel(h) { return { healthy: '正常', degraded: '降级', down: '不可用' }[h] || h }

async function load() {
  loading.value = true
  errorMsg.value = ''
  try {
    const res = await listGatewayUpstreams()
    upstreams.value = res.data.upstreams || []
    protocols.value = res.data.protocols || ['openai']
    tiers.value = res.data.tiers || ['paid', 'free', 'subscription']
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '加载上游失败'
  } finally {
    loading.value = false
  }
}

function resetForm() {
  Object.assign(form, {
    name: '', protocol: 'openai', base_url: '', api_key: '', tier: 'paid',
    modelsText: '', weight: 100, enabled: true, remark: '',
    has_api_key: false, api_key_masked: '',
  })
}
function openCreate() {
  editing.value = null; resetForm(); formError.value = ''
  showModal.value = true
}
function openEdit(u) {
  editing.value = u.id
  Object.assign(form, {
    name: u.name, protocol: u.protocol || 'openai', base_url: u.base_url || '',
    api_key: '', tier: u.tier || 'paid', modelsText: (u.models || []).join(', '),
    weight: u.weight || 100, enabled: !!u.enabled, remark: u.remark || '',
    has_api_key: !!u.has_api_key, api_key_masked: u.api_key_masked || '',
  })
  formError.value = ''
  showModal.value = true
}

function buildPayload() {
  const models = form.modelsText.split(/[,，]/).map((s) => s.trim()).filter(Boolean)
  return {
    name: form.name, protocol: form.protocol, base_url: form.base_url,
    api_key: form.api_key, tier: form.tier, models,
    weight: Math.max(1, Number(form.weight) || 100), enabled: form.enabled, remark: form.remark,
  }
}

async function onSave() {
  formError.value = ''
  const payload = buildPayload()
  if (!payload.api_key) delete payload.api_key // 留空表示不修改
  saving.value = true
  try {
    if (editing.value) await updateGatewayUpstream(editing.value, payload)
    else await createGatewayUpstream(payload)
    showModal.value = false
    await load()
  } catch (e) {
    formError.value = e.response?.data?.detail || '保存失败'
  } finally {
    saving.value = false
  }
}

async function onWeightChange(u, ev) {
  const w = Math.max(1, Number(ev.target.value) || 1)
  try {
    await updateGatewayUpstream(u.id, { weight: w })
    await load()
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '权重更新失败'
  }
}

async function onToggleEnabled(u) {
  try { await updateGatewayUpstream(u.id, { enabled: !u.enabled }); await load() }
  catch (e) { errorMsg.value = e.response?.data?.detail || '操作失败' }
}

async function onClone(u) {
  try { await cloneGatewayUpstream(u.id); await load() }
  catch (e) { errorMsg.value = e.response?.data?.detail || '克隆失败' }
}

async function onDelete(u) {
  if (!confirm(`确认删除上游「${u.name}」？相关路由规则将自动移除该上游。`)) return
  try { await deleteGatewayUpstream(u.id); await load() }
  catch (e) { errorMsg.value = e.response?.data?.detail || '删除失败' }
}

async function onTest(u) {
  testing.value = u.id
  try {
    const res = await testGatewayUpstream(u.id)
    testResults.value = { ...testResults.value, [u.id]: res.data }
    await load()
  } catch (e) {
    testResults.value = {
      ...testResults.value,
      [u.id]: { success: false, message: e.response?.data?.detail || '测试请求失败', results: [] },
    }
  } finally {
    testing.value = ''
  }
}

async function onResetHealth(u) {
  try { await resetGatewayUpstreamHealth(u.id); await load() }
  catch (e) { errorMsg.value = e.response?.data?.detail || '重置失败' }
}

function openImport() {
  formError.value = ''
  importSelected.value = []
  showImport.value = true
  if (!freeModels.value.length) loadFree()
}
async function loadFree() {
  importLoading.value = true
  formError.value = ''
  try {
    const res = await listOpenRouterFreeModels()
    freeModels.value = res.data.models || []
  } catch (e) {
    formError.value = e.response?.data?.detail || '拉取免费模型失败'
  } finally {
    importLoading.value = false
  }
}
async function onImport() {
  formError.value = ''
  if (!importSelected.value.length) { formError.value = '请至少勾选一个模型'; return }
  saving.value = true
  try {
    await createGatewayUpstream({
      name: importName.value || 'OpenRouter 免费模型',
      protocol: 'openai', base_url: '', api_key: '', tier: 'free',
      models: [...importSelected.value], weight: 100, enabled: true,
      remark: '从 OpenRouter 免费列表导入',
    })
    showImport.value = false
    await load()
  } catch (e) {
    formError.value = e.response?.data?.detail || '导入失败'
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px; flex-wrap: wrap; }
.panel-intro h2 { font-size: 16px; font-weight: 700; margin-bottom: 4px; }
.panel-intro p { font-size: 12.5px; color: var(--color-text-secondary); line-height: 1.6; max-width: 780px; }
.panel-intro code { background: #f4ece0; padding: 1px 5px; border-radius: 4px; font-size: 11.5px; }
.head-actions { display: flex; gap: 8px; flex-wrap: wrap; }

.count-line { font-size: 12.5px; color: var(--color-text-secondary); margin-bottom: 8px; }
.up-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.up-table th { text-align: left; padding: 9px 10px; color: var(--color-text-secondary); font-weight: 600; border-bottom: 1px solid var(--color-border); font-size: 12px; }
.up-table td { padding: 11px 10px; border-bottom: 1px solid #f4efe8; vertical-align: middle; }
.up-table th.num, .up-table td.num { text-align: right; }
.up-table th.act, .up-table td.act { text-align: right; white-space: nowrap; }
.up-table td.act .btn { margin-left: 4px; }
.row-disabled { opacity: 0.55; }
.up-name { font-weight: 700; }
.up-url { font-size: 11px; color: var(--color-text-secondary); word-break: break-all; margin-top: 2px; }
.up-err { font-size: 11px; color: #c0392b; margin-top: 3px; max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mono { font-family: 'SFMono-Regular', Consolas, monospace; }

.tier-pill { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 8px; }
.t-paid { background: #e8f0fb; color: #3a6ea5; }
.t-free { background: #e4f4e8; color: #3d8b52; }
.t-subscription { background: #f0e8fb; color: #7a4aa5; }
.health-pill { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 8px; }
.h-healthy { background: #e4f4e8; color: #3d8b52; }
.h-degraded { background: #fdf0d9; color: #b8802a; }
.h-down { background: #fbe4e3; color: #c0392b; }
.disabled-pill { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 8px; background: #eee9e2; color: #8a7e72; margin-left: 4px; }
.weight-input { width: 66px; padding: 4px 7px; border: 1px solid var(--color-border); border-radius: 6px; font-size: 12px; text-align: right; background: #fff; }
.weight-input:focus { border-color: var(--color-primary); outline: none; }
.model-count { font-weight: 700; }

.test-row td { padding: 0 10px 12px; background: #fbf8f3; }
.test-banner { font-size: 12.5px; padding: 9px 12px; border-radius: 8px; word-break: break-all; }
.test-banner.ok { color: #2f6b40; background: #e4f4e8; }
.test-banner.fail { color: #a83226; background: #fbe4e3; }
.test-summary { font-weight: 700; }
.test-models { list-style: none; margin: 6px 0 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.test-models li { display: flex; align-items: baseline; gap: 7px; font-size: 12px; }
.test-models .t-mark { font-weight: 700; flex: none; }
.test-models li.fail .t-mark, .test-models li.fail .t-msg { color: #a83226; }
.test-models .t-model { flex: none; font-weight: 600; }
.test-models .t-msg { opacity: .85; word-break: break-all; }

.reg-hint { font-size: 12px; color: #b8802a; background: #fdf0d9; padding: 8px 10px; border-radius: 6px; margin: 10px 0; }
.import-head { display: flex; gap: 8px; align-items: center; margin-bottom: 6px; }
.check-list { display: flex; flex-wrap: wrap; gap: 6px; max-height: 220px; overflow-y: auto; border: 1px solid var(--color-border); border-radius: 8px; padding: 8px; background: #fff; }
.check-item { display: flex; align-items: center; gap: 5px; font-size: 12px; padding: 3px 8px; border-radius: 6px; background: #f7f2ea; cursor: pointer; }
.check-item input { cursor: pointer; }

/* 弹窗 */
.modal-mask { position: fixed; inset: 0; background: rgba(45,42,36,0.35); display: flex; align-items: center; justify-content: center; z-index: 100; padding: 20px; }
.modal { background: #fffdfb; border-radius: 14px; padding: 22px 24px; width: 100%; max-width: 580px; max-height: 88vh; overflow-y: auto; box-shadow: 0 8px 30px rgba(0,0,0,0.18); }
.modal-title { font-size: 16px; font-weight: 700; margin-bottom: 0; }
.modal-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 16px; }
.modal-close { background: none; border: none; font-size: 22px; line-height: 1; cursor: pointer; color: var(--color-text-secondary); padding: 0 4px; }
.modal-close:hover { color: var(--color-primary); }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px 18px; }
.form-grid .span-2 { grid-column: span 2; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field-label { font-size: 12px; color: var(--color-text-secondary); font-weight: 600; }
.field-input { padding: 8px 11px; border: 1px solid var(--color-border); border-radius: 8px; font-size: 13px; background: #fff; color: var(--color-text); outline: none; }
.field-input:focus { border-color: var(--color-primary); }
textarea.field-input { resize: vertical; font-size: 12px; font-family: 'SFMono-Regular', Consolas, monospace; }
.field.checkbox { flex-direction: row; align-items: center; gap: 8px; font-size: 13px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 20px; }

/* 按钮 */
.btn { padding: 8px 16px; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer; border: 1px solid transparent; transition: all 0.15s; white-space: nowrap; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn.sm { padding: 5px 12px; font-size: 12px; }
.btn-primary { background: var(--color-primary); color: #fff; }
.btn-primary:hover:not(:disabled) { background: #b87a3f; }
.btn-danger { background: #fbe4e3; color: #c0392b; border-color: #f0c9c6; }
.btn-danger:hover:not(:disabled) { background: #f6d3d0; }
.btn-ghost { background: transparent; border-color: var(--color-border); color: var(--color-text); }
.btn-ghost:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
.err-tip { color: #d9534f; font-size: 12.5px; margin-bottom: 10px; }
.mini-loading, .mini-empty { color: var(--color-text-secondary); font-size: 13px; padding: 32px; text-align: center; }
</style>
