<template>
  <div class="tool-governance">
    <div class="tg-inner">
    <header class="page-head">
      <div>
        <h1>工具治理中心</h1>
        <p class="sub">个人工具生命周期评测工作流的唯一集中入口。聊天 <code>@tool</code> 发起后，这里按六步工作流跑完并展示全部配置与评测记录；工具须样例测试与标准化评测全部通过、启用且就绪后才进入注册表。注册工具默认全员可调用，可按工具设置用户黑名单。</p>
      </div>
      <button :disabled="busy" @click="load">刷新</button>
    </header>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notice" class="notice" role="status">{{ notice }}</p>

    <!-- ============ 全局就绪仪表盘：把聊天中才暴露的失败根因前置显性化 ============ -->
    <section class="panel">
      <h2>全局就绪</h2>
      <div class="ready-grid">
        <div v-for="item in overview?.readiness || []" :key="item.key" class="ready-card" :class="item.ok ? 'ok' : 'bad'">
          <strong><span class="dot"></span>{{ item.label }}</strong>
          <p class="detail">{{ item.detail }}</p>
          <p v-if="!item.ok" class="hint">{{ item.hint }}
            <router-link v-if="item.link && auth.canOps" :to="item.link">前往配置 →</router-link>
          </p>
        </div>
      </div>
    </section>

    <!-- ============ 生命周期工作流图例 ============ -->
    <section class="panel">
      <h2>工具生命周期评测工作流</h2>
      <ol class="lifecycle">
        <li>聊天 <code>@tool</code> 添加并确认草稿</li>
        <li>补齐审批／条款／凭据／隔离配置</li>
        <li>只读样例测试</li>
        <li>标准化评测（自动发起，可复跑）</li>
        <li>启用</li>
        <li class="final">注册表显示 · 全员可调用（黑名单除外）</li>
      </ol>
      <p class="hint">管理员在聊天输入 <code>@tool 功能描述或公开 API 文档链接</code> 添加；普通用户通过 <code>@skills</code> 派生工具。展开任一工具即可看到六步工作流的逐步状态，全部配置与评测记录内嵌在对应步骤中；「运行完整工作流」可一键重跑 测试→评测→启用 全链路。</p>
    </section>

    <!-- ============ 待确认草稿：引导回聊天完成确认 ============ -->
    <section v-if="overview?.pending_drafts?.length" class="panel">
      <h2>待确认草稿 <small>{{ overview.pending_drafts.length }}</small></h2>
      <p v-for="d in overview.pending_drafts" :key="d.id" class="draft-line">
        <strong>{{ d.name }}</strong> · {{ d.created_at }}
        <router-link :to="'/chat/' + d.session_id">回到聊天确认添加 →</router-link>
      </p>
    </section>

    <!-- ============ 工具生命周期工作流列表（管理员/运维可见全部） ============ -->
    <section class="panel">
      <h2>全部工具 <small>{{ tools.length }}</small></h2>
      <p v-if="!tools.length && !busy" class="hint">暂无个人工具。在聊天中用 <code>@tool</code>（管理员）或 <code>@skills</code> 派生添加后，这里会显示完整工作流进度。</p>
      <article v-for="tool in tools" :key="tool.id" :id="'tool-' + tool.id">
        <div class="row">
          <strong>{{ tool.name }}</strong>
          <span class="stage" :class="'stage-' + tool.stage">{{ tool.stage_label }}</span>
          <span class="owner">属主 {{ tool.owner_name }}</span>
          <button :disabled="busy" @click="open(tool)">{{ expandedId === tool.id ? '收起工作流' : '详情与工作流' }}</button>
          <button v-if="canManage(tool)" :disabled="busy || !!workflowRunning || ['testing', 'evaluating'].includes(tool.stage)" @click="runWorkflow(tool)">{{ workflowRunning === tool.id ? '工作流运行中…' : '运行完整工作流' }}</button>
        </div>
        <p class="meta-line">功能名 <code>{{ tool.function_name }}</code> · 标准化评测：{{ evalLine(tool) }} · 执行 {{ tool.exec_stats?.total || 0 }} 次<template v-if="tool.exec_stats?.success_rate != null">（完成率 {{ Math.round(tool.exec_stats.success_rate * 100) }}%）</template> · 调用范围：{{ tool.blocked_users?.length ? `全员除 ${tool.blocked_users.length} 人` : '全员' }}</p>

        <!-- ============ 六步工作流：全部配置与评测记录内嵌对应步骤 ============ -->
        <div v-if="expandedId === tool.id && monitoring" class="workflow">
          <!-- 步骤 1：创建与确认（规格／执行器／版本记录） -->
          <section class="wf-step" :class="wfState(1)">
            <header class="wf-head"><span class="wf-num">1</span><strong>创建与确认</strong><em class="wf-state">{{ wfStateText(1) }}</em></header>
            <div class="wf-body">
              <p class="meta-line">聊天草稿已确认安装 · 功能名 <code>{{ monitoring.tool.function_name }}</code> · 当前版本 <code>{{ monitoring.tool.version }}</code> · 执行器 {{ monitoring.tool.spec?.executor }}</p>
              <ToolSpecDetails :spec="monitoring.tool.spec" :review="monitoring.tool.review" />
              <details><summary>版本记录（{{ (monitoring.tool.versions || []).length }}）</summary>
                <ul><li v-for="v in monitoring.tool.versions || []" :key="v.version"><code>{{ v.version }}</code> · {{ v.created_at }}<template v-if="v.approved_at"> · 已审批 {{ v.approved_at }}</template></li></ul>
              </details>
            </div>
          </section>

          <!-- 步骤 2：配置与审批（阻塞项／条款／凭据／隔离配置） -->
          <section class="wf-step" :class="wfState(2)">
            <header class="wf-head"><span class="wf-num">2</span><strong>配置与审批</strong><em class="wf-state">{{ wfStateText(2) }}</em></header>
            <div class="wf-body">
              <p v-if="monitoring.tool.blockers?.length" class="warning">待处理：{{ monitoring.tool.blockers.map(blockerText).join('；') }}</p>
              <p v-else class="hint">审批／条款／凭据／隔离配置均已就绪。</p>
              <p class="meta-line">版本审批：{{ monitoring.tool.approved ? '本版本已审批' : (monitoring.tool.review?.requires_admin ? '待管理员审批（见下方待审区）' : '无需审批') }}</p>
              <template v-if="canManage(monitoring.tool)">
                <label v-if="monitoring.tool.review?.requires_terms"><input v-model="terms" type="checkbox" :disabled="busy" />仅用于非商业用途，遵守 Open-Meteo 免费条款、CC-BY 4.0 署名和限额</label>
                <button v-if="monitoring.tool.review?.requires_terms" :disabled="busy" @click="configure(monitoring.tool, { accept_terms: terms })">保存条款选择</button>
                <form v-if="monitoring.tool.spec?.config?.auth" @submit.prevent="saveCredential">
                  <label>API 凭据（仅加密保存，不放入聊天；调用时始终使用属主凭据）<input v-model="credential" type="password" autocomplete="new-password" maxlength="8192" :disabled="busy" /></label>
                  <button :disabled="busy || !credential">保存凭据并要求重新测试</button>
                  <span>{{ monitoring.tool.credential_configured ? '已配置，无法回读原文' : '尚未配置' }}</span>
                </form>
              </template>
              <details v-if="monitoring.tool.spec?.executor === 'python'"><summary>隔离运行配置（变化后须重新审批）</summary>
                <pre>{{ JSON.stringify(monitoring.tool.runtime_profile || {}, null, 2) }}</pre>
              </details>
            </div>
          </section>

          <!-- 步骤 3：只读样例测试（执行入口 + 测试历史） -->
          <section class="wf-step" :class="wfState(3)">
            <header class="wf-head"><span class="wf-num">3</span><strong>只读样例测试</strong><em class="wf-state">{{ wfStateText(3) }}</em></header>
            <div class="wf-body">
              <div v-if="canManage(monitoring.tool)" class="row">
                <button :disabled="busy || !!testing || !!workflowRunning" @click="runTest(monitoring.tool)">{{ testing === monitoring.tool.id ? '测试中，可刷新记录并取消' : '执行只读样例测试' }}</button>
                <button :disabled="busy" @click="readMonitoring(monitoring.tool.id)">刷新详情与执行状态</button>
              </div>
              <p v-if="!monitoring.tests.length" class="hint">尚无样例测试记录。确认草稿后自动执行一次；也可手动重跑。</p>
              <ul v-else class="record-list">
                <li v-for="t in monitoring.tests" :key="t.id">{{ stateText(t.status) }} · 版本 <code>{{ t.version }}</code> · {{ t.created_at }}<pre>{{ t.report_json }}</pre></li>
              </ul>
            </div>
          </section>

          <!-- 步骤 4：标准化评测（逐用例真实调用 + 断言校验） -->
          <section class="wf-step" :class="wfState(4)">
            <header class="wf-head"><span class="wf-num">4</span><strong>标准化评测</strong><em class="wf-state">{{ wfStateText(4) }}</em></header>
            <div class="wf-body">
              <div v-if="canManage(monitoring.tool)" class="row">
                <button :disabled="busy || monitoring.tool.eval_status === 'running' || !!workflowRunning" @click="reeval(monitoring.tool)">发起／复跑标准化评测</button>
              </div>
              <p v-if="!monitoring.eval?.results?.length" class="hint">尚无评测记录。样例测试通过后系统自动发起；也可手动复跑。</p>
              <template v-else>
                <p class="meta-line">数据集 <code>{{ monitoring.eval.dataset_id }}</code> · 运行 <code>{{ monitoring.eval.run_id }}</code><template v-if="monitoring.eval.summary?.pass_rate != null"> · 通过率 {{ Math.round(monitoring.eval.summary.pass_rate * 100) }}%</template></p>
                <ul class="case-list">
                  <li v-for="r in monitoring.eval.results" :key="r.id" :class="{ 'case-bad': r.status !== 'ok' }">
                    <span class="case-status">{{ r.status === 'ok' ? '通过' : '未通过' }}</span>
                    <span class="case-q" :title="r.question">{{ r.question }}</span>
                    <span class="case-latency">{{ r.latency_ms }}ms</span>
                    <p v-if="r.error" class="error">{{ r.error }}</p>
                    <p v-if="r.expected_outcome" class="hint">断言：{{ r.expected_outcome }}</p>
                  </li>
                </ul>
              </template>
            </div>
          </section>

          <!-- 步骤 5：启用 -->
          <section class="wf-step" :class="wfState(5)">
            <header class="wf-head"><span class="wf-num">5</span><strong>启用</strong><em class="wf-state">{{ wfStateText(5) }}</em></header>
            <div class="wf-body">
              <p class="hint">{{ monitoring.tool.enabled ? '工具已启用；评测通过即进入注册表可被聊天调用。' : '工具未启用。评测全部通过后启用即完成注册。' }}</p>
              <button v-if="canManage(monitoring.tool)" :disabled="busy || (!monitoring.tool.enabled && !['ready', 'disabled'].includes(monitoring.tool.status))" @click="configure(monitoring.tool, { enabled: !monitoring.tool.enabled })">{{ monitoring.tool.enabled ? '停用' : '启用' }}</button>
            </div>
          </section>

          <!-- 步骤 6：注册与调用（注册状态 + 用户黑名单 + 最近执行记录） -->
          <section class="wf-step" :class="wfState(6)">
            <header class="wf-head"><span class="wf-num">6</span><strong>注册与调用</strong><em class="wf-state">{{ wfStateText(6) }}</em></header>
            <div class="wf-body">
              <p class="meta-line">{{ monitoring.tool.stage === 'registered' ? '已进入工具注册表，默认全员可调用。' : '尚未注册：须评测通过、启用且就绪。' }}</p>
              <div class="blocked-box">
                <strong>调用黑名单</strong>
                <p class="hint">默认所有用户都可调用；勾选的用户将在聊天目录与执行门禁中被拒绝（属主不受影响）。</p>
                <template v-if="auth.isAdmin">
                  <div v-if="users.length" class="blocked-grid">
                    <label v-for="u in users.filter(u => u.id !== monitoring.tool.owner_id)" :key="u.id" class="blocked-user">
                      <input type="checkbox" :value="u.id" v-model="blockedDraft" :disabled="busy" />{{ u.username }}<em class="owner">{{ u.role }}</em>
                    </label>
                  </div>
                  <p v-else class="hint">用户列表加载中…</p>
                  <button :disabled="busy" @click="saveBlocked">保存黑名单</button>
                </template>
                <p v-else class="meta-line">{{ blockedNames.length ? '已限制：' + blockedNames.join('、') : '未限制任何用户（全员可调用）' }}</p>
              </div>
              <details open><summary>最近执行记录（{{ monitoring.executions.length }}）</summary>
                <ul class="record-list">
                  <li v-for="run in monitoring.executions" :key="run.id">{{ stateText(run.execution_status) }} · 调用者 {{ run.caller_name || run.owner_id }} · 数据 {{ run.validation_status }} · 需求 {{ run.requirement_status }} · {{ run.created_at }}
                    <button v-if="canManage(monitoring.tool) && ['running', 'cancellation_requested', 'cleanup_failed'].includes(run.execution_status)" :disabled="busy" @click="cancelRun(run)">取消／核验终止</button>
                  </li>
                </ul>
              </details>
            </div>
          </section>
        </div>
      </article>
    </section>

    <!-- ============ 管理员审批区 ============ -->
    <section v-if="overview?.is_admin" class="panel">
      <h2>管理员：待审工具版本 <small>{{ reviews.length }}</small></h2>
      <p class="hint">须核对真实文档、固定目标与参数权限。脚本还需独立隔离 Worker；审批不代表样例通过。</p>
      <article v-for="item in reviews" :key="item.tool_id + item.version">
        <p>属主 {{ item.owner_id }} · 版本 <code>{{ item.version }}</code></p>
        <ToolSpecDetails :spec="item.spec" :review="item.review" />
        <details v-if="item.spec.executor === 'python'"><summary>本次审批绑定的隔离运行配置（变化后须重新审批）</summary><pre>{{ JSON.stringify(item.runtime_profile, null, 2) }}</pre><p v-if="!item.profile_hash">尚未配置固定镜像，审批后仍不能运行。</p></details>
        <label><input v-model="approvals[item.tool_id]" type="checkbox" :disabled="busy" />已核验上述版本只读无外部副作用；不执行文档安装指令或脚本</label>
        <button :disabled="busy || !approvals[item.tool_id]" @click="approve(item)">批准此版本（仍需属主测试）</button>
      </article>
      <p v-if="!reviews.length" class="hint">当前没有待审批版本。</p>
    </section>

    <!-- ============ 工具注册表：仅评测通过且启用就绪的工具才显示 ============ -->
    <section class="panel">
      <h2>工具注册表 <small>{{ registryTools.length }}</small></h2>
      <p class="hint">内置工具 + 标准化评测通过的个人工具。未通过评测、被阻塞或停用的工具不会出现在这里，也不能被聊天调用。个人工具以功能名注册、默认全员可调用；管理员可在「调用范围」列直接限制用户（工作流第 6 步同样可管理）。</p>
      <div class="registry-scroll">
        <table class="registry-table">
          <thead>
            <tr><th>工具</th><th>命名空间</th><th>属主</th><th>调用范围</th><th>标签</th><th>适用场景</th><th>风险</th><th>状态</th><th>参数</th></tr>
          </thead>
          <tbody>
            <tr v-for="t in registryTools" :key="t.name">
              <td :title="t.description">{{ t.display_name || t.name }}<code v-if="t.source === 'personal_tool'" class="fn-code" title="功能名标识（聊天调用名）">{{ t.name }}</code><em v-if="t.source === 'personal_tool'" class="chip personal" title="个人工具：样例测试 + 标准化评测通过后自动注册">个人工具</em></td>
              <td><span class="chip ns">{{ t.namespace }}</span></td>
              <td>{{ t.source === 'personal_tool' ? (t.owner_name || t.owner_id) : '系统' }}</td>
              <td>{{ t.source === 'personal_tool' && t.blocked_users?.length ? `全员除 ${t.blocked_users.length} 人` : '全员' }}<button v-if="t.source === 'personal_tool' && auth.isAdmin" class="mini-btn" :disabled="busy" @click="openRestrict(t)">限制</button></td>
              <td><em v-for="tag in (t.tags || []).slice(0, 3)" :key="tag" class="chip">{{ tag }}</em><em v-if="(t.tags || []).length > 3" class="chip">+{{ (t.tags || []).length - 3 }}</em></td>
              <td class="scene" :title="(t.scenarios || []).join('；')">{{ (t.scenarios || []).join('；') || '-' }}</td>
              <td>{{ riskText(t.risk_level) }}</td>
              <td>{{ t.status === 'active' ? '可用' : t.status }}</td>
              <td><em v-for="p in schemaParams(t.parameters).slice(0, 3)" :key="p" class="chip param">{{ p }}</em><em v-if="schemaParams(t.parameters).length > 3" class="chip">+{{ schemaParams(t.parameters).length - 3 }}</em><span v-if="!schemaParams(t.parameters).length">无参数</span></td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- ============ 注册表调用限制弹层：按工具勾选不可调用用户 ============ -->
    <div v-if="restrictTarget" class="modal-overlay" @click.self="restrictTarget = null">
      <div class="modal-box restrict-box" role="dialog" aria-label="工具调用限制">
        <h2>调用限制 · {{ restrictTarget.display_name || restrictTarget.name }}</h2>
        <p class="hint">默认所有用户都可调用 <code>{{ restrictTarget.name }}</code>；勾选的用户将在聊天目录与执行门禁中被拒绝（属主不受影响）。</p>
        <div v-if="users.length" class="blocked-grid">
          <label v-for="u in users.filter(u => u.id !== restrictTarget.owner_id)" :key="u.id" class="blocked-user">
            <input type="checkbox" :value="u.id" v-model="restrictDraft" :disabled="busy" />{{ u.username }}<em class="owner">{{ u.role }}</em>
          </label>
        </div>
        <p v-else class="hint">用户列表加载中…</p>
        <div class="row">
          <button :disabled="busy" @click="saveRestrict">保存限制</button>
          <button :disabled="busy" @click="restrictTarget = null">取消</button>
        </div>
      </div>
    </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, onActivated, onDeactivated, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useQueryCacheStore } from '@/stores/queryCache'
