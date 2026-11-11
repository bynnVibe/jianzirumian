<template>
  <div class="panel">
    <div class="panel-head">
      <div class="panel-intro">
        <h2>路由规则 · 模型 → 上游列表</h2>
        <p>
          把「上游管理」中的模型清单编入一组规则：按<strong>模型模式</strong>（支持 <code>*</code> 通配，如
          <code>deepseek-*</code>）选中一组<strong>上游</strong>，并配置负载均衡策略、优先级、默认与授权。
          请求命中规则后按策略在健康上游间分发，失败自动切换下一个。
        </p>
      </div>
      <button class="btn btn-primary" @click="openCreate">+ 添加规则</button>
    </div>

    <div class="legend-bar">
      <span class="count-line">共 {{ rules.length }} 条规则（模型支持通配符）</span>
      <span class="legend">
        <i class="dot d-healthy"></i>正常
        <i class="dot d-degraded"></i>降级
        <i class="dot d-down"></i>异常
        <i class="dot d-unknown"></i>未知
      </span>
      <button class="btn btn-ghost sm" :disabled="loading" @click="load">刷新状态</button>
    </div>

    <p v-if="errorMsg" class="err-tip">{{ errorMsg }}</p>
    <div v-if="loading" class="mini-loading">加载规则中…</div>

    <div v-else-if="!rules.length" class="mini-empty">
      还没有路由规则。添加一条规则、填写模型模式并勾选上游后，网关即可对外选路。
    </div>

    <table v-else class="rule-table">
      <thead>
        <tr><th>模型</th><th>上游列表</th><th class="act">操作</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in rules" :key="r.id" :class="{ 'row-disabled': !r.enabled }">
          <td>
            <div class="rule-pattern mono">{{ r.model_pattern }}</div>
            <div class="rule-meta">
              <span class="strategy-pill">{{ strategyLabel(r.lb_strategy) }}</span>
              <span v-if="r.lb_strategy === 'primary_backup'" class="pb-pill">
                主：{{ upstreamName(r.primary_upstream) }} · 失败 {{ r.failover_threshold || 3 }} 次切备
              </span>
              <span v-if="r.is_default" class="default-pill">默认</span>
              <span class="prio">优先级 {{ r.priority }}</span>
              <span v-if="!r.enabled" class="disabled-pill">已停用</span>
              <span v-if="r.allowed_users?.length" class="auth-pill">{{ r.allowed_users.length }} 人可见</span>
            </div>
          </td>
          <td>
            <div class="chip-wrap">
              <span v-for="u in r.upstream_detail" :key="u.id" class="up-chip" :class="`c-${u.health}`"
                    :title="u.name">
                <i class="dot" :class="`d-${u.health}`"></i>{{ u.name }}
              </span>
              <span v-if="!r.upstream_detail?.length" class="model-wild">未绑定上游</span>
            </div>
          </td>
          <td class="act">
            <button class="btn btn-ghost sm" @click="openEdit(r)">编辑</button>
            <button class="btn btn-danger sm" @click="onDelete(r)">删除</button>
          </td>
        </tr>
      </tbody>
    </table>

    <!-- 规则表单弹窗 -->
    <div v-if="showModal" class="modal-mask">
      <div class="modal">
        <div class="modal-head">
          <h3 class="modal-title">{{ editing ? '编辑规则' : '添加规则' }}</h3>
          <button class="modal-close" title="关闭" @click="showModal = false">×</button>
        </div>
        <div class="form-grid">
          <label class="field span-2">
            <span class="field-label">模型模式 *（支持 * 通配，如 deepseek-*）</span>
            <input v-model.trim="form.model_pattern" class="field-input mono" list="model-suggest" placeholder="gpt-4o-mini 或 deepseek-* 或 *" />
            <datalist id="model-suggest">
              <option v-for="m in allModels" :key="m" :value="m" />
            </datalist>
          </label>
          <label class="field">
            <span class="field-label">负载均衡策略</span>
            <select v-model="form.lb_strategy" class="field-input">
              <option value="weighted">weighted（权重加权随机）</option>
              <option value="round_robin">round_robin（轮询）</option>
              <option value="random">random（随机）</option>
              <option value="priority">priority（按上游列表顺序）</option>
              <option value="primary_backup">primary_backup（主备路由）</option>
            </select>
          </label>
          <label class="field">
            <span class="field-label">优先级（越大越先匹配）</span>
            <input v-model.number="form.priority" type="number" class="field-input" />
          </label>

          <!-- 主备路由：主路由选择 + 连续失败切换阈值（仅 primary_backup 时展示） -->
          <template v-if="form.lb_strategy === 'primary_backup'">
            <label class="field">
              <span class="field-label">主路由（优先出网，连续失败后切备）</span>
              <select v-model="form.primary_upstream" class="field-input">
                <option value="" disabled>{{ selectedUpstreams.length ? '请选择主路由' : '请先勾选上游列表' }}</option>
                <option v-for="id in selectedUpstreams" :key="id" :value="id">{{ upstreamName(id) }}</option>
              </select>
            </label>
            <label class="field">
              <span class="field-label">连续失败阈值（1-20，默认 3）</span>
              <input v-model.number="form.failover_threshold" type="number" min="1" max="20" class="field-input" />
            </label>
            <p class="field span-2 pb-hint">
              其余已勾选上游自动作为<strong>备路由</strong>：主路由连续失败达到阈值（或健康探测为异常）时，
              网关自动把后续请求切换到备路由；主路由恢复成功后自动回切。
            </p>
          </template>

          <div class="field span-2">
            <span class="field-label">上游列表（多选，顺序即 priority 策略次序）</span>
            <div v-if="!upstreams.length" class="reg-hint">请先在「上游管理」页签中添加上游</div>
            <div v-else class="check-list">
              <label v-for="u in upstreams" :key="u.id" class="check-item">
                <input type="checkbox" :value="u.id" v-model="form.upstreams" />
                <span>{{ u.name }}</span>
                <em class="mono">{{ tierLabel(u.tier) }} · {{ u.model_count || 0 }} 模型</em>
              </label>
            </div>
          </div>
          <label class="field checkbox">
            <input type="checkbox" v-model="form.enabled" />
            <span>启用该规则</span>
          </label>
          <label class="field checkbox">
            <input type="checkbox" v-model="form.is_default" />
            <span>设为全局默认规则（唯一）</span>
          </label>
          <div class="field span-2">
            <span class="field-label">授权用户（不选=全员可用）</span>
            <div v-if="!users.length" class="reg-hint">暂无可选用户</div>
            <div v-else class="check-list">
              <label v-for="u in users" :key="u.id" class="check-item">
                <input type="checkbox" :value="String(u.id)" v-model="form.allowed_users" />
                <span>{{ u.username }}</span>
                <em>{{ u.role }}</em>
              </label>
            </div>
          </div>
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
import { ref, reactive, computed, onMounted } from 'vue'
import {
  listGatewayRoutingRules, createGatewayRoutingRule, updateGatewayRoutingRule,
  deleteGatewayRoutingRule, listUsers,
} from '@/api'

