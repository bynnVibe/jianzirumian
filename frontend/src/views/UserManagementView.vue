<template>
  <div class="usermgr-view">
    <div class="page-header">
      <h1 class="page-title">用户管理</h1>
      <p class="page-desc">管理系统中的所有用户账号与使用统计</p>
    </div>

    <div v-if="loading" class="loading-container">
      <div class="spinner"></div>
      <p>加载用户列表...</p>
    </div>

    <div v-else-if="users.length === 0" class="empty-state">
      <p>暂无用户数据</p>
    </div>

    <div v-else class="user-table-wrap">
      <table class="user-table">
        <thead>
          <tr>
            <th>用户名</th>
            <th>联系方式</th>
            <th>角色</th>
            <th>状态</th>
            <th>最近登录</th>
            <th>近30天查询</th>
            <th>Token 消耗</th>
            <th>好评/差评</th>
            <th>注册时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="u in users" :key="u.id">
            <tr
              :class="{ 'row-disabled': !u.is_active, 'row-expanded': expandedUserId === u.id }"
              class="user-row"
              @click="toggleDetail(u)"
            >
              <td class="cell-username">
                <span class="expand-icon">{{ expandedUserId === u.id ? '▾' : '▸' }}</span>
                <span class="username-text">{{ u.username }}</span>
                <span v-if="u.role === 'admin'" class="badge-admin">管理员</span>
              </td>
              <td>{{ u.contact }}</td>
              <td>
                <span class="role-badge" :class="u.role === 'admin' ? 'role-admin' : 'role-user'">
                  {{ u.role === 'admin' ? '管理员' : '普通用户' }}
                </span>
              </td>
              <td>
                <span class="status-badge" :class="u.is_active ? 'status-active' : 'status-disabled'">
                  {{ u.is_active ? '正常' : '已禁用' }}
                </span>
              </td>
              <td class="cell-date">{{ formatLastLogin(u.last_login_at) }}</td>
              <td class="cell-num">{{ u.query_count ?? '—' }}</td>
              <td class="cell-num">{{ formatTokens(u.tokens) }}</td>
              <td class="cell-num">
                <span class="fb-like">👍 {{ u.like_count ?? 0 }}</span>
                <span class="fb-dislike">👎 {{ u.dislike_count ?? 0 }}</span>
              </td>
              <td class="cell-date">{{ formatDate(u.created_at) }}</td>
              <td class="cell-actions" @click.stop>
                <template v-if="u.role !== 'admin'">
                  <button
                    class="btn-action btn-toggle"
                    @click="handleToggleActive(u)"
                    :title="u.is_active ? '禁用用户' : '启用用户'"
                  >
                    {{ u.is_active ? '禁用' : '启用' }}
                  </button>
                  <button
                    class="btn-action btn-delete"
                    @click="confirmDelete(u)"
                    title="删除用户"
                  >
                    删除
                  </button>
                </template>
                <span v-else class="text-muted">—</span>
              </td>
            </tr>

            <!-- 展开的统计详情面板 -->
            <tr v-if="expandedUserId === u.id" class="detail-row">
              <td :colspan="10">
                <div v-if="detailLoading" class="detail-loading">
                  <div class="spinner-sm"></div> 加载统计详情...
                </div>
                <div v-else-if="detail" class="detail-panel">
                  <div class="detail-summary">
                    <div class="summary-item">
                      <span class="summary-value">{{ detail.total_query_count }}</span>
                      <span class="summary-label">近30天查询次数</span>
                    </div>
                    <div class="summary-item">
                      <span class="summary-value">{{ formatTokens(detail.total_tokens) }}</span>
                      <span class="summary-label">Token 消耗（估算）</span>
                    </div>
                    <div class="summary-item">
                      <span class="summary-value">{{ detail.model_calls }}</span>
                      <span class="summary-label">模型调用次数</span>
                    </div>
                    <div class="summary-item">
                      <span class="summary-value fb-like">👍 {{ detail.like_count }}</span>
                      <span class="summary-label">好评回答</span>
                    </div>
                    <div class="summary-item">
                      <span class="summary-value fb-dislike">👎 {{ detail.dislike_count }}</span>
                      <span class="summary-label">差评回答</span>
                    </div>
                  </div>

                  <!-- 近 30 天查询趋势（CSS 柱状图） -->
                  <div class="detail-section">
                    <h4 class="detail-title">近 30 天查询趋势</h4>
                    <div v-if="chartBars.length === 0" class="detail-empty">近 30 天无查询记录</div>
                    <div v-else class="bar-chart">
                      <div
                        v-for="bar in chartBars"
                        :key="bar.day"
                        class="bar-col"
                        :title="`${bar.day}：${bar.count} 次查询 / ${formatTokens(bar.tokens)} Token`"
                      >
                        <div class="bar" :style="{ height: barHeight(bar.count) + 'px' }"></div>
                        <span class="bar-day">{{ bar.day.slice(5) }}</span>
                      </div>
                    </div>
                  </div>

                  <!-- 差评问题清单 -->
                  <div class="detail-section">
                    <h4 class="detail-title">差评问题清单（回答质量评估）</h4>
                    <div v-if="detail.disliked_questions.length === 0" class="detail-empty">暂无差评反馈</div>
                    <div v-else class="dislike-list">
                      <div
                        v-for="(dq, idx) in detail.disliked_questions"
                        :key="idx"
                        class="dislike-item"
                      >
                        <div class="dislike-question">问：{{ dq.question || '（无对应提问）' }}</div>
                        <div v-if="dq.answer_preview" class="dislike-answer">答：{{ dq.answer_preview }}...</div>
                        <div class="dislike-meta">
                          <span v-if="dq.comment" class="dislike-comment">备注：{{ dq.comment }}</span>
                          <span class="dislike-date">{{ formatIsoDate(dq.created_at) }}</span>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>

    <!-- 删除确认弹窗 -->
    <div v-if="showDeleteConfirm" class="modal-overlay" @click.self="showDeleteConfirm = false">
      <div class="modal-box">
        <h3 class="modal-title">确认删除</h3>
        <p class="modal-desc">
          确定要删除用户「{{ deleteTarget?.username }}」吗？此操作不可撤销，该用户的所有数据也将被移除。
        </p>
        <div class="modal-actions">
          <button class="btn btn-outline" @click="showDeleteConfirm = false">取消</button>
          <button class="btn btn-danger" @click="doDelete" :disabled="deleting">
            {{ deleting ? '删除中...' : '确认删除' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { getUsersStats, getUserStatsDetail, setUserActive, deleteUser } from '@/api'

const users = ref([])
const loading = ref(true)
const deleting = ref(false)
const showDeleteConfirm = ref(false)
const deleteTarget = ref(null)

// ---- 统计详情 ----
const expandedUserId = ref(null)
const detailLoading = ref(false)
const detail = ref(null)

async function loadUsers() {
  loading.value = true
  try {
    // 统计看板接口同时返回用户基础信息与用量汇总
    const res = await getUsersStats()
    users.value = res.data.stats || []
  } catch (err) {
    console.error('加载用户列表失败:', err)
    users.value = []
  } finally {
    loading.value = false
  }
}

async function toggleDetail(u) {
  if (expandedUserId.value === u.id) {
    expandedUserId.value = null
    detail.value = null
    return
  }
  expandedUserId.value = u.id
  detail.value = null
  detailLoading.value = true
  try {
    const res = await getUserStatsDetail(u.id)
    detail.value = res.data
  } catch (err) {
    console.error('加载用户统计详情失败:', err)
    detail.value = null
  } finally {
    detailLoading.value = false
  }
}

// 近 30 天柱状图：无数据的天补 0，保证横轴连续
const chartBars = computed(() => {
  if (!detail.value) return []
  const map = {}
  for (const d of detail.value.daily || []) {
    map[d.day] = d
  }
  const bars = []
  const today = new Date()
  for (let i = 29; i >= 0; i--) {
    const dt = new Date(today)
    dt.setDate(today.getDate() - i)
    const day = dt.toISOString().slice(0, 10)
    const d = map[day]
    bars.push({ day, count: d ? d.query_count : 0, tokens: d ? d.tokens : 0 })
  }
  return bars
})

const maxCount = computed(() => Math.max(1, ...chartBars.value.map((b) => b.count)))

function barHeight(count) {
  if (count <= 0) return 2
  return Math.max(4, Math.round((count / maxCount.value) * 80))
}

function formatTokens(n) {
  if (n === undefined || n === null) return '—'
  if (n >= 10000) return (n / 1000).toFixed(1) + 'k'
  return String(n)
}

async function handleToggleActive(u) {
  try {
    const newState = !u.is_active
    await setUserActive(u.id, newState)
    u.is_active = newState
  } catch (err) {
    alert('操作失败: ' + (err.response?.data?.detail || err.message))
  }
}

function confirmDelete(u) {
  deleteTarget.value = u
  showDeleteConfirm.value = true
}

async function doDelete() {
  if (!deleteTarget.value) return
  deleting.value = true
  try {
    await deleteUser(deleteTarget.value.id)
    users.value = users.value.filter((x) => x.id !== deleteTarget.value.id)
    showDeleteConfirm.value = false
    deleteTarget.value = null
  } catch (err) {
    alert('删除失败: ' + (err.response?.data?.detail || err.message))
  } finally {
    deleting.value = false
  }
}

function formatDate(ts) {
  if (!ts) return ''
  try {
    const d = new Date(ts * 1000)
    return d.toLocaleDateString('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return String(ts)
  }
}

function formatLastLogin(ts) {
  if (!ts) return '从未登录'
  return formatDate(ts)
}

function formatIsoDate(iso) {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    return d.toLocaleDateString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return iso
  }
}

onMounted(() => {
  loadUsers()
})
</script>

<style scoped>
.usermgr-view {
  height: 100vh;
  overflow-y: auto;
  padding: 32px 40px;
}

.page-header {
  margin-bottom: 24px;
}

.page-title {
  font-size: 22px;
  font-weight: 700;
  color: var(--color-text);
  margin-bottom: 4px;
}

.page-desc {
  font-size: 14px;
  color: var(--color-text-secondary);
}

.loading-container {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 60px 0;
  gap: 16px;
  color: var(--color-text-secondary);
}

.spinner {
  width: 32px;
  height: 32px;
  border: 3px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

.spinner-sm {
  width: 14px;
  height: 14px;
  border: 2px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  display: inline-block;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.empty-state {
  text-align: center;
  padding: 60px 0;
  color: var(--color-text-secondary);
  font-size: 15px;
}

.user-table-wrap {
  background: white;
  border: 1px solid var(--color-border);
  border-radius: 16px;
  overflow-x: auto;
}

.user-table {
  width: 100%;
  border-collapse: collapse;
}

.user-table th {
  padding: 14px 12px;
  text-align: left;
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-secondary);
  letter-spacing: 0.5px;
  background: #faf8f5;
  border-bottom: 1px solid var(--color-border);
  white-space: nowrap;
}

.user-table td {
  padding: 12px;
  font-size: 13px;
  color: var(--color-text);
  border-bottom: 1px solid #f0ebe5;
}

.user-table tr:last-child td {
  border-bottom: none;
}

.user-row {
  cursor: pointer;
  transition: background 0.15s;
}

.user-row:hover {
  background: #faf8f5;
}

.row-expanded {
  background: #faf6f0;
}

.row-disabled td {
  opacity: 0.5;
}

.expand-icon {
  display: inline-block;
  width: 14px;
  color: var(--color-text-secondary);
  font-size: 11px;
}

.cell-username {
  display: flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}

.username-text {
  font-weight: 600;
}

.badge-admin {
  font-size: 10px;
  background: #c98a4b;
  color: white;
  padding: 1px 7px;
  border-radius: 6px;
  font-weight: 600;
}

.role-badge {
  font-size: 12px;
  padding: 2px 10px;
  border-radius: 8px;
  font-weight: 500;
  white-space: nowrap;
}

.role-admin {
  background: #fff3e0;
  color: #c98a4b;
}

.role-user {
  background: #e8f5e9;
  color: #2e7d32;
}

.status-badge {
  font-size: 12px;
  padding: 2px 10px;
  border-radius: 8px;
  font-weight: 500;
  white-space: nowrap;
}

.status-active {
  background: #e8f5e9;
  color: #2e7d32;
}

.status-disabled {
  background: #fce4ec;
  color: #c62828;
}

.cell-date {
  font-size: 12px;
  color: var(--color-text-secondary);
  white-space: nowrap;
}

.cell-num {
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.fb-like {
  color: #2e7d32;
  margin-right: 6px;
}

.fb-dislike {
  color: #c62828;
}

.cell-actions {
  display: flex;
  gap: 8px;
}

.btn-action {
  padding: 4px 12px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  border: 1px solid transparent;
  transition: all 0.2s;
}

.btn-toggle {
  background: #fff8e1;
  color: #f57f17;
  border-color: #ffe082;
}

.btn-toggle:hover {
  background: #ffecb3;
  border-color: #f57f17;
}

.btn-delete {
  background: #fce4ec;
  color: #c62828;
  border-color: #ef9a9a;
}

.btn-delete:hover {
  background: #f8bbd0;
  border-color: #c62828;
}

.text-muted {
  font-size: 13px;
  color: var(--color-text-secondary);
}

/* ---- 统计详情面板 ---- */
.detail-row td {
  background: #fdfbf8;
  padding: 20px 24px;
}

.detail-loading {
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--color-text-secondary);
  font-size: 13px;
}

.detail-summary {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 20px;
}

.summary-item {
  flex: 1;
  min-width: 130px;
  background: white;
  border: 1px solid #ede6dc;
  border-radius: 10px;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.summary-value {
  font-size: 20px;
  font-weight: 700;
  color: var(--color-text);
}

.summary-label {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.detail-section {
  margin-bottom: 20px;
}

.detail-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: 10px;
}

.detail-empty {
  font-size: 13px;
  color: var(--color-text-secondary);
  padding: 12px 0;
}

/* CSS 柱状图 */
.bar-chart {
  display: flex;
  align-items: flex-end;
  gap: 4px;
  height: 110px;
  padding: 10px 12px 4px;
  background: white;
  border: 1px solid #ede6dc;
  border-radius: 10px;
  overflow-x: auto;
}

.bar-col {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-end;
  gap: 4px;
  min-width: 18px;
  height: 100%;
}

.bar {
  width: 12px;
  background: linear-gradient(180deg, #e9bc7d, #c98a4b);
  border-radius: 3px 3px 0 0;
  transition: opacity 0.2s;
}

.bar-col:hover .bar {
  opacity: 0.8;
}

.bar-day {
  font-size: 9px;
  color: var(--color-text-secondary);
  transform: rotate(-45deg);
  white-space: nowrap;
  margin-top: 6px;
}

/* 差评清单 */
.dislike-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 320px;
  overflow-y: auto;
}

.dislike-item {
  background: white;
  border: 1px solid #ede6dc;
  border-left: 3px solid #ef9a9a;
  border-radius: 8px;
  padding: 10px 14px;
}

.dislike-question {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: 4px;
}

.dislike-answer {
  font-size: 12px;
  color: var(--color-text-secondary);
  line-height: 1.5;
  margin-bottom: 6px;
}

.dislike-meta {
  display: flex;
  gap: 12px;
  font-size: 12px;
}

.dislike-comment {
  color: #b26a00;
}

.dislike-date {
  color: var(--color-text-secondary);
  margin-left: auto;
}

/* Modal */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}

.modal-box {
  background: white;
  border-radius: 16px;
  padding: 28px;
  max-width: 420px;
  width: 90%;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.15);
}

.modal-title {
  font-size: 17px;
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: 12px;
}

.modal-desc {
  font-size: 14px;
  color: var(--color-text-secondary);
  line-height: 1.6;
  margin-bottom: 20px;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

.btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 18px;
  border-radius: 10px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s;
  border: 1px solid transparent;
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-outline {
  background: white;
  color: var(--color-text-secondary);
  border-color: var(--color-border);
}

.btn-outline:hover:not(:disabled) {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.btn-danger {
  background: white;
  color: #c62828;
  border-color: #ef9a9a;
}

.btn-danger:hover:not(:disabled) {
  background: #fce4ec;
  border-color: #c62828;
}
</style>
