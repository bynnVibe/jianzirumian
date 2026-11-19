"""语义缓存（网关层）离线回归。

覆盖：余弦相似度（纯函数）、提问归一化、阈值匹配 match_best、lookup/store 的门禁
（开关/游客/长度）、写入-命中往返与命中计数、按归一化问题去重 upsert、用户隔离、
删除/清空/计数/统计；以及本轮整改新增：公共/个人域共享与隔离、阶梯 TTL、
实时过期、scope 安全判定与网关代理层拦截。全部用例仅操作隔离 SQLite，
embedding 以桩替换，不触达网络。
"""
import asyncio
import uuid
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.core import db
from app.services.gateway import semantic_cache as sc
from app.services import cache_policy as policy


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    monkeypatch.setattr(policy.settings, "ANSWER_CACHE_SIGNING_KEY", "isolated-cache-test-" * 3)

    async def equivalent(first, second, user):
        groups = ({"高血压吃什么", "高血压吃什么好", "高血压饮食注意"},
                  {"糖尿病怎么调理", "糖尿病饮食"})
        return first == second or any(first in g and second in g for g in groups)

    monkeypatch.setattr(policy, "equivalent", equivalent)
    yield conn
    conn.close()


def _uid():
    uid = f"u-{uuid.uuid4().hex[:8]}"
    db.execute("INSERT INTO users(id,username,password_hash,salt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
               (uid, uid, "unused", "unused", "0", "0"))
    return uid


def _cleanup(user_id):
    db.execute("DELETE FROM semantic_cache_v2 WHERE user_id = ?", (user_id,))


def _body(user, question, sources=None):
    messages = [{"role": "user", "content": question}]
    body = {"model": "test-model", "messages": messages}
    body["_gw_cache_contract"] = policy.sign_contract(
        user_id=user, query=question, messages=messages, model=body["model"],
        decision={"kind": "knowledge_semantic", "reason": "", "ttl_seconds": 86400},
        sources=[{"title": "来源1"}] if sources is None else sources,
        revision=policy.revisions(), use_knowledge=True)
    return body


def _verified(user, question, sources=None):
    body = _body(user, question, sources)
    contract, _ = policy.verify_contract(body, user)
    fingerprint = policy.fingerprint(contract, model=body["model"], upstream={}, rule={}, body=body) if contract else ""
    return contract, fingerprint


async def _store(user, question, answer, sources=None, **options):
    contract, fingerprint = _verified(user, question, sources)
    return await sc.store(user, question, answer, sources, contract=contract, fingerprint=fingerprint, **options)


async def _lookup(user, question, **options):
    contract, fingerprint = _verified(user, question)
    return await sc.lookup(user, question, contract=contract, fingerprint=fingerprint, **options)


def _fake_embed(text):
    """确定性桩：含「高血压」→ [1,0]，含「糖尿病」→ [0,1]，否则 [1,1] 归一化方向。"""
    async def _e(t):
        if "高血压" in t:
            return [1.0, 0.0]
        if "糖尿病" in t:
            return [0.0, 1.0]
        return [0.7071, 0.7071]
    return _e


# ============================================
# 一、纯函数
# ============================================

def test_cosine_basics():
    assert round(sc._cosine([1, 0], [1, 0]), 6) == 1.0
    assert sc._cosine([1, 0], [0, 1]) == 0.0
    assert sc._cosine([1, 0], [1, 0, 0]) == 0.0   # 维度不一致
    assert sc._cosine([], [1, 0]) == 0.0
    assert sc._cosine([0, 0], [1, 0]) == 0.0      # 零向量


def test_normalize_question():
    # 空白/大小写/中英文标点归一化后一致
    assert sc.normalize_question(" 高血压  吃什么？！ ") == "高血压 吃什么？！"
    assert sc.normalize_question("ABC") != sc.normalize_question(" a b c ")
    assert sc.normalize_question("-10°C") != sc.normalize_question("10°C")
    assert sc.normalize_question("cafe\u0301") == sc.normalize_question("café")
    assert sc.normalize_question("") == ""