import { getToolGovernanceOverview, getToolGovernanceRegistry, getToolMonitoring, governanceTestTool,
  governanceReevalTool, governanceConfigureTool, governanceSetCredential, governanceApproveTool, governanceCancelExecution,
  governanceRunWorkflow, listUsers } from '@/api'
import ToolSpecDetails from '@/components/ToolSpecDetails.vue'

const auth = useAuthStore()
const route = useRoute()
const overview = ref(null), registryTools = ref([]), monitoring = ref(null), expandedId = ref('')
const busy = ref(false), error = ref(''), notice = ref(''), terms = ref(false), credential = ref(''), testing = ref('')
const approvals = ref({}), workflowRunning = ref(''), blockedDraft = ref([]), users = ref([])
const restrictTarget = ref(null), restrictDraft = ref([])
let pollTimer = null, usersLoaded = false

const tools = computed(() => overview.value?.tools || [])
const reviews = computed(() => (overview.value?.reviews || []).map(r => ({ ...r, spec: JSON.parse(r.spec_json), review: JSON.parse(r.review_json) })))
const isMine = tool => tool.owner_id === auth.user?.id
// 治理页仅管理员/运维可见：管理员可代管全部工具，属主可管理自己的工具，运维只读
const canManage = tool => !!tool && (isMine(tool) || auth.isAdmin)
const blockedNames = computed(() => {
  const ids = monitoring.value?.tool?.blocked_users || []
  return ids.map(id => users.value.find(u => u.id === id)?.username || id)
})
const stateText = value => ({ ready: '已就绪', needs_config: '待配置或测试', testing: '测试中', disabled: '已停用', passed: '通过', failed: '失败', blocked: '被阻止', cancelled: '已取消', interrupted: '已中断', running: '执行中', completed: '执行结束', timeout: '超时', cancellation_requested: '取消待确认', cleanup_failed: '隔离清理未确认' }[value] || value)
const blockerText = value => ({ admin_review_required: '管理员版本审批', noncommercial_terms_required: '确认非商业条款', credentials_required: '配置服务端主密钥及 API 凭据', sandbox_unavailable: '隔离 Worker 尚不可用，禁止主机执行', version_changed: '版本变化，需重新确认', invalid_stored_spec: '已存工具声明损坏，已阻止执行，请重新创建并确认' }[value] || value)
const riskText = value => ({ low: '低', medium: '中', high: '高' }[value] || value || '-')
const schemaParams = parameters => Object.keys(parameters?.properties || {})
function evalLine(tool) {
  const s = tool.eval_summary || {}
  if (s.pass_rate != null) return `通过率 ${Math.round(s.pass_rate * 100)}%（${s.status === 'completed' ? '已完成' : stateText(s.status)}）`
  return ({ passed: '已通过', running: '进行中（真实调用 + 断言校验）', failed: '未通过，请查看逐用例结果并修复后复跑' }[tool.eval_status] || '尚未发起（样例测试通过后自动开始）')
}

