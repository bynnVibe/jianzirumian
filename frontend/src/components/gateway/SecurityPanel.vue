<template>
  <div class="panel">
    <div class="panel-head">
      <div class="panel-intro">
        <h2>安全审计中心</h2>
        <p>
          内置风险检测引擎，自动扫描请求中的凭证泄露、敏感路径访问、工具命令外联、Unicode 隐写、追踪像素、公网 IP 探测等风险。
          支持<strong>审计 / 警告 / 脱敏 / 阻断</strong>四种模式，内置规则可启停，可自定义黑白名单。
        </p>
      </div>
      <button class="btn btn-primary" :disabled="saving" @click="onSave">{{ saving ? '保存中…' : '保存配置' }}</button>
    </div>

    <p v-if="errorMsg" class="err-tip">{{ errorMsg }}</p>
    <p v-if="savedTip" class="ok-tip">✓ 配置已保存</p>
    <div v-if="loading" class="mini-loading">加载安全配置中…</div>

    <template v-else>
      <!-- 风险事件闭环：记录 → 弹窗告警 → 模型建议 → 处理留痕 -->
      <section class="card">
        <div class="card-head">
          <h3 class="card-title">
            风险事件
            <span v-if="eventStats.pending" class="ev-badge">待处理 {{ eventStats.pending }}</span>
            <span v-if="eventStats.unacked_alerts" class="ev-badge alert">未确认告警 {{ eventStats.unacked_alerts }}</span>
          </h3>
          <div class="rule-tools">
            <select v-model="eventStatusFilter" class="field-input sm" @change="loadEvents">
              <option value="">全部</option>
              <option value="pending">待处理</option>
              <option value="resolved">已处理</option>
            </select>
            <button class="btn btn-ghost sm" :disabled="eventsLoading" @click="loadEvents">刷新</button>
          </div>
        </div>
        <p class="card-sub">
          代理链路每次命中风险即在此留档；达到告警等级会自动弹窗。可对单条事件「模型建议」生成处置策略，处理后记录留痕。
        </p>
        <div v-if="eventsLoading && !events.length" class="mini-loading">加载风险事件中…</div>
        <div v-else-if="!events.length" class="mini-empty">暂无风险事件。</div>
        <div v-else class="ev-list">
          <article v-for="ev in events" :key="ev.id" class="ev-card" :class="`sev-${ev.risk_level}`">
            <div class="ev-head" @click="toggleExpand(ev.id)">
              <span class="risk-tag" :class="`risk-${ev.risk_level}`">{{ riskLabel(ev.risk_level) }} · {{ ev.risk_score }}</span>
              <span class="ev-title">{{ ev.finding_count }} 项命中</span>
              <span class="ev-meta">{{ ev.role || '—' }} · {{ ev.model || '—' }} · 密钥 {{ ev.key_name || '—' }}</span>
              <span class="ev-time">{{ formatTime(ev.created_at) }}</span>
              <span v-if="ev.status === 'resolved'" class="ev-state done">{{ ev.resolution_label || '已处理' }}</span>
              <span v-else class="ev-state pending">待处理</span>
              <span class="ev-caret">{{ expanded.has(ev.id) ? '▾' : '▸' }}</span>
            </div>
            <div v-if="expanded.has(ev.id)" class="ev-body">
              <div class="ev-findings">
                <div v-for="(f, i) in ev.findings" :key="i" class="finding" :class="`sev-${f.severity}`">
                  <div class="finding-head">
                    <strong>{{ f.rule_name }}</strong>
                    <span class="finding-cat">{{ f.category_label }}</span>
                    <span class="finding-sev" :class="`txt-${f.severity}`">{{ sevLabel(f.severity) }}</span>
                  </div>
                  <div class="finding-match mono">{{ f.matched }}</div>
                </div>
              </div>

              <!-- 模型建议 -->
              <div class="ev-suggest">
                <div class="ev-suggest-head">
                  <strong>处理策略建议</strong>
                  <button class="btn btn-ghost sm" :disabled="suggestingId === ev.id" @click="onSuggest(ev)">
                    {{ suggestingId === ev.id ? '生成中…' : (ev.suggestion ? '重新生成' : '模型建议') }}
                  </button>
                </div>
                <p v-if="ev.suggestion" class="ev-suggest-text">{{ ev.suggestion }}</p>
                <p v-else class="ev-suggest-empty">尚未生成建议，点「模型建议」由模型研判并推荐处置动作。</p>
              </div>

              <!-- 处理留痕 -->
              <div v-if="ev.status === 'pending'" class="ev-resolve">
                <select v-model="resolveForm[ev.id].resolution" class="field-input sm">
                  <option v-for="r in resolutions" :key="r" :value="r">{{ resolutionLabels[r] || r }}</option>
                </select>
                <input v-model.trim="resolveForm[ev.id].note" class="field-input sm ev-note" placeholder="处理说明（可选）" />
                <button class="btn btn-primary sm" :disabled="resolvingId === ev.id" @click="onResolve(ev)">
                  {{ resolvingId === ev.id ? '提交中…' : '确认处理' }}
                </button>
                <button class="btn btn-ghost sm" @click="onDeleteEvent(ev)">删除</button>
              </div>
              <div v-else class="ev-resolved">
                <span>处理动作：<strong>{{ ev.resolution_label || '—' }}</strong></span>
                <span v-if="ev.resolved_by">· 处理人 {{ ev.resolved_by }}</span>
                <span v-if="ev.resolved_at">· {{ formatTime(ev.resolved_at) }}</span>
                <span v-if="ev.resolved_note" class="ev-note-text">· 备注 {{ ev.resolved_note }}</span>
                <button class="btn btn-ghost sm" @click="onReopen(ev)">重新打开</button>
              </div>
            </div>
          </article>
        </div>
      </section>

      <!-- 审计模式 -->
      <section class="card">
        <h3 class="card-title">审计模式</h3>
        <div class="mode-grid">
          <label v-for="m in modes" :key="m.key" class="mode-card" :class="{ active: settings.audit_mode === m.key }">
            <input type="radio" :value="m.key" v-model="settings.audit_mode" />
            <div class="mode-body">
              <span class="mode-name">{{ m.label }}</span>
              <span class="mode-desc">{{ m.desc }}</span>
            </div>
          </label>
        </div>
      </section>

      <!-- 检测开关 + 重试 + 保留天数 -->
      <section class="card">
        <h3 class="card-title">检测开关与网关行为</h3>
        <div class="switch-grid">
          <label class="switch-row">
            <input type="checkbox" v-model="bools.unicode_detection" />
            <span class="switch-text"><strong>Unicode 隐写检测</strong><em>零宽字符 / 双向控制符 / Tag 字符等隐写</em></span>
          </label>
          <label class="switch-row">
            <input type="checkbox" v-model="bools.tool_cmd_detection" />
            <span class="switch-text"><strong>工具命令检测</strong><em>curl/wget 外联、管道执行、rm -rf、反弹 shell 等</em></span>
          </label>
          <label class="switch-row">
            <input type="checkbox" v-model="bools.outbound_detection" />
            <span class="switch-text"><strong>外联追踪检测</strong><em>IP 探测服务、数据外泄、追踪像素、公网 IP</em></span>
          </label>
          <label class="field">
            <span class="field-label">失败重试次数（故障切换）</span>
            <input v-model.number="settings.retry_count" type="number" min="0" max="5" class="field-input" />
          </label>
          <label class="field">
            <span class="field-label">日志保留天数</span>
            <input v-model.number="settings.log_retention_days" type="number" min="1" max="365" class="field-input" />
          </label>
          <label class="field">
            <span class="field-label">弹窗告警最低风险等级</span>
            <select v-model="settings.security_alert_min_severity" class="field-input">
              <option value="low">低危及以上</option>
              <option value="medium">中危及以上（默认）</option>
              <option value="high">高危及以上</option>
              <option value="critical">仅严重</option>
            </select>
          </label>
        </div>
      </section>

      <!-- 内置规则 -->
      <section class="card">
        <div class="card-head">
          <h3 class="card-title">内置规则（{{ enabledCount }}/{{ rules.length }} 启用）</h3>
          <div class="rule-tools">
            <select v-model="ruleFilter" class="field-input sm">
              <option value="">全部分类</option>
              <option v-for="(label, key) in categoryLabels" :key="key" :value="key">{{ label }}</option>
            </select>
            <button class="btn btn-ghost sm" @click="toggleAll(true)">全部启用</button>
            <button class="btn btn-ghost sm" @click="toggleAll(false)">全部停用</button>
          </div>
        </div>
        <div class="rule-list">
          <label v-for="r in filteredRules" :key="r.id" class="rule-row" :class="{ disabled: !ruleEnabled(r.id) }">
            <input type="checkbox" :checked="ruleEnabled(r.id)" @change="toggleRule(r.id)" />
            <span class="sev-dot" :class="`sev-${r.severity}`"></span>
            <span class="rule-name">{{ r.name }}</span>
            <span class="rule-cat">{{ r.category_label }}</span>
            <span class="rule-sev" :class="`txt-${r.severity}`">{{ sevLabel(r.severity) }}</span>
            <span v-if="r.gate && !gateOn(r.gate)" class="rule-gate-off">开关已关</span>
          </label>
        </div>
      </section>

      <!-- 黑白名单 -->
      <section class="card">
        <h3 class="card-title">自定义黑白名单</h3>
        <div class="list-grid">
          <div class="list-col">
            <span class="list-label">黑名单（命中即视为高风险，每行一条子串）</span>
            <textarea v-model="blacklistText" class="field-input mono" rows="5" placeholder="internal-secret&#10;10.0.0.5"></textarea>
          </div>
          <div class="list-col">
            <span class="list-label">白名单（命中的内容忽略，不告警，每行一条）</span>
            <textarea v-model="whitelistText" class="field-input mono" rows="5" placeholder="sk-example-doc-placeholder"></textarea>
          </div>
        </div>
      </section>

      <!-- 扫描试跑 -->
      <section class="card">
        <h3 class="card-title">扫描试跑（不入库、不转发）</h3>
        <textarea v-model="scanText" class="field-input mono" rows="4" placeholder="粘贴一段待检测文本，例如包含 sk-xxxx、~/.ssh/id_rsa、curl xxx | bash 的内容"></textarea>
        <div class="scan-actions">
          <button class="btn btn-primary sm" :disabled="scanning" @click="onScan">{{ scanning ? '扫描中…' : '立即扫描' }}</button>
          <span v-if="scanResult" class="scan-summary">
            风险等级
            <span class="risk-tag" :class="`risk-${scanResult.risk_level}`">{{ riskLabel(scanResult.risk_level) }}</span>
            · {{ scanResult.risk_score }} 分 · 命中 {{ scanResult.findings?.length || 0 }} 项
          </span>
        </div>
        <div v-if="scanResult?.findings?.length" class="scan-findings">
          <div v-for="(f, i) in scanResult.findings" :key="i" class="finding" :class="`sev-${f.severity}`">
            <div class="finding-head">
              <strong>{{ f.rule_name }}</strong>
              <span class="finding-cat">{{ f.category_label }}</span>
              <span class="finding-sev" :class="`txt-${f.severity}`">{{ sevLabel(f.severity) }}</span>
            </div>
            <div class="finding-match mono">{{ f.matched }}</div>
          </div>
        </div>
        <div v-else-if="scanResult" class="scan-clean">✓ 未检测到风险</div>
      </section>
    </template>

    <!-- 弹窗告警：轮询未确认且达阈值的风险事件，逐条弹出 -->
    <div v-if="alertQueue.length" class="modal-mask">
      <div class="modal alert-modal" :class="`sev-${alertCurrent.risk_level}`">
        <div class="modal-head">
          <h3 class="modal-title">⚠️ 安全风险告警</h3>
          <span class="alert-count">{{ alertIndex + 1 }} / {{ alertQueue.length }}</span>
        </div>
        <div class="alert-top">
          <span class="risk-tag" :class="`risk-${alertCurrent.risk_level}`">{{ riskLabel(alertCurrent.risk_level) }} · {{ alertCurrent.risk_score }} 分</span>
          <span class="alert-meta">{{ alertCurrent.finding_count }} 项命中 · {{ alertCurrent.role || '—' }} · {{ alertCurrent.model || '—' }}</span>
          <span class="alert-time">{{ formatTime(alertCurrent.created_at) }}</span>
        </div>
        <div class="alert-findings">
          <div v-for="(f, i) in alertCurrent.findings" :key="i" class="finding" :class="`sev-${f.severity}`">
            <div class="finding-head">
              <strong>{{ f.rule_name }}</strong>
              <span class="finding-cat">{{ f.category_label }}</span>
              <span class="finding-sev" :class="`txt-${f.severity}`">{{ sevLabel(f.severity) }}</span>
            </div>
            <div class="finding-match mono">{{ f.matched }}</div>
          </div>
        </div>
        <div class="modal-actions">
          <button class="btn btn-ghost" @click="onAlertDismiss">知道了</button>
          <button class="btn btn-primary" @click="onAlertHandle">前往处理</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import {
  getGatewayRules, getGatewaySettings, saveGatewaySettings, gatewayScanPreview,
  listGatewaySecurityEvents, ackGatewaySecurityEvents, suggestGatewaySecurityEvent,
  resolveGatewaySecurityEvent, reopenGatewaySecurityEvent, deleteGatewaySecurityEvent,
} from '@/api'

