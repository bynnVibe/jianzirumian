"""个人工具生命周期：版本不可变、确认幂等、真实测试、属主隔离和统一执行。"""
import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app.core import db
from app.core.json_schema import validate
from app.services import personal_tool_specs as specs, tool_credentials, tool_http, tool_result_cache
from app.services.tools.base import BaseTool, ToolContext, ToolResult
from app.services.sandbox import worker_client

logger = logging.getLogger("jianziruyang.personal_tools")

_RUNNING = {}


def now():
    return datetime.now(timezone.utc).isoformat()


def require_owner(owner):
    if not owner or owner in ("guest", "system", "-"):
        raise HTTPException(403, "仅登录用户可管理个人工具")
    if os.getenv("PERSONAL_TOOLS_ENABLED", "true").lower() != "true":
        raise HTTPException(503, "个人工具功能已关闭")


def _load(conn, owner, tool_id):
    row = conn.execute("SELECT t.*,v.version,v.function_name,v.spec_json,v.review_json,v.approved_by,v.approved_at "
        "FROM personal_tools t JOIN personal_tool_versions v ON v.owner_id=t.owner_id AND v.tool_id=t.id "
        "AND v.version=t.current_version WHERE t.owner_id=? AND t.id=?", (owner, tool_id)).fetchone()
    if not row:
        raise HTTPException(404, "工具不存在或不属于当前用户")
    return dict(row)


def _blocked_users(row):
    """按工具的用户黑名单（默认空＝全员可调用）；兼容 dict 与 sqlite Row。"""
    try:
        raw = row["blocked_users"]
    except (KeyError, IndexError):
        return []
    try:
        value = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return [u for u in value if isinstance(u, str)] if isinstance(value, list) else []


def _public(row):
    blockers = _blockers(row)
    if "invalid_stored_spec" in blockers:
        return {**{k: row[k] for k in ("id", "name", "version", "function_name", "created_at", "updated_at")},
                "status": "rejected", "enabled": False, "approved": False, "blockers": blockers,
                "spec": {}, "review": {}, "credential_configured": False, "terms_accepted": False, "runtime_profile": {},
                "owner_id": row["owner_id"], "blocked_users": _blocked_users(row)}
    return {**{k: row[k] for k in ("id", "name", "status", "enabled", "version", "function_name", "created_at", "updated_at")},
            "blockers": blockers,
            "owner_id": row["owner_id"],
            "blocked_users": _blocked_users(row),
            "status": "needs_config" if blockers else row["status"],
            "enabled": bool(row["enabled"] and not blockers),
            "spec": json.loads(row["spec_json"]), "review": json.loads(row["review_json"]),
            "approved": bool(row["approved_by"] and "admin_review_required" not in blockers and "version_changed" not in blockers),
            "credential_configured": bool(row["credential_id"]),
            "terms_accepted": bool(row["terms_accepted"]),
            "eval_status": row.get("eval_status") or "",
            "eval_dataset_id": row.get("eval_dataset_id") or "",
            "eval_run_id": row.get("eval_run_id") or "",
            "runtime_profile": worker_client.runtime_profile() if json.loads(row["spec_json"])["executor"] == "python" else {}}


def get(owner, tool_id):
    require_owner(owner)
    with db.transaction() as conn:
        row = _load(conn, owner, tool_id)
        value = _public(row)
        value["versions"] = [dict(r) for r in conn.execute(
            "SELECT version,created_at,approved_at FROM personal_tool_versions WHERE owner_id=? AND tool_id=? ORDER BY created_at DESC",
            (owner, tool_id))]
        value["tests"] = [dict(r) for r in conn.execute(
            "SELECT id,version,status,report_json,created_at,finished_at FROM personal_tool_tests WHERE owner_id=? AND tool_id=? ORDER BY created_at DESC LIMIT 20",
            (owner, tool_id))]
    return value


def listing(owner):
    require_owner(owner)
    with db.transaction() as conn:
        return [_public(_load(conn, owner, row[0])) for row in conn.execute(
            "SELECT id FROM personal_tools WHERE owner_id=? ORDER BY created_at DESC", (owner,)).fetchall()]


def _draft_public(row):
    return {**{k: row[k] for k in ("id", "session_id", "message_id", "tool_id", "status", "error", "content_hash",
                                  "created_at", "updated_at", "expires_at", "parent_skill_draft_id")},
            "spec": json.loads(row["spec_json"]), "review": json.loads(row["review_json"])}


