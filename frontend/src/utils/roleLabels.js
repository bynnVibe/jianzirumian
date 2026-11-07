// 网关业务角色（role）→ 中文场景名：日志审计/业务路由共用，避免各处文案漂移
export const ROLE_LABELS = {
  chat_answer: '聊天回答',
  assistant: '知识助手',
  intent_rewrite: '意图改写',
  evidence_check: '证据校验',
  wiki_compile: '知识编译',
  embed: '向量化',
  rerank: '重排序',
  ocr: 'OCR 识别',
  memory_extract: '记忆抽取',
  session_summary: '会话摘要',
  knowledge_summarize: '知识点总结',
  text_polish: 'OCR 润色',
  content_polish: '内容润色',
}

export function roleLabel(role) {
  return ROLE_LABELS[role] || role || ''
}
