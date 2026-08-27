import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
})

// 请求拦截器：自动注入 auth token
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('auth_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// 响应拦截器：401 时清除 token 并跳转登录页
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      // 游客模式下不跳转，只标记错误
      const isGuest = localStorage.getItem('guest_mode') === 'true' && !localStorage.getItem('auth_token')
      if (isGuest) {
        return Promise.reject(error)
      }
      localStorage.removeItem('auth_token')
      localStorage.removeItem('auth_user')
      // 用 pathname 而非 hash（项目使用 createWebHistory 模式）
      if (window.location.pathname !== '/login' && window.location.pathname !== '/register') {
        window.location.href = '/login'
      }
    }
    return Promise.reject(error)
  }
)

/**
 * 构造受保护图片的访问 URL
 * 后端图片接口需登录，而 <img> 无法携带 Authorization 头，故附加 token 查询参数
 */
export function getImageUrl(imagePath) {
  if (!imagePath) return ''
  const filename = imagePath.split('/').pop()
  const token = localStorage.getItem('auth_token')
  return `/uploads/${encodeURIComponent(filename)}${token ? `?token=${encodeURIComponent(token)}` : ''}`
}

/**
 * 构造聊天附件原文件访问 URL（游客可用，无需 token；文件名为不可猜测的 UUID）
 */
export function getAttachmentFileUrl(fileId) {
  if (!fileId) return ''
  return `/api/chat/attachment/${encodeURIComponent(fileId)}`
}

// ============================================
// 聊天 API
// ============================================

/**
 * 发送消息 (SSE 流式)
 * @returns {EventSource} 或使用 fetch 手动处理流
 */
export function sendChatMessage(params) {
  const headers = { 'Content-Type': 'application/json' }
  const token = localStorage.getItem('auth_token')
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }
  return fetch('/api/chat/send', {
    method: 'POST',
    headers,
    body: JSON.stringify(params),
  })
}

/**
 * 发送带附件的消息 (SSE 流式, multipart/form-data)
 * @param {object} params { session_id, query, use_knowledge, use_search, llm_provider, kb_id }
 * @param {File[]} files 拖入/选择的附件文件（图片/Word/PDF）
 */
export function sendChatMessageWithFiles(params, files) {
  const headers = {}
  const token = localStorage.getItem('auth_token')
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }
  const form = new FormData()
  form.append('message', params.query || '')
  form.append('session_id', params.session_id || '')
  form.append('use_knowledge', String(!!params.use_knowledge))
  form.append('use_search', String(!!params.use_search))
  form.append('llm_provider', params.llm_provider || '')
  form.append('kb_id', params.kb_id || '')
  for (const file of files || []) {
    form.append('files', file)
  }
  return fetch('/api/chat/send-with-files', {
    method: 'POST',
    headers,
    body: form,
  })
}

/**
 * 获取聊天历史
 */
export function getChatHistory(sessionId) {
  return api.get(`/chat/history/${sessionId}`)
}

/**
 * 清空聊天历史
 */
export function clearChatHistory(sessionId) {
  return api.delete(`/chat/history/${sessionId}`)
}

/**
 * 创建新会话
 */
export function createSession() {
  return api.post('/chat/sessions')
}

/**
 * 获取会话列表
 */
export function listSessions() {
  return api.get('/chat/sessions')
}

/**
 * 删除会话
 */
export function deleteSession(sessionId) {
  return api.delete(`/chat/sessions/${sessionId}`)
}

/**
 * 切换会话
 */
export function switchSession(sessionId) {
  return api.post(`/chat/sessions/${sessionId}/switch`)
}

/**
 * 提交回答反馈（点赞/点踩 + 备注）
 */
export function submitFeedback({ sessionId, messageId, rating, comment = '' }) {
  return api.post('/chat/feedback', {
    session_id: sessionId,
    message_id: messageId,
    rating,
    comment,
  })
}

// ============================================
// 知识库 API
// ============================================

/**
 * 上传图片并处理
 */
export function uploadImage(file, ocrProvider, visibility = 'public', kbId = '', overrideText = '', title = '') {
  const formData = new FormData()
  formData.append('file', file)
  if (ocrProvider) {
    formData.append('ocr_provider', ocrProvider)
  }
  formData.append('visibility', visibility)
  if (kbId) {
    formData.append('kb_id', kbId)
  }
  if (overrideText) {
    formData.append('override_text', overrideText)
  }
  if (title) {
    formData.append('title', title)
  }
  return api.post('/knowledge/upload', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 300000,
  })
}

