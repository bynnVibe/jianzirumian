<template>
  <div class="skills-view">
    <!-- 顶部栏：返回对话 + 标题 -->
    <header class="skills-header">
      <button class="back-btn" @click="router.push('/chat')" title="返回对话">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
          <line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>
        </svg>
        返回对话
      </button>
      <div class="header-title">
        <h1>Skill 配置</h1>
        <p class="subtitle">管理你自己的技能，并决定是否应用。技能分两类：<b>对话提示</b>（注入回答风格/领域约束）与<b>策略 (playbook)</b>（链接解析抓取规则、正文清洗策略）。新建/导入时会先做安全预审。</p>
      </div>
      <button class="btn btn-primary new-btn" @click="openCreate">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
          <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
        </svg>
        新建技能
      </button>
    </header>

    <div v-if="loading" class="loading-box">加载技能中…</div>
    <div v-else-if="loadError" class="error-box">{{ loadError }}</div>

    <template v-else>
      <!-- 内置技能（可折叠） -->
      <section class="skill-group">
        <h2 class="group-title clickable" @click="collapsed.builtin = !collapsed.builtin"
            :title="collapsed.builtin ? '展开' : '收起'">
          <svg class="chevron" :class="{ open: !collapsed.builtin }" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" stroke-width="2" width="14" height="14">
            <polyline points="9 6 15 12 9 18"/>
          </svg>
          内置技能 <span class="group-count">{{ builtinSkills.length }}</span>
          <span class="group-hint">系统沉淀，可开关但不可删除</span>
        </h2>
        <template v-if="!collapsed.builtin">
        <div v-if="!builtinSkills.length" class="empty-box">暂无内置技能</div>
        <div v-for="s in builtinSkills" :key="s.id" class="skill-card builtin">
          <div class="card-inner">
            <div class="card-main">
              <div class="card-head">
                <span class="kind-badge" :class="s.kind">{{ s.kind === 'prompt' ? '对话提示' : '策略' }}</span>
                <span class="scope-badge">{{ scopeLabel(s.scope) }}</span>
                <span class="builtin-badge">内置</span>
                <span class="card-name">{{ s.name }}</span>
              </div>
              <p v-if="s.description" class="card-desc">{{ s.description }}</p>
              <div class="card-foot">
                <button class="doc-btn" @click="openDoc(s)">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>
                  </svg>
                  查看文档
                </button>
                <span class="doc-hint">标题 + 简述展示，完整说明书按需懒加载</span>
              </div>
            </div>
            <div class="card-side">
              <button class="toggle-switch" :class="{ active: s.enabled }"
                      :title="s.enabled ? '已应用，点击关闭' : '未应用，点击开启'" @click="toggle(s)">
                <span class="knob"></span>
              </button>
              <div class="card-state">{{ s.enabled ? '已应用' : '未应用' }}</div>
              <div class="card-ops"><span class="op-disabled">内置不可编辑</span></div>
            </div>
          </div>
        </div>
        </template>
      </section>

      <!-- 自定义技能（可折叠） -->
      <section class="skill-group">
        <h2 class="group-title clickable" @click="collapsed.custom = !collapsed.custom"
            :title="collapsed.custom ? '展开' : '收起'">
          <svg class="chevron" :class="{ open: !collapsed.custom }" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" stroke-width="2" width="14" height="14">
            <polyline points="9 6 15 12 9 18"/>
          </svg>
          我的技能 <span class="group-count">{{ customSkills.length }}</span>
          <span class="group-hint">可自由增删改</span>
        </h2>
        <template v-if="!collapsed.custom">
        <div v-if="!customSkills.length" class="empty-box">还没有自定义技能，点击右上角「新建技能」开始沉淀你的第一条技能。</div>
        <div v-for="s in customSkills" :key="s.id" class="skill-card">
          <div class="card-inner">
            <div class="card-main">
              <div class="card-head">
                <span class="kind-badge" :class="s.kind">{{ s.kind === 'prompt' ? '对话提示' : '策略' }}</span>
                <span class="scope-badge">{{ scopeLabel(s.scope) }}</span>
                <span class="vis-badge" :class="s.visibility === 'public' ? 'public' : 'personal'">{{ s.visibility === 'public' ? '公共' : '个人' }}</span>
                <span class="card-name">{{ s.name }}</span>
              </div>
              <p v-if="s.description" class="card-desc">{{ s.description }}</p>
              <div class="card-foot">
                <button class="doc-btn" @click="openDoc(s)">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>
                  </svg>
                  查看文档
                </button>
                <span class="doc-hint">完整内容在文档弹窗按需展开，可直接编辑</span>
              </div>
            </div>
            <div class="card-side">
              <button class="toggle-switch" :class="{ active: s.enabled }"
                      :title="s.enabled ? '已应用，点击关闭' : '未应用，点击开启'" @click="toggle(s)">
                <span class="knob"></span>
              </button>
              <div class="card-state">{{ s.enabled ? '已应用' : '未应用' }}</div>
              <div class="card-ops">
                <button class="op-btn" @click="openEdit(s)">编辑</button>
                <button class="op-btn danger" @click="askDelete(s)">删除</button>
              </div>
            </div>
          </div>
        </div>
        </template>
      </section>

      <!-- 公共技能（他人共享，只读引用） -->
      <section class="skill-group" v-if="sharedSkills.length">
        <h2 class="group-title clickable" @click="collapsed.shared = !collapsed.shared"
            :title="collapsed.shared ? '展开' : '收起'">
          <svg class="chevron" :class="{ open: !collapsed.shared }" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" stroke-width="2" width="14" height="14">
            <polyline points="9 6 15 12 9 18"/>
          </svg>
          公共技能 <span class="group-count">{{ sharedSkills.length }}</span>
          <span class="group-hint">其他成员公开共享，已自动生效，只读</span>
        </h2>
        <template v-if="!collapsed.shared">
        <div v-for="s in sharedSkills" :key="s.id" class="skill-card shared">
          <div class="card-inner">
            <div class="card-main">
              <div class="card-head">
                <span class="kind-badge" :class="s.kind">{{ s.kind === 'prompt' ? '对话提示' : '策略' }}</span>
                <span class="scope-badge">{{ scopeLabel(s.scope) }}</span>
                <span class="vis-badge public">公共</span>
                <span class="card-name">{{ s.name }}</span>
                <span v-if="s.owner_name" class="owner-badge">来自 {{ s.owner_name }}</span>
              </div>
              <p v-if="s.description" class="card-desc">{{ s.description }}</p>
            </div>
            <div class="card-side">
              <div class="card-state">{{ s.enabled ? '已生效' : '未启用' }}</div>
              <div class="card-ops"><span class="op-disabled">共享只读</span></div>
            </div>
          </div>
        </div>
        </template>
      </section>
    </template>

    <!-- 编辑/新建弹窗 -->
    <div v-if="showEditor" class="modal-overlay" @click.self="closeEditor">
      <div class="modal-box">
        <h3 class="modal-title">{{ form.id ? '编辑技能' : '新建技能' }}</h3>
        <div v-if="!form.id" class="import-row">
          <button type="button" class="btn btn-outline import-btn" :disabled="saving" @click="fileInput?.click()">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>
            </svg>
            导入本地技能文件
          </button>
          <span class="field-hint">支持 .json / .md / .txt；解析后自动填入表单，保存前需确认，同样经过安全预审</span>
          <input ref="fileInput" type="file" accept=".json,.md,.markdown,.txt" hidden @change="onImportFile" />
        </div>
        <div class="form-row">
          <label>名称</label>
          <input v-model="form.name" class="input" maxlength="60" placeholder="如：通俗化健康解释" />
        </div>
        <div class="form-row">
          <label>类型</label>
          <div class="kind-picker">
            <button type="button" class="kind-opt" :class="{ active: form.kind === 'prompt' }"
                    :disabled="!!form.id" @click="pickKind('prompt')">对话提示 (prompt)</button>
            <button type="button" class="kind-opt" :class="{ active: form.kind === 'playbook' }"
                    :disabled="!!form.id" @click="pickKind('playbook')">策略 (playbook)</button>
          </div>
          <p class="field-hint">{{ form.kind === 'prompt' ? '注入到对话系统提示，定制回答风格或领域约束。' : '可驱动链接抓取回退或正文清洗（内容润色），内容为 JSON。' }}</p>
        </div>
        <div v-if="form.kind === 'playbook'" class="form-row">
          <label>作用域</label>
          <div class="kind-picker">
            <button type="button" class="kind-opt" :class="{ active: form.scope === 'url_import' }"
                    :disabled="!!form.id" @click="form.scope = 'url_import'">链接解析（抓取策略）</button>
            <button type="button" class="kind-opt" :class="{ active: form.scope === 'polish' }"
                    :disabled="!!form.id" @click="form.scope = 'polish'">正文清洗（内容润色）</button>
          </div>
        </div>
        <div class="form-row">
          <label>描述</label>
          <input v-model="form.description" class="input" maxlength="200" placeholder="一句话说明这个技能的用途" />
        </div>
        <div class="form-row">
          <label>{{ form.kind === 'prompt' ? '提示内容' : (form.scope === 'polish' ? '清洗策略 (JSON)' : '策略规则 (JSON)') }}</label>
          <textarea v-model="form.content" class="input textarea" rows="6"
                    :placeholder="form.kind === 'prompt' ? '请先用一句通俗的话给出结论，再展开专业细节…' : (form.scope === 'polish' ? polishPlaceholder : playbookPlaceholder)"></textarea>
          <p v-if="form.kind === 'playbook' && form.scope === 'url_import'" class="field-hint">
            每条规则：<code>{ "match": "主机名", "mobile_ua": true, "prefer_render": true, "note": "说明" }</code>；
            match 支持后缀匹配（如 <code>csdn.net</code> 命中 <code>blog.csdn.net</code>）。
          </p>
          <p v-else-if="form.kind === 'playbook' && form.scope === 'polish'" class="field-hint">
            四个布尔开关控制去角标/去噪声行/压缩空格/压缩空行；<code>extra_line_patterns</code> 可追加最多 10 条整行删除正则（≤120 字）。
          </p>
        </div>
        <div class="form-row inline">
          <label>创建后立即启用</label>
          <button type="button" class="toggle-switch" :class="{ active: form.enabled }" @click="form.enabled = !form.enabled">
            <span class="knob"></span>
          </button>
        </div>
        <div v-if="!form.id" class="form-row">
          <label>归属可见性</label>
          <div class="kind-picker">
            <button type="button" class="kind-opt" :class="{ active: form.visibility === 'personal' }" @click="form.visibility = 'personal'">个人（仅自己）</button>
            <button type="button" class="kind-opt" :class="{ active: form.visibility === 'public' }" @click="form.visibility = 'public'">公共（全员可用）</button>
          </div>
          <p class="field-hint">公共技能对所有成员可见并在对话中自动生效；个人技能仅自己可用。</p>
        </div>
        <div v-if="editorError" class="editor-error">{{ editorError }}</div>
        <div class="modal-actions">
          <button class="btn btn-primary" :disabled="saving" @click="save">
            {{ saving ? '保存中…' : '保存' }}
          </button>
          <button class="btn btn-outline" :disabled="saving" @click="closeEditor">取消</button>
        </div>
      </div>
    </div>

    <!-- 删除确认 -->
    <div v-if="deleteTarget" class="modal-overlay" @click.self="deleteTarget = null">
      <div class="modal-box small">
        <h3 class="modal-title">删除技能</h3>
        <p class="modal-desc">确定删除「{{ deleteTarget.name }}」吗？此操作不可恢复。</p>
        <div class="modal-actions">
          <button class="btn btn-danger" :disabled="deleting" @click="confirmDelete">{{ deleting ? '删除中…' : '删除' }}</button>
          <button class="btn btn-outline" :disabled="deleting" @click="deleteTarget = null">取消</button>
        </div>
      </div>
    </div>

    <!-- 技能文档弹窗（渐进式/懒加载：主体一次拉取，references 点开才拉） -->
    <div v-if="showDoc" class="modal-overlay" @click.self="closeDoc">
      <div class="modal-box doc-modal">
        <div class="doc-head">
          <div>
            <h3 class="modal-title">{{ doc?.title || '技能文档' }}</h3>
            <div class="doc-meta">
              <span v-if="doc?.kind" class="kind-badge" :class="doc.kind">{{ doc.kind === 'prompt' ? '对话提示' : '策略' }}</span>
              <span v-if="doc?.scope" class="scope-badge">{{ scopeLabel(doc.scope) }}</span>
              <span v-if="doc?.version" class="doc-version">v{{ doc.version }}</span>
              <span v-if="doc?.source === 'synthesized'" class="doc-src">即时合成</span>
            </div>
          </div>
          <button class="doc-close" @click="closeDoc" title="关闭">×</button>
        </div>

        <div v-if="docLoading" class="loading-box">加载文档中…</div>
        <div v-else-if="docError" class="error-box">{{ docError }}</div>
        <template v-else-if="doc">
          <p v-if="doc.description" class="doc-desc">{{ doc.description }}</p>
          <!-- L2：SKILL.md 主体 -->
          <div class="markdown-body" v-html="renderedDoc"></div>

          <!-- L3：references 按需懒加载 -->
          <div v-if="doc.references?.length" class="doc-refs">
            <div class="doc-refs-title">扩展参考（点开按需加载）</div>
            <div class="doc-ref-chips">
              <button v-for="r in doc.references" :key="r.path" class="ref-chip"
                      :class="{ active: activeRef?.path === r.path }" @click="toggleRef(r)">
                {{ r.title }}
              </button>
            </div>
            <div v-if="refLoading" class="loading-box ref-loading">加载参考中…</div>
            <div v-else-if="activeRef" class="doc-ref-body">
              <div class="doc-ref-path">{{ activeRef.path }}</div>
              <div class="markdown-body" v-html="renderedRef"></div>
            </div>
          </div>
        </template>

        <div class="modal-actions">
          <button class="btn btn-outline" @click="closeDoc">关闭</button>
        </div>
      </div>
    </div>

    <!-- 轻提示 -->
    <transition name="fade">
      <div v-if="toast" class="toast" :class="{ error: toast.error }">{{ toast.message }}</div>
    </transition>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import MarkdownIt from 'markdown-it'
