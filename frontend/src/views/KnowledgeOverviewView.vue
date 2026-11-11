<template>
  <div class="kb-overview">
    <div class="page-header">
      <div>
        <h1>知识库</h1>
        <p class="subtitle">管理公共与个人知识库，点击知识库名称进入详情</p>
      </div>
      <button v-if="!auth.isGuest" class="btn-primary" @click="openCreate">
        <span class="plus">＋</span> 新建知识库
      </button>
    </div>

    <div v-if="loading" class="loading-state">加载中…</div>
    <div v-else-if="error" class="error-state">
      {{ error }}
      <button class="btn-ghost" @click="load">重试</button>
    </div>

    <template v-else>
      <section class="kb-section">
        <div class="section-title">
          <svg class="section-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="17" height="17">
            <circle cx="12" cy="12" r="9" />
            <path d="M3 12h18M12 3c2.5 2.6 3.8 5.7 3.8 9S14.5 18.4 12 21c-2.5-2.6-3.8-5.7-3.8-9S9.5 5.6 12 3z" />
          </svg>
          <h2>公共知识库</h2>
          <span class="count-badge">{{ publicKbs.length }}</span>
        </div>
        <div v-if="publicKbs.length" class="kb-grid">
          <div
            v-for="kb in publicKbs"
            :key="kb.id"
            class="kb-card"
            @click="enterKb(kb)"
          >
            <div class="kb-card-top">
              <span class="kb-name">{{ kb.name }}</span>
              <span v-if="canManage(kb)" class="kb-actions" @click.stop>
                <button class="icon-btn" title="编辑" @click="openEdit(kb)">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                    <path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" />
                  </svg>
                </button>
                <button class="icon-btn danger" title="删除" @click="removeKb(kb)">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                    <path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
                  </svg>
                </button>
              </span>
            </div>
            <span class="topic-tag">{{ kb.topic }}</span>
            <div class="kb-stats">
              <span>{{ kb.doc_count }} 文档</span>
              <span class="dot">·</span>
              <span>{{ kb.entry_count }} 分片</span>
              <span class="dot">·</span>
              <span>{{ formatTokens(kb.token_estimate) }} Token</span>
            </div>
          </div>
        </div>
        <div v-else class="empty-hint">暂无公共知识库</div>
      </section>

      <section v-if="!auth.isGuest" class="kb-section">
        <div class="section-title">
          <svg class="section-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="17" height="17">
            <rect x="5" y="11" width="14" height="9" rx="2" />
            <path d="M8 11V7a4 4 0 0 1 8 0v4" />
          </svg>
          <h2>个人知识库</h2>
          <span class="count-badge">{{ privateKbs.length }}</span>
        </div>
        <div v-if="privateKbs.length" class="kb-grid">
          <div
            v-for="kb in privateKbs"
            :key="kb.id"
            class="kb-card"
            @click="enterKb(kb)"
          >
            <div class="kb-card-top">
              <span class="kb-name">{{ kb.name }}</span>
              <span class="kb-actions" @click.stop>
                <button class="icon-btn" title="编辑" @click="openEdit(kb)">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                    <path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" />
                  </svg>
                </button>
                <button class="icon-btn danger" title="删除" @click="removeKb(kb)">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                    <path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
                  </svg>
                </button>
              </span>
            </div>
            <span class="topic-tag">{{ kb.topic }}</span>
            <div class="kb-stats">
              <span>{{ kb.doc_count }} 文档</span>
              <span class="dot">·</span>
              <span>{{ kb.entry_count }} 分片</span>
              <span class="dot">·</span>
              <span>{{ formatTokens(kb.token_estimate) }} Token</span>
            </div>
          </div>
        </div>
        <div v-else class="empty-hint">还没有个人知识库，点击右上角「新建知识库」创建</div>
      </section>
    </template>

    <!-- 新建/编辑知识库弹窗 -->
    <div v-if="formVisible" class="modal-mask" @click.self="formVisible = false">
      <div class="modal-card">
        <h3>{{ editingKb ? '编辑知识库' : '新建知识库' }}</h3>
        <label class="field-label">名称</label>
        <input v-model="form.name" class="field-input" placeholder="例如：糖尿病饮食管理" maxlength="40" />
        <label class="field-label">主题</label>
        <select v-model="form.topic" class="field-input">
          <option v-for="t in topics" :key="t" :value="t">{{ t }}</option>
        </select>
        <template v-if="!editingKb">
          <label class="field-label">可见性</label>
          <select v-model="form.visibility" class="field-input">
            <option v-if="auth.isAdmin" value="public">公共知识库（所有人可见）</option>
            <option value="private">个人知识库（仅自己可见）</option>
          </select>
        </template>
        <p v-if="formError" class="form-error">{{ formError }}</p>
        <div class="modal-actions">
          <button class="btn-ghost" @click="formVisible = false">取消</button>
          <button class="btn-primary" :disabled="saving" @click="submitForm">
            {{ saving ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import {
  listKnowledgeBases,
  createKnowledgeBase,
  updateKnowledgeBase,
  deleteKnowledgeBase,
} from '@/api'

const router = useRouter()
const auth = useAuthStore()

const loading = ref(true)
const error = ref('')
const bases = ref([])
const topics = ref([])

const publicKbs = computed(() => bases.value.filter((b) => b.visibility === 'public'))
const privateKbs = computed(() => bases.value.filter((b) => b.visibility === 'private'))

const formVisible = ref(false)
const editingKb = ref(null)
const saving = ref(false)
const formError = ref('')
const form = ref({ name: '', topic: '其他', visibility: 'private' })

function canManage(kb) {
  if (auth.isGuest) return false
  if (kb.visibility === 'public') return auth.isAdmin
  return kb.owner_id === (auth.user?.id || auth.user?.username) || auth.isAdmin
}

function formatTokens(n) {
  if (!n) return '0'
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`
  return String(n)
}

function enterKb(kb) {
  router.push(`/kb/${kb.id}`)
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await listKnowledgeBases()
    bases.value = res.data.bases || []
    topics.value = res.data.topics || []
  } catch (e) {
    error.value = e.response?.data?.detail || '加载知识库列表失败'
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editingKb.value = null
  form.value = { name: '', topic: topics.value[0] || '其他', visibility: 'private' }
  formError.value = ''
  formVisible.value = true
}

function openEdit(kb) {
  editingKb.value = kb
  form.value = { name: kb.name, topic: kb.topic, visibility: kb.visibility }
  formError.value = ''
  formVisible.value = true
}

async function submitForm() {
  if (!form.value.name.trim()) {
    formError.value = '请输入知识库名称'
    return
  }
  saving.value = true
  formError.value = ''
  try {
    if (editingKb.value) {
      await updateKnowledgeBase(editingKb.value.id, {
        name: form.value.name.trim(),
        topic: form.value.topic,
      })
    } else {
      await createKnowledgeBase({
        name: form.value.name.trim(),
        topic: form.value.topic,
        visibility: form.value.visibility,
      })
    }
    formVisible.value = false
    await load()
  } catch (e) {
    formError.value = e.response?.data?.detail || '保存失败'
  } finally {
    saving.value = false
  }
}

async function removeKb(kb) {
  if (!window.confirm(`确定删除知识库「${kb.name}」？其中的文档与条目将一并删除，且无法恢复。`)) return
  try {
    await deleteKnowledgeBase(kb.id)
    await load()
  } catch (e) {
    window.alert(e.response?.data?.detail || '删除失败')
  }
}

onMounted(load)
</script>

<style scoped>
.kb-overview {
  height: 100%;
  overflow-y: auto;
  padding: 28px 32px;
  background: var(--color-bg);
}

.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 28px;
}

.page-header h1 {
  font-size: 22px;
  font-weight: 700;
  color: var(--color-text);
  margin: 0 0 6px;
}

.subtitle {
  margin: 0;
  font-size: 13px;
  color: var(--color-text-secondary);
}

.btn-primary {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: var(--color-text);
  color: #fff;
  border: none;
  border-radius: 8px;
  padding: 9px 18px;
  font-size: 14px;
  cursor: pointer;
  transition: opacity 0.15s;
  white-space: nowrap;
  flex-shrink: 0;
}

.btn-primary:hover { opacity: 0.85; }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
.plus { font-size: 15px; }

.btn-ghost {
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 8px 16px;
  font-size: 14px;
  color: var(--color-text);
  cursor: pointer;
}

.btn-ghost:hover { background: var(--color-sidebar); }

.loading-state,
.error-state {
  padding: 40px 0;
  text-align: center;
  color: var(--color-text-secondary);
  font-size: 14px;
}

.error-state { color: #c0392b; }
.error-state .btn-ghost { margin-left: 12px; }

.kb-section { margin-bottom: 32px; }

.section-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 14px;
}

.section-title h2 {
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
  margin: 0;
}

.section-icon { color: var(--color-primary); flex-shrink: 0; }

.count-badge {
  background: var(--color-primary-light);
  color: var(--color-primary);
  border-radius: 10px;
  padding: 1px 9px;
  font-size: 12px;
  font-weight: 600;
}

.kb-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 14px;
}

.kb-card {
  background: #fff;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 16px 18px;
  cursor: pointer;
  transition: border-color 0.15s, box-shadow 0.15s;
}

.kb-card:hover {
  border-color: var(--color-primary);
  box-shadow: 0 2px 10px rgba(201, 138, 75, 0.12);
}

.kb-card-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}

.kb-name {
  font-size: 15px;
  font-weight: 600;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kb-actions { display: inline-flex; gap: 4px; flex-shrink: 0; }

.icon-btn {
  background: transparent;
  border: none;
  cursor: pointer;
  font-size: 13px;
  padding: 2px 5px;
  border-radius: 6px;
  color: var(--color-text-secondary);
}

.icon-btn:hover { background: var(--color-primary-light); color: var(--color-primary); }
.icon-btn.danger:hover { background: #fdecea; color: #c0392b; }

.topic-tag {
  display: inline-block;
  background: var(--color-primary-light);
  color: var(--color-primary);
  border-radius: 6px;
  padding: 2px 8px;
  font-size: 12px;
  margin-bottom: 10px;
}

.kb-stats {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.kb-stats .dot { opacity: 0.5; }

.empty-hint {
  padding: 22px;
  border: 1px dashed var(--color-border);
  border-radius: 12px;
  text-align: center;
  color: var(--color-text-secondary);
  font-size: 13px;
}

.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(45, 42, 36, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}

.modal-card {
  background: #fff;
  border-radius: 14px;
  padding: 24px 26px;
  width: 400px;
  max-width: calc(100vw - 40px);
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.15);
}

.modal-card h3 {
  margin: 0 0 18px;
  font-size: 17px;
  color: var(--color-text);
}

.field-label {
  display: block;
  font-size: 13px;
  color: var(--color-text-secondary);
  margin: 12px 0 6px;
}

.field-input {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 9px 12px;
  font-size: 14px;
  color: var(--color-text);
  background: #fff;
  outline: none;
}

.field-input:focus { border-color: var(--color-primary); }

.form-error {
  color: #c0392b;
  font-size: 13px;
  margin: 10px 0 0;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 20px;
}
</style>