"""
见字如面 - CI/CD 流水线服务（运维角色专用）

面向 Agent 系统的评估驱动部署（EDD, Evaluation-Driven Deployment）流水线：
    代码提交 → 单元测试 → 评估回归 → 构建镜像 → 灰度发布 → 全量上线

设计要点：
- 一次运行 = 一条 pipeline_runs 记录 + 六条 pipeline_stages 记录，后台异步串行执行，
  前端轮询 run 详情即可看到每个阶段的实时状态与日志（执行状态显性化）。
- 评估回归阶段（EDD）：调用回归评测 evaluation.start_run 跑一遍评测集，取 Ragas 四指标
  均值作为质量分；分数低于阈值（默认 0.85）则判为 failed 并阻断后续发布阶段。
- 单元测试 / 构建镜像 / 灰度 / 全量：以子进程执行（pytest / docker build / 自定义命令），
  可通过 pipeline_settings 开关与命令配置；未配置的发布命令阶段自动 skipped。
- 任一阶段 failed 立即阻断：其后所有阶段标记 skipped，run 状态置 failed。
- 支持人工中止（abort）：置 abort 标志，执行循环在阶段边界检测后停止。

失败静默降级：任何阶段异常都转成该阶段 failed + 日志，不影响其它运行与主服务。
"""
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from app.core import db

logger = logging.getLogger("jianziruyang.pipeline")

# 项目根目录（backend 的上一级）与后端目录
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
PROJECT_ROOT = BACKEND_DIR.parent

# 六个固定阶段：(name, 中文标签)
STAGES = [
    ("commit", "代码提交"),
    ("unit_test", "单元测试"),
    ("eval_regression", "评估回归"),
    ("build_image", "构建镜像"),
    ("canary", "灰度发布"),
    ("full", "全量上线"),
]
STAGE_LABELS = dict(STAGES)

# 单条命令子进程最长执行时间（秒），防止卡死
_CMD_TIMEOUT = 600
# 评估回归轮询评测运行的最长等待（秒）与轮询间隔
_EVAL_TIMEOUT = 900
_EVAL_POLL_INTERVAL = 3

# 运行中标志：run_id -> bool，abort 时置 True 由执行循环在阶段边界检测
_abort_flags: dict = {}


def _now() -> str:
    return datetime.now().isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

_DEFAULT_SETTINGS = {
    "edd_threshold": 0.85,
    "edd_dataset_id": "",
    "run_unit_test": 1,
    "run_build_image": 0,
    "build_cmd": "",
    "canary_cmd": "",
    "full_cmd": "",
}


def get_settings() -> dict:
    """读取流水线配置（不存在则返回默认值）。"""
    row = db.query_one("SELECT * FROM pipeline_settings WHERE id = 'default'")
    if not row:
        return {**_DEFAULT_SETTINGS, "id": "default", "updated_at": _now()}
    out = dict(row)
    out["run_unit_test"] = bool(out.get("run_unit_test"))
    out["run_build_image"] = bool(out.get("run_build_image"))
    return out


def save_settings(patch: dict) -> dict:
    """保存流水线配置（部分字段更新）。"""
    cur = get_settings()
    allowed = ("edd_threshold", "edd_dataset_id", "run_unit_test",
               "run_build_image", "build_cmd", "canary_cmd", "full_cmd")
    for k in allowed:
        if k in patch and patch[k] is not None:
            cur[k] = patch[k]
    # 归一化
    try:
        cur["edd_threshold"] = max(0.0, min(1.0, float(cur["edd_threshold"])))
    except (TypeError, ValueError):
        cur["edd_threshold"] = 0.85
    cur["run_unit_test"] = 1 if cur.get("run_unit_test") else 0
    cur["run_build_image"] = 1 if cur.get("run_build_image") else 0
    for k in ("edd_dataset_id", "build_cmd", "canary_cmd", "full_cmd"):
        cur[k] = str(cur.get(k) or "").strip()

    exists = db.query_one("SELECT id FROM pipeline_settings WHERE id = 'default'")
    if exists:
        db.execute(
            "UPDATE pipeline_settings SET edd_threshold=?, edd_dataset_id=?, run_unit_test=?,"
            " run_build_image=?, build_cmd=?, canary_cmd=?, full_cmd=?, updated_at=?"
            " WHERE id='default'",
            (cur["edd_threshold"], cur["edd_dataset_id"], cur["run_unit_test"],
             cur["run_build_image"], cur["build_cmd"], cur["canary_cmd"],
             cur["full_cmd"], _now()),
        )
    else:
        db.execute(
            "INSERT INTO pipeline_settings (id, edd_threshold, edd_dataset_id, run_unit_test,"
            " run_build_image, build_cmd, canary_cmd, full_cmd, updated_at)"
            " VALUES ('default',?,?,?,?,?,?,?,?)",
            (cur["edd_threshold"], cur["edd_dataset_id"], cur["run_unit_test"],
             cur["run_build_image"], cur["build_cmd"], cur["canary_cmd"],
             cur["full_cmd"], _now()),
        )
    return get_settings()


