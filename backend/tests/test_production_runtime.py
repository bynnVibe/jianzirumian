"""A＋B 运行不变量；配合 run_isolated.py 全程使用临时数据和 Mock。"""
import asyncio
import multiprocessing
import time
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from app.core import db, metric_counters, observability, metrics_export
from app.core.agent_runtime import AgentRuntime
from app.services.gateway import keys, channels, proxy, logs, settings as gw_settings


@pytest.fixture(autouse=True)
def temporary_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(db, "_conn", None)
    conn = db.get_conn()
    yield
    if db._conn is not None and db._conn is not conn:
        db._conn.close()
    conn.close()


def _worker(database, operation, pipe, gate=None):
    if db._conn:
        db._conn.close()
    db._conn = None
    db.DB_PATH = Path(database)
    if gate:
        gate.wait(10)
    try:
        if operation == "init":
            db.get_conn()
            result = "initialized"
        elif operation == "lock":
            runtime = AgentRuntime()
            runtime.acquire()
            result = "owner"
        else:
            keys.consume_request(operation, "demo")
            result = "accepted"
    except keys.KeyRejected as e:
        result = e.code
    except RuntimeError as exc:
        result = "locked" if operation == "lock" else f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        result = f"{type(exc).__name__}: {exc}"
    pipe.send(result)
    if operation == "lock" and result == "owner":
        pipe.recv()
    pipe.close()


@pytest.mark.parametrize("attempt", range(10))
def test_simultaneous_initialization(tmp_path, attempt):
    ctx = multiprocessing.get_context("fork")
    gate = ctx.Barrier(2)
    pairs = [ctx.Pipe() for _ in range(2)]
    database = tmp_path / "fresh.db"
    processes = [ctx.Process(target=_worker, args=(str(database), "init", child, gate)) for _, child in pairs]
    for process in processes:
        process.start()
    try:
        for parent, _ in pairs:
            assert parent.poll(40)
            assert parent.recv() == "initialized"
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(5)
        for parent, child in pairs:
            parent.close()
            child.close()


def test_lock_exclusive_and_released_after_process_exit():
    ctx = multiprocessing.get_context("fork")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_worker, args=(str(db.DB_PATH), "lock", child))
    process.start()
    try:
        assert parent.poll(10) and parent.recv() == "owner"
        with pytest.raises(RuntimeError):
            AgentRuntime().acquire()
    finally:
        parent.send("exit")
        process.join(10)
    second = AgentRuntime()
    second.acquire()
    assert second.owns_process()
    second.stop()
    assert second.owns_process() and not second.ready
    second._lock.release()  # 仅隔离测试显式释放；生产由进程退出释放。


def test_quota_cross_process_exactly_one():
    key = keys.create_key("concurrent", request_quota=1)
    ctx = multiprocessing.get_context("fork")
    gate = ctx.Barrier(2)
    pairs = [ctx.Pipe() for _ in range(2)]
    processes = [ctx.Process(target=_worker, args=(str(db.DB_PATH), key["id"], child, gate)) for _, child in pairs]
    for process in processes:
        process.start()
    results = []
    for parent, _ in pairs:
        assert parent.poll(20)
        results.append(parent.recv())
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    assert sorted(results) == ["accepted", "quota_exceeded"]
    assert keys.get_key(key["id"])["request_count"] == 1


@pytest.mark.parametrize("reason", ["audit", "no_channel", "model"])
def test_rejected_prepare_does_not_consume(reason):
    key = keys.create_key("rejected", allowed_models=["other"] if reason == "model" else [])
    if reason == "audit":
        gw_settings.save_settings({"audit_mode": "block", "blacklist": ["MARKER"]})
    with pytest.raises(proxy.GatewayError):
        proxy.prepare({"model": "demo", "messages": [{"role": "user", "content": "MARKER"}]}, key["key"])
    assert keys.get_key(key["id"])["request_count"] == 0


def test_recovery_only_owner_and_never_database_connection():
    now = datetime.now().isoformat()
    db.execute("INSERT INTO wiki_compile_queue(id,status,created_at,updated_at) VALUES ('w','processing',?,?)", (now, now))
    db.execute("INSERT INTO eval_runs(id,dataset_id,status,started_at) VALUES ('e','d','running',?)", (now,))
    db.execute("INSERT INTO pipeline_runs(id,status,started_at) VALUES ('p','running',?)", (now,))
    db._conn.close()
    db._conn = None
    db.get_conn()
    assert db.query_one("SELECT status FROM wiki_compile_queue")["status"] == "processing"
    runtime = AgentRuntime()
    with pytest.raises(RuntimeError):
        runtime.recover()
    runtime.acquire()
    try:
        runtime.recover()
        assert db.query_one("SELECT status FROM wiki_compile_queue")["status"] == "pending"
        assert db.query_one("SELECT status FROM eval_runs")["status"] == "interrupted"
        assert db.query_one("SELECT status FROM pipeline_runs")["status"] == "aborted"
    finally:
        runtime._lock.release()


