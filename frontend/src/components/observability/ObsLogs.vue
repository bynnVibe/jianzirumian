<template>
  <div class="obs-logs">
    <div class="logs-toolbar">
      <select v-model="date" class="filter-select" :disabled="loading" @change="reload">
        <option v-for="d in dates" :key="d" :value="d">{{ d }}</option>
        <option v-if="!dates.length" value="">暂无日志文件</option>
      </select>
      <select v-model="user" class="filter-select" :disabled="loading" @change="reload">
        <option value="">全部用户</option>
        <option v-for="u in users" :key="u" :value="u">{{ u }}</option>
      </select>
      <select v-model="level" class="filter-select" :disabled="loading" @change="reload">
        <option value="">全部级别</option>
        <option v-for="l in LEVELS" :key="l" :value="l">{{ l }}</option>
      </select>
      <input
        v-model="keyword"
        class="filter-input"
        type="search"
        placeholder="关键词（消息 / 模块名）"
        @keyup.enter="reload"
      />
      <button class="btn btn-ghost sm" :disabled="loading" @click="reload">
        {{ loading ? '加载中…' : '查询' }}
      </button>
    </div>
    <p v-if="errorMsg" class="inline-error" role="alert">{{ errorMsg }}</p>
    <p v-else-if="!loading && !items.length" class="empty-hint">
      该条件下暂无日志记录（切换日期 / 用户 / 级别或清空关键词后重试）。
    </p>
    <table v-else class="obs-table log-table">
      <thead>
        <tr><th>时间</th><th>级别</th><th>用户</th><th>模块</th><th>日志内容</th><th>Trace</th></tr>
      </thead>
      <tbody>
        <tr v-for="(it, i) in items" :key="date + '-' + offset + '-' + i" class="log-row" @click="openDetail(it)">
          <td class="cell-time">
            {{ it.ts }}
            <span v-if="it.count > 1" class="merge-badge" :title="`同一分钟内合并了 ${it.count} 条日志`">×{{ it.count }}</span>
          </td>
          <td><span class="level-badge" :class="it.level.toLowerCase()">{{ it.level }}</span></td>
          <td>{{ it.user }}</td>
          <td class="cell-input">{{ it.logger }}</td>
          <td class="cell-msg"><pre class="msg-text">{{ it.message }}<template v-if="it.truncated"> …[单条过长已截断]</template><template v-if="it.count > 1"> …[同分钟 {{ it.count }} 条，点击展开]</template></pre></td>
          <td>
            <button v-if="it.trace && it.trace !== '-'" class="trace-link" :title="it.trace" @click.stop="$emit('open-trace', it.trace)">
              查看链路
            </button>
            <span v-else>-</span>
          </td>
        </tr>
      </tbody>
    </table>
    <div class="pager" v-if="total > LIMIT || offset > 0">
      <button class="btn btn-ghost sm" :disabled="offset === 0 || loading" @click="page(-1)">上一页</button>
      <span class="pager-info">{{ offset + 1 }}-{{ offset + items.length }} / {{ total }}</span>
      <button class="btn btn-ghost sm" :disabled="offset + items.length >= total || loading" @click="page(1)">下一页</button>
    </div>

    <!-- 日志详情弹窗 -->
    <Teleport to="body">
      <div v-if="detail" class="log-modal-mask" @click.self="detail = null">
        <div class="log-modal">
          <div class="log-modal-head">
            <h3>日志详情</h3>
            <button class="log-modal-close" title="关闭" @click="detail = null">&times;</button>
          </div>
          <div class="log-modal-body">
            <div class="log-meta">
              <div class="meta-row"><span class="meta-key">时间</span><span>{{ detail.ts }}</span></div>
              <div class="meta-row"><span class="meta-key">级别</span><span class="level-badge" :class="detail.level.toLowerCase()">{{ detail.level }}</span></div>
              <div class="meta-row"><span class="meta-key">用户</span><span>{{ detail.user || '-' }}</span></div>
              <div class="meta-row"><span class="meta-key">模块</span><span>{{ detail.logger || '-' }}</span></div>
              <div class="meta-row">
                <span class="meta-key">Trace</span>
                <button v-if="detail.trace && detail.trace !== '-'" class="trace-link" @click="$emit('open-trace', detail.trace); detail = null">{{ detail.trace }}</button>
                <span v-else>-</span>
              </div>
              <div v-if="detail.count > 1" class="meta-row">
                <span class="meta-key">合并</span>
                <span>同一分钟内共 {{ detail.count }} 条日志<template v-if="detail.entries && detail.entries.length < detail.count">（展示前 {{ detail.entries.length }} 条）</template></span>
              </div>
            </div>
            <!-- 同分钟合并：逐条展开原始日志 -->
            <template v-if="detail.entries && detail.entries.length > 1">
              <div class="log-msg-label">合并明细（共 {{ detail.count }} 条）</div>
              <div class="merge-entry-list">
                <div v-for="(e, ei) in detail.entries" :key="ei" class="merge-entry">
                  <div class="merge-entry-head">
                    <span class="level-badge" :class="e.level.toLowerCase()">{{ e.level }}</span>
                    <span class="merge-entry-ts">{{ e.ts }}</span>
                    <span class="merge-entry-logger">{{ e.logger || '-' }}</span>
                    <button v-if="e.trace && e.trace !== '-'" class="trace-link" @click="$emit('open-trace', e.trace); detail = null">链路</button>
                  </div>
                  <pre class="log-msg-full">{{ e.message }}<template v-if="e.truncated"> …[单条过长已截断]</template></pre>
                </div>
              </div>
            </template>
            <!-- 单条日志 -->
            <template v-else>
              <div class="log-msg-label">日志内容</div>
              <pre class="log-msg-full">{{ detail.message }}<template v-if="detail.truncated"> …[单条过长已截断]</template></pre>
            </template>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { getObsLogs, getObsLogsMeta } from '@/api'

