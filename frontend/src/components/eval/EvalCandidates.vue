<template>
  <div class="drawer-overlay" @click.self="$emit('close')">
    <aside class="drawer">
      <div class="drawer-head">
        <span class="drawer-title">候选审核</span>
        <span class="drawer-sub">{{ datasetName || '全部评测集' }}</span>
        <span v-if="pendingCount" class="pending-badge">{{ pendingCount }} 待审</span>
        <button class="icon-btn" title="关闭" @click="$emit('close')">✕</button>
      </div>

      <div class="cand-tabs">
        <button v-for="t in STATUS_TABS" :key="t.key" class="cand-tab" :class="{ active: status === t.key }" @click="switchStatus(t.key)">
          {{ t.label }}
        </button>
        <div class="cand-tabs-spacer"></div>
        <button v-if="selected.size" class="btn btn-primary xs" :disabled="batching" @click="doBatchApprove">
          {{ batching ? '通过中…' : `批量通过 (${selected.size})` }}
        </button>
      </div>

      <p v-if="errorMsg" class="inline-error">{{ errorMsg }}</p>
      <div class="drawer-body">
        <div v-if="loading && !items.length" class="mini-loading">加载候选中…</div>
        <div v-else-if="!items.length" class="empty-block">
          <p>{{ status === 'pending' ? '暂无待审候选。可在评测集「构建」菜单用 LLM 基于知识库生成，或触发日志挖掘。' : '该状态下暂无候选。' }}</p>
        </div>

        <div v-for="c in items" :key="c.id" class="cand-card" :class="{ checked: selected.has(c.id) }">
          <div class="cand-head">
            <label v-if="c.status === 'pending'" class="cand-check">
              <input type="checkbox" :checked="selected.has(c.id)" @change="toggleSelect(c.id)" />
            </label>
            <span class="chip origin" :class="'origin-' + c.origin">{{ ORIGIN_LABELS[c.origin] || c.origin }}</span>
            <span class="chip st" :class="'st-' + c.status">{{ STATUS_LABELS[c.status] || c.status }}</span>
            <span class="cand-time">{{ (c.created_at || '').slice(0, 16) }}</span>
          </div>

          <template v-if="editingId === c.id">
            <label class="field">
              <span class="field-label">问题</span>
              <textarea class="input textarea" v-model="editForm.question" rows="2"></textarea>
            </label>
            <label class="field">
              <span class="field-label">参考答案</span>
              <textarea class="input textarea" v-model="editForm.reference_answer" rows="3"></textarea>
            </label>
            <label class="field">
              <span class="field-label">期望关键词（逗号分隔）</span>
              <input class="input" v-model="editForm.keywords" />
            </label>
            <label class="field">
              <span class="field-label">期望来源</span>
              <input class="input" v-model="editForm.expected_source" />
            </label>
            <div class="cand-actions">
              <button class="btn btn-primary xs" :disabled="saving" @click="saveApprove(c)">通过并入集</button>
              <button class="btn btn-ghost xs" @click="editingId = ''">取消</button>
            </div>
          </template>
          <template v-else>
            <div class="cand-q">{{ c.question }}</div>
            <div v-if="c.reference_answer" class="cand-a">{{ c.reference_answer }}</div>
            <div class="cand-meta">
              <span v-if="c.expected_keywords && c.expected_keywords.length" class="kw-wrap">
                <span v-for="(k, i) in c.expected_keywords" :key="i" class="kw-tag">{{ k }}</span>
              </span>
              <span v-if="c.expected_source" class="src-tag">来源：{{ c.expected_source }}</span>
            </div>
            <div v-if="c.review_note" class="cand-note">审核备注：{{ c.review_note }}</div>
            <div v-if="c.status === 'pending'" class="cand-actions">
              <button class="btn btn-primary xs" @click="startEdit(c)">编辑后通过</button>
              <button class="btn btn-outline xs" :disabled="saving" @click="quickApprove(c)">直接通过</button>
              <button class="btn btn-ghost xs" :disabled="saving" @click="doReject(c)">拒绝</button>
            </div>
          </template>
        </div>

        <div v-if="total > items.length || offset > 0" class="cand-pager">
          <button class="btn btn-outline xs" :disabled="offset <= 0 || loading" @click="page(-1)">上一页</button>
          <span class="cand-page-text">{{ offset + 1 }}–{{ Math.min(offset + items.length, total) }} / {{ total }}</span>
          <button class="btn btn-outline xs" :disabled="offset + items.length >= total || loading" @click="page(1)">下一页</button>
        </div>
      </div>
    </aside>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import {
  listEvalCandidates, approveEvalCandidate, rejectEvalCandidate, batchApproveEvalCandidates,
} from '@/api'

const props = defineProps({
  datasetId: { type: String, default: '' },
  datasetName: { type: String, default: '' },
})
const emit = defineEmits(['close', 'changed'])

