"""
见字如面 - 向量数据库层 (FAISS + LangChain)
支持文档的增、删、查、存
"""
import hashlib
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS

from app.config import settings
from app.core.embeddings import EmbeddingFactory

logger = logging.getLogger(__name__)


class VectorStore:
    """FAISS 向量存储管理器"""

    def __init__(self):
        self.index_path = settings.VECTOR_STORE_PATH
        self.embeddings = EmbeddingFactory.create()
        self._store: Optional[FAISS] = None

        # 元数据文件 (存储原文与图片路径的映射)
        self.meta_path = str(Path(self.index_path).parent / "metadata.json")

        # 元数据内存缓存（按文件 mtime 失效），避免每次检索重复读盘解析
        self._meta_cache: Optional[Dict[str, dict]] = None
        self._meta_mtime: float = 0.0

    # 嵌入服务不可用的标记
    _EMBEDDING_UNAVAILABLE: bool = False

    def _load_store(self) -> Optional[FAISS]:
        """加载或创建向量库

        注意：如果 Embedding 服务（Ollama/API）未运行，
        会返回 None 而不是崩溃，后续操作会给出清晰提示。
        """
        if self._store is not None:
            return self._store
        if self._EMBEDDING_UNAVAILABLE:
            return None

        if os.path.exists(self.index_path):
            try:
                self._store = FAISS.load_local(
                    self.index_path,
                    self.embeddings,
                    allow_dangerous_deserialization=True,
                )
                return self._store
            except Exception as e:
                print(f"[向量库] 加载已有索引失败: {e}")
                self._store = None

        if self._store is None:
            try:
                # 创建一个空库: 先加一个占位文档再删除
                texts = ["初始化占位"]
                self._store = FAISS.from_texts(texts, self.embeddings)
                # 删除占位
                self._store.delete([self._store.index_to_docstore_id[0]])
            except Exception as e:
                err_msg = str(e).lower()
                if "connection refused" in err_msg or "connection error" in err_msg or "max retries exceeded" in err_msg:
                    print(f"[向量库] ⚠ Embedding 服务连接失败 ({self.embeddings.__class__.__name__})，向量库不可用")
                    print(f"[向量库]   请确保 Ollama 已启动（ollama serve）或 .env 中 EMBEDDING 配置正确")
                    print(f"[向量库]   应用将继续运行，使用知识库功能时需要 Embedding 服务可用后再重试")
                    self._EMBEDDING_UNAVAILABLE = True
                    self._store = None
                    return None
                raise

        return self._store

    def _save(self):
        """保存向量库到磁盘"""
        if self._store is None:
            return
        os.makedirs(Path(self.index_path).parent, exist_ok=True)
        self._store.save_local(self.index_path)

    def _load_metadata(self) -> Dict[str, dict]:
        """加载元数据（带 mtime 缓存，文件未变化时直接命中内存）"""
        try:
            mtime = os.path.getmtime(self.meta_path)
        except OSError:
            return {}
        if self._meta_cache is not None and mtime == self._meta_mtime:
            return self._meta_cache
        with open(self.meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self._meta_cache = data
        self._meta_mtime = mtime
        return data

    def _save_metadata(self, metadata: Dict[str, dict]):
        """保存元数据（同步更新缓存）"""
        os.makedirs(Path(self.meta_path).parent, exist_ok=True)
        with open(self.meta_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
        self._meta_cache = metadata
        self._meta_mtime = os.path.getmtime(self.meta_path)

    @staticmethod
    def result_key(text: str, metadata: dict) -> str:
        """生成检索结果去重 key：优先 doc_id，退化为来源+页码/分片+文本 hash。"""
        if metadata.get("doc_id"):
            return str(metadata["doc_id"])
        raw = "|".join([
            str(metadata.get("source_image", "")),
            str(metadata.get("page_number", "")),
            str(metadata.get("chunk_index", "")),
            hashlib.sha1((text or "").encode()).hexdigest()[:16],
        ])
        return raw

    def is_available(self) -> bool:
        """Embedding 服务与向量库是否可用"""
        return not self._EMBEDDING_UNAVAILABLE

    def initialize(self) -> bool:
        """尝试加载向量库，返回是否成功（供启动预热调用）"""
        return self._load_store() is not None

    def retry_init(self, force: bool = False) -> bool:
        """重置不可用标记并重新尝试初始化，返回是否成功

        Args:
            force: 强制重建 Embedding 实例（配置变更后使用）
        """
        if not force and not self._EMBEDDING_UNAVAILABLE and self._store is not None:
            return True
        self._EMBEDDING_UNAVAILABLE = False
        self._store = None
        # 重建 Embedding 实例，使页面保存的最新配置生效
        self.embeddings = EmbeddingFactory.create()
        try:
            return self._load_store() is not None
        except Exception:
            self._EMBEDDING_UNAVAILABLE = True
            raise

    def add_texts(
        self,
        texts: List[str],
        metadatas: Optional[List[dict]] = None,
    ) -> List[str]:
        """添加文本到向量库

        Args:
            texts: 文本列表
            metadatas: 元数据列表, 应包含 source_image (原始图片路径)

        Returns:
            ID 列表
        """
        import uuid

        store = self._load_store()
        if store is None:
            raise RuntimeError("向量库未初始化")

        if not texts:
            return []

        # 阿里云 DashScope text-embedding-v4 等接口限制单次 batch <= 10
        BATCH_SIZE = 10
        all_ids: List[str] = [str(uuid.uuid4()) for _ in texts]
        # 复制并注入 doc_id，使 FAISS docstore 与 metadata.json 保持一致的键
        metadatas = [
            {**(m if m else {}), "doc_id": doc_id}
            for m, doc_id in zip(
                metadatas if metadatas else [{}] * len(texts), all_ids
            )
        ]

        for i in range(0, len(texts), BATCH_SIZE):
            batch_texts = texts[i : i + BATCH_SIZE]
            batch_metadatas = metadatas[i : i + BATCH_SIZE]
            batch_ids = all_ids[i : i + BATCH_SIZE]
            store.add_texts(batch_texts, batch_metadatas, ids=batch_ids)

        self._save()

        # 同步元数据
        meta_map = self._load_metadata()
        for doc_id, meta in zip(all_ids, metadatas):
            meta_map[doc_id] = meta
        self._save_metadata(meta_map)

        return all_ids

    def _resolve_meta(self, meta_map: Dict[str, dict], doc) -> dict:
        """解析 chunk 的完整元数据

        优先按 doc_id 查 metadata.json；存量数据 FAISS 内无 doc_id，
        退化为按 source_image + page_number 匹配，避免同一文档多页时
        全部误取为第一页的元数据。
        """
        doc_id = doc.metadata.get("doc_id", "")
        meta = meta_map.get(doc_id)
        if meta is not None:
            return meta
        if not doc_id:
            source = doc.metadata.get("source_image", "")
            page_number = doc.metadata.get("page_number")
            if source:
                # 优先精确匹配来源+页码，保证 page_number 正确
                for m in meta_map.values():
                    if (
                        m.get("source_image") == source
                        and m.get("page_number") == page_number
                    ):
                        return m
                # 退化为按 source_image 匹配（兼容无 page_number 的旧数据）
                for m in meta_map.values():
                    if m.get("source_image") == source:
                        return m
        return doc.metadata

    def _scoped_store(
        self,
        store: FAISS,
        meta_map: Dict[str, dict],
        kb_ids: Optional[set],
        current_user_id: str,
    ) -> FAISS:
        """构建仅含可见条目的子向量库，使检索从一开始就不触及无权限数据。

        权限规则：
        - kb_ids 非 None 时仅保留指定知识库的条目
        - private 条目仅所有者可见（公共条目所有人可见）
        构建失败时抛异常，由调用方降级为全量检索+后置过滤。
        """
        allowed_ids = set()
        for doc_id, meta in meta_map.items():
            if kb_ids is not None and meta.get("kb_id", "") not in kb_ids:
                continue
            visibility = meta.get("visibility", "public")
            owner_id = meta.get("owner_id", "")
            if visibility == "private" and owner_id and owner_id != current_user_id:
                continue
            allowed_ids.add(doc_id)

        # 全部可见时无需构建子库
        if meta_map and len(allowed_ids) >= len(meta_map):
            return store

        import faiss
        import numpy as np
        from langchain_core.documents import Document
        from langchain_community.docstore.in_memory import InMemoryDocstore

        sub_index = faiss.IndexFlatL2(store.index.d)
        sub_docs: Dict[str, Document] = {}
        sub_i2d: Dict[int, str] = {}
        pos = 0
        for i in range(store.index.ntotal):
            doc_id = store.index_to_docstore_id.get(i)
            if doc_id not in allowed_ids:
                continue
            doc = store.docstore.search(doc_id)
            if not isinstance(doc, Document):
                continue
            vec = store.index.reconstruct(i)
            sub_index.add(np.asarray([vec], dtype=np.float32))
            sub_docs[doc_id] = doc
            sub_i2d[pos] = doc_id
            pos += 1
        return FAISS(self.embeddings, sub_index, InMemoryDocstore(sub_docs), sub_i2d)

    def similarity_search(
        self,
        query: str,
        k: int = None,
        current_user_id: str = "",
        kb_ids: Optional[set] = None,
    ) -> List[Tuple[str, dict, float]]:
        """相似度搜索

        Args:
            query: 查询文本
            k: 返回数量
            current_user_id: 当前用户 ID，用于过滤私人知识
            kb_ids: 限定只检索这些知识库的条目（None 表示不限）

        Returns:
            [(text, metadata, score), ...]
        """
        store = self._load_store()
        if store is None:
            return []

        k = k or settings.TOP_K_RETRIEVAL
        meta_map = self._load_metadata()

        # 权限前置：先在可见条目构建的子库中检索
        try:
            scoped = self._scoped_store(store, meta_map, kb_ids, current_user_id)
        except Exception as e:
            logger.warning("构建可见性子向量库失败，降级为全量检索+后置过滤: %s", e)
            scoped = store

        results = scoped.similarity_search_with_relevance_scores(query, k=k * 2)

        output = []
        for doc, score in results:
            meta = self._resolve_meta(meta_map, doc)
            # 知识库过滤：仅保留可见库的条目（子库已满足，此处为降级路径兜底）
            if kb_ids is not None and meta.get("kb_id", "") not in kb_ids:
                continue
            # 兼容旧数据可见性过滤：公共的所有人可见，私人的仅所有者可见
            visibility = meta.get("visibility", "public")
            owner_id = meta.get("owner_id", "")
            if visibility == "private" and owner_id and owner_id != current_user_id:
                continue
            output.append((doc.page_content, meta, score))
            if len(output) >= k:
                break

        return output

    def search_broad(
        self,
        query: str,
        k: int,
        current_user_id: str = "",
        kb_ids: Optional[set] = None,
    ) -> List[Tuple[str, dict, float]]:
        """大范围检索，供知识库路由聚合打分使用。
        同样只在用户可见的条目范围内检索，不触及他人私人知识。"""
        store = self._load_store()
        if store is None:
            return []

        meta_map = self._load_metadata()
        try:
            scoped = self._scoped_store(store, meta_map, kb_ids, current_user_id)
        except Exception as e:
            logger.warning("构建可见性子向量库失败，降级为全量检索+后置过滤: %s", e)
            scoped = store

        results = scoped.similarity_search_with_relevance_scores(query, k=k)
        output = []
        for doc, score in results:
            meta = self._resolve_meta(meta_map, doc)
            # 降级路径兜底过滤
            if kb_ids is not None and meta.get("kb_id", "") not in kb_ids:
                continue
            visibility = meta.get("visibility", "public")
            owner_id = meta.get("owner_id", "")
            if visibility == "private" and owner_id and owner_id != current_user_id:
                continue
            output.append((doc.page_content, meta, score))
        return output

    def delete_by_source(self, source_image: str) -> bool:
        """根据原始图片删除所有相关的文档片段

        Args:
            source_image: 原始图片路径

        Returns:
            是否成功
        """
        store = self._load_store()
        if store is None:
            return False

        meta_map = self._load_metadata()
        to_delete = [
            doc_id
            for doc_id, meta in meta_map.items()
            if meta.get("source_image") == source_image
        ]

        if not to_delete:
            return False

        store.delete(to_delete)
        for doc_id in to_delete:
            meta_map.pop(doc_id, None)

        self._save_metadata(meta_map)
        self._save()
        return True

    def delete_by_kb(self, kb_id: str) -> bool:
        """删除指定知识库的所有文档片段"""
        store = self._load_store()
        if store is None:
            return False

        meta_map = self._load_metadata()
        to_delete = [
            doc_id
            for doc_id, meta in meta_map.items()
            if meta.get("kb_id") == kb_id
        ]
        if not to_delete:
            return False

        store.delete(to_delete)
        for doc_id in to_delete:
            meta_map.pop(doc_id, None)

        self._save_metadata(meta_map)
        self._save()
        return True

    def move_source(self, source_image: str, kb_id: str, visibility: str) -> int:
        """将指定来源文件的全部 chunk 移动到目标知识库

        仅更新元数据（kb_id 与 visibility），向量本身不变，无需重建索引。

        Args:
            source_image: 原始文件路径
            kb_id: 目标知识库 ID
            visibility: 目标知识库可见性 public | private

        Returns:
            移动的 chunk 数量
        """
        meta_map = self._load_metadata()
        moved = 0
        for meta in meta_map.values():
            if meta.get("source_image") == source_image:
                meta["kb_id"] = kb_id
                meta["visibility"] = visibility
                moved += 1
        if moved:
            self._save_metadata(meta_map)
        return moved

    def clear_all(self):
        """清空向量库"""
        self._store = None
        if os.path.exists(self.index_path):
            shutil.rmtree(self.index_path, ignore_errors=True)
        if os.path.exists(self.meta_path):
            os.remove(self.meta_path)

    def get_document_count(self) -> int:
        """获取文档数量"""
        store = self._load_store()
        if store is None:
            return 0
        return store.index.ntotal

    def get_all_entries(self) -> list[dict]:
        """获取所有知识库条目（按来源图片分组）

        Returns:
            [{
                "source_image": str,
                "doc_ids": list[str],
                "chunk_count": int,
            }, ...]
        """
        meta_map = self._load_metadata()
        groups: dict[str, dict] = {}
        for doc_id, meta in meta_map.items():
            src = meta.get("source_image", "unknown")
            if src not in groups:
                groups[src] = {
                    "source_image": src,
                    "doc_ids": [],
                    "chunk_count": 0,
                    "kb_id": meta.get("kb_id", ""),
                }
            groups[src]["doc_ids"].append(doc_id)
            groups[src]["chunk_count"] += 1
        return list(groups.values())

    def split_text(
        self,
        text: str,
        source_image: str,
        visibility: str = "public",
        owner_id: str = "",
        **extra_metadata,
    ) -> List[Tuple[str, dict]]:
        """将长文本分割成块

        Args:
            text: 原始文本
            source_image: 来源图片路径
            visibility: 可见性 public | private
            owner_id: 所有者用户 ID
            **extra_metadata: 额外的元数据字段（如 page_number、pdf_preview_path）

        Returns:
            [(chunk, metadata), ...]
        """
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=settings.CHUNK_SIZE,
            chunk_overlap=settings.CHUNK_OVERLAP,
            separators=["\n\n", "\n", "。", "，", " ", ""],
        )

        chunks = splitter.split_text(text)
        result = []
        for chunk in chunks:
            if chunk.strip():
                result.append((chunk, {
                    "source_image": source_image,
                    "visibility": visibility,
                    "owner_id": owner_id,
                    **extra_metadata,
                }))
        return result

    def update_pdf_preview_path(self, source_image: str, pdf_preview_path: str) -> int:
        """更新指定来源全部 chunk 的 pdf_preview_path 元数据。

        同时更新 metadata.json 与 FAISS docstore，确保检索返回的来源信息带新路径。
        """
        meta_map = self._load_metadata()
        updated_meta = 0
        for meta in meta_map.values():
            if meta.get("source_image") == source_image:
                meta["pdf_preview_path"] = pdf_preview_path
                updated_meta += 1
        if updated_meta:
            self._save_metadata(meta_map)

        store = self._load_store()
        updated_store = 0
        if store and hasattr(store, "docstore") and hasattr(store.docstore, "_docs"):
            for doc in store.docstore._docs.values():
                if doc.metadata.get("source_image") == source_image:
                    doc.metadata["pdf_preview_path"] = pdf_preview_path
                    updated_store += 1
            if updated_store:
                self._save()

        return updated_meta


# 全局单例
vector_store = VectorStore()
