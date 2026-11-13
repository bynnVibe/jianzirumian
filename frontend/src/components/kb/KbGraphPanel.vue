<template>
  <div class="kb-graph">
    <!-- 工具栏：范围 / 搜索 / 语义搜索 -->
    <div class="graph-toolbar">
      <select v-model="docPath" class="scope-select" title="图谱范围：整库或单篇文档" @change="onScopeChange">
        <option value="">整库</option>
        <option v-for="d in docs" :key="d.source_image" :value="d.source_image">{{ d.title || d.filename }}</option>
      </select>
      <div class="search-box">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
          <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
        </svg>
        <input v-model="nodeQuery" placeholder="搜索节点…" />
        <button v-if="nodeQuery" class="clear-btn" @click="nodeQuery = ''">&times;</button>
      </div>
      <div v-if="!docScope" class="semantic-group">
        <input v-model="semanticQuery" class="semantic-input" placeholder="语义搜索节点…" @keyup.enter="runSemantic" />
        <select v-model.number="topN" class="mini-select" title="TopN">
          <option :value="5">Top 5</option>
          <option :value="10">Top 10</option>
          <option :value="20">Top 20</option>
        </select>
        <label class="threshold-label">
          阈值
          <input v-model.number="threshold" type="number" min="0" max="1" step="0.05" class="threshold-input" />
        </label>
        <button class="btn-tool" :disabled="semanticBusy || !semanticQuery.trim()" @click="runSemantic">
          {{ semanticBusy ? '搜索中…' : '语义搜索' }}
        </button>
      </div>
      <span class="graph-counts">{{ nodes.length }} 节点 · {{ edges.length }} 关系</span>
    </div>

    <!-- 工具栏：操作（单篇文档子图为实时聚合视图，不提供手动维护） -->
    <div class="graph-actions">
      <button v-if="!docScope" class="btn-tool" @click="openNodeForm">新建节点</button>
      <button v-if="!docScope" class="btn-tool" @click="openEdgeForm">新建关系</button>
      <button v-if="!docScope" class="btn-tool" @click="importVisible = true">导入JSONL</button>
      <button class="btn-tool" :disabled="rebuilding" :title="rebuilding ? '正在后台生成中' : (docScope ? '重新聚合该文档的百科子图' : '从知识百科重建图谱索引')" @click="rebuild">{{ rebuilding ? '重建中…' : (docScope ? '刷新子图' : '重建索引') }}</button>
      <button v-if="!docScope" class="btn-tool danger" @click="clearGraph">清空图谱</button>
      <div class="legend">
        <span v-for="(label, t) in TYPE_LABELS" :key="t" class="legend-item">
          <i :style="{ background: TYPE_COLORS[t] }"></i>{{ label }}
        </span>
      </div>
    </div>

    <p v-if="message" class="msg" :class="{ error: messageError }">{{ message }}</p>

    <!-- 画布 -->
    <div ref="canvasRef" class="graph-canvas" :class="{ dragging: !!drag }">
      <div v-if="loading" class="canvas-state">加载中…</div>
      <div v-else-if="!nodes.length" class="canvas-state">
        {{ docScope ? '该文档暂无关联百科页，入库后由知识百科自动编译生成子图' : '图谱为空。上传文档后由知识百科自动编译生成，或点击「重建索引」/「新建节点」' }}
      </div>
      <svg
        v-else
        ref="svgRef"
        class="graph-svg"
        @wheel.prevent="onWheel"
        @pointerdown="onBackgroundDown"
        @pointermove="onPointerMove"
        @pointerup="onPointerUp"
      >
        <defs>
          <marker id="kb-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="#b8a88f" />
          </marker>
        </defs>
        <g :transform="`translate(${view.x},${view.y}) scale(${view.k})`">
          <g v-for="e in renderEdges" :key="e.id" class="edge-g" :opacity="dimEdge(e) ? 0.12 : 1" @pointerdown.stop="selectEdge(e)">
            <line :x1="e.s.x" :y1="e.s.y" :x2="e.t.x" :y2="e.t.y" stroke="transparent" stroke-width="12" />
            <line
              :x1="e.s.x" :y1="e.s.y" :x2="edgeEnd(e).x" :y2="edgeEnd(e).y"
              stroke="#b8a88f" stroke-width="1.4" marker-end="url(#kb-arrow)"
              :stroke-dasharray="e.origin === 'manual' ? '5 3' : ''"
            />
            <text
              :x="(e.s.x + e.t.x) / 2" :y="(e.s.y + e.t.y) / 2 - 5"
              text-anchor="middle" font-size="10.5" fill="#8a7e72"
              stroke="#fefcf9" stroke-width="3" paint-order="stroke"
            >{{ e.label }}</text>
          </g>
          <g
            v-for="n in nodes" :key="n.id" class="node-g"
            :opacity="dimNode(n) ? 0.15 : 1"
            @pointerdown.stop="onNodeDown($event, n)"
          >
            <circle
              :cx="n.x" :cy="n.y" :r="nodeRadius(n)"
              :fill="TYPE_COLORS[n.type] || TYPE_COLORS.concept"
              :stroke="selectedNode?.id === n.id ? '#2d2a24' : '#fff'"
              :stroke-width="selectedNode?.id === n.id ? 2.5 : 1.5"
            />
            <text
              :x="n.x" :y="n.y + nodeRadius(n) + 13"
              text-anchor="middle" font-size="11.5" fill="#2d2a24"
              stroke="#fefcf9" stroke-width="3" paint-order="stroke"
            >{{ n.name }}</text>
            <text
              v-if="semanticScores[n.id] != null"
              :x="n.x + nodeRadius(n) + 3" :y="n.y - nodeRadius(n) - 2"
              font-size="9.5" fill="#c0392b" font-weight="600"
            >{{ semanticScores[n.id] }}</text>
          </g>
        </g>
      </svg>

      <!-- 选中信息卡 -->
      <div v-if="selectedNode" class="info-card">
        <div class="info-head">
          <span class="info-type" :style="{ background: TYPE_COLORS[selectedNode.type] }">{{ TYPE_LABELS[selectedNode.type] || '概念' }}</span>
          <strong>{{ selectedNode.name }}</strong>
          <button class="info-close" @click="selectedNode = null">&times;</button>
        </div>
        <p v-if="selectedNode.note" class="info-note">{{ selectedNode.note }}</p>
        <p class="info-meta">{{ selectedNode.origin === 'manual' ? '手动创建' : '百科生成' }} · {{ relatedEdges.length }} 条关系</p>
        <div v-if="selectedNode.sources && selectedNode.sources.length" class="info-sources">
          <span class="info-sources-label">引用来源（点击查看详细内容）：</span>
          <div class="source-chips">
            <button
              v-for="(s, i) in selectedNode.sources" :key="i"
              class="source-chip" :class="s.kind" :title="s.title"
              @click="openSource(s)"
            ><i class="chip-tag">{{ s.kind === 'wiki' ? '百科' : '文档' }}</i>{{ s.title }}</button>
          </div>
        </div>
        <div v-if="relatedEdges.length" class="info-edges">
          <div v-for="e in relatedEdges" :key="e.id" class="info-edge">
            <span>{{ e.source }} → {{ e.target }}：{{ e.label }}</span>
            <button v-if="!docScope" class="link-btn danger" @click="removeEdge(e)">删除</button>
          </div>
        </div>
        <button v-if="!docScope" class="btn-tool danger block" @click="removeNode(selectedNode)">删除节点</button>
      </div>
      <div v-else-if="selectedEdge" class="info-card">
        <div class="info-head">
          <strong>{{ selectedEdge.source }} → {{ selectedEdge.target }}</strong>
          <button class="info-close" @click="selectedEdge = null">&times;</button>
        </div>
        <p class="info-note">关系：{{ selectedEdge.label }}</p>
        <button v-if="!docScope" class="btn-tool danger block" @click="removeEdge(selectedEdge)">删除关系</button>
      </div>
    </div>

    <!-- 新建节点 -->
    <div v-if="nodeFormVisible" class="modal-mask" @click.self="nodeFormVisible = false">
      <div class="modal-card">
        <h3>新建节点</h3>
        <label class="field-label">名称</label>
        <input v-model="nodeForm.name" class="field-input" placeholder="实体名称" />
        <label class="field-label">类型</label>
        <select v-model="nodeForm.type" class="field-input">
          <option v-for="(label, t) in TYPE_LABELS" :key="t" :value="t">{{ label }}</option>
        </select>
        <label class="field-label">备注（可选）</label>
        <textarea v-model="nodeForm.note" class="field-input" rows="2"></textarea>
        <p v-if="formError" class="form-error">{{ formError }}</p>
        <div class="modal-actions">
          <button class="btn-ghost" @click="nodeFormVisible = false">取消</button>
          <button class="btn-primary" :disabled="saving" @click="submitNode">创建</button>
        </div>
      </div>
    </div>

    <!-- 新建关系 -->
    <div v-if="edgeFormVisible" class="modal-mask" @click.self="edgeFormVisible = false">
      <div class="modal-card">
        <h3>新建关系</h3>
        <label class="field-label">起点节点</label>
        <select v-model="edgeForm.source" class="field-input">
          <option value="" disabled>选择节点</option>
          <option v-for="n in nodes" :key="n.id" :value="n.name">{{ n.name }}</option>
        </select>
        <label class="field-label">终点节点</label>
        <select v-model="edgeForm.target" class="field-input">
          <option value="" disabled>选择节点</option>
          <option v-for="n in nodes" :key="n.id" :value="n.name">{{ n.name }}</option>
        </select>
        <label class="field-label">关系标签</label>
        <input v-model="edgeForm.label" class="field-input" placeholder="例如：属于 / 推荐 / 禁忌" />
        <p v-if="formError" class="form-error">{{ formError }}</p>
        <div class="modal-actions">
          <button class="btn-ghost" @click="edgeFormVisible = false">取消</button>
          <button class="btn-primary" :disabled="saving" @click="submitEdge">创建</button>
        </div>
      </div>
    </div>

    <!-- 导入 JSONL -->
    <div v-if="importVisible" class="modal-mask" @click.self="importVisible = false">
      <div class="modal-card wide">
        <h3>导入 JSONL</h3>
        <p class="import-hint">
          每行一个 JSON：{"kind":"node","name":"名称","type":"concept","note":"备注"}
          或 {"kind":"edge","source":"起点","target":"终点","label":"关系"}
        </p>
        <input type="file" accept=".jsonl,.json,.txt" @change="loadImportFile" />
        <textarea v-model="importText" class="field-input import-area" rows="8" placeholder='{"kind":"node","name":"糖尿病","type":"concept"}'></textarea>
        <p v-if="formError" class="form-error">{{ formError }}</p>
        <div class="modal-actions">
          <button class="btn-ghost" @click="importVisible = false">取消</button>
          <button class="btn-primary" :disabled="saving || !importText.trim()" @click="submitImport">导入</button>
        </div>
      </div>
    </div>

    <!-- 节点溯源引用详情（需求3） -->
    <div v-if="sourceModal.visible" class="modal-mask" @click.self="closeSource">
      <div class="modal-card wide source-modal">
        <div class="source-head">
          <span v-if="sourceModal.kind" class="chip-tag" :class="sourceModal.kind">{{ sourceModal.kind === 'wiki' ? '百科' : '文档' }}</span>
          <h3>{{ sourceModal.title }}</h3>
          <button class="info-close" @click="closeSource">&times;</button>
        </div>
        <div class="source-body">
          <p v-if="sourceModal.loading" class="state-hint">加载中…</p>
          <p v-else-if="sourceModal.error" class="state-hint error">{{ sourceModal.error }}</p>
          <template v-else>
            <img v-if="sourceModal.sourceType === 'image' && sourceModal.filename" :src="getImageUrl(sourceModal.filename)" class="source-image" alt="来源图片" />
            <div v-else-if="sourceModal.content" class="markdown-content" v-html="renderedSource"></div>
            <p v-else class="state-hint">该来源为{{ sourceModal.sourceType || '文件' }}，暂无可内联展示的文本内容，可点击下方按钮打开原文。</p>
            <button
              v-if="sourceModal.filename && sourceModal.sourceType !== 'md' && sourceModal.sourceType !== 'txt'"
              class="btn-tool source-open" @click="openSourceFile"
            >打开原文预览</button>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'