// ---- 六步工作流状态机：由工具阶段 + 监控记录推导，不另造状态 ----
const currentTestPassed = computed(() => {
  const t = monitoring.value?.tool
  return !!t && (monitoring.value.tests || []).some(x => x.version === t.version && x.status === 'passed')
})
function wfState(step) {
  const t = monitoring.value?.tool
  if (!t) return 'pending'
  const ev = t.eval_status || ''
  switch (step) {
    case 1: return 'done'
    case 2: return t.blockers?.length ? 'blocked' : 'done'
    case 3:
      if (t.blockers?.length) return 'pending'
      if (t.status === 'testing') return 'active'
      return currentTestPassed.value ? 'done' : 'active'
    case 4:
      if (ev === 'running') return 'active'
      if (ev === 'passed') return 'done'
      if (ev === 'failed') return 'blocked'
      return 'pending'
    case 5: return t.enabled ? 'done' : (ev === 'passed' ? 'active' : 'pending')
    case 6: return t.stage === 'registered' ? 'done' : 'pending'
    default: return 'pending'
  }
}
const wfStateText = step => ({ done: '已完成', active: '进行中', blocked: '需处理', pending: '待前置步骤' }[wfState(step)])

async function act(fn) {
  busy.value = true; error.value = ''; notice.value = ''
  try { await fn() }
  catch (err) { error.value = typeof err.response?.data?.detail === 'string' ? err.response.data.detail : '请求未完成，请刷新状态核验，不要假定操作已成功。' }
  finally { busy.value = false }
}
// 治理总览是实时执行状态（不缓存）；注册表是稳定元数据，经统一查询缓存
async function load(force = 0) {
  await act(async () => {
    const queryCache = useQueryCacheStore()
    const [ov, reg] = await Promise.all([
      getToolGovernanceOverview(),
      queryCache.cachedGet('tools', { kind: 'governance-registry' },
        async () => (await getToolGovernanceRegistry()).data,
        { force: !!force, onRevalidate: (d) => { registryTools.value = d.tools || [] } }),
    ])
    overview.value = ov.data
    registryTools.value = reg.tools || []
    approvals.value = {}
    if (expandedId.value) await readMonitoring(expandedId.value, true)
  })
  schedulePoll()
}
function schedulePoll() {
  clearTimeout(pollTimer)
  // 测试／评测／工作流进行中时自动轮询，让阶段流转可见（force 绕过缓存）
  if (workflowRunning.value || tools.value.some(t => ['testing', 'evaluating'].includes(t.stage))) {
    pollTimer = setTimeout(() => { if (!busy.value) load(1) }, 5000)
  }
}
async function readMonitoring(id, silent = false) {
  const run = async () => {
    monitoring.value = (await getToolMonitoring(id)).data
    terms.value = !!monitoring.value.tool.terms_accepted
    blockedDraft.value = [...(monitoring.value.tool.blocked_users || [])]
  }
  if (silent) { try { await run() } catch { /* 轮询失败不打断 */ } }
  else await act(run)
}
async function loadUsers() {
  if (usersLoaded || !auth.isAdmin) return
  try { users.value = (await listUsers()).data?.users || []; usersLoaded = true } catch { /* 非关键路径 */ }
}
async function open(tool) {
  credential.value = ''
  if (expandedId.value === tool.id) { expandedId.value = ''; monitoring.value = null; return }
  expandedId.value = tool.id
  await readMonitoring(tool.id)
  loadUsers()
  document.getElementById('tool-' + tool.id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}
async function refreshAfter(id) {
  await load(1)
  if (expandedId.value === id) await readMonitoring(id, true)
}
async function configure(tool, body) { await act(async () => { await governanceConfigureTool(tool.id, body); await refreshAfter(tool.id) }) }
async function saveBlocked() {
  const id = monitoring.value.tool.id
  await act(async () => {
    await governanceConfigureTool(id, { blocked_users: blockedDraft.value })
    await refreshAfter(id)
    notice.value = '调用黑名单已保存；被限制用户的聊天目录与执行门禁即时生效。'
  })
}
// 注册表「调用范围」列直接限制用户：与工作流第 6 步同一配置写点
function openRestrict(t) {
  restrictTarget.value = t
  restrictDraft.value = [...(t.blocked_users || [])]
  loadUsers()
}
async function saveRestrict() {
  const t = restrictTarget.value
  await act(async () => {
    await governanceConfigureTool(t.tool_id, { blocked_users: restrictDraft.value })
    restrictTarget.value = null
    await load(1)
    notice.value = '调用限制已保存；被限制用户的聊天目录与执行门禁即时生效。'
  })
}
async function saveCredential() {
  const id = monitoring.value.tool.id
  await act(async () => {
    try { await governanceSetCredential(id, credential.value) }
    finally { credential.value = '' }
    await refreshAfter(id)
    notice.value = '凭据已加密保存，请重新运行本版本样例测试。'
  })
}
async function runTest(tool) {
  if (testing.value) return
  testing.value = tool.id; error.value = ''; notice.value = '样例测试进行中，可刷新执行记录并取消正在运行的任务。'
  try {
    await governanceTestTool(tool.id)
    await refreshAfter(tool.id)
    notice.value = '测试请求已结束，请以版本测试结果与评测进度为准。'
  } catch (err) {
    error.value = typeof err.response?.data?.detail === 'string' ? err.response.data.detail : '测试连接结束，请刷新记录核验最终状态。'
  } finally { testing.value = '' }
}
async function runWorkflow(tool) {
  if (workflowRunning.value) return
  workflowRunning.value = tool.id; error.value = ''
  notice.value = '完整工作流运行中：样例测试 → 标准化评测 → 启用注册；页面自动刷新各步骤进度。'
  try {
    const res = await governanceRunWorkflow(tool.id)
    await refreshAfter(tool.id)
    notice.value = res.data?.stage === 'registered'
      ? '工作流已完成：评测全部通过，工具已注册，默认全员可调用。'
      : '工作流请求已结束，请以各步骤记录为准；评测仍在进行时页面会继续自动刷新。'
  } catch (err) {
    error.value = typeof err.response?.data?.detail === 'string' ? err.response.data.detail : '工作流连接结束，请刷新各步骤记录核验最终状态。'
  } finally { workflowRunning.value = ''; schedulePoll() }
}
async function reeval(tool) {
  await act(async () => {
    await governanceReevalTool(tool.id)
    await refreshAfter(tool.id)
    notice.value = '标准化评测已发起，页面会自动刷新进度；全部通过后进入工具注册表。'
  })
}
async function cancelRun(run) {
  await act(async () => { await governanceCancelExecution(run.id); notice.value = '已请求取消；请刷新执行状态确认终止。'; await refreshAfter(monitoring.value.tool.id) })
}
async function approve(item) {
  await act(async () => {
    await governanceApproveTool(item.tool_id, item.owner_id, item.version, item.profile_hash)
    await load(1)
    notice.value = '版本已批准，仍须属主完成配置和只读样例测试。'
  })
}
onMounted(async () => {
  await load()
  const focus = route.query.focus
  if (focus && tools.value.some(t => t.id === focus)) await open({ id: focus })
})
onUnmounted(() => clearTimeout(pollTimer))
// KeepAlive：停用视图暂停轮询，回页重新拉取实时状态
onDeactivated(() => clearTimeout(pollTimer))
onActivated(() => { if (overview.value) load() })
</script>

<style scoped>
.tool-governance { /* 父容器 .main-content 为 100vh + overflow:hidden，页面需自身承担滚动 */ flex: 1; min-height: 0; height: 100%; overflow-y: auto; color: #53493c; font-size: 13px; line-height: 1.7; }
.tg-inner { max-width: 1080px; margin: 0 auto; padding: 24px 20px 60px; }
.page-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
h1 { font-size: 22px; margin: 0 0 4px; color: #5d4a33; }
.sub { margin: 0; color: #82725c; max-width: 720px; }
h2 { font-size: 16px; margin: 0 0 10px; color: #6b5031; } small { color: #91816b; font-weight: normal; }
.panel { margin: 18px 0; padding: 18px; background: #fffaf3; border: 1px solid #e0d6c7; border-radius: 12px; min-width: 0; }
button { padding: 6px 12px; border-radius: 7px; background: #fffefb; border: 1px solid #d6c5aa; color: #705335; cursor: pointer; font-size: 13px; }
button:disabled { opacity: .55; cursor: not-allowed; }
.error { color: #a7352b; } .notice { color: #4c6b3c; } .warning { color: #95611f; } .hint { color: #82725c; }
a { color: #80613e; }
code { background: #f4ead8; padding: 1px 5px; border-radius: 4px; font-size: 12px; }
.ready-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }
.ready-card { padding: 12px; border-radius: 10px; border: 1px solid #e0d6c7; background: #fffefb; }
.ready-card strong { display: flex; align-items: center; gap: 7px; color: #5d4a33; }
.ready-card .dot { width: 9px; height: 9px; border-radius: 50%; background: #b9a888; flex: none; }
.ready-card.ok .dot { background: #6f9b5a; } .ready-card.bad .dot { background: #c0563f; }
.ready-card.bad { border-color: #e3b9a8; background: #fdf6f0; }
.ready-card .detail { margin: 6px 0 0; overflow-wrap: anywhere; }
.ready-card .hint { margin: 6px 0 0; font-size: 12px; }
.lifecycle { display: flex; flex-wrap: wrap; gap: 6px 0; margin: 0; padding: 0; list-style: none; counter-reset: step; }
.lifecycle li { counter-increment: step; display: flex; align-items: center; gap: 6px; padding: 4px 10px; }
.lifecycle li::before { content: counter(step); width: 20px; height: 20px; border-radius: 50%; background: #eadfc8; color: #6b5031; display: inline-flex; align-items: center; justify-content: center; font-size: 12px; flex: none; }
.lifecycle li:not(:last-child)::after { content: '→'; color: #b9a888; margin-left: 8px; }
.lifecycle li.final { color: #4c6b3c; font-weight: 600; }
.lifecycle li.final::before { background: #dbe8d2; color: #4c6b3c; }
.draft-line { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 6px 0; }
article { padding: 12px 0; border-top: 1px solid #e8dfd1; overflow-wrap: anywhere; }
.row { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin: 8px 0; }
.stage { padding: 2px 9px; border-radius: 20px; font-size: 12px; background: #efe6d4; color: #6b5031; }
.stage-registered { background: #dbe8d2; color: #3f6230; }
.stage-blocked, .stage-eval_failed { background: #f3d9cd; color: #97402c; }
.stage-testing, .stage-evaluating { background: #f6e7c6; color: #8a6420; }
.owner { color: #91816b; font-size: 12px; }
.meta-line { margin: 4px 0 0; color: #6b5b45; }
label { display: flex; align-items: flex-start; gap: 7px; margin: 10px 0; }
form { margin: 12px 0; padding: 10px; background: #f4f2ed; border-radius: 8px; } form label { display: block; }
input[type=password] { display: block; max-width: 100%; width: 320px; padding: 7px; border: 1px solid #d6c5aa; border-radius: 5px; margin-top: 4px; }
summary { cursor: pointer; margin: 10px 0; color: #6b5031; }
pre { max-width: 100%; overflow: auto; background: #f7f1e4; padding: 8px; border-radius: 6px; font-size: 12px; }
ul { margin: 6px 0; padding-left: 18px; } li { margin: 4px 0; }
.record-list pre { margin: 4px 0 0; }
.case-list { list-style: none; padding-left: 0; }
.case-list li { padding: 8px 10px; border: 1px solid #e8dfd1; border-radius: 8px; background: #fffefb; display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; }
.case-list li.case-bad { border-color: #e3b9a8; background: #fdf6f0; }
.case-status { font-weight: 600; color: #4c6b3c; } .case-bad .case-status { color: #97402c; }
.case-q { flex: 1; min-width: 180px; } .case-latency { color: #91816b; font-size: 12px; }
.case-list p { width: 100%; margin: 2px 0 0; }
/* ---- 六步工作流管线 ---- */
.workflow { margin: 10px 0 4px; border-left: 2px solid #e0d6c7; padding-left: 14px; display: flex; flex-direction: column; gap: 10px; }
.wf-step { border: 1px solid #e0d6c7; border-radius: 10px; background: #fffefb; padding: 10px 12px; }
.wf-step.done { border-color: #cfdcc4; }
.wf-step.active { border-color: #e2c98f; background: #fdf9ef; }
.wf-step.blocked { border-color: #e3b9a8; background: #fdf6f0; }
.wf-step.pending { opacity: .78; }
.wf-head { display: flex; align-items: center; gap: 8px; }
.wf-num { width: 22px; height: 22px; border-radius: 50%; background: #eadfc8; color: #6b5031; display: inline-flex; align-items: center; justify-content: center; font-size: 12px; flex: none; }
.wf-step.done .wf-num { background: #dbe8d2; color: #3f6230; }
.wf-step.active .wf-num { background: #f6e7c6; color: #8a6420; }
.wf-step.blocked .wf-num { background: #f3d9cd; color: #97402c; }
.wf-state { margin-left: auto; font-style: normal; font-size: 12px; color: #91816b; }
.wf-step.done .wf-state { color: #4c6b3c; } .wf-step.blocked .wf-state { color: #97402c; } .wf-step.active .wf-state { color: #8a6420; }
.wf-body { margin-top: 6px; }
.blocked-box { margin: 10px 0; padding: 10px; border: 1px dashed #d6c5aa; border-radius: 8px; background: #fffaf3; }
.blocked-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 2px 10px; margin: 8px 0; }
.blocked-user { margin: 2px 0; align-items: center; }
.registry-scroll { overflow-x: auto; }
.registry-table { border-collapse: collapse; width: 100%; min-width: 820px; }
.registry-table th, .registry-table td { border-bottom: 1px solid #e8dfd1; padding: 7px 8px; text-align: left; vertical-align: top; }
.registry-table th { color: #82725c; font-weight: 600; white-space: nowrap; }
.registry-table .scene { max-width: 240px; }
.chip { display: inline-block; font-style: normal; font-size: 11px; padding: 1px 7px; margin: 1px 3px 1px 0; border-radius: 12px; background: #efe6d4; color: #6b5031; white-space: nowrap; }
.chip.personal { background: #dbe8d2; color: #3f6230; } .chip.ns { background: #e4e0f0; color: #54487a; } .chip.param { background: #f4ead8; }
.fn-code { display: inline-block; margin-left: 6px; }
.mini-btn { margin-left: 6px; padding: 1px 8px; font-size: 11px; }
.modal-overlay { position: fixed; inset: 0; background: rgba(40, 30, 15, .45); display: flex; align-items: center; justify-content: center; z-index: 60; }
.modal-box { background: #fffaf3; border: 1px solid #e0d6c7; border-radius: 14px; padding: 18px 22px; box-shadow: 0 12px 40px rgba(0, 0, 0, .18); max-width: 560px; width: calc(100vw - 48px); max-height: 80vh; overflow-y: auto; }
@media (max-width: 600px) { .tg-inner { padding: 14px 12px 40px; } .panel { padding: 12px; } button { min-height: 38px; } }
</style>