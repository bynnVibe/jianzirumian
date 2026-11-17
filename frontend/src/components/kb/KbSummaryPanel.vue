<template>
  <div class="kb-summary">
    <section class="summary-section">
      <div class="section-bar">
        <h3>知识总结 <span class="count">book-to-skill 结构化摘要</span></h3>
        <div class="bar-actions">
          <button class="btn-tool" :disabled="loading" @click="loadAll">刷新</button>
          <button class="btn-dark" :disabled="generating || jobRunning" @click="generateAll">
            {{ generating ? '提交中…' : jobRunning ? `后台摘要中（${jobRunning}）…` : '摘要总结' }}
          </button>
        </div>
      </div>
      <p class="hint">开启内置技能「知识总结（book-to-skill）」后，文档入库将自动后台生成五段式结构化总结（核心概览 / 章节结构 / 术语表 / 关键模式 / 速查清单）。</p>
      <p v-if="message" class="msg" :class="{ error: messageError }">{{ message }}</p>

      <div v-if="!rows.length && !loading" class="empty-hint">知识库暂无文档或摘要记录</div>
      <div v-else class="doc-list">
        <div v-for="row in rows" :key="row.key" class="doc-item" :class="{ clickable: row.status === 'done' }" @click="row.status === 'done' && openDetail(row)">
          <div class="doc-main">
            <span class="doc-title">{{ row.title }}</span>
            <span v-if="row.status === 'error' && row.error" class="doc-error">{{ row.error }}</span>
            <span v-if="row.updated_at" class="doc-time">{{ formatTime(row.updated_at) }}</span>
          </div>
          <span class="status-badge" :class="row.status || 'none'">{{ statusLabel(row.status) }}</span>
          <button v-if="row.record_id && (row.status === 'done' || row.status === 'error')" class="link-btn" @click.stop="regenerate(row)">重新生成</button>
        </div>
      </div>
    </section>

    <!-- 技能未开启引导弹层 -->
    <div v-if="skillPrompt" class="modal-mask" @click.self="skillPrompt = null">
      <div class="modal-card">
        <h3>内置技能未开启</h3>
        <p class="prompt-text">
          生成知识总结需要开启内置技能「{{ skillPrompt.skill_name }}」。
          已为 {{ skillPrompt.waiting }} 篇文档登记等待任务，开启后将在后台继续执行。
        </p>
        <p v-if="skillError" class="form-error">{{ skillError }}</p>
        <div class="modal-actions">
          <button class="btn-ghost" @click="skillPrompt = null">取消</button>
          <button class="btn-ghost" @click="gotoSkills">前往技能配置</button>
          <button class="btn-dark" :disabled="enabling" @click="enableAndContinue">{{ enabling ? '开启中…' : '开启并继续' }}</button>
        </div>
      </div>
    </div>

    <!-- 结构化总结详情弹层 -->
    <div v-if="detail" class="modal-mask" @click.self="detail = null">
      <div class="modal-card wide">
        <div class="detail-head">
          <h3>{{ detail.title }}</h3>
          <button class="link-btn" @click="detail = null">关闭</button>
        </div>
        <div class="detail-body">
          <div v-if="structure.overview" class="detail-block">
            <h4>核心概览</h4>
            <p>{{ structure.overview }}</p>
          </div>
          <div v-if="structure.chapters?.length" class="detail-block">
            <h4>章节结构（{{ structure.chapters.length }}）</h4>
            <div v-for="(ch, i) in structure.chapters" :key="i" class="chapter">
              <div class="chapter-head" @click="toggleChapter(i)">
                <span class="chapter-toggle">{{ openChapters.has(i) ? '▾' : '▸' }}</span>
                <b>{{ ch.title }}</b>
              </div>
              <div v-if="openChapters.has(i)" class="chapter-body">
                <p v-if="ch.summary">{{ ch.summary }}</p>
                <ul v-if="ch.key_points?.length">
                  <li v-for="(kp, j) in ch.key_points" :key="j">{{ kp }}</li>
                </ul>
              </div>
            </div>
          </div>
          <div v-if="structure.glossary?.length" class="detail-block">
            <h4>术语表（{{ structure.glossary.length }}）</h4>
            <table class="glossary-table">
              <tbody>
                <tr v-for="(g, i) in structure.glossary" :key="i">
                  <td class="term">{{ g.term }}</td>
                  <td>{{ g.definition }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-if="structure.patterns?.length" class="detail-block">
            <h4>关键模式（{{ structure.patterns.length }}）</h4>
            <ul><li v-for="(p, i) in structure.patterns" :key="i">{{ textOf(p) }}</li></ul>
          </div>
          <div v-if="structure.cheatsheet?.length" class="detail-block">
            <h4>速查清单（{{ structure.cheatsheet.length }}）</h4>
            <ul><li v-for="(c, i) in structure.cheatsheet" :key="i">{{ textOf(c) }}</li></ul>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  listKbSummaries, getKbSummary, generateKbSummaries, getKbSummaryJobStatus, updateSkill,
} from '@/api'