import MarkdownIt from 'markdown-it'
import {
  getKbGraph, rebuildKbGraph, clearKbGraph,
  addKbGraphNode, deleteKbGraphNode, addKbGraphEdge, deleteKbGraphEdge,
  importKbGraphJsonl, searchKbGraphSemantic, getKbGraphSourceContent, getImageUrl,
  listKbDocuments,
} from '@/api'

const props = defineProps({
  kbId: { type: String, required: true },
})

const TYPE_COLORS = { person: '#e0705f', org: '#5b8fd9', product: '#5cb87a', concept: '#c98a4b', topic: '#9b6fc3' }
const TYPE_LABELS = { person: '人物', org: '组织', product: '产品', concept: '概念', topic: '主题' }

const nodes = ref([])
const edges = ref([])
const loading = ref(true)
const message = ref('')
const messageError = ref(false)

// 图谱范围：'' = 整库（含手动节点），否则为单篇文档 source_image（实时聚合子图，不落库）
const docPath = ref('')
const docs = ref([])
const docScope = computed(() => !!docPath.value)
async function loadDocs() {
  try {
    const res = await listKbDocuments(props.kbId)
    docs.value = res.data.documents || []
  } catch (e) { /* 文档清单加载失败不阻断整库视图 */ }
}
function onScopeChange() {
  stopPolling()
  job.value = null
  load()
}