/**
 * 批量上传图片
 */
export function uploadImages(files, ocrProvider, visibility = 'public', kbId = '', title = '') {
  const formData = new FormData()
  files.forEach((file) => formData.append('files', file))
  if (ocrProvider) {
    formData.append('ocr_provider', ocrProvider)
  }
  formData.append('visibility', visibility)
  if (kbId) {
    formData.append('kb_id', kbId)
  }
  if (title) {
    formData.append('title', title)
  }
  return api.post('/knowledge/upload/batch', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 300000,
  })
}

/**
 * 上传 Word/PDF 文档
 */
export function uploadDocument(file, docType, visibility = 'public', kbId = '', title = '') {
  const formData = new FormData()
  formData.append('file', file)
  formData.append('doc_type', docType)
  formData.append('visibility', visibility)
  if (kbId) {
    formData.append('kb_id', kbId)
  }
  if (title) {
    formData.append('title', title)
  }
  return api.post('/knowledge/upload/document', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 300000,
  })
}

/**
 * 预览 Word/PDF 文档分页内容（不入库）
 */
export function previewDocument(file, docType) {
  const formData = new FormData()
  formData.append('file', file)
  formData.append('doc_type', docType)
  return api.post('/knowledge/preview-document', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 300000,
  })
}

/**
 * 保存用户确认的文档页面到知识库
 */
export function saveDocumentPages(payload) {
  return api.post('/knowledge/save-document-pages', payload, {
    timeout: 300000,
  })
}

/**
 * 流式保存文档页面到知识库，SSE 逐页回传实时进度。
 * onEvent(ev) 事件：save_start / page_saved / page_error / save_done
 */
export async function saveDocumentPagesStream(payload, onEvent) {
  const token = localStorage.getItem('auth_token')
  const resp = await fetch('/api/knowledge/save-document-pages-stream', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(payload),
  })
  if (!resp.ok) {
    let detail = ''
    try {
      detail = (await resp.json()).detail || ''
    } catch (e) {
      /* 忽略解析失败 */
    }
    throw new Error(detail || `HTTP ${resp.status}`)
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, idx)
      buf = buf.slice(idx + 2)
      const line = chunk.split('\n').find((l) => l.startsWith('data: '))
      if (!line) continue
      try {
        onEvent && onEvent(JSON.parse(line.slice(6)))
      } catch (e) {
        /* 忽略单条事件解析失败 */
      }
    }
  }
}

/**
 * 为已有 Word 条目生成原始排版 PDF 预览（按需转换）
 */
export function convertWordPdf(sourceImage) {
  return api.post('/knowledge/convert-word-pdf', { source_image: sourceImage }, {
    timeout: 300000,
  })
}

/**
 * 预览 OCR 结果
 */
export function previewOCR(file, ocrProvider) {
  const formData = new FormData()
  formData.append('file', file)
  if (ocrProvider) {
    formData.append('ocr_provider', ocrProvider)
  }
  return api.post('/knowledge/preview-ocr', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 300000,
  })
}

/**
 * 获取知识库统计
 */
export function getKnowledgeStats() {
  return api.get('/knowledge/stats')
}

/**
 * 清空知识库
 */
export function clearKnowledge() {
  return api.delete('/knowledge/clear')
}

/**
 * 获取当前用户可见的主题知识库列表（含预设主题）
 */
export function listKnowledgeBases() {
  return api.get('/knowledge/bases')
}

/**
 * 创建主题知识库
 */
export function createKnowledgeBase({ name, topic, visibility }) {
  return api.post('/knowledge/bases', { name, topic, visibility })
}

/**
 * 删除知识库（及其全部条目）
 */
export function deleteKnowledgeBase(kbId) {
  return api.delete(`/knowledge/bases/${kbId}`)
}

/**
 * 修改知识库名称/主题
 */
export function updateKnowledgeBase(kbId, { name, topic }) {
  return api.patch(`/knowledge/bases/${kbId}`, { name, topic })
}

// ============================================
// 上传记录 API
// ============================================

/**
 * 获取上传记录列表
 */
export function getUploadRecords(limit = 50, offset = 0) {
  return api.get('/knowledge/records', { params: { limit, offset } })
}

/**
 * 获取单条上传记录
 */
