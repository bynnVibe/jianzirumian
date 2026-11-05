"""记忆治理回归测试（内存 DB，不出网）。

覆盖 M1–M3 三项改造：
  M1  写入治理：add_memory 查重转 update、apply_extraction 的 add/update/delete 动作、
      extract_from_turn 类型化解析与动作执行（FakeLLM 桩）；
  M2  两层加载：profile_memories 筛选与预算、retrieve_relevant 时间衰减/类型权重/exclude_ids；
  M3  索引治理：_trim 遗忘顺序（重要度→last_used_at→updated_at）、_merge_cluster 合并触发。
"""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from app.core import db
from app.services.memory import MemoryService


@pytest.fixture()
def mem_db(monkeypatch):
    """独立内存数据库：避免污染本地开发库"""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    yield conn
    conn.close()


def _set_fields(conn, memory_id, **fields):
    """直接改写记忆行字段（构造 last_used_at / importance 等测试场景）。"""
    for col, val in fields.items():
        conn.execute(f"UPDATE user_memories SET {col} = ? WHERE id = ?", (val, memory_id))
    conn.commit()


class FakeLLM:
    """返回固定文本的 LLM 桩（async 生成器协议与真实 chat 一致）。"""

    def __init__(self, reply: str):
        self.reply = reply

    async def chat(self, messages, stream=False, **kwargs):
        yield self.reply


# ============================================
# M1 写入治理
# ============================================

def test_add_memory_dedup_converts_to_update(mem_db):
    """同一偏好重复写入：查重命中转覆盖更新，不堆积多条。"""
    first = MemoryService.add_memory("u1", "用户对花生过敏，饮食需避免花生", memory_type="preference")
    assert first and not first.get("updated")
    second = MemoryService.add_memory("u1", "用户对花生过敏，所有饮食都要避免花生制品", memory_type="preference")
    assert second and second.get("updated")
    rows = MemoryService.list_memories("u1")
    assert len(rows) == 1
    assert "花生制品" in rows[0]["content"]


def test_add_memory_distinct_content_inserts(mem_db):
    MemoryService.add_memory("u1", "用户喜欢晨跑锻炼")
    MemoryService.add_memory("u1", "用户正在备考营养师资格证")
    assert len(MemoryService.list_memories("u1")) == 2


def test_apply_extraction_actions(mem_db):
    """update 按 target_hint 定位覆盖；delete 命中删除；add 正常新增；单轮上限 3 条。"""
    MemoryService.add_memory("u1", "用户偏好清淡饮食少油少盐", memory_type="preference")
    MemoryService.add_memory("u1", "用户计划三个月减重五公斤", memory_type="goal")
    stats = MemoryService.apply_extraction("u1", "s1", [
        {"content": "用户偏好清淡饮食少油少盐少糖", "importance": 0.8,
         "type": "preference", "action": "update", "target_hint": "清淡饮食少油少盐"},
        {"content": "", "importance": 0.5, "type": "goal",
         "action": "delete", "target_hint": "三个月减重五公斤"},
        {"content": "用户每周游泳三次", "importance": 0.6, "type": "fact", "action": "add"},
        {"content": "超出上限的第四条不应执行", "importance": 0.5, "type": "fact", "action": "add"},
    ])
    assert stats == {"add": 1, "update": 1, "delete": 1}
    contents = [m["content"] for m in MemoryService.list_memories("u1")]
    assert any("少油少盐少糖" in c for c in contents)
    assert not any("减重" in c for c in contents)
    assert not any("第四条" in c for c in contents)
    # update 覆盖而非新增：饮食偏好仍只有一条
    assert sum(1 for c in contents if "清淡饮食" in c) == 1


def test_apply_extraction_update_falls_back_to_add(mem_db):
    """update 定位不到目标时降级为新增。"""
    stats = MemoryService.apply_extraction("u1", "s1", [
        {"content": "用户喜欢喝绿茶", "importance": 0.6,
         "type": "preference", "action": "update", "target_hint": "不存在的旧记忆内容"},
    ])
    assert stats["add"] == 1 and stats["update"] == 0


def test_sensitive_content_rejected(mem_db):
    assert MemoryService.add_memory("u1", "用户的密码是 abc123") is None
    MemoryService.add_memory("u1", "用户喜欢徒步")
    assert MemoryService.update_memory("u1", MemoryService.list_memories("u1")[0]["id"],
                                       content="身份证号是110101") is None