// 后台重建任务（需求4）：job.status==='running' 时按钮置灰 + 轮询，不阻主线程
const job = ref(null)
const rebuilding = computed(() => job.value?.status === 'running')
let pollTimer = null

const nodeQuery = ref('')
const semanticQuery = ref('')
const topN = ref(10)
const threshold = ref(0.5)
const semanticBusy = ref(false)
const semanticHits = ref(null)
const semanticScores = ref({})

const selectedNode = ref(null)
const selectedEdge = ref(null)

const canvasRef = ref(null)
const svgRef = ref(null)
const view = reactive({ x: 0, y: 0, k: 1 })
const drag = ref(null)

const byName = computed(() => new Map(nodes.value.map((n) => [n.name, n])))
const renderEdges = computed(() =>
  edges.value
    .map((e) => ({ ...e, s: byName.value.get(e.source), t: byName.value.get(e.target) }))
    .filter((e) => e.s && e.t),
)
const relatedEdges = computed(() =>
  selectedNode.value
    ? edges.value.filter((e) => e.source === selectedNode.value.name || e.target === selectedNode.value.name)
    : [],
)

function nodeRadius(n) {
  const degree = edges.value.filter((e) => e.source === n.name || e.target === n.name).length
  return Math.min(10 + degree * 1.5, 22)
}

