"""控制面桩测试；不启动容器，不等同真实 Linux 隔离验收。"""
import asyncio
import hashlib
import importlib.util
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.services.sandbox import worker_client as client
from app.services import personal_tool_specs as specs
from app.services.tool_http import ToolExecutionError

ROOT = Path(__file__).resolve().parents[2]
module = importlib.util.spec_from_file_location("isolated_worker_test", ROOT / "sandbox_worker/worker.py")
worker = importlib.util.module_from_spec(module)
module.loader.exec_module(worker)
IMAGE = "example.com/approved/python@sha256:" + "a" * 64
OWNER = "b" * 64
TASK = "c" * 32


def contract():
    code = "def main(data): return {'value': data['n'] * 2}"
    return {"code": code, "code_hash": hashlib.sha256(code.encode()).hexdigest(), "version": "d" * 64,
            "data": {"n": 2}, "profile_hash": worker.digest(worker.profile(IMAGE)), "expires_at": time.time() + 90}


@pytest.fixture(autouse=True)
def seccomp_config(monkeypatch):
    monkeypatch.setenv("SANDBOX_SECCOMP_SHA256", worker.profile(IMAGE)["seccomp_sha256"])


@pytest.fixture
def control(tmp_path, monkeypatch):
    instance = worker.Worker(tmp_path, IMAGE, "synthetic-control-token-not-a-secret", accepted=True)
    instance.open()
    instance.ready = True
    monkeypatch.setattr(instance, "probe", AsyncMock())
    yield instance
    if instance.conn:
        instance.conn.close()
    if instance.lock:
        instance.lock.close()


def test_profile_and_container_flags_match_backend(monkeypatch):
    monkeypatch.setenv("SANDBOX_PYTHON_IMAGE", IMAGE)
    assert client.runtime_profile() == worker.profile(IMAGE)
    assert specs.fingerprint(client.runtime_profile()) == worker.digest(worker.profile(IMAGE))
    args = worker.create_args(IMAGE, "jzpy-" + TASK)
    for required in ("--network=none", "--read-only", "--user=65532:65532", "--cap-drop=ALL",
                     "--security-opt=no-new-privileges", "--memory=256m", "--pids-limit=32", "--cpus=1", "--image-volume=ignore"):
        assert required in args
    assert not any(a.startswith(("--volume", "--mount", "--privileged")) for a in args)
    assert args[-5:] == ["-i", IMAGE, "-I", "-B", "/opt/runner.py"]
    with pytest.raises(ValueError):
        worker.create_args("python:latest", "jzpy-" + TASK)
    seccomp = json.loads((ROOT / "sandbox_worker/seccomp.json").read_text())
    assert seccomp["defaultAction"] == "SCMP_ACT_ERRNO"
    allowed = {name for rule in seccomp["syscalls"] for name in rule["names"]}
    assert not {"socket", "connect", "mount", "ptrace", "bpf", "keyctl", "unshare", "setns"} & allowed
    clone = next(rule for rule in seccomp["syscalls"] if "clone" in rule["names"])
    assert clone["args"][0]["value"] == 0x7e020080
    assert clone["args"][0]["valueTwo"] == 0
    assert next(rule for rule in seccomp["syscalls"] if "clone3" in rule["names"])["errnoRet"] == 38


def test_worker_idempotence_owner_isolation_and_cleanup(control, monkeypatch):
    calls = []
    async def command(*args, **kwargs):
        calls.append(args)
        if args[0] == "container":
            return 1, b"", b""
        return 0, b'{"value":4}' if args[0] == "start" else b"", b""
    monkeypatch.setattr(worker, "command", command)
    async def check():
        body = contract()
        assert control.submit(OWNER, TASK, body)["status"] == "queued"
        assert control.submit(OWNER, TASK, body)["status"] == "queued"
        with pytest.raises(HTTPException):
            control.lookup("e" * 64, TASK)
        with pytest.raises(HTTPException):
            control.submit(OWNER, TASK, {**body, "data": {"n": 3}})
        with pytest.raises(HTTPException):
            control.submit(OWNER, "f" * 32, body)
        await control.tasks[TASK]
        assert control.lookup(OWNER, TASK) == {"id": TASK, "status": "completed", "output": {"value": 4}}
        assert sum(c[0] == "start" for c in calls) == 1
        assert any(c[0] == "rm" for c in calls)
    asyncio.run(check())