const props = defineProps({
  kbId: { type: String, required: true },
})

const router = useRouter()
const summaries = ref([])
const documents = ref([])
const loading = ref(false)
const generating = ref(false)
const message = ref('')
const messageError = ref(false)
const jobRunning = ref(0)
const skillPrompt = ref(null)
const skillError = ref('')
const enabling = ref(false)
const detail = ref(null)
const openChapters = ref(new Set([0]))

// 文档与摘要合并视图：每文档一行，未生成摘要的显示「未生成」
const rows = computed(() => {
  const byRecord = new Map(summaries.value.map((s) => [s.record_id, s]))
  const list = documents.value.map((d) => {
    const s = byRecord.get(d.record_id)
    byRecord.delete(d.record_id)
    return {
      key: d.record_id,
      record_id: d.record_id,
      title: s?.title || d.title,
      status: s?.status || '',
      error: s?.error || '',
      updated_at: s?.updated_at || '',
      summary_id: s?.id || '',
    }
  })
  // 摘要存在但已不在当前文档清单（如文档被移除）
  for (const s of byRecord.values()) {
    list.push({ key: s.id, record_id: s.record_id, title: s.title, status: s.status, error: s.error, updated_at: s.updated_at, summary_id: s.id })
  }
  return list
})

const structure = computed(() => detail.value?.structure || {})

function formatTime(t) {
  return t ? String(t).replace('T', ' ').slice(0, 16) : ''
}

function statusLabel(s) {
  return {
    done: '已完成', running: '生成中', pending: '排队中',
    waiting_skill: '等待开启技能', error: '失败', '': '未生成',
  }[s] ?? s
}

function textOf(item) {
  if (typeof item === 'string') return item
  if (item && typeof item === 'object') return item.title || item.name || item.text || JSON.stringify(item)
  return String(item ?? '')
}

function toggleChapter(i) {
  const next = new Set(openChapters.value)
  if (next.has(i)) next.delete(i)
  else next.add(i)
  openChapters.value = next
}

async function loadAll() {
  loading.value = true
  try {
    const res = await listKbSummaries(props.kbId)
    summaries.value = res.data.summaries || []
    documents.value = res.data.documents || []
    await refreshJob()
  } catch (e) {
    message.value = e.response?.data?.detail || '加载知识总结失败'
    messageError.value = true
  } finally {
    loading.value = false
  }
}

async function refreshJob() {
  try {
    const res = await getKbSummaryJobStatus(props.kbId)
    jobRunning.value = res.data.job?.running || 0
  } catch (e) {
    console.error('查询摘要任务状态失败:', e)
  }
}