# ---------------------------------------------------------------------------
# 阶段状态持久化
# ---------------------------------------------------------------------------

def _set_stage(run_id: str, name: str, status: str, log: str = "",
               detail: Optional[dict] = None, finish: bool = False) -> None:
    now = _now()
    if status == "running":
        db.execute(
            "UPDATE pipeline_stages SET status=?, started_at=?, log=?, detail=? WHERE run_id=? AND name=?",
            (status, now, log, json.dumps(detail or {}, ensure_ascii=False), run_id, name),
        )
    else:
        finished_at = now if finish else None
        db.execute(
            "UPDATE pipeline_stages SET status=?, log=?, detail=?, finished_at=COALESCE(?, finished_at)"
            " WHERE run_id=? AND name=?",
            (status, log, json.dumps(detail or {}, ensure_ascii=False), finished_at, run_id, name),
        )


def _append_stage_log(run_id: str, name: str, text: str) -> None:
    row = db.query_one(
        "SELECT log FROM pipeline_stages WHERE run_id=? AND name=?", (run_id, name)
    )
    old = (row or {}).get("log") or ""
    merged = (old + text)[-20000:]  # 限制日志体积
    db.execute(
        "UPDATE pipeline_stages SET log=? WHERE run_id=? AND name=?", (merged, run_id, name)
    )


def _set_run(run_id: str, **fields) -> None:
    if not fields:
        return
    keys = ", ".join(f"{k}=?" for k in fields)
    db.execute(f"UPDATE pipeline_runs SET {keys} WHERE id=?", (*fields.values(), run_id))


# ---------------------------------------------------------------------------
# 子进程执行
# ---------------------------------------------------------------------------

async def _run_cmd(cmd: str, cwd: Path, run_id: str, stage: str) -> tuple:
    """在子进程中执行 shell 命令，实时把输出追加到阶段日志。

    返回 (returncode, ok)。命令为空返回 (None, True)。
    """
    cmd = (cmd or "").strip()
    if not cmd:
        return None, True
    _append_stage_log(run_id, stage, f"$ {cmd}\n")
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as e:
        _append_stage_log(run_id, stage, f"启动命令失败：{e}\n")
        return -1, False

    try:
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=_CMD_TIMEOUT)
            if not line:
                break
            _append_stage_log(run_id, stage, line.decode("utf-8", "replace"))
        rc = await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        _append_stage_log(run_id, stage, f"\n[命令超时 { _CMD_TIMEOUT}s，已终止]\n")
        return -1, False
    ok = rc == 0
    _append_stage_log(run_id, stage, f"\n[退出码 {rc}]\n")
    return rc, ok


# ---------------------------------------------------------------------------
# 各阶段实现
# ---------------------------------------------------------------------------