def test_match_best_threshold_and_highest():
    entries = [
        {"id": "a", "embedding": [1.0, 0.0], "answer": "A"},
        {"id": "b", "embedding": [0.9, 0.1], "answer": "B"},
        {"id": "c", "embedding": [0.0, 1.0], "answer": "C"},
    ]
    # 命中最高相似度者，并附 score
    hit = sc.match_best([1.0, 0.0], entries, 0.9)
    assert hit["id"] == "a" and hit["score"] >= 0.9
    # 低于阈值 → None（与各条目相似度约 0.707，不足 0.9）
    assert sc.match_best([0.7071, 0.7071], entries, 0.9) is None
    # 空候选 → None
    assert sc.match_best([1.0, 0.0], [], 0.5) is None


# ============================================
# 二、门禁
# ============================================

def test_lookup_disabled_or_guest_or_short(monkeypatch):
    monkeypatch.setattr(sc, "_embed", _fake_embed(None))
    uid = _uid()
    try:
        # 开关关闭
        monkeypatch.setattr(sc, "semantic_cache_enabled", lambda: False)
        assert asyncio.run(_lookup(uid, "高血压吃什么")) is None
        monkeypatch.setattr(sc, "semantic_cache_enabled", lambda: True)
        # 游客 / 空用户
        assert asyncio.run(_lookup("guest", "高血压吃什么")) is None
        assert asyncio.run(_lookup("", "高血压吃什么")) is None
        # 提问过短（<4 字）
        assert asyncio.run(_lookup(uid, "好的")) is None
    finally:
        _cleanup(uid)


def test_store_gate_short_answer_and_guest(monkeypatch):
    monkeypatch.setattr(sc, "_embed", _fake_embed(None))
    monkeypatch.setattr(sc, "semantic_cache_enabled", lambda: True)
    uid = _uid()
    try:
        # 回答过短（<30 字）不缓存
        assert asyncio.run(_store(uid, "高血压吃什么", "少吃盐")) is False
        # 游客不缓存
        assert asyncio.run(_store("guest", "高血压吃什么", "低盐饮食" * 20)) is False
        assert sc.count(uid) == 0
    finally:
        _cleanup(uid)


# ============================================
# 三、写入-命中往返
# ============================================

def test_store_then_lookup_hit_and_bump(monkeypatch):
    monkeypatch.setattr(sc, "_embed", _fake_embed(None))
    monkeypatch.setattr(sc, "semantic_cache_enabled", lambda: True)
    uid = _uid()
    answer = "高血压患者应低盐饮食、规律作息、遵医嘱服药，并定期监测血压变化。"
    try:
        assert asyncio.run(_store(uid, "高血压吃什么好", answer, [{"title": "来源1"}])) is True
        assert sc.count(uid) == 1
        # 语义相似（同方向向量）→ 命中，返回缓存回答与来源
        hit = asyncio.run(_lookup(uid, "高血压饮食注意", threshold=0.9))
        assert hit is not None and hit["answer"] == answer
        assert hit["sources"] == [{"title": "来源1"}]
        assert hit["hit_count"] == 1     # 命中后计数 +1
        # 语义无关（糖尿病方向正交）→ 未命中
        assert asyncio.run(_lookup(uid, "糖尿病怎么调理", threshold=0.9)) is None
    finally:
        _cleanup(uid)


def test_store_dedup_by_normalized_question(monkeypatch):
    monkeypatch.setattr(sc, "_embed", _fake_embed(None))
    monkeypatch.setattr(sc, "semantic_cache_enabled", lambda: True)
    uid = _uid()
    a1 = "低盐饮食、规律作息、遵医嘱服药，定期监测血压。" * 2
    a2 = "更新后的回答：控盐控重、戒烟限酒、坚持运动与用药。" * 2
    try:
        asyncio.run(_store(uid, "高血压 吃什么？", a1))
        # 归一化后同一问题 → upsert 覆盖而非新增
        asyncio.run(_store(uid, " 高血压  吃什么？ ", a2))
        assert sc.count(uid) == 1
        entries = sc.list_entries(uid)
        assert entries[0]["answer"] == a2
    finally:
        _cleanup(uid)