async function generateAll() {
  generating.value = true
  message.value = ''
  try {
    const res = await generateKbSummaries(props.kbId)
    message.value = `已调度 ${res.data.scheduled || 0} 篇文档后台生成总结`
    messageError.value = false
    await loadAll()
  } catch (e) {
    const detailData = e.response?.data?.detail
    if (e.response?.status === 409 && detailData?.skill_disabled) {
      skillError.value = ''
      skillPrompt.value = detailData
      await loadAll()
    } else {
      message.value = (typeof detailData === 'string' ? detailData : '') || '提交摘要失败'
      messageError.value = true
    }
  } finally {
    generating.value = false
  }
}

async function regenerate(row) {
  try {
    const res = await generateKbSummaries(props.kbId, { recordId: row.record_id, force: true })
    message.value = `已重新调度「${row.title}」`
    messageError.value = false
    await loadAll()
    return res
  } catch (e) {
    const detailData = e.response?.data?.detail
    if (e.response?.status === 409 && detailData?.skill_disabled) {
      skillError.value = ''
      skillPrompt.value = detailData
    } else {
      message.value = (typeof detailData === 'string' ? detailData : '') || '重新生成失败'
      messageError.value = true
    }
  }
}

function gotoSkills() {
  skillPrompt.value = null
  router.push('/skills')
}

async function enableAndContinue() {
  if (!skillPrompt.value?.skill_id) return
  enabling.value = true
  skillError.value = ''
  try {
    await updateSkill(skillPrompt.value.skill_id, { enabled: true })
    skillPrompt.value = null
    // 开启后后端 resume_pending 已恢复等待任务；此处再触发一次确保覆盖
    const res = await generateKbSummaries(props.kbId)
    message.value = `技能已开启，后台继续执行（新调度 ${res.data.scheduled || 0} 篇）`
    messageError.value = false
    await loadAll()
  } catch (e) {
    skillError.value = e.response?.data?.detail || '开启技能失败，请前往技能配置手动开启'
  } finally {
    enabling.value = false
  }
}

async function openDetail(row) {
  if (!row.summary_id) return
  try {
    const res = await getKbSummary(props.kbId, row.summary_id)
    detail.value = res.data.summary || null
    openChapters.value = new Set([0])
  } catch (e) {
    message.value = e.response?.data?.detail || '加载总结详情失败'
    messageError.value = true
  }
}

let pollTimer = null
onMounted(async () => {
  await loadAll()
  pollTimer = setInterval(async () => {
    if (jobRunning.value > 0) {
      await loadAll()
    }
  }, 4000)
})
onUnmounted(() => { if (pollTimer) clearInterval(pollTimer) })
</script>

<style scoped>
.kb-summary { padding: 20px 24px; height: 100%; overflow-y: auto; box-sizing: border-box; }

.summary-section { margin-bottom: 26px; }
.section-bar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; gap: 10px; flex-wrap: wrap; }
.section-bar h3 { margin: 0; font-size: 15px; font-weight: 600; color: var(--color-text); }
.count { font-size: 12px; color: var(--color-text-secondary); font-weight: 400; margin-left: 6px; }
.bar-actions { display: flex; gap: 8px; }
.hint { font-size: 12px; color: var(--color-text-secondary); margin: 0 0 12px; line-height: 1.6; }

