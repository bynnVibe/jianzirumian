<template>
  <div class="panel">
    <div class="panel-head">
      <div class="panel-intro">
        <h2>本地密钥与配额管理</h2>
        <p>
          为每个下游用户 / 应用生成独立的本地访问密钥（<code>sk-jzrm-...</code>），可设置 Token 与请求次数配额、限定可访问模型。
          下游只持有本地密钥，真实上游 Key 始终留在渠道内。<strong>明文密钥仅在创建时展示一次</strong>，请及时复制保存。
        </p>
      </div>
      <button class="btn btn-primary" @click="openCreate">+ 生成密钥</button>
    </div>

    <p v-if="errorMsg" class="err-tip">{{ errorMsg }}</p>
    <div v-if="loading" class="mini-loading">加载密钥中…</div>
    <div v-else-if="!keys.length" class="mini-empty">还没有密钥。生成一个下游访问密钥以开始调用网关。</div>

    <table v-else class="key-table">
      <thead>
        <tr>
          <th>名称</th><th>密钥前缀</th><th class="num">Token 用量</th><th class="num">请求用量</th>
          <th>可访问模型</th><th>状态</th><th>最近使用</th><th class="ops">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="k in keys" :key="k.id">
          <td class="k-name">{{ k.name }}</td>
          <td class="mono k-prefix">{{ k.key_prefix }}…</td>
          <td class="num">
            <div class="quota-cell">
              <span>{{ formatNum(k.token_used) }}<template v-if="k.token_quota"> / {{ formatNum(k.token_quota) }}</template></span>
              <div v-if="k.token_quota" class="quota-bar"><div class="quota-fill" :style="{ width: pct(k.token_used, k.token_quota) + '%' }"></div></div>
            </div>
          </td>
          <td class="num">
            <div class="quota-cell">
              <span>{{ formatNum(k.request_count) }}<template v-if="k.request_quota"> / {{ formatNum(k.request_quota) }}</template></span>
              <div v-if="k.request_quota" class="quota-bar"><div class="quota-fill" :style="{ width: pct(k.request_count, k.request_quota) + '%' }"></div></div>
            </div>
          </td>
          <td>
            <span v-if="!k.allowed_models?.length" class="wild">不限</span>
            <span v-else class="model-chips"><span v-for="m in k.allowed_models" :key="m" class="model-chip">{{ m }}</span></span>
          </td>
          <td>
            <span v-if="k.expired" class="state-pill st-expired">已过期</span>
            <span v-else-if="!k.enabled" class="state-pill st-disabled">已禁用</span>
            <span v-else class="state-pill st-active">启用</span>
          </td>
          <td class="k-time">{{ k.last_used_at ? formatTime(k.last_used_at) : '从未使用' }}</td>
          <td class="ops">
            <button class="icon-btn" @click="openEdit(k)">编辑</button>
            <button class="icon-btn" @click="onReset(k)">重置用量</button>
            <button class="icon-btn" @click="onToggle(k)">{{ k.enabled ? '禁用' : '启用' }}</button>
            <button class="icon-btn danger" @click="onDelete(k)">删除</button>
          </td>
        </tr>
      </tbody>
    </table>

    <!-- 表单弹窗 -->
    <div v-if="showModal" class="modal-mask" @click.self="showModal = false">
      <div class="modal">
        <h3 class="modal-title">{{ editing ? '编辑密钥' : '生成密钥' }}</h3>
        <div class="form-grid">
          <label class="field span-2">
            <span class="field-label">密钥名称 *（下游用户/应用标识）</span>
            <input v-model.trim="form.name" class="field-input" placeholder="如 张三的桌面客户端" />
          </label>
          <label class="field">
            <span class="field-label">Token 配额（0 = 不限）</span>
            <input v-model.number="form.token_quota" type="number" min="0" class="field-input" />
          </label>
          <label class="field">
            <span class="field-label">请求次数配额（0 = 不限）</span>
            <input v-model.number="form.request_quota" type="number" min="0" class="field-input" />
          </label>
          <label class="field span-2">
            <span class="field-label">可访问模型（逗号分隔，留空 = 不限）</span>
            <input v-model.trim="form.modelsText" class="field-input mono" placeholder="gpt-4o-mini, gpt-3.5-turbo" list="gw-model-list" />
            <datalist id="gw-model-list">
              <option v-for="m in models" :key="m" :value="m" />
            </datalist>
          </label>
          <label class="field span-2">
            <span class="field-label">过期时间（留空 = 永不过期）</span>
            <input v-model="form.expires_at" type="datetime-local" class="field-input" />
          </label>
        </div>
        <p v-if="formError" class="err-tip">{{ formError }}</p>
        <div class="modal-actions">
          <button class="btn btn-ghost" @click="showModal = false">取消</button>
          <button class="btn btn-primary" :disabled="saving" @click="onSave">{{ saving ? '保存中…' : '保存' }}</button>
        </div>
      </div>
    </div>

    <!-- 明文密钥展示（仅一次） -->
    <div v-if="newPlainKey" class="modal-mask" @click.self="newPlainKey = ''">
      <div class="modal">
        <h3 class="modal-title">🔑 密钥已生成</h3>
        <p class="plain-warn">这是该密钥的<strong>唯一一次</strong>明文展示，数据库仅保存哈希值。请立即复制并妥善保管：</p>
        <div class="plain-key-box">
          <code class="plain-key mono">{{ newPlainKey }}</code>
          <button class="btn btn-ghost sm" @click="copyKey">{{ copied ? '已复制 ✓' : '复制' }}</button>
        </div>
        <p class="plain-hint">
          下游调用示例：<code>curl {{ origin }}/v1/chat/completions -H "Authorization: Bearer &lt;KEY&gt;" -d '{{ '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}' }}'</code>
        </p>
        <div class="modal-actions">
          <button class="btn btn-primary" @click="newPlainKey = ''">我已保存</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import {
  listGatewayKeys, createGatewayKey, updateGatewayKey,
  deleteGatewayKey, resetGatewayKey,
} from '@/api'