def test_user_isolation(monkeypatch):
    monkeypatch.setattr(sc, "_embed", _fake_embed(None))
    monkeypatch.setattr(sc, "semantic_cache_enabled", lambda: True)
    ua, ub = _uid(), _uid()
    answer = "这是用户A的高血压饮食建议：低盐低脂、规律作息、戒烟限酒、坚持适度运动，并遵医嘱用药与定期监测血压变化。"
    try:
        asyncio.run(_store(ua, "高血压吃什么", answer))
        # 用户B 即使问相同问题也不命中用户A 的缓存
        assert asyncio.run(_lookup(ub, "高血压吃什么", threshold=0.9)) is None
        assert sc.count(ub) == 0
        assert asyncio.run(_lookup(ua, "高血压吃什么", threshold=0.9)) is not None
    finally:
        _cleanup(ua)
        _cleanup(ub)


# ============================================
# 四、管理：删除 / 清空 / 统计
# ============================================

def test_delete_clear_count_stats(monkeypatch):
    monkeypatch.setattr(sc, "_embed", _fake_embed(None))
    monkeypatch.setattr(sc, "semantic_cache_enabled", lambda: True)
    uid = _uid()
    ans = "高血压患者应低盐低脂饮食、规律作息、戒烟限酒、坚持适度运动，并遵医嘱规律服药与定期监测血压变化。"
    try:
        asyncio.run(_store(uid, "高血压吃什么", ans))
        asyncio.run(_store(uid, "糖尿病怎么调理", "糖尿病患者应控制糖分与精制碳水摄入、规律运动、监测血糖，并遵医嘱用药与健康生活。"))
        assert sc.count(uid) == 2
        entries = sc.list_entries(uid)
        # 删除「高血压」那条（按问题定位，避免依赖排序）
        hyper = next(e for e in entries if "高血压" in e["question"])
        assert sc.delete_entry(uid, hyper["id"]) is True
        assert sc.count(uid) == 1
        assert sc.delete_entry(uid, "不存在的id") is False
        # 统计
        st = sc.stats(uid)
        assert st["enabled"] is True and st["count"] == 1 and st["threshold"] > 0
        # 命中剩下的「糖尿病」条目后累计命中反映到 stats
        asyncio.run(_lookup(uid, "糖尿病饮食", threshold=0.9))
        assert sc.stats(uid)["total_hits"] >= 1
        # 清空
        assert sc.clear(uid) == 1
        assert sc.count(uid) == 0
    finally:
        _cleanup(uid)


# ============================================
# 五、scope 安全判定（纯函数）
# ============================================

def test_resolve_scope():
    # 个人数据信号 → 强制 personal（即便 LLM 说 public，也绝不公共共享个性化答案）
    assert sc.resolve_scope("public", "根据我的记录该吃什么药") == "personal"
    assert sc.resolve_scope("public", "我的血压为何偏高") == "personal"
    # 本轮注入了用户相关记忆 → personal
    assert sc.resolve_scope("public", "高血压吃什么", uses_relevant_memory=True) == "personal"
    # LLM 明确 public 且无个人信号/无记忆注入 → public
    assert sc.resolve_scope("public", "高血压患者如何低盐饮食") == "personal"
    # 缺省/解析失败/其他取值 → 一律回落 personal
    assert sc.resolve_scope("", "高血压吃什么") == "personal"
    assert sc.resolve_scope("unknown", "高血压吃什么") == "personal"
    assert sc.resolve_scope("personal", "高血压吃什么") == "personal"


# ============================================
# 六、阶梯 TTL（实时固定短档 + 非实时热度三档）
# ============================================

def test_compute_ttl_seconds_fixed(monkeypatch):
    for hits in (0, 10, 999):
        assert sc.compute_ttl_seconds(True, hits) == 0
        assert sc.compute_ttl_seconds(False, hits) == 86400
    monkeypatch.setattr(sc.settings, "ANSWER_CACHE_TTL_SECONDS", 3600)
    assert sc.compute_ttl_seconds(False, 999) == 3600
    monkeypatch.setattr(sc.settings, "ANSWER_CACHE_TTL_SECONDS", 864000)
    assert sc.compute_ttl_seconds(False, 999) == 86400


def test_ttl_label():
    assert "不缓存" in sc.ttl_label(True, 0)
    assert "硬期限" in sc.ttl_label(False, 0)
    assert "命中不续期" in sc.ttl_label(False, 999)
    assert sc.ttl_label(False, 0) == sc.ttl_label(False, 999)


# ============================================
# 七、公共/个人域：跨用户共享与隔离
# ============================================