import { listSkills, createSkill, updateSkill, deleteSkill, getSkillDoc, getSkillDocRef } from '@/api'

const router = useRouter()
const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

const skills = ref([])
const loading = ref(true)
const loadError = ref('')

const showEditor = ref(false)
const saving = ref(false)
const editorError = ref('')
const form = reactive({ id: '', name: '', kind: 'prompt', scope: 'chat', description: '', content: '', enabled: true, visibility: 'personal' })

// 分组折叠状态（内置/我的）
const collapsed = reactive({ builtin: false, custom: false, shared: false })

// 导入本地技能文件
const fileInput = ref(null)

const deleteTarget = ref(null)
const deleting = ref(false)

const toast = ref(null)
let toastTimer = null

const playbookPlaceholder = `[
  { "match": "mp.weixin.qq.com", "mobile_ua": true, "note": "移动端 UA 更易拿到正文" }
]`

const polishPlaceholder = `{
  "remove_footnote_markers": true,
  "remove_noise_lines": true,
  "collapse_spaces": true,
  "collapse_blank_lines": true,
  "extra_line_patterns": []
}`

const SCOPE_LABELS = { chat: '对话', url_import: '链接解析', polish: '正文清洗' }
function scopeLabel(scope) { return SCOPE_LABELS[scope] || scope || '对话' }

