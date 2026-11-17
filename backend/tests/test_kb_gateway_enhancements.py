"""知识库与网关八项增强的回归测试（内存 DB / mock LLM，不出网）：

  D4 用户密钥自愈：绑定密钥明文缺失→重签自愈且强制 user 类型；存量迁移回填；
     禁用绑定密钥不强制；配额用尽不降级（见 test_gateway_keys_routing_security）。
  D8 上游熔断：连续 5 次失败→circuit_open；冷却 60s 半开探测；成功恢复/失败重开；
     排序置后 + proxy 候选过滤 fail-fast 消息。
  D3 KB 评测用例组：组 CRUD（删组连带用例）/generate 建组/start_run 按组/存量归默认组。
  D2 知识总结：技能关闭→waiting_skill 不摘要；开启→resume 后台执行；结构化 JSON 校验；
     解析失败重试后 error；手动 generate 未开启返回 skill_disabled。
  D7 期望来源接地：百科/文档标题命中；无命中占位文案；内置集 seed 后非空。
"""
import asyncio
import json
import sqlite3
import uuid
from datetime import datetime, timedelta

import pytest

from app.core import db


@pytest.fixture()
def mem_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    yield conn
    conn.close()


def _now():
    return datetime.now().isoformat()


def _record(rec_id="rec-1", title="糖尿病饮食指南", text="血糖控制与膳食纤维。" * 20, owner="o1"):
    db.execute(
        "INSERT INTO upload_records (id, filename, title, image_path, ocr_text, owner_id,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (rec_id, f"{title}.pdf", title, f"/tmp/{rec_id}.pdf", text, owner, _now(), _now()))
    return rec_id


# ============================================
# D4 用户密钥自愈重签 + 强制优先
# ============================================

def test_resolve_self_heals_missing_plaintext(mem_db):
    """绑定用户密钥明文丢失 → 重签自愈，仍返回 user 类型（绝不降级公共）。"""
    from app.services.gateway import keys as key_svc
    from app.services.gateway import settings as settings_svc

    key_svc.create_key("公共池", key_type="public")
    uk = key_svc.create_key("专属", key_type="user", bound_users=["u1"])
    # 模拟存量故障：明文未登记在 outbound_keys
    settings_svc.set_outbound_key(uk["id"], "")
    assert not settings_svc.get_outbound_keys().get(uk["id"])

    plain, ktype = key_svc.resolve_outbound_key("u1")
    assert ktype == "user"
    assert plain
    # 自愈后明文已登记，且可通过校验（prefix/hash 已同步更新）
    assert settings_svc.get_outbound_keys()[uk["id"]] == plain
    row = key_svc.authenticate(plain)
    assert row["id"] == uk["id"]
    assert row["key_prefix"] == plain[:14]


def test_reissue_invalidates_old_plaintext(mem_db):
    """重签后旧明文失效（系统签发密钥，旧值不可找回）。"""
    from app.services.gateway import keys as key_svc

    uk = key_svc.create_key("专属", key_type="user", bound_users=["u1"])
    old = uk["key"]
    new = key_svc.reissue_plaintext(uk["id"])
    assert new != old
    with pytest.raises(key_svc.KeyRejected):
        key_svc.authenticate(old)
    assert key_svc.authenticate(new)["id"] == uk["id"]


def test_disabled_bound_key_not_forced(mem_db):
    """管理员主动停用的绑定密钥不强制携带 → 按现有优先级走公共。"""
    from app.services.gateway import keys as key_svc

    pub = key_svc.create_key("公共池", key_type="public")
    uk = key_svc.create_key("专属", key_type="user", bound_users=["u1"])
    key_svc.update_key(uk["id"], {"enabled": False})
    plain, ktype = key_svc.resolve_outbound_key("u1")
    assert ktype == "public"
    assert plain == pub["key"]


def test_outbound_heal_migration_backfills(mem_db):
    """启动自愈迁移：enabled 密钥明文缺失者批量重签登记。"""
    from app.services.gateway import keys as key_svc
    from app.services.gateway import settings as settings_svc

    uk = key_svc.create_key("专属", key_type="user", bound_users=["u1"])
    settings_svc.set_outbound_key(uk["id"], "")
    # 回退迁移版本标记，重跑 init_db 触发存量自愈
    db.execute("DELETE FROM schema_migrations WHERE version='gateway_outbound_heal_v1'")
    db.init_db(db._conn)
    assert settings_svc.get_outbound_keys().get(uk["id"])


# ============================================
# D8 上游熔断（阈值 5 + 冷却半开）
# ============================================

def _upstream(name="up-a"):
    from app.services.gateway import upstreams as up_svc
    return up_svc.create_upstream({"name": name, "tier": "free"})


def test_five_failures_open_circuit(mem_db):
    from app.services.gateway import upstreams as up_svc

    up = _upstream()
    for _ in range(4):
        up_svc.mark_failure(up["id"], "boom")
    assert up_svc.get_upstream(up["id"])["health"] == "degraded"
    up_svc.mark_failure(up["id"], "boom")
    row = up_svc.get_upstream(up["id"])
    assert row["health"] == "circuit_open"
    assert row["fail_count"] == 5
    assert row["circuit_opened_at"]


def test_success_recovers_from_circuit(mem_db):
    from app.services.gateway import upstreams as up_svc

    up = _upstream()
    for _ in range(5):
        up_svc.mark_failure(up["id"])
    up_svc.mark_success(up["id"], latency_ms=120)
    row = up_svc.get_upstream(up["id"])
    assert row["health"] == "healthy"
    assert row["fail_count"] == 0
    assert row["circuit_opened_at"] == ""


def test_probe_requires_cooldown(mem_db):
    from app.services.gateway import upstreams as up_svc

    up = _upstream()
    for _ in range(5):
        up_svc.mark_failure(up["id"])
    info = up_svc.get_upstream(up["id"])
    assert up_svc.circuit_probe_allowed(info) is False  # 冷却期内禁止锤击
    # 冷却期（60s）已过 → 允许一次半开探测
    past = (datetime.now() - timedelta(seconds=61)).isoformat()
    db.execute("UPDATE gateway_upstreams SET circuit_opened_at=? WHERE id=?", (past, up["id"]))
    assert up_svc.circuit_probe_allowed(up_svc.get_upstream(up["id"])) is True


def test_half_open_failure_reopens_with_reset_cooldown(mem_db):
    from app.services.gateway import upstreams as up_svc

    up = _upstream()
    for _ in range(5):
        up_svc.mark_failure(up["id"])
    past = (datetime.now() - timedelta(seconds=61)).isoformat()
    db.execute("UPDATE gateway_upstreams SET circuit_opened_at=? WHERE id=?", (past, up["id"]))
    up_svc.mark_failure(up["id"], "probe failed")  # 半开探测再失败
    row = up_svc.get_upstream(up["id"])
    assert row["health"] == "circuit_open"
    assert row["circuit_opened_at"] > past  # 冷却重置
    assert up_svc.circuit_probe_allowed(row) is False


def test_rules_order_circuit_open_last(mem_db):
    from app.services.gateway import rules as rule_svc

    rule = {"primary_upstream": "a", "failover_threshold": 5}
    rows = [{"id": i, "name": i, "fail_count": f, "health": h, "weight": 100}
            for i, f, h in (("a", 5, "circuit_open"), ("b", 0, "healthy"))]
    ordered = rule_svc._order_by_strategy(rows, "primary_backup", rule)
    assert ordered[0]["id"] == "b"
    assert ordered[-1]["id"] == "a"


def test_proxy_filter_fail_fast_message(mem_db):
    from app.services.gateway import proxy as proxy_svc
    from app.services.gateway import upstreams as up_svc

    up = _upstream()
    for _ in range(5):
        up_svc.mark_failure(up["id"])
    ctx = proxy_svc.Ctx()
    ctx.settings = {}
    ctx.candidates = [up_svc.get_upstream(up["id"])]
    allowed, opened = proxy_svc._filter_circuit(ctx)
    assert allowed == []
    assert len(opened) == 1
    msg = proxy_svc._circuit_message(opened)
    assert "熔断" in msg and "5" in msg and "60" in msg


# ============================================
# D3 KB 评测用例组
# ============================================

def test_group_crud_and_cascade_delete(mem_db):
    from app.services import kb_eval

    g = kb_eval.create_group("kb1", "核心问答", "备注说明", "u1")
    assert kb_eval.list_groups("kb1")[0]["case_count"] == 0
    kb_eval.add_case("kb1", "问题1", "", [], "u1", group_id=g["id"])
    assert kb_eval.list_groups("kb1")[0]["case_count"] == 1

    renamed = kb_eval.update_group("kb1", g["id"], name="新名", note="新备注")
    assert renamed["name"] == "新名" and renamed["note"] == "新备注"
    assert len(kb_eval.list_cases("kb1", g["id"])) == 1

    assert kb_eval.delete_group("kb1", g["id"]) is True
    assert kb_eval.list_cases("kb1", g["id"]) == []  # 连带用例删除
    with pytest.raises(ValueError):
        kb_eval.create_group("kb1", "  ")


def test_generate_cases_creates_named_group(mem_db, monkeypatch):
    from app.services import kb_eval

    monkeypatch.setattr(kb_eval, "build_kb_context", lambda kb_id, budget: "知识库内容")

    async def _fake_llm(messages):
        return json.dumps({"cases": [
            {"question": "失眠吃什么？", "reference_answer": "小米粥",
             "expected_keywords": ["小米粥"], "expected_source": "失眠食疗"},
        ]}, ensure_ascii=False)
    monkeypatch.setattr(kb_eval.wiki, "_llm_complete", _fake_llm)

    result = asyncio.run(kb_eval.generate_cases(
        "kb1", "养生库", "u1", count=5, group_name="我的组", group_note="备注"))
    assert result["group"]["name"] == "我的组"
    assert result["group"]["note"] == "备注"
    case = result["cases"][0]
    assert case["group_id"] == result["group"]["id"]
    assert case["expected_source"] == "失眠食疗"


def test_start_run_selects_group(mem_db, monkeypatch):
    from app.services import kb_eval

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(kb_eval, "_run_all", _noop)

    g1 = kb_eval.create_group("kb1", "组一", "", "u1")
    g2 = kb_eval.create_group("kb1", "组二", "", "u1")
    kb_eval.add_case("kb1", "问题1", "", [], "u1", group_id=g1["id"])
    kb_eval.add_case("kb1", "问题2", "", [], "u1", group_id=g2["id"])

    with pytest.raises(ValueError):
        kb_eval.start_run("kb1", "u1", group_id="not-exist")

    async def _start():
        return kb_eval.start_run("kb1", "u1", group_id=g1["id"])
    run_id = asyncio.run(_start())
    run = db.query_one("SELECT * FROM kb_eval_runs WHERE id=?", (run_id,))
    assert run["group_id"] == g1["id"]
    assert run["group_name"] == "组一"
    assert run["status"] == "running"


def test_legacy_cases_backfill_default_group(mem_db):
    """存量用例（group_id=''）迁移归入自动创建的「默认用例组」。"""
    from app.services import kb_eval

    db.execute(
        "INSERT INTO kb_eval_cases (id, kb_id, question, reference_answer, expected_keywords,"
        " created_by, created_at, group_id) VALUES (?,?,?,?,?,?,?, '')",
        ("legacy-1", "kb-old", "存量问题", "", "[]", "u1", _now()))
    db.execute("DELETE FROM schema_migrations WHERE version='kb_eval_groups_v1'")
    db.init_db(db._conn)
    groups = kb_eval.list_groups("kb-old")
    assert len(groups) == 1
    assert groups[0]["name"] == "默认用例组"
    assert groups[0]["case_count"] == 1
    assert kb_eval.list_cases("kb-old", groups[0]["id"])[0]["id"] == "legacy-1"


# ============================================
# D2 知识总结（内置技能 + 状态机）
# ============================================

class _FakeLLM:
    def __init__(self, chunks):
        self._chunks = chunks

    async def chat(self, messages, stream=True):
        for c in self._chunks:
            yield c


_VALID_STRUCTURE = json.dumps({
    "overview": "核心概览", "chapters": [{"title": "第一章", "summary": "摘要", "key_points": ["要点"]}],
    "glossary": [{"term": "血糖", "definition": "血液中的葡萄糖"}],
    "patterns": ["模式"], "cheatsheet": ["速查"],
}, ensure_ascii=False)


def test_schedule_waiting_when_skill_disabled(mem_db, monkeypatch):
    """技能未开启：入库仅登记 waiting_skill，不执行摘要。"""
    from app.services import kb_summary
    from app.services.skill import skill_service

    spawned = []

    def _capture(coro):
        spawned.append(coro)
        coro.close()  # 测试不执行后台协程，避免未 await 警告
    monkeypatch.setattr(kb_summary, "_spawn", _capture)
    skill_service.ensure_builtin("o1")
    rec = _record()
    row = kb_summary.schedule_summary("kb1", rec, "糖尿病饮食指南", "o1")
    assert row["status"] == "waiting_skill"
    assert spawned == []  # 未开启不摘要


def test_enable_skill_resumes_and_generates(mem_db, monkeypatch):
    """开启技能 → resume_pending 转后台；LLM 按 spec 产出结构化 JSON → done。"""
    from app.services import kb_summary
    from app.services.skill import skill_service
    import app.core.llm as llm_mod

    spawned = []

    def _capture(coro):
        spawned.append(coro)
        coro.close()
    monkeypatch.setattr(kb_summary, "_spawn", _capture)
    monkeypatch.setattr(llm_mod.LLMFactory, "create_for_role",
                        lambda *a, **k: _FakeLLM([_VALID_STRUCTURE]))
    rec = _record()
    kb_summary.schedule_summary("kb1", rec, "糖尿病饮食指南", "o1")

    sid = skill_service.kb_summary_skill_id("o1")
    skill_service.update_skill("o1", sid, enabled=True)  # 钩子触发 resume_pending
    row = db.query_one("SELECT * FROM kb_doc_summaries WHERE record_id=?", (rec,))
    assert row["status"] == "pending"
    assert len(spawned) == 1

    asyncio.run(kb_summary._run_summary(row["id"]))
    done = kb_summary.get_summary("kb1", row["id"])
    assert done["status"] == "done"
    assert done["structure"]["overview"] == "核心概览"
    assert done["structure"]["chapters"][0]["title"] == "第一章"
    assert done["structure"]["glossary"][0]["term"] == "血糖"


def test_invalid_json_retry_then_error(mem_db, monkeypatch):
    """LLM 输出非法 JSON：重试 1 次后标 error。"""
    from app.services import kb_summary
    from app.services.skill import skill_service
    import app.core.llm as llm_mod

    monkeypatch.setattr(kb_summary, "_spawn", lambda coro: coro.close())
    calls = []

    def _factory(*a, **k):
        calls.append(1)
        return _FakeLLM(["这不是 JSON"])
    monkeypatch.setattr(llm_mod.LLMFactory, "create_for_role", _factory)

    skill_service.ensure_builtin("o1")
    sid = skill_service.kb_summary_skill_id("o1")
    skill_service.update_skill("o1", sid, enabled=True)
    rec = _record()
    row = kb_summary.schedule_summary("kb1", rec, "标题", "o1")
    asyncio.run(kb_summary._run_summary(row["id"]))
    err = db.query_one("SELECT * FROM kb_doc_summaries WHERE id=?", (row["id"],))
    assert err["status"] == "error"
    assert err["error"]
    assert len(calls) == 2  # 首次 + 重试 1 次


def test_generate_returns_skill_disabled(mem_db, monkeypatch):
    """手动触发未开启技能 → skill_disabled + 登记等待 + 返回技能 id。"""
    from app.services import kb_summary
    from app.services.skill import skill_service

    monkeypatch.setattr(kb_summary, "_spawn", lambda coro: coro.close())
    skill_service.ensure_builtin("o1")
    rec = _record()
    result = kb_summary.generate("kb1", "o1", record_id=rec)
    assert result["skill_disabled"] is True
    assert result["skill_id"] == skill_service.kb_summary_skill_id("o1")
    assert result["waiting"] == 1
    row = db.query_one("SELECT status FROM kb_doc_summaries WHERE record_id=?", (rec,))
    assert row["status"] == "waiting_skill"


def test_split_sections_deterministic(mem_db):
    from app.services import kb_summary

    text = "第一章 总论\n内容甲\n第二章 分论\n内容乙"
    sections = kb_summary._split_sections(text)
    assert len(sections) == 2
    assert sections[0].startswith("第一章")
    # 无标题长文本 → 固定窗口切块
    long_sections = kb_summary._split_sections("字" * 4000)
    assert len(long_sections) == 3


# ============================================
# D7 期望来源接地
# ============================================

def test_ground_expected_source_wiki_and_docs(mem_db):
    from app.services import evaluation as ev

    db.execute("INSERT INTO wiki_pages (id, title, created_at, updated_at)"
               " VALUES ('w1', '失眠食疗百科', ?, ?)", (_now(), _now()))
    src = ev._ground_expected_source({"expected_keywords": ["失眠"]})
    assert src == "[百科] 失眠食疗百科"

    _record(rec_id="r2", title="糖尿病饮食指南", text="无关正文")
    src2 = ev._ground_expected_source({"expected_keywords": ["糖尿病"]})
    assert src2 == "糖尿病饮食指南"

    # 无命中 → 明确占位文案（而非空值）
    src3 = ev._ground_expected_source({"expected_keywords": ["量子计算"]})
    assert src3 == "内置基准题（知识库暂无对应文档）"


def test_seed_builtin_datasets_sources_nonempty(mem_db):
    """内置评测集 seed 后期望来源全部非空（接地或占位/行为型文案）。"""
    from app.services import evaluation as ev

    ev.seed_builtin_datasets()
    rows = db.query(
        "SELECT c.expected_source FROM eval_cases c"
        " JOIN eval_datasets d ON d.id = c.dataset_id"
        " WHERE d.name IN (?, ?)",
        (ev.BUILTIN_RAGAS_DATASET["name"], ev.BUILTIN_AB_DATASET["name"]))
    assert rows
    assert all((r["expected_source"] or "").strip() for r in rows)