"""可观测性 PII 脱敏回归测试（内存 DB / 临时日志目录，不出网）。

覆盖需求3（对应 docs/记忆模块五层架构设计.md 第七节）：
  R1  四类 PII 脱敏：手机号/身份证(含末位X)/银行卡/邮箱；
  R2  顺序敏感：身份证(18位)先于银行卡(16-19位)/手机号(11位)，长证件号不被短模式截断；
  R3  数字边界守卫：trace id(uuid)/token 计数/时间戳/超长数字串不误伤；
  R4  凭据规则保留：Bearer/sk-/api_key 等仍脱敏；
  R5  _redact_obj 递归脱敏（str/dict/list/非字符串）且不改写原始对象；
  R6  read_day_logs 返回副本脱敏（items + entries），原始日志文件不改写；
  R7  get_trace 返回副本脱敏（span input/output），原始落库不改写；
  R8  网关日志明细 get_log 返回副本脱敏（request_body/response_preview/error）。
"""
import asyncio
import sqlite3
import subprocess
import sys

import pytest

from app.core import db
from app.core import log_setup
from app.core import observability as obs
from app.core.log_setup import redact_log_text


@pytest.fixture()
def mem_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    yield conn
    conn.close()


# ============================================
# R1 四类 PII 脱敏
# ============================================

def test_phone_redacted():
    out = redact_log_text("用户手机号13812345678已登记")
    assert "[手机号已脱敏]" in out
    assert "13812345678" not in out


def test_id_card_masked_front3_back4():
    out = redact_log_text("身份证110101199003078515核对")
    assert "110***********8515" in out
    assert "110101199003078515" not in out


def test_id_card_with_x_masked():
    out = redact_log_text("证件11010119900307851X结尾")
    assert "110***********851X" in out
    assert "11010119900307851X" not in out


def test_bank_card_redacted():
    for card in ("6222021234567890", "6222021234567890123"):  # 16 位 / 19 位
        out = redact_log_text(f"银行卡{card}绑定")
        assert "[银行卡已脱敏]" in out
        assert card not in out


def test_email_masked():
    out = redact_log_text("联系邮箱zhangsan@example.com谢谢")
    assert "z***@example.com" in out
    assert "zhangsan@example.com" not in out


def test_long_non_email_does_not_quadratically_backtrack():
    code = (
        "from app.core.log_setup import redact_log_text; "
        "raw='x'*220000; assert redact_log_text(raw)==raw; "
        "assert redact_log_text('hello+test@example.com')=='h***@example.com'"
    )
    subprocess.run([sys.executable, "-c", code], check=True, timeout=5,
                   cwd=str(__import__('pathlib').Path(__file__).resolve().parents[1]))


# ============================================
# R2 顺序敏感（身份证先于银行卡/手机号）
# ============================================

def test_id_card_not_eaten_by_shorter_patterns():
    # 18 位身份证必须整体掩码，不能被银行卡/手机号模式先吃掉部分数字
    out = redact_log_text("110101199003078515")
    assert out == "110***********8515"
    assert "[银行卡已脱敏]" not in out and "[手机号已脱敏]" not in out


# ============================================
# R3 数字边界守卫（不误伤 trace id / token / 时间戳 / 超长数字）
# ============================================

def test_trace_id_uuid_not_redacted():
    tid = "550e8400-e29b-41d4-a716-446655440000"
    assert redact_log_text(f"trace={tid} 完成") == f"trace={tid} 完成"


def test_token_counts_and_timestamp_not_redacted():
    text = "tokens=1420+480 cached=64 latency=210ms ts=2026-01-01 10:00:00"
    assert redact_log_text(text) == text


def test_overlong_digit_run_not_redacted():
    # 20 位连续数字超出银行卡 16-19 位范围，边界守卫保证不误伤
    long_digits = "12345678901234567890"
    assert redact_log_text(long_digits) == long_digits
    # 手机号嵌入更长数字串（前有其他数字）不匹配
    embedded = "9138123456789"
    assert redact_log_text(embedded) == embedded


# ============================================
# R4 凭据规则保留
# ============================================

def test_credentials_still_redacted():
    assert "[REDACTED]" in redact_log_text("Authorization: Bearer abcdef123456")
    assert "[REDACTED]" in redact_log_text("key=sk-abcdef123456")
    assert "[REDACTED]" in redact_log_text('{"api_key": "secret-value"}')
    assert "secret-value" not in redact_log_text('{"api_key": "secret-value"}')


