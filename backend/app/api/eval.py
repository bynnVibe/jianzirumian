"""
见字如面 - Agent 回归评测 API（全部管理员受限）

评测集/用例的增删查、批量导入、从点踩反馈导入、发起评测运行、
运行历史与明细查询。所有端点均通过 require_ops 校验。
"""
import json
import logging
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.deps import require_ops
from app.core import db
from app.services import evaluation
from app.services import prompt_skill_eval
from app.services import prompt_store

logger = logging.getLogger("jianziruyang.api.eval")

router = APIRouter(prefix="/api/eval", tags=["eval"])


def _now() -> str:
    return datetime.now().isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class DatasetCreate(BaseModel):
    name: str
    description: str = ""


class CaseCreate(BaseModel):
    question: str
    expected_keywords: List[str] = []
    reference_answer: str = ""
    expected_source: str = ""


class CaseImportItem(BaseModel):
    question: str
    expected_keywords: Optional[List[str]] = []
    reference_answer: Optional[str] = ""
    expected_source: Optional[str] = ""


class CaseImport(BaseModel):
    cases: List[CaseImportItem]


# ---------------------------------------------------------------------------
# 内置基准集接地重建
# ---------------------------------------------------------------------------
@router.post("/builtin/refresh")
async def refresh_builtin(_admin: dict = Depends(require_ops)):
    """以最新内置用例原地重建内置评测集（知识库内容变化后重新接地基准集）。

    仅重建两个内置评测集的用例行，保留评测集 id 与历史运行记录；不影响用户自建评测集。
    """
    refreshed = evaluation.refresh_builtin_datasets()
    if not refreshed:
        raise HTTPException(status_code=404, detail="未找到内置评测集，无法重建")
    return {"refreshed": refreshed}


# ---------------------------------------------------------------------------
# 评测集
# ---------------------------------------------------------------------------
@router.get("/datasets")
async def list_datasets(_admin: dict = Depends(require_ops)):
    """评测集列表（含用例数与最近一次运行状态）"""
    rows = db.query("SELECT * FROM eval_datasets ORDER BY created_at DESC")
    result = []
    for d in rows:
        case_count = db.query_one(
            "SELECT COUNT(*) AS c FROM eval_cases WHERE dataset_id = ?", (d["id"],)
        )["c"]
        last_run = db.query_one(
            "SELECT id, status, started_at, finished_at, summary FROM eval_runs"
            " WHERE dataset_id = ? ORDER BY started_at DESC LIMIT 1",
            (d["id"],),
        )
        result.append({
            **d,
            "case_count": case_count,
            "last_run": last_run,
        })
    return {"success": True, "datasets": result, "total": len(result)}


@router.post("/datasets")
async def create_dataset(body: DatasetCreate, _admin: dict = Depends(require_ops)):
    """新建评测集"""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="评测集名称不能为空")
    ds_id = _new_id()
    db.execute(
        "INSERT INTO eval_datasets (id, name, description, created_at) VALUES (?,?,?,?)",
        (ds_id, name, body.description.strip(), _now()),
    )
    row = db.query_one("SELECT * FROM eval_datasets WHERE id = ?", (ds_id,))
    return {"success": True, "dataset": {**row, "case_count": 0, "last_run": None}}


