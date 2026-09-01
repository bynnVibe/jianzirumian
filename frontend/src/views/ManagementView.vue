<template>
  <div class="management-view">
    <!-- 标签页导航 -->
    <div class="tabs-header">
      <div class="tabs-nav">
        <button
          v-if="isAdmin && isManagementRoute"
          class="tab-btn"
          :class="{ active: activeTab === 'settings' }"
          @click="activeTab = 'settings'"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="18" height="18">
            <circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>
          </svg>
          系统设置
        </button>
        <button
          v-if="isAdmin && isManagementRoute"
          class="tab-btn"
          :class="{ active: activeTab === 'users' }"
          @click="activeTab = 'users'"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="18" height="18">
            <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>
          </svg>
          用户管理
        </button>
        <button
          v-if="!isManagementRoute"
          class="tab-btn"
          :class="{ active: activeTab === 'knowledge' }"
          @click="activeTab = 'knowledge'"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="18" height="18">
            <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
          </svg>
          知识库管理
        </button>
      </div>
    </div>

    <!-- ============ 系统设置标签页（管理员） ============ -->
    <div v-show="activeTab === 'settings'" class="tab-content tab-settings">
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
          <!-- 参数在线配置 -->
          <div v-if="currentParamFields('llm').length" class="param-panel">
            <div class="param-row" v-for="f in currentParamFields('llm')" :key="f.key">
              <label class="param-label">{{ f.label }}</label>
              <input
                class="param-input"
                :type="f.secret ? 'password' : 'text'"
                v-model="paramForms.llm[f.key]"
                :placeholder="paramPlaceholder('llm', f)"
                autocomplete="off"
              />
            </div>
          </div>
          <div class="section-actions">
            <button
              v-if="currentParamFields('llm').length"
              class="btn btn-primary"
              @click="saveParams('llm')"
              :disabled="savingCategory === 'llm'"
            >
              {{ savingCategory === 'llm' ? '保存中...' : '保存配置' }}
            </button>
            <button class="btn btn-outline" @click="testConnection('llm', activeConfig.llm)" :disabled="llmTesting">
              <span v-if="llmTesting" class="spinner-xs"></span>
              {{ llmTesting ? '测试中...' : '测试连接' }}
            </button>
            <span v-if="saveResults.llm" class="test-result" :class="saveResults.llm.success ? 'success' : 'error'">
              {{ saveResults.llm.message }}
            </span>
            <span v-if="llmTestResult" class="test-result" :class="llmTestResult.success ? 'success' : 'error'">
              {{ llmTestResult.message }}
              <span v-if="llmTestResult.elapsed" class="test-elapsed">{{ llmTestResult.elapsed }}s</span>
            </span>
          </div>
        </section>

        <!-- OpenRouter 免费模型选择 -->
        <section class="config-section">
          <div class="section-header">
            <h2 class="section-title">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="20" height="20">
                <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
              </svg>
              OpenRouter 免费模型
            </h2>
            <span v-if="openrouterModels.length" class="section-badge ok">{{ openrouterTotal }} 个免费模型</span>
          </div>
          <p class="section-desc">实时搜索 OpenRouter 平台的免费模型，一键设为系统 LLM（免费模型仍需在 openrouter.ai 注册获取 API Key）</p>

          <div class="openrouter-toolbar">
            <input
              class="param-input openrouter-search"
              type="text"
              v-model="openrouterSearch"
              placeholder="输入关键词实时搜索，如 deepseek、qwen、gpt..."
              autocomplete="off"
            />
            <button class="btn btn-outline" @click="loadOpenRouterModels(true)" :disabled="openrouterLoading">
              <span v-if="openrouterLoading" class="spinner-xs"></span>
              {{ openrouterLoading ? '搜索中...' : '刷新列表' }}
            </button>
          </div>

          <!-- API Key 输入（可选：不填则沿用已保存的 Key） -->
          <div class="param-row openrouter-key-row">
            <label class="param-label">OpenRouter API Key</label>
            <input
              class="param-input"
              type="password"
              v-model="openrouterApiKey"
              placeholder="留空则沿用当前已配置的 Key"
              autocomplete="off"
            />
          </div>

          <!-- 模型列表 -->
          <div class="openrouter-list" v-if="openrouterModels.length">
            <div
              v-for="m in openrouterModels"
              :key="m.id"
              class="openrouter-item"
              :class="{ active: currentCustomModel === m.id }"
            >
              <div class="openrouter-item-main">
                <div class="openrouter-item-name">
                  {{ m.name || m.id }}
                  <span v-if="currentCustomModel === m.id" class="openrouter-current">当前使用中</span>
                </div>
                <div class="openrouter-item-id">{{ m.id }}</div>
                <div v-if="m.description" class="openrouter-item-desc" :title="m.description">{{ m.description }}</div>
              </div>
              <div class="openrouter-item-side">
                <span class="openrouter-ctx">上下文 {{ formatCtx(m.context_length) }}</span>
                <button
                  class="btn btn-primary openrouter-apply-btn"
                  @click="applyOpenRouter(m)"
                  :disabled="applyingModel === m.id"
                >
                  {{ applyingModel === m.id ? '应用中...' : '设为 LLM' }}
                </button>
              </div>
            </div>
          </div>
          <div v-else-if="openrouterLoaded && !openrouterLoading" class="openrouter-empty">
            {{ openrouterError || '未找到匹配的免费模型' }}
          </div>

          <div v-if="openrouterResult" class="section-actions">
            <span class="test-result" :class="openrouterResult.success ? 'success' : 'error'">
              {{ openrouterResult.message }}
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
              <div v-if="opt !== 'local'" class="option-config">
                <span v-if="!config.ocr_config?.[opt]?.has_key" class="no-key">未配置 API Key</span>
                <span v-else class="key-ok">已配置</span>
              </div>
            </label>
          </div>
          <!-- 参数在线配置 -->
          <div v-if="currentParamFields('ocr').length" class="param-panel">
            <div class="param-row" v-for="f in currentParamFields('ocr')" :key="f.key">
              <label class="param-label">{{ f.label }}</label>
              <input
                class="param-input"
                :type="f.secret ? 'password' : 'text'"
                v-model="paramForms.ocr[f.key]"
                :placeholder="paramPlaceholder('ocr', f)"
                autocomplete="off"
              />
            </div>
          </div>
          <div class="section-actions">
            <button
              v-if="currentParamFields('ocr').length"
              class="btn btn-primary"
              @click="saveParams('ocr')"
              :disabled="savingCategory === 'ocr'"
            >
              {{ savingCategory === 'ocr' ? '保存中...' : '保存配置' }}
            </button>
            <button class="btn btn-outline" @click="testOCR" :disabled="ocrTesting">
              <span v-if="ocrTesting" class="spinner-xs"></span>
              {{ ocrTesting ? '测试中...' : '测试连接' }}
            </button>
            <span v-if="saveResults.ocr" class="test-result" :class="saveResults.ocr.success ? 'success' : 'error'">
              {{ saveResults.ocr.message }}
            </span>
            <span v-if="ocrTestResult" class="test-result" :class="ocrTestResult.success ? 'success' : 'error'">
              {{ ocrTestResult.message }}
              <span v-if="ocrTestResult.elapsed" class="test-elapsed">{{ ocrTestResult.elapsed }}s</span>
            </span>
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
                <span v-if="config.search_config?.[opt]?.has_key" class="key-ok">已配置</span>
                <span v-else-if="opt === 'anysearch'" class="key-ok">匿名模式可用</span>
                <span v-else class="no-key">未配置 API Key</span>
              </div>
            </label>
          </div>
          <!-- 参数在线配置 -->
          <div v-if="currentParamFields('search').length" class="param-panel">
            <div class="param-row" v-for="f in currentParamFields('search')" :key="f.key">
              <label class="param-label">{{ f.label }}</label>
              <input
                class="param-input"
                :type="f.secret ? 'password' : 'text'"
                v-model="paramForms.search[f.key]"
                :placeholder="paramPlaceholder('search', f)"
                autocomplete="off"
              />
            </div>
          </div>
          <div class="section-actions">
            <button
              v-if="currentParamFields('search').length"
              class="btn btn-primary"
              @click="saveParams('search')"
              :disabled="savingCategory === 'search'"
            >
              {{ savingCategory === 'search' ? '保存中...' : '保存配置' }}
            </button>
            <button class="btn btn-outline" @click="testConnection('search', activeConfig.search)" :disabled="searchTesting">
              <span v-if="searchTesting" class="spinner-xs"></span>
              {{ searchTesting ? '测试中...' : '测试连接' }}
            </button>
            <span v-if="saveResults.search" class="test-result" :class="saveResults.search.success ? 'success' : 'error'">
              {{ saveResults.search.message }}
            </span>
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
          <p class="section-desc">向量嵌入模型，将文本转换为向量用于知识检索。注意：更换模型后已有知识库需重新上传构建</p>
          <div class="provider-options">
            <label
              v-for="opt in config.available_embedding_providers || []"
              :key="opt"
              class="provider-option"
              :class="{ selected: activeConfig.embedding === opt }"
            >
              <input type="radio" name="embedding" :value="opt" v-model="activeConfig.embedding" @change="handleProviderChange('embedding', opt)" />
              <div class="option-content">
                <span class="option-name">{{ embeddingLabels[opt] || opt }}</span>
                <span class="option-desc">{{ embeddingDescs[opt] }}</span>
              </div>
              <div class="option-config">
                模型: {{ config.embedding_config?.[opt]?.model }}
                <span v-if="opt !== 'ollama' && !config.embedding_config?.[opt]?.has_key" class="no-key">未配置 API Key</span>
              </div>
            </label>
          </div>
          <!-- 参数在线配置 -->
          <div v-if="currentParamFields('embedding').length" class="param-panel">
            <div class="param-row" v-for="f in currentParamFields('embedding')" :key="f.key">
              <label class="param-label">{{ f.label }}</label>
              <input
                class="param-input"
                :type="f.secret ? 'password' : 'text'"
                v-model="paramForms.embedding[f.key]"
                :placeholder="paramPlaceholder('embedding', f)"
                autocomplete="off"
              />
            </div>
          </div>
          <div class="embedding-info">
            <div class="info-row">
              <span class="info-label">知识库条目</span>
              <span class="info-value">{{ config.embedding?.knowledge_count }}</span>
            </div>
          </div>
          <div class="section-actions">
            <button
              v-if="currentParamFields('embedding').length"
              class="btn btn-primary"
              @click="saveParams('embedding')"
              :disabled="savingCategory === 'embedding'"
            >
              {{ savingCategory === 'embedding' ? '保存中...' : '保存配置' }}
            </button>
            <button class="btn btn-outline" @click="testEmbedding" :disabled="embeddingTesting">
              <span v-if="embeddingTesting" class="spinner-xs"></span>
              {{ embeddingTesting ? '测试中...' : '测试连接' }}
            </button>
            <span v-if="saveResults.embedding" class="test-result" :class="saveResults.embedding.success ? 'success' : 'error'">
              {{ saveResults.embedding.message }}
            </span>
            <span v-if="embeddingTestResult" class="test-result" :class="embeddingTestResult.success ? 'success' : 'error'">
              {{ embeddingTestResult.message }}
              <span v-if="embeddingTestResult.elapsed" class="test-elapsed">{{ embeddingTestResult.elapsed }}s</span>
            </span>
          </div>
        </section>

        <!-- Rerank 配置 -->
        <section class="config-section">
          <div class="section-header">
            <h2 class="section-title">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="20" height="20">
                <line x1="21" y1="6" x2="3" y2="6"/><line x1="17" y1="12" x2="3" y2="12"/><line x1="13" y1="18" x2="3" y2="18"/>
              </svg>
              Rerank 重排序模型
            </h2>
          </div>
          <p class="section-desc">对知识检索结果按相关度重排序，提升引用质量。本地模型离线可用但首次加载较慢；DashScope API 响应快，需要 API Key</p>
          <div class="provider-options">
            <label
              v-for="opt in config.available_rerank_providers || []"
              :key="opt"
              class="provider-option"
              :class="{ selected: activeConfig.rerank === opt }"
            >
              <input type="radio" name="rerank" :value="opt" v-model="activeConfig.rerank" @change="handleProviderChange('rerank', opt)" />
              <div class="option-content">
                <span class="option-name">{{ rerankLabels[opt] || opt }}</span>
                <span class="option-desc">{{ rerankDescs[opt] }}</span>
              </div>
              <div class="option-config">
                模型: {{ config.rerank_config?.[opt]?.model }}
                <span v-if="opt === 'dashscope' && !config.rerank_config?.[opt]?.has_key" class="no-key">未配置 API Key</span>
              </div>
            </label>
          </div>
          <!-- 参数在线配置 -->
          <div v-if="currentParamFields('rerank').length" class="param-panel">
            <div class="param-row" v-for="f in currentParamFields('rerank')" :key="f.key">
              <label class="param-label">{{ f.label }}</label>
              <input
                class="param-input"
                :type="f.secret ? 'password' : 'text'"
                v-model="paramForms.rerank[f.key]"
                :placeholder="paramPlaceholder('rerank', f)"
                autocomplete="off"
              />
            </div>
          </div>
          <div class="section-actions">
            <button
              v-if="currentParamFields('rerank').length"
              class="btn btn-primary"
              @click="saveParams('rerank')"
              :disabled="savingCategory === 'rerank'"
            >
              {{ savingCategory === 'rerank' ? '保存中...' : '保存配置' }}
            </button>
            <button class="btn btn-outline" @click="testRerank" :disabled="rerankTesting">
              <span v-if="rerankTesting" class="spinner-xs"></span>
              {{ rerankTesting ? '测试中...' : '测试连接' }}
            </button>
            <span v-if="saveResults.rerank" class="test-result" :class="saveResults.rerank.success ? 'success' : 'error'">
              {{ saveResults.rerank.message }}
            </span>
            <span v-if="rerankTestResult" class="test-result" :class="rerankTestResult.success ? 'success' : 'error'">
              {{ rerankTestResult.message }}
              <span v-if="rerankTestResult.elapsed" class="test-elapsed">{{ rerankTestResult.elapsed }}s</span>
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
            缓存知识库原始文件（图片 / Word / PDF），加快检索来源跳转与原文预览。公共知识库文件所有用户共享缓存，个人知识库文件按属主隔离。本页保存立即生效；修改 .env 需重启后端。
          </p>

          <div class="toggle-row">
            <div class="toggle-info">
              <span class="toggle-label">{{ redisForm.enabled ? '文件缓存已开启' : '文件缓存已关闭' }}</span>
              <span class="toggle-hint">{{ redisForm.enabled ? '跳转与预览优先读缓存' : '所有请求直读磁盘' }}</span>
            </div>
            <button class="toggle-switch" :class="{ active: redisForm.enabled }" @click="redisForm.enabled = !redisForm.enabled">
              <span class="toggle-knob"></span>
            </button>
          </div>

          <div class="param-panel">
            <div class="param-row">
              <label class="param-label">Redis 地址</label>
              <input
                class="param-input"
                v-model="redisForm.url"
                placeholder="留空使用 .env 默认值（redis://host:6379/0）"
                spellcheck="false"
                autocomplete="off"
              />
            </div>
            <div class="param-row">
              <label class="param-label">缓存策略</label>
              <span class="toggle-hint">有效期 {{ config.redis_config?.ttl || 86400 }} 秒 · 单文件上限 {{ config.redis_config?.max_mb || 20 }} MB（可在 .env 调整）</span>
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

        <!-- llm-wiki 知识编译 -->
        <section class="config-section">
          <div class="section-header">
            <h2 class="section-title">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="20" height="20">
                <path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>
              </svg>
              llm-wiki 知识编译
            </h2>
            <span class="section-badge" :class="wikiEnabled ? 'ok' : ''">{{ wikiEnabled ? '编译已开启' : '编译已关闭' }}</span>
            <span class="section-badge" :class="wikiPages.length ? 'ok' : ''">{{ wikiPages.length }} 则百科</span>
          </div>
          <p class="section-desc">
            借鉴 llm-wiki 思想：LLM 作为「知识编译器」，资料入库后先被编译成结构化百科卡片并回写向量库，检索时优先命中高质量卡片，原始碎片兜底；还可对主题跨素材生成持久化 digest 综合词条。编译完全异步、失败静默降级，不影响现有问答链路。
          </p>

          <div class="toggle-row">
            <div class="toggle-info">
              <span class="toggle-label">{{ wikiForm.compile_enabled ? '知识编译已开启' : '知识编译已关闭' }}</span>
              <span class="toggle-hint">开启后新上传资料将在后台自动编译为百科卡片</span>
            </div>
            <button class="toggle-switch" :class="{ active: wikiForm.compile_enabled }" @click="wikiForm.compile_enabled = !wikiForm.compile_enabled">
              <span class="toggle-knob"></span>
            </button>
          </div>
          <div class="toggle-row">
            <div class="toggle-info">
              <span class="toggle-label">{{ wikiForm.priority_enabled ? '检索优先已开启' : '检索优先已关闭' }}</span>
              <span class="toggle-hint">问答检索时百科卡片稳定排序前置，优先于原始碎片</span>
            </div>
            <button class="toggle-switch" :class="{ active: wikiForm.priority_enabled }" @click="wikiForm.priority_enabled = !wikiForm.priority_enabled">
              <span class="toggle-knob"></span>
            </button>
          </div>

          <div class="section-actions">
            <button class="btn btn-primary" @click="saveWikiSettings" :disabled="wikiSaving">
              {{ wikiSaving ? '保存中...' : '保存配置' }}
            </button>
            <button class="btn btn-outline" @click="doRecompileAll" :disabled="wikiRecompiling">
              <span v-if="wikiRecompiling" class="spinner-xs"></span>
              {{ wikiRecompiling ? '调度中...' : '存量补编译' }}
            </button>
            <span v-if="wikiMsg" class="test-result" :class="wikiMsg.success ? 'success' : 'error'">{{ wikiMsg.message }}</span>
          </div>

          <!-- digest 跨素材综合 -->
          <div class="param-panel">
            <div class="param-row">
              <label class="param-label">digest 主题</label>
              <input
                class="param-input"
                v-model="wikiDigestQuery"
                placeholder="输入主题，跨素材深度综合生成持久化百科词条（耗时较长）"
                spellcheck="false"
                @keyup.enter="doDigest"
              />
            </div>
            <div class="param-row">
              <label class="param-label"></label>
              <button class="btn btn-outline btn-sm" @click="doDigest" :disabled="wikiDigesting || !wikiDigestQuery.trim()">
                <span v-if="wikiDigesting" class="spinner-xs"></span>
                {{ wikiDigesting ? '综合生成中...' : '生成综合词条' }}
              </button>
            </div>
          </div>

          <!-- 百科页面列表 -->
          <div class="wiki-filter-row">
            <select v-model="wikiTypeFilter" class="kb-filter-select" @change="loadWikiPages">
              <option value="">全部类型</option>
              <option value="source">来源卡片</option>
              <option value="digest">综合词条</option>
            </select>
            <select v-model="wikiKbFilter" class="kb-filter-select" @change="loadWikiPages">
              <option value="">全部知识库</option>
              <option v-for="kb in kbList" :key="kb.id" :value="kb.id">{{ kb.name }}</option>
            </select>
            <button class="btn btn-outline btn-sm" @click="loadWikiPages" :disabled="wikiPagesLoading">刷新</button>
          </div>
          <div v-if="wikiPagesLoading" class="loading-container"><div class="spinner"></div><p>加载百科列表...</p></div>
          <div v-else-if="!wikiPages.length" class="empty-state"><p>暂无百科词条，可点击「存量补编译」为已有资料生成知识卡片</p></div>
          <ul v-else class="wiki-page-list">
            <li v-for="p in wikiPages" :key="p.id" class="wiki-page-item">
              <span class="wiki-type-tag" :class="p.page_type">{{ p.page_type === 'digest' ? '综合' : '卡片' }}</span>
              <button class="wiki-page-title" :title="p.excerpt || p.title" @click="openWikiPage(p.id)">{{ p.title }}</button>
              <span v-if="p.visibility === 'private'" class="kb-visibility-badge private">私人</span>
              <button v-if="canDeleteWikiPage(p)" class="btn btn-danger btn-sm" @click="wikiPageDeleteTarget = p">删除</button>
            </li>
          </ul>
        </section>

      </div>
    </div>

    <!-- ============ 用户管理标签页（管理员） ============ -->
    <div v-show="activeTab === 'users'" class="tab-content tab-users">
      <div class="tab-users-inner">
        <div class="users-header">
          <h2 class="users-title">用户管理</h2>
          <p class="users-desc">管理系统中的所有用户账号</p>
        </div>

        <!-- 注册功能开关 -->
        <section class="config-section registration-section">
          <div class="section-header">
            <h2 class="section-title">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="20" height="20">
                <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>
              </svg>
              用户注册
            </h2>
          </div>
          <p class="section-desc">控制是否允许新用户自行注册账号。关闭后注册页面不可用，已有用户不受影响。</p>
          <div class="toggle-row">
            <div class="toggle-info">
              <span class="toggle-label">{{ registrationEnabled ? '注册功能已开启' : '注册功能已关闭' }}</span>
              <span class="toggle-hint">{{ registrationEnabled ? '新用户可自由注册账号' : '仅管理员可在后台添加用户' }}</span>
            </div>
            <button
              class="toggle-switch"
              :class="{ active: registrationEnabled }"
              @click="handleToggleRegistration"
              :disabled="togglingRegistration"
            >
              <span class="toggle-knob"></span>
            </button>
          </div>
        </section>

        <!-- 游客免费访问次数 -->
        <section class="config-section registration-section">
          <div class="section-header">
            <h2 class="section-title">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="20" height="20">
                <path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8.5" cy="7" r="4"/><line x1="20" y1="8" x2="20" y2="14"/><line x1="23" y1="11" x2="17" y2="11"/>
              </svg>
              游客访问
            </h2>
          </div>
          <p class="section-desc">设定未登录用户（游客）可免费提问的次数，按 IP 计数；用完后需登录才能继续使用。设为 0 表示禁止游客提问。</p>
          <div class="toggle-row">
            <div class="guest-limit-input-wrap">
              <input
                v-model.number="guestLimitInput"
                type="number"
                min="0"
                class="guest-limit-input"
                placeholder="3"
              />
              <span class="toggle-hint">次 / 每个 IP</span>
            </div>
            <button
              class="btn btn-primary btn-sm"
              @click="handleSaveGuestLimit"
              :disabled="savingGuestLimit || guestLimitInput === config.guest_query_limit"
            >
              {{ savingGuestLimit ? '保存中...' : '保存' }}
            </button>
          </div>
        </section>

        <!-- 子标签：账号管理 / 统计看板 -->
        <div class="users-subtabs">
          <button
            class="subtab-btn"
            :class="{ active: usersSubTab === 'accounts' }"
            @click="usersSubTab = 'accounts'"
          >账号管理</button>
          <button
            class="subtab-btn"
            :class="{ active: usersSubTab === 'stats' }"
            @click="usersSubTab = 'stats'"
          >用户统计看板</button>
        </div>

        <template v-if="usersSubTab === 'accounts'">
        <div v-if="usersLoading" class="loading-container">
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
                <th>注册时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="u in users" :key="u.id" :class="{ 'row-disabled': !u.is_active }">
                <td class="cell-username">
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
                <td class="cell-date">{{ formatUserDate(u.created_at) }}</td>
                <td class="cell-actions">
                  <template v-if="u.role !== 'admin'">
                    <button class="btn-action btn-toggle" @click="handleToggleActive(u)" :title="u.is_active ? '禁用用户' : '启用用户'">
                      {{ u.is_active ? '禁用' : '启用' }}
                    </button>
                    <button class="btn-action btn-delete" @click="confirmDeleteUser(u)" title="删除用户">删除</button>
                  </template>
                  <span v-else class="text-muted">—</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- 用户删除确认弹窗 -->
        <div v-if="showUserDeleteConfirm" class="modal-overlay" @click.self="showUserDeleteConfirm = false">
          <div class="modal-box">
            <h3 class="modal-title">确认删除</h3>
            <p class="modal-desc">确定要删除用户「{{ userDeleteTarget?.username }}」吗？此操作不可撤销，该用户的所有数据也将被移除。</p>
            <div class="modal-actions">
              <button class="btn btn-outline" @click="showUserDeleteConfirm = false">取消</button>
              <button class="btn btn-danger" @click="doDeleteUser" :disabled="userDeleting">
                {{ userDeleting ? '删除中...' : '确认删除' }}
              </button>
            </div>
          </div>
        </div>
        </template>

        <!-- 用户统计看板（嵌入管理员看板页面组件） -->
        <UserStatsBoard v-else />
      </div>
    </div>

    <!-- ============ 知识库管理标签页（所有用户） ============ -->
    <div v-show="activeTab === 'knowledge'" class="tab-content tab-knowledge">
      <!-- 顶部操作栏 -->
      <div class="knowledge-toolbar">
        <div class="knowledge-stats">
          <span class="stat-item">共 <strong>{{ entries.length }}</strong> 个来源</span>
          <span class="stat-divider">|</span>
          <span class="stat-item">总文档片段 <strong>{{ totalChunks }}</strong></span>
        </div>

        <div class="toolbar-right">
          <!-- 搜索框 -->
          <div class="search-box">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="search-icon" width="16" height="16">
              <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
            </svg>
            <input
              v-model="searchQuery"
              type="text"
              placeholder="搜索标题、文件名或知识库内容..."
              @keyup.enter="doSearch"
              @input="isSearchMode = false; searchResults = []"
              class="search-input"
            />
            <button v-if="searchQuery" class="search-clear" @click="clearSearch" title="清除搜索">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          </div>

          <div class="toolbar-actions">
            <button
              class="btn btn-outline btn-sm"
              :class="{ 'btn-active': sortOrder === 'asc' }"
              @click="toggleSort"
              :title="sortOrder === 'desc' ? '切换为升序' : '切换为降序'"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                <line x1="12" y1="5" x2="12" y2="19"/><polyline points="19 12 12 19 5 12"/>
              </svg>
              {{ sortOrder === 'desc' ? '最新在前' : '最旧在前' }}
            </button>
            <button class="btn btn-outline btn-sm" @click="loadEntries" :disabled="loadingEntries">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                <polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
              </svg>
              刷新
            </button>
            <button v-if="!isGuest" class="btn btn-danger btn-sm" @click="confirmClearAll" :disabled="entries.length === 0">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                <polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
              </svg>
              清空知识库
            </button>
          </div>
        </div>
      </div>

      <!-- 知识库筛选与管理 -->
      <div class="kb-filter-bar">
        <label class="kb-filter-label">知识库筛选</label>
        <select v-model="kbFilterVisibility" class="kb-filter-select kb-filter-visibility">
          <option value="">全部来源</option>
          <option value="public">公共知识库</option>
          <option value="private">个人知识库</option>
        </select>
        <select v-model="filterKbId" class="kb-filter-select">
          <option value="">全部知识库（{{ filteredFilterKbList.length }} 个）</option>
          <option v-for="kb in filteredFilterKbList" :key="kb.id" :value="kb.id">
            {{ kb.name }}（{{ kb.entry_count }} 条）
          </option>
        </select>
        <button class="btn btn-outline btn-sm" @click="showKbPanel = !showKbPanel">
          {{ showKbPanel ? '收起管理' : '知识库管理' }}
        </button>
      </div>

      <!-- 知识库管理面板 -->
      <div v-if="showKbPanel" class="kb-panel">
        <div v-if="kbList.length === 0" class="kb-panel-empty">暂无可见的知识库</div>
        <div v-for="kb in kbList" :key="kb.id" class="kb-panel-item">
          <div class="kb-panel-info">
            <span class="kb-panel-name">{{ kb.name }}</span>
            <span class="kb-panel-tag" :class="kb.visibility === 'public' ? 'tag-public' : 'tag-private'">
              {{ kb.visibility === 'public' ? '公共' : '个人' }}
            </span>
            <span class="kb-panel-count">{{ kb.entry_count }} 条</span>
          </div>
          <button v-if="canDeleteKb(kb)" class="btn btn-danger btn-sm" @click="onDeleteKb(kb)">删除库</button>
        </div>
      </div>

      <!-- 语义搜索结果 header -->
      <div v-if="isSearchMode && searchResults.length > 0" class="search-results-header">
        <span>
          内容检索：<strong>{{ chunkResults.length }}</strong> 个相关片段
          <span v-if="titleSearchResults.length > 0">，另有 <strong>{{ titleSearchResults.length }}</strong> 个标题匹配文件</span>
        </span>
        <button class="btn btn-outline btn-sm" @click="clearSearch">返回全部</button>
      </div>

      <!-- 实时标题过滤提示（未触发语义搜索时） -->
      <div v-else-if="!isSearchMode && searchQuery.trim()" class="search-results-header search-filter-tip">
        <span>标题匹配：{{ displayEntries.length }} 条 &nbsp;·&nbsp; 按 Enter 进行内容检索</span>
        <button class="btn btn-outline btn-sm" @click="clearSearch">清除</button>
      </div>

      <!-- 搜索中 -->
      <div v-if="isSearching" class="loading-container">
        <div class="spinner"></div>
        <p>搜索中...</p>
      </div>

      <!-- 语义搜索无结果 -->
      <div v-else-if="isSearchMode && searchResults.length === 0 && !isSearching" class="empty-state">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" class="empty-icon">
          <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
        </svg>
        <p class="empty-title">内容检索无匹配</p>
        <p class="empty-desc">未找到包含「{{ searchQuery }}」的知识库文档片段</p>
        <button class="btn btn-outline" @click="clearSearch">返回全部</button>
      </div>

      <!-- 语义搜索结果：片段展示区 -->
      <div v-else-if="isSearchMode && !isSearching" class="search-chunk-area">
        <!-- 语义片段列表 -->
        <div v-if="chunkResults.length > 0" class="chunk-section">
          <div class="chunk-section-title">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
              <polyline points="14 2 14 8 20 8"/>
              <line x1="16" y1="13" x2="8" y2="13"/>
              <line x1="16" y1="17" x2="8" y2="17"/>
            </svg>
            内容检索片段 ({{ chunkResults.length }})
          </div>
          <div class="chunk-list">
            <div
              v-for="chunk in chunkResults"
              :key="chunk._key"
              class="chunk-item"
              :class="{ expanded: expandedChunkKey === chunk._key }"
            >
              <!-- 片段头部：可点击展开 -->
              <div class="chunk-header" @click="toggleChunk(chunk._key)">
                <span class="chunk-badge">
                  <span class="relevance-score" :class="chunkRelevanceClass(chunk.relevance)">
                    {{ chunk.relevance }}%
                  </span>
                </span>
                <div class="chunk-header-info">
                  <span class="chunk-title">{{ chunk.title || chunk.filename }}</span>
                  <span v-if="chunk.kb_name" class="chunk-kb-badge">{{ chunk.kb_name }}</span>
                  <span v-if="chunk.page_number" class="chunk-page">第 {{ chunk.page_number }} 页</span>
                </div>
                <span class="chunk-preview">{{ chunk.text ? chunk.text.slice(0, 80) + (chunk.text.length > 80 ? '...' : '') : '' }}</span>
                <span class="chunk-toggle">{{ expandedChunkKey === chunk._key ? '收起' : '展开' }}</span>
              </div>
              <!-- 片段展开详情 -->
              <div v-if="expandedChunkKey === chunk._key" class="chunk-detail">
                <div class="chunk-full-text">{{ chunk.text }}</div>
                <div class="chunk-actions">
                  <!-- 图片类来源：显示原图 -->
                  <div v-if="chunk.source_type === 'image' && chunk.source_image" class="chunk-image-box">
                    <img :src="getImageUrl(chunk.source_image)" class="chunk-image" alt="原始图片" @click="openImageFromChunk(chunk)" />
                    <a :href="getImageUrl(chunk.source_image)" target="_blank" class="chunk-link">查看原图</a>
                  </div>
                  <!-- 文档类来源：跳转预览 -->
                  <template v-else-if="chunk.source_type === 'pdf' || chunk.source_type === 'word'">
                    <a :href="getChunkSourceUrl(chunk)" target="_blank" class="chunk-link">
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12">
                        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                        <polyline points="14 2 14 8 20 8"/>
                      </svg>
                      {{ chunk.source_type === 'word' ? '查看 Word 文档' : '查看 PDF' }}{{ chunk.page_number ? '（第 ' + chunk.page_number + ' 页）' : '' }} ↗
                    </a>
                  </template>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- 标题匹配的文件列表 -->
        <div v-if="titleSearchResults.length > 0" class="chunk-section">
          <div class="chunk-section-title">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
              <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
            </svg>
            标题匹配文件 ({{ titleSearchResults.length }})
          </div>
          <div class="entries-list">
            <div
              v-for="entry in titleSearchResults"
              :key="entry._key"
              class="entry-card"
              @click="openPreview(entry)"
            >
              <div class="entry-main">
                <div class="entry-info">
                  <div class="entry-filename">{{ entry.title || entry.filename }}</div>
                  <div v-if="entry.title && entry.title !== entry.filename" class="entry-subname">{{ entry.filename }}</div>
                  <div class="entry-meta">
                    <span v-if="entry.kb_name" class="meta-item kb-badge">{{ entry.kb_name }}</span>
                    <span class="meta-item">{{ entry.chunk_count }} 个文档片段</span>
                    <span v-if="entry.created_at" class="meta-item">{{ formatDate(entry.created_at) }}</span>
                  </div>
                  <div class="entry-preview">{{ entry.text_preview || '无文本内容' }}</div>
                </div>
              </div>
              <div v-if="entry.source_type === 'word' || entry.source_type === 'pdf'" class="entry-thumb entry-thumb-doc">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="32" height="32">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                  <polyline points="14 2 14 8 20 8"/>
                </svg>
                <span class="doc-type-label">{{ entry.source_type === 'word' ? 'Word' : 'PDF' }}</span>
              </div>
              <div v-else-if="entry.source_image" class="entry-thumb">
                <img :src="getImageUrl(entry.source_image)" alt="" @error="onThumbError" />
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 实时标题过滤无结果 -->
      <div v-else-if="!isSearchMode && searchQuery.trim() && displayEntries.length === 0 && !isSearching" class="empty-state">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" class="empty-icon">
          <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
        </svg>
        <p class="empty-title">未找到匹配标题</p>
        <p class="empty-desc">按 Enter 可搜索知识库内容</p>
        <button class="btn btn-outline" @click="clearSearch">清除搜索</button>
      </div>

      <!-- 加载中 -->
      <div v-else-if="loadingEntries && entries.length === 0" class="loading-container">
        <div class="spinner"></div>
        <p>加载知识库中...</p>
      </div>

      <!-- 空知识库 -->
      <div v-else-if="entries.length === 0" class="empty-state">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" class="empty-icon">
          <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
        </svg>
        <p class="empty-title">知识库为空</p>
        <p class="empty-desc">尚未上传任何内容，请先到「知识库上传」页面上传图片 / Word / PDF</p>
        <router-link to="/upload" class="btn btn-primary">前往上传</router-link>
      </div>

      <!-- 条目列表 -->
      <div v-else class="entries-list">
        <div
          v-for="entry in displayEntries"
          :key="entry._key || entry.source_image"
          class="entry-card"
          @click="openPreview(entry)"
        >
          <div class="entry-main">
            <div class="entry-info">
              <div class="entry-filename">{{ entry._isSearchResult ? '搜索结果' : (entry.title || entry.filename) }}</div>
              <div v-if="entry.title && entry.title !== entry.filename" class="entry-subname">{{ entry.filename }}</div>
              <div class="entry-meta">
                <span v-if="entry.kb_name" class="meta-item kb-badge">{{ entry.kb_name }}</span>
                <span class="meta-item">{{ entry._isSearchResult ? '' : entry.chunk_count + ' 个文档片段' }}</span>
                <span v-if="entry._isSearchResult && entry.relevance" class="meta-item relevance-badge">相关度 {{ entry.relevance }}%</span>
                <span v-if="entry.created_at" class="meta-item">{{ formatDate(entry.created_at) }}</span>
              </div>
              <div class="entry-preview">{{ entry._isSearchResult ? entry.text : (entry.text_preview || '无文本内容') }}</div>
            </div>
            <button
              v-if="!isGuest && showKbPanel"
              class="btn btn-move-entry"
              @click.stop="onMoveEntry(entry)"
              title="移动到其它知识库"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
                <path d="M2 7V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H20a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-2"/>
                <line x1="2" y1="12" x2="13" y2="12"/>
                <polyline points="10 9 13 12 10 15"/>
              </svg>
            </button>
            <button
              v-if="!isGuest && showKbPanel"
              class="btn btn-delete-entry"
              @click.stop="onDeleteEntry(entry)"
              title="删除此来源的所有条目"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
                <polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
              </svg>
            </button>
          </div>
          <!-- 关联图片/文档缩略图 -->
          <div v-if="entry.source_type === 'word' || entry.source_type === 'pdf'" class="entry-thumb entry-thumb-doc" @click.stop="openPreview(entry)">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="32" height="32">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
              <polyline points="14 2 14 8 20 8"/>
              <line x1="16" y1="13" x2="8" y2="13"/>
              <line x1="16" y1="17" x2="8" y2="17"/>
              <polyline points="10 9 9 9 8 9"/>
            </svg>
            <span class="doc-type-label">{{ entry.source_type === 'word' ? 'Word' : 'PDF' }}</span>
          </div>
          <div v-else-if="entry.source_image" class="entry-thumb" @click.stop="openPreview(entry)">
            <img :src="getImageUrl(entry.source_image)" alt="" @error="onThumbError" />
          </div>
        </div>
      </div>

      <!-- 游客提示 -->
      <div v-if="isGuest" class="guest-limit-banner">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="20" height="20">
          <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
        </svg>
        <span>游客仅能查看前 3 条知识库记录，登录后可查看全部内容</span>
        <router-link to="/login" class="btn btn-primary btn-sm">去登录</router-link>
      </div>
    </div>
    <div v-if="showDeleteConfirm" class="modal-overlay" @click.self="showDeleteConfirm = false">
      <div class="modal-box">
        <h3 class="modal-title">确认删除</h3>
        <p class="modal-desc">确定要删除「{{ deleteTarget?.filename }}」的所有 {{ deleteTarget?.chunk_count }} 个知识条目吗？此操作不可撤销。</p>
        <div class="modal-actions">
          <button class="btn btn-outline" @click="showDeleteConfirm = false">取消</button>
          <button class="btn btn-danger" @click="doDeleteEntry" :disabled="deleting">
            {{ deleting ? '删除中...' : '确认删除' }}
          </button>
        </div>
      </div>
    </div>

    <!-- ============ 移动知识库确认 ============ -->
    <div v-if="moveTarget" class="modal-overlay" @click.self="moveTarget = null">
      <div class="modal-box">
        <h3 class="modal-title">移动知识库</h3>
        <p class="modal-desc">
          将「{{ moveTarget.filename }}」的 {{ moveTarget.chunk_count }} 个知识条目从「{{ moveTarget.kb_name || '未知库' }}」移动到：
        </p>
        <select v-model="moveTargetKbId" class="move-kb-select">
          <option value="" disabled>请选择目标知识库</option>
          <option v-for="kb in moveKbOptions" :key="kb.id" :value="kb.id">
            {{ kb.name }}（{{ kb.visibility === 'public' ? '公共' : '个人' }}）
          </option>
        </select>
        <div class="modal-actions">
          <button class="btn btn-outline" @click="moveTarget = null">取消</button>
          <button class="btn btn-primary" @click="doMoveEntry" :disabled="moving || !moveTargetKbId">
            {{ moving ? '移动中...' : '确认移动' }}
          </button>
        </div>
      </div>
    </div>

    <!-- ============ 删除知识库确认 ============ -->
    <div v-if="kbDeleteTarget" class="modal-overlay" @click.self="kbDeleteTarget = null">
      <div class="modal-box">
        <h3 class="modal-title">确认删除知识库</h3>
        <p class="modal-desc">
          确定要删除知识库「{{ kbDeleteTarget.name }}」及其全部 {{ kbDeleteTarget.entry_count }} 条向量条目吗？此操作不可撤销。
        </p>
        <div class="modal-actions">
          <button class="btn btn-outline" @click="kbDeleteTarget = null">取消</button>
          <button class="btn btn-danger" @click="doDeleteKb" :disabled="deletingKb">
            {{ deletingKb ? '删除中...' : '确认删除' }}
          </button>
        </div>
      </div>
    </div>

    <!-- ============ 清空确认 ============ -->
    <div v-if="showClearConfirm" class="modal-overlay" @click.self="showClearConfirm = false">
      <div class="modal-box">
        <h3 class="modal-title">确认清空</h3>
        <p class="modal-desc">确定要清空整个知识库吗？共 {{ entries.length }} 个来源、{{ totalChunks }} 个文档片段。此操作不可撤销。</p>
        <div class="modal-actions">
          <button class="btn btn-outline" @click="showClearConfirm = false">取消</button>
          <button class="btn btn-danger" @click="doClearAll" :disabled="clearing">
            {{ clearing ? '清空中...' : '确认清空' }}
          </button>
        </div>
      </div>
    </div>

    <!-- ============ 百科词条详情 ============ -->
    <div v-if="wikiDetailPage" class="modal-overlay" @click.self="wikiDetailPage = null">
      <div class="modal-box wiki-detail-box">
        <h3 class="modal-title">
          <span class="wiki-type-tag" :class="wikiDetailPage.page_type">{{ wikiDetailPage.page_type === 'digest' ? '综合' : '卡片' }}</span>
          {{ wikiDetailPage.title }}
        </h3>
        <div class="wiki-detail-body markdown-content" v-html="renderedWikiPage"></div>
        <div class="modal-actions">
          <button class="btn btn-outline" @click="wikiDetailPage = null">关闭</button>
        </div>
      </div>
    </div>

    <!-- ============ 百科卡片删除确认 ============ -->
    <div v-if="wikiPageDeleteTarget" class="modal-overlay" @click.self="wikiPageDeleteTarget = null">
      <div class="modal-box">
        <h3 class="modal-title">确认删除百科词条</h3>
        <p class="modal-desc">确定要删除「{{ wikiPageDeleteTarget?.title }}」吗？其向量条目一并清除，来源资料不受影响。</p>
        <div class="modal-actions">
          <button class="btn btn-outline" @click="wikiPageDeleteTarget = null">取消</button>
          <button class="btn btn-danger" @click="doDeleteWikiPage" :disabled="wikiDeletingPage">
            {{ wikiDeletingPage ? '删除中...' : '确认删除' }}
          </button>
        </div>
      </div>
    </div>

    <!-- ============ 知识条目预览弹窗 ============ -->
    <div v-if="showPreview" class="modal-overlay preview-overlay" @click.self="closePreview">
      <div class="preview-modal">
        <div class="preview-header">
          <h3 class="preview-title">{{ previewFilename }}</h3>
          <div class="preview-meta">
            <span v-if="previewChunkCount" class="preview-meta-item">{{ previewChunkCount }} 个文档片段</span>
            <span v-if="previewDate" class="preview-meta-item">{{ previewDate }}</span>
            <span v-if="previewRelevance !== null" class="preview-meta-item relevance-badge">相关度 {{ previewRelevance }}%</span>
          </div>
          <button class="preview-close" @click="closePreview">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20">
              <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>
        <div class="preview-body">
          <!-- 图片类型：显示来源图片 -->
          <div class="preview-image-section" v-if="previewImageUrl">
            <img :src="previewImageUrl" class="preview-image" alt="来源图片" @click="openImageViewer" />
            <div class="preview-image-hint" @click="openImageViewer">点击查看原图</div>
          </div>
          <!-- 文档类型（Word/PDF）：显示文档工具栏 + 预览内容 -->
          <div class="preview-pdf-section" v-if="previewSourceType === 'word' || previewSourceType === 'pdf'">
            <div class="preview-pdf-header">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="18" height="18">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                <polyline points="14 2 14 8 20 8"/>
              </svg>
              <span>{{ previewSourceType === 'word' ? (previewPdfUrl ? 'Word 原始文档分页预览' : 'Word 文档预览') : 'PDF 原始文档分页预览' }}</span>
              <div class="preview-pdf-actions">
                <!-- 在新标签页打开 PDF -->
                <a v-if="previewPdfUrl" :href="previewPdfUrl" target="_blank" class="preview-pdf-open" title="在浏览器中查看 PDF">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                    <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>
                    <polyline points="15 3 21 3 21 9"/>
                    <line x1="10" y1="14" x2="21" y2="3"/>
                  </svg>
                  查看 PDF
                </a>
                <!-- 下载原始文件 -->
                <a v-if="previewItem && previewItem.source_image" :href="getDocFileUrl(previewItem.source_image)" :download="previewFilename" class="preview-pdf-open" title="下载原始文件">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                    <polyline points="7 10 12 15 17 10"/>
                    <line x1="12" y1="15" x2="12" y2="3"/>
                  </svg>
                  下载原文
                </a>
              </div>
            </div>
            <!-- Word 类型：只展示原始文档，不展示解析文本；
                 已生成 PDF 直接 iframe 分页预览，否则按需转换 -->
            <div v-if="previewSourceType === 'word'">
              <div v-if="wordHtmlLoading" class="preview-doc-loading">
                <span>正在生成原始文档预览...</span>
              </div>
              <iframe v-else-if="previewPdfUrl" :src="previewPdfUrl" class="preview-pdf-frame" :title="previewFilename"></iframe>
              <div v-else class="preview-word-content preview-word-fallback">
                <div v-if="wordHtmlError" class="preview-word-error">{{ wordHtmlError }}</div>
                <div v-else>原始文档预览不可用，可下载原文查看</div>
              </div>
            </div>
            <!-- PDF 类型：直接 iframe 预览原始 PDF，不展示解析文本 -->
            <div v-else-if="previewSourceType === 'pdf'">
              <iframe v-if="previewPdfUrl" :src="previewPdfUrl" class="preview-pdf-frame" :title="previewFilename"></iframe>
              <div v-else class="preview-word-content preview-word-fallback">
                原始 PDF 预览不可用，可下载原文查看
              </div>
            </div>
            <!-- 其他文档类型：分页文本预览 -->
            <template v-else>
              <div class="preview-doc-pages" v-if="previewDocPages.length > 0">
                <div v-for="pg in previewDocPages" :key="pg.page_number" class="preview-doc-page">
                  <div class="preview-doc-page-num">第 {{ pg.page_number }} 页</div>
                  <div class="preview-doc-page-text">{{ pg.text }}</div>
                </div>
              </div>
              <!-- 无分页数据时用完整文本兜底 -->
              <div class="preview-doc-pages" v-else-if="previewFullText">
                <div class="preview-doc-page">
                  <div class="preview-doc-page-num">文档全文</div>
                  <div class="preview-doc-page-text">{{ previewFullText }}</div>
                </div>
              </div>
            </template>
          </div>
          <!-- 图片类型的文本区域（Word/PDF 已在上方分页展示，不重复） -->
          <div class="preview-text-section" v-if="previewSourceType === 'image'">
            <h4 class="preview-text-title">OCR 识别文本</h4>
            <div class="preview-text-content">{{ previewFullText }}</div>
          </div>
        </div>
      </div>
    </div>

    <!-- ============ 图片放大查看器 ============ -->
    <div v-if="showImageViewer" class="image-viewer-overlay" @click.self="closeImageViewer">
      <div class="image-viewer-toolbar">
        <span class="image-viewer-filename">{{ previewFilename }}</span>
        <div class="image-viewer-controls">
          <button class="viewer-btn" @click="zoomOut" title="缩小">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20">
              <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/><line x1="8" y1="11" x2="14" y2="11"/>
            </svg>
          </button>
          <span class="image-viewer-zoom-level">{{ Math.round(imageZoom * 100) }}%</span>
          <button class="viewer-btn" @click="zoomIn" title="放大">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20">
              <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/><line x1="11" y1="8" x2="11" y2="14"/><line x1="8" y1="11" x2="14" y2="11"/>
            </svg>
          </button>
          <button class="viewer-btn" @click="zoomReset" title="重置">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20">
              <polyline points="1 4 1 10 7 10"/><polyline points="23 20 23 14 17 14"/><path d="M20.49 9A9 9 0 0 0 5.64 5.64L1 10m22 4l-4.64 4.36A9 9 0 0 1 3.51 15"/>
            </svg>
          </button>
          <button class="viewer-btn viewer-close-btn" @click="closeImageViewer" title="关闭">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="22" height="22">
              <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>
      </div>
      <div class="image-viewer-body" @wheel.prevent="handleImageWheel">
        <img
          :src="previewImageUrl"
          class="image-viewer-img"
          :style="{ transform: 'scale(' + imageZoom + ')' }"
          alt="原图"
          @click="zoomIn"
        />
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, nextTick, watch } from 'vue'
import { useRoute } from 'vue-router'
import MarkdownIt from 'markdown-it'
import { useAuthStore } from '@/stores/auth'
import UserStatsBoard from '@/views/UserManagementView.vue'
import {
  getSystemConfig,
  switchProvider,
  saveProviderSettings,
  toggleRegistration,
  setGuestLimit,
  testLLMConnection,
  testSearchConnection,
  testOCRConnection,
  testEmbeddingConnection,
  testRerankConnection,
  saveRedisConfig,
  testRedisConnection,
  getWikiConfig,
  saveWikiConfig,
  getWikiPages,
  getWikiPage,
  deleteWikiPage,
  generateWikiDigest,
  recompileWikiAll,
  getOpenRouterModels,
  applyOpenRouterModel,
  getKnowledgeEntries,
  deleteKnowledgeEntriesBySource,
  moveSourceToKb,
  clearKnowledge,
  searchKnowledge,
  getUploadRecords,
  listKnowledgeBases,
  deleteKnowledgeBase,
  listUsers,
  setUserActive,
  deleteUser,
  getImageUrl as buildImageUrl,
  convertWordPdf,
} from '@/api'

