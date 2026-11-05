"""重复入库校验（来源指纹）离线回归。

覆盖：文件/URL 指纹计算的稳定性与规范化、同库拦截、跨库放行、幂等登记、
上传预警、事务内 conn 变体（URL 确认入库路径）、删除注销后可重新入库。
全部用例仅操作隔离 SQLite 与临时文件，不触达网络/向量库/LLM。
"""
import uuid

import pytest

from app.core import db
from app.services import ingest_dedup


@pytest.fixture()
def tmp_file(tmp_path):
    p = tmp_path / "sample.txt"
    p.write_bytes("见字如面-重复入库校验样例内容".encode("utf-8"))
    return str(p)


def _kb():
    return f"kb-{uuid.uuid4().hex[:8]}"


# ============================================
# 一、指纹计算
# ============================================

def test_file_fingerprint_content_based_and_rename_invariant(tmp_path):
    a = tmp_path / "a.txt"; a.write_bytes(b"same-bytes")
    b = tmp_path / "renamed.txt"; b.write_bytes(b"same-bytes")
    c = tmp_path / "c.txt"; c.write_bytes(b"other-bytes")
    fa = ingest_dedup.compute_file_fingerprint(str(a))
    fb = ingest_dedup.compute_file_fingerprint(str(b))
    fc = ingest_dedup.compute_file_fingerprint(str(c))
    # 与文件名无关，只看内容；不同内容指纹不同
    assert fa == fb != fc
    # 与内存字节指纹一致（批量上传路径复用）
    assert fa == ingest_dedup.compute_bytes_fingerprint(b"same-bytes")
    # 缺失文件返回空串而非抛异常（不阻断入库主流程）
    assert ingest_dedup.compute_file_fingerprint(str(tmp_path / "nope")) == ""


def test_url_fingerprint_normalizes_case_scheme_and_fragment():
    base = ingest_dedup.compute_url_fingerprint("https://example.com/a")
    # 大小写主机/协议、片段差异规范化后一致
    assert base == ingest_dedup.compute_url_fingerprint("HTTPS://Example.COM/a#frag")
    # 根路径补斜杠后一致
    assert (ingest_dedup.compute_url_fingerprint("http://example.com")
            == ingest_dedup.compute_url_fingerprint("http://example.com/"))
    assert ingest_dedup.compute_url_fingerprint("") == ""


# ============================================
# 二、入口校验：同库拦截 / 跨库放行 / 注销后可重入
# ============================================

def test_same_kb_blocks_cross_kb_allowed(tmp_file):
    fp = ingest_dedup.compute_file_fingerprint(tmp_file)
    kb1, kb2 = _kb(), _kb()
    rec1 = f"rec-{uuid.uuid4().hex[:8]}"
    try:
        ingest_dedup.register(fp, ingest_dedup.KIND_FILE, kb1, source_type="image",
                              title="样例", owner_id="u1", record_id=rec1, source_ref=tmp_file)
        # 同库重复：入口拦截
        with pytest.raises(ingest_dedup.DuplicateIngestError) as exc:
            ingest_dedup.ensure_can_ingest(fp, kb1, ingest_dedup.KIND_FILE, "样例")
        assert "重复入库" in str(exc.value)
        # 跨库：放行（同一公共资料归入不同主题库合法）
        ingest_dedup.ensure_can_ingest(fp, kb2, ingest_dedup.KIND_FILE, "样例")
    finally:
        db.execute("DELETE FROM ingest_fingerprints WHERE fingerprint = ?", (fp,))


def test_register_idempotent_and_hint(tmp_file):
    fp = ingest_dedup.compute_file_fingerprint(tmp_file)
    kb = _kb()
    rec = f"rec-{uuid.uuid4().hex[:8]}"
    try:
        ingest_dedup.register(fp, ingest_dedup.KIND_FILE, kb, title="样例",
                              record_id=rec, source_ref=tmp_file)
        # 幂等：同 (fingerprint, kb_id) 再登记不产生第二行（UNIQUE 索引兜底）
        ingest_dedup.register(fp, ingest_dedup.KIND_FILE, kb, title="样例",
                              record_id=rec, source_ref=tmp_file)
        assert len([r for r in ingest_dedup.find(fp) if r["kb_id"] == kb]) == 1
        # 上传预警：found + same_kb 标记
        hint = ingest_dedup.duplicate_hint(fp, kb)
        assert hint["found"] is True and hint["same_kb"] is True and hint["message"]
        # 未指定 kb 时仅作跨库预警（same_kb=False）
        assert ingest_dedup.duplicate_hint(fp)["same_kb"] is False
        # 无指纹来源：found=False
        assert ingest_dedup.duplicate_hint("")["found"] is False
    finally:
        db.execute("DELETE FROM ingest_fingerprints WHERE fingerprint = ?", (fp,))


def test_unregister_allows_reingest(tmp_file):
    fp = ingest_dedup.compute_file_fingerprint(tmp_file)
    kb = _kb()
    rec = f"rec-{uuid.uuid4().hex[:8]}"
    try:
        ingest_dedup.register(fp, ingest_dedup.KIND_FILE, kb, record_id=rec, source_ref=tmp_file)
        with pytest.raises(ingest_dedup.DuplicateIngestError):
            ingest_dedup.ensure_can_ingest(fp, kb)
        # 来源删除 → 注销指纹 → 可重新入库（不被误拦）
        assert ingest_dedup.unregister_by_record(rec) == 1
        ingest_dedup.ensure_can_ingest(fp, kb)  # 不抛
    finally:
        db.execute("DELETE FROM ingest_fingerprints WHERE fingerprint = ?", (fp,))


def test_transaction_conn_variant(tmp_file):
    """URL 确认入库在 db.transaction() 内校验：conn 变体不得重入全局锁。"""
    fp = ingest_dedup.compute_file_fingerprint(tmp_file)
    kb = _kb()
    try:
        with db.transaction() as conn:
            assert ingest_dedup.find_in_kb(fp, kb, conn=conn) is None
            ingest_dedup.register(fp, ingest_dedup.KIND_FILE, kb, title="事务登记", conn=conn)
            assert ingest_dedup.find_in_kb(fp, kb, conn=conn) is not None
        # 事务提交后顶层查询可见
        assert ingest_dedup.find_in_kb(fp, kb) is not None
    finally:
        db.execute("DELETE FROM ingest_fingerprints WHERE fingerprint = ?", (fp,))
