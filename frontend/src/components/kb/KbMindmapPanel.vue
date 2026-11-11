<template>
  <div class="kb-mindmap">
    <div class="mindmap-toolbar">
      <span v-if="updatedAt" class="updated-at">生成于 {{ formatTime(updatedAt) }}</span>
      <span v-if="stale && !rebuilding" class="stale-badge">内容已更新，建议重新生成</span>
      <span v-if="rebuilding" class="gen-hint">正在后台生成中…</span>
      <button
        class="btn-rebuild" :disabled="rebuilding || loading"
        :title="rebuilding ? '正在后台生成中' : ''" @click="rebuild"
      >
        {{ rebuilding ? '生成中…' : (tree ? '重新生成' : '生成思维导图') }}
      </button>
    </div>

    <p v-if="error" class="msg error">{{ error }}</p>

    <div v-if="loading" class="state-box">加载中…</div>
    <div v-else-if="!tree && rebuilding" class="state-box">正在基于知识库内容生成思维导图，请稍候…</div>
    <div v-else-if="!tree" class="state-box">
      <p>{{ hasContent ? '尚未生成思维导图，点击右上角「生成思维导图」' : '知识库暂无内容，请先上传文档' }}</p>
    </div>

    <div v-else-if="layout" class="canvas-scroll">
      <svg :width="layout.width" :height="layout.height" class="mindmap-svg">
        <path
          v-for="(l, i) in layout.links"
          :key="'l' + i"
          :d="linkPath(l)"
          fill="none"
          stroke="#d8c9b4"
          stroke-width="1.6"
        />
        <g
          v-for="(n, i) in layout.nodes" :key="'n' + i"
          :class="{ clickable: !!n.src }"
          @click="onNodeClick(n)"
        >
          <rect
            :x="n.x" :y="n.y" :width="n.w" :height="NODE_H" :rx="NODE_H / 2"
            :fill="nodeFill(n.depth)" :stroke="n.src ? '#c98a4b' : nodeStroke(n.depth)" :stroke-width="n.src ? 1.6 : 1"
          />
          <text
            :x="n.x + n.w / 2" :y="n.y + NODE_H / 2"
            text-anchor="middle" dominant-baseline="central"
            :font-size="n.depth === 0 ? 14 : 12.5"
            :font-weight="n.depth <= 1 ? 600 : 400"
            :fill="n.depth === 0 ? '#fff' : '#4a3f35'"
          >{{ n.label }}</text>
          <title v-if="n.src">来源：{{ n.src }}（点击跳转至文档）</title>
        </g>
      </svg>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { getKbMindmap, rebuildKbMindmap } from '@/api'

const props = defineProps({
  kbId: { type: String, required: true },
})
const emit = defineEmits(['jump-doc'])

const NODE_H = 30
const ROW_H = 46
const GAP_X = 56

const tree = ref(null)
const updatedAt = ref('')
const stale = ref(false)
const hasContent = ref(false)
const loading = ref(true)
const error = ref('')

// 后台生成任务（需求4）：job.status==='running' 时按钮置灰 + 轮询，不阻主线程
const job = ref(null)
const rebuilding = computed(() => job.value?.status === 'running')
let pollTimer = null

function formatTime(t) {
  return t ? String(t).replace('T', ' ').slice(0, 16) : ''
}

function nodeFill(depth) {
  if (depth === 0) return '#c98a4b'
  if (depth === 1) return '#f9eddb'
  return '#ffffff'
}

function nodeStroke(depth) {
  if (depth === 0) return '#c98a4b'
  if (depth === 1) return '#e2c39a'
  return '#ede6dc'
}

function measure(name) {
  return Math.min(name.length * 13 + 28, 320)
}