def test_cancel_tombstone_blocks_late_submission(control):
    async def check():
        result = await control.cancel(OWNER, TASK)
        assert result["status"] == "cancelled"
        assert control.submit(OWNER, TASK, contract())["status"] == "cancelled"
        assert not control.tasks
    asyncio.run(check())


def test_cancel_running_requires_container_cleanup(control, monkeypatch):
    started = asyncio.Event()
    calls = []
    async def command(*args, **kwargs):
        calls.append(args)
        if args[0] == "start":
            started.set()
            await asyncio.sleep(60)
        return (1 if args[0] == "container" else 0), b"", b""
    monkeypatch.setattr(worker, "command", command)
    async def check():
        control.submit(OWNER, TASK, contract())
        await started.wait()
        result = await control.cancel(OWNER, TASK)
        assert result["status"] == "cancelled"
        assert any(c[0] == "rm" for c in calls)
    asyncio.run(check())


def test_cleanup_failure_closes_execution_gate(control, monkeypatch):
    async def command(*args, **kwargs):
        return 0, b'{"value":4}', b""
    monkeypatch.setattr(worker, "command", command)
    async def check():
        control.submit(OWNER, TASK, contract())
        await control.tasks[TASK]
        assert control.lookup(OWNER, TASK)["status"] == "cleanup_failed"
        assert not control.ready
        with pytest.raises(HTTPException) as caught:
            control.submit("f" * 64, "f" * 32, contract())
        assert caught.value.status_code == 503
    asyncio.run(check())


@pytest.mark.parametrize("change", [{"code_hash": "f" * 64}, {"profile_hash": "f" * 64},
    {"expires_at": 0}, {"data": []}, {"version": "latest"}, {"extra_permission": True}])
def test_contract_tampering_rejected(control, change):
    with pytest.raises(HTTPException):
        control.submit(OWNER, TASK, {**contract(), **change})


def test_missing_runtime_stays_unavailable(tmp_path, monkeypatch):
    control = worker.Worker(tmp_path, IMAGE, "x" * 32, accepted=True)
    monkeypatch.setattr(control, "probe", AsyncMock(side_effect=OSError("no runtime")))
    async def check():
        await control.startup()
        assert not control.ready
        await control.shutdown()
    asyncio.run(check())


def test_backend_default_does_not_execute_locally(monkeypatch):
    monkeypatch.delenv("SANDBOX_PYTHON_ENABLED", raising=False)
    assert not client.configured()
    with pytest.raises(ToolExecutionError, match="sandbox_unavailable"):
        asyncio.run(client.health())


def test_client_validates_output_and_runtime_binding(monkeypatch):
    monkeypatch.setenv("SANDBOX_PYTHON_IMAGE", IMAGE)
    spec = {"config": {"code": contract()["code"]}, "input_schema": specs.object_schema({"n": {"type": "number"}}),
            "output_schema": specs.object_schema({"value": {"type": "number"}})}
    row = {"owner_id": "alice", "version": "d" * 64, "approved_by": "admin", "review_json": json.dumps({"runtime_profile": client.runtime_profile()})}
    calls = []
    async def request(method, path, *args, **kwargs):
        calls.append((method, path))
        if path == "/health":
            return {"available": True, "profile": client.runtime_profile(), "profile_hash": specs.fingerprint(client.runtime_profile())}
        return {"id": TASK, "status": "completed", "output": {"value": 4}}
    monkeypatch.setattr(client, "request", request)
    result = asyncio.run(client.execute(row, spec, {"n": 2}, TASK))
    assert result["output"] == {"value": 4} and result["requirement_status"] == "unverified"
    assert calls[0][1] == "/health"
    row["review_json"] = "{}"
    with pytest.raises(ToolExecutionError, match="admin_review_required"):
        asyncio.run(client.execute(row, spec, {"n": 2}, TASK))