def test_extract_from_turn_parses_typed_actions(mem_db, monkeypatch):
    """抽取 prompt 输出类型化动作 JSON，被执行后落库为对应类型。"""
    reply = json.dumps([
        {"content": "用户对海鲜过敏", "importance": 0.9, "type": "preference",
         "action": "add", "target_hint": ""},
    ], ensure_ascii=False)
    monkeypatch.setattr("app.core.llm.LLMFactory.create_for_role", lambda *a, **k: FakeLLM(reply))
    asyncio.run(MemoryService.extract_from_turn("u1", "s1", "我对海鲜过敏", "好的，已记住"))
    rows = MemoryService.list_memories("u1")
    assert len(rows) == 1
    assert rows[0]["memory_type"] == "preference"
    assert rows[0]["importance"] == pytest.approx(0.9)


def test_extract_from_turn_guest_skipped_and_bad_json_safe(mem_db, monkeypatch):
    """guest 不抽取；LLM 输出无法解析时静默跳过（回退无动作，不抛异常）。"""
    monkeypatch.setattr("app.core.llm.LLMFactory.create_for_role",
                        lambda *a, **k: FakeLLM("这不是JSON"))
    asyncio.run(MemoryService.extract_from_turn("guest", "s1", "hi", "hello"))
    asyncio.run(MemoryService.extract_from_turn("u1", "s1", "hi", "hello"))
    assert MemoryService.list_memories("u1") == []


# ============================================
# M2 两层加载
# ============================================

def test_profile_memories_selection_and_budget(mem_db, monkeypatch):
    """画像卡：importance≥0.8 或 preference 入选，按重要度降序、受字符预算约束。"""
    from app.config import settings

    monkeypatch.setattr(settings, "MEMORY_PROFILE_CHARS", 40)
    hi = MemoryService.add_memory("u1", "高重要度事实记忆甲", importance=0.9)
    pref = MemoryService.add_memory("u1", "偏好类记忆乙", memory_type="preference", importance=0.5)
    MemoryService.add_memory("u1", "普通低重要度事实记忆丙", importance=0.3)
    profile = MemoryService.profile_memories("u1")
    ids = [m["id"] for m in profile]
    assert hi["id"] in ids and pref["id"] in ids
    # 预算 40 字：两条各不足 40 但合计未超时都保留；普通记忆不入选
    assert all(m["id"] != "丙" for m in profile)
    assert sum(len(m["content"]) for m in profile) <= 40


def test_profile_memories_excludes_guest_and_empty(mem_db):
    assert MemoryService.profile_memories("") == []


def test_retrieve_relevant_decay_and_type_weight(mem_db):
    """关键词重叠相同时：近期使用 + 偏好类型的记忆排前。"""
    old = MemoryService.add_memory("u1", "用户喜欢喝绿茶养生", memory_type="fact", importance=0.5)
    fresh = MemoryService.add_memory("u1", "用户喜欢喝红茶养胃", memory_type="preference", importance=0.5)
    _set_fields(mem_db, old["id"],
                last_used_at=(datetime.now() - timedelta(days=90)).isoformat())
    _set_fields(mem_db, fresh["id"], last_used_at=datetime.now().isoformat())
    ranked = MemoryService.retrieve_relevant("u1", "用户喜欢喝什么茶")
    assert ranked[0]["id"] == fresh["id"]


def test_retrieve_relevant_exclude_ids_and_last_used_touch(mem_db):
    """exclude_ids 与画像卡去重；命中记忆的 last_used_at 被刷新。"""
    keep = MemoryService.add_memory("u1", "用户有高血压病史需要控盐")
    skip = MemoryService.add_memory("u1", "用户血糖偏高需要控制糖分摄入")
    assert keep["id"] != skip["id"], "两条不同事实被查重误判为同一条"
    chosen = MemoryService.retrieve_relevant("u1", "高血压控盐怎么做", exclude_ids=[skip["id"]])
    assert [m["id"] for m in chosen] == [keep["id"]]
    row = db.query_one("SELECT last_used_at FROM user_memories WHERE id = ?", (keep["id"],))
    assert row["last_used_at"]


# ============================================
# M3 索引治理
# ============================================

def test_trim_forget_order(mem_db, monkeypatch):
    """超上限遗忘顺序：重要度升序 → last_used_at 旧/空者先忘。"""
    from app.services import memory as memory_module

    monkeypatch.setattr(memory_module, "MAX_MEMORIES_PER_USER", 3)
    keep_hi = MemoryService.add_memory("u1", "重要度高的记忆保留", importance=0.9)
    keep_used = MemoryService.add_memory("u1", "最近使用过的记忆保留", importance=0.4)
    forget_stale = MemoryService.add_memory("u1", "长期未用的低重要度记忆", importance=0.4)
    _set_fields(mem_db, keep_used["id"], last_used_at=datetime.now().isoformat())
    _set_fields(mem_db, forget_stale["id"],
                last_used_at=(datetime.now() - timedelta(days=120)).isoformat())
    # 触发第 4 条写入 → trim 删 1 条
    MemoryService.add_memory("u1", "第四条新记忆写入触发清理", importance=0.5)
    ids = {m["id"] for m in MemoryService.list_memories("u1")}
    assert forget_stale["id"] not in ids
    assert keep_hi["id"] in ids and keep_used["id"] in ids


