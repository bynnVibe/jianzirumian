"""
见字如面 - 知识图谱服务

图谱与 llm-wiki 互相绑定：节点/边主要从 wiki_pages.analysis 的 entities 与
entity_relations 聚合生成（digest 词条的「实体关联」段落 likewise 解析为边），
wiki 编译落库后自动刷新；同时支持手动新建节点/关系、JSONL 导入与清空。
手动维护的节点/边带 origin=manual 标记，重建时保留。

- 节点溯源：wiki 派生节点/边携带 sources（百科页 + 原始文档），前端信息卡可点击查看文档内容；
- 后台重建：start_rebuild_background 线程内重建 + 预计算节点向量，接口立即返回；
- 语义搜索：节点向量持久化于 kb_graph_vectors（重建时后台预热），请求期仅向量化查询词，
  避免全量 embed 串行网关往返超时；
- 双存储：SQLite 本地一份 + Redis 缓存一份（insight_cache），页面加载优先读缓存。
"""
import base64
import hashlib
import json
import logging
import re
import threading
import uuid
from datetime import datetime

from app.core import db
from app.core.insight_cache import cache_delete, cache_get, cache_set, graph_key

logger = logging.getLogger("jianziruyang.kb_graph")

NODE_TYPES = ("person", "org", "product", "concept", "topic")

# 后台重建任务登记：kb_id -> {status, started_at, finished_at, error}
_JOBS: dict = {}
_JOB_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now().isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _load(kb_id: str) -> dict:
    row = db.query_one("SELECT data, updated_at FROM kb_graphs WHERE kb_id = ?", (kb_id,))
    if not row:
        return {"nodes": [], "edges": [], "updated_at": ""}
    try:
        data = json.loads(row["data"] or "{}")
    except (json.JSONDecodeError, ValueError):
        data = {}
    data.setdefault("nodes", [])
    data.setdefault("edges", [])
    data["updated_at"] = row["updated_at"]
    return data


def _save(kb_id: str, data: dict) -> dict:
    data.pop("updated_at", None)
    now = _now()
    db.execute(
        "INSERT INTO kb_graphs (kb_id, data, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(kb_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
        (kb_id, json.dumps(data, ensure_ascii=False), now),
    )
    cache_delete(graph_key(kb_id))
    data["updated_at"] = now
    return data


def get_graph(kb_id: str) -> dict:
    """读取图谱：Redis 缓存优先，未命中回源 SQLite 并回写缓存。"""
    cached = cache_get(graph_key(kb_id))
    if cached is not None:
        cached.setdefault("nodes", [])
        cached.setdefault("edges", [])
        return cached
    data = _load(kb_id)
    payload = {"nodes": data["nodes"], "edges": data["edges"], "updated_at": data.get("updated_at", "")}
    cache_set(graph_key(kb_id), payload)
    return data


def job_status(kb_id: str) -> dict | None:
    with _JOB_LOCK:
        job = _JOBS.get(kb_id)
        return dict(job) if job else None


def _merge_sources(item: dict, sources) -> None:
    """合并溯源信息（去重、上限 6 条）。"""
    if not sources:
        return
    cur = item.setdefault("sources", [])
    for s in sources:
        if s not in cur:
            cur.append(s)
        if len(cur) >= 6:
            break


def _add_node(nodes: dict, name: str, ntype: str, note: str, origin: str, sources=None) -> dict:
    name = (name or "").strip()[:80]
    if not name:
        return None
    if name in nodes:
        node = nodes[name]
        if note and not node.get("note"):
            node["note"] = note[:300]
        if sources:
            _merge_sources(node, sources)
        return node
    node = {
        "id": _new_id("n"),
        "name": name,
        "type": ntype if ntype in NODE_TYPES else "concept",
        "note": (note or "")[:300],
        "origin": origin,
    }
    if sources:
        _merge_sources(node, sources)
    nodes[name] = node
    return node


def _add_edge(edges: dict, source: str, target: str, label: str, origin: str, sources=None) -> None:
    source, target = (source or "").strip(), (target or "").strip()
    label = (label or "").strip()[:60]
    if not source or not target or source == target:
        return
    key = (source, target, label)
    if key not in edges:
        edges[key] = {
            "id": _new_id("e"),
            "source": source,
            "target": target,
            "label": label or "关联",
            "origin": origin,
        }
        if sources:
            _merge_sources(edges[key], sources)


_REL_LINE = re.compile(r"^\s*[-*]\s*(.+?)\s*(?:→|->)\s*(.+?)\s*[:：]\s*(.+)$", re.MULTILINE)


