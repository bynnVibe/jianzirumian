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
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { getGatewayRules, getGatewaySettings, saveGatewaySettings, gatewayScanPreview } from '@/api'

const loading = ref(true)
const saving = ref(false)
const errorMsg = ref('')
const savedTip = ref(false)

const rules = ref([])
const categoryLabels = ref({})
const ruleFilter = ref('')
const settings = reactive({ audit_mode: 'audit', retry_count: 2, log_retention_days: 30, disabled_rules: [], blacklist: [], whitelist: [] })
const bools = reactive({ unicode_detection: true, tool_cmd_detection: true, outbound_detection: true })
const blacklistText = ref('')
const whitelistText = ref('')

const scanText = ref('')
const scanning = ref(false)
const scanResult = ref(null)

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

onMounted(load)
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