const ORIGIN_LABELS = { llm_gen: 'LLM 生成', log_mining: '日志挖掘', manual: '手动', badcase: 'BadCase' }
const STATUS_LABELS = { pending: '待审', approved: '已通过', rejected: '已拒绝' }
const STATUS_TABS = [
  { key: 'pending', label: '待审' },
  { key: 'approved', label: '已通过' },
  { key: 'rejected', label: '已拒绝' },
  { key: '', label: '全部' },
]
const PAGE_SIZE = 50

const status = ref('pending')
const items = ref([])
const total = ref(0)
const offset = ref(0)
const pendingCount = ref(0)
const loading = ref(false)
const saving = ref(false)
const batching = ref(false)
const errorMsg = ref('')
const selected = ref(new Set())
const editingId = ref('')
const editForm = reactive({ question: '', reference_answer: '', keywords: '', expected_source: '' })

onMounted(() => reload(true))

function errMsg(e, fallback) {
  return e?.response?.data?.message || e?.response?.data?.detail || e?.message || fallback
}

async function reload(reset = false) {
  if (reset) { offset.value = 0; selected.value = new Set() }
  loading.value = true
  errorMsg.value = ''
  try {
    const res = await listEvalCandidates({
      dataset_id: props.datasetId, status: status.value, limit: PAGE_SIZE, offset: offset.value,
    })
    items.value = res.data.items || []
    total.value = res.data.total || 0
    pendingCount.value = res.data.pending_count || 0
  } catch (e) {
    errorMsg.value = errMsg(e, '加载候选失败')
  } finally {
    loading.value = false
  }
}

function switchStatus(s) {
  status.value = s
  editingId.value = ''
  reload(true)
}

function page(dir) {
  const next = offset.value + dir * PAGE_SIZE
  if (next < 0 || next >= total.value) return
  offset.value = next
  reload()
}