defineEmits(['open-trace'])

const LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
// 需求1：单页 50 行（同分钟后端已合并，避免条数过多）
const LIMIT = 50

const dates = ref([])
const users = ref([])
const date = ref('')
const user = ref('')
const level = ref('')
const keyword = ref('')
const items = ref([])
const total = ref(0)
const offset = ref(0)
const loading = ref(false)
const errorMsg = ref('')
const detail = ref(null)

function openDetail(it) { detail.value = it }

onMounted(async () => {
  try {
    const res = await getObsLogsMeta()
    dates.value = res.data.dates || []
    users.value = res.data.users || []
    date.value = dates.value[0] || ''
    await reload()
  } catch (err) {
    errorMsg.value = err.response?.data?.detail || err.message || '日志元数据加载失败'
  }
})

async function reload() {
  if (!date.value) { items.value = []; total.value = 0; return }
  loading.value = true
  errorMsg.value = ''
  try {
    const res = await getObsLogs({
      date: date.value,
      user: user.value,
      level: level.value,
      q: keyword.value,
      limit: LIMIT,
      offset: offset.value,
    })
    items.value = res.data.items || []
    total.value = res.data.total || 0
  } catch (err) {
    errorMsg.value = err.response?.data?.detail || err.message || '日志查询失败'
    items.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

function page(dir) {
  offset.value = Math.max(0, offset.value + dir * LIMIT)
  reload()
}
</script>

<style scoped>
.logs-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 10px;
}
.filter-select,
.filter-input {
  border: 1px solid var(--color-border);
  background: #fff;
  border-radius: 8px;
  padding: 6px 10px;
  font-size: 13px;
  color: var(--color-text);
}
.filter-input { min-width: 200px; }
.btn {
  border: 1px solid var(--color-border);
  background: #fff;
  border-radius: 8px;
  cursor: pointer;
  font-size: 13px;
  padding: 7px 14px;
  color: var(--color-text);
}
.btn-ghost:hover { background: rgba(0, 0, 0, 0.04); }
.sm { padding: 5px 10px; font-size: 12px; }
.obs-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  background: #fff;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  overflow: hidden;
}
.obs-table th,
.obs-table td {
  padding: 7px 9px;
  border-bottom: 1px solid var(--color-border);
  text-align: left;
  vertical-align: top;
}
.obs-table th {
  background: rgba(0, 0, 0, 0.03);
  font-weight: 600;
  white-space: nowrap;
}
.cell-time { white-space: nowrap; color: var(--color-text-secondary); }
.cell-input { max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cell-msg { max-width: 560px; }
.msg-text {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
  font-family: inherit;
  font-size: 12px;
  max-height: 96px;
  overflow-y: auto;
}
.level-badge {
  display: inline-block;
  padding: 1px 7px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  background: rgba(0, 0, 0, 0.06);
}
.level-badge.error, .level-badge.critical { background: #fdecea; color: #c62828; }
.level-badge.warning { background: #fff4e0; color: #a06a00; }
.level-badge.info { background: #e8f1e8; color: #3c6e3c; }
.level-badge.debug { background: rgba(0, 0, 0, 0.05); color: var(--color-text-secondary); }
.trace-link {
  border: none;
  background: none;
  color: var(--color-primary, #8a5a2b);
  cursor: pointer;
  font-size: 12px;
  padding: 0;
  text-decoration: underline;
}
.pager {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
}
.pager-info { font-size: 12px; color: var(--color-text-secondary); }
.empty-hint, .inline-error { font-size: 12px; }
.inline-error { color: #c62828; }

.log-row { cursor: pointer; }
.log-row:hover { background: rgba(138, 90, 43, 0.05); }

.log-modal-mask {
  position: fixed; inset: 0; z-index: 300;
  background: rgba(45, 42, 36, 0.45);
  display: flex; align-items: center; justify-content: center; padding: 24px;
}
.log-modal {
  width: min(760px, 94vw); max-height: 84vh;
  background: var(--color-bg); border-radius: 12px; overflow: hidden;
  display: flex; flex-direction: column;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.25);
}
.log-modal-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 18px; border-bottom: 1px solid var(--color-border); flex-shrink: 0;
}
.log-modal-head h3 { margin: 0; font-size: 15px; color: var(--color-text); }
.log-modal-close {
  border: none; background: none; font-size: 22px; line-height: 1;
  cursor: pointer; color: var(--color-text-secondary);
}
.log-modal-body { padding: 16px 18px; overflow-y: auto; }
.log-meta { display: flex; flex-direction: column; gap: 8px; margin-bottom: 14px; }
.meta-row { display: flex; align-items: center; gap: 10px; font-size: 13px; color: var(--color-text); }
.meta-key {
  flex-shrink: 0; width: 56px; font-size: 12px;
  color: var(--color-text-secondary);
}
.log-msg-label { font-size: 12px; color: var(--color-text-secondary); margin-bottom: 6px; }
.log-msg-full {
  margin: 0; padding: 12px 14px;
  background: #fff; border: 1px solid var(--color-border); border-radius: 8px;
  white-space: pre-wrap; word-break: break-all;
  font-family: inherit; font-size: 13px; line-height: 1.6;
  color: var(--color-text);
}
.merge-badge {
  display: inline-block; margin-left: 6px; padding: 0 6px;
  border-radius: 999px; background: rgba(138, 90, 43, 0.12);
  color: var(--color-primary, #8a5a2b); font-size: 11px; font-weight: 600;
}
.merge-entry-list { display: flex; flex-direction: column; gap: 10px; }
.merge-entry { border: 1px solid var(--color-border); border-radius: 8px; padding: 8px 10px; background: rgba(0, 0, 0, 0.015); }
.merge-entry-head { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; flex-wrap: wrap; }
.merge-entry-ts { font-size: 12px; color: var(--color-text-secondary); white-space: nowrap; }
.merge-entry-logger { font-size: 12px; color: var(--color-text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.merge-entry .log-msg-full { font-size: 12px; padding: 8px 10px; }
</style>