const route = useRoute()
const authStore = useAuthStore()
const isAdmin = computed(() => authStore.isAdmin)
const isGuest = computed(() => authStore.isGuest)
const isManagementRoute = computed(() => route.name === 'Management')

// ---- Tab state ----
const activeTab = ref('settings')

// ---- Init tab based on route ----
onMounted(() => {
  if (route.name === 'KnowledgeBase') {
    activeTab.value = 'knowledge'
  } else if (!isAdmin.value) {
    // 非管理员访问 /management => 跳转到知识库标签
    activeTab.value = 'knowledge'
  }
  // load everything
  if (isAdmin.value) {
    loadConfig()
    loadWikiSettings()
    loadWikiPages()
  }
  loadEntries()
  loadKbList()
  loadFullTexts()
  if (isAdmin.value) loadUsers()
})

// ---- Config state ----
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
  embedding: '',
  rerank: '',
})

// LLM test state
const llmTesting = ref(false)
const llmTestResult = ref(null)
const llmTestStatus = ref('')

// Search test state
const searchTesting = ref(false)
const searchTestResult = ref(null)
const searchTestStatus = ref('')

// OCR test state
const ocrTesting = ref(false)
const ocrTestResult = ref(null)

// Embedding test state
const embeddingTesting = ref(false)
const embeddingTestResult = ref(null)