@router.delete("/datasets/{dataset_id}")
async def delete_dataset(dataset_id: str, _admin: dict = Depends(require_ops)):
    """删除评测集（级联删除用例、运行与结果）"""
    row = db.query_one("SELECT id FROM eval_datasets WHERE id = ?", (dataset_id,))
    if not row:
        raise HTTPException(status_code=404, detail="评测集不存在")
    run_ids = [r["id"] for r in db.query(
        "SELECT id FROM eval_runs WHERE dataset_id = ?", (dataset_id,)
    )]
    for rid in run_ids:
        db.execute("DELETE FROM eval_results WHERE run_id = ?", (rid,))
    db.execute("DELETE FROM eval_runs WHERE dataset_id = ?", (dataset_id,))
    db.execute("DELETE FROM eval_cases WHERE dataset_id = ?", (dataset_id,))
    db.execute("DELETE FROM eval_datasets WHERE id = ?", (dataset_id,))
    return {"success": True, "message": "评测集已删除"}


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------
def _case_to_dict(row: dict) -> dict:
    d = dict(row)
    try:
        d["expected_keywords"] = json.loads(d.get("expected_keywords") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["expected_keywords"] = []
    return d


@router.get("/datasets/{dataset_id}/cases")
async def list_cases(dataset_id: str, _admin: dict = Depends(require_ops)):
    """评测集内用例列表"""
    rows = db.query(
        "SELECT * FROM eval_cases WHERE dataset_id = ? ORDER BY created_at, rowid",
        (dataset_id,),
    )
    cases = [_case_to_dict(r) for r in rows]
    return {"success": True, "cases": cases, "total": len(cases)}


@router.post("/datasets/{dataset_id}/cases")
async def create_case(dataset_id: str, body: CaseCreate, _admin: dict = Depends(require_ops)):
    """新增单条用例"""
    if not db.query_one("SELECT id FROM eval_datasets WHERE id = ?", (dataset_id,)):
        raise HTTPException(status_code=404, detail="评测集不存在")
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="用例问题不能为空")
    case_id = _new_id()
    db.execute(
        "INSERT INTO eval_cases (id, dataset_id, question, expected_keywords,"
        " reference_answer, expected_source, created_at) VALUES (?,?,?,?,?,?,?)",
        (
            case_id, dataset_id, question,
            json.dumps(body.expected_keywords or [], ensure_ascii=False),
            body.reference_answer or "", body.expected_source or "", _now(),
        ),
    )
    row = db.query_one("SELECT * FROM eval_cases WHERE id = ?", (case_id,))
    return {"success": True, "case": _case_to_dict(row)}


@router.delete("/cases/{case_id}")
async def delete_case(case_id: str, _admin: dict = Depends(require_ops)):
    """删除单条用例"""
    if not db.query_one("SELECT id FROM eval_cases WHERE id = ?", (case_id,)):
        raise HTTPException(status_code=404, detail="用例不存在")
    db.execute("DELETE FROM eval_cases WHERE id = ?", (case_id,))
    return {"success": True, "message": "用例已删除"}


@router.post("/datasets/{dataset_id}/cases/import")
async def import_cases(dataset_id: str, body: CaseImport, _admin: dict = Depends(require_ops)):
    """JSON 批量导入用例"""
    if not db.query_one("SELECT id FROM eval_datasets WHERE id = ?", (dataset_id,)):
        raise HTTPException(status_code=404, detail="评测集不存在")
    if not body.cases:
        raise HTTPException(status_code=400, detail="导入内容为空")

    params = []
    imported = 0
    for c in body.cases:
        question = (c.question or "").strip()
        if not question:
            continue
        params.append((
            _new_id(), dataset_id, question,
            json.dumps(c.expected_keywords or [], ensure_ascii=False),
            c.reference_answer or "", c.expected_source or "", _now(),
        ))
        imported += 1
    if not params:
        raise HTTPException(status_code=400, detail="没有有效的用例（question 均为空）")
    db.executemany(
        "INSERT INTO eval_cases (id, dataset_id, question, expected_keywords,"
        " reference_answer, expected_source, created_at) VALUES (?,?,?,?,?,?,?)",
        params,
    )
    return {"success": True, "imported": imported, "message": f"已导入 {imported} 条用例"}


