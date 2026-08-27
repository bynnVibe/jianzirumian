<template>
  <div class="config-view">
    <div class="page-header">
      <h1 class="page-title">系统配置</h1>
      <p class="page-desc">查看和切换各模块的提供商，测试连接状态</p>
    </div>

    <div v-if="loading" class="loading-container">
      <div class="spinner"></div>
      <p>加载配置中...</p>
    </div>

    <div v-else class="config-sections">

      <!-- LLM 配置 -->
      <section class="config-section">
        <div class="section-header">
          <h2 class="section-title">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="20" height="20">
              <circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/>
            </svg>
            LLM 语言模型
          </h2>
          <span class="section-badge" :class="llmTestStatus">{{ llmTestStatus === 'ok' ? '正常' : llmTestStatus === 'fail' ? '异常' : '' }}</span>
        </div>
        <p class="section-desc">选择用于回答用户问题的 AI 模型</p>

        <div class="provider-options">
          <label
            v-for="opt in config.available_llm_providers"
            :key="opt"
            class="provider-option"
            :class="{ selected: activeConfig.llm === opt }"
          >
            <input type="radio" name="llm" :value="opt" v-model="activeConfig.llm" @change="handleProviderChange('llm', opt)" />
            <div class="option-content">
              <span class="option-name">{{ llmLabels[opt] || opt }}</span>
              <span class="option-desc">{{ llmDescs[opt] }}</span>
            </div>
            <div class="option-config">
              <template v-if="opt === 'ollama'">
                地址: {{ config.llm_config?.ollama?.base_url }}
              </template>
              <template v-else>
                模型: {{ config.llm_config?.[opt]?.model }}
                <span v-if="!config.llm_config?.[opt]?.has_key" class="no-key">未配置 API Key</span>
              </template>
            </div>
          </label>
        </div>

        <div class="section-actions">
          <button class="btn btn-outline" @click="testConnection('llm', activeConfig.llm)" :disabled="llmTesting">
            <span v-if="llmTesting" class="spinner-xs"></span>
            {{ llmTesting ? '测试中...' : '测试连接' }}
          </button>
          <span v-if="llmTestResult" class="test-result" :class="llmTestResult.success ? 'success' : 'error'">
            {{ llmTestResult.message }}
            <span v-if="llmTestResult.elapsed" class="test-elapsed">{{ llmTestResult.elapsed }}s</span>
          </span>
        </div>
      </section>

      <!-- OCR 配置 -->
      <section class="config-section">
        <div class="section-header">
          <h2 class="section-title">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="20" height="20">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>
            </svg>
            OCR 文字识别
          </h2>
        </div>
        <p class="section-desc">选择图片文字识别引擎</p>

        <div class="provider-options">
          <label
            v-for="opt in config.available_ocr_providers"
            :key="opt"
            class="provider-option"
            :class="{ selected: activeConfig.ocr === opt }"
          >
            <input type="radio" name="ocr" :value="opt" v-model="activeConfig.ocr" @change="handleProviderChange('ocr', opt)" />
            <div class="option-content">
              <span class="option-name">{{ ocrLabels[opt] || opt }}</span>
              <span class="option-desc">{{ ocrDescs[opt] }}</span>
            </div>
          </label>
        </div>
      </section>

      <!-- 搜索配置 -->
      <section class="config-section">
        <div class="section-header">
          <h2 class="section-title">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="20" height="20">
              <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
            </svg>
            联网搜索
          </h2>
          <span class="section-badge" :class="searchTestStatus">{{ searchTestStatus === 'ok' ? '正常' : searchTestStatus === 'fail' ? '异常' : '' }}</span>
        </div>
        <p class="section-desc">选择联网搜索引擎。DuckDuckGo 免费但可能不稳定；Bing/SerpAPI 需要 API Key</p>

        <div class="provider-options">
          <label
            v-for="opt in config.available_search_providers"
            :key="opt"
            class="provider-option"
            :class="{ selected: activeConfig.search === opt }"
          >
            <input type="radio" name="search" :value="opt" v-model="activeConfig.search" @change="handleProviderChange('search', opt)" />
            <div class="option-content">
              <span class="option-name">{{ searchLabels[opt] || opt }}</span>
              <span class="option-desc">{{ searchDescs[opt] }}</span>
            </div>
            <div v-if="opt !== 'duckduckgo'" class="option-config">
              <span v-if="!config.search_config?.[opt]?.has_key" class="no-key">未配置 API Key</span>
              <span v-else class="key-ok">已配置</span>
            </div>
          </label>
        </div>

        <div class="section-actions">
          <button class="btn btn-outline" @click="testConnection('search', activeConfig.search)" :disabled="searchTesting">
            <span v-if="searchTesting" class="spinner-xs"></span>
            {{ searchTesting ? '测试中...' : '测试连接' }}
          </button>
          <span v-if="searchTestResult" class="test-result" :class="searchTestResult.success ? 'success' : 'error'">
            {{ searchTestResult.message }}
            <span v-if="searchTestResult.elapsed" class="test-elapsed">{{ searchTestResult.elapsed }}s</span>
          </span>
        </div>
      </section>

      <!-- Embedding 配置 -->
      <section class="config-section">
        <div class="section-header">
          <h2 class="section-title">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="20" height="20">
              <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
            </svg>
            Embedding 向量模型
          </h2>
          <span class="section-badge" :class="config.embedding?.available ? 'ok' : 'fail'">{{ config.embedding?.available ? '正常' : '异常' }}</span>
        </div>
        <p class="section-desc">向量嵌入模型，将文本转换为向量用于知识检索</p>

        <div class="embedding-info">
          <div class="info-row">
            <span class="info-label">Provider</span>
            <span class="info-value">{{ config.embedding?.provider }}</span>
          </div>
          <div class="info-row">
            <span class="info-label">模型</span>
            <span class="info-value">{{ config.embedding?.model }}</span>
          </div>
          <div class="info-row">
            <span class="info-label">知识库条目</span>
            <span class="info-value">{{ config.embedding?.knowledge_count }}</span>
          </div>
        </div>

        <div class="section-actions">
          <button class="btn btn-outline" @click="testEmbedding" :disabled="embeddingTesting">
            <span v-if="embeddingTesting" class="spinner-xs"></span>
            {{ embeddingTesting ? '测试中...' : '测试连接' }}
          </button>
          <span v-if="embeddingTestResult" class="test-result" :class="embeddingTestResult.success ? 'success' : 'error'">
            {{ embeddingTestResult.message }}
            <span v-if="embeddingTestResult.elapsed" class="test-elapsed">{{ embeddingTestResult.elapsed }}s</span>
          </span>
        </div>
      </section>

      <!-- Redis 文件缓存 -->
      <section class="config-section">
        <div class="section-header">
          <h2 class="section-title">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="20" height="20">
              <ellipse cx="12" cy="5" rx="9" ry="3"/>
              <path d="M3 5v14a9 3 0 0 0 18 0V5"/>
              <path d="M3 12a9 3 0 0 0 18 0"/>
            </svg>
            Redis 文件缓存
          </h2>
          <span class="section-badge" :class="redisBadgeClass">{{ redisBadgeText }}</span>
        </div>
        <p class="section-desc">
          缓存知识库原始文件（图片 / Word / PDF），加快检索来源跳转与原文预览。
          公共知识库文件所有用户共享缓存，个人知识库文件按属主隔离。
        </p>

        <div class="redis-form">
          <div class="redis-row">
            <span class="redis-label">文件缓存</span>
            <label class="toggle-switch">
              <input type="checkbox" v-model="redisForm.enabled" />
              <span class="toggle-slider"></span>
            </label>
            <span class="redis-hint">{{ redisForm.enabled ? '已开启，跳转与预览优先读缓存' : '已关闭，所有请求直读磁盘' }}</span>
          </div>
          <div class="redis-row">
            <span class="redis-label">Redis 地址</span>
            <input
              class="redis-input"
              v-model="redisForm.url"
              placeholder="留空使用 .env 默认值（redis://host:6379/0）"
              spellcheck="false"
            />
          </div>
          <div class="redis-row">
            <span class="redis-label">缓存策略</span>
            <span class="redis-hint">
              有效期 {{ config.redis_config?.ttl || 86400 }} 秒 · 单文件上限 {{ config.redis_config?.max_mb || 20 }} MB（可在 .env 中调整）
            </span>
          </div>
        </div>

        <div class="section-actions">
          <button class="btn btn-primary" @click="saveRedis" :disabled="redisSaving">
            {{ redisSaving ? '保存中...' : '保存配置' }}
          </button>
          <button class="btn btn-outline" @click="testRedis" :disabled="redisTesting">
            <span v-if="redisTesting" class="spinner-xs"></span>
            {{ redisTesting ? '测试中...' : '测试连接' }}
          </button>
          <span v-if="redisTestResult" class="test-result" :class="redisTestResult.success ? 'success' : 'error'">
            {{ redisTestResult.message }}
            <span v-if="redisTestResult.elapsed" class="test-elapsed">{{ redisTestResult.elapsed }}s</span>
          </span>
        </div>
      </section>

    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import {
  getSystemConfig,
  switchProvider,
  testLLMConnection,
  testSearchConnection,
  testEmbeddingConnection,
  saveRedisConfig,
  testRedisConnection,
} from '@/api'