def test_public_scope_rejected_even_with_valid_contract(monkeypatch):
    monkeypatch.setattr(sc, "_embed", _fake_embed(None))
    monkeypatch.setattr(sc, "semantic_cache_enabled", lambda: True)
    ua, ub = _uid(), _uid()
    answer = "通用知识回答" * 20
    assert asyncio.run(_store(ua, "高血压吃什么", answer, scope="public")) is False
    assert sc.count(ua) == 0
    assert asyncio.run(_store(ua, "高血压吃什么", answer)) is True
    for user in (ua, ub):
        assert asyncio.run(_lookup(user, "高血压吃什么", scope="public")) is None
    assert sc.list_entries(ub, include_public=True) == []
    own = sc.list_entries(ua)[0]
    assert sc.delete_entry(ub, own["id"]) is False


def test_personal_scope_not_shared(monkeypatch):
    monkeypatch.setattr(sc, "_embed", _fake_embed(None))
    monkeypatch.setattr(sc, "semantic_cache_enabled", lambda: True)
    ua, ub = _uid(), _uid()
    answer = "A 的个性化建议：结合你的档案，血压偏高应低盐饮食、规律作息并遵医嘱用药与监测。"
    try:
        # A 以个人域写入
        assert asyncio.run(_store(ua, "高血压吃什么", answer, None, scope="personal")) is True
        # B 无论公共域还是个人域查询均不命中 A 的个人条目
        assert asyncio.run(_lookup(ub, "高血压饮食注意", scope="public", threshold=0.9)) is None
        assert asyncio.run(_lookup(ub, "高血压饮食注意", scope="personal", threshold=0.9)) is None
        # A 自己仍可命中
        assert asyncio.run(_lookup(ua, "高血压饮食注意", scope="personal", threshold=0.9)) is not None
    finally:
        _cleanup(ua)
        _cleanup(ub)


# ============================================
# 八、实时过期跳过 + 惰性清理
# ============================================

def test_realtime_bypass_and_static_expiry_purge(monkeypatch):
    monkeypatch.setattr(sc, "_embed", _fake_embed(None))
    monkeypatch.setattr(sc, "semantic_cache_enabled", lambda: True)
    uid = _uid()
    answer = "有依据的静态知识回答" * 20
    assert asyncio.run(_store(uid, "高血压今天数据", answer, is_realtime=True)) is False
    assert asyncio.run(_store(uid, "高血压吃什么", answer)) is True
    assert asyncio.run(_lookup(uid, "高血压吃什么", is_realtime=True)) is None
    assert asyncio.run(_lookup(uid, "高血压吃什么")) is not None
    db.execute("UPDATE semantic_cache_v2 SET expires_at=? WHERE user_id=?",
               ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), uid))
    assert asyncio.run(_lookup(uid, "高血压吃什么")) is None
    assert sc.purge_expired() == 1
    assert sc.count(uid) == 0


# ============================================
# 九、网关代理层拦截（Ctx + _cache_lookup + 合成响应）
# ============================================

def test_proxy_cache_interception(monkeypatch):
    monkeypatch.setattr(sc, "_embed", _fake_embed(None))
    monkeypatch.setattr(sc, "semantic_cache_enabled", lambda: True)
    from app.services.gateway import proxy
    uid = _uid()
    answer = "高血压患者应低盐低脂饮食、规律作息、戒烟限酒、坚持适度运动，并遵医嘱规律服药与定期监测血压变化。"
    try:
        # 预置一条个人域缓存
        ctx = proxy.Ctx()
        ctx.user_id = uid
        ctx.model_req = "test-model"
        ctx.cache_enabled = True
        ctx.cache_query = "高血压饮食注意"
        ctx.cache_scope = "personal"
        ctx.cache_realtime = False
        ctx.forward_body = _body(uid, ctx.cache_query)
        monkeypatch.setattr(proxy.up_svc, "effective", lambda x: x)
        assert asyncio.run(proxy._cache_lookup(ctx)) is None
        asyncio.run(proxy._cache_store(ctx, answer))
        # 出网前查缓存 → 命中，返回缓存正文（跳过上游 LLM）
        got = asyncio.run(proxy._cache_lookup(ctx))
        assert got == answer
        # 合成非流式响应：OpenAI 兼容、usage 全 0、带命中标记
        comp = proxy._synthetic_completion(ctx, got)
        assert comp["object"] == "chat.completion"
        assert comp["choices"][0]["message"]["content"] == answer
        assert comp["choices"][0]["finish_reason"] == "stop"
        assert comp["usage"]["total_tokens"] == 0
        assert comp["_gw_cache"] == "hit"
        # 合成流式响应：块序列以 [DONE] 收尾
        chunks = proxy._synthetic_stream(ctx, got)
        assert chunks[0].startswith("data: ") and answer in chunks[0]
        assert chunks[-1] == "data: [DONE]\n\n"
        # 未启用缓存的上下文不查询
        ctx2 = proxy.Ctx()
        ctx2.user_id = uid
        ctx2.cache_enabled = False
        assert asyncio.run(proxy._cache_lookup(ctx2)) is None
    finally:
        _cleanup(uid)


