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
export const createUrlImport = (url) => api.post('/knowledge/url-imports', { url })
export const listUrlImports = () => api.get('/knowledge/url-imports')
export const getUrlImport = (id) => api.get(`/knowledge/url-imports/${encodeURIComponent(id)}`)
export const saveUrlImport = (id, body) => api.patch(`/knowledge/url-imports/${encodeURIComponent(id)}`, body)
export const retryUrlImport = (id, revision) => api.post(`/knowledge/url-imports/${encodeURIComponent(id)}/retry`, { expected_revision: revision })
export const confirmUrlImport = (id, revision, kbId) => api.post(`/knowledge/url-imports/${encodeURIComponent(id)}/confirm`, { revision, kb_id: kbId })
export const deleteUrlImport = (id) => api.delete(`/knowledge/url-imports/${encodeURIComponent(id)}`)
export const getUrlSnapshot = (path) => api.get(`/uploads/${encodeURIComponent(path.split('/').pop())}`, { baseURL: '/', responseType: 'text' })

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
export function sendChatMessage(params, signal) {
  const headers = { 'Content-Type': 'application/json' }
  const token = localStorage.getItem('auth_token')
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }
  return fetch('/api/chat/send', {
    signal,
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
export function sendChatMessageWithFiles(params, files, signal) {
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
  form.append('rule_id', params.rule_id || '')
  form.append('model', params.model || '')
  form.append('skip_search_prompt', String(!!params.skip_search_prompt))
  for (const file of files || []) {
    form.append('files', file)
  }
  return fetch('/api/chat/send-with-files', {
    signal,
    method: 'POST',
    headers,
    body: form,
  })
}

/**
 * 获取聊天历史
 */
export const getChatAction = (id, signal) => api.get(`/chat/actions/${id}`, { signal })
export const confirmChatAction = (id, kbId) => api.post(`/chat/actions/${id}/confirm`, { kb_id: kbId })
export const cancelChatAction = (id) => api.post(`/chat/actions/${id}/cancel`)

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
 * 手动压缩会话历史（/compact 等价物）：focus 为可选压缩侧重点
 */
export function compactSession(sessionId, focus = '') {
  return api.post(`/chat/sessions/${sessionId}/compact`, { focus })
}

/**
 * 我的记忆：列表 / 主动写入 / 编辑 / 删除（个人中心「我的记忆」块）
 */
export function listMyMemories() {
  return api.get('/me/memories')
}

export function createMyMemory(content, memoryType = '', importance = null) {
  return api.post('/me/memories', { content, memory_type: memoryType, importance })
}

export function updateMyMemory(memoryId, { content = '', memory_type: memoryType = '', importance = null } = {}) {
  return api.put(`/me/memories/${encodeURIComponent(memoryId)}`, { content, memory_type: memoryType, importance })
}

export function deleteMyMemory(memoryId) {
  return api.delete(`/me/memories/${encodeURIComponent(memoryId)}`)
}

/**
 * Skill 配置：列表 / 新建 / 编辑（含开关） / 删除
 * kind: prompt（对话提示注入） | playbook（链接抓取策略）；内置技能仅可开关
 */
export function listSkills() {
  return api.get('/skills')
}

export function createSkill({ name, kind = 'prompt', content = '', description = '', enabled = true, scope = null }) {
  return api.post('/skills', { name, kind, content, description, enabled, scope })
}

export function updateSkill(skillId, { name = null, description = null, content = null, enabled = null } = {}) {
  return api.put(`/skills/${encodeURIComponent(skillId)}`, { name, description, content, enabled })
}

export function deleteSkill(skillId) {
  return api.delete(`/skills/${encodeURIComponent(skillId)}`)
}

/**
 * Skill 文档（渐进式/懒加载）：
 * - getSkillDoc：L2 激活，拉取 SKILL.md 主体 + 可用 references 列表（不含正文）
 * - getSkillDocRef：L3 执行，按 path 单独拉取某一篇扩展参考文档
 */
export function getSkillDoc(skillId) {
  return api.get(`/skills/${encodeURIComponent(skillId)}/doc`)
}

export function getSkillDocRef(skillId, path) {
  return api.get(`/skills/${encodeURIComponent(skillId)}/doc/ref`, { params: { path } })
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

// ============================================
// 知识库上传流水线：统一上传 → 后台解析 → 确认入库
// ============================================

/**
 * 统一文件上传（图片/Word/PDF 同一入口），XHR 实时上报上传进度。
 * 上传成功后后端自动后台解析，不立即入库。
 * @param {File} file
 * @param {string} ocrProvider 图片 OCR 引擎（仅图片生效）
 * @param {function} onProgress 进度回调 (0-100)
 * @returns {Promise<object>} 响应 data：{ success, item }
 */
export function uploadPendingFile(file, ocrProvider = '', onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', '/api/knowledge/pending/upload')
    const token = localStorage.getItem('auth_token')
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) {
        onProgress(Math.round((e.loaded / e.total) * 100))
      }
    }
    xhr.onload = () => {
      let data = null
      try {
        data = JSON.parse(xhr.responseText)
      } catch (e) {
        /* 忽略解析失败 */
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve({ data })
      } else {
        const err = new Error(data?.detail || `上传失败 HTTP ${xhr.status}`)
        err.response = { data, status: xhr.status }
        reject(err)
      }
    }
    xhr.onerror = () => reject(new Error('网络异常，上传失败'))
    const form = new FormData()
    form.append('file', file)
    if (ocrProvider) form.append('ocr_provider', ocrProvider)
    xhr.send(form)
  })
}