def _page_sources(row: dict, title_by_image: dict) -> list:
    """wiki 页的溯源清单：百科页自身 + 其原始文档来源（供信息卡点击查看详情）。"""
    sources = [{"kind": "wiki", "id": row.get("id", ""), "title": row.get("title", "")}]
    try:
        refs = json.loads(row.get("source_refs") or "[]")
    except (json.JSONDecodeError, ValueError):
        refs = []
    for ref in refs:
        if not ref:
            continue
        sources.append({
            "kind": "doc",
            "title": title_by_image.get(ref, ref.rsplit("/", 1)[-1]),
            "path": ref,
        })
        if len(sources) >= 6:
            break
    return sources


def _title_by_image(kb_id: str) -> dict:
    from app.core.vector_store import vector_store
    from app.services.records import upload_records
    images = {
        meta.get("source_image", "")
        for meta in vector_store._load_metadata().values()
        if meta.get("kb_id") == kb_id
    }
    out = {}
    if not images:
        return out
    all_records, _ = upload_records.list_records(limit=10000, offset=0)
    for rec in all_records:
        if rec["image_path"] in images:
            out[rec["image_path"]] = (rec.get("title") or "").strip() or rec.get("filename", "")
    return out


def rebuild_from_wiki(kb_id: str) -> dict:
    """从 llm-wiki 编译产物聚合图谱；保留手动节点/边，替换 wiki 派生部分。"""
    old = _load(kb_id)
    nodes = {n["name"]: n for n in old["nodes"] if n.get("origin") == "manual"}
    edges = {(e["source"], e["target"], e["label"]): e
             for e in old["edges"] if e.get("origin") == "manual"}

    rows = db.query(
        "SELECT id, page_type, title, content, analysis, source_refs FROM wiki_pages WHERE kb_id = ?",
        (kb_id,),
    )
    title_by_image = _title_by_image(kb_id)
    for row in rows:
        try:
            analysis = json.loads(row["analysis"] or "{}")
        except (json.JSONDecodeError, ValueError):
            analysis = {}
        page_sources = _page_sources(row, title_by_image)
        entities = analysis.get("entities") or []
        names = []
        for ent in entities:
            if not isinstance(ent, dict):
                continue
            node = _add_node(nodes, ent.get("name"), ent.get("type"), ent.get("note"), "wiki", page_sources)
            if node:
                names.append(node["name"])

        relations = analysis.get("entity_relations") or []
        has_relations = False
        for rel in relations:
            if not isinstance(rel, dict):
                continue
            src = _add_node(nodes, rel.get("source"), "concept", "", "wiki", page_sources)
            dst = _add_node(nodes, rel.get("target"), "concept", "", "wiki", page_sources)
            if src and dst:
                _add_edge(edges, src["name"], dst["name"], rel.get("label"), "wiki", page_sources)
                has_relations = True

        # digest 词条的「实体关联」Markdown 段落解析为带标签的边
        if row["page_type"] == "digest":
            for m in _REL_LINE.finditer(row["content"] or ""):
                src = _add_node(nodes, m.group(1), "concept", "", "wiki", page_sources)
                dst = _add_node(nodes, m.group(2), "concept", "", "wiki", page_sources)
                if src and dst:
                    _add_edge(edges, src["name"], dst["name"], m.group(3), "wiki", page_sources)
                    has_relations = True

        # 旧版分析无 entity_relations：同来源实体共现兜底（限 12 对，避免噪声）
        if not has_relations and len(names) > 1:
            pairs = 0
            for i in range(len(names)):
                for j in range(i + 1, len(names)):
                    _add_edge(edges, names[i], names[j], "相关", "wiki", page_sources)
                    pairs += 1
                    if pairs >= 12:
                        break
                if pairs >= 12:
                    break

    data = {"nodes": list(nodes.values()), "edges": list(edges.values())}
    saved = _save(kb_id, data)
    logger.info("[KB_GRAPH] 重建图谱 kb=%s nodes=%d edges=%d (wiki_pages=%d)",
                kb_id, len(saved["nodes"]), len(saved["edges"]), len(rows))
    return saved


