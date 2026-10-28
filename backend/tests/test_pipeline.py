"""CI/CD 流水线服务单元测试：阶段日志保留与发起人名称解析（内存 DB，不触发真实运行）"""
import json
import sqlite3
import time

import pytest

from app.core import db
from app.services import pipeline


@pytest.fixture()
def mem_db(monkeypatch):
    """独立内存数据库：避免污染本地开发库"""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    yield conn
    conn.close()


def _insert_run(run_id, created_by="", created_by_name=""):
    db.execute(
        "INSERT INTO pipeline_runs (id, trigger, git_ref, status, current_stage,"
        " edd_threshold, summary, created_by, created_by_name, started_at)"
        " VALUES (?,'manual','','running','',0.85,'{}',?,?,?)",
        (run_id, created_by, created_by_name, pipeline._now()),
    )
    for seq, (name, _label) in enumerate(pipeline.STAGES):
        db.execute(
            "INSERT INTO pipeline_stages (id, run_id, seq, name, status, log, detail)"
            " VALUES (?,?,?,?,'pending','','{}')",
            (f"{run_id}-{name}", run_id, seq, name),
        )


def test_set_stage_keeps_appended_log(mem_db):
    """实时追加的日志不得被阶段结束时的状态更新清空（回归：日志被抹空 bug）"""
    _insert_run("r1")
    pipeline._append_stage_log("r1", "unit_test", "$ python -m pytest -q\n")
    pipeline._append_stage_log("r1", "unit_test", "test_auth.py ....\n")
    pipeline._set_stage("r1", "unit_test", "failed", detail={"returncode": 1}, finish=True)
    row = db.query_one(
        "SELECT log, detail, finished_at FROM pipeline_stages WHERE run_id='r1' AND name='unit_test'"
    )
    assert "$ python -m pytest -q" in row["log"]
    assert "test_auth.py" in row["log"]
    assert json.loads(row["detail"])["returncode"] == 1
    assert row["finished_at"]


def test_set_stage_explicit_log_still_writes(mem_db):
    """显式传入 log（如跳过说明）时仍正常写入"""
    _insert_run("r1")
    pipeline._set_stage("r1", "unit_test", "skipped", log="（单元测试跳过）\n", finish=True)
    row = db.query_one("SELECT log FROM pipeline_stages WHERE run_id='r1' AND name='unit_test'")
    assert row["log"] == "（单元测试跳过）\n"


def test_creator_name_prefers_stored_name(mem_db):
    """发起时落库的用户名优先展示"""
    _insert_run("r1", created_by="u1", created_by_name="opsuser")
    assert pipeline.get_run("r1")["created_by_name"] == "opsuser"
    assert pipeline.list_runs()[0]["created_by_name"] == "opsuser"


def test_creator_name_fallback_to_users_table(mem_db):
    """存量数据 created_by_name 为空：按 created_by（用户 id）回查 users 表用户名"""
    db.execute(
        "INSERT INTO users (id, username, password_hash, salt, created_at, updated_at)"
        " VALUES ('u1','test','x','x',?,?)",
        (time.time(), time.time()),
    )
    _insert_run("r1", created_by="u1", created_by_name="")
    assert pipeline.get_run("r1")["created_by_name"] == "test"
    assert pipeline.list_runs()[0]["created_by_name"] == "test"


def test_creator_name_fallback_raw_value(mem_db):
    """用户已删除且无存量用户名时：回退展示 created_by 原值"""
    _insert_run("r1", created_by="ghost-id", created_by_name="")
    assert pipeline.get_run("r1")["created_by_name"] == "ghost-id"