export function getUploadRecord(recordId) {
  return api.get(`/knowledge/records/${recordId}`)
}

/**
 * 更新上传记录的 OCR 文本
 */
export function updateUploadRecord(recordId, ocrText) {
  return api.put(`/knowledge/records/${recordId}`, { ocr_text: ocrText })
}

/**
 * 删除上传记录
 */
export function deleteUploadRecord(recordId) {
  return api.delete(`/knowledge/records/${recordId}`)
}

/**
 * 润色 OCR 文本（用 LLM 调整语序）
 */
export function polishOCR(ocrText) {
  return api.post('/knowledge/polish-ocr', { ocr_text: ocrText })
}

/**
 * 获取知识库条目列表（按来源图片分组）
 * @param {string} q 按标题/文件名搜索关键字
 */
export function getKnowledgeEntries(q = '') {
  const params = q ? { q } : {}
  return api.get('/knowledge/entries', { params })
}

/**
 * 按来源图片删除知识库条目
 */
export function deleteKnowledgeEntriesBySource(sourceImage) {
  return api.delete('/knowledge/entries/by-source', { params: { source_image: sourceImage } })
}

/**
 * 移动知识文档（同来源全部条目）到目标知识库
 */
export function moveSourceToKb(sourceImage, targetKbId) {
  return api.post('/knowledge/entries/move-source', {
    source_image: sourceImage,
    target_kb_id: targetKbId,
  })
}

/**
 * 在知识库中搜索检索
 */
export function searchKnowledge(query, topK = 5) {
  return api.post('/knowledge/search', { query, top_k: topK })
}

/**
 * 批量上传图片（SSE 流式进度）
 * 返回 fetch Response，需自行处理 EventSource 流
 */
export function uploadImagesBatch(files, ocrProvider, visibility = 'public', kbId = '') {
  const formData = new FormData()
  files.forEach((file) => formData.append('files', file))
  if (ocrProvider) {
    formData.append('ocr_provider', ocrProvider)
  }
  formData.append('visibility', visibility)
  if (kbId) {
    formData.append('kb_id', kbId)
  }
  const headers = {}
  const token = localStorage.getItem('auth_token')
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }
  return fetch('/api/knowledge/upload/batch', {
    method: 'POST',
    headers,
    body: formData,
  })
}

// ============================================
// 配置 & 健康
// ============================================

/**
 * 获取完整系统配置
 */
export function getSystemConfig() {
  return api.get('/config')
}

/**
 * 获取公开配置（无需登录）：注册开关 + 游客免费次数
 */
export function getPublicConfig() {
  return api.get('/config/public')
}

/**
 * 切换运行时提供商
 */
export function switchProvider(providerType, value) {
  return api.post('/config/provider', { provider_type: providerType, value })
}

/**
 * 保存提供商参数（管理员），同步为系统默认配置
 */
export function saveProviderSettings(category, provider, values) {
  return api.post('/config/provider-settings', { category, provider, values })
}

/**
 * 开启/关闭用户注册功能
 */
export function toggleRegistration(enabled) {
  return api.post('/config/registration', { enabled })
}

/**
 * 设定游客免费访问次数（仅管理员）
 */
export function setGuestLimit(limit) {
  return api.post('/config/guest-limit', { limit })
}

/**
 * 搜索 OpenRouter 免费模型列表
 * @param {string} search 搜索关键词
 * @param {boolean} refresh 是否强制刷新
 */
export function getOpenRouterModels(search = '', refresh = false) {
  return api.get('/config/openrouter-models', { params: { search, refresh }, timeout: 60000 })
}

/**
 * 将 OpenRouter 免费模型应用为系统 LLM
 */
export function applyOpenRouterModel(model, apiKey = '') {
  return api.post('/config/openrouter-apply', { model, api_key: apiKey })
}

// ============================================
// 知识收藏 API
// ============================================

/**
 * 创建收藏
 */
export function createBookmark({ title, tags, content, sourceType, sourceInfo }) {
  return api.post('/bookmarks', { title, tags, content, source_type: sourceType, source_info: sourceInfo })
}

/**
 * 获取当前用户的收藏列表
 */
export function listBookmarks() {
  return api.get('/bookmarks')
}

/**
 * 删除收藏
 */
export function deleteBookmark(bookmarkId) {
  return api.delete(`/bookmarks/${bookmarkId}`)
}

