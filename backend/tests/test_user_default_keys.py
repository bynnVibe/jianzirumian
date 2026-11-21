"""用户默认密钥生命周期：注册同事务创建、存量迁移回填（不重签）、删除联动"""
import time

import pytest

from app.core import db
from app.core import attribution_schema
from app.services import auth
from app.services.gateway import keys as key_svc
from app.services.gateway import settings as settings_svc


@pytest.fixture(autouse=True)
def isolate_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(db, "_conn", None)
    auth._active_sessions.clear()
    yield
    if db._conn is not None:
        db._conn.close()
    auth._active_sessions.clear()


def _insert_legacy_user(uid, username):
    now = time.time()
    db.execute(
        "INSERT INTO users (id, username, password_hash, salt, hash_algo, contact,"
        " role, is_active, created_at, updated_at, last_login_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (uid, username, "x", "s", "pbkdf2", f"{username}@test.com", "user", 1, now, now, None),
    )


def _rerun_default_key_migration():
    """模拟存量库升级：清除版本门控后重跑迁移（其余版本块已应用不受影响）"""
    db.execute("DELETE FROM schema_migrations WHERE version='user_default_keys_v1'")
    db.execute("DROP TABLE IF EXISTS user_default_keys")
    with db.transaction() as conn:
        attribution_schema.migrate(conn)


def test_register_creates_default_key_same_transaction():
    u = auth.register("alice", "pass123", "alice@test.com")
    kid = key_svc.get_default_key_id(u["id"])
    assert kid
    info = key_svc.get_key(kid)
    assert info["key_type"] == "user"
    assert info["bound_users"] == [u["id"]]
    # 出网明文已登记，解析结果即该默认密钥
    plain = settings_svc.get_outbound_keys().get(kid)
    assert plain
    assert key_svc.resolve_outbound_key(u["id"]) == (plain, "user")


def test_register_duplicate_leaves_no_orphan_key():
    auth.register("alice", "pass123", "alice@test.com")
    before = len(key_svc.list_keys())
    with pytest.raises(ValueError):
        auth.register("alice", "other", "other@test.com")
    assert len(key_svc.list_keys()) == before
    assert db.query_one("SELECT COUNT(*) AS n FROM user_default_keys")["n"] == 1


def test_ensure_default_key_idempotent():
    u = auth.register("alice", "pass123", "alice@test.com")
    first = key_svc.ensure_user_default_key(u["id"], "alice")
    before = len(key_svc.list_keys())
    second = key_svc.ensure_user_default_key(u["id"], "alice")
    assert first["id"] == second["id"]
    assert len(key_svc.list_keys()) == before


def test_backfill_single_multi_and_never_bound():
    # 存量用户与绑定密钥（迁移前旧链路创建，无默认映射）
    _insert_legacy_user("u_single", "single")
    _insert_legacy_user("u_multi", "multi")
    _insert_legacy_user("u_none", "none")
    k1 = key_svc.create_key("单一绑定", key_type="user", bound_users=["u_single"])
    k2 = key_svc.create_key("多绑定较早", key_type="user", bound_users=["u_multi"])
    key_svc.create_key("多绑定较晚", key_type="user", bound_users=["u_multi"])
    hash_before = {k1["id"]: db.query_one("SELECT key_hash FROM gateway_keys WHERE id=?", (k1["id"],))["key_hash"],
                   k2["id"]: db.query_one("SELECT key_hash FROM gateway_keys WHERE id=?", (k2["id"],))["key_hash"]}
    plain_before = dict(settings_svc.get_outbound_keys())

    _rerun_default_key_migration()

    assert key_svc.get_default_key_id("u_single") == k1["id"]
    assert key_svc.get_default_key_id("u_multi") == k2["id"]  # 多绑定沿用最早有效
    new_kid = key_svc.get_default_key_id("u_none")
    assert new_kid and new_kid not in (k1["id"], k2["id"])
    assert key_svc.get_key(new_kid)["bound_users"] == ["u_none"]
    # 不重签现有密钥：key_hash 与出网明文保持不变
    for kid, h in hash_before.items():
        assert db.query_one("SELECT key_hash FROM gateway_keys WHERE id=?", (kid,))["key_hash"] == h
    after = settings_svc.get_outbound_keys()
    for kid, plain in plain_before.items():
        assert after.get(kid) == plain


def test_backfill_skips_disabled_and_expired_bindings():
    _insert_legacy_user("u_dis", "disabled")
    _insert_legacy_user("u_exp", "expired")
    dis = key_svc.create_key("已禁用", key_type="user", bound_users=["u_dis"])
    key_svc.update_key(dis["id"], {"enabled": False})
    exp = key_svc.create_key("已过期", key_type="user", bound_users=["u_exp"],
                             expires_at="2000-01-01T00:00:00")

    _rerun_default_key_migration()

    # 禁用/过期绑定不可沿用 → 各自新建独立默认密钥
    for uid, old in (("u_dis", dis["id"]), ("u_exp", exp["id"])):
        kid = key_svc.get_default_key_id(uid)
        assert kid and kid != old
        assert key_svc.get_key(kid)["enabled"]


def test_delete_user_unbinds_and_disables_keys():
    admin = auth.register("admin", "pass123", "admin@test.com")
    bob = auth.register("bob", "pass456", "bob@test.com")
    token, _ = auth.login("admin", "pass123")
    shared = key_svc.create_key("共享密钥", key_type="user",
                                bound_users=[admin["id"], bob["id"]])
    bob_default = key_svc.get_default_key_id(bob["id"])

    auth.delete_user(token, bob["id"])

    assert key_svc.get_default_key_id(bob["id"]) == ""
    # 专属默认密钥：无剩余绑定 → 禁用并回收出网明文（保留行供历史追溯）
    info = key_svc.get_key(bob_default)
    assert info and not info["enabled"]
    assert bob_default not in settings_svc.get_outbound_keys()
    # 共享密钥：仍有管理员绑定 → 仅解绑 bob，保持启用
    shared_info = key_svc.get_key(shared["id"])
    assert shared_info["enabled"]
    assert shared_info["bound_users"] == [admin["id"]]


def test_legacy_user_without_binding_gets_key_on_login_era_migration():
    # 旧格式存量用户（迁移回填覆盖所有 users 行，与哈希格式无关）
    _insert_legacy_user("legacy-1", "legacy")
    _rerun_default_key_migration()
    kid = key_svc.get_default_key_id("legacy-1")
    assert kid
    plain, kt = key_svc.resolve_outbound_key("legacy-1")
    assert kt == "user" and plain == settings_svc.get_outbound_keys()[kid]