.btn-tool { border: 1px solid var(--color-border); background: #fff; border-radius: 8px; padding: 6px 13px; font-size: 12.5px; color: var(--color-text); cursor: pointer; }
.btn-tool:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
.btn-tool:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-dark { background: var(--color-text); color: #fff; border: none; border-radius: 8px; padding: 6px 16px; font-size: 12.5px; cursor: pointer; }
.btn-dark:hover:not(:disabled) { opacity: 0.85; }
.btn-dark:disabled { opacity: 0.5; cursor: not-allowed; }

.msg { font-size: 12.5px; color: var(--color-text-secondary); margin: 0 0 10px; }
.msg.error { color: #c0392b; }
.empty-hint { padding: 18px; border: 1px dashed var(--color-border); border-radius: 10px; text-align: center; color: var(--color-text-secondary); font-size: 12.5px; }

.doc-list { display: flex; flex-direction: column; gap: 8px; }
.doc-item { display: flex; align-items: center; gap: 12px; background: #fff; border: 1px solid var(--color-border); border-radius: 8px; padding: 10px 14px; }
.doc-item.clickable { cursor: pointer; }
.doc-item.clickable:hover { border-color: var(--color-primary); }
.doc-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.doc-title { font-size: 13px; font-weight: 500; color: var(--color-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.doc-error { font-size: 11.5px; color: #c0392b; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.doc-time { font-size: 11px; color: var(--color-text-secondary); }
.link-btn { border: none; background: none; color: var(--color-primary); font-size: 12px; cursor: pointer; flex-shrink: 0; }
.link-btn:hover { text-decoration: underline; }

.status-badge { font-size: 11px; padding: 2px 8px; border-radius: 6px; background: #f3efe9; color: var(--color-text-secondary); flex-shrink: 0; }
.status-badge.done { background: #e8f5e9; color: #2e7d32; }
.status-badge.running, .status-badge.pending { background: #fff8e1; color: #b8860b; }
.status-badge.waiting_skill { background: #eef3fb; color: #3b6ea5; }
.status-badge.error { background: #fdecea; color: #c0392b; }

.modal-mask { position: fixed; inset: 0; background: rgba(45, 42, 36, 0.4); display: flex; align-items: center; justify-content: center; z-index: 100; }
.modal-card { background: #fff; border-radius: 14px; padding: 22px 24px; width: 460px; max-width: calc(100vw - 40px); box-shadow: 0 12px 40px rgba(0, 0, 0, 0.15); }
.modal-card.wide { width: 720px; max-height: 82vh; display: flex; flex-direction: column; }
.modal-card h3 { margin: 0 0 12px; font-size: 16px; }
.prompt-text { font-size: 13px; line-height: 1.7; color: var(--color-text); margin: 0; }
.form-error { color: #c0392b; font-size: 12.5px; margin: 10px 0 0; }
.modal-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 18px; }
.btn-ghost { background: transparent; border: 1px solid var(--color-border); border-radius: 8px; padding: 7px 16px; font-size: 13px; cursor: pointer; color: var(--color-text); }

.detail-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.detail-head h3 { margin: 0; }
.detail-body { overflow-y: auto; padding-right: 4px; }
.detail-block { margin-bottom: 18px; }
.detail-block h4 { margin: 0 0 8px; font-size: 13.5px; color: var(--color-primary); }
.detail-block p { margin: 0; font-size: 12.5px; line-height: 1.7; color: #5a4f42; white-space: pre-wrap; word-break: break-word; }
.detail-block ul { margin: 6px 0 0; padding-left: 20px; }
.detail-block li { font-size: 12.5px; line-height: 1.7; color: #5a4f42; }

.chapter { border: 1px solid var(--color-border); border-radius: 8px; margin-bottom: 6px; overflow: hidden; }
.chapter-head { display: flex; align-items: center; gap: 8px; padding: 8px 12px; cursor: pointer; background: var(--color-sidebar); font-size: 12.5px; }
.chapter-head:hover { background: #f3efe9; }
.chapter-toggle { color: var(--color-text-secondary); font-size: 11px; }
.chapter-body { padding: 8px 12px; }
.chapter-body p { margin: 0 0 6px; font-size: 12.5px; line-height: 1.7; color: #5a4f42; white-space: pre-wrap; }
.chapter-body ul { margin: 0; padding-left: 20px; }

.glossary-table { width: 100%; border-collapse: collapse; }
.glossary-table td { padding: 6px 10px; font-size: 12.5px; color: #5a4f42; border-bottom: 1px solid #f3efe9; line-height: 1.6; vertical-align: top; }
.glossary-table .term { font-weight: 600; color: var(--color-text); white-space: nowrap; width: 140px; }
</style>