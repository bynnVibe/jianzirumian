"""个人工具标准化评测：样例测试通过 → 自动构建 kind='tool' 评测集 → 逐用例真实调用 +
输出断言评测 → 全部通过（eval_status='passed'）方可进入标准化工具注册表。

评测集与用例由工具的不可变版本声明（spec.tests）派生，复用通用 eval_runs /
eval_results 表，评测界面「工具评测」域可见；也可在评测界面手动复跑。
"""
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime

from app.core import db
from app.services import personal_tool_specs as specs

logger = logging.getLogger("jianziruyang.tool_eval")

_BG_TASKS: set = set()
DATASET_PREFIX = "tooleval_"


def _now() -> str:
    return datetime.now().isoformat()


def dataset_id_for(tool_id: str) -> str:
    return DATASET_PREFIX + tool_id


def _tool_row(tool_id: str):
    return db.query_one(
        "SELECT t.*, v.version, v.function_name, v.spec_json FROM personal_tools t "
        "JOIN personal_tool_versions v ON v.owner_id=t.owner_id AND v.tool_id=t.id "
        "AND v.version=t.current_version WHERE t.id=?", (tool_id,))


def ensure_dataset(owner: str, tool_id: str) -> str:
    """按当前版本声明重建工具评测集与用例（幂等；版本变化时用例随之更新）。"""
    row = _tool_row(tool_id)
    if not row or row["owner_id"] != owner:
        raise ValueError("工具不存在或不属于当前用户")
    spec = json.loads(row["spec_json"])
    samples = spec.get("tests") or []
    if not samples:
        raise ValueError("工具声明缺少样例测试，无法构建评测用例")
    ds_id = dataset_id_for(tool_id)
    name = f"工具评测 · {spec['name']}"
    description = (f"个人工具「{spec['name']}」标准化评测：逐用例真实调用并校验输出断言，"
                   "全部通过后进入标准化工具注册表。用例由工具版本声明自动派生。")
    if db.query_one("SELECT id FROM eval_datasets WHERE id=?", (ds_id,)):
        db.execute("UPDATE eval_datasets SET name=?, description=? WHERE id=?", (name, description, ds_id))
    else:
        db.execute("INSERT INTO eval_datasets (id, name, description, created_at, kind) VALUES (?,?,?,?, 'tool')",
                   (ds_id, name, description, _now()))
    db.execute("DELETE FROM eval_cases WHERE dataset_id=?", (ds_id,))
    for sample in samples:
        db.execute(
            "INSERT INTO eval_cases (id, dataset_id, question, expected_keywords, reference_answer,"
            " expected_source, created_at, expected_tools, expected_outcome, origin)"
            " VALUES (?,?,?,?,?,?,?,?,?, 'tool_test')",
            (uuid.uuid4().hex, ds_id, specs.canonical(sample["input"]), "[]", "", "", _now(),
             json.dumps([row["function_name"]], ensure_ascii=False), specs.canonical(sample["assertions"])))
    return ds_id


def start_tool_run(dataset_id: str, user_id: str = "") -> str:
    """创建评测 run 并后台逐用例执行，立即返回 run_id（与 RAG 评测同口径的并发保护）。"""
    from app.services.evaluation import ConcurrentRunError

    if not dataset_id.startswith(DATASET_PREFIX):
        raise ValueError("不是工具评测集")
    row = _tool_row(dataset_id[len(DATASET_PREFIX):])
    if not row:
        raise ValueError("评测集对应的工具不存在")
    cases = db.query("SELECT * FROM eval_cases WHERE dataset_id=? ORDER BY created_at, rowid", (dataset_id,))
    if not cases:
        raise ValueError("评测集内没有用例，无法发起评测")
    if db.query_one("SELECT id FROM eval_runs WHERE dataset_id=? AND status='running'", (dataset_id,)):
        raise ConcurrentRunError("该评测集已有正在执行的评测，请等待其完成后再发起")
    run_id = uuid.uuid4().hex
    db.execute("INSERT INTO eval_runs (id, dataset_id, status, started_at, finished_at, summary, created_by)"
               " VALUES (?,?, 'running', ?, NULL, '{}', ?)", (run_id, dataset_id, _now(), user_id))
    db.execute("UPDATE personal_tools SET eval_dataset_id=?, eval_run_id=?, eval_status='running', updated_at=? WHERE id=?",
               (dataset_id, run_id, _now(), row["id"]))
    task = asyncio.create_task(_run_all(run_id, row, cases))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    logger.info("发起工具评测 run: %s | tool=%s | cases=%d", run_id, row["id"], len(cases))
    return run_id