const loading = ref(true)
const config = ref({
  available_llm_providers: [],
  available_ocr_providers: [],
  available_search_providers: [],
  llm_config: {},
  search_config: {},
  embedding: {},
})

const activeConfig = reactive({
  llm: '',
  ocr: '',
  search: '',
})

// LLM test state
const llmTesting = ref(false)
const llmTestResult = ref(null)
const llmTestStatus = ref('')

// Search test state
const searchTesting = ref(false)
const searchTestResult = ref(null)
const searchTestStatus = ref('')

// Embedding test state
const embeddingTesting = ref(false)
const embeddingTestResult = ref(null)

// Redis 文件缓存配置状态
const redisForm = reactive({ url: '', enabled: true })
const redisSaving = ref(false)
const redisTesting = ref(false)
const redisTestResult = ref(null)

const redisBadgeClass = computed(() => {
  if (!redisForm.enabled) return ''
  return config.value.redis_config?.connected ? 'ok' : 'fail'
})
const redisBadgeText = computed(() => {
  if (!redisForm.enabled) return '已关闭'
  return config.value.redis_config?.connected ? '已连接' : '未连接'
})

// Labels and descriptions
const llmLabels = {
  ollama: 'Ollama 本地',
  openai: 'OpenAI 兼容 API',
  custom: '自定义 API',
}