const layout = computed(() => {
  if (!tree.value) return null
  const colWidth = []
  function scan(n, d) {
    colWidth[d] = Math.max(colWidth[d] || 0, measure(n.name || ''))
    ;(n.children || []).forEach((c) => scan(c, d + 1))
  }
  scan(tree.value, 0)
  const colX = [24]
  for (let d = 1; d < colWidth.length; d++) colX[d] = colX[d - 1] + colWidth[d - 1] + GAP_X

  const nodes = []
  const links = []
  let y = 24
  function place(n, d, parent) {
    const name = n.name || ''
    const w = measure(name)
    const maxChars = Math.floor((w - 24) / 13)
    const node = {
      name,
      label: name.length > maxChars ? name.slice(0, maxChars - 1) + '…' : name,
      depth: d, x: colX[d], w, y: 0, src: n.src || '',
    }
    nodes.push(node)
    const kids = n.children || []
    if (!kids.length) {
      node.y = y
      y += ROW_H
    } else {
      const placed = kids.map((k) => place(k, d + 1, node))
      node.y = (placed[0].y + placed[placed.length - 1].y) / 2
    }
    if (parent) links.push({ from: parent, to: node })
    return node
  }
  place(tree.value, 0, null)
  const last = colWidth.length - 1
  return { nodes, links, width: colX[last] + colWidth[last] + 24, height: Math.max(y + 10, 120) }
})

function linkPath(l) {
  const x1 = l.from.x + l.from.w
  const y1 = l.from.y + NODE_H / 2
  const x2 = l.to.x
  const y2 = l.to.y + NODE_H / 2
  const mx = (x1 + x2) / 2
  return `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await getKbMindmap(props.kbId)
    applyPayload(res.data)
    job.value = res.data.job || null
    if (rebuilding.value) startPolling()
  } catch (e) {
    error.value = e.response?.data?.detail || '加载思维导图失败'
  } finally {
    loading.value = false
  }
}

function applyPayload(data) {
  tree.value = data.tree || null
  updatedAt.value = data.updated_at || ''
  stale.value = !!data.stale
  hasContent.value = !!data.has_content
}

// ---- 后台生成轮询（需求4） ----
function startPolling() {
  stopPolling()
  pollTimer = setInterval(pollJob, 2500)
}
function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}
async function pollJob() {
  try {
    const res = await getKbMindmap(props.kbId)
    job.value = res.data.job || null
    if (!rebuilding.value) {
      stopPolling()
      applyPayload(res.data)
      if (job.value?.status === 'failed') error.value = `生成失败：${job.value.error || '未知错误'}`
    }
  } catch (e) { /* 轮询瞬时错误忽略，下一轮重试 */ }
}

async function rebuild() {
  error.value = ''
  try {
    const res = await rebuildKbMindmap(props.kbId)
    job.value = res.data.job || { status: 'running' }
    startPolling()
  } catch (e) {
    if (e.response?.status === 409) {
      job.value = { status: 'running' }
      startPolling()
    } else {
      error.value = e.response?.data?.detail || '生成失败，请重试'
    }
  }
}

// 点击带溯源的节点 → 通知父视图跳转至对应文档（需求1）
function onNodeClick(n) {
  if (n.src) emit('jump-doc', n.src)
}

onMounted(load)
onUnmounted(stopPolling)
</script>

<style scoped>
.kb-mindmap { display: flex; flex-direction: column; height: 100%; padding: 20px 24px; box-sizing: border-box; }

.mindmap-toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; }
.updated-at { font-size: 12px; color: var(--color-text-secondary); }
.stale-badge { font-size: 12px; color: #b8860b; background: #fff8e1; padding: 2px 10px; border-radius: 6px; }
.gen-hint { font-size: 12px; color: var(--color-primary); background: var(--color-primary-light); padding: 2px 10px; border-radius: 6px; }
.btn-rebuild {
  margin-left: auto; background: var(--color-text); color: #fff; border: none;
  border-radius: 8px; padding: 8px 18px; font-size: 13px; cursor: pointer;
}
.btn-rebuild:hover:not(:disabled) { opacity: 0.85; }
.btn-rebuild:disabled { opacity: 0.5; cursor: not-allowed; }

.msg { font-size: 13px; margin: 0 0 10px; }
.msg.error { color: #c0392b; }

.state-box { flex: 1; display: flex; align-items: center; justify-content: center; color: var(--color-text-secondary); font-size: 14px; text-align: center; }

.canvas-scroll { flex: 1; overflow: auto; background: #fff; border: 1px solid var(--color-border); border-radius: 10px; }
.mindmap-svg { display: block; }
.mindmap-svg g.clickable { cursor: pointer; }
.mindmap-svg g.clickable:hover rect { filter: brightness(0.97); }
</style>