def test_mixed_pii_and_credentials():
    text = "用户13812345678邮箱a@b.com的token=abc123请求"
    out = redact_log_text(text)
    assert "[手机号已脱敏]" in out and "a***@b.com" in out and "[REDACTED]" in out
    assert "13812345678" not in out and "abc123" not in out


# ============================================
# R5 _redact_obj 递归 + 不改写原始对象
# ============================================

def test_redact_obj_recursive_and_non_mutating():
    from app.api.observability import _redact_obj

    original = {
        "input": "手机号13812345678",
        "nested": {"email": "zhang@example.com", "count": 42},
        "list": ["身份证110101199003078515", 100, None],
    }
    snapshot = repr(original)
    out = _redact_obj(original)
    assert "[手机号已脱敏]" in out["input"]
    assert out["nested"]["email"] == "z***@example.com"
    assert out["nested"]["count"] == 42  # 非字符串原样
    assert "110***********8515" in out["list"][0]
    assert out["list"][1] == 100 and out["list"][2] is None
    # 原始对象不被改写
    assert repr(original) == snapshot
    assert _redact_obj(123) == 123 and _redact_obj(None) is None


# ============================================
# R6 read_day_logs 返回副本脱敏 + 原文件不变
# ============================================

def test_read_day_logs_redacts_copy_not_file(mem_db, monkeypatch, tmp_path):
    monkeypatch.setattr(log_setup, "LOG_DIR", tmp_path)
    day = "2026-01-01"
    tid = "550e8400-e29b-41d4-a716-446655440000"
    raw_msg = "用户手机号13812345678邮箱zhang@example.com身份证110101199003078515"
    line = f"[u1] {day} 10:00:00 [INFO] [trace={tid}] jianziruyang.test: {raw_msg}\n"
    log_file = tmp_path / f"{day}.log"
    log_file.write_text(line, encoding="utf-8")

    result = log_setup.read_day_logs(day)
    assert result["total"] == 1
    item = result["items"][0]
    assert "[手机号已脱敏]" in item["message"]
    assert "z***@example.com" in item["message"]
    assert "110***********8515" in item["message"]
    assert "13812345678" not in item["message"]
    # entries 逐条脱敏
    for e in item.get("entries") or []:
        assert "13812345678" not in e["message"]
    # trace id 不被误伤
    assert item["trace"] == tid
    # 原始文件不改写
    assert log_file.read_text(encoding="utf-8") == line
    assert "13812345678" in log_file.read_text(encoding="utf-8")


# ============================================
# R7 get_trace 返回副本脱敏 + 原始落库不变
# ============================================

def test_get_trace_redacts_copy_not_db(mem_db):
    from app.api.observability import get_trace

    tid = obs.start_trace("test-agent", user_id="u1", input_text="问：我的手机号13812345678")
    try:
        obs.record_llm(
            "chat_answer",
            "系统提示+用户手机13812345678邮箱zhang@example.com",
            "回答涉及身份证110101199003078515",
        )
    finally:
        obs.end_trace("ok")

    res = asyncio.run(get_trace(tid, {"id": "admin"}))
    assert res["success"]
    trace = res["trace"]
    span = trace["spans"][0]
    assert "[手机号已脱敏]" in span["input"] and "z***@example.com" in span["input"]
    assert "110***********8515" in span["output"]
    assert "13812345678" not in span["input"]
    # trace 级 input 同样脱敏
    assert "13812345678" not in (trace.get("input") or "")
    # 原始落库不改写：agent_spans 仍存原文
    row = db.query_one("SELECT input, output FROM agent_spans WHERE trace_id = ?", (tid,))
    assert "13812345678" in row["input"]
    assert "110101199003078515" in row["output"]


# ============================================
# R8 网关日志明细 get_log 返回副本脱敏
# ============================================

def test_gateway_get_log_redacts_copy(mem_db, monkeypatch):
    from app.api import gateway as gateway_api

    fake_entry = {
        "id": "log1",
        "request_body": "用户手机13812345678的请求体",
        "response_preview": "回复邮箱zhang@example.com",
        "error": "上游回显身份证110101199003078515",
    }
    monkeypatch.setattr(gateway_api.log_svc, "get_log", lambda log_id: dict(fake_entry))
    res = asyncio.run(gateway_api.get_log("log1", {"id": "ops"}))
    assert res["success"]
    log = res["log"]
    assert "[手机号已脱敏]" in log["request_body"]
    assert "z***@example.com" in log["response_preview"]
    assert "110***********8515" in log["error"]
    assert "13812345678" not in log["request_body"]