def test_client_cancel_unconfirmed_is_not_reported_terminated(monkeypatch):
    monkeypatch.setenv("SANDBOX_PYTHON_IMAGE", IMAGE)
    monkeypatch.setattr(client, "health", AsyncMock())
    monkeypatch.setattr(client, "cancel", AsyncMock(return_value=False))
    started = asyncio.Event()
    async def request(*args, **kwargs):
        started.set()
        await asyncio.sleep(60)
    monkeypatch.setattr(client, "request", request)
    row = {"owner_id": "alice", "version": "d" * 64, "approved_by": "admin", "review_json": json.dumps({"runtime_profile": client.runtime_profile()})}
    spec = {"config": {"code": contract()["code"]}, "input_schema": specs.object_schema({"n": {"type": "number"}})}
    async def check():
        task = asyncio.create_task(client.execute(row, spec, {"n": 2}, TASK))
        await started.wait()
        task.cancel()
        with pytest.raises(client.CancellationUnconfirmed):
            await task
        client.cancel.assert_awaited_once_with("alice", TASK)
    asyncio.run(check())


def test_concurrent_cancellation_cannot_interrupt_cleanup(control, monkeypatch):
    started, cleaning, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def command(*args, **kwargs):
        if args[0] == "start":
            started.set()
            await asyncio.sleep(60)
        return 0, b"", b""
    async def cleanup(name):
        cleaning.set()
        await finish.wait()
    monkeypatch.setattr(worker, "command", command)
    monkeypatch.setattr(control, "cleanup", cleanup)
    async def check():
        control.submit(OWNER, TASK, contract())
        await started.wait()
        first = asyncio.create_task(control.cancel(OWNER, TASK))
        await cleaning.wait()
        second = asyncio.create_task(control.cancel(OWNER, TASK))
        await asyncio.sleep(0)
        assert not first.done() and not second.done()
        finish.set()
        results = await asyncio.gather(first, second)
        assert all(r["status"] == "cancelled" for r in results)
    asyncio.run(check())


def test_cancel_during_late_creation_cleans_after_creation(control, monkeypatch):
    started, finish = asyncio.Event(), asyncio.Event()
    events = []
    async def command(*args, **kwargs):
        if args[0] == "create":
            started.set()
            await finish.wait()
            events.append("created")
        return 0, b"", b""
    async def cleanup(name):
        events.append("cleaned")
    monkeypatch.setattr(worker, "command", command)
    monkeypatch.setattr(control, "cleanup", cleanup)
    async def check():
        control.submit(OWNER, TASK, contract())
        await started.wait()
        cancel = asyncio.create_task(control.cancel(OWNER, TASK))
        await asyncio.sleep(0)
        finish.set()
        assert (await cancel)["status"] == "cancelled"
        assert events == ["created", "cleaned"]
    asyncio.run(check())


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'[]'])
def test_invalid_script_output_is_not_success(control, monkeypatch, raw):
    async def command(*args, **kwargs):
        return (1 if args[0] == "container" else 0), raw, b""
    monkeypatch.setattr(worker, "command", command)
    async def check():
        control.submit(OWNER, TASK, contract())
        await control.tasks[TASK]
        assert control.lookup(OWNER, TASK)["status"] == "failed"
    asyncio.run(check())