function edgeEnd(e) {
  const dx = e.t.x - e.s.x
  const dy = e.t.y - e.s.y
  const d = Math.sqrt(dx * dx + dy * dy) || 1
  const r = nodeRadius(e.t) + 4
  return { x: e.t.x - (dx / d) * r, y: e.t.y - (dy / d) * r }
}

function dimNode(n) {
  if (semanticHits.value) return !semanticHits.value.has(n.id)
  const q = nodeQuery.value.trim().toLowerCase()
  if (!q) return false
  return !n.name.toLowerCase().includes(q)
}

function dimEdge(e) {
  return dimNode(e.s) || dimNode(e.t)
}

// ---- 力导向布局（同步迭代收敛，随后静态渲染 + 拖拽） ----
function runLayout() {
  const ns = nodes.value
  if (!ns.length) return
  const radius = Math.max(120, Math.min(380, ns.length * 14))
  ns.forEach((n, i) => {
    const a = (2 * Math.PI * i) / ns.length
    n.x = Math.cos(a) * radius
    n.y = Math.sin(a) * radius
  })
  const springs = renderEdges.value
  const REPULSE = 5200
  const SPRING = 0.035
  const LEN = 130
  const CENTER = 0.012
  for (let iter = 0; iter < 260; iter++) {
    const fx = new Array(ns.length).fill(0)
    const fy = new Array(ns.length).fill(0)
    for (let i = 0; i < ns.length; i++) {
      for (let j = i + 1; j < ns.length; j++) {
        let dx = ns[i].x - ns[j].x
        let dy = ns[i].y - ns[j].y
        let d2 = dx * dx + dy * dy
        if (d2 < 1) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 1 }
        const f = REPULSE / (d2 + 60)
        const d = Math.sqrt(d2)
        fx[i] += (dx / d) * f; fy[i] += (dy / d) * f
        fx[j] -= (dx / d) * f; fy[j] -= (dy / d) * f
      }
    }
    const idx = new Map(ns.map((n, i) => [n, i]))
    for (const e of springs) {
      const i = idx.get(e.s)
      const j = idx.get(e.t)
      const dx = e.t.x - e.s.x
      const dy = e.t.y - e.s.y
      const d = Math.sqrt(dx * dx + dy * dy) || 1
      const f = SPRING * (d - LEN)
      fx[i] += (dx / d) * f; fy[i] += (dy / d) * f
      fx[j] -= (dx / d) * f; fy[j] -= (dy / d) * f
    }
    const damp = 0.6 + 0.3 * (1 - iter / 260)
    for (let i = 0; i < ns.length; i++) {
      fx[i] -= ns[i].x * CENTER
      fy[i] -= ns[i].y * CENTER
      const cap = 14
      ns[i].x += Math.max(-cap, Math.min(cap, fx[i] * damp))
      ns[i].y += Math.max(-cap, Math.min(cap, fy[i] * damp))
    }
  }
  fitView()
}

function fitView() {
  const el = canvasRef.value
  if (!el || !nodes.value.length) return
  const { width, height } = el.getBoundingClientRect()
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  for (const n of nodes.value) {
    minX = Math.min(minX, n.x); maxX = Math.max(maxX, n.x)
    minY = Math.min(minY, n.y); maxY = Math.max(maxY, n.y)
  }
  const pad = 70
  const bw = maxX - minX + pad * 2
  const bh = maxY - minY + pad * 2
  const k = Math.max(0.2, Math.min(1.4, Math.min(width / bw, height / bh)))
  view.k = k
  view.x = width / 2 - ((minX + maxX) / 2) * k
  view.y = height / 2 - ((minY + maxY) / 2) * k
}

// ---- 交互：缩放 / 平移 / 节点拖拽 ----
function toWorld(clientX, clientY) {
  const rect = svgRef.value.getBoundingClientRect()
  return { x: (clientX - rect.left - view.x) / view.k, y: (clientY - rect.top - view.y) / view.k }
}