// Rerank test state
const rerankTesting = ref(false)
const rerankTestResult = ref(null)

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
  anysearch: 'AnySearch',
}

const searchDescs = {
  duckduckgo: '免费，无需配置',
  bing: '需要 Azure API Key',
  serpapi: '需要 SerpAPI Key',
  anysearch: '统一实时搜索，Key 可选（匿名可用，限额较低）',
}

const embeddingLabels = {
  ollama: 'Ollama 本地',
  openai: 'OpenAI 兼容 API',
  custom: '自定义 API',
}

const embeddingDescs = {
  ollama: '本地运行，无需网络',
  openai: '百炼 / OpenAI 等在线向量服务',
  custom: '兼容 OpenAI 接口的任意向量服务',
}

const rerankLabels = {
  local: '本地模型 (CrossEncoder)',
  dashscope: '阿里云 DashScope',
}

const rerankDescs = {
  local: '离线可用，CPU 推理较慢（首次下载 ~2.2GB）',
  dashscope: 'OpenAI 兼容 /reranks 在线 API（qwen3-rerank 等），需要 API Key',
}

// ---- 参数在线配置 ----
// 各类别 × 提供商可编辑的字段定义（与后端 EDITABLE_FIELDS 白名单一致）
const paramFieldDefs = {
  llm: {
    ollama: [
      { key: 'base_url', label: '服务地址' },
      { key: 'model', label: '模型名称' },
    ],
    openai: [
      { key: 'base_url', label: 'API 地址' },
      { key: 'model', label: '模型名称' },
      { key: 'api_key', label: 'API Key', secret: true },
    ],
    custom: [
      { key: 'base_url', label: 'API 地址' },
      { key: 'model', label: '模型名称' },
      { key: 'api_key', label: 'API Key', secret: true },
    ],
  },
  ocr: {
    local: [],
    aliyun: [
      { key: 'base_url', label: 'API 地址' },
      { key: 'model', label: '模型名称' },
      { key: 'api_key', label: 'API Key', secret: true },
    ],
    custom_api: [
      { key: 'api_url', label: 'OCR API 地址' },
      { key: 'api_key', label: 'API Key', secret: true },
    ],
  },
  search: {
    duckduckgo: [],
    bing: [{ key: 'api_key', label: 'API Key', secret: true }],
    serpapi: [{ key: 'api_key', label: 'API Key', secret: true }],
    anysearch: [{ key: 'api_key', label: 'API Key', secret: true, optional: true }],
  },
  embedding: {
    ollama: [
      { key: 'base_url', label: '服务地址' },
      { key: 'model', label: '模型名称' },
    ],
    openai: [
      { key: 'base_url', label: 'API 地址' },
      { key: 'model', label: '模型名称' },
      { key: 'api_key', label: 'API Key', secret: true },
    ],
    custom: [
      { key: 'base_url', label: 'API 地址' },
      { key: 'model', label: '模型名称' },
      { key: 'api_key', label: 'API Key', secret: true },
    ],
  },
  rerank: {
    local: [{ key: 'model', label: '模型名称' }],
    dashscope: [
      { key: 'base_url', label: 'API 地址' },
      { key: 'model', label: '模型名称' },
      { key: 'api_key', label: 'API Key', secret: true },
    ],
  },
}

