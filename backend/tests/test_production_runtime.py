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
from app.services.gateway import keys, rules, proxy, upstreams, logs, settings as gw_settings


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


@pytest.fixture
def embedding_store(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from app.core import embeddings as engine
    from app.core.vector_store import VectorStore
    from app.services.config_manager import runtime_config
    cfg = {"base_url": "https://workspace.invalid/compatible-mode/v1",
           "api_key": "synthetic-secret", "model": "text-embedding-v4"}
    monkeypatch.setattr(runtime_config, "_file", tmp_path / "runtime.json")
    monkeypatch.setattr(runtime_config, "_data", {
        "embedding_provider": "custom", "provider_settings": {"embedding": {"custom": cfg}},
    })
    monkeypatch.setattr(engine.settings, "EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setattr(engine.settings, "VECTOR_STORE_PATH", str(tmp_path / "index"))
    client = Mock()
    client.embed_query.return_value = [1.0, 0.0, 0.0, 0.0]
    client.embed_documents.side_effect = lambda texts: [[1.0, 0.0, 0.0, 0.0] for _ in texts]
    factory = Mock(return_value=client)
    monkeypatch.setattr(engine.EmbeddingFactory, "create", factory)
    return VectorStore(), client, factory


def _existing_embedding_index(store):
    from app.core.vector_store import FAISS
    store._store = FAISS.from_embeddings([("合成知识", [1.0, 0.0, 0.0, 0.0])], store.embeddings)
    store._store.save_local(store.index_path)
    return {p.name: p.read_bytes() for p in Path(store.index_path).iterdir()}


def test_environment_file_is_anchored_to_backend_not_working_directory(tmp_path, monkeypatch):
    import importlib.util
    from unittest.mock import Mock
    from app import config
    import dotenv
    loader = Mock(return_value=False)
    monkeypatch.setattr(dotenv, "load_dotenv", loader)
    monkeypatch.setattr(config.os, "makedirs", lambda *a, **kw: None)
    monkeypatch.chdir(tmp_path)
    spec = importlib.util.spec_from_file_location("isolated_config_location", config.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    loader.assert_called_once_with(Path(config.__file__).resolve().parent.parent / ".env")


def test_embedding_factory_remote_runtime_override_and_bounded_requests(monkeypatch):
    from unittest.mock import Mock
    from app.core import embeddings as engine
    from app.services.config_manager import runtime_config
    cfg = {"base_url": "https://workspace.invalid/v1", "api_key": "synthetic-key", "model": "text-embedding-v4"}
    monkeypatch.setattr(runtime_config, "_data", {
        "embedding_provider": "custom", "provider_settings": {"embedding": {"custom": cfg}},
    })
    monkeypatch.setattr(engine, "_instance_cache", {})
    remote, local = Mock(), Mock()
    monkeypatch.setattr(engine, "OpenAIEmbeddings", remote)
    monkeypatch.setattr(engine, "OllamaEmbeddings", local)
    first = engine.EmbeddingFactory.create()
    assert engine.EmbeddingFactory.create() is first
    remote.assert_called_once_with(**cfg, check_embedding_ctx_length=False,
                                   request_timeout=engine.settings.EMBEDDING_REQUEST_TIMEOUT,
                                   max_retries=0, chunk_size=10)
    local.assert_not_called()


@pytest.mark.parametrize("existing", [False, True])
def test_embedding_probe_always_requests_model_and_never_writes(embedding_store, existing):
    store, client, factory = embedding_store
    before = _existing_embedding_index(store) if existing else {}
    store._store = None
    assert store.get_document_count() == int(existing)
    assert store.embedding_status()["status"] == "unknown"
    assert store.embedding_status()["available"] is None
    client.embed_query.assert_not_called()
    for _ in range(2):
        result = store.probe_embeddings()
        assert result["available"] and result["dimension"] == 4
        assert result["provider"] == "custom" and result["model"] == "text-embedding-v4"
        assert result["index_compatible"] is (True if existing else None)
    assert client.embed_query.call_count == 2
    client.embed_documents.assert_not_called()
    assert factory.call_args.args == ("custom",)
    assert "synthetic-secret" not in str(result)
    if existing:
        assert before == {p.name: p.read_bytes() for p in Path(store.index_path).iterdir()}
    else:
        assert not Path(store.index_path).exists()
        assert store._store is None


@pytest.mark.parametrize("status,detail,expected", [
    (401, "bad key synthetic-secret", "鉴权失败"),
    (403, "denied synthetic-secret", "访问被拒绝"),
    (403, "insufficient_quota synthetic-secret", "额度不足"),
    (429, "rate limit", "限流"),
    (404, "model not found", "模型配置"),
])
def test_embedding_upstream_failure_is_not_local_index_success(embedding_store, status, detail, expected):
    from openai import APIStatusError
    store, client, _ = embedding_store
    before = _existing_embedding_index(store)
    client.embed_query.side_effect = APIStatusError(
        detail, response=httpx.Response(status, request=httpx.Request("POST", "https://test.invalid")),
        body={"message": detail},
    )
    result = store.probe_embeddings()
    assert not result["available"] and not result["connected"]
    assert expected in result["message"] and "synthetic-secret" not in str(result)
    assert store.get_document_count() == 1
    assert before == {p.name: p.read_bytes() for p in Path(store.index_path).iterdir()}
    client.embed_query.side_effect = None
    assert store.probe_embeddings()["available"]


@pytest.mark.parametrize("vector", [[], [float("nan")] * 4, [float("inf")] * 4, [0.0] * 4, [1.0, 0.0]])
def test_embedding_invalid_vector_or_dimension_never_changes_index(embedding_store, vector):
    store, client, _ = embedding_store
    before = _existing_embedding_index(store)
    client.embed_query.return_value = vector
    result = store.probe_embeddings()
    assert result["status"] == "error" and not result["available"]
    if len(vector) == 2:
        assert result["connected"] and result["index_compatible"] is False
        assert "已有索引为 4 维" in result["message"]
    assert before == {p.name: p.read_bytes() for p in Path(store.index_path).iterdir()}


def test_embedding_network_save_is_rejected_migrated_to_gateway(embedding_store, monkeypatch):
    """WS3「网关唯一来源」：embedding 网络 provider 连接配置写入被拒（410），改由网关上游管理。"""
    from fastapi import HTTPException
    from app.api import config_api
    store, client, factory = embedding_store
    before = _existing_embedding_index(store)
    original_store = store._store
    old_embeddings = store.embeddings
    monkeypatch.setattr(config_api, "vector_store", store)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(config_api.save_provider_settings(config_api.ProviderSettingsBody(
            category="embedding", provider="custom", values={"model": "replacement-model"},
        ), {}))
    assert exc.value.status_code == 410
    # 写入被拒：未重绑客户端、未请求模型、未改写索引
    assert store.embeddings is old_embeddings
    assert store._store is original_store
    client.embed_documents.assert_not_called()
    assert before == {p.name: p.read_bytes() for p in Path(store.index_path).iterdir()}


def test_embedding_provider_switch_rebinds_store_and_business_uses_new_client(embedding_store, monkeypatch):
    """切换 embedding provider 仍重绑 store（不请求模型、不改写索引），业务使用新客户端。"""
    from app.api import config_api
    store, client, factory = embedding_store
    _existing_embedding_index(store)
    original_store = store._store
    assert store.probe_embeddings()["available"]
    old_embeddings = store.embeddings
    monkeypatch.setattr(config_api, "vector_store", store)
    response = asyncio.run(config_api.switch_provider(config_api.ProviderSwitch(
        provider_type="embedding", value="custom"), {}))
    assert response["success"]
    assert store._store is original_store
    assert store.embeddings is not old_embeddings
    assert original_store.embedding_function is store.embeddings
    assert store.embedding_status()["status"] == "unknown"
    client.embed_documents.assert_not_called()
    # 检索与写入都会调用新配置，不需要清空或重新加载索引。
    store.search_broad("合成问题", k=1)
    assert factory.call_args.args == ("custom",)
    store.add_texts(["新合成知识"], [{"visibility": "public"}])
    assert store.get_document_count() == 2 and store.is_available()
    client.embed_documents.assert_called_once()


def test_embedding_configuration_read_is_offline_and_status_is_unknown(embedding_store, monkeypatch):
    from app.api import config_api
    store, client, _ = embedding_store
    monkeypatch.setattr(config_api, "vector_store", store)
    result = asyncio.run(config_api.get_full_config())["embedding"]
    assert result["available"] is None and result["status"] == "unknown"
    assert result["model"] == "text-embedding-v4" and result["has_key"]
    client.embed_query.assert_not_called()
    client.embed_documents.assert_not_called()
    assert not Path(store.index_path).exists()


def test_embedding_stale_probe_cannot_mark_new_configuration_available(embedding_store):
    store, client, _ = embedding_store
    def switch_during_request(text):
        store.reload_embeddings()
        return [1.0, 0.0, 0.0, 0.0]
    client.embed_query.side_effect = switch_during_request
    result = store.probe_embeddings()
    assert not result["available"] and "配置已变更" in result["message"]
    assert store.embedding_status()["status"] == "unknown"


def test_embedding_endpoint_runs_network_off_event_loop(embedding_store, monkeypatch):
    import threading
    from app.api import config_api
    store, client, _ = embedding_store
    monkeypatch.setattr(config_api, "vector_store", store)
    entered, release = threading.Event(), threading.Event()
    def delayed(text):
        entered.set()
        assert release.wait(2), "网络请求阻塞了事件循环"
        return [1.0, 0.0, 0.0, 0.0]
    client.embed_query.side_effect = delayed
    async def run():
        task = asyncio.create_task(config_api.test_embedding_connection({}))
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(.01)
        release.set()
        response = await task
        assert response.success and response.embedding["dimension"] == 4
    asyncio.run(run())


def test_embedding_real_openai_protocol_uses_remote_model_and_batches(monkeypatch):
    import json
    from app.core import embeddings as engine
    calls = []
    def respond(request):
        payload = json.loads(request.content)
        calls.append((str(request.url), payload))
        assert request.headers["authorization"] == "Bearer synthetic-key"
        return httpx.Response(200, json={
            "object": "list", "model": "text-embedding-v4",
            "data": [{"object": "embedding", "index": i, "embedding": [1.0, 0.0, 0.0, 0.0]}
                     for i in range(len(payload["input"]))],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        })
    transport = httpx.MockTransport(respond)
    original = engine.OpenAIEmbeddings
    with httpx.Client(transport=transport) as http_client:
        monkeypatch.setattr(engine, "OpenAIEmbeddings", lambda **kw: original(http_client=http_client, **kw))
        monkeypatch.setattr(engine, "_instance_cache", {})
        client = engine.EmbeddingFactory.create("custom", config={
            "base_url": "https://workspace.invalid/compatible-mode/v1", "api_key": "synthetic-key",
            "model": "text-embedding-v4",
        })
        assert len(client.embed_documents(["合成文本"] * 11)) == 11
        assert len(client.embed_query("合成查询")) == 4
    assert [len(payload["input"]) for _, payload in calls] == [10, 1, 1]
    assert all(url == "https://workspace.invalid/compatible-mode/v1/embeddings" for url, _ in calls)
    assert all(payload["model"] == "text-embedding-v4" for _, payload in calls)


@pytest.mark.parametrize("enabled,fails", [(False, False), (True, False), (True, True)])
def test_embedding_startup_probe_reports_effective_remote_configuration(embedding_store, monkeypatch, capsys, enabled, fails):
    from unittest.mock import Mock
    from app import main
    from app.core import embeddings as engine
    from app.services import kb, upload_pipeline, wiki_store, wiki
    store, client, _ = embedding_store
    monkeypatch.setattr(main, "vector_store", store)
    monkeypatch.setattr(main.settings, "EMBEDDING_STARTUP_PROBE", enabled)
    monkeypatch.setattr(main, "runtime", Mock())
    monkeypatch.setattr(main, "run_migration", lambda: None)
    monkeypatch.setattr(kb, "patch_legacy_metadata", lambda: None)
    monkeypatch.setattr(upload_pipeline.upload_pipeline, "recover", lambda: None)
    monkeypatch.setattr(wiki_store, "ensure_layout", lambda: None)
    monkeypatch.setattr(wiki_store, "backfill_raw", lambda: None)
    monkeypatch.setattr(wiki.wiki_service, "recover_queue", lambda: 0)
    # 启动探测已改为「直连上游 /embeddings」（不经同进程网关自调用，避免 lifespan 未受理入站请求而挂到超时），
    # 故用合成探针替换真实 HTTP；探针内部已对密钥脱敏，返回消息不含明文 secret。
    probe = Mock(return_value=(
        {"ok": False, "dimension": None, "status_code": None,
         "message": "Embedding 请求超时，请检查远程服务及网络连接", "elapsed": 0.1}
        if fails else
        {"ok": True, "dimension": 4, "status_code": 200,
         "message": "text-embedding-v4 连接成功，返回 4 维向量", "elapsed": 0.1}
    ))
    monkeypatch.setattr(engine, "probe_upstream_direct", probe)
    async def run():
        async with main.lifespan(main.app):
            assert main.runtime.ready
            health = await main.health_check()
            expected = "unavailable" if fails else "available" if enabled else "unknown"
            assert health["embedding"] == expected
            assert health["embedding_model"] == "text-embedding-v4"
            assert health["embedding_mode"] == "remote"
    asyncio.run(run())
    assert probe.call_count == int(enabled)
    output = capsys.readouterr().out
    assert "远程 API / custom / text-embedding-v4" in output
    assert "synthetic-secret" not in output
    assert "未探测 Embedding" not in output


def test_embedding_endpoint_reports_timeout_with_existing_index(embedding_store, monkeypatch):
    from app.api import config_api
    store, client, _ = embedding_store
    _existing_embedding_index(store)
    monkeypatch.setattr(config_api, "vector_store", store)
    client.embed_query.side_effect = httpx.ReadTimeout("synthetic-secret")
    response = asyncio.run(config_api.test_embedding_connection({}))
    assert not response.success
    assert "超时" in response.message and "synthetic-secret" not in response.message
    assert response.embedding["index_loaded"]


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
    up = upstreams.create_upstream({"name": "mock", "base_url": "https://mock.invalid/v1",
                                    "models": ["demo"], "tier": "paid"})
    rules.create_rule({"model_pattern": "demo", "upstreams": [up["id"]]})
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


def test_log_context_isolated_and_copied_to_thread():
    import logging
    from app.core.log_setup import UsernameFilter, current_username

    def snapshot():
        record = logging.LogRecord("app.test", logging.INFO, "", 0, "测试", (), None)
        UsernameFilter().filter(record)
        return record.trace_id, record.username

    async def worker(user):
        token = current_username.set(user)
        tid = observability.start_trace("test", user)
        try:
            await asyncio.sleep(0)
            assert snapshot() == (tid, user)
            assert await asyncio.to_thread(snapshot) == (tid, user)
        finally:
            observability.end_trace()
            current_username.reset(token)
        assert snapshot()[0] == "-"
        return tid

    async def run():
        first, second = await asyncio.gather(worker("alice"), worker("bob"))
        assert first != second
    asyncio.run(run())


def test_daily_handler_trace_logs_roundtrip(tmp_path, monkeypatch):
    import logging
    import sys
    from app.core import log_setup
    monkeypatch.setattr(log_setup, "LOG_DIR", tmp_path)
    handler = log_setup.DailyFileHandler(tmp_path)
    handler.setFormatter(logging.Formatter(log_setup.LOG_FORMAT, datefmt=log_setup.DATE_FORMAT))
    handler.addFilter(log_setup.UsernameFilter())
    tid = observability.start_trace("test")
    try:
        try:
            raise ValueError("测试堆栈")
        except ValueError:
            record = logging.LogRecord("app.chat", logging.ERROR, "", 0,
                                       "API Key: sample-credential", (), sys.exc_info())
            handler.handle(record)
    finally:
        observability.end_trace()
        handler.close()
    result = observability.get_trace_logs(tid)
    assert len(result["entries"]) == 1
    text = result["entries"][0]["text"]
    assert tid in text and "Traceback" in text and "ValueError" in text
    assert "sample-credential" not in text
    assert "REDACTED" in text


def test_trace_log_pagination_exact_match_multiline_and_midnight(tmp_path, monkeypatch):
    from app.core import log_setup
    monkeypatch.setattr(log_setup, "LOG_DIR", tmp_path)
    tid = "11111111-1111-4111-8111-111111111111"
    other = "22222222-2222-4222-8222-222222222222"
    trace = {"id": tid, "started_at": "2026-10-01T23:59:58", "ended_at": "2026-10-02T00:00:03"}
    prefix = "[alice] 2026-10-01 23:59:59 [INFO] "
    (tmp_path / "2026-10-01.log").write_text(
        prefix + f"[trace={other}] app.chat: 用户提到了 trace={tid}\n"
        + prefix + f"[trace={tid}] app.chat: 当前请求报错\nTraceback (most recent call last):\nValueError: 测试异常\n"
        + prefix + f"app.chat: 不可关联 trace={tid}\n"
        + prefix + f"jianziruyang.agent: [AGENT] trace={tid} step=旧日志\n",
        encoding="utf-8",
    )
    (tmp_path / "2026-10-02.log").write_text(
        f"[alice] 2026-10-02 00:00:01 [INFO] [trace={tid}] app.chat: 完成\n", encoding="utf-8",
    )
    entries, cursor = [], ""
    for _ in range(8):
        page = log_setup.read_trace_logs(trace, cursor, limit=1)
        entries.extend(page["entries"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert cursor is None
    assert len(entries) == 3
    assert len({entry["id"] for entry in entries}) == 3
    text = "".join(entry["text"] for entry in entries)
    assert "Traceback" in text and "ValueError" in text and "旧日志" in text and "完成" in text
    assert "用户提到了" not in text and "不可关联" not in text
    assert entries[-1]["source"] == "2026-10-02.log"


@pytest.mark.parametrize("secret", [
    'Authorization: Bearer sample-credential',
    '{"api_key": "sample-credential"}',
    "password='sample-credential'",
    'https://service.invalid/?access_token=sample-credential&ok=1',
    'redis://user:sample-credential@host:6379/0',
    'Cookie: session=sample-credential; user=sample-credential',
    'Authorization: Basic sample-credential',
    'sk-sample-credential',
])
def test_trace_log_credentials_redacted(secret):
    from app.core.log_setup import redact_log_text
    assert "sample-credential" not in redact_log_text(secret)
    assert "REDACTED" in redact_log_text(secret)


def test_trace_log_limits_missing_files_and_bad_cursor(tmp_path, monkeypatch):
    from app.core import log_setup
    monkeypatch.setattr(log_setup, "LOG_DIR", tmp_path)
    tid = "11111111-1111-4111-8111-111111111111"
    trace = {"id": tid, "started_at": "2026-10-01", "ended_at": "2026-10-01"}
    assert log_setup.read_trace_logs(trace)["warnings"]
    with pytest.raises(ValueError):
        log_setup.read_trace_logs(trace, "../../.env")
    with pytest.raises(ValueError):
        log_setup.read_trace_logs(trace, "2026-10-02:0")
    path = tmp_path / "2026-10-01.log"
    header = f"[-] 2026-10-01 12:00:00 [INFO] [trace={tid}] app.chat: "
    path.write_text(header + "a" * 220000 + "\n" + header + "结束\n", encoding="utf-8")
    page = log_setup.read_trace_logs(trace)
    assert len(page["entries"]) == 2
    assert page["entries"][0]["truncated"]
    assert "已截断" in page["entries"][0]["text"]
    assert "结束" in page["entries"][1]["text"]
    monkeypatch.setattr(log_setup, "_SCAN_BYTES", 10)
    assert log_setup.read_trace_logs(trace)["next_cursor"]


def test_trace_log_endpoint_authorization_and_errors(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from app.api import observability as api, deps
    from app.core import log_setup
    monkeypatch.setattr(log_setup, "LOG_DIR", tmp_path)
    tid = observability.start_trace("test")
    observability.end_trace()
    app = FastAPI()
    app.include_router(api.router)
    monkeypatch.setattr(deps.auth_service, "get_session_user", lambda token: {"id": token, "role": token})

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            path = f"/api/observability/traces/{tid}/logs"
            assert (await client.get(path)).status_code == 401
            assert (await client.get(path, headers={"Authorization": "Bearer user"})).status_code == 403
            for role in ("admin", "ops"):
                response = await client.get(path, headers={"Authorization": f"Bearer {role}"})
                assert response.status_code == 200
                assert response.headers["cache-control"] == "no-store"
                assert response.json()["trace_id"] == tid
                assert response.json()["warnings"]
            headers = {"Authorization": "Bearer ops"}
            assert (await client.get(path, headers=headers, params={"cursor": "bad"})).status_code == 400
            assert (await client.get(path, headers=headers, params={"limit": 1000})).status_code == 422
            missing = "/api/observability/traces/00000000-0000-4000-8000-000000000000/logs"
            assert (await client.get(missing, headers=headers)).status_code == 404
            assert (await client.get("/api/observability/traces/not-a-uuid/logs", headers=headers)).status_code == 422
    asyncio.run(run())
