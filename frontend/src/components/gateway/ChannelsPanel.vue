<template>
  <div class="panel">
    <div class="panel-head">
      <div class="panel-intro">
        <h2>上游渠道 · 负载均衡与故障切换</h2>
        <p>
          按<strong>优先级</strong>分层（高优先级先选），同层内按<strong>权重</strong>加权随机；某渠道失败自动切换到下一候选，
          连续失败 ≥3 次熔断为 <code>down</code>（仍作兜底）。真实上游 Key 仅存于此，不分发给下游。
        </p>
      </div>
      <button class="btn btn-primary" @click="openCreate">+ 新增渠道</button>
    </div>

    <p v-if="errorMsg" class="err-tip">{{ errorMsg }}</p>
    <div v-if="loading" class="mini-loading">加载渠道中…</div>

    <div v-else-if="!channels.length" class="mini-empty">
      还没有渠道。新增一个上游渠道（OpenAI / Anthropic / Ollama 兼容）后即可对外提供网关服务。
    </div>

    <div v-else class="channel-list">
      <div v-for="c in channels" :key="c.id" class="channel-card">
        <div class="ch-top">
          <div class="ch-title">
            <span class="ch-name">{{ c.name }}</span>
            <span class="proto-pill">{{ c.protocol }}</span>
            <span class="health-pill" :class="`h-${c.health}`">{{ healthLabel(c.health) }}</span>
            <span v-if="!c.enabled" class="disabled-pill">已停用</span>
          </div>
          <div class="ch-actions">
            <button class="btn btn-ghost sm" :disabled="testing === c.id" @click="onTest(c)">
              {{ testing === c.id ? '测试中…' : '连通测试' }}
            </button>
            <button v-if="c.health !== 'healthy'" class="btn btn-ghost sm" @click="onResetHealth(c)">重置健康</button>
            <button class="btn btn-ghost sm" @click="openEdit(c)">编辑</button>
            <button class="btn btn-danger sm" @click="onDelete(c)">删除</button>
          </div>
        </div>
        <div class="ch-url mono">{{ c.base_url }}</div>
        <div class="ch-stats">
          <span>优先级 <strong>{{ c.priority }}</strong></span>
          <span>权重 <strong>{{ c.weight }}</strong></span>
          <span>成功 <strong class="ok">{{ c.success_count }}</strong></span>
          <span>失败 <strong class="err">{{ c.fail_count }}</strong></span>
          <span>ApiKey <strong class="mono">{{ c.api_key_masked || '（未设置）' }}</strong></span>
        </div>
        <div class="ch-models">
          <span class="models-label">模型：</span>
          <template v-if="c.models?.length">
            <span v-for="m in c.models" :key="m" class="model-chip">{{ m }}</span>
          </template>
          <span v-else class="model-wild">通配（服务所有请求模型）</span>
          <template v-if="Object.keys(c.model_mapping || {}).length">
            <span class="map-chip" v-for="(v, k) in c.model_mapping" :key="k">{{ k }} → {{ v }}</span>
          </template>
        </div>
        <div v-if="c.last_error" class="ch-error">最近错误：{{ c.last_error }}</div>
        <div v-if="testResult[c.id]" class="ch-test" :class="testResult[c.id].success ? 'ok' : 'fail'">
          {{ testResult[c.id].success ? '✓' : '✕' }} HTTP {{ testResult[c.id].status_code }} · {{ testResult[c.id].url }} · {{ testResult[c.id].detail }}
        </div>
      </div>
    </div>

    <!-- 渠道表单弹窗 -->
    <div v-if="showModal" class="modal-mask" @click.self="showModal = false">
      <div class="modal">
        <h3 class="modal-title">{{ editing ? '编辑渠道' : '新增渠道' }}</h3>
        <div class="form-grid">
          <label class="field">
            <span class="field-label">渠道名称 *</span>
            <input v-model.trim="form.name" class="field-input" placeholder="如 OpenAI 主渠道" />
          </label>
          <label class="field">
            <span class="field-label">协议 *</span>
            <select v-model="form.protocol" class="field-input">
              <option value="openai">openai（兼容 /v1）</option>
              <option value="anthropic">anthropic（/v1/messages）</option>
              <option value="ollama">ollama（/api/chat）</option>
            </select>
          </label>
          <label class="field span-2">
            <span class="field-label">Base URL *</span>
            <input v-model.trim="form.base_url" class="field-input mono" placeholder="https://api.openai.com/v1" />
          </label>
          <label class="field span-2">
            <span class="field-label">上游 API Key {{ editing ? '（留空表示不修改）' : '' }}</span>
            <input v-model.trim="form.api_key" class="field-input mono" type="password" placeholder="sk-..." />
          </label>
          <label class="field span-2">
            <span class="field-label">服务模型（逗号分隔，留空=通配所有模型）</span>
            <input v-model.trim="form.modelsText" class="field-input mono" placeholder="gpt-4o-mini, gpt-3.5-turbo" />
          </label>
          <label class="field span-2">
            <span class="field-label">模型映射 JSON（请求名 → 上游名，可留空 {}）</span>
            <textarea v-model.trim="form.mappingText" class="field-input mono" rows="2" placeholder='{"gpt-4o":"gpt-4o-2024-08-06"}'></textarea>
          </label>
          <label class="field">
            <span class="field-label">优先级（越大越先选）</span>
            <input v-model.number="form.priority" type="number" class="field-input" />
          </label>
          <label class="field">
            <span class="field-label">权重（同层加权随机）</span>
            <input v-model.number="form.weight" type="number" min="1" class="field-input" />
          </label>
          <label class="field checkbox span-2">
            <input type="checkbox" v-model="form.enabled" />
            <span>启用该渠道（参与负载均衡）</span>
          </label>
        </div>
        <p v-if="formError" class="err-tip">{{ formError }}</p>
        <div class="modal-actions">
          <button class="btn btn-ghost" @click="showModal = false">取消</button>
          <button class="btn btn-primary" :disabled="saving" @click="onSave">{{ saving ? '保存中…' : '保存' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import {
  listGatewayChannels, createGatewayChannel, updateGatewayChannel,
  deleteGatewayChannel, testGatewayChannel, resetGatewayChannelHealth,
} from '@/api'

const channels = ref([])
const loading = ref(true)
const errorMsg = ref('')
const testing = ref('')
const testResult = reactive({})

const showModal = ref(false)
const editing = ref(null)
const saving = ref(false)
const formError = ref('')
const form = reactive({
  name: '', protocol: 'openai', base_url: '', api_key: '',
  modelsText: '', mappingText: '', priority: 0, weight: 1, enabled: true,
})

function healthLabel(h) { return { healthy: '健康', degraded: '降级', down: '熔断' }[h] || h }

async function load() {
  loading.value = true
  try {
    const res = await listGatewayChannels()
    channels.value = res.data.channels || []
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '加载渠道失败'
  } finally {
    loading.value = false
  }
}

function resetForm() {
  Object.assign(form, {
    name: '', protocol: 'openai', base_url: '', api_key: '',
    modelsText: '', mappingText: '', priority: 0, weight: 1, enabled: true,
  })
}
function openCreate() { editing.value = null; resetForm(); formError.value = ''; showModal.value = true }
function openEdit(c) {
  editing.value = c.id
  Object.assign(form, {
    name: c.name, protocol: c.protocol, base_url: c.base_url, api_key: '',
    modelsText: (c.models || []).join(', '),
    mappingText: Object.keys(c.model_mapping || {}).length ? JSON.stringify(c.model_mapping, null, 2) : '',
    priority: c.priority, weight: c.weight, enabled: c.enabled,
  })
  formError.value = ''
  showModal.value = true
}

function buildPayload() {
  const models = form.modelsText.split(',').map((s) => s.trim()).filter(Boolean)
  let mapping = {}
  if (form.mappingText.trim()) {
    mapping = JSON.parse(form.mappingText)
    if (typeof mapping !== 'object' || Array.isArray(mapping)) throw new Error('模型映射必须是 JSON 对象')
  }
  const payload = {
    name: form.name, protocol: form.protocol, base_url: form.base_url,
    models, model_mapping: mapping,
    priority: Number(form.priority) || 0, weight: Math.max(1, Number(form.weight) || 1),
    enabled: form.enabled,
  }
  if (form.api_key.trim()) payload.api_key = form.api_key.trim()
  return payload
}

async function onSave() {
  formError.value = ''
  let payload
  try { payload = buildPayload() } catch (e) { formError.value = e.message || '表单格式有误'; return }
  saving.value = true
  try {
    if (editing.value) await updateGatewayChannel(editing.value, payload)
    else await createGatewayChannel(payload)
    showModal.value = false
    await load()
  } catch (e) {
    formError.value = e.response?.data?.detail || '保存失败'
  } finally {
    saving.value = false
  }
}

async function onDelete(c) {
  if (!confirm(`确认删除渠道「${c.name}」？`)) return
  try { await deleteGatewayChannel(c.id); await load() }
  catch (e) { errorMsg.value = e.response?.data?.detail || '删除失败' }
}

async function onTest(c) {
  testing.value = c.id
  delete testResult[c.id]
  try {
    const res = await testGatewayChannel(c.id)
    testResult[c.id] = res.data
    await load()
  } catch (e) {
    testResult[c.id] = { success: false, status_code: 0, url: '', detail: e.response?.data?.detail || '测试请求失败' }
  } finally {
    testing.value = ''
  }
}

async function onResetHealth(c) {
  try { await resetGatewayChannelHealth(c.id); await load() }
  catch (e) { errorMsg.value = e.response?.data?.detail || '重置失败' }
}

onMounted(load)
</script>

<style scoped>
.panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px; flex-wrap: wrap; }
.panel-intro h2 { font-size: 16px; font-weight: 700; margin-bottom: 4px; }
.panel-intro p { font-size: 12.5px; color: var(--color-text-secondary); line-height: 1.6; max-width: 760px; }
.panel-intro code { background: #f4ece0; padding: 1px 5px; border-radius: 4px; font-size: 11.5px; }

.channel-list { display: flex; flex-direction: column; gap: 14px; }
.channel-card { background: #fffdfb; border: 1px solid var(--color-border); border-radius: 12px; padding: 16px 18px; box-shadow: 0 1px 3px rgba(120,90,50,0.04); }
.ch-top { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.ch-title { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ch-name { font-size: 15px; font-weight: 700; }
.ch-actions { display: flex; gap: 6px; flex-wrap: wrap; }
.proto-pill { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 8px; background: #eef2f7; color: #4a6fa5; }
.health-pill { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 8px; }
.h-healthy { background: #e4f4e8; color: #3d8b52; }
.h-degraded { background: #fdf0d9; color: #b8802a; }
.h-down { background: #fbe4e3; color: #c0392b; }
.disabled-pill { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 8px; background: #eee9e2; color: #8a7e72; }
.ch-url { font-size: 12px; color: var(--color-text-secondary); margin: 8px 0; word-break: break-all; }
.mono { font-family: 'SFMono-Regular', Consolas, monospace; }
.ch-stats { display: flex; gap: 18px; font-size: 12px; color: var(--color-text-secondary); flex-wrap: wrap; }
.ch-stats strong { color: var(--color-text); }
.ch-stats strong.ok { color: #3d8b52; }
.ch-stats strong.err { color: #c0392b; }
.ch-models { margin-top: 10px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; font-size: 12px; }
.models-label { color: var(--color-text-secondary); }
.model-chip { background: var(--color-primary-light); color: #a5703a; padding: 2px 8px; border-radius: 8px; font-size: 11px; }
.model-wild { color: var(--color-text-secondary); font-style: italic; }
.map-chip { background: #f4ece0; color: #8a6a3f; padding: 2px 8px; border-radius: 8px; font-size: 11px; font-family: 'SFMono-Regular', Consolas, monospace; }
.ch-error { margin-top: 8px; font-size: 11.5px; color: #c0392b; background: #fbe4e3; padding: 6px 10px; border-radius: 6px; }
.ch-test { margin-top: 8px; font-size: 11.5px; padding: 6px 10px; border-radius: 6px; word-break: break-all; }
.ch-test.ok { color: #2f6b40; background: #e4f4e8; }
.ch-test.fail { color: #a83226; background: #fbe4e3; }

/* 弹窗 */
.modal-mask { position: fixed; inset: 0; background: rgba(45,42,36,0.35); display: flex; align-items: center; justify-content: center; z-index: 100; padding: 20px; }
.modal { background: #fffdfb; border-radius: 14px; padding: 22px 24px; width: 100%; max-width: 560px; max-height: 88vh; overflow-y: auto; box-shadow: 0 8px 30px rgba(0,0,0,0.18); }
.modal-title { font-size: 16px; font-weight: 700; margin-bottom: 16px; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px 18px; }
.form-grid .span-2 { grid-column: span 2; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field-label { font-size: 12px; color: var(--color-text-secondary); font-weight: 600; }
.field-input { padding: 8px 11px; border: 1px solid var(--color-border); border-radius: 8px; font-size: 13px; background: #fff; color: var(--color-text); outline: none; }
.field-input:focus { border-color: var(--color-primary); }
textarea.field-input { resize: vertical; font-size: 12px; }
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