def start_rebuild_background(kb_id: str) -> bool:
    """后台线程重建索引 + 预热节点向量（不阻塞请求）；已有任务在跑返回 False。"""
    with _JOB_LOCK:
        job = _JOBS.get(kb_id)
        if job and job.get("status") == "running":
            return False
        _JOBS[kb_id] = {"status": "running", "started_at": _now(), "finished_at": "", "error": ""}

    def _worker():
        try:
            rebuild_from_wiki(kb_id)
            prewarm_vectors(kb_id)
            with _JOB_LOCK:
                _JOBS[kb_id] = {"status": "done", "started_at": _JOBS[kb_id]["started_at"],
                                "finished_at": _now(), "error": ""}
        except Exception as e:
            logger.warning("[KB_GRAPH] 后台重建失败 kb=%s: %s", kb_id, e)
            with _JOB_LOCK:
                _JOBS[kb_id] = {"status": "failed", "started_at": _JOBS[kb_id]["started_at"],
                                "finished_at": _now(), "error": str(e)}

    threading.Thread(target=_worker, daemon=True, name=f"graph-rebuild-{kb_id[:8]}").start()
    logger.info("[KB_GRAPH] 后台重建任务已启动 kb=%s", kb_id)
    return True


def refresh_kb(kb_id: str) -> None:
    """wiki 编译完成后的静默刷新钩子：失败不影响编译主链路。"""
    if not kb_id:
        return
    try:
        rebuild_from_wiki(kb_id)
    except Exception as e:
        logger.warning("[KB_GRAPH] 自动刷新失败 kb=%s: %s", kb_id, e)


def clear_graph(kb_id: str) -> None:
    db.execute("DELETE FROM kb_graphs WHERE kb_id = ?", (kb_id,))
    db.execute("DELETE FROM kb_graph_vectors WHERE kb_id = ?", (kb_id,))
    cache_delete(graph_key(kb_id))