function toggleSelect(id) {
  const next = new Set(selected.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  selected.value = next
}

function startEdit(c) {
  editingId.value = c.id
  editForm.question = c.question || ''
  editForm.reference_answer = c.reference_answer || ''
  editForm.keywords = (c.expected_keywords || []).join(', ')
  editForm.expected_source = c.expected_source || ''
}

function _edits() {
  return {
    question: editForm.question,
    reference_answer: editForm.reference_answer,
    expected_keywords: editForm.keywords.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
    expected_source: editForm.expected_source,
  }
}

async function saveApprove(c) {
  saving.value = true
  errorMsg.value = ''
  try {
    await approveEvalCandidate(c.id, _edits())
    editingId.value = ''
    emit('changed')
    await reload(true)
  } catch (e) {
    errorMsg.value = errMsg(e, '审核通过失败')
  } finally {
    saving.value = false
  }
}

async function quickApprove(c) {
  saving.value = true
  errorMsg.value = ''
  try {
    await approveEvalCandidate(c.id, {})
    emit('changed')
    await reload(true)
  } catch (e) {
    errorMsg.value = errMsg(e, '审核通过失败')
  } finally {
    saving.value = false
  }
}

async function doReject(c) {
  const note = window.prompt('拒绝备注（选填）：', '')
  if (note === null) return
  saving.value = true
  errorMsg.value = ''
  try {
    await rejectEvalCandidate(c.id, note)
    await reload(true)
  } catch (e) {
    errorMsg.value = errMsg(e, '拒绝失败')
  } finally {
    saving.value = false
  }
}

async function doBatchApprove() {
  batching.value = true
  errorMsg.value = ''
  try {
    const res = await batchApproveEvalCandidates([...selected.value])
    selected.value = new Set()
    emit('changed')
    await reload(true)
    if (res.data.skipped) errorMsg.value = `${res.data.approved} 条通过，${res.data.skipped} 条跳过（非待审）`
  } catch (e) {
    errorMsg.value = errMsg(e, '批量通过失败')
  } finally {
    batching.value = false
  }
}
</script>

<style scoped>
.drawer-overlay { position: fixed; inset: 0; background: rgba(45,42,36,0.4); display: flex; justify-content: flex-end; z-index: 100; }
.drawer { width: 560px; max-width: 94vw; height: 100%; background: #fff; display: flex; flex-direction: column; box-shadow: -8px 0 32px rgba(0,0,0,0.16); }
.drawer-head { display: flex; align-items: center; gap: 10px; padding: 16px 20px; border-bottom: 1px solid var(--color-border); }
.drawer-title { font-size: 15px; font-weight: 700; }
.drawer-sub { font-size: 12px; color: var(--color-text-secondary); max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pending-badge { font-size: 11px; font-weight: 700; background: #fdf0d8; color: #b5822a; padding: 2px 9px; border-radius: 12px; }
.drawer-head .icon-btn { margin-left: auto; }
.cand-tabs { display: flex; align-items: center; gap: 4px; padding: 10px 20px; border-bottom: 1px solid var(--color-border); }
.cand-tab { border: none; background: transparent; cursor: pointer; font-size: 12.5px; font-weight: 600; color: var(--color-text-secondary); padding: 5px 12px; border-radius: 7px; }
.cand-tab:hover { color: var(--color-primary); }
.cand-tab.active { background: var(--color-primary-light); color: var(--color-primary); }
.cand-tabs-spacer { flex: 1; }
.drawer-body { flex: 1; overflow-y: auto; padding: 16px 20px; display: flex; flex-direction: column; gap: 12px; }

.inline-error { background: #fdecea; border: 1px solid #f5c6c0; color: #c0392b; font-size: 12.5px; padding: 9px 13px; border-radius: 10px; }
.mini-loading { padding: 26px; text-align: center; color: var(--color-text-secondary); font-size: 12.5px; }
.empty-block { border: 1px dashed var(--color-border); border-radius: 12px; padding: 30px 16px; text-align: center; color: var(--color-text-secondary); font-size: 12.5px; line-height: 1.6; }

.cand-card { border: 1px solid var(--color-border); border-radius: 12px; padding: 13px 15px; display: flex; flex-direction: column; gap: 8px; background: #fffdfa; }
.cand-card.checked { border-color: var(--color-primary); background: var(--color-primary-light); }
.cand-head { display: flex; align-items: center; gap: 8px; }
.cand-check input { accent-color: var(--color-primary); cursor: pointer; }
.cand-time { margin-left: auto; font-size: 11px; color: var(--color-text-secondary); }
.chip { font-size: 11px; font-weight: 600; padding: 2px 9px; border-radius: 6px; }
.chip.origin-llm_gen { background: #e3edfd; color: #1d4ed8; }
.chip.origin-log_mining { background: #e6f4ea; color: #2e7d46; }
.chip.origin-manual { background: #f0ece6; color: #8a7e72; }
.chip.origin-badcase { background: #fdecea; color: #c0392b; }
.chip.st-pending { background: #fdf0d8; color: #b5822a; }
.chip.st-approved { background: #e6f4ea; color: #2e7d46; }
.chip.st-rejected { background: #f0ece6; color: #8a7e72; }
.cand-q { font-size: 13px; font-weight: 600; line-height: 1.5; }
.cand-a { font-size: 12px; color: var(--color-text-secondary); line-height: 1.55; white-space: pre-wrap; max-height: 120px; overflow-y: auto; }
.cand-meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.kw-wrap { display: flex; flex-wrap: wrap; gap: 4px; }
.kw-tag { background: var(--color-primary-light); color: #a9713a; font-size: 11px; padding: 1px 8px; border-radius: 20px; }
.src-tag { font-size: 11px; color: var(--color-text-secondary); }
.cand-note { font-size: 11.5px; color: #8a7e72; }
.cand-actions { display: flex; gap: 6px; flex-wrap: wrap; }
.cand-pager { display: flex; align-items: center; justify-content: center; gap: 12px; padding-top: 4px; }
.cand-page-text { font-size: 12px; color: var(--color-text-secondary); }

.field { display: flex; flex-direction: column; gap: 4px; }
.field-label { font-size: 11px; font-weight: 600; color: var(--color-text-secondary); }
.input { width: 100%; padding: 8px 10px; font-size: 12.5px; border: 1px solid var(--color-border); border-radius: 8px; background: #fff; color: var(--color-text); outline: none; font-family: inherit; box-sizing: border-box; }
.input:focus { border-color: var(--color-primary); }
.textarea { resize: vertical; line-height: 1.55; }

.btn { display: inline-flex; align-items: center; gap: 5px; border: 1px solid transparent; cursor: pointer; border-radius: 8px; font-weight: 600; font-size: 12.5px; padding: 7px 13px; transition: all 0.16s; font-family: inherit; }
.btn.xs { padding: 4px 10px; font-size: 11.5px; }
.btn:disabled { opacity: 0.55; cursor: not-allowed; }
.btn-primary { background: linear-gradient(135deg, #c98a4b, #d9a76a); color: #fff; }
.btn-primary:hover:not(:disabled) { background: linear-gradient(135deg, #b0763c, #cf9a55); }
.btn-outline { background: #fff; border-color: var(--color-border); color: var(--color-text-secondary); }
.btn-outline:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
.btn-ghost { background: transparent; color: var(--color-text-secondary); }
.btn-ghost:hover:not(:disabled) { background: #f3ece2; color: var(--color-primary); }
.icon-btn { width: 28px; height: 28px; display: inline-flex; align-items: center; justify-content: center; border: none; background: transparent; border-radius: 7px; cursor: pointer; color: var(--color-text-secondary); font-size: 14px; }
.icon-btn:hover { background: #f0e7da; color: var(--color-primary); }
</style>