def draft(owner, draft_id):
    require_owner(owner)
    db.execute("UPDATE personal_tool_drafts SET status='expired',updated_at=? WHERE id=? AND owner_id=? "
               "AND status='awaiting_confirmation' AND expires_at<=?", (now(), draft_id, owner, now()))
    row = db.query_one("SELECT * FROM personal_tool_drafts WHERE id=? AND owner_id=?", (draft_id, owner))
    if not row:
        raise HTTPException(404, "工具草稿不存在或不属于当前用户")
    value = _draft_public(row)
    if value["status"] == "installed":
        value["tool"] = get(owner, value["tool_id"])
    return value


def history(owner, session_id):
    if not owner or owner in ("guest", "system", "-"):
        return []
    rows = db.query("SELECT d.* FROM personal_tool_drafts d WHERE d.owner_id=? AND d.session_id=? AND d.parent_skill_draft_id='' "
                    "AND NOT EXISTS (SELECT 1 FROM skill_tool_links l WHERE l.owner_id=d.owner_id AND l.tool_draft_id=d.id) "
                    "ORDER BY d.created_at", (owner, session_id))
    values = []
    for row in rows:
        value = _draft_public(row)
        if value["status"] == "awaiting_confirmation" and value["expires_at"] <= now():
            value["status"] = "expired"
        if value["status"] == "installed":
            with db.transaction() as conn:
                value["tool"] = _public(_load(conn, owner, value["tool_id"]))
        values.append(value)
    return values


def invalidate_session(session_id):
    db.execute("UPDATE personal_tool_drafts SET status='expired',updated_at=? WHERE session_id=? AND status='awaiting_confirmation'",
               (now(), session_id))


def _not_running(conn, owner, tool_id):
    if conn.execute("SELECT 1 FROM personal_tool_executions WHERE owner_id=? AND tool_id=? AND execution_status IN ('running','cancellation_requested','cleanup_failed')",
                    (owner, tool_id)).fetchone():
        raise HTTPException(409, "工具仍在运行，请先取消任务再修改配置或版本")


