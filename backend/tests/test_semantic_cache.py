"""语义缓存（网关层）离线回归。

覆盖：余弦相似度（纯函数）、提问归一化、阈值匹配 match_best、lookup/store 的门禁
（开关/游客/长度）、写入-命中往返与命中计数、按归一化问题去重 upsert、用户隔离、
删除/清空/计数/统计；以及本轮整改新增：公共/个人域共享与隔离、阶梯 TTL、
实时过期、scope 安全判定与网关代理层拦截。全部用例仅操作隔离 SQLite，
embedding 以桩替换，不触达网络。
"""
import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from app.core import db
from app.services.gateway import semantic_cache as sc


def _uid():
    return f"u-{uuid.uuid4().hex[:8]}"


def _cleanup(user_id):
    db.execute("DELETE FROM semantic_cache WHERE user_id = ?", (user_id,))


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
    assert sc.normalize_question("高血压  吃什么？！") == sc.normalize_question("高血压吃什么")
    assert sc.normalize_question("ABC") == sc.normalize_question(" a b c ")
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
        assert asyncio.run(sc.lookup(uid, "高血压吃什么")) is None
        monkeypatch.setattr(sc, "semantic_cache_enabled", lambda: True)
        # 游客 / 空用户
        assert asyncio.run(sc.lookup("guest", "高血压吃什么")) is None
        assert asyncio.run(sc.lookup("", "高血压吃什么")) is None
        # 提问过短（<4 字）
        assert asyncio.run(sc.lookup(uid, "好的")) is None
    finally:
        _cleanup(uid)


def test_store_gate_short_answer_and_guest(monkeypatch):
    monkeypatch.setattr(sc, "_embed", _fake_embed(None))
    monkeypatch.setattr(sc, "semantic_cache_enabled", lambda: True)
    uid = _uid()
    try:
        # 回答过短（<30 字）不缓存
        assert asyncio.run(sc.store(uid, "高血压吃什么", "少吃盐")) is False
        # 游客不缓存
        assert asyncio.run(sc.store("guest", "高血压吃什么", "低盐饮食" * 20)) is False
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
        assert asyncio.run(sc.store(uid, "高血压吃什么好", answer, [{"title": "来源1"}])) is True
        assert sc.count(uid) == 1
        # 语义相似（同方向向量）→ 命中，返回缓存回答与来源
        hit = asyncio.run(sc.lookup(uid, "高血压饮食注意", threshold=0.9))
        assert hit is not None and hit["answer"] == answer
        assert hit["sources"] == [{"title": "来源1"}]
        assert hit["hit_count"] == 1     # 命中后计数 +1
        # 语义无关（糖尿病方向正交）→ 未命中
        assert asyncio.run(sc.lookup(uid, "糖尿病怎么调理", threshold=0.9)) is None
    finally:
        _cleanup(uid)


def test_store_dedup_by_normalized_question(monkeypatch):
    monkeypatch.setattr(sc, "_embed", _fake_embed(None))
    monkeypatch.setattr(sc, "semantic_cache_enabled", lambda: True)
    uid = _uid()
    a1 = "低盐饮食、规律作息、遵医嘱服药，定期监测血压。" * 2
    a2 = "更新后的回答：控盐控重、戒烟限酒、坚持运动与用药。" * 2
    try:
        asyncio.run(sc.store(uid, "高血压吃什么？", a1))
        # 归一化后同一问题 → upsert 覆盖而非新增
        asyncio.run(sc.store(uid, "高血压  吃什么", a2))
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
        asyncio.run(sc.store(ua, "高血压吃什么", answer))
        # 用户B 即使问相同问题也不命中用户A 的缓存
        assert asyncio.run(sc.lookup(ub, "高血压吃什么", threshold=0.9)) is None
        assert sc.count(ub) == 0
        assert asyncio.run(sc.lookup(ua, "高血压吃什么", threshold=0.9)) is not None
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
        asyncio.run(sc.store(uid, "高血压吃什么", ans))
        asyncio.run(sc.store(uid, "糖尿病怎么调理", "糖尿病患者应控制糖分与精制碳水摄入、规律运动、监测血糖，并遵医嘱用药与健康生活。"))
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
        asyncio.run(sc.lookup(uid, "糖尿病饮食", threshold=0.9))
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
    assert sc.resolve_scope("public", "高血压患者如何低盐饮食") == "public"
    # 缺省/解析失败/其他取值 → 一律回落 personal
    assert sc.resolve_scope("", "高血压吃什么") == "personal"
    assert sc.resolve_scope("unknown", "高血压吃什么") == "personal"
    assert sc.resolve_scope("personal", "高血压吃什么") == "personal"


# ============================================
# 六、阶梯 TTL（实时固定短档 + 非实时热度三档）
# ============================================