/**
 * 测试 LLM 连接
 */
export function testLLMConnection(provider, model = '') {
  return api.post('/config/test-llm', { provider_type: 'llm', provider, model })
}

/**
 * 测试搜索连接
 */
export function testSearchConnection(provider) {
  return api.post('/config/test-search', { provider_type: 'search', provider })
}

/**
 * 测试 OCR 配置
 */
export function testOCRConnection(provider) {
  return api.post('/config/test-ocr', { provider_type: 'ocr', provider })
}

/**
 * 测试 Embedding 连接
 */
export function testEmbeddingConnection() {
  return api.post('/config/test-embedding', { provider_type: 'embedding', provider: '' })
}

/**
 * 保存 Redis 文件缓存配置（url 留空 = 回退 .env 默认）
 */
export function saveRedisConfig(payload) {
  return api.post('/config/redis', payload)
}

/**
 * 测试 Redis 连接（不传 url 则测当前生效配置）
 */
export function testRedisConnection(url = '') {
  return api.post('/config/test-redis', { url })
}

/**
 * 测试 Rerank 配置（本地模型 / DashScope API）
 */
export function testRerankConnection(provider) {
  return api.post('/config/test-rerank', { provider_type: 'rerank', provider })
}

// ============================================
// llm-wiki 知识百科 API
// ============================================

/**
 * 知识编译状态（开关/已编译页面数）
 */
export function getWikiStatus() {
  return api.get('/wiki/status')
}

/**
 * 百科页面列表（可按知识库/类型过滤）
 */
export function getWikiPages(params = {}) {
  return api.get('/wiki/pages', { params })
}

/**
 * 百科页面详情（完整 Markdown）
 */
export function getWikiPage(pageId) {
  return api.get(`/wiki/pages/${encodeURIComponent(pageId)}`)
}

/**
 * 删除百科页面（含其向量条目）
 */
export function deleteWikiPage(pageId) {
  return api.delete(`/wiki/pages/${encodeURIComponent(pageId)}`)
}

/**
 * digest 工作流：跨素材深度综合生成持久化百科条目（LLM 耗时长，单独放宽超时）
 */
export function generateWikiDigest(query) {
  return api.post('/wiki/digest', { query }, { timeout: 300000 })
}

/**
 * 存量资料批量补编译（管理员，后台调度）
 */
export function recompileWikiAll() {
  return api.post('/wiki/recompile-all')
}

/**
 * 获取知识编译配置
 */
export function getWikiConfig() {
  return api.get('/wiki/config')
}

/**
 * 保存知识编译配置（仅管理员）
 */
export function saveWikiConfig(payload) {
  return api.post('/wiki/config', payload)
}

/**
 * 健康检查
 */
export function healthCheck() {
  return api.get('/health')
}

// ============================================
// 认证 API
// ============================================

/**
 * 用户注册
 */
export function registerUser(username, password, contact) {
  return api.post('/auth/register', { username, password, contact })
}

/**
 * 用户登录
 */
export function loginUser(username, password) {
  return api.post('/auth/login', { username, password })
}

/**
 * 用户登出
 */
export function logoutUser() {
  return api.post('/auth/logout')
}

/**
 * 获取当前登录用户信息
 */
export function getCurrentUser() {
  return api.get('/auth/me')
}

/**
 * 修改当前登录用户的个人信息（用户名/联系方式/密码）
 */
export function updateProfile(data) {
  return api.put('/auth/me', data)
}

/**
 * 当前用户个人使用看板（近 N 天）
 */
export function getMyUsageStats(days = 30) {
  return api.get('/auth/me/stats', { params: { days } })
}

/**
 * 获取所有用户列表（管理员）
 */
export function listUsers() {
  return api.get('/auth/users')
}

/**
 * 启用/禁用用户（管理员）
 */
export function setUserActive(userId, isActive) {
  return api.put(`/auth/users/${userId}/active`, { is_active: isActive })
}

/**
 * 删除用户（管理员）
 */
export function deleteUser(userId) {
  return api.delete(`/auth/users/${userId}`)
}

/**
 * 用户统计看板汇总（管理员）
 */
export function getUsersStats() {
  return api.get('/auth/users/stats')
}

/**
 * 单个用户统计详情（管理员）：每日趋势 + 点踩问题清单
 */
export function getUserStatsDetail(userId) {
  return api.get(`/auth/users/${userId}/stats`)
}

export default api