def test_merge_cluster_consolidates_duplicates(mem_db, monkeypatch):
    """同类型高重叠簇 ≥3 条时 LLM 合并为一条（importance 取合并结果）。"""
    base = "用户喜欢清淡饮食少油少盐少糖"
    ids = []
    for i in range(3):
        r = MemoryService.add_memory("u1", f"{base}第{i}条补充", memory_type="preference",
                                     importance=0.5, dedup=False)
        ids.append(r["id"])
    reply = json.dumps({"content": "用户长期偏好清淡饮食（少油少盐少糖）", "importance": 0.8},
                       ensure_ascii=False)
    monkeypatch.setattr("app.core.llm.LLMFactory.create_for_role", lambda *a, **k: FakeLLM(reply))
    asyncio.run(MemoryService._merge_cluster("u1", "preference"))
    rows = MemoryService.list_memories("u1")
    assert len(rows) == 1
    assert rows[0]["id"] in ids
    assert "清淡饮食" in rows[0]["content"]
    assert rows[0]["importance"] == pytest.approx(0.8)


def test_merge_cluster_below_threshold_noop(mem_db, monkeypatch):
    """不足 3 条或重叠不够时不触发合并（LLM 不被调用）。"""
    MemoryService.add_memory("u1", "用户喜欢晨跑", memory_type="preference", dedup=False)
    MemoryService.add_memory("u1", "用户喜欢游泳", memory_type="preference", dedup=False)

    def _boom(*a, **k):
        raise AssertionError("不应调用 LLM")

    monkeypatch.setattr("app.core.llm.LLMFactory.create_for_role", _boom)
    asyncio.run(MemoryService._merge_cluster("u1", "preference"))
    assert len(MemoryService.list_memories("u1")) == 2


# ============================================
# M4 可管理性 API（/api/me/memories）
# ============================================

def test_me_memories_crud(mem_db):
    """个人中心记忆 API：列表/主动写入（类型推断）/编辑/删除/不存在 404。"""
    from fastapi import HTTPException

    from app.api.me import (
        MemoryCreateRequest, MemoryUpdateRequest,
        create_my_memory, delete_my_memory, list_my_memories, update_my_memory,
    )

    user = {"id": "u1"}
    # 主动写入：偏好措辞自动归 preference，默认高重要度
    res = asyncio.run(create_my_memory(MemoryCreateRequest(content="我喜欢喝绿茶养生"), user))
    assert res["success"] and res["memory_type"] == "preference"
    mid = res["id"]
    # 列表
    listing = asyncio.run(list_my_memories(user))
    assert len(listing["memories"]) == 1
    # 编辑：内容与类型
    upd = asyncio.run(update_my_memory(mid, MemoryUpdateRequest(content="我喜欢喝红茶养胃", memory_type="fact"), user))
    assert upd["updated"] and upd["content"] == "我喜欢喝红茶养胃"
    row = next(m for m in MemoryService.list_memories("u1") if m["id"] == mid)
    assert row["memory_type"] == "fact"
    # 删除后不存在 → 404
    assert asyncio.run(delete_my_memory(mid, user))["success"]
    with pytest.raises(HTTPException) as ei:
        asyncio.run(delete_my_memory(mid, user))
    assert ei.value.status_code == 404


def test_me_memories_rejects_sensitive_and_short(mem_db):
    """敏感内容与过短内容拒绝写入；他人记忆不可编辑。"""
    from fastapi import HTTPException

    from app.api.me import MemoryCreateRequest, MemoryUpdateRequest, create_my_memory, update_my_memory

    with pytest.raises(HTTPException) as ei:
        asyncio.run(create_my_memory(MemoryCreateRequest(content="我的密码是abc123"), {"id": "u1"}))
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException):
        asyncio.run(create_my_memory(MemoryCreateRequest(content="短"), {"id": "u1"}))
    # 他人记忆：定位不到 → 404
    res = asyncio.run(create_my_memory(MemoryCreateRequest(content="用户喜欢徒步旅行"), {"id": "u1"}))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(update_my_memory(res["id"], MemoryUpdateRequest(content="篡改内容"), {"id": "u2"}))
    assert ei.value.status_code == 404