const rules = ref([])
const upstreams = ref([])
const allModels = ref([])
const users = ref([])
const loading = ref(true)
const errorMsg = ref('')

const showModal = ref(false)
const editing = ref(null)
const saving = ref(false)
const formError = ref('')
const form = reactive({
  model_pattern: '', upstreams: [], lb_strategy: 'weighted', priority: 0,
  primary_upstream: '', failover_threshold: 3,
  is_default: false, allowed_users: [], enabled: true,
})

// 主备路由：主路由下拉候选 = 已勾选的上游；upstreamName 用于列表回显
const selectedUpstreams = computed(() => form.upstreams.slice())
function upstreamName(id) {
  if (!id) return '未指定'
  const u = upstreams.value.find((x) => x.id === id)
  return u ? u.name : id
}

function strategyLabel(s) {
  return { weighted: '加权', round_robin: '轮询', random: '随机', priority: '顺序', primary_backup: '主备路由' }[s] || '加权'
}

function tierLabel(t) { return { paid: '付费', free: '免费', subscription: '订阅' }[t] || t }

async function load() {
  loading.value = true
  errorMsg.value = ''
  try {
    const res = await listGatewayRoutingRules()
    rules.value = res.data.rules || []
    upstreams.value = res.data.upstreams || []
    allModels.value = res.data.models || []
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '加载规则失败'
  } finally {
    loading.value = false
  }
}

async function loadUsers() {
  try {
    const res = await listUsers()
    users.value = res.data.users || res.data || []
  } catch (e) {
    // 辅助数据加载失败不阻断规则管理
  }
}

function resetForm() {
  Object.assign(form, {
    model_pattern: '', upstreams: [], lb_strategy: 'weighted', priority: 0,
    primary_upstream: '', failover_threshold: 3,
    is_default: false, allowed_users: [], enabled: true,
  })
}
function openCreate() {
  editing.value = null; resetForm(); formError.value = ''
  showModal.value = true
}
function openEdit(r) {
  editing.value = r.id
  Object.assign(form, {
    model_pattern: r.model_pattern, upstreams: [...(r.upstreams || [])],
    lb_strategy: r.lb_strategy || 'weighted', priority: r.priority || 0,
    primary_upstream: r.primary_upstream || '', failover_threshold: r.failover_threshold || 3,
    is_default: !!r.is_default, allowed_users: (r.allowed_users || []).map(String),
    enabled: !!r.enabled,
  })
  formError.value = ''
  showModal.value = true
}