// 各类别当前编辑中的表单值
const paramForms = reactive({ llm: {}, ocr: {}, search: {}, embedding: {}, rerank: {} })
const savingCategory = ref('')
const saveResults = reactive({ llm: null, ocr: null, search: null, embedding: null, rerank: null })

// ---- Redis 文件缓存配置 ----
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

// ---- llm-wiki 知识编译 ----
const wikiMd = new MarkdownIt({ html: false, linkify: true, breaks: true })
const wikiForm = reactive({ compile_enabled: true, priority_enabled: true })
const wikiEnabled = computed(() => wikiForm.compile_enabled)
const wikiSaving = ref(false)
const wikiRecompiling = ref(false)
const wikiMsg = ref(null)
const wikiDigestQuery = ref('')
const wikiDigesting = ref(false)
const wikiPages = ref([])
const wikiPagesLoading = ref(false)
const wikiTypeFilter = ref('')
const wikiKbFilter = ref('')
const wikiDetailPage = ref(null)
const wikiPageDeleteTarget = ref(null)
const wikiDeletingPage = ref(false)

const renderedWikiPage = computed(() =>
  wikiDetailPage.value ? wikiMd.render(wikiDetailPage.value.content || '') : ''
)

// 删除权限与后端一致：私人页面仅属主/管理员；公共来源卡片仅管理员
function canDeleteWikiPage(p) {
  if (isAdmin.value) return true
  if (p.visibility === 'private') return p.owner_id === authStore.user?.id
  return p.page_type === 'digest'
}

