"""
见字如面 - 主题知识库服务
知识库按主题创建，可设为公共或个人；不同主题知识库互相隔离。
检索时按向量相关度挑选前 3 个知识库（库数 > 3 时）。
"""
import logging
import uuid
from datetime import datetime
from typing import List, Optional

from app.core import db

logger = logging.getLogger("jianziruyang.kb")

# 预设主题（创建知识库时只能从中选择）
KB_TOPICS = ["饮食营养", "运动健身", "中医养生", "疾病预防", "心理健康", "睡眠作息", "其他"]

DEFAULT_KB_ID = "kb-default-public"
# 默认公共知识库的常用主题（存量内容以健康饮食类为主）
DEFAULT_KB_TOPIC = "饮食营养"


def ensure_default_kb() -> dict:
    """确保默认公共知识库存在，返回该库"""
    row = db.query_one("SELECT * FROM knowledge_bases WHERE id = ?", (DEFAULT_KB_ID,))
    if row:
        return row
    db.execute(
        "INSERT INTO knowledge_bases (id, name, topic, visibility, owner_id, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (DEFAULT_KB_ID, "公共知识库", DEFAULT_KB_TOPIC, "public", "", datetime.now().isoformat()),
    )
    logger.info("已创建默认公共知识库（主题: %s）", DEFAULT_KB_TOPIC)
    return db.query_one("SELECT * FROM knowledge_bases WHERE id = ?", (DEFAULT_KB_ID,))


def patch_legacy_metadata():
    """启动补丁：向量库元数据中无 kb_id 的存量 chunk 归入默认公共知识库"""
    from app.core.vector_store import vector_store

    kb = ensure_default_kb()
    # 存量默认库主题为“其他”时升级为常用主题
    if kb["topic"] == "其他":
        db.execute(
            "UPDATE knowledge_bases SET topic = ? WHERE id = ?",
            (DEFAULT_KB_TOPIC, DEFAULT_KB_ID),
        )
        logger.info("默认公共知识库主题已更新为: %s", DEFAULT_KB_TOPIC)

    meta_map = vector_store._load_metadata()
    patched = 0
    for doc_id, meta in meta_map.items():
        if not meta.get("kb_id"):
            meta["kb_id"] = DEFAULT_KB_ID
            patched += 1
    if patched:
        vector_store._save_metadata(meta_map)
        logger.info("已将 %d 条存量向量归入默认公共知识库", patched)


def get_kb(kb_id: str) -> Optional[dict]:
    return db.query_one("SELECT * FROM knowledge_bases WHERE id = ?", (kb_id,))


def list_accessible_kbs(user_id: str) -> List[dict]:
    """当前用户可见的知识库：所有 public + 自己的 private，含条目数"""
    rows = db.query(
        "SELECT * FROM knowledge_bases"
        " WHERE visibility = 'public' OR owner_id = ?"
        " ORDER BY created_at",
        (user_id or "",),
    )
    # 统计各库的向量条目数
    from app.core.vector_store import vector_store
    meta_map = vector_store._load_metadata()
    counts: dict[str, int] = {}
    for meta in meta_map.values():
        kid = meta.get("kb_id", "")
        counts[kid] = counts.get(kid, 0) + 1

    result = []
    for r in rows:
        result.append({
            "id": r["id"],
            "name": r["name"],
            "topic": r["topic"],
            "visibility": r["visibility"],
            "owner_id": r["owner_id"],
            "entry_count": counts.get(r["id"], 0),
            "created_at": r["created_at"],
        })
    return result