function buildPayload() {
  const payload = {
    model_pattern: form.model_pattern, upstreams: [...form.upstreams],
    lb_strategy: form.lb_strategy, priority: Number(form.priority) || 0,
    is_default: form.is_default, allowed_users: [...form.allowed_users], enabled: form.enabled,
  }
  if (form.lb_strategy === 'primary_backup') {
    payload.primary_upstream = form.primary_upstream
    payload.failover_threshold = Number(form.failover_threshold) || 3
  }
  return payload
}

async function onSave() {
  formError.value = ''
  const payload = buildPayload()
  if (!payload.model_pattern) { formError.value = '模型模式不能为空'; return }
  if (!payload.upstreams.length) { formError.value = '请至少选择一个上游'; return }
  if (payload.lb_strategy === 'primary_backup' && !payload.primary_upstream) {
    formError.value = '主备路由需指定一个主路由（须来自已勾选的上游列表）'; return
  }

  saving.value = true
  try {
    if (editing.value) await updateGatewayRoutingRule(editing.value, payload)
    else await createGatewayRoutingRule(payload)
    showModal.value = false
    await load()
  } catch (e) {
    formError.value = e.response?.data?.detail || '保存失败'
  } finally {
    saving.value = false
  }
}

async function onDelete(r) {
  if (!confirm(`确认删除规则「${r.model_pattern}」？`)) return
  try { await deleteGatewayRoutingRule(r.id); await load() }
  catch (e) { errorMsg.value = e.response?.data?.detail || '删除失败' }
}

onMounted(() => { load(); loadUsers() })
</script>

<style scoped>
.panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 14px; flex-wrap: wrap; }
.panel-intro h2 { font-size: 16px; font-weight: 700; margin-bottom: 4px; }
.panel-intro p { font-size: 12.5px; color: var(--color-text-secondary); line-height: 1.6; max-width: 780px; }
.panel-intro code { background: #f4ece0; padding: 1px 5px; border-radius: 4px; font-size: 11.5px; }

.legend-bar { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; margin-bottom: 10px; }
.count-line { font-size: 12.5px; color: var(--color-text-secondary); }
.legend { display: flex; align-items: center; gap: 6px; font-size: 11.5px; color: var(--color-text-secondary); }
.legend .dot { margin-left: 8px; }
.dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
.d-healthy { background: #4a9d5f; }
.d-degraded { background: #e0a92e; }
.d-down { background: #d9534f; }
.d-unknown { background: #c9c0b4; }

.rule-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.rule-table th { text-align: left; padding: 9px 10px; color: var(--color-text-secondary); font-weight: 600; border-bottom: 1px solid var(--color-border); font-size: 12px; }
.rule-table td { padding: 12px 10px; border-bottom: 1px solid #f4efe8; vertical-align: middle; }
.rule-table th.act, .rule-table td.act { text-align: right; white-space: nowrap; }
.rule-table td.act .btn { margin-left: 4px; }
.row-disabled { opacity: 0.55; }
.rule-pattern { font-weight: 700; font-size: 13.5px; }
.rule-meta { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-top: 5px; }
.prio { font-size: 11px; color: var(--color-text-secondary); }
.mono { font-family: 'SFMono-Regular', Consolas, monospace; }

.chip-wrap { display: flex; flex-wrap: wrap; gap: 6px; }
.up-chip { display: inline-flex; align-items: center; gap: 5px; font-size: 11.5px; padding: 3px 9px; border-radius: 10px; background: #e4f4e8; color: #2f6b40; }
.up-chip.c-degraded { background: #fdf0d9; color: #b8802a; }
.up-chip.c-down { background: #fbe4e3; color: #c0392b; }
.up-chip.c-unknown { background: #eee9e2; color: #8a7e72; }
.model-wild { color: var(--color-text-secondary); font-style: italic; font-size: 12px; }

.strategy-pill { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 8px; background: #eef2f7; color: #5a6b7d; }
.default-pill { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 8px; background: var(--color-primary-light); color: #a5703a; }
.disabled-pill { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 8px; background: #eee9e2; color: #8a7e72; }
.auth-pill { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 8px; background: #f0e8fb; color: #7a4aa5; }
.pb-pill { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 8px; background: #e6f2f6; color: #2f6b7d; }
.pb-hint { font-size: 11.5px; color: var(--color-text-secondary); line-height: 1.6; background: #f7f2ea; border-radius: 6px; padding: 8px 10px; margin: 0; }
.pb-hint strong { color: var(--color-text); }

.reg-hint { font-size: 12px; color: #b8802a; background: #fdf0d9; padding: 8px 10px; border-radius: 6px; }
.check-list { display: flex; flex-wrap: wrap; gap: 6px; max-height: 180px; overflow-y: auto; border: 1px solid var(--color-border); border-radius: 8px; padding: 8px; background: #fff; }
.check-item { display: flex; align-items: center; gap: 5px; font-size: 12px; padding: 3px 8px; border-radius: 6px; background: #f7f2ea; cursor: pointer; }
.check-item em { font-style: normal; color: var(--color-text-secondary); font-size: 11px; }
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