// 各类别在后端配置中的字段来源
const categoryConfigKey = {
  llm: 'llm_config',
  ocr: 'ocr_config',
  search: 'search_config',
  embedding: 'embedding_config',
  rerank: 'rerank_config',
}

function currentParamFields(category) {
  const provider = activeConfig[category]
  return (paramFieldDefs[category] || {})[provider] || []
}

function currentProviderConfig(category) {
  const provider = activeConfig[category]
  return config.value[categoryConfigKey[category]]?.[provider] || {}
}

function paramPlaceholder(category, field) {
  if (field.secret) {
    if (currentProviderConfig(category).has_key) return '已配置，留空保持不变'
    return field.optional ? '可选，留空则匿名访问' : '未配置，请输入 API Key'
  }
  return '留空使用 .env 默认值'
}

// 从后端配置填充表单（secret 字段不回显明文，置空）
function fillParamForm(category) {
  const cfg = currentProviderConfig(category)
  const form = {}
  for (const f of currentParamFields(category)) {
    form[f.key] = f.secret ? '' : (cfg[f.key] || '')
  }
  paramForms[category] = form
}

function fillAllParamForms() {
  for (const category of Object.keys(paramFieldDefs)) {
    fillParamForm(category)
  }
}

async function saveParams(category) {
  const provider = activeConfig[category]
  if (!provider) return
  savingCategory.value = category
  saveResults[category] = null
  try {
    const values = {}
    for (const f of currentParamFields(category)) {
      values[f.key] = (paramForms[category][f.key] || '').trim()
    }
    const res = await saveProviderSettings(category, provider, values)
    saveResults[category] = res.data
    // 重新拉取配置并重填表单（同步 has_key 等状态）
    const cfgRes = await getSystemConfig()
    config.value = cfgRes.data
    fillParamForm(category)
  } catch (err) {
    saveResults[category] = {
      success: false,
      message: '保存失败: ' + (err.response?.data?.detail || err.message),
    }
  } finally {
    savingCategory.value = ''
  }
}

async function testOCR() {
  ocrTesting.value = true
  ocrTestResult.value = null
  try {
    const res = await testOCRConnection(activeConfig.ocr)
    ocrTestResult.value = res.data
  } catch (err) {
    ocrTestResult.value = { success: false, message: '请求失败: ' + (err.response?.data?.detail || err.message) }
  } finally {
    ocrTesting.value = false
  }
}

async function loadConfig() {
  loading.value = true
  try {
    const res = await getSystemConfig()
    config.value = res.data
    guestLimitInput.value = res.data.guest_query_limit ?? 3
    activeConfig.llm = res.data.llm_provider
    activeConfig.ocr = res.data.ocr_provider
    activeConfig.search = res.data.search_provider
    activeConfig.embedding = res.data.embedding_provider
    activeConfig.rerank = res.data.rerank_provider
    // 回填 Redis 配置（页面保存过的覆盖值优先，否则显示 .env 默认）
    redisForm.url = res.data.redis_config?.url || ''
    redisForm.enabled = res.data.redis_config?.enabled !== false
    fillAllParamForms()
    loadOpenRouterModels()
  } catch (err) {
    console.error('加载配置失败:', err)
  } finally {
    loading.value = false
  }
}

async function saveRedis() {
  redisSaving.value = true
  redisTestResult.value = null
  try {
    const res = await saveRedisConfig({ url: redisForm.url.trim(), enabled: redisForm.enabled })
    if (res.data.success) {
      // 静默刷新配置，同步状态徽标（不触发全局 loading）
      const cfgRes = await getSystemConfig()
      config.value = cfgRes.data
      redisForm.url = cfgRes.data.redis_config?.url || ''
      redisForm.enabled = cfgRes.data.redis_config?.enabled !== false
      redisTestResult.value = { success: true, message: '配置已保存并立即生效' }
    } else {
      redisTestResult.value = { success: false, message: res.data.message || '保存失败' }
    }
  } catch (err) {
    redisTestResult.value = { success: false, message: '请求失败: ' + (err.response?.data?.detail || err.message) }
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
      const cfgRes = await getSystemConfig()
      config.value = cfgRes.data
    }
  } catch (err) {
    redisTestResult.value = { success: false, message: '请求失败: ' + (err.response?.data?.detail || err.message) }
  } finally {
    redisTesting.value = false
  }
}

// ---- llm-wiki 知识编译操作 ----
async function loadWikiSettings() {
  try {
    const res = await getWikiConfig()
    wikiForm.compile_enabled = res.data.compile_enabled !== false
    wikiForm.priority_enabled = res.data.priority_enabled !== false
  } catch (err) {
    console.error('加载知识编译配置失败:', err)
  }
}

async function loadWikiPages() {
  wikiPagesLoading.value = true
  try {
    const params = {}
    if (wikiTypeFilter.value) params.page_type = wikiTypeFilter.value
    if (wikiKbFilter.value) params.kb_id = wikiKbFilter.value
    const res = await getWikiPages(params)
    wikiPages.value = res.data.pages || []
  } catch (err) {
    console.error('加载百科列表失败:', err)
  } finally {
    wikiPagesLoading.value = false
  }
}

async function saveWikiSettings() {
  wikiSaving.value = true
  wikiMsg.value = null
  try {
    const res = await saveWikiConfig({
      compile_enabled: wikiForm.compile_enabled,
      priority_enabled: wikiForm.priority_enabled,
    })
    wikiMsg.value = { success: !!res.data.success, message: res.data.success ? '配置已保存并立即生效' : (res.data.detail || '保存失败') }
    await loadWikiSettings()
  } catch (err) {
    wikiMsg.value = { success: false, message: '保存失败: ' + (err.response?.data?.detail || err.message) }
  } finally {
    wikiSaving.value = false
  }
}

async function doRecompileAll() {
  wikiRecompiling.value = true
  wikiMsg.value = null
  try {
    const res = await recompileWikiAll()
    const n = res.data.scheduled ?? 0
    wikiMsg.value = { success: true, message: `已调度 ${n} 条存量资料后台编译，稍后刷新查看` }
    // 后台编译需 LLM 逐条处理，轮询刷新列表直至数量稳定或超时
    let last = -1
    for (let i = 0; i < 12; i++) {
      await new Promise(r => setTimeout(r, 5000))
      await loadWikiPages()
      const cur = wikiPages.value.length
      if (cur === last && cur > 0) break
      last = cur
    }
    wikiMsg.value = { success: true, message: `存量补编译结束，当前 ${wikiPages.value.length} 则百科` }
  } catch (err) {
    wikiMsg.value = { success: false, message: '调度失败: ' + (err.response?.data?.detail || err.message) }
  } finally {
    wikiRecompiling.value = false
  }
}

async function doDigest() {
  const q = wikiDigestQuery.value.trim()
  if (!q) return
  wikiDigesting.value = true
  wikiMsg.value = null
  try {
    const res = await generateWikiDigest(q)
    if (res.data.success) {
      wikiMsg.value = { success: true, message: `综合词条已生成：${res.data.page?.title || q}` }
      wikiDigestQuery.value = ''
      await loadWikiPages()
      if (res.data.page) wikiDetailPage.value = res.data.page
    } else {
      wikiMsg.value = { success: false, message: res.data.detail || '生成失败' }
    }
  } catch (err) {
    wikiMsg.value = { success: false, message: '生成失败: ' + (err.response?.data?.detail || err.message) }
  } finally {
    wikiDigesting.value = false
  }
}

async function openWikiPage(pageId) {
  try {
    const res = await getWikiPage(pageId)
    if (res.data.success) wikiDetailPage.value = res.data.page
  } catch (err) {
    wikiMsg.value = { success: false, message: '加载详情失败: ' + (err.response?.data?.detail || err.message) }
  }
}

async function doDeleteWikiPage() {
  if (!wikiPageDeleteTarget.value) return
  wikiDeletingPage.value = true
  try {
    await deleteWikiPage(wikiPageDeleteTarget.value.id)
    wikiPages.value = wikiPages.value.filter(p => p.id !== wikiPageDeleteTarget.value.id)
    if (wikiDetailPage.value?.id === wikiPageDeleteTarget.value.id) wikiDetailPage.value = null
    wikiPageDeleteTarget.value = null
    wikiMsg.value = { success: true, message: '百科词条已删除' }
  } catch (err) {
    wikiMsg.value = { success: false, message: '删除失败: ' + (err.response?.data?.detail || err.message) }
  } finally {
    wikiDeletingPage.value = false
  }
}

// ============================================
// OpenRouter 免费模型
// ============================================
const openrouterSearch = ref('')
const openrouterModels = ref([])
const openrouterTotal = ref(0)
const openrouterLoading = ref(false)
const openrouterLoaded = ref(false)
const openrouterError = ref('')
const openrouterApiKey = ref('')
const openrouterResult = ref(null)
const applyingModel = ref('')
let openrouterDebounce = null

// 当前 custom 提供商已配置的模型（用于高亮“当前使用中”）
const currentCustomModel = computed(() => config.value?.llm_config?.custom?.model || '')

function formatCtx(len) {
  if (!len) return '-'
  if (len >= 1000) return `${(len / 1000).toFixed(len % 1000 === 0 ? 0 : 1)}K`
  return String(len)
}

async function loadOpenRouterModels(refresh = false) {
  openrouterLoading.value = true
  openrouterError.value = ''
  try {
    const res = await getOpenRouterModels(openrouterSearch.value.trim(), refresh)
    openrouterModels.value = res.data.models || []
    openrouterTotal.value = res.data.total || 0
    openrouterLoaded.value = true
    if (!res.data.success && res.data.message) {
      openrouterError.value = res.data.message
    }
  } catch (err) {
    openrouterError.value = err.response?.data?.message || '加载模型列表失败'
    openrouterModels.value = []
    openrouterLoaded.value = true
  } finally {
    openrouterLoading.value = false
  }
}

// 实时搜索：输入防抖 400ms 后自动拉取
watch(openrouterSearch, () => {
  if (openrouterDebounce) clearTimeout(openrouterDebounce)
  openrouterDebounce = setTimeout(() => loadOpenRouterModels(), 400)
})

async function applyOpenRouter(model) {
  applyingModel.value = model.id
  openrouterResult.value = null
  try {
    const res = await applyOpenRouterModel(model.id, openrouterApiKey.value)
    openrouterResult.value = res.data
    if (res.data.success) {
      // 同步刷新当前配置展示
      activeConfig.llm = 'custom'
      await loadConfig()
    }
  } catch (err) {
    openrouterResult.value = { success: false, message: err.response?.data?.message || '应用模型失败' }
  } finally {
    applyingModel.value = ''
  }
}

