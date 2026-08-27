"""
见字如面 - Pydantic 数据模型
"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


# ============================================
# 会话相关
# ============================================
class SessionCreate(BaseModel):
    """创建会话请求"""
    pass


class SessionInfo(BaseModel):
    """会话摘要"""
    session_id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int


class SessionList(BaseModel):
    """会话列表"""
    sessions: List[SessionInfo]
    current_session_id: Optional[str] = None


# ============================================
# 聊天相关
# ============================================
class ChatRequest(BaseModel):
    """聊天请求"""
    session_id: Optional[str] = None
    query: str = Field(..., min_length=1, max_length=2000)
    use_knowledge: bool = True
    use_search: bool = False
    llm_provider: Optional[str] = None


class SourceItem(BaseModel):
    """来源信息"""
    index: int
    text: str
    source_image: Optional[str] = None
    score: float = 0.0
    # 文档跳转所需字段（与当下检索返回保持一致，避免历史接口序列化丢失）
    source_type: Optional[str] = None
    page_number: Optional[int] = None
    pdf_preview_path: Optional[str] = None
    relevance: Optional[int] = None
    kb_id: Optional[str] = None
    kb_name: Optional[str] = None
    # llm-wiki 百科卡片标记（来源为预编译知识时前端可差异化展示）
    is_wiki_page: bool = False
    wiki_title: Optional[str] = None


class SearchResultItem(BaseModel):
    """搜索结果项"""
    title: str
    url: str
    snippet: str


class MessageResponse(BaseModel):
    """消息响应"""
    message_id: Optional[str] = None
    role: str
    content: str
    sources: List[SourceItem] = []
    search_results: List[SearchResultItem] = []
    use_knowledge: bool = False
    use_search: bool = False
    feedback: Optional[dict] = None  # {rating, comment} 当前用户的反馈
    attachments: List[dict] = []  # 用户消息附件元数据 [{file_id, filename, kind}]


class FeedbackRequest(BaseModel):
    """回答反馈请求"""
    session_id: str
    message_id: str
    rating: str  # like | dislike
    comment: str = ""


class ChatHistoryResponse(BaseModel):
    """聊天历史响应"""
    session_id: str
    title: str
    messages: List[MessageResponse]


# ============================================
# 知识上传相关
# ============================================
class UploadResponse(BaseModel):
    """上传响应"""
    filename: str
    title: str = ""  # 用户自定义标题
    image_path: str
    ocr_text: str
    chunk_count: int
    doc_ids: List[str]
    success: bool
    visibility: str = "public"
    source_type: str = "image"
    pages: Optional[List[dict]] = None
    pdf_preview_path: Optional[str] = None


class OCRPreviewResponse(BaseModel):
    """OCR 预览响应"""
    ocr_text: str


class DocumentPage(BaseModel):
    """文档单页内容"""
    page_number: int
    text: str
    tables: List[str] = []
    markdown: str


class DocumentPreviewResponse(BaseModel):
    """文档分页预览响应"""
    filename: str
    file_path: str
    doc_type: str
    total_pages: int
    pages: List[DocumentPage]
    pdf_preview_path: Optional[str] = None


class SaveDocumentPageItem(BaseModel):
    """保存文档页面项"""
    page_number: int
    text: str


class SaveDocumentPagesRequest(BaseModel):
    """保存文档页面请求"""
    file_path: str
    doc_type: str
    visibility: str = "public"
    kb_id: str = ""
    title: str = ""  # 用户自定义标题
    pages: List[SaveDocumentPageItem]


class UploadRecord(BaseModel):
    """上传记录"""
    id: str
    filename: str
    title: str = ""  # 用户自定义标题，用于知识库管理搜索与展示
    image_path: str
    ocr_text: str
    ocr_provider: str
    chunk_count: int = 0
    doc_ids: List[str] = []
    visibility: str = "public"
    owner_id: str = ""
    source_type: str = "image"
    created_at: str
    updated_at: str
    pages: Optional[List[dict]] = None
    pdf_preview_path: Optional[str] = None


class UploadRecordList(BaseModel):
    """上传记录列表"""
    records: List[UploadRecord]
    total: int


class UploadRecordUpdate(BaseModel):
    """更新上传记录"""
    ocr_text: str


class KnowledgeStats(BaseModel):
    """知识库统计"""
    document_count: int
    total_entries: int


class KnowledgeEntryInfo(BaseModel):
    """知识库条目信息（按来源图片分组）"""
    source_image: str
    filename: str
    title: str = ""  # 用户自定义标题，用于知识库管理搜索与展示
    chunk_count: int
    text_preview: str
    created_at: str = ""
    doc_ids: List[str] = []
    visibility: str = "public"
    owner_id: str = ""
    source_type: str = "image"
    pdf_preview_path: Optional[str] = None
    pages: Optional[List[dict]] = None  # 文档分页内容（Word/PDF）
    kb_id: str = ""    # 所属主题知识库
    kb_name: str = ""  # 所属知识库名称


class KnowledgeEntryList(BaseModel):
    """知识库条目列表"""
    entries: List[KnowledgeEntryInfo]
    total: int


# ============================================
# 配置相关
# ============================================
class SystemConfig(BaseModel):
    """系统配置"""
    llm_provider: str
    ocr_provider: str
    search_provider: str
    available_llm_providers: List[str] = ["ollama", "openai", "custom"]
    available_ocr_providers: List[str] = ["local", "aliyun", "custom_api"]
    available_search_providers: List[str] = ["duckduckgo", "bing", "serpapi"]