def create_kb(name: str, topic: str, visibility: str, owner_id: str, is_admin: bool) -> dict:
    """创建知识库。管理员可建公共库，普通用户只能建个人库"""
    name = (name or "").strip()
    if not name:
        raise ValueError("知识库名称不能为空")
    if topic not in KB_TOPICS:
        raise ValueError(f"主题必须是预设主题之一: {KB_TOPICS}")
    if visibility not in ("public", "private"):
        raise ValueError("可见性必须是 public 或 private")
    if visibility == "public" and not is_admin:
        raise ValueError("仅管理员可创建公共知识库")

    kb_id = f"kb-{str(uuid.uuid4())[:12]}"
    now = datetime.now().isoformat()
    db.execute(
        "INSERT INTO knowledge_bases (id, name, topic, visibility, owner_id, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (kb_id, name, topic, visibility, owner_id or "", now),
    )
    logger.info("创建知识库: %s (%s/%s) owner=%s", name, topic, visibility, owner_id[:8] if owner_id else "-")
    return {"id": kb_id, "name": name, "topic": topic, "visibility": visibility,
            "owner_id": owner_id or "", "entry_count": 0, "created_at": now}


def update_kb(kb_id: str, name: str, topic: str, user_id: str, is_admin: bool) -> Optional[dict]:
    """修改知识库名称/主题。公共库仅管理员可改；个人库仅本人可改"""
    kb = get_kb(kb_id)
    if not kb:
        return None
    if kb["visibility"] == "public":
        if not is_admin:
            raise PermissionError("仅管理员可修改公共知识库")
    elif kb["owner_id"] != user_id:
        raise PermissionError("只能修改自己的个人知识库")

    name = (name or kb["name"]).strip()
    if not name:
        raise ValueError("知识库名称不能为空")
    topic = topic or kb["topic"]
    if topic not in KB_TOPICS:
        raise ValueError(f"主题必须是预设主题之一: {KB_TOPICS}")

    db.execute(
        "UPDATE knowledge_bases SET name = ?, topic = ? WHERE id = ?",
        (name, topic, kb_id),
    )
    logger.info("修改知识库: %s -> %s (%s)", kb_id, name, topic)
    return get_kb(kb_id)


def delete_kb(kb_id: str, user_id: str, is_admin: bool) -> bool:
    """删除知识库及其全部向量条目。公共库仅管理员可删；个人库仅本人可删"""
    kb = get_kb(kb_id)
    if not kb:
        return False
    if kb["visibility"] == "public":
        if not is_admin:
            raise PermissionError("仅管理员可删除公共知识库")
        if kb_id == DEFAULT_KB_ID:
            raise ValueError("默认公共知识库不可删除")
    elif kb["owner_id"] != user_id:
        raise PermissionError("只能删除自己的个人知识库")

    from app.core.vector_store import vector_store
    from app.services.records import upload_records

    # 先收集该库所有向量条目的来源文件，同步删除上传记录与物理文件
    meta_map = vector_store._load_metadata()
    sources = {
        meta.get("source_image")
        for meta in meta_map.values()
        if meta.get("kb_id") == kb_id
    }
    removed = upload_records.delete_records_by_sources(sources)
    if removed:
        logger.info("删除知识库 %s 关联的 %d 条上传记录及原始文件", kb_id, removed)

    vector_store.delete_by_kb(kb_id)
    db.execute("DELETE FROM knowledge_bases WHERE id = ?", (kb_id,))
    logger.info("删除知识库: %s (%s)", kb["name"], kb_id)
    return True


def select_kbs_for_query(results: list, accessible_ids: set, top_n: int = 3) -> list:
    """从检索结果中按 kb_id 聚合平均相关度，挑选最相关的前 top_n 个知识库

    Args:
        results: [(text, metadata, score), ...]
        accessible_ids: 用户可见的知识库 ID 集合
        top_n: 挑选的库数量

    Returns:
        选中的 kb_id 列表（按相关度降序）
    """
    scores: dict[str, list] = {}
    for _, meta, score in results:
        kid = meta.get("kb_id", "")
        if kid in accessible_ids:
            scores.setdefault(kid, []).append(score)
    ranked = sorted(
        scores.items(),
        key=lambda kv: sum(kv[1]) / len(kv[1]),
        reverse=True,
    )
    return [kid for kid, _ in ranked[:top_n]]