const keys = ref([])
const models = ref([])
const loading = ref(true)
const errorMsg = ref('')
const origin = window.location.origin

const showModal = ref(false)
const editing = ref(null)
const saving = ref(false)
const formError = ref('')
const newPlainKey = ref('')
const copied = ref(false)
const form = reactive({ name: '', token_quota: 0, request_quota: 0, modelsText: '', expires_at: '' })

function formatNum(n) { return (n || 0).toLocaleString('en-US') }
function pct(used, quota) { return quota ? Math.min(100, Math.round((used / quota) * 100)) : 0 }
function formatTime(iso) {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

async function load() {
  loading.value = true
  try {
    const res = await listGatewayKeys()
    keys.value = res.data.keys || []
    models.value = res.data.models || []
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '加载密钥失败'
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editing.value = null
  Object.assign(form, { name: '', token_quota: 0, request_quota: 0, modelsText: '', expires_at: '' })
  formError.value = ''; showModal.value = true
}
function openEdit(k) {
  editing.value = k.id
  Object.assign(form, {
    name: k.name, token_quota: k.token_quota, request_quota: k.request_quota,
    modelsText: (k.allowed_models || []).join(', '),
    expires_at: k.expires_at ? k.expires_at.slice(0, 16) : '',
  })
  formError.value = ''; showModal.value = true
}

async function onSave() {
  formError.value = ''
  const modelsList = form.modelsText.split(',').map((s) => s.trim()).filter(Boolean)
  const expires = form.expires_at ? new Date(form.expires_at).toISOString() : null
  const payload = {
    name: form.name, token_quota: Number(form.token_quota) || 0,
    request_quota: Number(form.request_quota) || 0, allowed_models: modelsList, expires_at: expires,
  }
  saving.value = true
  try {
    if (editing.value) {
      await updateGatewayKey(editing.value, payload)
    } else {
      const res = await createGatewayKey(payload)
      newPlainKey.value = res.data.key?.key || ''
      copied.value = false
    }
    showModal.value = false
    await load()
  } catch (e) {
    formError.value = e.response?.data?.detail || '保存失败'
  } finally {
    saving.value = false
  }
}

async function copyKey() {
  try { await navigator.clipboard.writeText(newPlainKey.value); copied.value = true }
  catch { copied.value = false }
}
async function onReset(k) {
  if (!confirm(`确认重置「${k.name}」的 Token 与请求用量？`)) return
  try { await resetGatewayKey(k.id); await load() }
  catch (e) { errorMsg.value = e.response?.data?.detail || '重置失败' }
}
async function onToggle(k) {
  try { await updateGatewayKey(k.id, { enabled: !k.enabled }); await load() }
  catch (e) { errorMsg.value = e.response?.data?.detail || '操作失败' }
}
async function onDelete(k) {
  if (!confirm(`确认删除密钥「${k.name}」？删除后该密钥立即失效。`)) return
  try { await deleteGatewayKey(k.id); await load() }
  catch (e) { errorMsg.value = e.response?.data?.detail || '删除失败' }
}

onMounted(load)
</script>

<style scoped>
.panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px; flex-wrap: wrap; }
.panel-intro h2 { font-size: 16px; font-weight: 700; margin-bottom: 4px; }
.panel-intro p { font-size: 12.5px; color: var(--color-text-secondary); line-height: 1.6; max-width: 760px; }
.panel-intro code { background: #f4ece0; padding: 1px 5px; border-radius: 4px; font-size: 11.5px; }
.mono { font-family: 'SFMono-Regular', Consolas, monospace; }

.key-table { width: 100%; border-collapse: collapse; font-size: 12.5px; background: #fffdfb; border: 1px solid var(--color-border); border-radius: 12px; overflow: hidden; }
.key-table th { text-align: left; padding: 10px; color: var(--color-text-secondary); font-weight: 600; border-bottom: 1px solid var(--color-border); font-size: 11.5px; background: #faf6f0; }
.key-table td { padding: 11px 10px; border-bottom: 1px solid #f4efe8; vertical-align: middle; }
.key-table tbody tr:hover { background: #faf6f0; }
.key-table .num { text-align: right; }
.key-table th.num { text-align: right; }
.k-name { font-weight: 600; }
.k-prefix { color: var(--color-text-secondary); font-size: 11.5px; }
.k-time { color: var(--color-text-secondary); font-size: 11.5px; white-space: nowrap; }
.ops { display: flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap; }

.quota-cell { display: flex; flex-direction: column; align-items: flex-end; gap: 3px; }
.quota-bar { width: 62px; height: 5px; background: #f0e9df; border-radius: 3px; overflow: hidden; }
.quota-fill { height: 100%; background: var(--color-primary); border-radius: 3px; }
.wild { color: var(--color-text-secondary); font-style: italic; }
.model-chips { display: flex; gap: 4px; flex-wrap: wrap; }
.model-chip { background: var(--color-primary-light); color: #a5703a; padding: 1px 7px; border-radius: 7px; font-size: 10.5px; }
.state-pill { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 8px; }
.st-active { background: #e4f4e8; color: #3d8b52; }
.st-disabled { background: #eee9e2; color: #8a7e72; }
.st-expired { background: #fbe4e3; color: #c0392b; }

.icon-btn { background: transparent; border: none; color: var(--color-text-secondary); cursor: pointer; font-size: 12px; padding: 2px 4px; }
.icon-btn:hover { color: var(--color-primary); }
.icon-btn.danger:hover { color: #c0392b; }

.modal-mask { position: fixed; inset: 0; background: rgba(45,42,36,0.35); display: flex; align-items: center; justify-content: center; z-index: 100; padding: 20px; }
.modal { background: #fffdfb; border-radius: 14px; padding: 22px 24px; width: 100%; max-width: 520px; max-height: 88vh; overflow-y: auto; box-shadow: 0 8px 30px rgba(0,0,0,0.18); }
.modal-title { font-size: 16px; font-weight: 700; margin-bottom: 16px; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px 18px; }
.form-grid .span-2 { grid-column: span 2; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field-label { font-size: 12px; color: var(--color-text-secondary); font-weight: 600; }
.field-input { padding: 8px 11px; border: 1px solid var(--color-border); border-radius: 8px; font-size: 13px; background: #fff; color: var(--color-text); outline: none; }
.field-input:focus { border-color: var(--color-primary); }
.modal-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 20px; }

.plain-warn { font-size: 12.5px; color: var(--color-text-secondary); line-height: 1.6; margin-bottom: 12px; }
.plain-key-box { display: flex; align-items: center; gap: 10px; background: #faf6f0; border: 1px dashed var(--color-primary); border-radius: 10px; padding: 12px 14px; }
.plain-key { flex: 1; font-size: 12.5px; word-break: break-all; color: var(--color-text); }
.plain-hint { font-size: 11.5px; color: var(--color-text-secondary); margin-top: 14px; line-height: 1.7; }
.plain-hint code { background: #f4ece0; padding: 1px 5px; border-radius: 4px; word-break: break-all; }

.btn { padding: 8px 16px; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer; border: 1px solid transparent; transition: all 0.15s; white-space: nowrap; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn.sm { padding: 5px 12px; font-size: 12px; }
.btn-primary { background: var(--color-primary); color: #fff; }
.btn-primary:hover:not(:disabled) { background: #b87a3f; }
.btn-ghost { background: transparent; border-color: var(--color-border); color: var(--color-text); }
.btn-ghost:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
.err-tip { color: #d9534f; font-size: 12.5px; margin-bottom: 10px; }
.mini-loading, .mini-empty { color: var(--color-text-secondary); font-size: 13px; padding: 32px; text-align: center; }
</style>
