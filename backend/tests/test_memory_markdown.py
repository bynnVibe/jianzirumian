"""五层记忆 Markdown 持久层测试（memory_markdown_v1，Claude Code 存储分级剔除 Team）。

覆盖：
  存储根目录定位（文件 DB 同址 / 内存 DB 不落盘）；
  AutoMem 写路径双写（topic 文件 + MEMORY.md 索引 + file_path 回填）与编辑/删除演进；
  User 层 USER.md 镜像、助手分域 agents/<agent>.md 镜像；
  存量迁移 migrate_from_conn（DB → markdown，文件已存在不覆盖）；
  静态层读取路径组装（Managed→Project→Local 顺序与字符预算）；
  五层 API 权限（managed=admin / project=KB 属主 / 敏感拦截）与观测事件落库。
"""
import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.core import db
from app.services import memory_store, user_instructions
from app.services.agent_memory import AgentMemoryService
from app.services.memory import MemoryService


@pytest.fixture()
def file_db(tmp_path, monkeypatch):
    """临时文件数据库：markdown 持久层真实落盘（隔离目录内，不污染开发库）。"""
    conn = sqlite3.connect(str(tmp_path / "app.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    yield conn
    conn.close()


def _auto_dir(uid="u1"):
    return memory_store.base_dir() / "users" / uid / "auto"


# ============================================
# 存储根目录与种子
# ============================================

def test_base_dir_colocated_with_sqlite(file_db, tmp_path):
    assert memory_store.base_dir() == tmp_path / "memory"


def test_memory_db_no_disk(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(db, "_conn", conn)
    assert memory_store.base_dir() is None
    # 内存 DB 下写路径静默跳过（DB 镜像仍生效）
    assert MemoryService.add_memory("u1", "用户喜欢喝绿茶养生") is not None
    conn.close()


def test_managed_seed_on_migration(file_db):
    p = memory_store.layer_path("managed")
    assert p.is_file() and "平台记忆策略" in p.read_text(encoding="utf-8")


# ============================================
# AutoMem 写路径双写与持续演进
# ============================================

def test_add_memory_writes_topic_and_index(file_db):
    res = MemoryService.add_memory("u1", "用户对花生过敏，饮食需避免花生", memory_type="preference")
    files = list(_auto_dir().glob("preference_*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    meta = memory_store.parse_frontmatter(text)
    assert meta["id"] == res["id"] and meta["memory_type"] == "preference"
    assert "花生过敏" in text
    # 索引行格式：- [file](file) — 描述
    index = (_auto_dir() / "MEMORY.md").read_text(encoding="utf-8")
    assert f"- [{files[0].name}]({files[0].name}) — " in index
    # file_path 镜像列回填
    row = db.query_one("SELECT file_path FROM user_memories WHERE id=?", (res["id"],))
    assert row["file_path"] == f"users/u1/auto/{files[0].name}"


def test_update_and_delete_evolve_files(file_db):
    res = MemoryService.add_memory("u1", "用户偏好清淡饮食")
    MemoryService.update_memory("u1", res["id"], content="用户偏好清淡饮食，少油少盐")
    f = _auto_dir() / memory_store.memory_file_name(res)
    assert "少油少盐" in f.read_text(encoding="utf-8")
    assert MemoryService.delete_memory("u1", res["id"])
    assert not f.exists()
    index = (_auto_dir() / "MEMORY.md").read_text(encoding="utf-8")
    assert f.name not in index


def test_write_events_logged(file_db):
    res = MemoryService.add_memory("u1", "用户长期关注睡眠质量改善")
    MemoryService.update_memory("u1", res["id"], content="用户长期关注睡眠质量与作息规律")
    MemoryService.delete_memory("u1", res["id"])
    actions = [r["action"] for r in db.query(
        "SELECT action FROM memory_write_events WHERE user_id='u1' ORDER BY created_at")]
    assert actions == ["add", "update", "delete"]


# ============================================
# 静态层镜像与助手分域镜像
# ============================================

def test_user_layer_mirror(file_db):
    user_instructions.set_instruction("u1", "回答先给结论再给依据")
    p = memory_store.layer_path("user", user_id="u1")
    assert p.read_text(encoding="utf-8").strip() == "回答先给结论再给依据"


def test_agent_mirror(file_db):
    AgentMemoryService.add_memory("u1", "wiki_assistant", "常分析睡眠与穴位主题文档")
    p = memory_store.base_dir() / "users" / "u1" / "agents" / "wiki_assistant.md"
    assert "睡眠与穴位" in p.read_text(encoding="utf-8")


def test_layers_overview_five_layers(file_db):
    MemoryService.add_memory("u1", "用户喜欢喝绿茶养生")
    layers = memory_store.layers_overview("u1")
    assert [l["layer"] for l in layers] == list(memory_store.LAYERS)
    assert all(l["label"] for l in layers)
    auto = next(l for l in layers if l["layer"] == "auto")
    assert auto["exists"] and auto["chars"] > 0


# ============================================
# 存量迁移（DB → markdown）
# ============================================

def test_migrate_exports_existing_rows(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)  # 先建全量 schema（含新列）
    now = "2026-01-01T00:00:00"
    conn.execute("INSERT INTO user_memories (id, user_id, memory_type, content, created_at, updated_at)"
                 " VALUES ('m-legacy-1','u9','fact','存量记忆：用户关注血压管理',?,?)", (now, now))
    conn.execute("INSERT INTO user_instructions (id, user_id, content, updated_at)"
                 " VALUES ('i1','u9','涉及剂量务必标注来源',?)", (now,))
    conn.execute("INSERT INTO agent_memories (id, user_id, agent_key, memory_type, content, created_at, updated_at)"
                 " VALUES ('a1','u9','wiki_assistant','fact','常分析心血管文档',?,?)", (now, now))
    conn.commit()
    # 模拟存量库：清除迁移标记后重跑 init_db
    conn.execute("DELETE FROM schema_migrations WHERE version='memory_markdown_v1'")
    conn.commit()
    db.init_db(conn)
    base = path.parent / "memory"
    assert (base / "users" / "u9" / "auto" / "fact_m-legacy.md").is_file()
    assert "血压管理" in (base / "users" / "u9" / "auto" / "fact_m-legacy.md").read_text(encoding="utf-8")
    assert "剂量" in (base / "users" / "u9" / "USER.md").read_text(encoding="utf-8")
    assert "心血管" in (base / "users" / "u9" / "agents" / "wiki_assistant.md").read_text(encoding="utf-8")
    index = (base / "users" / "u9" / "auto" / "MEMORY.md").read_text(encoding="utf-8")
    assert "fact_m-legacy.md" in index
    conn.close()


# ============================================
# 读取路径：静态层组装顺序与预算
# ============================================

def test_static_layer_messages_order_and_budget(file_db):
    memory_store.write_layer("managed", "平台策略甲")
    memory_store.write_layer("project", "项目规则乙", kb_id="kb1")
    memory_store.write_layer("local", "私人笔记丙", user_id="u1", kb_id="kb1")
    msgs = memory_store.static_layer_messages("u1", "kb1")
    assert [m["segment"] for m in msgs] == ["memory_managed", "memory_project", "memory_local"]
    # 游客不注入 Project/Local；无 kb_id 同理
    assert [m["segment"] for m in memory_store.static_layer_messages("guest", "kb1")] == ["memory_managed"]
    assert [m["segment"] for m in memory_store.static_layer_messages("u1", "")] == ["memory_managed"]
    # 单层注入预算截断
    memory_store.write_layer("project", "长" * 9000, kb_id="kb1")
    msgs = memory_store.static_layer_messages("u1", "kb1")
    proj = next(m for m in msgs if m["segment"] == "memory_project")
    assert len(proj["content"]) <= memory_store.MAX_LAYER_INJECT_CHARS + 40


# ============================================
# 五层 API 权限与敏感拦截
# ============================================

def test_layer_api_permissions(file_db):
    import asyncio

    from app.api.me import LayerUpdateRequest, my_memory_layers, update_memory_layer

    admin = {"id": "u1", "role": "admin"}
    member = {"id": "u2", "role": "user"}
    # managed：仅管理员可写
    res = asyncio.run(update_memory_layer("managed", LayerUpdateRequest(content="平台策略更新"), admin))
    assert res["success"]
    with pytest.raises(HTTPException) as ei:
        asyncio.run(update_memory_layer("managed", LayerUpdateRequest(content="越权"), member))
    assert ei.value.status_code == 403
    # project：无 kb_id 拒绝；敏感内容拒绝
    with pytest.raises(HTTPException) as ei:
        asyncio.run(update_memory_layer("project", LayerUpdateRequest(content="规则"), admin))
    assert ei.value.status_code == 403
    with pytest.raises(HTTPException) as ei:
        asyncio.run(update_memory_layer("local", LayerUpdateRequest(content="我的密码是 abc", kb_id="kb1"), member))
    assert ei.value.status_code == 400
    # user 层委托 user_instructions（500 字预算同源）
    res = asyncio.run(update_memory_layer("user", LayerUpdateRequest(content="养" * 800), member))
    assert len(res["content"]) == user_instructions.MAX_INSTRUCTION_CHARS
    # auto 层不开放直编
    with pytest.raises(HTTPException) as ei:
        asyncio.run(update_memory_layer("auto", LayerUpdateRequest(content="x"), admin))
    assert ei.value.status_code == 400
    # 概览接口：writable 按角色区分
    got = asyncio.run(my_memory_layers("", admin))
    assert got["writable"]["managed"] is True
    got = asyncio.run(my_memory_layers("", member))
    assert got["writable"]["managed"] is False


# ============================================
# SessionMemory 持久载体（压缩链第三级读写往返）
# ============================================

def test_session_memory_roundtrip(file_db):
    meta = {"covered_until_message_id": "m3", "compact_kind": "auto",
            "edited_by_user": 0, "updated_at": "2026-01-01T00:00:00", "before_tokens": 1200}
    memory_store.write_session_memory("s1", "u1", "1.意图：测试摘要", meta)
    got = memory_store.read_session_memory("s1")
    assert got["summary"] == "1.意图：测试摘要"
    assert got["covered_until_message_id"] == "m3" and got["compact_kind"] == "auto"
    assert memory_store.read_session_memory("s-none") is None