def add_node(kb_id: str, name: str, ntype: str, note: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("节点名称不能为空")
    data = _load(kb_id)
    if any(n["name"] == name for n in data["nodes"]):
        raise ValueError(f"节点「{name}」已存在")
    node = _add_node({n["name"]: n for n in data["nodes"]}, name, ntype, note, "manual")
    data["nodes"].append(node)
    _save(kb_id, data)
    return node


def delete_node(kb_id: str, node_id: str) -> bool:
    data = _load(kb_id)
    node = next((n for n in data["nodes"] if n["id"] == node_id), None)
    if not node:
        return False
    data["nodes"] = [n for n in data["nodes"] if n["id"] != node_id]
    data["edges"] = [e for e in data["edges"]
                     if e["source"] != node["name"] and e["target"] != node["name"]]
    _save(kb_id, data)
    db.execute("DELETE FROM kb_graph_vectors WHERE kb_id = ? AND node_id = ?", (kb_id, node_id))
    return True


def add_edge(kb_id: str, source: str, target: str, label: str) -> dict:
    data = _load(kb_id)
    names = {n["name"] for n in data["nodes"]}
    source, target = (source or "").strip(), (target or "").strip()
    if source not in names or target not in names:
        raise ValueError("关系的起点/终点节点不存在，请先创建节点")
    if source == target:
        raise ValueError("关系起点与终点不能相同")
    edge = {
        "id": _new_id("e"),
        "source": source,
        "target": target,
        "label": (label or "").strip()[:60] or "关联",
        "origin": "manual",
    }
    data["edges"].append(edge)
    _save(kb_id, data)
    return edge


def delete_edge(kb_id: str, edge_id: str) -> bool:
    data = _load(kb_id)
    before = len(data["edges"])
    data["edges"] = [e for e in data["edges"] if e["id"] != edge_id]
    if len(data["edges"]) == before:
        return False
    _save(kb_id, data)
    return True


def import_jsonl(kb_id: str, text: str) -> dict:
    """每行一个 JSON：{"kind":"node","name","type","note"} 或
    {"kind":"edge","source","target","label"}（source/target 为节点名）。"""
    data = _load(kb_id)
    nodes = {n["name"]: n for n in data["nodes"]}
    edges = {(e["source"], e["target"], e["label"]): e for e in data["edges"]}
    added_nodes = added_edges = 0
    errors = []
    for lineno, line in enumerate((text or "").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            errors.append(f"第 {lineno} 行不是合法 JSON")
            continue
        kind = item.get("kind") or item.get("type_kind") or ""
        if kind == "node" or ("name" in item and "source" not in item):
            before = len(nodes)
            _add_node(nodes, item.get("name"), item.get("type"), item.get("note"), "manual")
            added_nodes += len(nodes) - before
        elif kind == "edge" or ("source" in item and "target" in item):
            src = (item.get("source") or "").strip()
            dst = (item.get("target") or "").strip()
            if src not in nodes:
                _add_node(nodes, src, "concept", "", "manual")
            if dst not in nodes:
                _add_node(nodes, dst, "concept", "", "manual")
            before = len(edges)
            _add_edge(edges, src, dst, item.get("label"), "manual")
            added_edges += len(edges) - before
        else:
            errors.append(f"第 {lineno} 行缺少 kind/name/source 字段")
    data["nodes"] = list(nodes.values())
    data["edges"] = list(edges.values())
    _save(kb_id, data)
    return {"added_nodes": added_nodes, "added_edges": added_edges, "errors": errors}


def _node_text(node: dict) -> str:
    return f"{node['name']} {node.get('note') or ''}".strip()


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _pack(vec: list) -> str:
    """float32 打包 base64，压缩存储体积。"""
    import array
    return base64.b64encode(array.array("f", vec).tobytes()).decode("ascii")


def _unpack(blob: str) -> list:
    import array
    arr = array.array("f")
    arr.frombytes(base64.b64decode(blob.encode("ascii")))
    return arr.tolist()


def _embed_texts(texts: list) -> list:
    """分批 embed（单批失败重试一次）；仍失败则抛出由上层处理。"""
    from app.core.vector_store import vector_store
    out: list = []
    for i in range(0, len(texts), 20):
        batch = texts[i:i + 20]
        try:
            out.extend(vector_store.embeddings.embed_documents(batch))
        except Exception:
            logger.warning("[KB_GRAPH] 节点向量批次 embed 失败，重试一次: batch=%d", len(batch))
            out.extend(vector_store.embeddings.embed_documents(batch))
    return out


def _store_vectors(kb_id: str, items: list) -> None:
    """items: [(node_id, text, vec)] 落库 kb_graph_vectors。"""
    if not items:
        return
    now = _now()
    db.executemany(
        "INSERT INTO kb_graph_vectors (kb_id, node_id, text_hash, vec, updated_at)"
        " VALUES (?,?,?,?,?) ON CONFLICT(kb_id, node_id) DO UPDATE SET"
        " text_hash=excluded.text_hash, vec=excluded.vec, updated_at=excluded.updated_at",
        [(kb_id, nid, _text_hash(text), _pack(vec), now) for nid, text, vec in items],
    )


def prewarm_vectors(kb_id: str) -> int:
    """后台预热：为缺失/文本变化的节点补算向量（重建索引后调用）。返回补算数。"""
    data = _load(kb_id)
    if not data["nodes"]:
        return 0
    stored = {r["node_id"]: r for r in db.query(
        "SELECT node_id, text_hash, vec FROM kb_graph_vectors WHERE kb_id = ?", (kb_id,))}
    missing = [(n, _node_text(n)) for n in data["nodes"]
               if not (stored.get(n["id"]) and stored[n["id"]]["text_hash"] == _text_hash(_node_text(n)))]
    if not missing:
        return 0
    vectors = _embed_texts([t for _, t in missing])
    _store_vectors(kb_id, [(n["id"], t, v) for (n, t), v in zip(missing, vectors)])
    logger.info("[KB_GRAPH] 节点向量预热 kb=%s 补算=%d", kb_id, len(missing))
    return len(missing)


def _node_vectors(kb_id: str, data: dict) -> list:
    """节点向量：优先读 kb_graph_vectors 持久化结果，仅对缺失节点增量 embed。"""
    nodes = data["nodes"]
    stored = {r["node_id"]: r for r in db.query(
        "SELECT node_id, text_hash, vec FROM kb_graph_vectors WHERE kb_id = ?", (kb_id,))}
    vectors: list = [None] * len(nodes)
    missing_idx = []
    for i, n in enumerate(nodes):
        text = _node_text(n)
        row = stored.get(n["id"])
        if row and row["text_hash"] == _text_hash(text):
            vectors[i] = _unpack(row["vec"])
        else:
            missing_idx.append(i)
    if missing_idx:
        texts = [_node_text(nodes[i]) for i in missing_idx]
        embedded = _embed_texts(texts)
        _store_vectors(kb_id, [(nodes[i]["id"], texts[k], embedded[k])
                               for k, i in enumerate(missing_idx)])
        for k, i in enumerate(missing_idx):
            vectors[i] = embedded[k]
    return vectors


def semantic_search(kb_id: str, query: str, top_n: int = 10, threshold: float = 0.5) -> list:
    query = (query or "").strip()
    if not query:
        raise ValueError("语义搜索内容不能为空")
    data = _load(kb_id)
    if not data["nodes"]:
        return []
    from app.core.vector_store import vector_store
    from app.services.evaluation import _cosine
    q_vec = vector_store.embeddings.embed_query(query)
    vectors = _node_vectors(kb_id, data)
    scored = []
    for node, vec in zip(data["nodes"], vectors):
        score = max(0.0, min(1.0, _cosine(q_vec, vec)))
        if score >= threshold:
            scored.append({"node": node, "score": round(score, 4)})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:max(1, top_n)]