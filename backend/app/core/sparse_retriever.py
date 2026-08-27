"""
见字如面 - 轻量 BM25 稀疏检索

不引入额外依赖：从 FAISS docstore + metadata.json 构建稀疏索引，按 metadata
mtime 与 FAISS 文档数量自动失效。用于与稠密向量召回做 RRF 融合。
"""
import math
import os
import re
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

from langchain_core.documents import Document

from app.core.vector_store import vector_store

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    """中英文混合 tokenizer：英文数字按词，中文单字 + 相邻二元片段。"""
    if not text:
        return []
    base = [m.group(0).lower() for m in _TOKEN_RE.finditer(text)]
    tokens: list[str] = []
    zh_run: list[str] = []
    for tok in base:
        if "\u4e00" <= tok <= "\u9fff":
            zh_run.append(tok)
            tokens.append(tok)
        else:
            if len(zh_run) >= 2:
                tokens.extend("".join(zh_run[i:i + 2]) for i in range(len(zh_run) - 1))
            zh_run = []
            if len(tok) > 1:
                tokens.append(tok)
    if len(zh_run) >= 2:
        tokens.extend("".join(zh_run[i:i + 2]) for i in range(len(zh_run) - 1))
    return tokens


class SparseRetriever:
    def __init__(self):
        self._mtime = 0.0
        self._ntotal = -1
        self._docs: list[dict] = []
        self._doc_freq: Counter[str] = Counter()
        self._avg_len = 0.0

    @staticmethod
    def _allowed(meta: dict, current_user_id: str, kb_ids: Optional[set]) -> bool:
        if kb_ids is not None and meta.get("kb_id", "") not in kb_ids:
            return False
        if meta.get("visibility", "public") == "private":
            owner_id = meta.get("owner_id", "")
            if owner_id and owner_id != current_user_id:
                return False
        return True

    def _ensure_index(self):
        store = vector_store._load_store()
        if store is None:
            self._docs = []
            return
        try:
            mtime = os.path.getmtime(vector_store.meta_path)
        except OSError:
            mtime = 0.0
        ntotal = getattr(store.index, "ntotal", 0)
        if self._docs and self._mtime == mtime and self._ntotal == ntotal:
            return

        meta_map = vector_store._load_metadata()
        docs: list[dict] = []
        doc_freq: Counter[str] = Counter()
        total_len = 0

        for pos in range(ntotal):
            doc_id = store.index_to_docstore_id.get(pos)
            doc = store.docstore.search(doc_id) if doc_id else None
            if not isinstance(doc, Document):
                continue
            meta = vector_store._resolve_meta(meta_map, doc)
            text = doc.page_content or ""
            tokens = tokenize(text)
            if not tokens:
                continue
            tf = Counter(tokens)
            for token in tf:
                doc_freq[token] += 1
            total_len += len(tokens)
            docs.append({
                "doc_id": doc_id,
                "text": text,
                "meta": meta,
                "tf": tf,
                "length": len(tokens),
            })

        self._docs = docs
        self._doc_freq = doc_freq
        self._avg_len = total_len / len(docs) if docs else 0.0
        self._mtime = mtime
        self._ntotal = ntotal

    def search(
        self,
        query: str,
        k: int,
        current_user_id: str = "",
        kb_ids: Optional[set] = None,
    ) -> List[Tuple[str, dict, float]]:
        self._ensure_index()
        query_tokens = tokenize(query)
        if not query_tokens or not self._docs:
            return []

        qtf = Counter(query_tokens)
        n_docs = len(self._docs)
        k1 = 1.5
        b = 0.75
        scores: Dict[int, float] = defaultdict(float)

        for token, q_weight in qtf.items():
            df = self._doc_freq.get(token, 0)
            if df <= 0:
                continue
            idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
            for idx, doc in enumerate(self._docs):
                if not self._allowed(doc["meta"], current_user_id, kb_ids):
                    continue
                freq = doc["tf"].get(token, 0)
                if freq <= 0:
                    continue
                denom = freq + k1 * (1 - b + b * doc["length"] / (self._avg_len or 1))
                scores[idx] += q_weight * idf * (freq * (k1 + 1) / denom)

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:k]
        return [
            (self._docs[idx]["text"], self._docs[idx]["meta"], score)
            for idx, score in ranked
            if score > 0
        ]


sparse_retriever = SparseRetriever()