def test_persistent_counters_survive_duplicates_purge_and_reconnect():
    tid = observability.start_trace("test")
    observability.record_llm("mock", "问题", "回答")
    observability.end_trace()
    observability.end_trace()
    before = metric_counters.read("agent")
    assert before[("requests", "ok")] == 1
    db.execute("UPDATE agent_traces SET status='ok' WHERE id=?", (tid,))
    db.execute("DELETE FROM agent_traces")
    entry = {"id": "log", "status": "ok", "prompt_tokens": 5, "created_at": "2000-01-01"}
    logs.write_log(entry)
    logs.write_log(entry)
    logs.purge_old()
    db._conn.close()
    db._conn = None
    assert metric_counters.read("agent") == before
    assert metric_counters.read("gateway")[("requests", "ok")] == 1
    assert metric_counters.read("gateway")[("prompt_tokens", "")] == 5
    assert "jzrm_agent_traces_total" not in metrics_export.render_prometheus("llm-proxy")


@pytest.mark.parametrize("existing", [False, True])
def test_startup_index_never_calls_embedding(tmp_path, monkeypatch, existing):
    from app.core.vector_store import VectorStore, FAISS
    store = object.__new__(VectorStore)
    store.index_path, store._store, store.embeddings = str(tmp_path / "index"), None, object()
    loaded = object()
    def unexpected(*args, **kwargs):
        pytest.fail("启动不得创建占位文档或调用 Embedding")
    monkeypatch.setattr(FAISS, "from_texts", unexpected)
    if existing:
        Path(store.index_path).mkdir()
    monkeypatch.setattr(FAISS, "load_local", lambda *a, **kw: loaded)
    assert store.initialize() is existing
    assert store._store is (loaded if existing else None)


def test_corrupt_local_index_blocks_agent_ready(tmp_path, monkeypatch):
    from app.core.vector_store import VectorStore, FAISS
    from app import main
    store = object.__new__(VectorStore)
    store.index_path, store._store, store.embeddings = str(tmp_path / "index"), None, object()
    Path(store.index_path).mkdir()
    def corrupt(*args, **kwargs):
        raise ValueError("合成损坏索引")
    monkeypatch.setattr(FAISS, "load_local", corrupt)
    runtime = AgentRuntime()
    monkeypatch.setattr(main, "runtime", runtime)
    monkeypatch.setattr(main, "vector_store", store)
    monkeypatch.setattr(main, "run_migration", lambda: None)
    async def run():
        with pytest.raises(ValueError, match="损坏索引"):
            async with main.lifespan(main.app):
                pytest.fail("损坏索引不能进入 ready")
    try:
        asyncio.run(run())
        assert not runtime.ready and runtime.owns_process()
    finally:
        runtime._lock.release()


class Stream(httpx.AsyncByteStream):
    def __init__(self, done=False, slow=False):
        self.done, self.slow, self.closed = done, slow, False

    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        if self.slow:
            await asyncio.sleep(1)
        if self.done:
            yield b'data: [DONE]\n\n'

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("mode,status", [("done", "ok"), ("eof", "error"), ("cancel", "cancelled"), ("timeout", "timeout")])
def test_stream_terminal_and_resources(mode, status, monkeypatch):
    channels.create_channel({"name": "mock", "base_url": "https://mock.invalid/v1", "models": ["demo"]})
    key = keys.create_key("stream")
    ctx = proxy.prepare({"model": "demo", "stream": True}, key["key"])
    stream = Stream(mode == "done", mode == "timeout")
    client = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=stream)), **kw))

    async def run():
        generator = proxy.stream_response(ctx)
        if mode == "timeout":
            ctx.deadline = time.monotonic() + 0.03
        if mode == "cancel":
            await anext(generator)
            await generator.aclose()
        else:
            events = [event async for event in generator]
            assert ("[DONE]" in "".join(events)) == (mode == "done")
    asyncio.run(run())
    assert stream.closed
    assert db.query_one("SELECT status FROM gateway_logs")["status"] == status
    assert keys.get_key(key["id"])["request_count"] == 1
    usage = keys.get_key(key["id"])["token_used"]
    proxy._finalize(ctx, status="ok", status_code=200, completion_tokens=100)
    assert keys.get_key(key["id"])["token_used"] == usage