/**
 * 待处理文件列表（含解析状态）
 */
export function listPendingUploads() {
  return api.get('/knowledge/pending')
}

/**
 * 待处理文件详情（含解析全文/分页）
 */
export function getPendingUpload(itemId) {
  return api.get(`/knowledge/pending/${itemId}`)
}

/**
 * 保存解析结果编辑（入库前检查修正）
 */
export function updatePendingUpload(itemId, payload) {
  return api.put(`/knowledge/pending/${itemId}`, payload)
}

/**
 * 确认入库：写入知识库 + 后台编译 llm-wiki 卡片
 */
export function ingestPendingUpload(itemId, payload) {
  return api.post(`/knowledge/pending/${itemId}/ingest`, payload || {}, {
    timeout: 300000,
  })
}

/**
 * 解析失败后重新解析
 */
export function retryPendingParse(itemId) {
  return api.post(`/knowledge/pending/${itemId}/retry`)
}

/**
 * 删除待入库文件（连同磁盘文件）
 */
export function deletePendingUpload(itemId) {
  return api.delete(`/knowledge/pending/${itemId}`)
}

/**
 * 入库历史列表（仅本人成功入库记录），按入库时间倒序
 * @param {number} limit 每页条数（默认 50）
 * @param {number} offset 偏移量
 */
export function listIngestHistory(limit = 50, offset = 0) {
  return api.get('/knowledge/ingest-history', { params: { limit, offset } })
}

/**
 * 入库历史详情：某条记录的解析文档内容（文本片段，含页码）
 * @param {string} recordId
 */
