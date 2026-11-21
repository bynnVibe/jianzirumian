"""
见字如面 - 资源版本与变更事件（前端缓存失效推送的记录源）

版本 bump 与变更事件由资源表触发器在业务写入同事务内原子落库
（见 core/attribution_schema.sync_triggers_v1），跨进程一致；本模块只负责
读取侧：授权范围内的版本快照、游标补发（24h 窗口）与过期清理。

授权范围：global=全体登录用户；ops=管理员/运维；user=仅属主。
事件不携带业务正文或秘密，前端据此定向重验，不广播 Token。
"""
import logging
from datetime import datetime, timedelta

from app.core import db

logger = logging.getLogger("jianziruyang.sync")

# 变更事件补发窗口：超窗/游标失效 → 客户端 reset 后按版本快照定向重验
EVENT_WINDOW_HOURS = 24


def _visible(user_id: str, can_ops: bool):
    """授权范围过滤（SQL 片段 + 参数）：跨用户不能订阅他人会话/越权资源。"""
    clause = "(scope = 'global'"
    params: list = []
    if can_ops:
        clause += " OR scope = 'ops'"
    if user_id:
        clause += " OR (scope = 'user' AND user_id = ?)"
        params.append(user_id)
    clause += ")"
    return clause, params


def snapshot(user_id: str, can_ops: bool) -> dict:
    """一致的版本快照 + 水位线（同一读事务）：首连/重连/reset 的衔接基准。

    快照与水位线一起读取：订阅从水位线之后补发，避免「取快照到订阅成功
    之间」的漏事件（触发器保证版本与事件原子写入）。资源可见性按静态
    范围映射判定（不依赖事件窗口，久未变更的资源仍在快照内）。
    """
    from app.core.attribution_schema import _SYNC_RESOURCES
    scopes = {res: scope for res, (scope, _tables) in _SYNC_RESOURCES.items()}
    scopes["sessions"] = "user"
    with db.transaction() as conn:
        rows = conn.execute("SELECT resource, version FROM resource_revisions").fetchall()
        watermark = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS w FROM resource_change_events").fetchone()["w"]
    versions = {}
    for r in rows:
        scope = scopes.get(r["resource"], "global")
        if scope == "global" or (scope == "ops" and can_ops) or (scope == "user" and user_id):
            versions[r["resource"]] = int(r["version"])
    return {"versions": versions, "watermark": int(watermark)}


def events_after(cursor: int, user_id: str, can_ops: bool, limit: int = 200) -> dict:
    """游标补发：返回 {reset, events, watermark}。

    游标早于窗口内最早事件（超窗/服务重启清理后）或非法（超前）→ reset=True，
    客户端改用版本快照定向重验；正常则返回授权范围内的增量事件。
    """
    cutoff = (datetime.now() - timedelta(hours=EVENT_WINDOW_HOURS)).isoformat()
    clause, params = _visible(user_id, can_ops)
    bounds = db.query_one(
        "SELECT MIN(id) AS lo, MAX(id) AS hi FROM resource_change_events WHERE created_at >= ?",
        (cutoff,)) or {}
    lo, hi = bounds.get("lo"), int(bounds.get("hi") or 0)
    if cursor > 0 and (lo is None or cursor < lo - 1 or cursor > hi):
        return {"reset": True, "events": [], "watermark": hi}
    rows = db.query(
        f"SELECT id, resource, version, scope, user_id, created_at"
        f" FROM resource_change_events WHERE id > ? AND created_at >= ? AND {clause}"
        f" ORDER BY id LIMIT ?", tuple([cursor, cutoff] + params + [max(1, min(limit, 500))]))
    events = [{"id": int(r["id"]), "resource": r["resource"], "version": int(r["version"]),
               "created_at": r["created_at"]} for r in rows]
    return {"reset": False, "events": events, "watermark": hi}


def purge_old() -> int:
    """清理超出补发窗口的变更事件（启动时调用；版本表保留当前水位）。"""
    cutoff = (datetime.now() - timedelta(hours=EVENT_WINDOW_HOURS)).isoformat()
    try:
        n = db.execute("DELETE FROM resource_change_events WHERE created_at < ?", (cutoff,))
        if n:
            logger.info("已清理 %d 条过期资源变更事件（保留 %dh 窗口）", n, EVENT_WINDOW_HOURS)
        return n
    except Exception as e:
        logger.warning("清理资源变更事件失败: %s", e)
        return 0