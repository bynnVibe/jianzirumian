<template>
  <div class="panel">
    <div class="panel-head">
      <div class="panel-intro">
        <h2>业务路由 · role → 规则 + 模型</h2>
        <p>
          为不同业务角色指定<strong>路由规则</strong>与<strong>模型名</strong>。未配置的角色回退到<code>默认规则</code> + 网关默认模型。
          规则下拉仅列出启用规则；选定规则后模型名仅可从<strong>该规则包含的模型</strong>中选择（也可手动输入），留空则走网关默认模型（不在规则上游模型清单内时自动在规则内<strong>随机均衡</strong>选取）。留空并保存即清除该角色绑定。
        </p>
        <p class="strict-note">
          ⚠️ <strong>严格绑定</strong>：角色绑定规则后，网关只会在该规则的上游内选择支持该模型的上游出网；
          若无任何上游支持，将直接报错（503）而<strong>不会静默回退到其他规则/默认规则</strong>。
          规则的「模型模式」仅作规则名/模式匹配用途，不影响角色绑定选路；请确保绑定规则的上游模型列表覆盖该角色实际使用的模型。
        </p>
      </div>
      <button class="btn btn-primary" :disabled="saving" @click="onSaveAll">{{ saving ? '保存中…' : '保存全部' }}</button>
    </div>

    <!-- 网关默认模型：业务路由未指定模型名时的兜底出网模型（原系统设置「LLM 语言模型」职责并入） -->
    <div class="default-model-row">
      <span class="dm-label">网关默认模型</span>
      <input v-model.trim="defaultModel" class="field-input mono dm-input" list="gw-default-model-suggest"
             placeholder="（回退 .env API_MODEL）" />
      <datalist id="gw-default-model-suggest">
        <option v-for="m in allModels" :key="m" :value="m" />
      </datalist>
      <button class="btn btn-ghost sm" :disabled="savingDefault" @click="onSaveDefault">
        {{ savingDefault ? '保存中…' : '保存默认模型' }}
      </button>
      <span v-if="defaultMsg" class="dm-msg">{{ defaultMsg }}</span>
      <span class="dm-hint">角色未填模型名时使用该模型出网</span>
    </div>

    <p v-if="errorMsg" class="err-tip">{{ errorMsg }}</p>
    <p v-if="okMsg" class="ok-tip">{{ okMsg }}</p>
    <div v-if="loading" class="mini-loading">加载路由中…</div>

    <table v-else class="route-table">
      <thead>
        <tr><th>业务角色</th><th>路由规则</th><th>模型名</th><th class="act">操作</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.role">
          <td>
            <div class="role-name">{{ roleLabel(r.role) }}</div>
            <div class="role-key mono">{{ r.role }}</div>
          </td>
          <td>
            <select v-model="r.rule_id" class="field-input">
              <option value="">（默认规则）</option>
              <option v-for="rule in rules" :key="rule.id" :value="rule.id">{{ rule.model_pattern }}</option>
            </select>
          </td>
          <td>
            <input v-model.trim="r.model_name" class="field-input mono" :list="'role-model-suggest-' + r.role"
                   placeholder="（网关默认模型）" />
          </td>
          <td class="act">
            <button class="btn btn-ghost sm" :disabled="!r.bound" @click="onClear(r)">清除</button>
          </td>
        </tr>
      </tbody>
    </table>
    <datalist v-for="r in rows" :id="'role-model-suggest-' + r.role" :key="r.role">
      <option v-for="m in modelsForRow(r)" :key="m" :value="m" />
    </datalist>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted, inject } from 'vue'
import {
  listGatewayRoleRoutes, upsertGatewayRoleRoute, deleteGatewayRoleRoute,
  listGatewayRoutingRules, getGatewaySettings, saveGatewaySettings,
} from '@/api'
import { roleLabel } from '@/utils/roleLabels'

const roles = ref([])
const routes = ref({})
const rules = ref([])
const upstreams = ref([])
const allModels = ref([])
const loading = ref(true)
const saving = ref(false)
const errorMsg = ref('')
const okMsg = ref('')
// 网关默认模型（gateway_settings.default_model）
const defaultModel = ref('')
// 已保存的默认模型快照：与 defaultModel 不一致即视为未保存改动（供离开拦截判脏）
const savedDefault = ref('')
const savingDefault = ref(false)
const defaultMsg = ref('')