# ============================================
# 十、按用户隔离安全加固（防跨用户个人知识库泄露）
# ============================================

def test_resolve_scope_personal_kb_forces_personal():
    """回答引用了个人知识库内容时，即使意图识别输出 public 也强制个人域。"""
    assert sc.resolve_scope("public", "云边端三层架构是什么", uses_personal_kb=True) == "personal"
    assert sc.resolve_scope("public", "云边端三层架构是什么") == "personal"
    assert sc.resolve_scope("", "云边端三层架构是什么", uses_personal_kb=True) == "personal"


def test_touches_personal_kb_visibility():
    """chat 侧判定：命中源来自非公共库/库不可查 → 视为触碰个人库。"""
    from app.services import chat as chat_svc
    from app.services import kb as kb_service

    origin = kb_service.get_kb
    try:
        kb_service.get_kb = lambda kb_id: {"id": kb_id, "visibility": "private"} \
            if kb_id == "kb-priv" else {"id": kb_id, "visibility": "public"}
        assert chat_svc._touches_personal_kb([{"kb_id": "kb-priv"}]) is True
        assert chat_svc._touches_personal_kb([{"kb_id": "kb-pub"}]) is False
        assert chat_svc._touches_personal_kb([{"kb_id": "kb-gone"}]) is False
        kb_service.get_kb = lambda kb_id: None   # 库已删除 → 保守按个人域
        assert chat_svc._touches_personal_kb([{"kb_id": "kb-gone"}]) is True
        assert chat_svc._touches_personal_kb([]) is False
    finally:
        kb_service.get_kb = origin


def test_legacy_public_pool_never_used(monkeypatch):
    monkeypatch.setattr(sc, "_embed", _fake_embed(None))
    monkeypatch.setattr(sc, "semantic_cache_enabled", lambda: True)
    ua, ub = _uid(), _uid()
    own = "属于当前用户的已验证知识回答" * 10
    db.execute("INSERT INTO semantic_cache(user_id,question,question_norm,answer,scope,created_at,last_used_at) "
               "VALUES('__public__','高血压吃什么','高血压吃什么','旧公共回答','public','','')")
    assert asyncio.run(_store(ua, "高血压吃什么", own)) is True
    assert asyncio.run(_lookup(ua, "高血压饮食注意"))["answer"] == own
    assert asyncio.run(_lookup(ub, "高血压饮食注意")) is None
    assert sc.list_entries(ub, include_public=True) == []
    assert db.query_one("SELECT COUNT(*) n FROM semantic_cache")["n"] == 1


# ============================================
# 十一、整改2：检索前探针 probe
# ============================================

def test_probe_hits_similar_question_and_bumps(monkeypatch):
    monkeypatch.setattr(sc, "_embed", _fake_embed(None))
    monkeypatch.setattr(sc, "semantic_cache_enabled", lambda: True)

    async def eq(first, second, user, score=None):
        return True

    monkeypatch.setattr(policy, "equivalent", eq)
    uid = _uid()
    answer = "网关模块采用规则路由、上游管理与可观测性三层设计，支持主备兜底与语义缓存拦截。"
    try:
        assert asyncio.run(_store(uid, "系统的网关模块设计", answer)) is True
        # 高相似改述提问 → 检索前命中，复用缓存答案
        hit = asyncio.run(sc.probe(uid, "系统网关模块的设计"))
        assert hit is not None and hit["answer"] == answer
        assert hit["hit_count"] == 1
        # 个人化/指代/游客/过短 一律旁路
        assert asyncio.run(sc.probe(uid, "我的网关模块设计")) is None
        assert asyncio.run(sc.probe(uid, "继续讲网关模块")) is None
        assert asyncio.run(sc.probe("guest", "系统的网关模块设计")) is None
        assert asyncio.run(sc.probe(uid, "好")) is None
        # 知识库版本变化 → 探针失效
        db.execute("UPDATE semantic_cache_v2 SET revisions='{}' WHERE user_id=?", (uid,))
        assert asyncio.run(sc.probe(uid, "系统网关模块的设计")) is None
    finally:
        _cleanup(uid)