@router.post("/datasets/{dataset_id}/cases/from-feedback")
async def import_cases_from_feedback(dataset_id: str, _admin: dict = Depends(require_ops)):
    """从点踩反馈（message_feedbacks rating=dislike）导入对应用户问题，去重。

    点踩记录挂在助手消息上，取其同会话中紧邻的上一条用户消息作为评测问题；
    与该评测集已有用例问题去重，避免重复导入。
    """
    if not db.query_one("SELECT id FROM eval_datasets WHERE id = ?", (dataset_id,)):
        raise HTTPException(status_code=404, detail="评测集不存在")

    # 已存在问题（去重基准）
    existing = {
        r["question"].strip()
        for r in db.query(
            "SELECT question FROM eval_cases WHERE dataset_id = ?", (dataset_id,)
        )
    }

    dislike_rows = db.query(
        "SELECT f.session_id, f.message_id, m.seq"
        " FROM message_feedbacks f"
        " JOIN chat_messages m ON m.message_id = f.message_id"
        " WHERE f.rating = 'dislike'"
        " ORDER BY f.created_at DESC"
    )

    params = []
    seen = set(existing)
    for row in dislike_rows:
        q_row = db.query_one(
            "SELECT content FROM chat_messages"
            " WHERE session_id = ? AND role = 'user' AND seq < ?"
            " ORDER BY seq DESC LIMIT 1",
            (row["session_id"], row["seq"]),
        )
        if not q_row:
            continue
        question = (q_row["content"] or "").strip()
        if not question or question in seen:
            continue
        seen.add(question)
        params.append((
            _new_id(), dataset_id, question,
            json.dumps([], ensure_ascii=False), "", "", _now(),
        ))

    if params:
        db.executemany(
            "INSERT INTO eval_cases (id, dataset_id, question, expected_keywords,"
            " reference_answer, expected_source, created_at) VALUES (?,?,?,?,?,?,?)",
            params,
        )
    return {
        "success": True,
        "imported": len(params),
        "message": f"已从点踩反馈导入 {len(params)} 条用例",
    }


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------
@router.post("/datasets/{dataset_id}/runs")
async def start_run(dataset_id: str, admin: dict = Depends(require_ops)):
    """发起评测运行（后台逐用例执行，立即返回 run_id）"""
    try:
        run_id = evaluation.start_run(dataset_id, admin)
    except evaluation.ConcurrentRunError as e:
        # 并发保护：统一 code/message 结构 + 409
        return JSONResponse(
            status_code=409,
            content={"code": "eval_run_in_progress", "message": str(e)},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "run_id": run_id, "status": "running"}