const loading = ref(true)
const saving = ref(false)
const errorMsg = ref('')
const savedTip = ref(false)

const rules = ref([])
const categoryLabels = ref({})
const ruleFilter = ref('')
const settings = reactive({ audit_mode: 'audit', retry_count: 2, log_retention_days: 30, disabled_rules: [], blacklist: [], whitelist: [], security_alert_min_severity: 'medium' })

const bools = reactive({ unicode_detection: true, tool_cmd_detection: true, outbound_detection: true })
const blacklistText = ref('')
const whitelistText = ref('')

const scanText = ref('')
const scanning = ref(false)
const scanResult = ref(null)

// ---- 风险事件闭环 ----
const events = ref([])
const eventsLoading = ref(false)
const eventStats = reactive({ pending: 0, unacked_alerts: 0 })
const eventStatusFilter = ref('')
const expanded = ref(new Set())
const resolutions = ref([])
const resolutionLabels = ref({})
const resolveForm = reactive({})
const suggestingId = ref('')
const resolvingId = ref('')
// 弹窗告警队列（轮询未确认且达阈值的事件）
const alertQueue = ref([])
const alertIndex = ref(0)
const alertCurrent = computed(() => alertQueue.value[alertIndex.value] || {})
let alertTimer = null

function formatTime(iso) {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
function toggleExpand(id) {
  const s = new Set(expanded.value)
  if (s.has(id)) s.delete(id); else { s.add(id); ensureResolveForm(id) }
  expanded.value = s
}
function ensureResolveForm(id) {
  if (!resolveForm[id]) resolveForm[id] = { resolution: 'investigated', note: '' }
}

const modes = [
  { key: 'audit', label: '审计', desc: '只记录风险，不改变请求（默认）' },
  { key: 'warn', label: '警告', desc: '记录并在响应头/日志标注警告，仍放行' },
  { key: 'mask', label: '脱敏', desc: '命中内容脱敏为 [REDACTED] 后转发上游' },
  { key: 'block', label: '阻断', desc: '命中风险即拒绝请求（返回 403）' },
]

const SEV_LABELS = { low: '低危', medium: '中危', high: '高危', critical: '严重' }
const RISK_LABELS = { none: '无风险', low: '低', medium: '中', high: '高', critical: '严重' }
function sevLabel(s) { return SEV_LABELS[s] || s }
function riskLabel(l) { return RISK_LABELS[l] || l }

const filteredRules = computed(() =>
  ruleFilter.value ? rules.value.filter((r) => r.category === ruleFilter.value) : rules.value)
const enabledCount = computed(() => rules.value.filter((r) => ruleEnabled(r.id)).length)
function ruleEnabled(id) { return !settings.disabled_rules.includes(id) }
function gateOn(gate) { return bools[gate] }
function toggleRule(id) {
  const i = settings.disabled_rules.indexOf(id)
  if (i >= 0) settings.disabled_rules.splice(i, 1)
  else settings.disabled_rules.push(id)
}
function toggleAll(enable) {
  settings.disabled_rules = enable ? [] : rules.value.map((r) => r.id)
}

async function load() {
  loading.value = true
  try {
    const [rRes, sRes] = await Promise.all([getGatewayRules(), getGatewaySettings()])
    rules.value = rRes.data.rules || []
    categoryLabels.value = rRes.data.category_labels || {}
    const s = sRes.data.settings || {}
    settings.audit_mode = s.audit_mode || 'audit'
    settings.retry_count = s.retry_count ?? 2
    settings.log_retention_days = s.log_retention_days ?? 30
    settings.disabled_rules = [...(s.disabled_rules || [])]
    settings.blacklist = [...(s.blacklist || [])]
    settings.whitelist = [...(s.whitelist || [])]
    bools.unicode_detection = !!Number(s.unicode_detection ?? 1)
    bools.tool_cmd_detection = !!Number(s.tool_cmd_detection ?? 1)
    bools.outbound_detection = !!Number(s.outbound_detection ?? 1)
    settings.security_alert_min_severity = s.security_alert_min_severity || 'medium'
    blacklistText.value = (s.blacklist || []).join('\n')
    whitelistText.value = (s.whitelist || []).join('\n')
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '加载安全配置失败'
  } finally {
    loading.value = false
  }
}

function parseLines(text) {
  return text.split('\n').map((s) => s.trim()).filter(Boolean)
}

async function onSave() {
  saving.value = true
  errorMsg.value = ''
  savedTip.value = false
  try {
    const res = await saveGatewaySettings({
      audit_mode: settings.audit_mode,
      unicode_detection: bools.unicode_detection,
      tool_cmd_detection: bools.tool_cmd_detection,
      outbound_detection: bools.outbound_detection,
      retry_count: Number(settings.retry_count) || 0,
      log_retention_days: Number(settings.log_retention_days) || 30,
      security_alert_min_severity: settings.security_alert_min_severity,
      disabled_rules: settings.disabled_rules,
      blacklist: parseLines(blacklistText.value),
      whitelist: parseLines(whitelistText.value),
    })
    const s = res.data.settings || {}
    settings.blacklist = [...(s.blacklist || [])]
    settings.whitelist = [...(s.whitelist || [])]
    savedTip.value = true
    setTimeout(() => (savedTip.value = false), 2000)
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '保存失败'
  } finally {
    saving.value = false
  }
}

async function onScan() {
  scanning.value = true
  scanResult.value = null
  try {
    const res = await gatewayScanPreview(scanText.value)
    scanResult.value = res.data
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '扫描失败'
  } finally {
    scanning.value = false
  }
}

// ---- 风险事件闭环 ----
async function loadEvents() {
  eventsLoading.value = true
  try {
    const res = await listGatewaySecurityEvents({ status: eventStatusFilter.value, limit: 50 })
    events.value = res.data.events || []
    eventStats.pending = res.data.pending || 0
    eventStats.unacked_alerts = res.data.unacked_alerts || 0
    resolutions.value = res.data.resolutions || []
    resolutionLabels.value = res.data.resolution_labels || {}
    // 预填每条待处理事件的处理表单（默认动作：已排查处置）
    events.value.forEach((ev) => { if (ev.status === 'pending') ensureResolveForm(ev.id) })
  } catch (e) {
    // 事件加载失败不阻断安全配置页
  } finally {
    eventsLoading.value = false
  }
}

async function onSuggest(ev) {
  suggestingId.value = ev.id
  try {
    const res = await suggestGatewaySecurityEvent(ev.id)
    if (res.data.event) Object.assign(ev, res.data.event)
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '建议生成失败'
  } finally {
    suggestingId.value = ''
  }
}

async function onResolve(ev) {
  const f = resolveForm[ev.id] || { resolution: 'investigated', note: '' }
  resolvingId.value = ev.id
  try {
    const res = await resolveGatewaySecurityEvent(ev.id, { resolution: f.resolution, note: f.note })
    if (res.data.event) Object.assign(ev, res.data.event)
    await loadEvents()
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '处理失败'
  } finally {
    resolvingId.value = ''
  }
}

async function onReopen(ev) {
  try {
    const res = await reopenGatewaySecurityEvent(ev.id)
    if (res.data.event) Object.assign(ev, res.data.event)
    ensureResolveForm(ev.id)
    await loadEvents()
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '重开失败'
  }
}

async function onDeleteEvent(ev) {
  if (!confirm('确认删除该风险事件？删除后不可恢复。')) return
  try {
    await deleteGatewaySecurityEvent(ev.id)
    events.value = events.value.filter((x) => x.id !== ev.id)
    await loadEvents()
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '删除失败'
  }
}

// 轮询未确认告警：达到阈值即弹窗（弹窗只弹一次，确认后不再打扰）
async function pollAlerts() {
  if (alertQueue.value.length) return // 已有弹窗在处理，跳过本轮
  try {
    const res = await listGatewaySecurityEvents({ alert_only: true, limit: 20 })
    const list = res.data.events || []
    if (list.length) { alertQueue.value = list; alertIndex.value = 0 }
  } catch (e) {
    // 轮询失败静默
  }
}

async function onAlertDismiss() {
  // 确认当前弹窗（不再重复弹），前进到下一条
  const cur = alertCurrent.value
  if (cur?.id) { try { await ackGatewaySecurityEvents([cur.id]) } catch (e) { /* 忽略 */ } }
  if (alertIndex.value < alertQueue.value.length - 1) {
    alertIndex.value += 1
  } else {
    alertQueue.value = []; alertIndex.value = 0
    loadEvents()
  }
}

async function onAlertHandle() {
  const cur = alertCurrent.value
  alertQueue.value = []; alertIndex.value = 0
  if (cur?.id) {
    try { await ackGatewaySecurityEvents([cur.id]) } catch (e) { /* 忽略 */ }
    eventStatusFilter.value = 'pending'
    await loadEvents()
    const s = new Set(expanded.value); s.add(cur.id); expanded.value = s
    ensureResolveForm(cur.id)
  }
}

onMounted(() => {
  load()
  loadEvents()
  pollAlerts()
  alertTimer = setInterval(pollAlerts, 15000)
})
onUnmounted(() => { if (alertTimer) clearInterval(alertTimer) })

</script>

<style scoped>
.panel { display: flex; flex-direction: column; gap: 18px; }
.panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.panel-intro h2 { font-size: 16px; font-weight: 700; margin-bottom: 4px; }
.panel-intro p { font-size: 12.5px; color: var(--color-text-secondary); line-height: 1.6; max-width: 780px; }

.card { background: #fffdfb; border: 1px solid var(--color-border); border-radius: 12px; padding: 18px 20px; box-shadow: 0 1px 3px rgba(120,90,50,0.04); }
.card-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
.card-title { font-size: 14.5px; font-weight: 700; margin-bottom: 12px; }
.card-head .card-title { margin-bottom: 0; }
.rule-tools { display: flex; gap: 8px; align-items: center; }

/* 审计模式 */
.mode-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; }
.mode-card { display: flex; gap: 10px; align-items: flex-start; padding: 12px 14px; border: 1px solid var(--color-border); border-radius: 10px; cursor: pointer; transition: all 0.15s; }
.mode-card:hover { border-color: var(--color-primary); }
.mode-card.active { border-color: var(--color-primary); background: var(--color-primary-light); }
.mode-card input { margin-top: 3px; }
.mode-body { display: flex; flex-direction: column; gap: 3px; }
.mode-name { font-size: 13.5px; font-weight: 700; }
.mode-desc { font-size: 11.5px; color: var(--color-text-secondary); line-height: 1.5; }

/* 开关 */
.switch-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px 20px; }
@media (max-width: 760px) { .switch-grid { grid-template-columns: 1fr; } }
.switch-row { display: flex; gap: 10px; align-items: flex-start; cursor: pointer; }
.switch-row input { margin-top: 3px; }
.switch-text { display: flex; flex-direction: column; gap: 2px; font-size: 12.5px; }
.switch-text em { font-size: 11px; color: var(--color-text-secondary); font-style: normal; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field-label { font-size: 12px; color: var(--color-text-secondary); font-weight: 600; }
.field-input { padding: 8px 11px; border: 1px solid var(--color-border); border-radius: 8px; font-size: 13px; background: #fff; color: var(--color-text); outline: none; }
.field-input:focus { border-color: var(--color-primary); }
.field-input.sm { padding: 5px 9px; font-size: 12px; }
textarea.field-input { resize: vertical; font-size: 12px; width: 100%; box-sizing: border-box; }
.mono { font-family: 'SFMono-Regular', Consolas, monospace; }

/* 规则列表 */
.rule-list { display: flex; flex-direction: column; gap: 4px; max-height: 420px; overflow-y: auto; }
.rule-row { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border-radius: 8px; cursor: pointer; font-size: 12.5px; }
.rule-row:hover { background: #faf6f0; }
.rule-row.disabled { opacity: 0.5; }
.sev-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.sev-dot.sev-low { background: #6a9fd8; }
.sev-dot.sev-medium { background: #e0a92e; }
.sev-dot.sev-high { background: #d97b3f; }
.sev-dot.sev-critical { background: #d9534f; }
.rule-name { flex: 1; }
.rule-cat { font-size: 10.5px; color: var(--color-text-secondary); background: #f4efe8; padding: 1px 8px; border-radius: 7px; }
.rule-sev { font-size: 10.5px; font-weight: 700; min-width: 34px; text-align: right; }
.txt-low { color: #3a6ea5; }
.txt-medium { color: #b8802a; }
.txt-high { color: #c1622a; }
.txt-critical { color: #c0392b; }
.rule-gate-off { font-size: 10px; color: #a99c8c; background: #eee9e2; padding: 1px 6px; border-radius: 6px; }

/* 黑白名单 */
.list-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 760px) { .list-grid { grid-template-columns: 1fr; } }
.list-col { display: flex; flex-direction: column; gap: 6px; }
.list-label { font-size: 12px; color: var(--color-text-secondary); font-weight: 600; }

/* 扫描试跑 */
.scan-actions { display: flex; align-items: center; gap: 14px; margin-top: 12px; flex-wrap: wrap; }
.scan-summary { font-size: 12.5px; color: var(--color-text-secondary); display: flex; align-items: center; gap: 6px; }
.scan-findings { display: flex; flex-direction: column; gap: 8px; margin-top: 14px; }
.scan-clean { margin-top: 14px; font-size: 12.5px; color: #3d8b52; }
.finding { border-left: 3px solid var(--color-border); padding: 6px 10px; background: #faf6f0; border-radius: 0 6px 6px 0; }
.finding.sev-low { border-left-color: #6a9fd8; }
.finding.sev-medium { border-left-color: #e0a92e; }
.finding.sev-high { border-left-color: #d97b3f; }
.finding.sev-critical { border-left-color: #d9534f; }
.finding-head { display: flex; align-items: center; gap: 8px; font-size: 12px; flex-wrap: wrap; }
.finding-cat { font-size: 10.5px; color: var(--color-text-secondary); background: #f4efe8; padding: 1px 7px; border-radius: 7px; }
.finding-sev { font-size: 10.5px; font-weight: 700; margin-left: auto; }
.finding-match { margin-top: 5px; font-size: 11.5px; color: #a83226; word-break: break-all; }

.risk-tag { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 8px; }
.risk-none { background: #eee9e2; color: #8a7e72; }
.risk-low { background: #e8f0fb; color: #3a6ea5; }
.risk-medium { background: #fdf0d9; color: #b8802a; }
.risk-high { background: #fbe7d9; color: #c1622a; }
.risk-critical { background: #fbe4e3; color: #c0392b; }

/* 风险事件闭环 */
.card-sub { font-size: 11.5px; color: var(--color-text-secondary); line-height: 1.6; margin: -4px 0 12px; }
.ev-badge { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 8px; background: #fdf0d9; color: #b8802a; margin-left: 6px; }
.ev-badge.alert { background: #fbe4e3; color: #c0392b; }
.ev-list { display: flex; flex-direction: column; gap: 8px; max-height: 560px; overflow-y: auto; }
.ev-card { border: 1px solid var(--color-border); border-left: 3px solid var(--color-border); border-radius: 8px; background: #fffdfb; overflow: hidden; }
.ev-card.sev-low { border-left-color: #6a9fd8; }
.ev-card.sev-medium { border-left-color: #e0a92e; }
.ev-card.sev-high { border-left-color: #d97b3f; }
.ev-card.sev-critical { border-left-color: #d9534f; }
.ev-head { display: flex; align-items: center; gap: 10px; padding: 10px 12px; cursor: pointer; flex-wrap: wrap; }
.ev-head:hover { background: #faf6f0; }
.ev-title { font-size: 12.5px; font-weight: 700; }
.ev-meta { font-size: 11.5px; color: var(--color-text-secondary); }
.ev-time { font-size: 11px; color: var(--color-text-secondary); margin-left: auto; }
.ev-state { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 8px; }
.ev-state.pending { background: #fdf0d9; color: #b8802a; }
.ev-state.done { background: #e4f4e8; color: #3d8b52; }
.ev-caret { font-size: 11px; color: var(--color-text-secondary); }
.ev-body { padding: 4px 12px 14px; border-top: 1px solid #f4efe8; }
.ev-findings { display: flex; flex-direction: column; gap: 8px; margin: 12px 0; }
.ev-suggest { background: #f7f2ea; border-radius: 8px; padding: 10px 12px; margin-bottom: 12px; }
.ev-suggest-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 6px; }
.ev-suggest-head strong { font-size: 12.5px; }
.ev-suggest-text { font-size: 12px; line-height: 1.7; color: var(--color-text); white-space: pre-wrap; margin: 0; }
.ev-suggest-empty { font-size: 11.5px; color: var(--color-text-secondary); margin: 0; }
.ev-resolve { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ev-note { flex: 1; min-width: 160px; }
.ev-resolved { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 11.5px; color: var(--color-text-secondary); }
.ev-resolved strong { color: #3d8b52; }
.ev-note-text { font-style: italic; }

/* 弹窗告警 */
.modal-mask { position: fixed; inset: 0; background: rgba(45,42,36,0.4); display: flex; align-items: center; justify-content: center; z-index: 200; padding: 20px; }
.modal { background: #fffdfb; border-radius: 14px; padding: 22px 24px; width: 100%; max-width: 560px; max-height: 86vh; overflow-y: auto; box-shadow: 0 8px 30px rgba(0,0,0,0.22); }
.alert-modal { border-top: 4px solid #d9534f; }
.alert-modal.sev-medium { border-top-color: #e0a92e; }
.alert-modal.sev-high { border-top-color: #d97b3f; }
.alert-modal.sev-low { border-top-color: #6a9fd8; }
.modal-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
.modal-title { font-size: 16px; font-weight: 700; margin: 0; }
.alert-count { font-size: 11.5px; color: var(--color-text-secondary); }
.alert-top { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
.alert-meta { font-size: 12px; color: var(--color-text-secondary); }
.alert-time { font-size: 11px; color: var(--color-text-secondary); margin-left: auto; }
.alert-findings { display: flex; flex-direction: column; gap: 8px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 18px; }

.btn { padding: 8px 16px; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer; border: 1px solid transparent; transition: all 0.15s; white-space: nowrap; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn.sm { padding: 5px 12px; font-size: 12px; }
.btn-primary { background: var(--color-primary); color: #fff; }
.btn-primary:hover:not(:disabled) { background: #b87a3f; }
.btn-ghost { background: transparent; border-color: var(--color-border); color: var(--color-text); }
.btn-ghost:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
.err-tip { color: #d9534f; font-size: 12.5px; }
.ok-tip { color: #4a9d5f; font-size: 12.5px; }
.mini-loading, .mini-empty { color: var(--color-text-secondary); font-size: 13px; padding: 32px; text-align: center; }
</style>