// 上游 id → 模型清单，供按规则聚合可选模型
const upstreamModels = computed(() => {
  const map = {}
  for (const u of upstreams.value) map[u.id] = u.models || []
  return map
})

// role → 上游分类（镜像后端 rules.ROLE_CATEGORY）：候选模型仅从同类上游里聚合，
// 防 LLM 角色误选 embedding/rerank/ocr 上游模型（反之亦然）；未登记 role 归 llm。
const ROLE_CATEGORY = {
  chat_answer: 'llm', assistant: 'llm', intent_rewrite: 'llm',
  evidence_check: 'llm', wiki_compile: 'llm', memory_extract: 'llm',
  session_summary: 'llm', knowledge_summarize: 'llm', text_polish: 'llm',
  embed: 'embedding', rerank: 'rerank', ocr: 'ocr',
}
function categoryOf(row) {
  return ROLE_CATEGORY[row.role] || 'llm'
}

// 选定规则后仅暴露该规则绑定上游（且同类别）的模型；未选规则（默认）时回退同类别全部模型
function modelsForRow(row) {
  const cat = categoryOf(row)
  if (!row.rule_id) {
    const set = new Set()
    for (const u of upstreams.value) {
      if ((u.category || 'llm') !== cat) continue
      for (const m of (u.models || [])) set.add(m)
    }
    return [...set].sort()
  }
  const rule = rules.value.find((x) => x.id === row.rule_id)
  if (!rule) return modelsForRow({ ...row, rule_id: '' })
  const byId = {}
  for (const u of upstreams.value) byId[u.id] = u
  const set = new Set()
  for (const uid of (rule.upstreams || [])) {
    const u = byId[uid]
    if (!u || (u.category || 'llm') !== cat) continue
    for (const m of (upstreamModels.value[uid] || [])) set.add(m)
  }
  return [...set].sort()
}

const rows = computed(() => roles.value.map((role) => {
  const rt = routes.value[role] || {}
  // 行对象需响应式：v-model 改写 rule_id/model_name 后才能触发模型候选 datalist 重渲染
  return reactive({
    role,
    rule_id: rt.rule_id || '',
    model_name: rt.model_name || '',
    bound: !!(rt.rule_id || rt.model_name),
    _orig_rule: rt.rule_id || '',
    _orig_model: rt.model_name || '',
  })
}))

async function load() {
  loading.value = true
  errorMsg.value = ''
  try {
    const [rRes, ruleRes] = await Promise.all([
      listGatewayRoleRoutes(), listGatewayRoutingRules(),
    ])
    roles.value = rRes.data.roles || []
    const map = {}
    for (const rt of (rRes.data.routes || [])) map[rt.role] = rt
    routes.value = map
    rules.value = (ruleRes.data.rules || []).filter((r) => r.enabled)
    upstreams.value = ruleRes.data.upstreams || []
    allModels.value = ruleRes.data.models || []
    const sRes = await getGatewaySettings()
    defaultModel.value = sRes.data.settings?.default_model || ''
    savedDefault.value = defaultModel.value
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '加载路由失败'
  } finally {
    loading.value = false
  }
}

async function onSaveAll() {
  saving.value = true
  errorMsg.value = ''
  okMsg.value = ''
  try {
    for (const r of rows.value) {
      const changed = r.rule_id !== r._orig_rule || r.model_name !== r._orig_model
      if (!changed) continue
      if (!r.rule_id && !r.model_name) {
        await deleteGatewayRoleRoute(r.role)
      } else {
        await upsertGatewayRoleRoute(r.role, { role: r.role, rule_id: r.rule_id, model_name: r.model_name })
      }
    }
    okMsg.value = '已保存业务路由'
    await load()
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '保存失败'
  } finally {
    saving.value = false
  }
}

async function onClear(r) {
  try {
    await deleteGatewayRoleRoute(r.role)
    await load()
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '清除失败'
  }
}