def test_compute_ttl_seconds_tiers():
    s = sc.settings
    # 实时：固定短档，与命中次数无关
    assert sc.compute_ttl_seconds(True, 0) == s.SEMANTIC_CACHE_TTL_REALTIME_HOURS * 3600
    assert sc.compute_ttl_seconds(True, 999) == s.SEMANTIC_CACHE_TTL_REALTIME_HOURS * 3600
    # 非实时：冷 → 常命中 → 热点 三档升级
    assert sc.compute_ttl_seconds(False, 0) == s.SEMANTIC_CACHE_TTL_COLD_DAYS * 86400
    assert sc.compute_ttl_seconds(False, s.SEMANTIC_CACHE_WARM_HITS) == s.SEMANTIC_CACHE_TTL_WARM_DAYS * 86400
    assert sc.compute_ttl_seconds(False, s.SEMANTIC_CACHE_HOT_HITS) == s.SEMANTIC_CACHE_TTL_HOT_DAYS * 86400


def test_ttl_label():
    s = sc.settings
    assert "实时" in sc.ttl_label(True, 0)
    assert sc.ttl_label(False, 0).startswith("冷")
    assert sc.ttl_label(False, s.SEMANTIC_CACHE_WARM_HITS).startswith("常命中")
    assert sc.ttl_label(False, s.SEMANTIC_CACHE_HOT_HITS).startswith("热点")


# ============================================
# 七、公共/个人域：跨用户共享与隔离
# ============================================

def test_public_scope_shared_across_users(monkeypatch):
    monkeypatch.setattr(sc, "_embed", _fake_embed(None))
    monkeypatch.setattr(sc, "semantic_cache_enabled", lambda: True)
    ua, ub = _uid(), _uid()
    answer = "通用科普：高血压患者应低盐低脂饮食、规律作息、戒烟限酒、坚持适度运动，并遵医嘱规律服药。"
    try:
        # A 以公共域写入 → 属主为哨兵 PUBLIC_USER
        assert asyncio.run(sc.store(ua, "高血压吃什么", answer, None, scope="public")) is True
        assert sc.count(ua) == 0   # 公共条目不计入 A 的个人域
        # B 以公共域查询相似问题 → 命中（跨用户共享复用）
        hit = asyncio.run(sc.lookup(ub, "高血压饮食注意", scope="public", threshold=0.9))
        assert hit is not None and hit["answer"] == answer and hit["scope"] == "public"
        # B 以个人域查询 → 不命中公共条目（避免用通用答案回复个性化问题）
        assert asyncio.run(sc.lookup(ub, "高血压饮食注意", scope="personal", threshold=0.9)) is None
        assert sc.count(ub) == 0
        # 公共条目不允许普通用户删除
        pub = next(e for e in sc.list_entries(ub, include_public=True) if e["scope"] == "public")
        assert sc.delete_entry(ub, pub["id"]) is False
    finally:
        _cleanup(ua)
        _cleanup(ub)
        _cleanup(sc.PUBLIC_USER)


def test_personal_scope_not_shared(monkeypatch):
    monkeypatch.setattr(sc, "_embed", _fake_embed(None))
    monkeypatch.setattr(sc, "semantic_cache_enabled", lambda: True)
    ua, ub = _uid(), _uid()
    answer = "A 的个性化建议：结合你的档案，血压偏高应低盐饮食、规律作息并遵医嘱用药与监测。"
    try:
        # A 以个人域写入
        assert asyncio.run(sc.store(ua, "高血压吃什么", answer, None, scope="personal")) is True
        # B 无论公共域还是个人域查询均不命中 A 的个人条目
        assert asyncio.run(sc.lookup(ub, "高血压饮食注意", scope="public", threshold=0.9)) is None
        assert asyncio.run(sc.lookup(ub, "高血压饮食注意", scope="personal", threshold=0.9)) is None
        # A 自己仍可命中
        assert asyncio.run(sc.lookup(ua, "高血压饮食注意", scope="personal", threshold=0.9)) is not None
    finally:
        _cleanup(ua)
        _cleanup(ub)


# ============================================
# 八、实时过期跳过 + 惰性清理
# ============================================

def test_realtime_expiry_skip_and_purge(monkeypatch):
    monkeypatch.setattr(sc, "_embed", _fake_embed(None))
    monkeypatch.setattr(sc, "semantic_cache_enabled", lambda: True)
    uid = _uid()
    answer = "今日实时监测：当前血压偏高，建议尽快复测并咨询医生，避免剧烈运动与情绪波动。"
    try:
        assert asyncio.run(sc.store(uid, "高血压今天数据", answer, None, is_realtime=True)) is True
        # 未过期时应命中
        assert asyncio.run(sc.lookup(uid, "高血压今天数据", is_realtime=True)) is not None
        # 手动把过期时间改到过去 → lookup 应跳过（不返回陈旧实时数据）
        db.execute("UPDATE semantic_cache SET expires_at = ? WHERE user_id = ?",
                   ((datetime.now() - timedelta(hours=1)).isoformat(), uid))
        assert asyncio.run(sc.lookup(uid, "高血压今天数据", is_realtime=True)) is None
        # purge_expired 应删除该过期条目
        assert sc.purge_expired() >= 1
        assert sc.count(uid) == 0
    finally:
        _cleanup(uid)


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
        asyncio.run(sc.store(uid, "高血压吃什么", answer))
        # 构造网关上下文：启用缓存 + 语义相似的查询
        ctx = proxy.Ctx()
        ctx.user_id = uid
        ctx.model_req = "test-model"
        ctx.cache_enabled = True
        ctx.cache_query = "高血压饮食注意"
        ctx.cache_scope = "personal"
        ctx.cache_realtime = False
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