const builtinSkills = computed(() => skills.value.filter(s => s.builtin && !s.is_shared))
const customSkills = computed(() => skills.value.filter(s => !s.builtin && !s.is_shared))
// 他人公开共享的技能：全员可见、只读引用（不可开关/编辑/删除）
const sharedSkills = computed(() => skills.value.filter(s => s.is_shared))

function showToast(message, error = false) {
  toast.value = { message, error }
  if (toastTimer) clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toast.value = null }, 2600)
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const res = await listSkills()
    skills.value = res.data.skills || []
  } catch (err) {
    loadError.value = '加载技能失败：' + (err.response?.data?.detail || err.message)
  } finally {
    loading.value = false
  }
}

async function toggle(skill) {
  const next = !skill.enabled
  try {
    await updateSkill(skill.id, { enabled: next })
    skill.enabled = next
    showToast(next ? `已应用「${skill.name}」` : `已关闭「${skill.name}」`)
  } catch (err) {
    showToast('切换失败：' + (err.response?.data?.detail || err.message), true)
  }
}

function pickKind(kind) {
  form.kind = kind
  form.scope = kind === 'prompt' ? 'chat' : 'url_import'
}

// 解析本地技能文件（.json / .md / .txt）；只填表不直接保存，由用户确认后走正常创建（含预审）
function parseSkillFile(filename, raw) {
  const ext = (filename.split('.').pop() || '').toLowerCase()
  const base = filename.replace(/\.[^.]+$/, '')
  let name = '', description = '', kind = 'prompt', scope = '', content = ''
  let enabled = true
  if (ext === 'json') {
    let data
    try { data = JSON.parse(raw) } catch { throw new Error('JSON 解析失败，请检查文件格式') }
    if (Array.isArray(data)) {
      if (data.length !== 1) throw new Error('JSON 数组仅支持包含单个技能对象')
      data = data[0]
    }
    if (data && typeof data === 'object' && data.skill && typeof data.skill === 'object') data = data.skill
    if (!data || typeof data !== 'object') throw new Error('JSON 内容必须是技能对象')
    name = String(data.name || base)
    description = String(data.description || '')
    kind = data.kind === 'playbook' ? 'playbook' : 'prompt'
    scope = typeof data.scope === 'string' ? data.scope : ''
    content = typeof data.content === 'string' ? data.content
      : typeof data.prompt === 'string' ? data.prompt
      : typeof data.body === 'string' ? data.body
      : (() => { throw new Error('缺少技能内容字段（content/prompt/body）') })()
    if (data.enabled === false) enabled = false
  } else {
    // md / txt：可选 --- frontmatter（name/description/kind/enabled），正文作为内容
    let body = raw
    const fm = raw.match(/^\uFEFF?---\r?\n([\s\S]*?)\r?\n---\r?\n?/)
    if (fm) {
      body = raw.slice(fm[0].length)
      for (const line of fm[1].split(/\r?\n/)) {
        const idx = line.indexOf(':')
        if (idx < 0) continue
        const key = line.slice(0, idx).trim().toLowerCase()
        const val = line.slice(idx + 1).trim().replace(/^["']|["']$/g, '')
        if (key === 'name') name = val
        else if (key === 'description') description = val
        else if (key === 'kind') kind = val === 'playbook' ? 'playbook' : 'prompt'
        else if (key === 'enabled') enabled = val !== 'false'
      }
    }
    content = body.trim()
    if (!content) throw new Error('文件正文为空，没有可导入的技能内容')
    name = name || base
  }
  if (kind === 'playbook' && !scope) scope = 'url_import'
  if (kind === 'prompt') scope = 'chat'
  if (scope && !['chat', 'url_import', 'polish'].includes(scope)) scope = ''
  return { name: name.slice(0, 60), kind, scope, description: description.slice(0, 200), content, enabled }
}

async function onImportFile(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  if (file.size > 200 * 1024) { editorError.value = '技能文件超过 200KB，请精简后导入'; return }
  try {
    const raw = await file.text()
    const parsed = parseSkillFile(file.name, raw)
    Object.assign(form, { id: '', ...parsed })
    editorError.value = ''
    showToast(`已解析「${file.name}」，请确认后保存`)
  } catch (err) {
    editorError.value = '导入失败：' + (err.message || err)
  }
}

function openCreate() {
  Object.assign(form, { id: '', name: '', kind: 'prompt', scope: 'chat', description: '', content: '', enabled: true, visibility: 'personal' })
  editorError.value = ''
  showEditor.value = true
}

function openEdit(skill) {
  Object.assign(form, {
    id: skill.id, name: skill.name, kind: skill.kind, scope: skill.scope || (skill.kind === 'prompt' ? 'chat' : 'url_import'),
    description: skill.description, content: skill.content, enabled: skill.enabled,
  })
  editorError.value = ''
  showEditor.value = true
}

function closeEditor() {
  if (saving.value) return
  showEditor.value = false
}

async function save() {
  editorError.value = ''
  if (!form.name.trim()) { editorError.value = '请填写技能名称'; return }
  if (!form.content.trim()) { editorError.value = '请填写技能内容'; return }
  if (form.kind === 'playbook') {
    try { JSON.parse(form.content) } catch { editorError.value = form.scope === 'polish' ? '清洗策略内容必须是合法 JSON' : '策略内容必须是合法 JSON'; return }
  }
  saving.value = true
  try {
    if (form.id) {
      await updateSkill(form.id, {
        name: form.name, description: form.description, content: form.content, enabled: form.enabled,
      })
      showToast('技能已更新')
    } else {
      await createSkill({
        name: form.name, kind: form.kind, description: form.description,
        content: form.content, enabled: form.enabled,
        scope: form.kind === 'playbook' ? form.scope : null,
        visibility: form.visibility,
      })
      showToast('技能已创建')
    }
    showEditor.value = false
    await load()
  } catch (err) {
    editorError.value = err.response?.data?.detail || err.message
  } finally {
    saving.value = false
  }
}

function askDelete(skill) {
  deleteTarget.value = skill
}

async function confirmDelete() {
  if (!deleteTarget.value) return
  deleting.value = true
  try {
    await deleteSkill(deleteTarget.value.id)
    showToast('技能已删除')
    deleteTarget.value = null
    await load()
  } catch (err) {
    showToast('删除失败：' + (err.response?.data?.detail || err.message), true)
  } finally {
    deleting.value = false
  }
}

// ---- 技能文档（渐进式/懒加载）----
const showDoc = ref(false)
const docLoading = ref(false)
const docError = ref('')
const doc = ref(null)
const docSkillId = ref('')
const activeRef = ref(null)
const refLoading = ref(false)

const renderedDoc = computed(() => (doc.value ? md.render(doc.value.body || '') : ''))
const renderedRef = computed(() => (activeRef.value ? md.render(activeRef.value.content || '') : ''))

async function openDoc(skill) {
  docSkillId.value = skill.id
  doc.value = null
  activeRef.value = null
  docError.value = ''
  docLoading.value = true
  showDoc.value = true
  try {
    const res = await getSkillDoc(skill.id)
    doc.value = res.data.doc || null
    if (!doc.value) docError.value = '未获取到文档内容'
  } catch (err) {
    docError.value = '加载文档失败：' + (err.response?.data?.detail || err.message)
  } finally {
    docLoading.value = false
  }
}

// L3：点开某条参考才拉取其正文；再次点同一条则收起
async function toggleRef(refItem) {
  if (activeRef.value?.path === refItem.path) { activeRef.value = null; return }
  refLoading.value = true
  try {
    const res = await getSkillDocRef(docSkillId.value, refItem.path)
    activeRef.value = res.data.reference || null
  } catch (err) {
    showToast('加载参考失败：' + (err.response?.data?.detail || err.message), true)
  } finally {
    refLoading.value = false
  }
}

function closeDoc() {
  showDoc.value = false
  doc.value = null
  activeRef.value = null
}

onMounted(load)
</script>

<style scoped>
.skills-view {
  max-width: 900px;
  margin: 0 auto;
  padding: 28px 24px 80px;
  color: var(--color-text);
}
.skills-header {
  display: flex;
  align-items: flex-start;
  gap: 16px;
  padding-bottom: 18px;
  border-bottom: 1px dashed var(--color-border);
  margin-bottom: 22px;
}
.back-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: none;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  padding: 7px 12px;
  color: var(--color-text-secondary);
  cursor: pointer;
  font-size: 13px;
  white-space: nowrap;
}
.back-btn:hover { border-color: var(--color-primary); color: var(--color-primary); }
.header-title { flex: 1; }
.header-title h1 { font-size: 22px; font-weight: 700; }
.subtitle { font-size: 13px; color: var(--color-text-secondary); margin-top: 6px; line-height: 1.6; }
.new-btn { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }

.loading-box, .error-box, .empty-box {
  padding: 20px; text-align: center; color: var(--color-text-secondary);
  font-size: 14px; border: 1px dashed var(--color-border); border-radius: 12px;
}
.error-box { color: #b4553d; border-color: #e6c3b6; background: #fdf3ef; }

.skill-group { margin-bottom: 28px; }
.group-title {
  font-size: 15px; font-weight: 700; margin-bottom: 12px;
  display: flex; align-items: center; gap: 8px;
}
.group-title.clickable { cursor: pointer; user-select: none; }
.group-title.clickable:hover { color: var(--color-primary); }
.chevron { transition: transform .2s; flex: none; color: var(--color-text-secondary); }
.chevron.open { transform: rotate(90deg); }
.group-count {
  font-size: 11px; font-weight: 600; background: #f2ece2; color: var(--color-text-secondary);
  border-radius: 999px; padding: 1px 8px;
}
.group-hint { font-size: 12px; font-weight: 400; color: var(--color-text-secondary); }
.skill-card { margin-bottom: 12px; }

.card-inner {
  display: flex; gap: 16px; align-items: stretch;
  background: #fff; border: 1px solid var(--color-border);
  border-radius: 14px; padding: 16px 18px;
  box-shadow: 0 1px 2px rgba(120, 90, 50, 0.04);
}
.skill-card.builtin .card-inner { background: var(--color-primary-light); border-color: #ecd9bd; }
.card-main { flex: 1; min-width: 0; }
.card-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 6px; }
.card-name { font-size: 15px; font-weight: 700; }
.kind-badge, .scope-badge, .builtin-badge {
  font-size: 11px; padding: 2px 8px; border-radius: 999px; font-weight: 600;
}
.kind-badge.prompt { background: #e7f0e9; color: #4a7a55; }
.kind-badge.playbook { background: #e8eef7; color: #4a6a9a; }
.scope-badge { background: #f2ece2; color: var(--color-text-secondary); }
.builtin-badge { background: #f6e2c8; color: #a9713a; }
.vis-badge { font-size: 11px; padding: 2px 8px; border-radius: 999px; font-weight: 600; }
.vis-badge.personal { background: #eef0f3; color: #6a7280; }
.vis-badge.public { background: #e6f2ee; color: #2f7d63; }
.owner-badge { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: #f2ece2; color: var(--color-text-secondary); }
.skill-card.shared { background: #fbfdfc; border-style: dashed; }
.card-desc { font-size: 13px; color: var(--color-text-secondary); line-height: 1.6; margin-bottom: 8px; }
.card-content {
  font-size: 12px; line-height: 1.5; color: #6b6155; background: #fbf8f3;
  border: 1px solid var(--color-border); border-radius: 8px; padding: 8px 10px;
  white-space: pre-wrap; word-break: break-word; max-height: 120px; overflow: auto;
  font-family: 'SFMono-Regular', Consolas, monospace; margin: 0;
}
/* 卡片底部：查看文档入口（标题+简述卡片，详情懒加载） */
.card-foot { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.doc-btn {
  display: inline-flex; align-items: center; gap: 5px; cursor: pointer;
  font-size: 12px; padding: 4px 11px; border-radius: 8px;
  border: 1px solid var(--color-border); background: #fff; color: var(--color-text-secondary);
}
.doc-btn:hover { border-color: var(--color-primary); color: var(--color-primary); }
.doc-hint { font-size: 11px; color: #b7ab9b; }

/* 文档弹窗 */
.doc-modal { max-width: 720px; }
.doc-head { display: flex; align-items: flex-start; gap: 12px; margin-bottom: 12px; }
.doc-head > div { flex: 1; min-width: 0; }
.doc-head .modal-title { margin-bottom: 8px; }
.doc-meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.doc-version, .doc-src {
  font-size: 11px; padding: 2px 8px; border-radius: 999px; font-weight: 600;
  background: #f2ece2; color: var(--color-text-secondary);
}
.doc-src { background: #eef3ec; color: #4a7a55; }
.doc-close {
  flex: none; border: none; background: none; cursor: pointer; font-size: 24px; line-height: 1;
  color: var(--color-text-secondary); padding: 0 4px;
}
.doc-close:hover { color: var(--color-primary); }
.doc-desc {
  font-size: 13px; color: var(--color-text-secondary); line-height: 1.6;
  padding: 10px 12px; background: var(--color-primary-light); border: 1px dashed #ecd9bd;
  border-radius: 10px; margin-bottom: 14px;
}
.doc-refs { margin-top: 18px; border-top: 1px dashed var(--color-border); padding-top: 14px; }
.doc-refs-title { font-size: 13px; font-weight: 700; margin-bottom: 10px; }
.doc-ref-chips { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
.ref-chip {
  cursor: pointer; font-size: 12px; padding: 5px 12px; border-radius: 999px;
  border: 1px solid var(--color-border); background: #fff; color: var(--color-text-secondary);
}
.ref-chip:hover { border-color: var(--color-primary); color: var(--color-primary); }
.ref-chip.active { background: var(--color-primary); border-color: var(--color-primary); color: #fff; font-weight: 600; }
.ref-loading { padding: 12px; }
.doc-ref-body { border: 1px solid var(--color-border); border-radius: 10px; padding: 12px 14px; background: #fffdf9; }
.doc-ref-path {
  font-size: 11px; color: #b7ab9b; font-family: 'SFMono-Regular', Consolas, monospace;
  margin-bottom: 8px; word-break: break-all;
}

/* Markdown 正文渲染（v-html 需 :deep） */
.markdown-body { font-size: 13.5px; line-height: 1.75; color: var(--color-text); word-break: break-word; }
.markdown-body :deep(h1) { font-size: 19px; font-weight: 700; margin: 4px 0 12px; }
.markdown-body :deep(h2) { font-size: 16px; font-weight: 700; margin: 18px 0 8px; padding-bottom: 4px; border-bottom: 1px dashed var(--color-border); }
.markdown-body :deep(h3) { font-size: 14px; font-weight: 700; margin: 14px 0 6px; }
.markdown-body :deep(p) { margin: 8px 0; }
.markdown-body :deep(ul), .markdown-body :deep(ol) { margin: 8px 0; padding-left: 22px; }
.markdown-body :deep(li) { margin: 3px 0; }
.markdown-body :deep(code) { background: #f2ece2; padding: 1px 5px; border-radius: 4px; font-size: 12px; font-family: 'SFMono-Regular', Consolas, monospace; }
.markdown-body :deep(pre) { background: #fbf8f3; border: 1px solid var(--color-border); border-radius: 8px; padding: 10px 12px; overflow: auto; margin: 10px 0; }
.markdown-body :deep(pre code) { background: none; padding: 0; }
.markdown-body :deep(table) { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 12.5px; }
.markdown-body :deep(th), .markdown-body :deep(td) { border: 1px solid var(--color-border); padding: 6px 9px; text-align: left; }
.markdown-body :deep(th) { background: #f6f1e8; font-weight: 700; }
.markdown-body :deep(blockquote) { border-left: 3px solid #ecd9bd; padding: 2px 12px; margin: 10px 0; color: var(--color-text-secondary); background: #fdfaf5; }
.markdown-body :deep(a) { color: var(--color-primary); }
.markdown-body :deep(hr) { border: none; border-top: 1px dashed var(--color-border); margin: 14px 0; }
.card-side {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 8px; min-width: 92px; border-left: 1px dashed var(--color-border); padding-left: 16px;
}
.card-state { font-size: 12px; color: var(--color-text-secondary); }
.card-ops { display: flex; gap: 8px; }
.op-btn {
  font-size: 12px; padding: 4px 10px; border-radius: 8px; cursor: pointer;
  border: 1px solid var(--color-border); background: #fff; color: var(--color-text-secondary);
}
.op-btn:hover { border-color: var(--color-primary); color: var(--color-primary); }
.op-btn.danger:hover { border-color: #c96a4b; color: #c96a4b; }
.op-disabled { font-size: 11px; color: #b7ab9b; }

.toggle-switch {
  width: 44px; height: 24px; border-radius: 999px; border: none; cursor: pointer;
  background: #ddd3c6; position: relative; transition: background .2s; padding: 0;
}
.toggle-switch.active { background: var(--color-primary); }
.toggle-switch .knob {
  position: absolute; top: 3px; left: 3px; width: 18px; height: 18px;
  border-radius: 50%; background: #fff; transition: transform .2s; box-shadow: 0 1px 2px rgba(0,0,0,.2);
}
.toggle-switch.active .knob { transform: translateX(20px); }

/* 弹窗 */
.modal-overlay {
  position: fixed; inset: 0; background: rgba(45, 42, 36, 0.4);
  display: flex; align-items: center; justify-content: center; z-index: 1000; padding: 20px;
}
.modal-box {
  background: var(--color-bg); border-radius: 16px; padding: 24px;
  width: 100%; max-width: 560px; max-height: 90vh; overflow: auto;
  box-shadow: 0 8px 30px rgba(80, 60, 30, 0.18);
}
.modal-box.small { max-width: 400px; }
.modal-title { font-size: 17px; font-weight: 700; margin-bottom: 16px; }
.modal-desc { font-size: 14px; color: var(--color-text-secondary); line-height: 1.6; margin-bottom: 18px; }
.form-row { margin-bottom: 16px; }
.form-row > label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; }
.form-row.inline { display: flex; align-items: center; gap: 12px; }
.form-row.inline > label { margin-bottom: 0; }
.input {
  width: 100%; padding: 9px 12px; border: 1px solid var(--color-border); border-radius: 10px;
  font-size: 14px; font-family: inherit; color: var(--color-text); background: #fff;
}
.input:focus { outline: none; border-color: var(--color-primary); }
.textarea { resize: vertical; line-height: 1.6; font-family: 'SFMono-Regular', Consolas, monospace; font-size: 13px; }
.kind-picker { display: flex; gap: 8px; }
.kind-opt {
  flex: 1; padding: 8px; border-radius: 10px; border: 1px solid var(--color-border);
  background: #fff; cursor: pointer; font-size: 13px; color: var(--color-text-secondary);
}
.kind-opt.active { border-color: var(--color-primary); background: var(--color-primary-light); color: #a9713a; font-weight: 600; }
.kind-opt:disabled { opacity: .5; cursor: not-allowed; }
.field-hint { font-size: 12px; color: var(--color-text-secondary); margin-top: 6px; line-height: 1.6; }
.field-hint code { background: #f2ece2; padding: 1px 5px; border-radius: 4px; font-size: 11px; }
.editor-error { color: #b4553d; font-size: 13px; background: #fdf3ef; border: 1px solid #e6c3b6; border-radius: 8px; padding: 8px 12px; margin-bottom: 12px; white-space: pre-wrap; }
.modal-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 8px; }
.import-row {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 10px 12px; margin-bottom: 16px;
  background: var(--color-primary-light); border: 1px dashed #ecd9bd; border-radius: 10px;
}
.import-btn { display: inline-flex; align-items: center; gap: 6px; padding: 7px 12px; font-size: 13px; white-space: nowrap; }
.import-row .field-hint { margin-top: 0; flex: 1; min-width: 180px; }

.btn { padding: 9px 18px; border-radius: 10px; font-size: 14px; cursor: pointer; border: 1px solid transparent; font-weight: 600; }
.btn:disabled { opacity: .6; cursor: not-allowed; }
.btn-primary { background: var(--color-primary); color: #fff; }
.btn-primary:hover:not(:disabled) { background: #b87a3f; }
.btn-outline { background: #fff; border-color: var(--color-border); color: var(--color-text-secondary); }
.btn-outline:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
.btn-danger { background: #c96a4b; color: #fff; }
.btn-danger:hover:not(:disabled) { background: #b4553d; }

.toast {
  position: fixed; bottom: 28px; left: 50%; transform: translateX(-50%);
  background: #2d2a24; color: #fff; padding: 10px 20px; border-radius: 999px;
  font-size: 13px; z-index: 1100; box-shadow: 0 4px 16px rgba(0,0,0,.2);
}
.toast.error { background: #b4553d; }
.fade-enter-active, .fade-leave-active { transition: opacity .25s; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
</style>