async def _run_all(run_id: str, row: dict, cases: list):
    from app.services.tools.base import ToolContext
    from app.services.tools.invoke import invoke_tool

    passed, latencies = 0, []
    try:
        for case in cases:
            t0 = time.monotonic()
            arguments = json.loads(case["question"])
            assertions = json.loads(case["expected_outcome"] or "[]")
            ctx = ToolContext(row["owner_id"], "", "", "", force_refresh=True)
            status, error, answer, ok = "ok", "", "", 0
            try:
                result = await invoke_tool(row["function_name"], ctx, arguments, routed_by="tool_eval")
                if result.success:
                    specs.assert_result(result.extra["output"], arguments, assertions)
                    ok, answer = 1, result.text or ""
                else:
                    status, error = "error", result.error or "invoke_failed"
            except ValueError:
                status, error = "error", "assertion_failed"
            except Exception as exc:
                status, error = "error", str(exc)[:200]
            latency = int((time.monotonic() - t0) * 1000)
            latencies.append(latency)
            passed += ok
            db.execute(
                "INSERT INTO eval_results (id, run_id, case_id, question, answer, status, error,"
                " latency_ms, faithfulness_pass, metric_details)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, run_id, case["id"], case["question"], answer, status, error,
                 latency, ok, json.dumps({"assertions_passed": bool(ok)}, ensure_ascii=False)))
        total = len(cases)
        summary = {"total": total, "ok": passed, "error": total - passed,
                   "pass_rate": round(passed / total, 4) if total else 0.0,
                   "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0}
        db.execute("UPDATE eval_runs SET status='finished', finished_at=?, summary=? WHERE id=?",
                   (_now(), json.dumps(summary, ensure_ascii=False), run_id))
        _mark_tool(row["id"], run_id, "passed" if total and passed == total else "failed")
        logger.info("工具评测 run 完成: %s | %s", run_id, summary)
    except asyncio.CancelledError:
        db.execute("UPDATE eval_runs SET status='interrupted', finished_at=? WHERE id=?", (_now(), run_id))
        _mark_tool(row["id"], run_id, "failed")
        raise
    except Exception as exc:
        logger.error("工具评测 run 整体失败: %s | %s", run_id, exc)
        db.execute("UPDATE eval_runs SET status='failed', finished_at=?, summary=? WHERE id=?",
                   (_now(), json.dumps({"error": str(exc)[:500]}, ensure_ascii=False), run_id))
        _mark_tool(row["id"], run_id, "failed")


def _mark_tool(tool_id: str, run_id: str, eval_status: str):
    db.execute("UPDATE personal_tools SET eval_status=?, updated_at=? WHERE id=? AND eval_run_id=?",
               (eval_status, _now(), tool_id, run_id))


def schedule_after_test(owner: str, tool_id: str):
    """样例测试通过后自动进入评测（后台运行，不阻塞聊天确认响应）。"""
    try:
        ds_id = ensure_dataset(owner, tool_id)
        start_tool_run(ds_id, owner)
    except Exception as exc:
        logger.warning("工具测试通过后自动评测未启动（不阻断）: %s | %s", tool_id, exc)