async function handleProviderChange(type, value) {
  try {
    await switchProvider(type, value)
    activeConfig[type] = value
    // 切换提供商后重填对应参数表单，并清空上次保存/测试提示
    fillParamForm(type)
    saveResults[type] = null
    if (type === 'ocr') ocrTestResult.value = null
    if (type === 'rerank') rerankTestResult.value = null
  } catch (err) {
    console.error(`切换 ${type} 提供商失败:`, err)
    alert('切换失败: ' + (err.response?.data?.detail || err.message))
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

async function testRerank() {
  rerankTesting.value = true
  rerankTestResult.value = null
  try {
    const res = await testRerankConnection(activeConfig.rerank)
    rerankTestResult.value = res.data
  } catch (err) {
    rerankTestResult.value = { success: false, message: '请求失败: ' + (err.response?.data?.detail || err.message) }
  } finally {
    rerankTesting.value = false
  }
}

// ---- Registration toggle ----
const registrationEnabled = computed(() => config.value.registration_enabled !== false)
const togglingRegistration = ref(false)

// ---- 游客免费访问次数 ----
const usersSubTab = ref('accounts') // 'accounts' | 'stats'
const guestLimitInput = ref(3)
const savingGuestLimit = ref(false)

async function handleSaveGuestLimit() {
  const limit = Number(guestLimitInput.value)
  if (!Number.isInteger(limit) || limit < 0) {
    alert('请输入不小于 0 的整数')
    return
  }
  savingGuestLimit.value = true
  try {
    await setGuestLimit(limit)
    config.value.guest_query_limit = limit
  } catch (err) {
    alert('保存失败: ' + (err.response?.data?.detail || err.message))
  } finally {
    savingGuestLimit.value = false
  }
}

async function handleToggleRegistration() {
  togglingRegistration.value = true
  const newValue = !registrationEnabled.value
  try {
    await toggleRegistration(newValue)
    config.value.registration_enabled = newValue
  } catch (err) {
    console.error('切换注册状态失败:', err)
    alert('操作失败: ' + (err.response?.data?.detail || err.message))
  } finally {
    togglingRegistration.value = false
  }
}

// ---- Knowledge entries state ----
const entries = ref([])
const loadingEntries = ref(false)
const totalChunks = computed(() => entries.value.reduce((sum, e) => sum + e.chunk_count, 0))

// Sort
const sortOrder = ref('desc') // 'desc' | 'asc'

function toggleSort() {
  sortOrder.value = sortOrder.value === 'desc' ? 'asc' : 'desc'
  applySort()
}

function applySort() {
  entries.value.sort((a, b) => {
    const dateA = new Date(a.created_at || 0).getTime()
    const dateB = new Date(b.created_at || 0).getTime()
    return sortOrder.value === 'desc' ? dateB - dateA : dateA - dateB
  })
}

// Search
const searchQuery = ref('')
const searchResults = ref([])
const chunkResults = ref([])          // 语义片段列表
const titleSearchResults = ref([])    // 标题命中的文件列表
const expandedChunkKey = ref(null)    // 当前展开的片段 key
const isSearching = ref(false)
// isSearchMode: 已按 Enter 触发过语义内容检索
const isSearchMode = ref(false)

function toggleChunk(key) {
  expandedChunkKey.value = expandedChunkKey.value === key ? null : key
}

// 片段相关度样式
function chunkRelevanceClass(score) {
  if (score >= 80) return 'score-high'
  if (score >= 60) return 'score-medium'
  return 'score-low'
}

// 文档片段来源 URL
function getChunkSourceUrl(chunk) {
  if (!chunk.source_image) return null
  const token = localStorage.getItem('auth_token')
  const filename = chunk.source_image.split('/').pop()
  const baseUrl = `/uploads/${encodeURIComponent(filename)}${token ? `?token=${encodeURIComponent(token)}` : ''}`
  if (chunk.source_type === 'pdf') {
    const pdfPath = chunk.pdf_preview_path || chunk.source_image
    const pdfFile = pdfPath.split('/').pop()
    const pdfUrl = `/uploads/${encodeURIComponent(pdfFile)}${token ? `?token=${encodeURIComponent(token)}` : ''}`
    return chunk.page_number ? `${pdfUrl}#page=${chunk.page_number}` : pdfUrl
  }
  if (chunk.source_type === 'word') {
    if (chunk.pdf_preview_path) {
      const pdfFile = chunk.pdf_preview_path.split('/').pop()
      const page = chunk.page_number ? `&page=${chunk.page_number}` : ''
      const pdf = `&pdf=${encodeURIComponent(pdfFile)}`
      return `/doc-preview?file=${encodeURIComponent(filename)}${page}${pdf}`
    }
    return baseUrl
  }
  return null
}

// 标题本地模糊匹配（字符级，命中率 >= 60%）
function titleFuzzyMatch(keyword, title, filename) {
  const kw = keyword.toLowerCase()
  const t = (title || '').toLowerCase()
  const f = (filename || '').toLowerCase()
  if (kw.length === 0) return true
  // 精确子串
  if (t.includes(kw) || f.includes(kw)) return true
  // 字符级模糊（至少 2 字）
  const kwClean = kw.replace(/\s/g, '')
  if (kwClean.length < 2) return false
  const hit = [...kwClean].filter(ch => t.includes(ch) || f.includes(ch)).length
  return hit / kwClean.length >= 0.6
}

async function doSearch() {
  const q = searchQuery.value.trim()
  if (!q) return
  isSearchMode.value = true
  isSearching.value = true
  searchResults.value = []
  chunkResults.value = []
  titleSearchResults.value = []
  try {
    // 并行发起语义内容搜索 + 标题/文件名搜索
    const [contentRes, titleRes] = await Promise.all([
      searchKnowledge(q, 20),
      getKnowledgeEntries(q),
    ])

    // 语义片段：每个 chunk 独立展示，不合并去重
    if (contentRes.data.success) {
      chunkResults.value = (contentRes.data.results || []).map((r, i) => ({
        ...r,
        _key: 'chunk_' + i + '_' + r.source_image,
        _expanded: false,
      }))
    }

    // 标题命中：展示为普通文件卡片（语义片段里已有的来源不重复展示）
    if (titleRes.data.entries) {
      const chunkSources = new Set(chunkResults.value.map(r => r.source_image))
      titleSearchResults.value = titleRes.data.entries
        .filter(e => !chunkSources.has(e.source_image))
        .map(e => ({ ...e, _matchType: 'title', _key: 'title_' + e.source_image }))
    }

    searchResults.value = [...chunkResults.value, ...titleSearchResults.value]
  } catch (err) {
    console.error('搜索失败:', err)
  } finally {
    isSearching.value = false
  }
}

function clearSearch() {
  searchQuery.value = ''
  searchResults.value = []
  chunkResults.value = []
  titleSearchResults.value = []
  expandedChunkKey.value = null
  isSearchMode.value = false
}

// Display entries: 三种状态
// 1. 语义搜索模式（已按 Enter）→ 展示 searchResults
// 2. 实时标题过滤模式（输入中）→ 对 entries 本地过滤
// 3. 默认模式 → 全量 entries，受 KB 筛选
const displayEntries = computed(() => {
  // 状态 1：语义搜索模式
  if (isSearchMode.value) {
    return searchResults.value
  }

  let list = entries.value

  // 状态 2：实时标题过滤
  const q = searchQuery.value.trim()
  if (q) {
    list = list.filter(e => titleFuzzyMatch(q, e.title, e.filename))
  }

  // KB 来源筛选
  if (kbFilterVisibility.value) {
    const visibleKbIds = new Set(filteredFilterKbList.value.map((kb) => kb.id))
    list = list.filter((e) =>
      e.kb_id ? visibleKbIds.has(e.kb_id) : e.visibility === kbFilterVisibility.value
    )
  }
  if (filterKbId.value) {
    return list.filter((e) => e.kb_id === filterKbId.value)
  }
  return list
})

// ---- 知识库筛选/管理 ----
const kbList = ref([])
const filterKbId = ref('')
// 先按公共/个人筛选，再按名称选择知识库
const kbFilterVisibility = ref('')

const filteredFilterKbList = computed(() =>
  kbFilterVisibility.value
    ? kbList.value.filter((kb) => kb.visibility === kbFilterVisibility.value)
    : kbList.value
)

// 切换来源筛选：当前选中知识库不在结果中时重置为全部
watch(kbFilterVisibility, () => {
  if (filterKbId.value && !filteredFilterKbList.value.some((kb) => kb.id === filterKbId.value)) {
    filterKbId.value = ''
  }
})
const showKbPanel = ref(false)
const kbDeleteTarget = ref(null)
const deletingKb = ref(false)
const currentUserId = computed(() => authStore.user?.id || '')

async function loadKbList() {
  try {
    const res = await listKnowledgeBases()
    kbList.value = res.data.bases || []
  } catch (e) {
    console.error('加载知识库列表失败:', e)
  }
}

function canDeleteKb(kb) {
  if (isGuest.value) return false
  if (kb.visibility === 'public') {
    // 公共库仅管理员可删，默认库不可删
    return isAdmin.value && kb.id !== 'kb-default-public'
  }
  return kb.owner_id === currentUserId.value
}

function onDeleteKb(kb) {
  kbDeleteTarget.value = kb
}

async function doDeleteKb() {
  const kb = kbDeleteTarget.value
  if (!kb) return
  deletingKb.value = true
  try {
    await deleteKnowledgeBase(kb.id)
    kbDeleteTarget.value = null
    if (filterKbId.value === kb.id) filterKbId.value = ''
    await Promise.all([loadKbList(), loadEntries()])
  } catch (e) {
    alert('删除知识库失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    deletingKb.value = false
  }
}

// Delete confirmation
const showDeleteConfirm = ref(false)
const deleteTarget = ref(null)
const deleting = ref(false)

// Clear confirmation
const showClearConfirm = ref(false)
const clearing = ref(false)

async function loadEntries() {
  loadingEntries.value = true
  try {
    const res = await getKnowledgeEntries()
    entries.value = res.data.entries || []
    applySort()
  } catch (err) {
    console.error('加载知识库条目失败:', err)
    entries.value = []
  } finally {
    loadingEntries.value = false
  }
}

function onDeleteEntry(entry) {
  // 如果是搜索结果，找到对应的原始 entry
  if (entry._isSearchResult) {
    const orig = entries.value.find(e => e.source_image === entry.source_image)
    if (orig) {
      deleteTarget.value = orig
    } else {
      deleteTarget.value = {
        source_image: entry.source_image,
        filename: entry.filename || '未知',
        chunk_count: entry.chunk_count || 0,
      }
    }
  } else {
    deleteTarget.value = entry
  }
  showDeleteConfirm.value = true
}

async function doDeleteEntry() {
  if (!deleteTarget.value) return
  deleting.value = true
  try {
    await deleteKnowledgeEntriesBySource(deleteTarget.value.source_image)
    entries.value = entries.value.filter(e => e.source_image !== deleteTarget.value.source_image)
    // 也清关联的搜索结果
    searchResults.value = searchResults.value.filter(r => r.source_image !== deleteTarget.value.source_image)
    showDeleteConfirm.value = false
    deleteTarget.value = null
  } catch (err) {
    console.error('删除失败:', err)
    alert('删除失败: ' + (err.response?.data?.detail || err.message))
  } finally {
    deleting.value = false
  }
}

// Move knowledge base (only available when the management panel is expanded)
const moveTarget = ref(null)
const moveTargetKbId = ref('')
const moving = ref(false)

// Target knowledge base options: exclude the knowledge base the document currently belongs to
const moveKbOptions = computed(() =>
  kbList.value.filter((kb) => kb.id !== moveTarget.value?.kb_id)
)

function onMoveEntry(entry) {
  moveTarget.value = entry
  moveTargetKbId.value = ''
}

async function doMoveEntry() {
  if (!moveTarget.value || !moveTargetKbId.value) return
  moving.value = true
  try {
    await moveSourceToKb(moveTarget.value.source_image, moveTargetKbId.value)
    moveTarget.value = null
    await Promise.all([loadKbList(), loadEntries()])
  } catch (err) {
    console.error('移动失败:', err)
    alert('移动失败: ' + (err.response?.data?.detail || err.message))
  } finally {
    moving.value = false
  }
}

function confirmClearAll() {
  showClearConfirm.value = true
}

async function doClearAll() {
  clearing.value = true
  try {
    await clearKnowledge()
    entries.value = []
    searchResults.value = []
    showClearConfirm.value = false
  } catch (err) {
    console.error('清空失败:', err)
    alert('清空失败: ' + (err.response?.data?.detail || err.message))
  } finally {
    clearing.value = false
  }
}

// Full text for preview
const fullTextMap = ref({})

async function loadFullTexts() {
  try {
    const res = await getUploadRecords(10000, 0)
    const records = res.data.records || []
    const map = {}
    records.forEach(r => {
      if (r.image_path) {
        map[r.image_path] = r.ocr_text || ''
      }
    })
    fullTextMap.value = map
  } catch (err) {
    console.error('加载完整文本失败:', err)
  }
}

// ---- Preview modal ----
const showPreview = ref(false)
const previewItem = ref(null)
const previewFullText = ref('')
const previewImageUrl = ref('')
const previewFilename = ref('')
const previewChunkCount = ref('')
const previewDate = ref('')
const previewRelevance = ref(null)
const previewPdfUrl = ref('')
const previewSourceType = ref('image')
const previewDocPages = ref([])  // Word/PDF 分页内容
const wordHtmlLoading = ref(false)
const wordHtmlError = ref('')

function getImageUrl(sourceImage) {
  // 图片接口需鉴权，统一走 api 层 helper（自动附带 token）
  return buildImageUrl(sourceImage)
}

/**
 * 构造 PDF 预览 URL（与图片相同，走 /uploads 静态目录 + token 鉴权）
 */
function getPdfUrl(pdfPreviewPath) {
  if (!pdfPreviewPath) return ''
  const filename = pdfPreviewPath.split('/').pop()
  const token = localStorage.getItem('auth_token')
  return `/uploads/${encodeURIComponent(filename)}${token ? `?token=${encodeURIComponent(token)}` : ''}`
}

/**
 * 构造原始文档文件下载 URL（docx/pdf 原文件）
 */
function getDocFileUrl(sourceImage) {
  if (!sourceImage) return ''
  const filename = sourceImage.split('/').pop()
  const token = localStorage.getItem('auth_token')
  return `/uploads/${encodeURIComponent(filename)}${token ? `?token=${encodeURIComponent(token)}` : ''}`
}

/**
 * 为已有 Word 条目按需转换为 PDF，并在 iframe 中分页预览原始文档。
 */
async function convertWordPdfAndPreview(entry) {
  if (!entry?.source_image) return
  wordHtmlLoading.value = true
  wordHtmlError.value = ''
  try {
    const res = await convertWordPdf(entry.source_image)
    const pdfPath = res.data?.pdf_preview_path
    if (pdfPath) {
      entry.pdf_preview_path = pdfPath
      previewPdfUrl.value = getPdfUrl(pdfPath)
    } else {
      wordHtmlError.value = '未能生成 PDF 预览'
    }
  } catch (err) {
    console.error('Word 转 PDF 预览失败:', err)
    wordHtmlError.value = err.response?.data?.detail || String(err)
  } finally {
    wordHtmlLoading.value = false
  }
}

function onThumbError(e) {
  e.target.style.display = 'none'
}

function openPreview(entry) {
  previewItem.value = entry
  previewFilename.value = entry.title || entry.filename || '知识条目'
  previewChunkCount.value = entry.chunk_count || ''
  previewRelevance.value = entry.relevance !== undefined ? entry.relevance : null
  previewSourceType.value = entry.source_type || 'image'

  const dateStr = entry.created_at || ''
  previewDate.value = dateStr ? formatDate(dateStr) : ''

  // Full text
  if (entry._isSearchResult) {
    previewFullText.value = entry.text || ''
  } else {
    previewFullText.value = fullTextMap.value[entry.source_image] || entry.text_preview || ''
  }

  const isDocument = entry.source_type === 'word' || entry.source_type === 'pdf'

  // Word 类型：只展示原始文档（PDF 分页预览），不展示解析文本；
  // 已有 pdf_preview_path 直接预览，否则按需调用后端转换
  if (entry.source_type === 'word') {
    previewDocPages.value = []
    wordHtmlError.value = ''
    previewPdfUrl.value = entry.pdf_preview_path ? getPdfUrl(entry.pdf_preview_path) : ''
    if (!entry.pdf_preview_path) {
      convertWordPdfAndPreview(entry)
    }
  } else if (entry.source_type === 'pdf') {
    // PDF 类型：只展示原始 PDF，不展示解析文本
    previewDocPages.value = []
  } else if (isDocument && entry.pages && entry.pages.length > 0) {
    previewDocPages.value = entry.pages
  } else {
    previewDocPages.value = []
  }

  // URL 设置：PDF 原件 / Word 转换的原始排版 PDF 可分页预览，其余 Word 只提供下载链接
  if (entry.source_type === 'pdf') {
    // PDF 直接预览原始上传文件
    previewPdfUrl.value = getPdfUrl(entry.source_image)
    previewImageUrl.value = ''
  } else if (entry.source_type === 'word' && entry.pdf_preview_path) {
    previewPdfUrl.value = getPdfUrl(entry.pdf_preview_path)
    previewImageUrl.value = ''
  } else {
    previewPdfUrl.value = ''
    previewImageUrl.value = isDocument ? '' : getImageUrl(entry.source_image)
  }
  showPreview.value = true
}

function closePreview() {
  showPreview.value = false
  previewItem.value = null
}

// ---- Image viewer (zoom) ----
const showImageViewer = ref(false)
const imageZoom = ref(1)

function openImageViewer() {
  imageZoom.value = 1
  showImageViewer.value = true
}

// 从片段卡片直接打开图片放大查看器
function openImageFromChunk(chunk) {
  if (!chunk.source_image) return
  previewImageUrl.value = getImageUrl(chunk.source_image)
  imageZoom.value = 1
  showImageViewer.value = true
}

function closeImageViewer() {
  showImageViewer.value = false
  imageZoom.value = 1
}

function zoomIn() {
  imageZoom.value = Math.min(imageZoom.value + 0.25, 5)
}

function zoomOut() {
  imageZoom.value = Math.max(imageZoom.value - 0.25, 0.25)
}

function zoomReset() {
  imageZoom.value = 1
}

function handleImageWheel(e) {
  if (e.deltaY < 0) {
    imageZoom.value = Math.min(imageZoom.value + 0.1, 5)
  } else {
    imageZoom.value = Math.max(imageZoom.value - 0.1, 0.25)
  }
}

function formatDate(dateStr) {
  if (!dateStr) return ''
  try {
    const d = new Date(dateStr)
    return d.toLocaleDateString('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return dateStr
  }
}

// ---- User management (embedded in tab) ----
const users = ref([])
const usersLoading = ref(false)
const userDeleting = ref(false)
const showUserDeleteConfirm = ref(false)
const userDeleteTarget = ref(null)

async function loadUsers() {
  usersLoading.value = true
  try {
    const res = await listUsers()
    users.value = res.data.users || []
  } catch (err) {
    console.error('加载用户列表失败:', err)
    users.value = []
  } finally {
    usersLoading.value = false
  }
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

function confirmDeleteUser(u) {
  userDeleteTarget.value = u
  showUserDeleteConfirm.value = true
}

async function doDeleteUser() {
  if (!userDeleteTarget.value) return
  userDeleting.value = true
  try {
    await deleteUser(userDeleteTarget.value.id)
    users.value = users.value.filter(x => x.id !== userDeleteTarget.value.id)
    showUserDeleteConfirm.value = false
    userDeleteTarget.value = null
  } catch (err) {
    alert('删除失败: ' + (err.response?.data?.detail || err.message))
  } finally {
    userDeleting.value = false
  }
}

function formatUserDate(ts) {
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
</script>

<style scoped>
.management-view {
  height: 100vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}

/* Tabs */
.tabs-header {
  position: sticky;
  top: 0;
  z-index: 10;
  background: var(--color-bg);
  border-bottom: 1px solid var(--color-border);
  padding: 0 40px;
}

.tabs-nav {
  display: flex;
  gap: 4px;
  padding-top: 16px;
}

.tab-btn {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 20px;
  border: none;
  background: transparent;
  font-size: 14px;
  font-weight: 500;
  color: var(--color-text-secondary);
  cursor: pointer;
  border-radius: 10px 10px 0 0;
  transition: all 0.2s;
}

.tab-btn:hover {
  color: var(--color-text);
  background: #f3efe9;
}

.tab-btn.active {
  color: var(--color-primary);
  background: white;
  box-shadow: 0 -1px 3px rgba(0,0,0,0.05);
}

.tab-content {
  flex: 1;
  overflow-y: auto;
}

/* Settings tab */
.tab-settings {
  padding: 32px 40px;
  max-width: 900px;
  margin: 0 auto;
}

/* Users tab */
.tab-users {
  padding: 32px 40px;
  max-width: 1000px;
  margin: 0 auto;
}

.tab-users-inner {
  max-width: 100%;
}

.users-header {
  margin-bottom: 24px;
}

.users-title {
  font-size: 20px;
  font-weight: 700;
  color: var(--color-text);
  margin-bottom: 4px;
}

.users-desc {
  font-size: 14px;
  color: var(--color-text-secondary);
}

.registration-section {
  margin-bottom: 20px;
}

/* Knowledge tab */
.tab-knowledge {
  padding: 24px 40px;
  max-width: 1100px;
  margin: 0 auto;
}

/* Toolbar */
.knowledge-toolbar {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 16px;
  flex-wrap: wrap;
  gap: 12px;
}

.knowledge-stats {
  font-size: 14px;
  color: var(--color-text-secondary);
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.knowledge-stats strong {
  color: var(--color-text);
}

.stat-divider {
  color: var(--color-border);
}

.toolbar-right {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  flex: 1;
  justify-content: flex-end;
}

/* Search box */
.search-box {
  position: relative;
  display: flex;
  align-items: center;
  min-width: 240px;
}

.search-icon {
  position: absolute;
  left: 12px;
  color: var(--color-text-secondary);
  pointer-events: none;
}

.search-input {
  width: 100%;
  padding: 8px 36px 8px 36px;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  font-size: 13px;
  color: var(--color-text);
  background: white;
  outline: none;
  transition: border-color 0.2s;
}

.search-input:focus {
  border-color: var(--color-primary);
}

.search-input::placeholder {
  color: #bbb;
}

.search-clear {
  position: absolute;
  right: 8px;
  background: none;
  border: none;
  padding: 4px;
  cursor: pointer;
  color: var(--color-text-secondary);
  border-radius: 4px;
  display: flex;
  align-items: center;
}

.search-clear:hover {
  color: var(--color-text);
  background: #f3efe9;
}

.toolbar-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}

/* Search results header */
.search-results-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
  background: #fff8e1;
  border-radius: 10px;
  font-size: 13px;
  color: #795548;
  margin-bottom: 12px;
}

/* 实时标题过滤提示栏：颜色偏淡 */
.search-filter-tip {
  background: #f5f5f5;
  color: #888;
}

/* ---- 语义搜索片段展示区 ---- */
.search-chunk-area {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.chunk-section {
  background: #faf6f0;
  border-radius: 10px;
  border: 1px solid #ede6dc;
  padding: 12px 14px;
}

.chunk-section-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-secondary);
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 10px;
}

.chunk-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.chunk-item {
  background: white;
  border-radius: 8px;
  border: 1px solid #ede6dc;
  overflow: hidden;
  transition: border-color 0.2s;
}

.chunk-item.expanded {
  border-color: var(--color-primary);
}

.chunk-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  cursor: pointer;
  transition: background 0.15s;
}

.chunk-header:hover {
  background: #fefcf9;
}

.chunk-badge {
  flex-shrink: 0;
  display: flex;
  align-items: center;
}

.chunk-header-info {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  max-width: 220px;
}

.chunk-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 140px;
}

.chunk-kb-badge {
  flex-shrink: 0;
  font-size: 11px;
  background: #fff3e0;
  color: var(--color-primary);
  padding: 1px 6px;
  border-radius: 4px;
  white-space: nowrap;
}

.chunk-page {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--color-text-secondary);
  white-space: nowrap;
}

.chunk-preview {
  flex: 1;
  font-size: 13px;
  color: var(--color-text-secondary);
  line-height: 1.5;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}

.chunk-toggle {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--color-primary);
  font-weight: 500;
}