export function getIngestHistoryContent(recordId) {
  return api.get(`/knowledge/ingest-history/${encodeURIComponent(recordId)}/content`)
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

/**
 * 保存知识助手专用模型配置（与问答界面模型分离）
 * @param {{enabled:boolean, provider:string, base_url:string, model:string, api_key:string}} cfg
 */
export function saveAssistantLLM(cfg) {
  return api.post('/config/assistant-llm', cfg)
}

/**
 * 测试知识助手专用模型连接（使用已保存配置）
 */
export function testAssistantLLMConnection() {
  return api.post('/config/test-assistant-llm', {}, { timeout: 60000 })
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

// ============================================
// 对话/检索历史沉淀（llm-wiki 路径A：高频主题 → 私人综合词条）
// ============================================

/**
 * 对话沉淀预览：已沉淀私人词条 + 归纳出的高频候选主题
 * refresh=true 时调 LLM 归纳（稍慢），单独放宽超时
 */
export function getConversationTopics(refresh = true) {
  return api.get('/wiki/conversation/topics', { params: { refresh }, timeout: 120000 })
}

/**
 * 手动触发对话沉淀：把当前用户高频提问主题综合成私人 digest 词条（LLM 耗时长）
 */
export function compileConversationTopics() {
  return api.post('/wiki/conversation/compile', {}, { timeout: 300000 })
}

// ============================================
// 主问答语义缓存 API（当前用户专属：相似问题命中直回、绕过检索路由）
// ============================================

/**
 * 语义缓存面板：开关/阈值/条目数/累计命中 + 当前用户缓存列表
 */
export function getSemanticCache(limit = 50) {
  return api.get('/chat/cache', { params: { limit } })
}

/**
 * 开/关语义缓存（运行时覆盖）
 */
export function toggleSemanticCache(enabled) {
  return api.post('/chat/cache/toggle', { enabled })
}

/**
 * 清空当前用户的全部语义缓存
 */
export function clearSemanticCache() {
  return api.delete('/chat/cache')
}

/**
 * 删除当前用户的单条语义缓存
 */
export function deleteSemanticCacheEntry(entryId) {
  return api.delete(`/chat/cache/${entryId}`)
}

// ============================================
// 知识助手 Agent API（侧栏问答 + 人工确认编辑）
// ============================================

/**
 * 知识助手问答（SSE 流式）。
 * payload: { message, page_id, page_title, route_name, route_label, page_state, user_actions, history, session_id }
 * onEvent(ev) 事件：session / status / thought / tool / clarify / token / confirm / done / error
 */
export async function assistantChat(payload, onEvent, signal) {
  const token = localStorage.getItem('auth_token')
  const resp = await fetch('/api/wiki/assistant/chat', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(payload),
    signal,
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
 * 待确认编辑列表（可按 page_id 过滤）
 */
export function listAssistantEdits(pageId = '') {
  return api.get('/wiki/assistant/edits', { params: pageId ? { page_id: pageId } : {} })
}

/**
 * 确认并应用 Agent 提议的编辑（含向量重索引，放宽超时）
 */
export function approveAssistantEdit(editId) {
  return api.post(`/wiki/assistant/edits/${encodeURIComponent(editId)}/approve`, {}, { timeout: 120000 })
}

/**
 * 驳回 Agent 提议的编辑
 */
export function rejectAssistantEdit(editId) {
  return api.post(`/wiki/assistant/edits/${encodeURIComponent(editId)}/reject`)
}

/**
 * 历史助手会话列表（按最近更新倒序）
 */
export function listAssistantSessions() {
  return api.get('/wiki/assistant/sessions')
}

/**
 * 历史会话消息（含 meta，供回放思考/工具/追问/编辑卡）
 */
export function getAssistantSessionMessages(sessionId) {
  return api.get(`/wiki/assistant/sessions/${encodeURIComponent(sessionId)}/messages`)
}

/**
 * 删除历史会话（仅本人）
 */
export function deleteAssistantSession(sessionId) {
  return api.delete(`/wiki/assistant/sessions/${encodeURIComponent(sessionId)}`)
}

// ============================================
// Agent 可观测性 API（仅管理员）
// ============================================

/**
 * Agent 聚合指标：Token 消耗 / 响应延迟 / 成功率 / 工具调用次数
 * @param {number} days 统计窗口天数（默认 7）
 */
export function getObservabilityMetrics(days = 7) {
  return api.get('/observability/metrics', { params: { days } })
}

/** 当前登录用户的业务概览，不含任何技术追踪数据。 */
export function getMyObservabilityMetrics(days = 7) {
  return api.get('/observability/my-metrics', { params: { days } })
}

/** 当前登录用户的「上下文窗口 + 记忆模块」使用概览。 */
export function getMyContextOverview(days = 7) {
  return api.get('/observability/my-context', { params: { days } })
}

/**
 * Agent trace 列表（一次用户请求 = 一条 trace）
 * @param {object} params { agent, status, limit, offset }
 */
export function getObservabilityTraces(params = {}) {
  return api.get('/observability/traces', { params })
}

/**
 * trace 全链路详情：所有步骤 span（llm span 含完整 prompt 与 response）
 */
export function getObservabilityTrace(traceId) {
  return api.get(`/observability/traces/${encodeURIComponent(traceId)}`)
}

export function getObservabilityTraceLogs(traceId, params = {}) {
  return api.get(`/observability/traces/${encodeURIComponent(traceId)}/logs`, { params })
}

/**
 * Skill 预审记录（仅 ops/admin）：汇总 + 明细列表，拒绝行为重点暴露
 */
export function getSkillReviews(limit = 30) {
  return api.get('/observability/skill-reviews', { params: { limit } })
}

/**
 * 链接正文「内容润色」：按 Skill 配置（scope=polish）做确定性清洗，
 * 去角标/噪声行/多余空格，保留段落顺序与自然段，不新增内容。
 */
export function cleanArticle(text) {
  return api.post('/knowledge/clean-article', { text })
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
 * 忘记密码：凭「用户名 + 注册联系方式」验证身份后重置密码（免登录）
 */
export function forgotPassword(username, contact, newPassword) {
  return api.post('/auth/forgot-password', {
    username,
    contact,
    new_password: newPassword,
  })
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
 * 修改用户角色（管理员）：role 取值 user / admin / ops
 */
export function setUserRole(userId, role) {
  return api.put(`/auth/users/${userId}/role`, { role })
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

// ============================================
// Agent 回归评测 API（管理员）
// ============================================

/** 评测集列表（含用例数与最近运行） */
export function listEvalDatasets() {
  return api.get('/eval/datasets')
}

/** 新建评测集 */
export function createEvalDataset({ name, description = '' }) {
  return api.post('/eval/datasets', { name, description })
}

/** 删除评测集（级联删除用例/运行/结果） */
export function deleteEvalDataset(datasetId) {
  return api.delete(`/eval/datasets/${datasetId}`)
}

/** 评测集内用例列表 */
export function listEvalCases(datasetId) {
  return api.get(`/eval/datasets/${datasetId}/cases`)
}

/** 新增单条用例 */
export function createEvalCase(datasetId, payload) {
  return api.post(`/eval/datasets/${datasetId}/cases`, payload)
}

/** 删除单条用例 */
export function deleteEvalCase(caseId) {
  return api.delete(`/eval/cases/${caseId}`)
}

/** JSON 批量导入用例：cases=[{question, expected_keywords?, reference_answer?, expected_source?}] */
export function importEvalCases(datasetId, cases) {
  return api.post(`/eval/datasets/${datasetId}/cases/import`, { cases })
}

/** 从点踩反馈导入用例（去重） */
export function importEvalCasesFromFeedback(datasetId) {
  return api.post(`/eval/datasets/${datasetId}/cases/from-feedback`)
}

/** 发起评测运行，返回 { run_id, status } */
export function startEvalRun(datasetId) {
  return api.post(`/eval/datasets/${datasetId}/runs`)
}

/** 运行历史（含 summary 与进度计数） */
export function listEvalRuns(datasetId) {
  return api.get('/eval/runs', { params: { dataset_id: datasetId } })
}

/** 运行详情（run + 结果明细 + 上一次 finished run 环比） */
export function getEvalRun(runId) {
  return api.get(`/eval/runs/${runId}`)
}

// ---- Prompt / Skill 专项评测（静态断言 + Rubric 打分 + A/B + 优化上线）----

/** 可评测目标：系统提示词 + 当前管理员的用户技能 */
export function listPromptSkillTargets() {
  return api.get('/eval/prompt-skill/targets')
}

/** 系统提示词版本历史与当前生效版本 */
export function getPromptVersions() {
  return api.get('/eval/prompt-skill/prompt-versions')
}

/** 发起 Prompt/Skill 评测，返回 { eval_id, status } */
export function startPromptSkillEval(targetType, targetId) {
  return api.post('/eval/prompt-skill/evaluate', { target_type: targetType, target_id: targetId })
}

/** Prompt/Skill 评测历史（概要） */
export function listPromptSkillEvals(params = {}) {
  return api.get('/eval/prompt-skill/evals', { params })
}

/** Prompt/Skill 评测详情 */
export function getPromptSkillEval(evalId) {
  return api.get(`/eval/prompt-skill/evals/${encodeURIComponent(evalId)}`)
}

/** 对优化候选发起 A/B 行为对比 */
export function startPromptSkillAB(evalId, datasetId) {
  return api.post(`/eval/prompt-skill/evals/${encodeURIComponent(evalId)}/ab`, { dataset_id: datasetId })
}

/** 上线：Prompt 发布新版本 / Skill 更新内容 */
export function publishPromptSkill(evalId, content = null) {
  return api.post(`/eval/prompt-skill/evals/${encodeURIComponent(evalId)}/publish`, { content })
}


// ============================================
// CI/CD 流水线 API（管理员 / 运维角色）
// ============================================

/** 流水线阶段元信息（流程图渲染用） */
export function getPipelineStages() {
  return api.get('/pipeline/stages')
}

/** 读取流水线配置（EDD 阈值/评测集/各阶段命令） */
export function getPipelineSettings() {
  return api.get('/pipeline/settings')
}

/** 保存流水线配置 */
export function savePipelineSettings(payload) {
  return api.put('/pipeline/settings', payload)
}

/** 发起一次流水线运行，返回 { run_id, status } */
export function startPipelineRun(gitRef = '', trigger = 'manual') {
  return api.post('/pipeline/runs', { git_ref: gitRef, trigger })
}

/** 流水线运行历史 */
export function listPipelineRuns(limit = 20) {
  return api.get('/pipeline/runs', { params: { limit } })
}

/** 流水线运行详情（六阶段状态 + 日志 + EDD 门控结果） */
export function getPipelineRun(runId) {
  return api.get(`/pipeline/runs/${encodeURIComponent(runId)}`)
}

/** 人工中止流水线运行 */
export function abortPipelineRun(runId) {
  return api.post(`/pipeline/runs/${encodeURIComponent(runId)}/abort`)
}

// ============================================
// 模型网关 API（LLM Local Gateway，管理员 / 运维角色）
// ============================================

/** 网关总览：渠道/密钥计数 + 近 7 天统计 + 最近风险日志 */
export function getGatewayOverview() {
  return api.get('/gateway/overview')
}

/** 网关调用统计（仪表盘） */
export function getGatewayStats(days = 7) {
  return api.get('/gateway/stats', { params: { days } })
}

// ---- 上游管理（协议 + Base URL + Key + 成本层级 + 模型清单 + 权重/健康/延迟）----
export function listGatewayUpstreams() {
  return api.get('/gateway/upstreams')
}
export function createGatewayUpstream(payload) {
  return api.post('/gateway/upstreams', payload)
}
export function updateGatewayUpstream(upstreamId, payload) {
  return api.put(`/gateway/upstreams/${encodeURIComponent(upstreamId)}`, payload)
}
export function deleteGatewayUpstream(upstreamId) {
  return api.delete(`/gateway/upstreams/${encodeURIComponent(upstreamId)}`)
}
export function cloneGatewayUpstream(upstreamId) {
  return api.post(`/gateway/upstreams/${encodeURIComponent(upstreamId)}/clone`)
}
export function testGatewayUpstream(upstreamId, model = '') {
  return api.post(`/gateway/upstreams/${encodeURIComponent(upstreamId)}/test`, { model })
}
export function resetGatewayUpstreamHealth(upstreamId) {
  return api.post(`/gateway/upstreams/${encodeURIComponent(upstreamId)}/reset-health`)
}
export function listOpenRouterFreeModels() {
  return api.get('/gateway/openrouter-free')
}

// ---- 路由规则（模型模式 → 上游列表 + 负载均衡策略）----
export function listGatewayRoutingRules() {
  return api.get('/gateway/routing-rules')
}
export function createGatewayRoutingRule(payload) {
  return api.post('/gateway/routing-rules', payload)
}
export function updateGatewayRoutingRule(ruleId, payload) {
  return api.put(`/gateway/routing-rules/${encodeURIComponent(ruleId)}`, payload)
}
export function deleteGatewayRoutingRule(ruleId) {
  return api.delete(`/gateway/routing-rules/${encodeURIComponent(ruleId)}`)
}

// ---- 密钥与配额 ----
export function listGatewayKeys() {
  return api.get('/gateway/keys')
}
export function createGatewayKey(payload) {
  return api.post('/gateway/keys', payload)
}
export function updateGatewayKey(keyId, payload) {
  return api.put(`/gateway/keys/${encodeURIComponent(keyId)}`, payload)
}
export function deleteGatewayKey(keyId) {
  return api.delete(`/gateway/keys/${encodeURIComponent(keyId)}`)
}
export function resetGatewayKey(keyId) {
  return api.post(`/gateway/keys/${encodeURIComponent(keyId)}/reset`)
}

// ---- 请求日志与审计 ----
export function queryGatewayLogs(params = {}) {
  return api.get('/gateway/logs', { params })
}
export function getGatewayLog(logId) {
  return api.get(`/gateway/logs/${encodeURIComponent(logId)}`)
}
export function purgeGatewayLogs() {
  return api.delete('/gateway/logs')
}

// ---- 安全审计中心 ----
export function getGatewayRules() {
  return api.get('/gateway/rules')
}
export function getGatewaySettings() {
  return api.get('/gateway/settings')
}
export function saveGatewaySettings(payload) {
  return api.put('/gateway/settings', payload)
}
export function gatewayScanPreview(text) {
  return api.post('/gateway/scan-preview', { text })
}

// ---- （模型注册表与旧上游端点已退役：模型清单并入上游、连接配置归属上游）----

// ---- 业务路由（role → 规则 + 模型）----
export function listGatewayRoleRoutes() {
  return api.get('/gateway/role-routes')
}
export function upsertGatewayRoleRoute(role, payload) {
  return api.put(`/gateway/role-routes/${encodeURIComponent(role)}`, payload)
}
export function deleteGatewayRoleRoute(role) {
  return api.delete(`/gateway/role-routes/${encodeURIComponent(role)}`)
}

// ---- 当前用户可用规则（聊天页路由下拉，普通登录用户可访问）----
export function listMyGatewayRules(model = '') {
  return api.get('/gateway/my-rules', { params: { model } })
}

// ---- 聊天页模型下拉：匹配路由规则的上游模型清单（普通登录用户可访问）----
export function getMyChatModels() {
  return api.get('/gateway/my-chat-models')
}

// ---- 系统内部出网密钥：配置现状与轮换（管理员/运维）----
export function getGatewayInternalKey() {
  return api.get('/gateway/keys/internal')
}
export function rotateGatewayInternalKey() {
  return api.post('/gateway/keys/internal/rotate')
}

export default api