@router.get("/runs")
async def list_runs(
    dataset_id: str = Query(""),
    _admin: dict = Depends(require_ops),
):
    """运行历史（含 summary 与已完成用例进度计数）。

    dataset_id 为空时返回全部评测集的运行（实验页签），附 dataset_name。
    """
    if dataset_id:
        rows = db.query(
            "SELECT * FROM eval_runs WHERE dataset_id = ? ORDER BY started_at DESC",
            (dataset_id,),
        )
    else:
        rows = db.query("SELECT * FROM eval_runs ORDER BY started_at DESC LIMIT 200")
    ds_names = {r["id"]: r["name"] for r in db.query("SELECT id, name FROM eval_datasets")}
    case_totals: dict = {}
    runs = []
    for r in rows:
        ds_id = r["dataset_id"]
        if ds_id not in case_totals:
            case_totals[ds_id] = db.query_one(
                "SELECT COUNT(*) AS c FROM eval_cases WHERE dataset_id = ?", (ds_id,)
            )["c"]
        completed = db.query_one(
            "SELECT COUNT(*) AS c FROM eval_results WHERE run_id = ?", (r["id"],)
        )["c"]
        try:
            summary = json.loads(r.get("summary") or "{}")
        except (json.JSONDecodeError, TypeError):
            summary = {}
        runs.append({
            "id": r["id"],
            "dataset_id": ds_id,
            "dataset_name": ds_names.get(ds_id, "(评测集已删除)"),
            "status": r["status"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "created_by": r["created_by"],
            "summary": summary,
            "completed": completed,
            "total": case_totals[ds_id],
        })
    return {"success": True, "runs": runs, "total": len(runs)}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, _admin: dict = Depends(require_ops)):
    """运行详情（run + 聚合 summary + 逐用例结果明细 + 上一次 finished run 环比）"""
    run = db.query_one("SELECT * FROM eval_runs WHERE id = ?", (run_id,))
    if not run:
        raise HTTPException(status_code=404, detail="评测运行不存在")
    try:
        summary = json.loads(run.get("summary") or "{}")
    except (json.JSONDecodeError, TypeError):
        summary = {}

    result_rows = db.query(
        "SELECT * FROM eval_results WHERE run_id = ? ORDER BY rowid", (run_id,)
    )
    results = []
    for r in result_rows:
        d = dict(r)
        try:
            d["retrieved_sources"] = json.loads(d.get("retrieved_sources") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["retrieved_sources"] = []
        try:
            d["metric_details"] = json.loads(d.get("metric_details") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["metric_details"] = {}
        results.append(d)

    # 环比基准：本 run 之前、同评测集最近一次 finished run
    prev = db.query_one(
        "SELECT id, summary FROM eval_runs"
        " WHERE dataset_id = ? AND status = 'finished' AND started_at < ?"
        " ORDER BY started_at DESC LIMIT 1",
        (run["dataset_id"], run["started_at"]),
    )
    prev_summary = {}
    if prev:
        try:
            prev_summary = json.loads(prev.get("summary") or "{}")
        except (json.JSONDecodeError, TypeError):
            prev_summary = {}

    return {
        "success": True,
        "run": {
            "id": run["id"],
            "dataset_id": run["dataset_id"],
            "status": run["status"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "created_by": run["created_by"],
            "summary": summary,
        },
        "results": results,
        "prev_run_id": prev["id"] if prev else None,
        "prev_summary": prev_summary,
    }


# ---------------------------------------------------------------------------
# 评估器（基于已有评测集构建的评测工作流）
# ---------------------------------------------------------------------------
class EvaluatorCreate(BaseModel):
    name: str
    dataset_id: str
    description: str = ""


@router.get("/evaluators")
async def list_evaluators(_admin: dict = Depends(require_ops)):
    """评估器列表（绑定评测集名称/用例数 + 运行次数与最近一次运行）"""
    rows = db.query("SELECT * FROM eval_evaluators ORDER BY created_at DESC")
    result = []
    for e in rows:
        ds = db.query_one("SELECT id, name FROM eval_datasets WHERE id = ?", (e["dataset_id"],))
        case_count = db.query_one(
            "SELECT COUNT(*) AS c FROM eval_cases WHERE dataset_id = ?", (e["dataset_id"],)
        )["c"]
        run_count = db.query_one(
            "SELECT COUNT(*) AS c FROM eval_runs WHERE dataset_id = ?", (e["dataset_id"],)
        )["c"]
        last_run = db.query_one(
            "SELECT id, status, started_at, finished_at, summary FROM eval_runs"
            " WHERE dataset_id = ? ORDER BY started_at DESC LIMIT 1",
            (e["dataset_id"],),
        )
        result.append({
            **e,
            "dataset_name": ds["name"] if ds else "(评测集已删除)",
            "dataset_exists": bool(ds),
            "case_count": case_count,
            "run_count": run_count,
            "last_run": last_run,
        })
    return {"success": True, "evaluators": result, "total": len(result)}


@router.post("/evaluators")
async def create_evaluator(body: EvaluatorCreate, admin: dict = Depends(require_ops)):
    """基于已有评测集创建评估器（评测工作流）"""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="评估器名称不能为空")
    ds = db.query_one("SELECT id FROM eval_datasets WHERE id = ?", (body.dataset_id,))
    if not ds:
        raise HTTPException(status_code=404, detail="绑定的评测集不存在")
    ev_id = _new_id()
    db.execute(
        "INSERT INTO eval_evaluators (id, name, dataset_id, description, created_by, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (ev_id, name, body.dataset_id, body.description.strip(), admin.get("username", ""), _now()),
    )
    row = db.query_one("SELECT * FROM eval_evaluators WHERE id = ?", (ev_id,))
    return {"success": True, "evaluator": row}


class EvaluatorUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


@router.put("/evaluators/{evaluator_id}")
async def update_evaluator(evaluator_id: str, body: EvaluatorUpdate, _admin: dict = Depends(require_ops)):
    """编辑评估器名称/描述（绑定评测集不可变更）"""
    row = db.query_one("SELECT * FROM eval_evaluators WHERE id = ?", (evaluator_id,))
    if not row:
        raise HTTPException(status_code=404, detail="评估器不存在")
    name = (body.name if body.name is not None else row["name"]).strip()
    if not name:
        raise HTTPException(status_code=400, detail="评估器名称不能为空")
    description = (body.description if body.description is not None else row["description"]).strip()
    db.execute(
        "UPDATE eval_evaluators SET name = ?, description = ? WHERE id = ?",
        (name, description, evaluator_id),
    )
    updated = db.query_one("SELECT * FROM eval_evaluators WHERE id = ?", (evaluator_id,))
    return {"success": True, "evaluator": updated}


@router.delete("/evaluators/{evaluator_id}")
async def delete_evaluator(evaluator_id: str, _admin: dict = Depends(require_ops)):
    """删除评估器（不影响评测集与运行历史）"""
    row = db.query_one("SELECT id FROM eval_evaluators WHERE id = ?", (evaluator_id,))
    if not row:
        raise HTTPException(status_code=404, detail="评估器不存在")
    db.execute("DELETE FROM eval_evaluators WHERE id = ?", (evaluator_id,))
    return {"success": True}


# ---------------------------------------------------------------------------
# Prompt / Skill 专项评测（静态断言 + Rubric 打分 + A/B + 优化上线）
# ---------------------------------------------------------------------------
class PromptSkillEvalCreate(BaseModel):
    target_type: str            # prompt | skill
    target_id: str = ""         # skill 时为技能 id；prompt 固定 system_prompt


class PromptSkillABCreate(BaseModel):
    dataset_id: str


class PromptSkillPublish(BaseModel):
    content: Optional[str] = None   # 缺省用建议 optimized 文本；可在建议基础上二次编辑后上线


@router.get("/prompt-skill/targets")
async def list_prompt_skill_targets(admin: dict = Depends(require_ops)):
    """可评测目标：系统提示词（当前生效）+ 当前管理员的用户技能。"""
    owner_id = admin.get("id") or ""
    return {"success": True, "targets": prompt_skill_eval.list_targets(owner_id)}


@router.get("/prompt-skill/prompt-versions")
async def list_prompt_versions(_admin: dict = Depends(require_ops)):
    """系统提示词版本历史与当前生效版本（「上线」记录）。"""
    return {
        "success": True,
        "active": prompt_store.get_active_version(),
        "default_body": prompt_store.DEFAULT_BODY,
        "versions": prompt_store.list_versions(),
    }


@router.post("/prompt-skill/evaluate")
async def create_prompt_skill_eval(body: PromptSkillEvalCreate, admin: dict = Depends(require_ops)):
    """发起 Prompt/Skill 评测（后台执行断言 + Rubric 打分 + 优化建议，立即返回 eval_id）。"""
    owner_id = admin.get("id") or ""
    try:
        eval_id = prompt_skill_eval.start_evaluation(
            body.target_type, body.target_id, owner_id, admin
        )
    except prompt_skill_eval.ConcurrentEvalError as e:
        return JSONResponse(status_code=409,
                            content={"code": "prompt_eval_in_progress", "message": str(e)})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "eval_id": eval_id, "status": "running"}


@router.get("/prompt-skill/evals")
async def list_prompt_skill_evals(
    target_type: str = Query(""),
    target_id: str = Query(""),
    limit: int = Query(30, ge=1, le=100),
    _admin: dict = Depends(require_ops),
):
    """Prompt/Skill 评测历史（概要，不含长文本正文）。"""
    return {"success": True, **prompt_skill_eval.list_evals(target_type, target_id, limit)}


@router.get("/prompt-skill/evals/{eval_id}")
async def get_prompt_skill_eval(eval_id: str, _admin: dict = Depends(require_ops)):
    """评测详情：断言 + Rubric 各维度 + 优化建议 + A/B 结果 + 上线状态。"""
    data = prompt_skill_eval.get_eval(eval_id)
    if not data:
        raise HTTPException(status_code=404, detail="评测记录不存在")
    return {"success": True, "eval": data}


@router.post("/prompt-skill/evals/{eval_id}/ab")
async def start_prompt_skill_ab(eval_id: str, body: PromptSkillABCreate,
                                admin: dict = Depends(require_ops)):
    """对优化候选发起 A/B 行为对比（在真实问答链路上 pairwise 裁判，后台执行）。"""
    try:
        result = prompt_skill_eval.start_ab(eval_id, body.dataset_id, admin)
    except prompt_skill_eval.ConcurrentEvalError as e:
        return JSONResponse(status_code=409,
                            content={"code": "prompt_eval_in_progress", "message": str(e)})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, **result}


@router.post("/prompt-skill/evals/{eval_id}/publish")
async def publish_prompt_skill(eval_id: str, body: PromptSkillPublish,
                               admin: dict = Depends(require_ops)):
    """上线：Prompt 发布为新版本（chat 即刻采用）；Skill 更新内容（重跑安全预审）。"""
    result = prompt_skill_eval.publish(eval_id, admin, body.content)
    if result.get("error"):
        return JSONResponse(status_code=400, content={"success": False, **result})
    return {"success": True, **result}