.chunk-detail {
  border-top: 1px solid #ede6dc;
  padding: 10px;
}

.chunk-full-text {
  font-size: 13px;
  line-height: 1.7;
  color: var(--color-text);
  white-space: pre-wrap;
  word-wrap: break-word;
  margin-bottom: 10px;
  max-height: 300px;
  overflow-y: auto;
}

.chunk-actions {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  flex-wrap: wrap;
}

.chunk-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--color-primary);
  font-size: 12px;
  font-weight: 500;
  text-decoration: none;
  cursor: pointer;
}

.chunk-link:hover {
  text-decoration: underline;
}

.chunk-image-box {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
  width: 100%;
}

.chunk-image {
  max-width: 100%;
  max-height: 280px;
  border-radius: 6px;
  border: 1px solid #ede6dc;
  cursor: zoom-in;
  transition: opacity 0.2s;
}

.chunk-image:hover {
  opacity: 0.9;
}

/* relevance score 颜色 */
.relevance-score {
  font-size: 11px;
  font-weight: 500;
  padding: 1px 6px;
  border-radius: 4px;
}

.score-high {
  background: #d4edda;
  color: #155724;
}

.score-medium {
  background: #fff3cd;
  color: #856404;
}

.score-low {
  background: #f8d7da;
  color: #721c24;
}

/* Button styles */
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
  text-decoration: none;
  white-space: nowrap;
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-sm {
  padding: 6px 14px;
  font-size: 12px;
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

.btn-active {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.btn-primary {
  background: var(--color-primary);
  color: white;
  border-color: var(--color-primary);
}

.btn-primary:hover:not(:disabled) {
  background: #b0763c;
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

/* Loading */
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

/* Empty state */
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 80px 20px;
  text-align: center;
}

.empty-icon {
  width: 48px;
  height: 48px;
  color: var(--color-text-secondary);
  opacity: 0.4;
  margin-bottom: 16px;
}

.empty-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: 8px;
}

.empty-desc {
  font-size: 14px;
  color: var(--color-text-secondary);
  margin-bottom: 20px;
  max-width: 360px;
}

/* Config sections */
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

/* ---- llm-wiki 知识编译 ---- */
.wiki-filter-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 16px 0 8px;
}

.wiki-page-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.wiki-page-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  background: #faf8f5;
}

.wiki-type-tag {
  flex-shrink: 0;
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 8px;
  font-weight: 500;
  background: #eef2f7;
  color: #4a6fa5;
}

.wiki-type-tag.digest {
  background: #f3e8fd;
  color: #7b1fa2;
}

.wiki-page-title {
  flex: 1;
  min-width: 0;
  text-align: left;
  background: none;
  border: none;
  font-size: 13px;
  color: var(--color-text-primary, #333);
  cursor: pointer;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 0;
}

.wiki-page-title:hover {
  color: #b0763c;
  text-decoration: underline;
}

.wiki-detail-box {
  max-width: 720px;
  width: 92%;
  max-height: 82vh;
  display: flex;
  flex-direction: column;
}

.wiki-detail-body {
  overflow-y: auto;
  font-size: 14px;
  margin: 12px 0 16px;
  flex: 1;
  min-height: 120px;
}

.section-desc {
  font-size: 13px;
  color: var(--color-text-secondary);
  margin-bottom: 16px;
}

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

.section-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

/* ============================================
   OpenRouter 免费模型模块
   ============================================ */
.openrouter-toolbar {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-bottom: 12px;
}

.openrouter-search {
  flex: 1;
}

.openrouter-key-row {
  margin-bottom: 14px;
}

.openrouter-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 420px;
  overflow-y: auto;
  padding-right: 4px;
}

.openrouter-item {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 14px;
  background: #faf8f5;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  transition: border-color 0.2s, background 0.2s;
}

.openrouter-item.active {
  border-color: var(--color-primary);
  background: #fdf6ee;
}

.openrouter-item-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.openrouter-item-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text);
  display: flex;
  align-items: center;
  gap: 8px;
}

.openrouter-current {
  font-size: 11px;
  font-weight: 500;
  color: var(--color-primary);
  background: rgba(193, 139, 78, 0.12);
  padding: 1px 8px;
  border-radius: 10px;
}

.openrouter-item-id {
  font-size: 12px;
  color: var(--color-text-secondary);
  font-family: 'SF Mono', Menlo, monospace;
}

.openrouter-item-desc {
  font-size: 12px;
  color: var(--color-text-secondary);
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.openrouter-item-side {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 8px;
  flex-shrink: 0;
}

.openrouter-ctx {
  font-size: 11px;
  color: var(--color-text-secondary);
  white-space: nowrap;
}

.openrouter-apply-btn {
  white-space: nowrap;
}

.openrouter-empty {
  padding: 24px;
  text-align: center;
  font-size: 13px;
  color: var(--color-text-secondary);
  background: #faf8f5;
  border-radius: 10px;
  border: 1px dashed var(--color-border);
}

/* 参数在线配置面板 */
.param-panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-bottom: 16px;
  padding: 14px 16px;
  background: #faf8f5;
  border-radius: 10px;
  border: 1px solid var(--color-border);
}

.param-row {
  display: flex;
  align-items: center;
  gap: 12px;
}

.param-label {
  font-size: 13px;
  color: var(--color-text-secondary);
  width: 100px;
  flex-shrink: 0;
}

.param-input {
  flex: 1;
  padding: 8px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  font-size: 13px;
  color: var(--color-text);
  background: white;
  outline: none;
  transition: border-color 0.2s;
}

.param-input:focus {
  border-color: var(--color-primary);
}

.param-input::placeholder {
  color: #bbb;
}

/* Toggle switch */
.toggle-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  background: #faf8f5;
  border-radius: 10px;
  border: 1px solid var(--color-border);
}

.toggle-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.toggle-label {
  font-size: 14px;
  font-weight: 500;
  color: var(--color-text);
}