function onWheel(e) {
  const rect = svgRef.value.getBoundingClientRect()
  const mx = e.clientX - rect.left
  const my = e.clientY - rect.top
  const k = Math.max(0.15, Math.min(3.5, view.k * (e.deltaY < 0 ? 1.15 : 0.87)))
  view.x = mx - ((mx - view.x) / view.k) * k
  view.y = my - ((my - view.y) / view.k) * k
  view.k = k
}

function onBackgroundDown(e) {
  selectedNode.value = null
  selectedEdge.value = null
  drag.value = { kind: 'pan', startX: e.clientX, startY: e.clientY, ox: view.x, oy: view.y, moved: false }
  svgRef.value.setPointerCapture(e.pointerId)
}

function onNodeDown(e, n) {
  const w = toWorld(e.clientX, e.clientY)
  drag.value = { kind: 'node', node: n, dx: n.x - w.x, dy: n.y - w.y, startX: e.clientX, startY: e.clientY, moved: false }
  svgRef.value.setPointerCapture(e.pointerId)
}

function onPointerMove(e) {
  if (!drag.value) return
  const d = drag.value
  if (Math.abs(e.clientX - d.startX) + Math.abs(e.clientY - d.startY) > 4) d.moved = true
  if (d.kind === 'pan') {
    view.x = d.ox + (e.clientX - d.startX)
    view.y = d.oy + (e.clientY - d.startY)
  } else {
    const w = toWorld(e.clientX, e.clientY)
    d.node.x = w.x + d.dx
    d.node.y = w.y + d.dy
  }
}

function onPointerUp(e) {
  const d = drag.value
  drag.value = null
  if (!d) return
  if (d.kind === 'node' && !d.moved) {
    selectedNode.value = d.node
    selectedEdge.value = null
  }
}

function selectEdge(e) {
  selectedEdge.value = e
  selectedNode.value = null
}

// ---- 数据加载与操作 ----
function applyGraph(graph) {
  nodes.value = (graph.nodes || []).map((n) => ({ ...n, x: 0, y: 0 }))
  edges.value = graph.edges || []
  selectedNode.value = null
  selectedEdge.value = null
  semanticHits.value = null
  semanticScores.value = {}
  nextTick(() => { runLayout() })
}

async function load() {
  loading.value = true
  message.value = ''
  try {
    const res = await getKbGraph(props.kbId, docPath.value)
    applyGraph(res.data.graph || {})
    job.value = res.data.job || null
    if (rebuilding.value) startPolling()
  } catch (e) {
    message.value = e.response?.data?.detail || '加载图谱失败'
    messageError.value = true
  } finally {
    loading.value = false
  }
}

function notify(text, isError = false) {
  message.value = text
  messageError.value = isError
}

// ---- 后台重建轮询（需求4）：不阻塞主线程，完成后自动刷新 ----
function startPolling() {
  stopPolling()
  pollTimer = setInterval(pollJob, 2500)
}
function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}
async function pollJob() {
  try {
    const res = await getKbGraph(props.kbId, docPath.value)
    job.value = res.data.job || null
    if (!rebuilding.value) {
      stopPolling()
      applyGraph(res.data.graph || {})
      if (job.value?.status === 'done') {
        notify(`重建完成：${nodes.value.length} 节点 / ${edges.value.length} 关系（来自知识百科）`)
      } else if (job.value?.status === 'failed') {
        notify(`重建失败：${job.value.error || '未知错误'}`, true)
      }
    }
  } catch (e) { /* 轮询瞬时错误忽略，下一轮重试 */ }
}

async function rebuild() {
  message.value = ''
  try {
    const res = await rebuildKbGraph(props.kbId, docPath.value)
    if (res.data.started === false) {
      // 单篇文档子图：后端实时聚合直接返回，无需后台任务
      applyGraph(res.data.graph || {})
      notify(`已刷新该文档子图：${nodes.value.length} 节点 / ${edges.value.length} 关系`)
      return
    }
    job.value = res.data.job || { status: 'running' }
    notify('正在后台重建索引，完成后自动刷新…')
    startPolling()
  } catch (e) {
    if (e.response?.status === 409) {
      job.value = { status: 'running' }
      notify('正在后台生成中，请稍后…')
      startPolling()
    } else {
      notify(e.response?.data?.detail || '重建失败', true)
    }
  }
}

// ---- 节点溯源引用（需求3）：点击信息卡引用查看文档/百科页详细内容 ----
const md = new MarkdownIt({ html: false, linkify: true, breaks: true })
const sourceModal = reactive({
  visible: false, loading: false, title: '', kind: '', content: '',
  sourceType: '', filename: '', pdfPreviewName: '', error: '',
})
const renderedSource = computed(() => (sourceModal.content ? md.render(sourceModal.content) : ''))

