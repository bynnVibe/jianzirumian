"""
见字如面 - 聊天沉淀个人 Skill 草稿确认

Agent 提炼出的技能配置不再直接写库，而是先落为「待确认草稿」随回答呈现给用户；
用户点击确认后才由系统自动导入个人 Skill 配置（复用 skill_service.create_skill
的校验与安全预审）。与聊天附件确认入库同款约束：

- 明确确认：pending 草稿只有属主本人可确认/取消，确认即原子领取，不自动重放；
- 持久状态：草稿落 skill_drafts 表，刷新/重进会话仍可继续确认；
- 24 小时有效：过期自动置 expired，不再允许导入；
- 游客不可沉淀：仅登录用户可创建/确认草稿。
"""
import uuid
import json
import hashlib
from datetime import datetime, timedelta

from fastapi import HTTPException

from app.core import db
from app.services.skill import skill_service, _SENSITIVE_RE
from app.services import skill_packages, skill_review, skill_dependencies
from app.config import settings

_PUBLIC_FIELDS = ("id", "owner_id", "session_id", "name", "description", "content",
                  "status", "skill_id", "error", "created_at", "updated_at", "expires_at",
                  "message_id", "kind", "content_hash")

# 草稿名称/内容上限（与 skill_service 校验口径一致，提前拦截避免确认时才失败）
_MAX_NAME_CHARS = 60
_MAX_CONTENT_CHARS = 4000


def _now():
    return datetime.now().isoformat()


def serialize(row, conn=None) -> dict:
    result = {key: row.get(key, "") for key in _PUBLIC_FIELDS}
    result["review"] = json.loads(row.get("review_json") or "{}")
    package = json.loads(row.get("package_json") or "{}")
    if package:
        info = skill_packages.validate(package)
        result.update(files=info["files"], provenance=package.get("provenance", {}), slug=info["metadata"]["name"])
        result.update(skill_dependencies.snapshot(row, conn=conn))
        if row.get("status") == "imported" and row.get("skill_id"):
            result["review"] = skill_dependencies.installed_review(row["owner_id"], row["skill_id"], package, conn=conn)
    sql = "SELECT id,name FROM user_skills WHERE owner_id=? AND name=? AND id!=?"
    args = (row["owner_id"], row["name"], row.get("skill_id") or "")
    result["same_name_skills"] = ([dict(r) for r in conn.execute(sql, args).fetchall()]
                                  if conn is not None else db.query(sql, args))
    return result


def content_hash(name, description, content, kind="prompt", package=None):
    return hashlib.sha256(skill_packages.canonical({"name": name, "description": description,
        "content": content, "kind": kind, "package": package or {}}).encode()).hexdigest()


def _review(name, description, content, kind, package):
    if not name or len(name) > 60 or len(description) > 200:
        raise ValueError("名称不能为空且最多 60 字，描述最多 200 字")
    if kind == "agent_skill":
        info = skill_packages.validate(package)
        if info["texts"]["SKILL.md"] != content:
            raise ValueError("草稿正文与 SKILL.md 不一致")
        return skill_packages.review(package)
    if kind != "prompt" or not content.strip() or len(content) > _MAX_CONTENT_CHARS:
        raise ValueError("prompt 指令需为 1–4000 字")
    if _SENSITIVE_RE.search(name + content):
        raise ValueError("技能内容不得包含密码/密钥/证件号等敏感信息")
    result = skill_review.review_skill(name, kind, "chat", content)
    return {**result, "status": "ready" if result["verdict"] == "pass" else "rejected"}


def _load(conn, draft_id, user_id):
    if not user_id or user_id == "guest":
        raise HTTPException(403, "仅登录用户可确认或取消 Skill 草稿")
    row = conn.execute("SELECT * FROM skill_drafts WHERE id=? AND owner_id=?",
                       (draft_id, user_id)).fetchone()
    if not row:
        raise HTTPException(404, "草稿不存在或不属于当前用户")
    return dict(row)


