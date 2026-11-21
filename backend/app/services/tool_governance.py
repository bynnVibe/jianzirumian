"""工具治理中心：个人工具就绪／总览／监控／评测／审批的唯一集中后台模块。

聊天草稿链（propose/confirm）仍走 personal_tools；管理侧操作（配置／凭据／
样例测试／复评／审批／执行取消）统一收口到本模块对应的 API，内部复用
personal_tools / tool_eval 的单一写点，不另造状态机。

注册门禁不变量：聊天目录 catalog、标准化注册表 standardized_registry、
Skill 依赖可用性 _available 均要求 eval_status='passed'——评测通过后才在
注册界面正常显示并可被聊天调用；未通过的工具只在本中心暴露进度与阻塞原因。
"""
import asyncio
import json
import logging
import os

from fastapi import HTTPException

from app.core import db
from app.services import personal_tools as tools, tool_eval, tool_credentials
from app.services.sandbox import worker_client

logger = logging.getLogger("jianziruyang.tool_governance")

STAGE_LABELS = {
    "blocked": "存在阻塞项",
    "awaiting_test": "待配置／样例测试",
    "testing": "样例测试中",
    "awaiting_eval": "待标准化评测",
    "evaluating": "标准化评测中",
    "eval_failed": "评测未通过",
    "awaiting_enable": "评测通过·待启用",
    "registered": "已注册·可调用",
    "disabled": "已停用",
}


def readiness() -> list:
    """全局就绪仪表盘：把聊天里才会暴露的失败根因（如 tool_decision 模型
    未配置原生 Function Calling）前置显性化，并给出修复入口。"""
    items = []
    enabled = os.getenv("PERSONAL_TOOLS_ENABLED", "true").lower() == "true"
    items.append({"key": "feature", "label": "个人工具功能开关", "ok": enabled,
                  "detail": "PERSONAL_TOOLS_ENABLED=" + ("true" if enabled else "false"),
                  "hint": "环境变量 PERSONAL_TOOLS_ENABLED 设为 true 后重启后端。", "link": ""})
    cred_ok = tool_credentials.available()
    items.append({"key": "credentials", "label": "凭据加密主密钥", "ok": cred_ok,
                  "detail": "已配置，可加密保存 API 凭据" if cred_ok else "未配置 TOOL_CREDENTIAL_MASTER_KEY",
                  "hint": "声明 auth 的工具须配置服务端主密钥后才能就绪；凭据仅加密保存。", "link": ""})
    profile = worker_client.runtime_profile()
    sandbox_ok = worker_client.configured() and bool(profile)
    items.append({"key": "sandbox", "label": "隔离脚本 Worker", "ok": sandbox_ok,
                  "detail": "固定镜像与 seccomp 已配置" if sandbox_ok else "未配置固定镜像／seccomp 或 Worker 地址",
                  "hint": "python 执行器须专用 Linux 隔离 Worker 验收后就绪；未就绪一律禁止主机执行。", "link": ""})
    decision_ok, decision_detail = True, ""
    try:
        from app.core.llm import LLMFactory
        llm = LLMFactory.create_for_role("tool_decision", require_tools=True)
        decision_detail = f"tool_decision 路由就绪，模型 {llm.model} 支持原生 Function Calling"
    except Exception as exc:
        decision_ok, decision_detail = False, str(exc)
    items.append({"key": "tool_decision", "label": "工具决策模型（原生 FC）", "ok": decision_ok,
                  "detail": decision_detail,
                  "hint": "在模型网关为 tool_decision 业务路由绑定上游，并在上游「原生工具模型」声明该模型。",
                  "link": "/admin/gateway"})
    return items


def _stage(pub: dict) -> str:
    if pub.get("blockers"):
        return "blocked"
    status, ev = pub.get("status"), pub.get("eval_status") or ""
    if status == "testing":
        return "testing"
    if ev == "running":
        return "evaluating"
    if ev == "failed":
        return "eval_failed"
    if ev == "passed":
        if pub.get("enabled"):
            return "registered"
        return "disabled" if status == "disabled" else "awaiting_enable"
    if status == "ready":
        return "awaiting_eval"
    if status == "disabled":
        return "disabled"
    return "awaiting_test"


def _eval_summary(pub: dict) -> dict:
    run_id = pub.get("eval_run_id") or ""
    if not run_id:
        return {}
    run = db.query_one("SELECT id,status,summary,started_at,finished_at FROM eval_runs WHERE id=?", (run_id,))
    if not run:
        return {}
    try:
        summary = json.loads(run["summary"] or "{}")
    except (ValueError, TypeError):
        summary = {}
    return {"run_id": run["id"], "status": run["status"], "started_at": run["started_at"],
            "finished_at": run["finished_at"], **summary}