def test_http_auth_owner_and_request_limits(control):
    import httpx
    worker.app.state.worker = control
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=worker.app), base_url="http://test") as http:
            assert (await http.get("/health")).status_code == 401
            headers = {"Authorization": "Bearer " + control.token, "X-Task-Owner": OWNER}
            assert (await http.get("/health", headers=headers)).status_code == 200
            assert (await http.delete("/tasks/" + TASK, headers=headers)).status_code == 200
            other = {**headers, "X-Task-Owner": "e" * 64}
            assert (await http.get("/tasks/" + TASK, headers=other)).status_code == 404
            for raw in (b'{"data":{},"data":{}}', b'{"data":NaN}'):
                assert (await http.put("/tasks/" + TASK, headers=headers, content=raw)).status_code == 400
            assert (await http.put("/tasks/" + TASK, headers=headers, content=b"x" * 200001)).status_code == 413
    asyncio.run(check())


def test_client_repeated_cancel_and_control_failure_is_unconfirmed(monkeypatch):
    monkeypatch.setenv("SANDBOX_PYTHON_IMAGE", IMAGE)
    monkeypatch.setattr(client, "health", AsyncMock())
    started, cleaning, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def request(*args, **kwargs):
        started.set()
        await asyncio.sleep(60)
    async def cancel(*args):
        cleaning.set()
        await finish.wait()
        raise ToolExecutionError("sandbox_unavailable")
    monkeypatch.setattr(client, "request", request)
    monkeypatch.setattr(client, "cancel", cancel)
    row = {"owner_id": "alice", "version": "d" * 64, "approved_by": "admin", "review_json": json.dumps({"runtime_profile": client.runtime_profile()})}
    spec = {"config": {"code": contract()["code"]}, "input_schema": specs.object_schema({"n": {"type": "number"}})}
    async def check():
        task = asyncio.create_task(client.execute(row, spec, {"n": 2}, TASK))
        await started.wait()
        task.cancel()
        await cleaning.wait()
        task.cancel()
        await asyncio.sleep(0)
        finish.set()
        with pytest.raises(client.CancellationUnconfirmed):
            await task
    asyncio.run(check())


@pytest.mark.parametrize("kind", ["python_script", "unknown"])
def test_legacy_profile_override_never_runs_dynamic_code(monkeypatch, kind):
    from app.services.sandbox import controller
    monkeypatch.setattr(controller, "_isolated_runtime_ready", lambda: True)
    run = AsyncMock()
    monkeypatch.setattr(controller.cli_runner.default_runner, "run", run)
    result = asyncio.run(controller.SandboxController().submit_execution(controller.ExecutionSpec(
        executor_kind=kind, runtime_profile_id="fixed_adapter", argv=["python", "-c", "print(1)"])))
    assert result.execution_status in ("blocked", "sandbox_unavailable")
    run.assert_not_awaited()


@pytest.mark.parametrize("token", ["", "x" * 31, "x" * 4097, "密" * 32, "x" * 31 + "\n", "x" * 31 + " "])
def test_invalid_token_never_opens_gate_or_authenticates(control, monkeypatch, token):
    from types import SimpleNamespace
    control.token = token
    monkeypatch.setattr(control, "open", lambda: None)
    monkeypatch.setattr(worker, "command", AsyncMock(return_value=(0, b"", b"")))
    asyncio.run(control.startup())
    assert not control.ready
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(worker=control)),
                              headers={"Authorization": "Bearer " + token})
    with pytest.raises(HTTPException) as caught:
        worker.authenticated(request)
    assert caught.value.status_code == 401


def test_non_ascii_authorization_is_unauthorized_not_server_error(control):
    from types import SimpleNamespace
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(worker=control)),
                              headers={"Authorization": "Bearer 非ASCII"})
    with pytest.raises(HTTPException) as caught:
        worker.authenticated(request)
    assert caught.value.status_code == 401


def test_token_file_invalid_encoding_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "token"
    path.write_bytes(b"\xff" * 32)
    monkeypatch.setenv("SANDBOX_WORKER_TOKEN_FILE", str(path))
    assert worker.load_token() == ""
    path.write_text("x" * 32 + "\n")
    assert worker.load_token() == "x" * 32