async function openSource(src) {
  sourceModal.visible = true
  sourceModal.loading = true
  sourceModal.error = ''
  sourceModal.content = ''
  sourceModal.sourceType = ''
  sourceModal.filename = ''
  sourceModal.pdfPreviewName = ''
  sourceModal.title = src.title || '来源详情'
  sourceModal.kind = src.kind || ''
  try {
    const res = await getKbGraphSourceContent(props.kbId, { title: src.title || '', path: src.path || '' })
    const d = res.data
    sourceModal.kind = d.kind || src.kind
    sourceModal.title = d.title || src.title || '来源详情'
    sourceModal.content = d.content || ''
    sourceModal.sourceType = d.source_type || ''
    sourceModal.filename = d.filename || ''
    sourceModal.pdfPreviewName = d.pdf_preview_name || ''
  } catch (e) {
    sourceModal.error = e.response?.data?.detail || '加载来源内容失败'
  } finally {
    sourceModal.loading = false
  }
}
function closeSource() { sourceModal.visible = false }
function openSourceFile() {
  const filename = sourceModal.filename
  if (!filename) return
  if (sourceModal.sourceType === 'word' && sourceModal.pdfPreviewName) {
    window.open(`/doc-preview?file=${encodeURIComponent(filename)}&pdf=${encodeURIComponent(sourceModal.pdfPreviewName)}`, '_blank')
    return
  }
  window.open(getImageUrl(filename), '_blank')
}

async function clearGraph() {
  if (!window.confirm('确定清空当前知识库图谱？手动创建的节点与关系也会一并删除。')) return
  try {
    await clearKbGraph(props.kbId)
    applyGraph({ nodes: [], edges: [] })
    notify('图谱已清空')
  } catch (e) {
    notify(e.response?.data?.detail || '清空失败', true)
  }
}

async function runSemantic() {
  const q = semanticQuery.value.trim()
  if (!q) return
  semanticBusy.value = true
  try {
    const res = await searchKbGraphSemantic(props.kbId, q, topN.value, threshold.value)
    const matches = res.data.matches || []
    semanticHits.value = new Set(matches.map((m) => m.node.id))
    const scores = {}
    matches.forEach((m) => { scores[m.node.id] = m.score.toFixed(2) })
    semanticScores.value = scores
    notify(matches.length ? `语义搜索命中 ${matches.length} 个节点` : '没有达到阈值的节点')
  } catch (e) {
    notify(e.response?.data?.detail || '语义搜索失败', true)
  } finally {
    semanticBusy.value = false
  }
}

// ---- 新建节点 / 关系 / 导入 ----
const nodeFormVisible = ref(false)
const edgeFormVisible = ref(false)
const importVisible = ref(false)
const saving = ref(false)
const formError = ref('')
const nodeForm = ref({ name: '', type: 'concept', note: '' })
const edgeForm = ref({ source: '', target: '', label: '' })
const importText = ref('')

function openNodeForm() {
  nodeForm.value = { name: '', type: 'concept', note: '' }
  formError.value = ''
  nodeFormVisible.value = true
}

function openEdgeForm() {
  edgeForm.value = { source: '', target: '', label: '' }
  formError.value = ''
  edgeFormVisible.value = true
}

async function submitNode() {
  if (!nodeForm.value.name.trim()) { formError.value = '请输入节点名称'; return }
  saving.value = true
  formError.value = ''
  try {
    await addKbGraphNode(props.kbId, nodeForm.value)
    nodeFormVisible.value = false
    await load()
  } catch (e) {
    formError.value = e.response?.data?.detail || '创建失败'
  } finally {
    saving.value = false
  }
}

async function submitEdge() {
  if (!edgeForm.value.source || !edgeForm.value.target) { formError.value = '请选择起点与终点节点'; return }
  saving.value = true
  formError.value = ''
  try {
    await addKbGraphEdge(props.kbId, edgeForm.value)
    edgeFormVisible.value = false
    await load()
  } catch (e) {
    formError.value = e.response?.data?.detail || '创建失败'
  } finally {
    saving.value = false
  }
}

function loadImportFile(e) {
  const file = e.target.files?.[0]
  if (!file) return
  const reader = new FileReader()
  reader.onload = () => { importText.value = String(reader.result || '') }
  reader.readAsText(file)
}

async function submitImport() {
  saving.value = true
  formError.value = ''
  try {
    const res = await importKbGraphJsonl(props.kbId, importText.value)
    importVisible.value = false
    importText.value = ''
    await load()
    const errs = res.data.errors || []
    notify(`导入完成：新增 ${res.data.added_nodes} 节点 / ${res.data.added_edges} 关系` + (errs.length ? `；${errs.length} 行有误（${errs[0]}）` : ''), !!errs.length)
  } catch (e) {
    formError.value = e.response?.data?.detail || '导入失败'
  } finally {
    saving.value = false
  }
}