const llmDescs = {
  ollama: '本地运行，无需网络',
  openai: '通义千问 / DeepSeek / OpenAI',
  custom: '兼容 OpenAI 接口的任意服务',
}

const ocrLabels = {
  local: '本地 OCR (EasyOCR)',
  aliyun: '阿里云百炼 OCR',
  custom_api: '自定义 OCR API',
}

const ocrDescs = {
  local: 'CPU 识别，离线可用',
  aliyun: 'qwen-vl-ocr / qwen3.5-ocr',
  custom_api: '调用远程 API',
}

const searchLabels = {
  duckduckgo: 'DuckDuckGo',
  bing: 'Bing Search API',
  serpapi: 'SerpAPI',
}

const searchDescs = {
  duckduckgo: '免费，无需配置',
  bing: '需要 Azure API Key',
  serpapi: '需要 SerpAPI Key',
}

async function loadConfig() {
  loading.value = true
  try {
    const res = await getSystemConfig()
    config.value = res.data
    activeConfig.llm = res.data.llm_provider
    activeConfig.ocr = res.data.ocr_provider
    activeConfig.search = res.data.search_provider
    // 回填 Redis 配置（页面保存过的覆盖值优先，否则显示 .env 默认）
    redisForm.url = res.data.redis_config?.url || ''
    redisForm.enabled = res.data.redis_config?.enabled !== false
  } catch (err) {
    console.error('加载配置失败:', err)
  } finally {
    loading.value = false
  }
}

async function handleProviderChange(type, value) {
  try {
    const res = await switchProvider(type, value)
    // 更新本地显示
    if (type === 'llm') activeConfig.llm = value
    else if (type === 'ocr') activeConfig.ocr = value
    else if (type === 'search') activeConfig.search = value
  } catch (err) {
    console.error(`切换 ${type} 提供商失败:`, err)
  }
}

async function testConnection(type, provider) {
  if (type === 'llm') {
    llmTesting.value = true
    llmTestResult.value = null
    try {
      const res = await testLLMConnection(provider)
      llmTestResult.value = res.data
      llmTestStatus.value = res.data.success ? 'ok' : 'fail'
    } catch (err) {
      llmTestResult.value = { success: false, message: '请求失败: ' + err.message }
      llmTestStatus.value = 'fail'
    } finally {
      llmTesting.value = false
    }
  } else if (type === 'search') {
    searchTesting.value = true
    searchTestResult.value = null
    try {
      const res = await testSearchConnection(provider)
      searchTestResult.value = res.data
      searchTestStatus.value = res.data.success ? 'ok' : 'fail'
    } catch (err) {
      searchTestResult.value = { success: false, message: '请求失败: ' + err.message }
      searchTestStatus.value = 'fail'
    } finally {
      searchTesting.value = false
    }
  }
}

async function testEmbedding() {
  embeddingTesting.value = true
  embeddingTestResult.value = null
  try {
    const res = await testEmbeddingConnection()
    embeddingTestResult.value = res.data
  } catch (err) {
    embeddingTestResult.value = { success: false, message: '请求失败: ' + err.message }
  } finally {
    embeddingTesting.value = false
  }
}

async function saveRedis() {
  redisSaving.value = true
  redisTestResult.value = null
  try {
    const res = await saveRedisConfig({ url: redisForm.url.trim(), enabled: redisForm.enabled })
    if (res.data.success) {
      redisForm.url = res.data.url
      redisForm.enabled = res.data.enabled
      redisTestResult.value = { success: true, message: '配置已保存并立即生效' }
      // 刷新连接状态徽标
      await loadConfig()
      redisTestResult.value = { success: true, message: '配置已保存并立即生效' }
    } else {
      redisTestResult.value = { success: false, message: res.data.message || '保存失败' }
    }
  } catch (err) {
    redisTestResult.value = { success: false, message: '请求失败: ' + err.message }
  } finally {
    redisSaving.value = false
  }
}