def propose(user_id: str, session_id: str, name: str, description: str, content: str,
            *, message_id: str = "", kind: str = "prompt", package=None):
    """创建待确认草稿；返回 (草稿 dict, 错误文本)。基础校验前置，安全预审在确认导入时执行。"""
    if not user_id or user_id == "guest":
        return None, "游客无法沉淀个人 Skill，请登录后重试"
    name = (name or "").strip()
    content = (content or "").strip()
    if not name:
        return None, "技能名称为空"
    if len(name) > _MAX_NAME_CHARS:
        return None, f"技能名称过长（上限 {_MAX_NAME_CHARS} 字）"
    if not content:
        return None, "技能指令内容为空"
    description = (description or "").strip()
    package = package or {}
    try:
        if kind == "agent_skill":
            content = skill_packages.validate(package)["texts"]["SKILL.md"]
        review = _review(name, description, content, kind, package)
    except ValueError as exc:
        return None, str(exc)
    skill_review.log_review(user_id, "", "create", name, kind, "chat", review)
    if review["status"] == "rejected":
        return None, "安全或能力预审未通过：" + "；".join(review["reasons"])
    now = _now()
    row = {"id": str(uuid.uuid4()), "owner_id": user_id, "session_id": session_id or "",
           "name": name, "description": description, "content": content,
           "status": "pending", "skill_id": "", "error": "", "message_id": message_id,
           "kind": kind, "package_json": skill_packages.canonical(package),
           "content_hash": content_hash(name, description, content, kind, package),
           "review_json": skill_packages.canonical(review), "created_at": now, "updated_at": now,
           "expires_at": (datetime.now() + timedelta(hours=24)).isoformat()}
    columns = list(row)
    db.execute(f"INSERT INTO skill_drafts ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
               tuple(row.values()))
    return serialize(row), ""


def get_draft(draft_id: str, user_id: str) -> dict:
    with db.transaction() as conn:
        row = _load(conn, draft_id, user_id)
        if row["status"] == "pending" and row["expires_at"] <= _now():
            conn.execute("UPDATE skill_drafts SET status='expired',updated_at=? WHERE id=? AND status='pending'",
                         (_now(), draft_id))
            row["status"] = "expired"
    return serialize(row)


def history(session_id: str, user_id: str) -> list:
    db.execute("UPDATE skill_drafts SET status='expired',updated_at=? WHERE session_id=? AND owner_id=? "
               "AND status='pending' AND expires_at<=?", (_now(), session_id, user_id, _now()))
    return [serialize(r) for r in db.query(
        "SELECT * FROM skill_drafts WHERE session_id=? AND owner_id=? ORDER BY created_at",
        (session_id, user_id))]


def _apply_dependencies(conn, row, skill_id, package, expected_dependency_hash):
    review = skill_dependencies.save_bindings(conn, row, skill_id, package, expected_dependency_hash)
    if review["status"] == "rejected":
        raise HTTPException(409, "Skill 安全预审未通过，不能应用依赖")
    conn.execute("UPDATE skill_package_versions SET review_json=? WHERE owner_id=? AND skill_id=? AND version=?",
                 (skill_packages.canonical(review), row["owner_id"], skill_id, skill_packages.validate(package)["hash"]))
    conn.execute("UPDATE user_skills SET enabled=?,updated_at=? WHERE owner_id=? AND id=?",
                 (int(review["status"] == "ready"), _now(), row["owner_id"], skill_id))
    return review