async function removeNode(n) {
  if (!window.confirm(`确定删除节点「${n.name}」？其关联关系也会删除。`)) return
  try {
    await deleteKbGraphNode(props.kbId, n.id)
    await load()
  } catch (e) {
    notify(e.response?.data?.detail || '删除失败', true)
  }
}

async function removeEdge(e) {
  try {
    await deleteKbGraphEdge(props.kbId, e.id)
    selectedEdge.value = null
    await load()
  } catch (err) {
    notify(err.response?.data?.detail || '删除失败', true)
  }
}

onMounted(() => { loadDocs(); load() })
onUnmounted(stopPolling)
</script>

<style scoped>
.kb-graph { display: flex; flex-direction: column; height: 100%; padding: 16px 24px 20px; box-sizing: border-box; gap: 10px; }

.graph-toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.scope-select {
  border: 1px solid var(--color-border); border-radius: 8px; padding: 6px 10px;
  font-size: 12.5px; background: #fff; color: var(--color-text); outline: none; max-width: 240px;
}
.scope-select:focus { border-color: var(--color-primary); }
.search-box {
  display: flex; align-items: center; gap: 6px; background: #fff;
  border: 1px solid var(--color-border); border-radius: 8px; padding: 6px 10px;
  width: 180px; color: var(--color-text-secondary);
}
.search-box:focus-within { border-color: var(--color-primary); }
.search-box input { border: none; outline: none; flex: 1; font-size: 12.5px; background: transparent; color: var(--color-text); min-width: 0; }
.clear-btn { border: none; background: none; font-size: 15px; cursor: pointer; color: var(--color-text-secondary); line-height: 1; }