def test_probe_disabled_switch(monkeypatch):
    monkeypatch.setattr(sc, "_embed", _fake_embed(None))
    monkeypatch.setattr(sc, "semantic_cache_enabled", lambda: False)
    uid = _uid()
    try:
        assert asyncio.run(sc.probe(uid, "系统的网关模块设计")) is None
    finally:
        _cleanup(uid)


def test_probe_profile_hash_guards_reuse(monkeypatch):
    """整改2：常驻画像不阻断复用（同用户画像稳定），但画像变更后旧条目不再命中。"""
    monkeypatch.setattr(sc, "_embed", _fake_embed(None))
    monkeypatch.setattr(sc, "semantic_cache_enabled", lambda: True)
    uid = _uid()
    answer = "网关模块采用规则路由与上游管理两层设计，支持主备兜底与熔断降级。"
    try:
        assert asyncio.run(_store(uid, "系统的网关模块设计", answer)) is True
        ph = policy.digest("用户画像（常驻）:\n注重养生")
        db.execute("UPDATE semantic_cache_v2 SET profile_hash=? WHERE user_id=?", (ph, uid))
        # 画像一致 → 命中
        hit = asyncio.run(sc.probe(uid, "系统的网关模块设计", profile_hash=ph))
        assert hit is not None and hit["answer"] == answer
        # 画像变更 → 旧条目不再命中
        assert asyncio.run(sc.probe(uid, "系统的网关模块设计",
                                    profile_hash=policy.digest("新画像"))) is None
        # 未传画像（默认空）与带画像条目不匹配
        assert asyncio.run(sc.probe(uid, "系统的网关模块设计")) is None
    finally:
        _cleanup(uid)


def test_memory_writes_do_not_invalidate_cache_revisions():
    """整改2：每轮记忆抽取/checkpoint 留痕写 user_memories 不得使全体缓存失效。"""
    before = policy.revisions()
    now = datetime.now().isoformat()
    db.execute(
        "INSERT INTO user_memories (id,user_id,memory_type,content,keywords,"
        "source_session_id,source_message_id,importance,confidence,created_at,updated_at)"
        " VALUES ('m-rev-1','u-rev','fact','测试记忆','[]','','',0.5,1.0,?,?)", (now, now))
    assert policy.revisions() == before
    db.execute("DELETE FROM user_memories WHERE id='m-rev-1'")
    assert policy.revisions() == before


def test_migration_clears_pre_isolation_public_pool():
    """存量库一次性清理：隔离前的公共池可能含他人个人 KB 答案，迁移须清空公共池
    且保留个人域条目，并记录迁移版本避免重复执行。"""
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = _sqlite3.Row
    db.init_db(conn)
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO semantic_cache (user_id, question, question_norm, answer, scope,"
        " created_at, last_used_at) VALUES ('__public__','q','q','a','public',?,?)", (now, now))
    conn.execute(
        "INSERT INTO semantic_cache (user_id, question, question_norm, answer, scope,"
        " created_at, last_used_at) VALUES ('u-x','q','q','a','personal',?,?)", (now, now))
    conn.commit()
    # 模拟「修复前已建库」：抹掉迁移标记后重启（init_db）触发一次性清理
    conn.execute("DELETE FROM schema_migrations WHERE version='semcache_isolation_v1'")
    conn.commit()
    db.init_db(conn)
    pub = conn.execute(
        "SELECT COUNT(*) c FROM semantic_cache WHERE user_id='__public__'").fetchone()["c"]
    own = conn.execute(
        "SELECT COUNT(*) c FROM semantic_cache WHERE user_id='u-x'").fetchone()["c"]
    mig = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version='semcache_isolation_v1'").fetchone()
    assert pub == 0 and own == 1 and mig is not None
    conn.close()