def confirm(draft_id: str, user_id: str, actor_name: str = "", expected_hash: str = "",
            allow_same_name: bool = False, expected_dependency_hash: str = "") -> dict:
    """确认导入：原子领取 pending → importing，调用 create_skill 自动写入个人 Skill 配置。"""
    # 先提交过期状态，再返回错误，避免异常回滚导致状态留在 pending。
    get_draft(draft_id, user_id)
    failure = ""
    with db.transaction() as conn:
        row = _load(conn, draft_id, user_id)
        if row["status"] == "imported" and not expected_dependency_hash:
            return serialize(row, conn=conn)  # 幂等：已导入直接返回
        if row["status"] not in ("pending", "imported"):
            raise HTTPException(410 if row["status"] == "expired" else 409,
                                f"草稿状态为 {row['status']}，不能导入")
        package = json.loads(row["package_json"] or "{}")
        actual_hash = content_hash(row["name"], row["description"], row["content"], row["kind"], package)
        if (not row["content_hash"] or actual_hash != row["content_hash"]
                or (expected_hash and expected_hash != actual_hash)
                or (row["kind"] == "agent_skill" and not expected_hash)):
            raise HTTPException(409, "预览内容已变化或缺少确认 hash，请重新预览后确认")
        if row["status"] == "imported":
            if not package or not conn.execute("SELECT 1 FROM user_skills WHERE id=? AND owner_id=?",
                                                (row["skill_id"], user_id)).fetchone():
                raise HTTPException(409, "原 Skill 已删除或没有可配置的依赖包")
            _apply_dependencies(conn, row, row["skill_id"], package, expected_dependency_hash)
            return serialize(row, conn=conn)
        if package:
            version = skill_packages.validate(package)["hash"]
            installed = conn.execute(
                "SELECT v.skill_id FROM skill_package_versions v JOIN user_skills s ON s.id=v.skill_id "
                "AND s.owner_id=v.owner_id WHERE v.owner_id=? AND v.version=?", (user_id, version)).fetchone()
            if installed:
                if expected_dependency_hash:
                    _apply_dependencies(conn, row, installed[0], package, expected_dependency_hash)
                conn.execute("UPDATE skill_drafts SET status='imported',skill_id=?,updated_at=? WHERE id=?",
                             (installed[0], _now(), draft_id))
                row.update(status="imported", skill_id=installed[0])
                return serialize(row, conn=conn)
        if not allow_same_name and conn.execute(
                "SELECT 1 FROM user_skills WHERE owner_id=? AND name=?", (user_id, row["name"])).fetchone():
            raise HTTPException(409, "已有同名技能，请确认保留原技能并另存一份，或取消草稿")
        count = conn.execute("SELECT COUNT(*) FROM user_skills WHERE owner_id=? AND builtin=0", (user_id,)).fetchone()[0]
        if count >= settings.MAX_SKILLS_PER_USER:
            raise HTTPException(409, "个人技能数量已达上限")
        try:
            review = _review(row["name"], row["description"], row["content"], row["kind"], package)
            if review["status"] == "rejected":
                failure = "；".join(review["reasons"])
        except ValueError as exc:
            failure = str(exc)
        now = _now()
        if failure:
            conn.execute("UPDATE skill_drafts SET status='failed',error=?,updated_at=? WHERE id=?",
                         (failure, now, draft_id))
        else:
            sid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"skill-draft:{user_id}:{draft_id}"))
            conn.execute(
                "INSERT INTO user_skills (id,owner_id,name,description,kind,scope,content,enabled,builtin,visibility,created_at,updated_at) "
                "VALUES (?,?,?,?,?,'chat',?,?,0,'personal',?,?)",
                (sid, user_id, row["name"], row["description"], row["kind"], row["content"],
                 int(review["status"] == "ready"), now, now))
            if package:
                skill_packages.save(conn, user_id, sid, package, review, now)
                review = _apply_dependencies(conn, row, sid, package, expected_dependency_hash)
            conn.execute("UPDATE skill_drafts SET status='imported',skill_id=?,review_json=?,error='',updated_at=? WHERE id=?",
                         (sid, skill_packages.canonical(review), now, draft_id))
    if failure:
        raise HTTPException(400, "导入失败：" + failure)
    return get_draft(draft_id, user_id)


def draft_package(draft_id: str, user_id: str, expected_hash: str) -> dict:
    """下载与资源预览始终校验属主和已展示的内容指纹，不自动执行资源。"""
    get_draft(draft_id, user_id)
    with db.transaction() as conn:
        row = _load(conn, draft_id, user_id)
        if row["status"] not in ("pending", "imported"):
            raise HTTPException(410, "草稿已失效")
        package = json.loads(row["package_json"] or "{}")
        actual = content_hash(row["name"], row["description"], row["content"], row["kind"], package)
        if not expected_hash or expected_hash != actual or row["content_hash"] != actual:
            raise HTTPException(409, "预览已变化，请重新获取草稿")
        if not package:
            raise HTTPException(404, "此草稿没有资源包")
        skill_packages.validate(package)
        return package


def cancel(draft_id: str, user_id: str) -> dict:
    get_draft(draft_id, user_id)
    with db.transaction() as conn:
        row = _load(conn, draft_id, user_id)
        if row["status"] == "cancelled":
            return serialize(row, conn=conn)
        if row["status"] != "pending":
            raise HTTPException(409, "只能取消尚未导入的草稿")
        conn.execute("UPDATE skill_drafts SET status='cancelled',updated_at=? WHERE id=? AND status='pending'",
                     (_now(), draft_id))
        row["status"] = "cancelled"
        conn.execute("UPDATE personal_tool_drafts SET status='cancelled',updated_at=? WHERE owner_id=? "
                     "AND status='awaiting_confirmation' AND (parent_skill_draft_id=? OR id IN "
                     "(SELECT tool_draft_id FROM skill_tool_links WHERE owner_id=? AND draft_id=?))",
                     (_now(), user_id, draft_id, user_id, draft_id))
    skill_dependencies.cancel_discoveries(user_id, draft_id)
    return serialize(row)


def invalidate_session(session_id: str):
    db.execute("UPDATE skill_drafts SET status='cancelled',updated_at=?,error='原会话已删除' "
               "WHERE session_id=? AND status='pending'", (_now(), session_id))