async function onSaveDefault() {
  savingDefault.value = true
  defaultMsg.value = ''
  try {
    await saveGatewaySettings({ default_model: defaultModel.value })
    savedDefault.value = defaultModel.value
    defaultMsg.value = '已保存'
    setTimeout(() => { defaultMsg.value = '' }, 2500)
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '保存默认模型失败'
  } finally {
    savingDefault.value = false
  }
}

// ---- 未保存草稿注册到网关离开拦截（GatewayView provide 的注册器）----
// 业务路由是网关唯一有页内联草稿态的面板：行 rule_id/model_name 与 _orig 不一致、
// 或默认模型与已保存值不一致即视为脏；save 复用 onSaveDefault + onSaveAll。
const gwRegisterDirty = inject('gwRegisterDirty', null)
const gwUnregisterDirty = inject('gwUnregisterDirty', null)
const DIRTY_ID = 'role-routes'

function rowsDirty() {
  return rows.value.some((r) => r.rule_id !== r._orig_rule || r.model_name !== r._orig_model)
}
function isDirty() {
  return rowsDirty() || defaultModel.value !== savedDefault.value
}
async function saveDirty() {
  // 先存默认模型（onSaveDefault 不 reload），再存路由行（onSaveAll 会 reload 并重置快照）
  if (defaultModel.value !== savedDefault.value) await onSaveDefault()
  if (rowsDirty()) await onSaveAll()
}

onMounted(async () => {
  await load()
  if (gwRegisterDirty) gwRegisterDirty({ id: DIRTY_ID, isDirty, save: saveDirty })
})
onUnmounted(() => {
  if (gwUnregisterDirty) gwUnregisterDirty(DIRTY_ID)
})
</script>

<style scoped>
.panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 12px; flex-wrap: wrap; }
.default-model-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; padding: 10px 12px; background: #faf6ef; border: 1px dashed var(--color-border); border-radius: 10px; }
.dm-label { font-size: 13px; font-weight: 700; }
.dm-input { max-width: 300px; }
.dm-msg { font-size: 12px; color: #3d8b52; font-weight: 600; }
.dm-hint { font-size: 12px; color: var(--color-text-secondary); }
.panel-intro h2 { font-size: 16px; font-weight: 700; margin-bottom: 4px; }
.panel-intro p { font-size: 12.5px; color: var(--color-text-secondary); line-height: 1.6; max-width: 760px; }
.panel-intro .strict-note { margin-top: 6px; padding: 8px 10px; background: #fdf3e7; border: 1px dashed #e0b98a; border-radius: 8px; color: #8a5a2b; }
.panel-intro code { background: #f4ece0; padding: 1px 5px; border-radius: 4px; font-size: 11.5px; }

.route-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.route-table th { text-align: left; padding: 9px 10px; color: var(--color-text-secondary); font-weight: 600; border-bottom: 1px solid var(--color-border); font-size: 12px; }
.route-table td { padding: 10px; border-bottom: 1px solid #f4efe8; vertical-align: middle; }
.route-table th.act, .route-table td.act { text-align: right; }
.role-name { font-weight: 600; }
.role-key { font-size: 11px; color: var(--color-text-secondary); }
.mono { font-family: 'SFMono-Regular', Consolas, monospace; }
.field-input { padding: 7px 10px; border: 1px solid var(--color-border); border-radius: 8px; font-size: 12.5px; background: #fff; color: var(--color-text); outline: none; min-width: 180px; }
.field-input:focus { border-color: var(--color-primary); }

.btn { padding: 8px 16px; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer; border: 1px solid transparent; transition: all 0.15s; white-space: nowrap; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn.sm { padding: 5px 12px; font-size: 12px; }
.btn-primary { background: var(--color-primary); color: #fff; }
.btn-primary:hover:not(:disabled) { background: #b87a3f; }
.btn-ghost { background: transparent; border-color: var(--color-border); color: var(--color-text); }
.btn-ghost:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
.err-tip { color: #d9534f; font-size: 12.5px; margin-bottom: 10px; }
.ok-tip { color: #2f6b40; font-size: 12.5px; margin-bottom: 10px; }
.mini-loading { color: var(--color-text-secondary); font-size: 13px; padding: 32px; text-align: center; }
</style>