def _exec_stats(owner: str, tool_id: str) -> dict:
    row = db.query_one(
        "SELECT COUNT(*) total, SUM(CASE WHEN execution_status='completed' THEN 1 ELSE 0 END) completed,"
        " SUM(CASE WHEN validation_status='passed' THEN 1 ELSE 0 END) validated"
        " FROM personal_tool_executions WHERE owner_id=? AND tool_id=?", (owner, tool_id))
    total = row["total"] or 0
    completed = row["completed"] or 0
    return {"total": total, "completed": completed,
            "success_rate": round(completed / total, 4) if total else None,
            "validated": row["validated"] or 0}


def _owner_name(owner: str) -> str:
    row = db.query_one("SELECT username FROM users WHERE id=?", (owner,))
    return row["username"] if row else owner


def overview(user: dict) -> dict:
    """集中总览：就绪仪表盘 + 工具生命周期列表 + 待确认草稿 + 管理员待审。

    管理员与运维可见全部工具（运维只读）；普通用户不可访问治理中心。"""
    admin = user.get("role") == "admin"
    can_ops = admin or user.get("role") == "ops"
    owner = user["id"]
    rows = db.query("SELECT id, owner_id FROM personal_tools ORDER BY created_at DESC" if can_ops
                    else "SELECT id, owner_id FROM personal_tools WHERE owner_id=? ORDER BY created_at DESC",
                    () if can_ops else (owner,))
    tools_out = []
    for row in rows:
        pub = tools.get(row["owner_id"], row["id"])
        stage = _stage(pub)
        tools_out.append({**pub, "owner_id": row["owner_id"], "owner_name": _owner_name(row["owner_id"]), "stage": stage,
                          "stage_label": STAGE_LABELS[stage],
                          "registered": stage == "registered",
                          "eval_summary": _eval_summary(pub),
                          "exec_stats": _exec_stats(row["owner_id"], row["id"])})
    drafts = db.query(
        "SELECT id, session_id, spec_json, created_at, expires_at FROM personal_tool_drafts"
        " WHERE owner_id=? AND status='awaiting_confirmation' AND expires_at>? ORDER BY created_at DESC",
        (owner, tools.now()))
    pending_drafts = [{"id": r["id"], "session_id": r["session_id"], "created_at": r["created_at"],
                       "name": (json.loads(r["spec_json"]).get("name") or "个人工具")} for r in drafts]
    result = {"readiness": readiness(), "tools": tools_out, "pending_drafts": pending_drafts,
              "is_admin": admin}
    if admin:
        result["reviews"] = tools.pending_reviews()
    return result


def registry(user: dict) -> dict:
    """注册界面唯一快照：内置工具 + 评测通过的个人工具。

    个人工具默认全员可调用，注册表对管理员/运维展示全部属主的工具，
    附 owner_name 与 blocked_users 供调用范围管理。"""
    from app.services.tools import base as tools_base
    personal = tools.standardized_registry()
    for p in personal:
        p["owner_name"] = _owner_name(p["owner_id"])
    return {"tools": tools_base.registry_snapshot() + personal}


def _owned_tool(user: dict, tool_id: str) -> dict:
    row = db.query_one("SELECT owner_id FROM personal_tools WHERE id=?", (tool_id,))
    if not row or (row["owner_id"] != user["id"] and user.get("role") != "admin"):
        raise HTTPException(404, "工具不存在或无权访问")
    return row


def _actor_owner(user: dict, tool_id: str) -> str:
    """写入操作的实际属主：属主本人或管理员代管；运维只读不可写。"""
    return _owned_tool(user, tool_id)["owner_id"]


def _view_tool(user: dict, tool_id: str) -> dict:
    """只读监控：属主／管理员／运维均可查看。"""
    row = db.query_one("SELECT owner_id FROM personal_tools WHERE id=?", (tool_id,))
    if not row or (row["owner_id"] != user["id"] and user.get("role") not in ("admin", "ops")):
        raise HTTPException(404, "工具不存在或无权访问")
    return row