def propose(owner, spec, session_id="", message_id="", tool_id="", *, parent_skill_draft_id="", skill_requirement=""):
    require_owner(owner)
    assessment = specs.review(spec)
    if session_id:
        session = db.query_one("SELECT user_id FROM chat_sessions WHERE session_id=?", (session_id,))
        if not session or session["user_id"] != owner:
            raise HTTPException(404, "会话不存在或无权访问")
    if tool_id:
        get(owner, tool_id)
    identifier, created = uuid.uuid4().hex, now()
    with db.transaction() as conn:
        if parent_skill_draft_id:
            from app.services import skill_dependencies, skill_packages
            parent, bundle = skill_dependencies._draft(conn, owner, parent_skill_draft_id)
            if (parent["session_id"] != session_id or parent["message_id"] != message_id
                    or skill_requirement not in skill_packages.requirements(bundle)):
                raise HTTPException(409, "关联工具的原 Skill 轮次或依赖声明已变化")
        if conn.execute("SELECT COUNT(*) FROM personal_tool_drafts WHERE owner_id=? AND status='awaiting_confirmation' AND expires_at>?",
                        (owner, created)).fetchone()[0] >= 20:
            raise HTTPException(409, "待确认工具草稿过多，请先处理已有草稿")
        conn.execute("INSERT INTO personal_tool_drafts (id,owner_id,session_id,message_id,tool_id,spec_json,content_hash,review_json,"
            "status,created_at,updated_at,expires_at,parent_skill_draft_id,skill_requirement) VALUES (?,?,?,?,?,?,?,?,'awaiting_confirmation',?,?,?,?,?)",
            (identifier, owner, session_id, message_id, tool_id, specs.canonical(spec), specs.fingerprint(spec), specs.canonical(assessment),
             created, created, (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(), parent_skill_draft_id, skill_requirement))
    return draft(owner, identifier)


def _unique_function_name(conn, base, tool_id):
    """功能名全局唯一（内置注册表 + 其他个人工具当前版本）；冲突追加确定数字后缀，不引入随机码。"""
    from app.services.tools import base as tools_base
    if not tools_base.all_tools():
        try:
            from app.services.tools import bootstrap_registry
            bootstrap_registry()
        except Exception:
            pass
    used = {t["name"] for t in tools_base.registry_snapshot()}
    used.update(r["function_name"] for r in conn.execute(
        "SELECT v.function_name FROM personal_tools t JOIN personal_tool_versions v "
        "ON v.owner_id=t.owner_id AND v.tool_id=t.id AND v.version=t.current_version WHERE t.id<>?", (tool_id,)))
    name, suffix = base[:60], 1
    while name in used:
        suffix += 1
        name = f"{base[:56]}_{suffix}"
    return name


def _blockers(row):
    try:
        spec = json.loads(row["spec_json"])
        assessment = specs.review(spec)
        if not isinstance(json.loads(row["review_json"]), dict):
            raise ValueError("invalid_review")
    except (ValueError, TypeError, KeyError, RecursionError):
        return ["invalid_stored_spec"]
    if assessment["version"] != row["version"]:
        return ["version_changed"]
    issues = []
    if assessment["requires_admin"] and (not row["approved_by"] or json.loads(row["review_json"]).get("scan_version") != specs.SCAN_VERSION):
        issues.append("admin_review_required")
    if assessment["requires_terms"] and not row["terms_accepted"]:
        issues.append("noncommercial_terms_required")
    if spec["config"].get("auth"):
        if not row["credential_id"] or not tool_credentials.available():
            issues.append("credentials_required")
    if spec["executor"] == "python":
        if not worker_client.configured():
            issues.append("sandbox_unavailable")
        if json.loads(row["review_json"]).get("runtime_profile") != worker_client.runtime_profile():
            if "admin_review_required" not in issues:
                issues.append("admin_review_required")
    return issues


async def confirm(owner, draft_id, expected_hash, *, accept_terms=False, allow_same_name=False):
    current = draft(owner, draft_id)
    if not expected_hash or expected_hash != current["content_hash"] or specs.fingerprint(current["spec"]) != expected_hash:
        raise HTTPException(409, "草稿内容变化或缺少确认 hash，请重新预览")
    assessment = specs.review(current["spec"])
    with db.transaction() as conn:
        row = dict(conn.execute("SELECT * FROM personal_tool_drafts WHERE id=? AND owner_id=?", (draft_id, owner)).fetchone())
        if row["status"] in ("installed", "testing"):
            return {**_draft_public(row), "tool": _public(_load(conn, owner, row["tool_id"]))}
        if row["status"] != "awaiting_confirmation" or row["expires_at"] <= now():
            raise HTTPException(409, "草稿已取消或过期")
        if row["parent_skill_draft_id"]:
            from app.services import skill_dependencies
            skill_dependencies._draft(conn, owner, row["parent_skill_draft_id"])
        parents = conn.execute("SELECT d.status,d.expires_at FROM skill_tool_links l JOIN skill_drafts d "
                               "ON d.id=l.draft_id AND d.owner_id=l.owner_id WHERE l.owner_id=? AND l.tool_draft_id=?",
                               (owner, draft_id)).fetchall()
        if any(p["status"] not in ("pending", "imported") or
               (p["status"] == "pending" and p["expires_at"] <= datetime.now().isoformat()) for p in parents):
            raise HTTPException(409, "关联 Skill 草稿已取消或过期，不能继续添加依赖")
        if row["content_hash"] != expected_hash or specs.fingerprint(json.loads(row["spec_json"])) != expected_hash:
            raise HTTPException(409, "草稿版本已变化，请重新确认")
        tool_id, created = row["tool_id"], now()
        spec = current["spec"]
        version = assessment["version"]
        base = None
        if not tool_id:
            duplicate = conn.execute("SELECT t.id FROM personal_tools t WHERE t.owner_id=? AND t.current_version=?",
                                     (owner, version)).fetchone()
            if duplicate:
                conn.execute("UPDATE personal_tool_drafts SET status='installed',tool_id=?,updated_at=? WHERE id=?",
                             (duplicate[0], created, draft_id))
                row.update(status="installed", tool_id=duplicate[0], updated_at=created)
                return {**_draft_public(row), "tool": _public(_load(conn, owner, duplicate[0]))}
            if not allow_same_name and conn.execute("SELECT 1 FROM personal_tools WHERE owner_id=? AND name=?", (owner, spec["name"])).fetchone():
                raise HTTPException(409, "已有同名工具，请确认另存一份或取消草稿")
            if conn.execute("SELECT COUNT(*) FROM personal_tools WHERE owner_id=?", (owner,)).fetchone()[0] >= 50:
                raise HTTPException(409, "个人工具已达 50 个上限")
            tool_id = uuid.uuid4().hex
            conn.execute("INSERT INTO personal_tools(id,owner_id,name,current_version,status,terms_accepted,created_at,updated_at) "
                         "VALUES (?,?,?,?,'needs_config',?,?,?)", (tool_id, owner, spec["name"], version, int(accept_terms), created, created))
            base = specs.derive_function_name(spec)
        else:
            existing = _load(conn, owner, tool_id)
            _not_running(conn, owner, tool_id)
            if existing["status"] == "testing":
                raise HTTPException(409, "当前版本仍在测试，不能并发切换版本")
            conn.execute("UPDATE personal_tools SET name=?,current_version=?,status='needs_config',enabled=0,credential_id='',terms_accepted=?,updated_at=? "
                         "WHERE id=? AND owner_id=?", (spec["name"], version, int(accept_terms), created, tool_id, owner))
            # 功能名跨版本稳定；仅历史随机码名在升级时改为功能名
            base = existing["function_name"]
            if specs.LEGACY_FUNCTION_NAME_RE.fullmatch(base):
                base = specs.derive_function_name(spec)
        if not base:
            base = "http_api_reader" if spec["executor"] == "http" else "python_compute"
        function_name = _unique_function_name(conn, base, tool_id)
        conn.execute("INSERT OR IGNORE INTO personal_tool_versions(tool_id,owner_id,version,function_name,spec_json,review_json,created_at) VALUES (?,?,?,?,?,?,?)",
                     (tool_id, owner, version, function_name, row["spec_json"], specs.canonical(assessment), created))
        conn.execute("UPDATE personal_tool_drafts SET status='installed',tool_id=?,updated_at=? WHERE id=?", (tool_id, created, draft_id))
    await test(owner, tool_id)
    return draft(owner, draft_id)


def cancel_draft(owner, draft_id):
    current = draft(owner, draft_id)
    if current["status"] not in ("awaiting_confirmation", "cancelled"):
        raise HTTPException(409, "只能取消尚未确认的工具草稿")
    db.execute("UPDATE personal_tool_drafts SET status='cancelled',updated_at=? WHERE owner_id=? AND id=? AND status='awaiting_confirmation'",
               (now(), owner, draft_id))
    return draft(owner, draft_id)


def configure(owner, tool_id, *, enabled=None, accept_terms=None, credential=None, blocked_users=None):
    require_owner(owner)
    with db.transaction() as conn:
        row = _load(conn, owner, tool_id)
        _not_running(conn, owner, tool_id)
        if row["status"] == "testing":
            raise HTTPException(409, "工具正在测试，暂不能修改配置")
        if blocked_users is not None:
            if not isinstance(blocked_users, list) or any(not isinstance(u, str) for u in blocked_users):
                raise HTTPException(400, "blocked_users 须为用户 ID 字符串列表")
            cleaned = sorted({u for u in blocked_users if u and u != owner})
            conn.execute("UPDATE personal_tools SET blocked_users=? WHERE id=?", (json.dumps(cleaned), tool_id))
        if credential is not None:
            if not json.loads(row["spec_json"])["config"].get("auth"):
                raise HTTPException(400, "此工具未声明凭据，不需要填写密钥")
            encrypted = tool_credentials.encrypt(owner, tool_id, row["version"], credential)
            cid = uuid.uuid4().hex
            conn.execute("INSERT INTO personal_tool_credentials VALUES (?,?,?,?,?,?)", (cid, owner, tool_id, row["version"], encrypted, now()))
            conn.execute("UPDATE personal_tools SET credential_id=?,enabled=0,status='needs_config' WHERE id=?", (cid, tool_id))
        if accept_terms is not None:
            conn.execute("UPDATE personal_tools SET terms_accepted=? WHERE id=?", (int(accept_terms), tool_id))
        row = _load(conn, owner, tool_id)
        if enabled is True:
            passed = conn.execute("SELECT 1 FROM personal_tool_tests WHERE owner_id=? AND tool_id=? AND version=? AND status='passed'",
                                  (owner, tool_id, row["version"])).fetchone()
            if not passed or _blockers(row) or row["status"] not in ("ready", "disabled"):
                raise HTTPException(409, "工具必须完成配置与本版本真实测试后才能启用")
            conn.execute("UPDATE personal_tools SET enabled=1,status='ready' WHERE id=?", (tool_id,))
        elif enabled is False:
            conn.execute("UPDATE personal_tools SET enabled=0,status=CASE WHEN status='ready' THEN 'disabled' ELSE status END WHERE id=?", (tool_id,))
        if _blockers(row):
            conn.execute("UPDATE personal_tools SET enabled=0,status='needs_config' WHERE id=?", (tool_id,))
        conn.execute("UPDATE personal_tools SET updated_at=? WHERE id=?", (now(), tool_id))
    return get(owner, tool_id)


def pending_reviews():
    rows = db.query("SELECT v.owner_id,v.tool_id,v.version,v.spec_json,v.review_json,v.approved_by "
                    "FROM personal_tool_versions v JOIN personal_tools t ON t.id=v.tool_id AND t.owner_id=v.owner_id "
                    "AND t.current_version=v.version ORDER BY v.created_at")
    pending = []
    for row in rows:
        try:
            assessment = specs.review(json.loads(row["spec_json"]))
            stored = json.loads(row["review_json"])
            profile = worker_client.runtime_profile() if json.loads(row["spec_json"])["executor"] == "python" else {}
            stale = stored.get("scan_version") != specs.SCAN_VERSION or bool(profile and stored.get("runtime_profile") != profile)
            if assessment["requires_admin"] and (not row["approved_by"] or stale):
                pending.append({**{k: v for k, v in row.items() if k != "approved_by"}, "runtime_profile": profile,
                                "profile_hash": specs.fingerprint(profile) if profile else ""})
        except (ValueError, KeyError, TypeError):
            continue
        if len(pending) >= 100:
            break
    return pending


def approve(owner, tool_id, expected_hash, actor, expected_profile_hash=""):
    if actor.get("role") != "admin":
        raise HTTPException(403, "版本审批仅限管理员")
    with db.transaction() as conn:
        row = _load(conn, owner, tool_id)
        if row["version"] != expected_hash or specs.version_fingerprint(json.loads(row["spec_json"])) != expected_hash:
            raise HTTPException(409, "审批版本发生变化")
        _not_running(conn, owner, tool_id)
        if row["status"] == "testing":
            raise HTTPException(409, "测试期间不能变更审批")
        spec = json.loads(row["spec_json"])
        assessment = specs.review(spec)
        if spec["executor"] == "python":
            profile = worker_client.runtime_profile()
            if expected_profile_hash != (specs.fingerprint(profile) if profile else ""):
                raise HTTPException(409, "隔离运行配置变化，请重新核验镜像与权限")
            assessment["runtime_profile"] = profile
        conn.execute("UPDATE personal_tools SET status='needs_config',enabled=0,updated_at=? WHERE owner_id=? AND id=?", (now(), owner, tool_id))
        conn.execute("UPDATE personal_tool_versions SET approved_by=?,approved_at=?,review_json=? WHERE owner_id=? AND tool_id=? AND version=?",
                     (actor["id"], now(), specs.canonical(assessment), owner, tool_id, expected_hash))
    return {"approved": True, "version": expected_hash, "message": "审批已绑定版本；仍须属主配置并运行只读样例测试"}


async def test(owner, tool_id):
    require_owner(owner)
    test_id = uuid.uuid4().hex
    with db.transaction() as conn:
        row = _load(conn, owner, tool_id)
        _not_running(conn, owner, tool_id)
        if conn.execute("SELECT 1 FROM personal_tool_tests WHERE owner_id=? AND status='running'", (owner,)).fetchone():
            raise HTTPException(409, "已有个人工具正在测试")
        blockers = _blockers(row)
        conn.execute("INSERT INTO personal_tool_tests(id,owner_id,tool_id,version,status,report_json,created_at) VALUES (?,?,?,?,?,?,?)",
                     (test_id, owner, tool_id, row["version"], "blocked" if blockers else "running", specs.canonical({"blockers": blockers}), now()))
        conn.execute("UPDATE personal_tools SET status=?,enabled=0,eval_status='',eval_run_id='',updated_at=? WHERE id=?",
                     ("needs_config" if blockers else "testing", now(), tool_id))
    if blockers:
        return get(owner, tool_id)
    from app.services.tools.invoke import invoke_tool
    status, report = "failed", {"passed": 0, "error": "test_failed"}
    try:
        async with asyncio.timeout(90):
            spec = json.loads(row["spec_json"])
            ctx = ToolContext(owner, "", "", "", tool_test_id=test_id, force_refresh=True)
            for sample in spec["tests"]:
                result = await invoke_tool(row["function_name"], ctx, sample["input"], routed_by="personal_tool_test")
                if not result.success:
                    report["error"] = result.error
                    break
                specs.assert_result(result.extra["output"], sample["input"], sample["assertions"])
                report["passed"] += 1
            else:
                status, report["error"] = "passed", ""
    except worker_client.CancellationUnconfirmed:
        status, report["error"] = "cancellation_requested", "sandbox_cancellation_unconfirmed"
        raise
    except asyncio.CancelledError:
        status, report["error"] = "cancelled", "cancelled"
        raise
    except TimeoutError:
        report["error"] = "test_timeout"
    except Exception:
        report["error"] = "test_assertion_failed"
    finally:
        with db.transaction() as conn:
            conn.execute("UPDATE personal_tool_tests SET status=?,report_json=?,finished_at=? WHERE id=?",
                         (status, specs.canonical(report), now(), test_id))
            conn.execute("UPDATE personal_tools SET status=?,enabled=?,updated_at=? WHERE id=? AND owner_id=? AND current_version=? AND status='testing'",
                         ("ready" if status == "passed" else "needs_config", int(status == "passed"), now(), tool_id, owner, row["version"]))
    if status == "passed":
        # 测试通过 → 自动构建工具评测集并后台评测；全部用例通过才进标准化工具注册表
        from app.services import tool_eval
        tool_eval.schedule_after_test(owner, tool_id)
    return get(owner, tool_id)


def recover():
    with db.transaction() as conn:
        conn.execute("UPDATE personal_tool_executions SET execution_status='cancellation_requested',error_code='backend_restarted' "
                     "WHERE execution_status='running' AND NOT EXISTS (SELECT 1 FROM personal_tool_versions v "
                     "WHERE v.tool_id=personal_tool_executions.tool_id AND v.owner_id=personal_tool_executions.owner_id "
                     "AND v.version=personal_tool_executions.version "
                     "AND CASE WHEN json_valid(v.spec_json) THEN json_extract(v.spec_json,'$.executor') END IN ('http','connector'))")
        conn.execute("UPDATE personal_tool_tests SET status='interrupted',finished_at=? WHERE status='running'", (now(),))
        conn.execute("UPDATE personal_tools SET status='needs_config',enabled=0 WHERE status='testing'")
        conn.execute("UPDATE personal_tools SET eval_status='failed' WHERE eval_status='running'")
        conn.execute("UPDATE personal_tool_executions SET execution_status='interrupted',validation_status='failed',requirement_status='unmet',finished_at=? "
                     "WHERE execution_status='running'", (now(),))


def resolve(caller, function_name, test_id=""):
    """执行门禁：默认全员可调用（跨属主），黑名单用户拒绝；属主自测不受黑名单影响。"""
    if not specs.FUNCTION_NAME_RE.fullmatch(function_name or ""):
        return None
    require_owner(caller)
    with db.transaction() as conn:
        found = conn.execute("SELECT t.*,v.version,v.function_name,v.spec_json,v.review_json,v.approved_by,v.approved_at "
            "FROM personal_tools t JOIN personal_tool_versions v ON v.owner_id=t.owner_id AND v.tool_id=t.id "
            "AND v.version=t.current_version WHERE v.function_name=?", (function_name,)).fetchone()
        if not found:
            return None
        row = dict(found)
        if function_name != row["function_name"]:
            return None
        owner = row["owner_id"]
        testing = bool(test_id and conn.execute("SELECT 1 FROM personal_tool_tests WHERE id=? AND owner_id=? AND tool_id=? AND version=? AND status='running'",
                                                (test_id, owner, row["id"], row["version"])).fetchone())
        if not testing and caller in _blocked_users(row):
            return None
        if (not testing and (not row["enabled"] or row["status"] != "ready")) or _blockers(row):
            return None
    return PersonalTool(row, testing)


def catalog(caller):
    if not caller or caller in ("guest", "system", "-") or os.getenv("PERSONAL_TOOLS_ENABLED", "true").lower() != "true":
        return []
    result = []
    # 注册门禁：评测通过（eval_status='passed'）才进入聊天可调用目录，
    # 与 standardized_registry 同口径；默认全员可见，黑名单用户除外。
    for row in db.query("SELECT t.id, t.blocked_users, v.function_name FROM personal_tools t "
                        "JOIN personal_tool_versions v ON v.owner_id=t.owner_id AND v.tool_id=t.id AND v.version=t.current_version "
                        "WHERE t.enabled=1 AND t.status='ready' AND t.eval_status='passed'"):
        if caller in _blocked_users(row):
            continue
        try:
            tool = resolve(caller, row["function_name"])
            if tool:
                result.append(tool.to_function_schema())
        except (ValueError, HTTPException, KeyError):
            continue
    return result


def standardized_registry():
    """评测通过（eval_status='passed'）且当前可用的个人工具 → 标准化注册表快照。

    与内置工具 to_spec_dict 同构，附 source/owner/tool_id 供可观测性区分来源；
    未通过评测或存在配置阻塞的工具一律不进入注册表。
    """
    rows = db.query(
        "SELECT t.*, v.version, v.function_name, v.spec_json, v.review_json, v.approved_by FROM personal_tools t "
        "JOIN personal_tool_versions v ON v.owner_id=t.owner_id AND v.tool_id=t.id "
        "AND v.version=t.current_version "
        "WHERE t.eval_status='passed' AND t.enabled=1 AND t.status='ready' ORDER BY t.created_at")
    out = []
    for row in rows:
        try:
            spec = json.loads(row["spec_json"])
            if _blockers(row):
                continue
            out.append({
                "name": row["function_name"],
                "display_name": spec["name"],
                "description": spec["name"] + "：" + spec["description"],
                "namespace": "personal",
                "tags": ["personal_tool", spec["executor"]],
                "scenarios": [spec["description"]],
                "parameters": spec.get("input_schema") or {"type": "object", "properties": {}},
                "permissions": ["authenticated"],
                "risk_level": "low",
                "status": "active",
                "requires_confirmation": False,
                "source": "personal_tool",
                "owner_id": row["owner_id"],
                "tool_id": row["id"],
                "eval_status": "passed",
                "blocked_users": _blocked_users(row),
            })
        except (ValueError, TypeError, KeyError):
            continue
    return out


class PersonalTool(BaseTool):
    namespace = "personal"
    permissions = ("authenticated",)
    strict_schema = True

    def __init__(self, row, testing=False):
        self.row, self.testing = row, testing
        self.spec = json.loads(row["spec_json"])
        self.name, self.description = row["function_name"], self.spec["name"] + "：" + self.spec["description"]
        self.parameters = self.spec["input_schema"]

    async def run(self, ctx, **arguments):
        # 默认全员可调用；resolve 内含黑名单／启用／就绪门禁，凭据始终用属主的
        if not resolve(ctx.user_id, self.name, ctx.tool_test_id):
            return ToolResult(self.name, False, error="permission_denied")
        specs.check_no_credentials(arguments)
        eid = uuid.uuid4().hex
        owner = self.row["owner_id"]
        with db.transaction() as conn:
            current = _load(conn, owner, self.row["id"])
            testing = bool(ctx.tool_test_id and conn.execute(
                "SELECT 1 FROM personal_tool_tests WHERE id=? AND owner_id=? AND tool_id=? AND version=? AND status='running'",
                (ctx.tool_test_id, owner, current["id"], current["version"])).fetchone())
            if not testing and ctx.user_id in _blocked_users(current):
                return ToolResult(self.name, False, error="permission_denied")
            if (current["version"] != self.row["version"] or _blockers(current)
                    or (not testing and (current["status"] != "ready" or not current["enabled"]))):
                return ToolResult(self.name, False, error="tool_configuration_changed")
            if self.spec["executor"] == "python":
                _not_running(conn, owner, current["id"])
            self.row = current
            conn.execute("INSERT INTO personal_tool_executions(id,owner_id,tool_id,version,session_id,message_id,test_id,args_hash,execution_status,validation_status,requirement_status,created_at,caller_id) "
                "VALUES (?,?,?,?,?,?,?,?,'running','unverified','unverified',?,?)", (eid, owner, self.row["id"], self.row["version"],
                ctx.session_id, ctx.message_id, ctx.tool_test_id, specs.fingerprint(arguments), now(), ctx.user_id))
        _RUNNING[eid] = asyncio.current_task()
        state, error, payload = "failed", "execution_failed", {}
        error_detail = ""
        result_origin = "unknown"
        try:
            secret = tool_credentials.load(owner, self.row["id"], self.row["version"], self.row["credential_id"]) if self.spec["config"].get("auth") else ""
            payload = None if ctx.force_refresh or self.testing else tool_result_cache.get(self.row, self.spec, arguments)
            result_origin = "live" if payload is None else "cache"
            if payload is None:
                if self.spec["executor"] == "python":
                    payload = await worker_client.execute(self.row, self.spec, arguments, eid)
                else:
                    payload = await tool_http.execute(self.spec, arguments, secret)
                if not self.testing:
                    tool_result_cache.put(self.row, self.spec, arguments, payload)
            state, error = "completed", ""
            return ToolResult(self.name, True, summary="个人工具返回结构化结果；需求满足状态单独核验", text=specs.canonical(payload["output"]), extra=payload)
        except worker_client.CancellationUnconfirmed:
            state, error = "cancellation_requested", "sandbox_cancellation_unconfirmed"
            raise
        except asyncio.CancelledError:
            state, error = "cancelled", "cancelled"
            raise
        except TimeoutError:
            state, error = "timeout", "tool_timeout"
        except tool_http.ToolExecutionError as exc:
            error = exc.code
            if error == "sandbox_cleanup_unconfirmed":
                state = "cleanup_failed"
        except Exception as exc:
            error = "validation_or_execution_failed"
            # 保留根因：错误码供前端分类，异常详情进日志与结果 extra，不再吞掉真实原因
            logger.warning("个人工具执行异常 tool=%s eid=%s: %s", self.name, eid, exc, exc_info=True)
            error_detail = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            _RUNNING.pop(eid, None)
            db.execute("UPDATE personal_tool_executions SET execution_status=?,validation_status=?,requirement_status=?,error_code=?,finished_at=?,result_origin=? WHERE id=?",
                       (state, (payload or {}).get("validation_status", "failed"), (payload or {}).get("requirement_status", "unmet"), error,
                        "" if state in ("cancellation_requested", "cleanup_failed") else now(), result_origin, eid))
        return ToolResult(self.name, False, summary="个人工具执行未完成或结果验证失败", error=error,
                          extra={"execution_status": state, "validation_status": "failed", "requirement_status": "unmet",
                                 **({"error_detail": error_detail} if error == "validation_or_execution_failed" else {})})


def executions(owner, tool_id):
    get(owner, tool_id)
    return db.query("SELECT * FROM personal_tool_executions WHERE owner_id=? AND tool_id=? ORDER BY created_at DESC LIMIT 50", (owner, tool_id))


async def reconcile_execution(user_id, execution_id):
    # 属主或实际调用者均可核验终止；沙箱取消以属主身份发起
    row = db.query_one("SELECT e.*,v.spec_json FROM personal_tool_executions e JOIN personal_tool_versions v "
                       "ON e.owner_id=v.owner_id AND e.tool_id=v.tool_id AND e.version=v.version "
                       "WHERE e.id=? AND (e.owner_id=? OR e.caller_id=?)",
                       (execution_id, user_id, user_id))
    if not row or execution_id in _RUNNING or row["execution_status"] not in ("running", "cancellation_requested", "cleanup_failed"):
        return
    if json.loads(row["spec_json"])["executor"] == "python":
        try:
            confirmed = await worker_client.cancel(row["owner_id"], execution_id)
        except Exception:
            return
        if confirmed:
            db.execute("UPDATE personal_tool_executions SET execution_status='interrupted',validation_status='failed',requirement_status='unmet',finished_at=? WHERE id=?",
                       (now(), execution_id))
            return {"status": "interrupted"}


def cancel_execution(user_id, execution_id):
    require_owner(user_id)
    row = db.query_one("SELECT execution_status FROM personal_tool_executions WHERE id=? AND (owner_id=? OR caller_id=?)",
                       (execution_id, user_id, user_id))
    if not row:
        raise HTTPException(404, "执行记录不存在或无权访问")
    task = _RUNNING.get(execution_id)
    if task and not task.done():
        if not task.cancelling():
            task.cancel()
        return {"status": "cancellation_requested"}
    return {"status": row["execution_status"]}