.semantic-group { display: flex; align-items: center; gap: 6px; }
.semantic-input {
  border: 1px solid var(--color-border); border-radius: 8px; padding: 6px 10px;
  font-size: 12.5px; width: 170px; outline: none; background: #fff; color: var(--color-text);
}
.semantic-input:focus { border-color: var(--color-primary); }
.mini-select { border: 1px solid var(--color-border); border-radius: 8px; padding: 5px 6px; font-size: 12px; background: #fff; outline: none; }
.threshold-label { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; color: var(--color-text-secondary); }
.threshold-input { width: 52px; border: 1px solid var(--color-border); border-radius: 6px; padding: 4px 6px; font-size: 12px; outline: none; }

.graph-counts { margin-left: auto; font-size: 12px; color: var(--color-text-secondary); white-space: nowrap; }

.graph-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.btn-tool {
  border: 1px solid var(--color-border); background: #fff; border-radius: 8px;
  padding: 6px 13px; font-size: 12.5px; color: var(--color-text); cursor: pointer;
}
.btn-tool:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
.btn-tool:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-tool.danger { color: #c0392b; }
.btn-tool.danger:hover:not(:disabled) { border-color: #c0392b; }
.btn-tool.block { display: block; width: 100%; margin-top: 10px; }

.legend { margin-left: auto; display: flex; gap: 10px; }
.legend-item { display: inline-flex; align-items: center; gap: 4px; font-size: 11.5px; color: var(--color-text-secondary); }
.legend-item i { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }

.msg { margin: 0; font-size: 12.5px; color: var(--color-text-secondary); }
.msg.error { color: #c0392b; }

.graph-canvas {
  position: relative; flex: 1; min-height: 320px;
  background: #fff; border: 1px solid var(--color-border); border-radius: 10px; overflow: hidden;
}
.graph-canvas.dragging { cursor: grabbing; }
.graph-svg { width: 100%; height: 100%; display: block; touch-action: none; cursor: grab; }
.node-g { cursor: pointer; }
.edge-g { cursor: pointer; }

.canvas-state {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  color: var(--color-text-secondary); font-size: 13.5px; padding: 0 40px; text-align: center;
}

.info-card {
  position: absolute; top: 12px; right: 12px; width: 260px;
  background: #fff; border: 1px solid var(--color-border); border-radius: 10px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.08); padding: 12px 14px; font-size: 12.5px;
}
.info-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.info-head strong { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.info-type { color: #fff; font-size: 10.5px; padding: 1px 7px; border-radius: 5px; flex-shrink: 0; }
.info-close { border: none; background: none; font-size: 16px; cursor: pointer; color: var(--color-text-secondary); line-height: 1; }
.info-note { margin: 0 0 6px; color: #5a4f42; line-height: 1.6; word-break: break-word; }
.info-meta { margin: 0 0 8px; color: var(--color-text-secondary); font-size: 11.5px; }
.info-edges { max-height: 140px; overflow-y: auto; display: flex; flex-direction: column; gap: 4px; }
.info-edge { display: flex; align-items: center; justify-content: space-between; gap: 6px; font-size: 11.5px; color: #5a4f42; }
.info-edge span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.link-btn { border: none; background: none; color: var(--color-primary); font-size: 11.5px; cursor: pointer; flex-shrink: 0; padding: 0; }
.link-btn.danger { color: #c0392b; }

.modal-mask { position: fixed; inset: 0; background: rgba(45, 42, 36, 0.4); display: flex; align-items: center; justify-content: center; z-index: 100; }
.modal-card { background: #fff; border-radius: 14px; padding: 22px 24px; width: 380px; max-width: calc(100vw - 40px); box-shadow: 0 12px 40px rgba(0, 0, 0, 0.15); }
.modal-card.wide { width: 520px; }
.modal-card h3 { margin: 0 0 14px; font-size: 16px; color: var(--color-text); }
.field-label { display: block; font-size: 12.5px; color: var(--color-text-secondary); margin: 10px 0 5px; }
.field-input {
  width: 100%; box-sizing: border-box; border: 1px solid var(--color-border); border-radius: 8px;
  padding: 8px 11px; font-size: 13px; color: var(--color-text); background: #fff; outline: none;
  font-family: inherit;
}
.field-input:focus { border-color: var(--color-primary); }
.import-hint { font-size: 11.5px; color: var(--color-text-secondary); line-height: 1.6; margin: 0 0 10px; word-break: break-all; }
.import-area { margin-top: 10px; font-family: monospace; font-size: 12px; resize: vertical; }
.form-error { color: #c0392b; font-size: 12.5px; margin: 10px 0 0; }
.modal-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 18px; }
.btn-ghost { background: transparent; border: 1px solid var(--color-border); border-radius: 8px; padding: 7px 16px; font-size: 13px; color: var(--color-text); cursor: pointer; }
.btn-primary { background: var(--color-text); color: #fff; border: none; border-radius: 8px; padding: 7px 18px; font-size: 13px; cursor: pointer; }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }

/* 节点溯源引用 chips（需求3） */
.info-sources { margin: 0 0 8px; }
.info-sources-label { display: block; font-size: 11px; color: var(--color-text-secondary); margin-bottom: 5px; }
.source-chips { display: flex; flex-wrap: wrap; gap: 5px; }
.source-chip {
  display: inline-flex; align-items: center; gap: 4px; max-width: 100%;
  border: 1px solid var(--color-border); background: #fdfaf5; border-radius: 7px;
  padding: 3px 8px; font-size: 11.5px; color: #5a4f42; cursor: pointer;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.source-chip:hover { border-color: var(--color-primary); color: var(--color-primary); }
.chip-tag {
  flex-shrink: 0; font-style: normal; font-size: 10px; padding: 0 5px; border-radius: 4px;
  background: var(--color-primary-light); color: var(--color-primary);
}
.source-chip.wiki .chip-tag, .chip-tag.wiki { background: #eef3ff; color: #3b6ecc; }
.source-chip.doc .chip-tag, .chip-tag.doc { background: #eef7f0; color: #2e7d32; }

/* 溯源详情弹窗 */
.source-modal { width: 640px; max-height: calc(100vh - 80px); display: flex; flex-direction: column; }
.source-head { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
.source-head h3 { flex: 1; min-width: 0; margin: 0; font-size: 15px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.source-body { overflow-y: auto; flex: 1; min-height: 120px; }
.source-image { max-width: 100%; border-radius: 8px; border: 1px solid var(--color-border); display: block; margin: 0 auto; }
.source-open { margin-top: 12px; }
.state-hint { color: var(--color-text-secondary); font-size: 13px; text-align: center; padding: 24px 0; }
.state-hint.error { color: #c0392b; }
.markdown-content { font-size: 13.5px; line-height: 1.75; color: var(--color-text); word-break: break-word; }
.markdown-content :deep(h1), .markdown-content :deep(h2), .markdown-content :deep(h3) { margin: 14px 0 8px; line-height: 1.4; }
.markdown-content :deep(h1) { font-size: 18px; } .markdown-content :deep(h2) { font-size: 16px; } .markdown-content :deep(h3) { font-size: 14.5px; }
.markdown-content :deep(p) { margin: 8px 0; }
.markdown-content :deep(ul), .markdown-content :deep(ol) { margin: 8px 0; padding-left: 22px; }
.markdown-content :deep(code) { background: #f4efe7; padding: 1px 5px; border-radius: 4px; font-size: 12.5px; }
.markdown-content :deep(pre) { background: #f4efe7; padding: 10px 12px; border-radius: 8px; overflow-x: auto; }
.markdown-content :deep(a) { color: var(--color-primary); }
.markdown-content :deep(table) { border-collapse: collapse; margin: 8px 0; }
.markdown-content :deep(th), .markdown-content :deep(td) { border: 1px solid var(--color-border); padding: 5px 9px; font-size: 12.5px; }
.markdown-content :deep(blockquote) { margin: 8px 0; padding: 4px 12px; border-left: 3px solid var(--color-border); color: var(--color-text-secondary); }
</style>