def monitoring(user: dict, tool_id: str) -> dict:
    """单工具监控：配置/阻塞 + 样例测试历史 + 评测逐用例结果 + 执行记录。"""
    row = _view_tool(user, tool_id)
    pub = tools.get(row["owner_id"], tool_id)
    tests = db.query("SELECT id,version,status,report_json,created_at,finished_at FROM personal_tool_tests"
                     " WHERE owner_id=? AND tool_id=? ORDER BY created_at DESC LIMIT 20", (row["owner_id"], tool_id))
    executions = [dict(e) for e in tools.executions(row["owner_id"], tool_id)]
    for e in executions:
        e["caller_name"] = _owner_name(e.get("caller_id") or row["owner_id"])
    eval_block = {}
    ds_id = pub.get("eval_dataset_id") or ""
    if ds_id:
        run_id = pub.get("eval_run_id") or ""
        results = db.query(
            "SELECT r.id,r.case_id,r.question,r.answer,r.status,r.error,r.latency_ms,r.metric_details,"
            " c.expected_outcome FROM eval_results r LEFT JOIN eval_cases c ON c.id=r.case_id"
            " WHERE r.run_id=? ORDER BY r.rowid", (run_id,)) if run_id else []
        eval_block = {"dataset_id": ds_id, "run_id": run_id, "summary": _eval_summary(pub), "results": results}
    return {"tool": {**pub, "owner_id": row["owner_id"], "stage": _stage(pub), "stage_label": STAGE_LABELS[_stage(pub)]},
            "tests": tests, "executions": executions, "eval": eval_block}


def reeval(user: dict, tool_id: str) -> dict:
    """集中复评入口：须本版本样例测试通过且无阻塞；评测中不可并发复跑。"""
    owner = _actor_owner(user, tool_id)
    pub = tools.get(owner, tool_id)
    if pub["blockers"]:
        raise HTTPException(409, "存在阻塞项，先完成审批／条款／凭据／隔离配置后再评测")
    passed = db.query_one("SELECT 1 FROM personal_tool_tests WHERE owner_id=? AND tool_id=? AND version=? AND status='passed'",
                          (owner, tool_id, pub["version"]))
    if not passed:
        raise HTTPException(409, "本版本样例测试尚未通过，请先执行只读样例测试")
    if pub.get("eval_status") == "running":
        raise HTTPException(409, "标准化评测正在进行，请等待完成后再复跑")
    ds_id = tool_eval.ensure_dataset(owner, tool_id)
    run_id = tool_eval.start_tool_run(ds_id, user["id"])
    logger.info("治理中心发起复评: tool=%s | run=%s | actor=%s", tool_id, run_id, user["id"])
    return {"run_id": run_id, "dataset_id": ds_id}


async def run_workflow(user: dict, tool_id: str) -> dict:
    """一键跑完生命周期评测工作流：样例测试 → 标准化评测（自动）→ 启用注册。

    聊天 @tool 确认后的自动链（confirm→test→schedule_after_test）与本入口
    复用同一写点；阻塞项未补齐时拒绝启动，评测超时则交由前端轮询继续跟踪。"""
    owner = _actor_owner(user, tool_id)
    pub = tools.get(owner, tool_id)
    if pub["blockers"]:
        raise HTTPException(409, "存在阻塞项，先完成审批／条款／凭据／隔离配置后再运行工作流")
    if pub["status"] == "testing" or pub.get("eval_status") == "running":
        raise HTTPException(409, "工具正在测试或评测中，请等待当前流程完成")
    pub = await tools.test(owner, tool_id)
    deadline = asyncio.get_event_loop().time() + 100
    while pub.get("eval_status") == "running" and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(2)
        pub = tools.get(owner, tool_id)
    if (pub.get("eval_status") == "passed" and not pub["enabled"] and not pub["blockers"]
            and pub["status"] in ("ready", "disabled")):
        pub = tools.configure(owner, tool_id, enabled=True)
    stage = _stage(pub)
    logger.info("治理中心运行完整工作流: tool=%s | stage=%s | actor=%s", tool_id, stage, user["id"])
    return {**pub, "owner_id": owner, "stage": stage, "stage_label": STAGE_LABELS[stage],
            "registered": stage == "registered"}


async def cancel_execution(user: dict, execution_id: str) -> dict:
    """治理侧取消：属主／实际调用者可取消本人执行；管理员可代管取消。"""
    row = db.query_one("SELECT owner_id FROM personal_tool_executions WHERE id=?", (execution_id,))
    if not row:
        raise HTTPException(404, "执行记录不存在")
    actor = row["owner_id"] if user.get("role") == "admin" else user["id"]
    result = tools.cancel_execution(actor, execution_id)
    if result["status"] in ("running", "cancellation_requested", "cleanup_failed"):
        result = (await tools.reconcile_execution(actor, execution_id)) or result
    return result