async def _stage_commit(run_id: str, git_ref: str) -> tuple:
    """代码提交阶段（只读）：展示最近一次提交信息，非 git 仓库时降级通过。"""
    _set_stage(run_id, "commit", "running")
    try:
        proc = await asyncio.create_subprocess_shell(
            "git log -1 --pretty=format:'%h | %an | %ad | %s' --date=iso",
            cwd=str(PROJECT_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        text = out.decode("utf-8", "replace").strip()
        rc = proc.returncode
    except Exception as e:
        text, rc = f"读取 git 提交信息失败：{e}", -1

    if rc == 0 and text:
        _append_stage_log(run_id, "commit", f"最近提交：{text}\n")
        detail = {"git_ref": git_ref or text.split("|")[0].strip(), "head": text}
        _set_stage(run_id, "commit", "passed", detail=detail, finish=True)
        return "passed", detail
    # 非 git 仓库或读取失败：不阻断流水线，标记通过并注明
    _append_stage_log(run_id, "commit", "（当前目录非 git 仓库或无提交记录，跳过提交校验）\n")
    _set_stage(run_id, "commit", "passed", detail={"note": "非 git 仓库，跳过"}, finish=True)
    return "passed", {"note": "非 git 仓库，跳过"}


async def _stage_unit_test(run_id: str, settings: dict) -> tuple:
    """单元测试阶段：执行 pytest；未开启时 skipped。"""
    if not settings.get("run_unit_test"):
        _set_stage(run_id, "unit_test", "skipped", log="（配置未开启单元测试，跳过）\n", finish=True)
        return "skipped", {}
    _set_stage(run_id, "unit_test", "running")
    rc, ok = await _run_cmd("python -m pytest -q", BACKEND_DIR, run_id, "unit_test")
    status = "passed" if ok else "failed"
    _set_stage(run_id, "unit_test", status, detail={"returncode": rc}, finish=True)
    return status, {"returncode": rc}


async def _stage_eval_regression(run_id: str, settings: dict, user: dict) -> tuple:
    """评估回归阶段（EDD 核心）：跑评测集，取 Ragas 四指标均值对阈值门控。

    未配置评测集时 skipped（不阻断）；配置了则分数 < 阈值判 failed 阻断发布。
    """
    from app.services import evaluation

    dataset_id = (settings.get("edd_dataset_id") or "").strip()
    threshold = float(settings.get("edd_threshold") or 0.85)
    if not dataset_id:
        _set_stage(run_id, "eval_regression", "skipped",
                   log="（未配置 EDD 评测集，跳过评估回归；建议配置以启用发布门控）\n", finish=True)
        return "skipped", {"threshold": threshold}

    _set_stage(run_id, "eval_regression", "running",
               detail={"dataset_id": dataset_id, "threshold": threshold})
    _append_stage_log(run_id, "eval_regression",
                      f"发起评估回归：dataset={dataset_id}，EDD 阈值={threshold}\n")
    try:
        eval_run_id = evaluation.start_run(dataset_id, user)
    except evaluation.ConcurrentRunError as e:
        _append_stage_log(run_id, "eval_regression", f"评测集正在运行，无法发起：{e}\n")
        _set_stage(run_id, "eval_regression", "failed", detail={"error": str(e)}, finish=True)
        return "failed", {"error": str(e), "threshold": threshold}
    except Exception as e:
        _append_stage_log(run_id, "eval_regression", f"发起评测失败：{e}\n")
        _set_stage(run_id, "eval_regression", "failed", detail={"error": str(e)}, finish=True)
        return "failed", {"error": str(e), "threshold": threshold}

    # 轮询评测运行直到结束
    deadline = time.monotonic() + _EVAL_TIMEOUT
    summary = {}
    status = "failed"
    while time.monotonic() < deadline:
        await asyncio.sleep(_EVAL_POLL_INTERVAL)
        run = db.query_one("SELECT status, summary FROM eval_runs WHERE id=?", (eval_run_id,))
        if not run:
            break
        if run["status"] in ("finished", "failed", "interrupted"):
            try:
                summary = json.loads(run.get("summary") or "{}")
            except (json.JSONDecodeError, TypeError):
                summary = {}
            status = run["status"]
            break
        _append_stage_log(run_id, "eval_regression", ".")
    else:
        _append_stage_log(run_id, "eval_regression", f"\n[评测等待超时 {_EVAL_TIMEOUT}s]\n")

    if status != "finished":
        _append_stage_log(run_id, "eval_regression", f"\n评测未成功完成（status={status}）\n")
        _set_stage(run_id, "eval_regression", "failed",
                   detail={"eval_run_id": eval_run_id, "status": status}, finish=True)
        return "failed", {"eval_run_id": eval_run_id, "status": status, "threshold": threshold}

    # EDD 质量分 = Ragas 四指标非空均值
    metric_keys = ("avg_faithfulness", "avg_answer_relevance",
                   "avg_context_precision", "avg_context_recall")
    vals = [summary[k] for k in metric_keys if summary.get(k) is not None]
    score = round(sum(vals) / len(vals), 4) if vals else None
    passed = score is not None and score >= threshold

    _append_stage_log(
        run_id, "eval_regression",
        f"\n评估完成：EDD 质量分={score if score is not None else '—'}，阈值={threshold} → "
        f"{'通过' if passed else '未达标，阻断发布'}\n"
        f"（忠实度={summary.get('avg_faithfulness')} 相关性={summary.get('avg_answer_relevance')} "
        f"精确率={summary.get('avg_context_precision')} 召回率={summary.get('avg_context_recall')}）\n",
    )
    detail = {"eval_run_id": eval_run_id, "score": score, "threshold": threshold,
              "passed": passed, "summary": summary}
    _set_stage(run_id, "eval_regression", "passed" if passed else "failed",
               detail=detail, finish=True)
    # 回写 run 级 EDD 分数
    _set_run(run_id, edd_score=score)
    return ("passed" if passed else "failed"), detail


async def _stage_build_image(run_id: str, settings: dict) -> tuple:
    """构建镜像阶段：未开启或未配置命令时 skipped。"""
    if not settings.get("run_build_image"):
        _set_stage(run_id, "build_image", "skipped", log="（配置未开启镜像构建，跳过）\n", finish=True)
        return "skipped", {}
    build_cmd = (settings.get("build_cmd") or "").strip() or \
        "docker build -f Dockerfile.backend -t jianzirumian-backend:ci ."
    _set_stage(run_id, "build_image", "running")
    rc, ok = await _run_cmd(build_cmd, PROJECT_ROOT, run_id, "build_image")
    status = "passed" if ok else "failed"
    _set_stage(run_id, "build_image", status, detail={"returncode": rc, "cmd": build_cmd}, finish=True)
    return status, {"returncode": rc}


async def _stage_deploy(run_id: str, stage: str, cmd: str) -> tuple:
    """灰度 / 全量发布阶段：配置了命令则执行，否则 skipped。"""
    cmd = (cmd or "").strip()
    if not cmd:
        _set_stage(run_id, stage, "skipped",
                   log=f"（未配置{STAGE_LABELS.get(stage, stage)}命令，跳过）\n", finish=True)
        return "skipped", {}
    _set_stage(run_id, stage, "running")
    rc, ok = await _run_cmd(cmd, PROJECT_ROOT, run_id, stage)
    status = "passed" if ok else "failed"
    _set_stage(run_id, stage, status, detail={"returncode": rc, "cmd": cmd}, finish=True)
    return status, {"returncode": rc}


# ---------------------------------------------------------------------------
# 流水线执行主循环
# ---------------------------------------------------------------------------

async def _run_pipeline(run_id: str, user: dict, git_ref: str, trigger: str) -> None:
    """后台串行执行六个阶段；任一阶段 failed 立即阻断后续（标记 skipped）。"""
    settings = get_settings()
    threshold = float(settings.get("edd_threshold") or 0.85)
    _abort_flags[run_id] = False
    blocked = False
    final_status = "finished"

    try:
        for name, _label in STAGES:
            # 人工中止检测（阶段边界）
            if _abort_flags.get(run_id):
                _set_stage(run_id, name, "aborted", log="（流水线已被人工中止）\n", finish=True)
                final_status = "aborted"
                blocked = True
                break
            # 前序阶段失败：本阶段及之后全部 skipped
            if blocked:
                _set_stage(run_id, name, "skipped", log="（前序阶段失败，已阻断）\n", finish=True)
                continue

            _set_run(run_id, current_stage=name)
            if name == "commit":
                status, detail = await _stage_commit(run_id, git_ref)
            elif name == "unit_test":
                status, detail = await _stage_unit_test(run_id, settings)
            elif name == "eval_regression":
                status, detail = await _stage_eval_regression(run_id, settings, user)
            elif name == "build_image":
                status, detail = await _stage_build_image(run_id, settings)
            elif name == "canary":
                status, detail = await _stage_deploy(run_id, "canary", settings.get("canary_cmd"))
            else:  # full
                status, detail = await _stage_deploy(run_id, "full", settings.get("full_cmd"))

            if status == "failed":
                blocked = True
                final_status = "failed"
    except Exception as e:
        logger.error("流水线执行异常: %s | %s", run_id, e)
        final_status = "failed"
        _set_run(run_id, summary=json.dumps({"error": str(e)[:500]}, ensure_ascii=False))
    finally:
        _abort_flags.pop(run_id, None)
        # 阻断后把尚未处理的阶段统一标 skipped（针对 abort 提前 break 的情况）
        if final_status == "aborted":
            for name, _ in STAGES:
                row = db.query_one(
                    "SELECT status FROM pipeline_stages WHERE run_id=? AND name=?", (run_id, name)
                )
                if row and row["status"] in ("pending", "running"):
                    _set_stage(run_id, name, "aborted", finish=True)
        summary_row = db.query_one("SELECT edd_score FROM pipeline_runs WHERE id=?", (run_id,))
        edd_score = (summary_row or {}).get("edd_score")
        _set_run(
            run_id,
            status=final_status,
            current_stage="",
            finished_at=_now(),
            summary=json.dumps({
                "final_status": final_status,
                "edd_score": edd_score,
                "edd_threshold": threshold,
                "passed": final_status == "finished",
            }, ensure_ascii=False),
        )
        logger.info("流水线完成: %s | status=%s", run_id, final_status)


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def start_pipeline(user: dict, git_ref: str = "", trigger: str = "manual") -> str:
    """发起一次流水线运行（后台异步执行），立即返回 run_id。

    并发保护：已有 running 的运行时抛 ConcurrentRunError。
    """
    running = db.query_one("SELECT id FROM pipeline_runs WHERE status = 'running'")
    if running:
        raise ConcurrentRunError("已有正在执行的流水线，请等待其完成或中止后再发起")

    settings = get_settings()
    run_id = _new_id()
    user_id = user.get("id") or user.get("username") or ""
    db.execute(
        "INSERT INTO pipeline_runs (id, trigger, git_ref, status, current_stage, edd_score,"
        " edd_threshold, summary, created_by, started_at, finished_at)"
        " VALUES (?,?,?,'running','',NULL,?, '{}', ?, ?, NULL)",
        (run_id, trigger, git_ref or "", float(settings.get("edd_threshold") or 0.85),
         user_id, _now()),
    )
    # 初始化六条阶段记录
    for seq, (name, label) in enumerate(STAGES):
        db.execute(
            "INSERT INTO pipeline_stages (id, run_id, seq, name, status, log, detail, started_at, finished_at)"
            " VALUES (?,?,?,?,'pending','','{}',NULL,NULL)",
            (_new_id(), run_id, seq, name),
        )

    asyncio.create_task(_run_pipeline(run_id, user, git_ref, trigger))
    logger.info("发起流水线运行: %s | by=%s | trigger=%s", run_id, user_id, trigger)
    return run_id


def abort_pipeline(run_id: str) -> bool:
    """请求中止某次运行：置 abort 标志，执行循环在阶段边界停止。"""
    run = db.query_one("SELECT status FROM pipeline_runs WHERE id=?", (run_id,))
    if not run or run["status"] != "running":
        return False
    _abort_flags[run_id] = True
    return True


class ConcurrentRunError(Exception):
    """已有正在执行的流水线运行。"""


def _row_to_stage(row: dict) -> dict:
    try:
        detail = json.loads(row.get("detail") or "{}")
    except (json.JSONDecodeError, TypeError):
        detail = {}
    return {
        "name": row["name"],
        "label": STAGE_LABELS.get(row["name"], row["name"]),
        "seq": row["seq"],
        "status": row["status"],
        "log": row.get("log") or "",
        "detail": detail,
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
    }


def get_run(run_id: str) -> Optional[dict]:
    """运行详情：run + 六阶段（含日志与明细）。"""
    run = db.query_one("SELECT * FROM pipeline_runs WHERE id=?", (run_id,))
    if not run:
        return None
    try:
        summary = json.loads(run.get("summary") or "{}")
    except (json.JSONDecodeError, TypeError):
        summary = {}
    stage_rows = db.query(
        "SELECT * FROM pipeline_stages WHERE run_id=? ORDER BY seq", (run_id,)
    )
    return {
        "id": run["id"],
        "trigger": run["trigger"],
        "git_ref": run["git_ref"],
        "status": run["status"],
        "current_stage": run["current_stage"],
        "edd_score": run["edd_score"],
        "edd_threshold": run["edd_threshold"],
        "summary": summary,
        "created_by": run["created_by"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "stages": [_row_to_stage(r) for r in stage_rows],
    }


def list_runs(limit: int = 20) -> List[dict]:
    """运行历史（不含阶段日志，避免响应过大）。"""
    rows = db.query(
        "SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT ?", (limit,)
    )
    out = []
    for r in rows:
        try:
            summary = json.loads(r.get("summary") or "{}")
        except (json.JSONDecodeError, TypeError):
            summary = {}
        stage_rows = db.query(
            "SELECT name, status FROM pipeline_stages WHERE run_id=? ORDER BY seq", (r["id"],)
        )
        out.append({
            "id": r["id"],
            "trigger": r["trigger"],
            "git_ref": r["git_ref"],
            "status": r["status"],
            "current_stage": r["current_stage"],
            "edd_score": r["edd_score"],
            "edd_threshold": r["edd_threshold"],
            "summary": summary,
            "created_by": r["created_by"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "stages": [{"name": s["name"], "label": STAGE_LABELS.get(s["name"], s["name"]),
                        "status": s["status"]} for s in stage_rows],
        })
    return out