async function testRedis() {
  redisTesting.value = true
  redisTestResult.value = null
  try {
    // 用输入框中的地址测试（留空则测当前生效配置）
    const res = await testRedisConnection(redisForm.url.trim())
    redisTestResult.value = res.data
    if (res.data.success) {
      await loadConfig()
    }
  } catch (err) {
    redisTestResult.value = { success: false, message: '请求失败: ' + err.message }
  } finally {
    redisTesting.value = false
  }
}

onMounted(() => {
  loadConfig()
})
</script>

<style scoped>
.config-view {
  height: 100vh;
  overflow-y: auto;
  padding: 32px 40px;
  max-width: 900px;
  margin: 0 auto;
}

.page-header {
  margin-bottom: 32px;
}

.page-title {
  font-size: 24px;
  font-weight: 700;
  color: var(--color-text);
  margin-bottom: 6px;
}

.page-desc {
  font-size: 14px;
  color: var(--color-text-secondary);
}

.loading-container {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 80px 0;
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

.spinner-xs {
  display: inline-block;
  width: 12px;
  height: 12px;
  border: 2px solid rgba(0,0,0,0.1);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  vertical-align: middle;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* Sections */
.config-sections {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.config-section {
  background: white;
  border: 1px solid var(--color-border);
  border-radius: 16px;
  padding: 24px;
}

.section-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 6px;
}

.section-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 17px;
  font-weight: 600;
  color: var(--color-text);
}

.section-badge {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 8px;
  font-weight: 500;
}

.section-badge.ok {
  background: #e8f5e9;
  color: #2e7d32;
}

.section-badge.fail {
  background: #fce4ec;
  color: #c62828;
}

.section-desc {
  font-size: 13px;
  color: var(--color-text-secondary);
  margin-bottom: 16px;
}

/* Provider options */
.provider-options {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 16px;
}

.provider-option {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  cursor: pointer;
  transition: all 0.2s;
  background: #faf8f5;
}

.provider-option:hover {
  border-color: #d4c9bd;
}

.provider-option.selected {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
}

.provider-option input[type="radio"] {
  accent-color: var(--color-primary);
  width: 16px;
  height: 16px;
  flex-shrink: 0;
}

.option-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.option-name {
  font-size: 14px;
  font-weight: 500;
  color: var(--color-text);
}

.option-desc {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.option-config {
  font-size: 12px;
  color: var(--color-text-secondary);
  text-align: right;
  flex-shrink: 0;
}

.no-key {
  display: inline-block;
  margin-top: 2px;
  font-size: 11px;
  color: #ef6c00;
  background: #fff3e0;
  padding: 1px 6px;
  border-radius: 4px;
}

.key-ok {
  font-size: 11px;
  color: #2e7d32;
  background: #e8f5e9;
  padding: 1px 6px;
  border-radius: 4px;
}

/* Redis 配置表单 */
.redis-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-bottom: 16px;
}

.redis-row {
  display: flex;
  align-items: center;
  gap: 12px;
}

.redis-label {
  width: 88px;
  flex-shrink: 0;
  font-size: 13px;
  color: var(--color-text-secondary);
}

.redis-input {
  flex: 1;
  padding: 8px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  font-size: 13px;
  color: var(--color-text);
  background: #faf8f5;
  outline: none;
  transition: border-color 0.2s;
}

.redis-input:focus {
  border-color: var(--color-primary);
  background: #fff;
}

.redis-hint {
  font-size: 12px;
  color: var(--color-text-secondary);
}

/* Actions */
.section-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
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

.btn-primary {
  background: var(--color-primary);
  color: #fff;
}

.btn-primary:hover:not(:disabled) {
  opacity: 0.9;
}

.test-result {
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.test-result.success {
  color: #2e7d32;
}

.test-result.error {
  color: #c62828;
}

.test-elapsed {
  font-size: 11px;
  color: var(--color-text-secondary);
  background: #f3efe9;
  padding: 1px 6px;
  border-radius: 4px;
}

/* Embedding info */
.embedding-info {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 16px;
  padding: 12px 16px;
  background: #faf8f5;
  border-radius: 10px;
  border: 1px solid var(--color-border);
}

.info-row {
  display: flex;
  align-items: center;
  gap: 12px;
}

.info-label {
  font-size: 13px;
  color: var(--color-text-secondary);
  width: 100px;
  flex-shrink: 0;
}

.info-value {
  font-size: 13px;
  color: var(--color-text);
  font-weight: 500;
}
</style>