.toggle-hint {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.toggle-switch {
  position: relative;
  width: 48px;
  height: 26px;
  border-radius: 13px;
  border: none;
  background: #ccc;
  cursor: pointer;
  transition: background 0.25s;
  flex-shrink: 0;
  padding: 0;
}

.toggle-switch.active {
  background: var(--color-primary);
}

.toggle-switch:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.toggle-knob {
  position: absolute;
  top: 3px;
  left: 3px;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: white;
  box-shadow: 0 1px 3px rgba(0,0,0,0.2);
  transition: transform 0.25s;
}

.toggle-switch.active .toggle-knob {
  transform: translateX(22px);
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

/* User table */
.user-table-wrap {
  background: white;
  border: 1px solid var(--color-border);
  border-radius: 16px;
  overflow: hidden;
}

.user-table {
  width: 100%;
  border-collapse: collapse;
}

.user-table th {
  padding: 14px 16px;
  text-align: left;
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  background: #faf8f5;
  border-bottom: 1px solid var(--color-border);
}

.user-table td {
  padding: 14px 16px;
  font-size: 14px;
  color: var(--color-text);
  border-bottom: 1px solid #f0ebe5;
}

.user-table tr:last-child td {
  border-bottom: none;
}

.row-disabled td {
  opacity: 0.5;
}

.cell-username {
  display: flex;
  align-items: center;
  gap: 8px;
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
  font-size: 13px;
  color: var(--color-text-secondary);
  white-space: nowrap;
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

/* Entry cards */
.entries-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.entry-card {
  background: white;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  transition: all 0.2s;
  display: flex;
  align-items: stretch;
  overflow: hidden;
  cursor: pointer;
  height: 140px;
}

.entry-card:hover {
  border-color: var(--color-primary);
  box-shadow: 0 2px 8px rgba(212, 133, 46, 0.08);
}

.entry-main {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 16px;
  flex: 1;
  min-width: 0;
}

.entry-info {
  flex: 1;
  min-width: 0;
}

.entry-filename {
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: 4px;
}

.entry-subname {
  font-size: 12px;
  color: var(--color-text-secondary);
  margin-bottom: 4px;
}

.entry-meta {
  display: flex;
  gap: 12px;
  margin-bottom: 6px;
  flex-wrap: wrap;
}

.meta-item {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.relevance-badge {
  background: #e8f5e9;
  color: #2e7d32;
  padding: 0 6px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 500;
}

.entry-preview {
  font-size: 13px;
  color: var(--color-text-secondary);
  line-height: 1.6;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
}

.btn-delete-entry {
  flex-shrink: 0;
  padding: 6px;
  border: none;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  border-radius: 8px;
  transition: all 0.2s;
}

.btn-delete-entry:hover {
  background: #fce4ec;
  color: #c62828;
}

.btn-move-entry {
  flex-shrink: 0;
  padding: 6px;
  border: none;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  border-radius: 8px;
  transition: all 0.2s;
}

.btn-move-entry:hover {
  background: #fff3e0;
  color: #ef6c00;
}

.move-kb-select {
  width: 100%;
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: #fff;
  font-size: 14px;
  color: var(--color-text);
  margin: 4px 0 8px;
}

.entry-thumb {
  width: 160px;
  height: 140px;
  flex-shrink: 0;
  overflow: hidden;
  border-left: 1px solid var(--color-border);
  background: #faf8f5;
  display: flex;
  align-items: center;
  justify-content: center;
}

.entry-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

/* Modal */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.4);
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
  box-shadow: 0 8px 32px rgba(0,0,0,0.15);
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

/* Preview modal */
.preview-overlay {
  align-items: flex-start;
  padding: 5vh 0;
  overflow-y: auto;  /* overlay 本身可滚动（备用） */
}

.preview-modal {
  background: white;
  border-radius: 16px;
  max-width: 960px;
  width: 92%;
  max-height: 90vh;
  height: 90vh;          /* 固定高度，配合 flex 布局让 body 撑满 */
  display: flex;
  flex-direction: column;
  box-shadow: 0 8px 32px rgba(0,0,0,0.2);
  overflow: hidden;
  margin: auto;          /* 居中 */
}

.preview-header {
  padding: 20px 24px;
  border-bottom: 1px solid var(--color-border);
  display: flex;
  flex-direction: column;
  gap: 6px;
  position: relative;
}

.preview-title {
  font-size: 17px;
  font-weight: 600;
  color: var(--color-text);
  padding-right: 32px;
}

.preview-meta {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}

.preview-meta-item {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.preview-close {
  position: absolute;
  top: 18px;
  right: 18px;
  background: none;
  border: none;
  cursor: pointer;
  color: var(--color-text-secondary);
  padding: 4px;
  border-radius: 6px;
  display: flex;
  align-items: center;
}

.preview-close:hover {
  background: #f3efe9;
  color: var(--color-text);
}

.preview-body {
  flex: 1 1 0;       /* 1 1 0 而非 1 1 auto：基础大小 0，完全由 flex 分配高度 */
  min-height: 0;     /* 允许 flex 子项收缩，触发 overflow-y 滚动 */
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.preview-image-section {
  border-radius: 12px;
  overflow: hidden;
  border: 1px solid var(--color-border);
  background: #faf8f5;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 300px;
}

.preview-image {
  width: 100%;
  height: auto;
  object-fit: contain;
  cursor: zoom-in;
  transition: opacity 0.2s;
}

.preview-image:hover {
  opacity: 0.85;
}

.preview-image-hint {
  position: absolute;
  bottom: 12px;
  right: 12px;
  background: rgba(0,0,0,0.6);
  color: white;
  font-size: 12px;
  padding: 4px 10px;
  border-radius: 6px;
  cursor: zoom-in;
  opacity: 0;
  transition: opacity 0.2s;
  pointer-events: auto;
}

.preview-image-section {
  position: relative;
  border-radius: 12px;
  overflow: hidden;
  border: 1px solid var(--color-border);
  background: #faf8f5;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 300px;
}

.preview-image-section:hover .preview-image-hint {
  opacity: 1;
}

/* ---- PDF 文档预览 ---- */
.preview-pdf-section {
  border-radius: 12px;
  overflow: visible;   /* 不裁剪内容，让 .preview-body 统一滚动 */
  border: 1px solid var(--color-border);
  background: #faf8f5;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;      /* 不压缩，内容多少展示多少 */
}

.preview-pdf-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  background: white;
  border-bottom: 1px solid var(--color-border);
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
}

.preview-pdf-header svg {
  color: var(--color-primary);
  flex-shrink: 0;
}

.preview-pdf-header span {
  flex: 1;
}

.preview-pdf-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}

.preview-pdf-open {
  display: flex;
  align-items: center;
  gap: 4px;
  color: var(--color-primary);
  text-decoration: none;
  font-size: 12px;
  font-weight: 500;
  padding: 4px 8px;
  border-radius: 6px;
  transition: background 0.2s;
}

.preview-pdf-open:hover {
  background: var(--color-primary-light);
}

/* 文档分页预览区域 */
.preview-doc-pages {
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  background: #fafafa;
}

.preview-doc-page {
  background: white;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  overflow: hidden;
  box-shadow: 0 1px 4px rgba(0,0,0,0.06);
}

.preview-doc-page-num {
  font-size: 11px;
  font-weight: 600;
  color: var(--color-text-secondary);
  background: #f3efe9;
  padding: 6px 14px;
  border-bottom: 1px solid var(--color-border);
  letter-spacing: 0.3px;
}

.preview-doc-page-text {
  font-size: 13px;
  line-height: 1.9;
  color: var(--color-text);
  padding: 14px 16px;
  white-space: pre-wrap;
  word-wrap: break-word;
}

/* ---- Word 原始排版 PDF 分页预览 ---- */
.preview-pdf-frame {
  width: 100%;
  height: 60vh;
  border: none;
  background: #fff;
}

/* ---- Word 原始文档预览区域 ---- */
.preview-word-content {
  padding: 24px 28px;
  background: #fff;
  font-size: 14px;
  line-height: 1.8;
  color: #1a1a1a;
}

.preview-doc-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 48px;
  color: var(--color-text-secondary);
  font-size: 14px;
}

.preview-word-fallback {
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--color-text);
}

.preview-word-error {
  font-size: 12px;
  color: #e05050;
  background: #fff3f3;
  border: 1px solid #fcc;
  border-radius: 6px;
  padding: 8px 12px;
  margin-bottom: 12px;
  word-break: break-all;
}

/* ---- 文档类型缩略图 ---- */
.entry-thumb-doc {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  color: var(--color-text-secondary);
  background: #f3efe9;
  cursor: pointer;
}

.entry-thumb-doc:hover {
  background: var(--color-primary-light);
  color: var(--color-primary);
}

.doc-type-label {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.preview-text-section {
  background: #faf8f5;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 20px;
}

.preview-text-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: 12px;
}

.preview-text-content {
  font-size: 14px;
  color: var(--color-text);
  line-height: 1.8;
  white-space: pre-wrap;
  word-wrap: break-word;
}

/* Image viewer overlay */
.image-viewer-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.92);
  z-index: 200;
  display: flex;
  flex-direction: column;
}

.image-viewer-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 20px;
  background: rgba(0, 0, 0, 0.5);
  flex-shrink: 0;
}

.image-viewer-filename {
  color: rgba(255, 255, 255, 0.8);
  font-size: 14px;
  font-weight: 500;
}

.image-viewer-controls {
  display: flex;
  align-items: center;
  gap: 8px;
}

.viewer-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border: none;
  background: rgba(255, 255, 255, 0.1);
  color: white;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.2s;
}

.viewer-btn:hover {
  background: rgba(255, 255, 255, 0.25);
}

.viewer-close-btn:hover {
  background: rgba(198, 40, 40, 0.7);
}

.image-viewer-zoom-level {
  color: white;
  font-size: 13px;
  min-width: 48px;
  text-align: center;
  font-variant-numeric: tabular-nums;
}

.image-viewer-body {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  padding: 20px;
}

.image-viewer-img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  transition: transform 0.15s ease;
  cursor: zoom-in;
  user-select: none;
  -webkit-user-drag: none;
}

/* ---- 移动端适配 ---- */
@media (max-width: 768px) {
  .management-view {
    padding: 0;
  }

  .tabs-header {
    padding: 0 12px;
  }

  .tabs-nav {
    overflow-x: auto;
    gap: 0;
    padding-top: 52px; /* 为汉堡菜单图标留空间 */
  }

  .tab-btn {
    padding: 10px 14px;
    font-size: 13px;
    white-space: nowrap;
    flex-shrink: 0;
  }

  .tab-btn svg {
    display: none;
  }

  .tab-settings {
    padding: 16px;
  }

  .tab-users {
    padding: 16px;
  }

  .tab-knowledge {
    padding: 16px;
  }

  .config-section {
    padding: 16px;
  }

  .section-title {
    font-size: 15px;
  }

  .provider-option {
    padding: 10px 12px;
    gap: 8px;
    flex-wrap: wrap;
  }

  .option-config {
    width: 100%;
    text-align: left;
    padding-left: 28px;
  }

  .param-row {
    flex-direction: column;
    align-items: stretch;
    gap: 4px;
  }

  .param-label {
    width: auto;
  }

  .knowledge-toolbar {
    flex-direction: column;
  }

  .toolbar-right {
    width: 100%;
    flex-direction: column;
    align-items: stretch;
  }

  .search-box {
    min-width: 0;
    width: 100%;
  }

  .toolbar-actions {
    justify-content: flex-start;
    flex-wrap: wrap;
  }

  .user-table-wrap {
    overflow-x: auto;
  }

  .user-table {
    min-width: 600px;
  }

  .user-table th,
  .user-table td {
    padding: 10px 12px;
    font-size: 13px;
  }

  .entry-card {
    height: auto;
    flex-direction: column;
  }

  .entry-main {
    padding: 12px;
  }

  .entry-thumb {
    width: 100%;
    height: 120px;
    border-left: none;
    border-top: 1px solid var(--color-border);
  }

  .entry-filename {
    font-size: 13px;
  }

  .preview-modal {
    width: 96%;
    max-height: 85vh;
    height: 85vh;
  }

  .preview-header {
    padding: 14px 16px;
  }

  .preview-body {
    padding: 14px;
  }

  .preview-image-section {
    min-height: 180px;
  }

  .users-title {
    font-size: 18px;
  }

  .toggle-row {
    flex-direction: column;
    align-items: flex-start;
    gap: 12px;
  }
}

/* ---- 游客限制提示 ---- */
.guest-limit-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 18px;
  margin-top: 16px;
  background: linear-gradient(135deg, #faf8f5, #f3efe9);
  border: 1px solid #ede6dc;
  border-radius: 12px;
  color: #8c7e6e;
  font-size: 13px;
}

.guest-limit-banner svg {
  flex-shrink: 0;
  color: #c98a4b;
}

.guest-limit-banner span {
  flex: 1;
}

.guest-limit-banner .btn {
  flex-shrink: 0;
}

/* ---- 知识库筛选与管理 ---- */
.kb-filter-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 16px;
}

.kb-filter-label {
  font-size: 13px;
  color: var(--color-text-secondary);
  flex-shrink: 0;
}

.kb-filter-select {
  flex: 1;
  max-width: 360px;
  padding: 8px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  font-size: 13px;
  color: var(--color-text);
  background: white;
  outline: none;
  cursor: pointer;
}

.kb-filter-select:focus {
  border-color: var(--color-primary);
}

.kb-panel {
  background: #faf6f0;
  border: 1px solid #ede6dc;
  border-radius: 10px;
  padding: 12px 14px;
  margin-bottom: 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.kb-panel-empty {
  font-size: 13px;
  color: var(--color-text-secondary);
}

.kb-panel-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  background: white;
  border: 1px solid #ede6dc;
  border-radius: 8px;
  padding: 8px 12px;
}

.kb-panel-info {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
}

.kb-panel-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
}

/* 用户管理子标签 */
.users-subtabs {
  display: flex;
  gap: 4px;
  margin: 20px 0 16px;
  border-bottom: 1px solid var(--color-border);
}

.subtab-btn {
  padding: 8px 18px;
  font-size: 14px;
  color: var(--color-text-secondary);
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  cursor: pointer;
  transition: all 0.2s;
}

.subtab-btn:hover {
  color: var(--color-text);
}

.subtab-btn.active {
  color: var(--color-primary);
  border-bottom-color: var(--color-primary);
  font-weight: 600;
}

/* 游客次数输入 */
.guest-limit-input-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
}

.guest-limit-input {
  width: 80px;
  padding: 6px 10px;
  font-size: 14px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg-secondary);
  color: var(--color-text);
}

.guest-limit-input:focus {
  outline: none;
  border-color: var(--color-primary);
}

.kb-panel-tag {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 4px;
  background: #f3efe9;
  color: var(--color-text-secondary);
}

.kb-panel-tag.tag-public {
  background: #e8f5e9;
  color: #2e7d32;
}

.kb-panel-tag.tag-private {
  background: #fff3e0;
  color: #b26a00;
}

.kb-panel-count {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.kb-badge {
  background: var(--color-primary-light);
  color: var(--color-primary);
  padding: 0 6px;
  border-radius: 4px;
  font-size: 11px;
}
</style>