def test_worker_client_invalid_encoding_returns_unavailable(tmp_path, monkeypatch):
    from app.services.sandbox import worker_client
    from app.services.tool_http import ToolExecutionError
    path = tmp_path / "client-token"
    path.write_bytes(b"\xff" * 32)
    monkeypatch.setenv("SANDBOX_WORKER_TOKEN_FILE", str(path))
    monkeypatch.setenv("SANDBOX_PYTHON_ENABLED", "true")
    monkeypatch.setenv("SANDBOX_WORKER_URL", "https://worker.example.com")
    monkeypatch.setattr(worker_client, "runtime_profile", lambda: {"profile": "test"})
    assert worker_client.configured() is False
    with pytest.raises(ToolExecutionError, match="sandbox_unavailable"):
        worker_client.configuration()


def test_worker_startup_recovers_orphans_and_requires_acceptance(control, monkeypatch):
    calls = []
    control.conn.execute("INSERT INTO tasks(id,owner,request_hash,status,created_at) VALUES (?,?,?,'running',?)", (TASK, OWNER, 'hash', time.time()))
    monkeypatch.setattr(control, "open", lambda: None)
    async def command(*args, **kwargs):
        calls.append(args)
        if args[0] == 'ps':
            return 0, ('jzpy-' + TASK).encode(), b''
        return (1 if args[0] == 'container' else 0), b'', b''
    monkeypatch.setattr(worker, 'command', command)
    control.accepted = False
    asyncio.run(control.startup())
    assert not control.ready
    assert control.lookup(OWNER, TASK)['status'] == 'interrupted'
    assert any(args[:3] == ('rm', '--force', '--ignore') for args in calls)


def test_worker_queue_timeout_does_not_create_container(control, monkeypatch):
    class Full:
        async def acquire(self):
            raise TimeoutError()
    control.slots = Full()
    command = AsyncMock()
    monkeypatch.setattr(worker, 'command', command)
    async def check():
        control.submit(OWNER, TASK, contract())
        await control.tasks[TASK]
        assert control.lookup(OWNER, TASK)['status'] == 'timeout'
        command.assert_not_awaited()
    asyncio.run(check())


def test_package_exports_only_approved_image_and_allowlisted_sources(tmp_path, monkeypatch):
    import sys
    import tarfile
    module = importlib.util.spec_from_file_location('worker_package_test', ROOT / 'sandbox_worker/package.py')
    package = importlib.util.module_from_spec(module)
    module.loader.exec_module(package)
    calls = []
    def run(*args):
        calls.append(args)
        if args[0] == 'info':
            return b'{"host":{"security":{"rootless":true}}}'
        if args[:2] == ('image', 'inspect'):
            return json.dumps([{'RepoDigests': [IMAGE], 'Architecture': 'amd64'}]).encode()
        if args[0] == 'save':
            Path(args[args.index('--output') + 1]).write_bytes(b'synthetic-oci')
            return b''
        raise AssertionError(args)
    monkeypatch.setattr(package, 'run', run)
    bundle = tmp_path / 'worker.tar.gz'
    monkeypatch.setattr(sys, 'argv', ['package.py', 'export', '--image', IMAGE, '--output', str(bundle)])
    package.main()
    with tarfile.open(bundle) as archive:
        names = archive.getnames()
        assert set(names) == {'manifest.json', 'python-image.oci.tar'} | {'jianzi-worker/' + name for name in package.SOURCES}
        manifest = json.load(archive.extractfile('manifest.json'))
        assert not manifest['isolation_verified'] and manifest['image'] == IMAGE
        assert manifest['seccomp_sha256'] == worker.profile(IMAGE)['seccomp_sha256']
        for name, checksum in manifest['files'].items():
            assert hashlib.sha256(archive.extractfile(name).read()).hexdigest() == checksum
    assert not any(args[0] in ('push', 'run', 'start') for args in calls)
    with pytest.raises(ValueError, match='目标包已存在'):
        package.main()
