"""专用 Linux 主机上的可信 Worker；动态代码仅进入一次性 rootless Podman 容器。"""
import asyncio
import fcntl
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

PROTOCOL = "isolated-python-1"
LABEL = "io.jianzirumian.sandbox=python-v1"
MAX_INPUT = 65536
MAX_OUTPUT = 262144
MAX_BODY = 200000
ID = re.compile(r"[a-f0-9]{32}")
OWNER = re.compile(r"[a-f0-9]{64}")
IMAGE = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[a-f0-9]{64}")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result
    def reject_constant(_):
        raise ValueError("nonfinite")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=reject_constant)


async def settle(task):
    """清理和迟到创建不能被重复取消打断；子任务自身的取消仍向上传递。"""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


def profile(image):
    return {"protocol": PROTOCOL, "image": image, "python": "3.11", "dependencies": "stdlib",
            "network": "none", "seccomp": "python-stdlib-1",
            "seccomp_sha256": hashlib.sha256(Path(__file__).with_name("seccomp.json").read_bytes()).hexdigest(),
            "cpus": 1, "memory": 268435456, "pids": 32, "seconds": 30,
            "tmp_bytes": 33554432, "output_bytes": MAX_OUTPUT, "user": "65532:65532"}


def create_args(image, name):
    if not IMAGE.fullmatch(image) or not re.fullmatch(r"jzpy-[a-f0-9]{32}", name):
        raise ValueError("invalid_runtime_configuration")
    return ["create", "--name", name, "--label", LABEL, "--pull=never", "--network=none",
            "--user=65532:65532", "--read-only", "--read-only-tmpfs=false", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--security-opt=seccomp=" + str(Path(__file__).with_name("seccomp.json")),
            "--image-volume=ignore", "--ipc=private", "--pid=private", "--cgroupns=private",
            "--cpus=1", "--memory=256m", "--memory-swap=256m", "--pids-limit=32",
            "--ulimit=nofile=64:64", "--ulimit=core=0:0", "--ulimit=fsize=1048576:1048576",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=33554432,mode=1777", "--workdir=/tmp",
            "--log-driver=none", "--http-proxy=false", "--unsetenv-all", "--env=LANG=C.UTF-8",
            "--env=PYTHONDONTWRITEBYTECODE=1", "--entrypoint=/usr/local/bin/python3",
            "-i", image, "-I", "-B", "/opt/runner.py"]


class RuntimeFailure(Exception):
    pass


async def command(*args, input_data=None, timeout=10, limit=MAX_OUTPUT):
    """仅管理固定 Podman argv；限量读取两个管道，不使用 shell 或宿主脚本。"""
    binary = os.getenv("SANDBOX_PODMAN", "/usr/bin/podman")
    launch = asyncio.create_task(asyncio.create_subprocess_exec(binary, *args,
        stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE))
    try:
        proc = await asyncio.shield(launch)
    except asyncio.CancelledError:
        proc = await settle(launch)
        if proc.returncode is None:
            proc.kill()
        await settle(asyncio.create_task(proc.wait()))
        raise
    size = 0
    async def read(pipe):
        nonlocal size
        chunks = []
        while chunk := await pipe.read(8192):
            size += len(chunk)
            if size > limit:
                raise RuntimeFailure("output_limit")
            chunks.append(chunk)
        return b"".join(chunks)
    async def write():
        if proc.stdin:
            try:
                proc.stdin.write(input_data)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()
    try:
        async with asyncio.timeout(timeout):
            async with asyncio.TaskGroup() as group:
                out = group.create_task(read(proc.stdout))
                err = group.create_task(read(proc.stderr))
                group.create_task(write())
                group.create_task(proc.wait())
        return proc.returncode, out.result(), err.result()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


class Worker:
    def __init__(self, directory, image, token, accepted=False):
        self.directory, self.image, self.token = Path(directory), image, token
        self.accepted = accepted
        self.ready = False
        self.tasks = {}
        self.owners = set()
        self.slots = asyncio.Semaphore(2)
        self.conn = None
        self.lock = None

    def open(self):
        previous_umask = os.umask(0o077)
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.directory.chmod(0o700)
            self.lock = (self.directory / "worker.lock").open("a")
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.conn = sqlite3.connect(self.directory / "tasks.sqlite", isolation_level=None)
        finally:
            os.umask(previous_umask)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, owner TEXT NOT NULL, "
                          "request_hash TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}', "
                          "created_at REAL NOT NULL, finished_at REAL)")

    async def probe(self):
        if sys.platform != "linux" or os.geteuid() == 0 or not IMAGE.fullmatch(self.image):
            raise RuntimeFailure("requires_linux_rootless_pinned_image")
        code, out, _ = await command("info", "--format=json")
        if code:
            raise RuntimeFailure("runtime_unavailable")
        host = json.loads(out)["host"]
        if (host["security"].get("rootless") is not True or host["security"].get("seccompEnabled") is not True
                or host.get("cgroupVersion") != "v2"
                or not {"cpu", "memory", "pids"}.issubset(set(host.get("cgroupControllers", [])))):
            raise RuntimeFailure("requires_rootless_seccomp_cgroup_v2")
        code, out, _ = await command("image", "inspect", self.image)
        if code or self.image not in (json.loads(out)[0].get("RepoDigests") or []):
            raise RuntimeFailure("approved_image_unavailable")

    async def cleanup(self, name):
        for _ in range(3):
            try:
                await command("rm", "--force", "--ignore", name)
                code, _, _ = await command("container", "exists", name)
                if code == 1:
                    return
            except Exception:
                continue
        self.ready = False
        raise RuntimeFailure("cleanup_unconfirmed")

    async def startup(self):
        self.open()
        try:
            await self.probe()
            code, out, _ = await command("ps", "-a", "--filter", "label=" + LABEL, "--format={{.Names}}")
            if code:
                raise RuntimeFailure("recovery_failed")
            for name in out.decode().splitlines():
                if not re.fullmatch(r"jzpy-[a-f0-9]{32}", name):
                    raise RuntimeFailure("unexpected_managed_container")
                await self.cleanup(name)
            self.conn.execute("UPDATE tasks SET status='interrupted',finished_at=? WHERE status IN ('queued','running','cancelling','cleanup_failed')", (time.time(),))
            self.ready = self.accepted and valid_token(self.token)
        except Exception:
            # 具体引擎错误可能含宿主路径；健康端点只返回不可用。
            self.ready = False

    def lookup(self, owner, task_id):
        row = self.conn.execute("SELECT * FROM tasks WHERE owner=? AND id=?", (owner, task_id)).fetchone()
        if row is None:
            raise HTTPException(404, "task_not_found")
        result = json.loads(row["result_json"])
        return {"id": task_id, "status": row["status"], **result}

    def validate(self, owner, task_id, body):
        if (not ID.fullmatch(task_id) or not OWNER.fullmatch(owner) or not isinstance(body, dict)
                or set(body) != {"code", "code_hash", "version", "data", "profile_hash", "expires_at"}):
            raise HTTPException(400, "invalid_contract")
        if (not isinstance(body["code"], str) or not 1 <= len(body["code"].encode()) <= MAX_INPUT
                or not isinstance(body["data"], dict) or len(canonical(body["data"]).encode()) > MAX_INPUT
                or body["code_hash"] != hashlib.sha256(body["code"].encode()).hexdigest()
                or not isinstance(body["version"], str) or not OWNER.fullmatch(body["version"])
                or body["profile_hash"] != digest(profile(self.image))
                or type(body["expires_at"]) not in (float, int) or not time.time() < body["expires_at"] <= time.time() + 120):
            raise HTTPException(400, "invalid_contract")

    def submit(self, owner, task_id, body):
        self.validate(owner, task_id, body)
        # 有效期变化不改变幂等身份，代码、输入与所有权限仍绑定。
        signature = digest({k: v for k, v in body.items() if k != "expires_at"})
        row = self.conn.execute("SELECT owner,request_hash FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row:
            if row["owner"] != owner or row["request_hash"] not in (signature, "cancelled_before_submit"):
                raise HTTPException(409, "task_conflict")
            return self.lookup(owner, task_id)
        if not self.ready:
            raise HTTPException(503, "sandbox_unavailable")
        if owner in self.owners or len(self.tasks) >= 32:
            raise HTTPException(429, "sandbox_busy")
        self.conn.execute("DELETE FROM tasks WHERE finished_at<?", (time.time() - 7 * 86400,))
        self.conn.execute("INSERT INTO tasks(id,owner,request_hash,status,created_at) VALUES (?,?,?,'queued',?)",
                          (task_id, owner, signature, time.time()))
        self.owners.add(owner)
        task = asyncio.create_task(self.execute(owner, task_id, body))
        self.tasks[task_id] = task
        task.add_done_callback(lambda _: self.release(owner, task_id))
        return self.lookup(owner, task_id)

    def release(self, owner, task_id):
        self.owners.discard(owner)
        self.tasks.pop(task_id, None)

    async def execute(self, owner, task_id, body):
        name, acquired, attempted = "jzpy-" + task_id, False, False
        state, result = "failed", {"error": "sandbox_execution_failed"}
        try:
            async with asyncio.timeout(30):
                await self.slots.acquire()
                acquired = True
            if not self.ready:
                raise RuntimeFailure("sandbox_unavailable")
            self.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
            async with asyncio.timeout(30):
                await self.probe()
                attempted = True
                # 取消时等待 create 命令结束后才清理，避免迟到创建留下容器。
                creation = asyncio.create_task(command(*create_args(self.image, name)))
                try:
                    code, _, _ = await asyncio.shield(creation)
                except asyncio.CancelledError:
                    await settle(creation)
                    raise
                if code:
                    raise RuntimeFailure("container_create_failed")
                code, out, _ = await command("start", "--attach", "--interactive", name,
                    input_data=canonical({"code": body["code"], "data": body["data"]}).encode(), timeout=30)
                if code:
                    raise RuntimeFailure("script_failed")
                output = strict_json(out)
                if not isinstance(output, dict) or len(canonical(output).encode()) > MAX_OUTPUT:
                    raise RuntimeFailure("invalid_output")
                state, result = "completed", {"output": output}
        except asyncio.CancelledError:
            state, result = "cancelled", {"error": "cancelled"}
        except TimeoutError:
            state, result = "timeout", {"error": "sandbox_timeout"}
        except Exception:
            state, result = "failed", {"error": "sandbox_execution_failed"}
        finally:
            if attempted:
                try:
                    cleanup = asyncio.create_task(self.cleanup(name))
                    await settle(cleanup)
                except Exception:
                    self.ready = False
                    state, result = "cleanup_failed", {"error": "cleanup_unconfirmed"}
            if acquired:
                self.slots.release()
            self.conn.execute("UPDATE tasks SET status=?,result_json=?,finished_at=? WHERE id=? AND owner=?",
                              (state, canonical(result), time.time(), task_id, owner))

    async def cancel(self, owner, task_id):
        if not OWNER.fullmatch(owner) or not ID.fullmatch(task_id):
            raise HTTPException(400, "invalid_identity")
        row = self.conn.execute("SELECT owner FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            # 删除请求可能先于迟到的提交到达；墓碑阻止后续意外执行。
            self.conn.execute("INSERT INTO tasks(id,owner,request_hash,status,created_at,finished_at) VALUES (?,?,'cancelled_before_submit','cancelled',?,?)",
                              (task_id, owner, time.time(), time.time()))
            return self.lookup(owner, task_id)
        if row["owner"] != owner:
            raise HTTPException(404, "task_not_found")
        task = self.tasks.get(task_id)
        if task and not task.done():
            self.conn.execute("UPDATE tasks SET status='cancelling' WHERE id=?", (task_id,))
            # 让新任务先进入 try/finally，再取消排队或运行状态。
            await asyncio.sleep(0)
            if not task.done() and not task.cancelling():
                task.cancel()
            await asyncio.shield(task)
        result = self.lookup(owner, task_id)
        if result["status"] == "cleanup_failed":
            try:
                await self.cleanup("jzpy-" + task_id)
            except Exception:
                return result
            self.conn.execute("UPDATE tasks SET status='interrupted',result_json='{}',finished_at=? WHERE id=?",
                              (time.time(), task_id))
        return self.lookup(owner, task_id)

    async def shutdown(self):
        self.ready = False
        pending = list(self.tasks.values())
        await asyncio.sleep(0)
        for task in pending:
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if self.conn:
            self.conn.close()
        if self.lock:
            self.lock.close()


def valid_token(value):
    return isinstance(value, str) and 32 <= len(value) <= 4096 and all(33 <= ord(c) <= 126 for c in value)


def load_token():
    path = os.getenv("SANDBOX_WORKER_TOKEN_FILE", "")
    try:
        value = Path(path).read_text().strip() if path else ""
    except (OSError, UnicodeError):
        return ""
    return value if valid_token(value) else ""


@asynccontextmanager
async def lifespan(app):
    worker = Worker(os.getenv("SANDBOX_WORKER_STATE", "./state"), os.getenv("SANDBOX_PYTHON_IMAGE", ""),
                    load_token(), os.getenv("SANDBOX_ISOLATION_ACCEPTED", "false").lower() == "true")
    app.state.worker = worker
    await worker.startup()
    try:
        yield
    finally:
        await worker.shutdown()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def authenticated(request):
    worker = request.app.state.worker
    if not valid_token(worker.token) or not hmac.compare_digest(
            request.headers.get("Authorization", "").encode("utf-8"), ("Bearer " + worker.token).encode("ascii")):
        raise HTTPException(401, "unauthorized")
    return worker


@app.get("/health")
async def health(request: Request):
    worker = authenticated(request)
    if worker.ready:
        try:
            await worker.probe()
        except Exception:
            worker.ready = False
    return {"available": worker.ready, "profile": profile(worker.image), "profile_hash": digest(profile(worker.image))}


@app.put("/tasks/{task_id}")
async def submit(task_id: str, request: Request):
    worker = authenticated(request)
    raw = bytearray()
    try:
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > MAX_BODY:
                    raise HTTPException(413, "input_limit")
    except TimeoutError:
        raise HTTPException(408, "input_timeout") from None
    try:
        body = strict_json(raw)
        if not isinstance(body, dict):
            raise ValueError()
        return worker.submit(request.headers.get("X-Task-Owner", ""), task_id, body)
    except (ValueError, TypeError, RecursionError, KeyError):
        raise HTTPException(400, "invalid_contract") from None


@app.get("/tasks/{task_id}")
async def get_task(task_id: str, request: Request):
    return authenticated(request).lookup(request.headers.get("X-Task-Owner", ""), task_id)


@app.delete("/tasks/{task_id}")
async def cancel_task(task_id: str, request: Request):
    return await authenticated(request).cancel(request.headers.get("X-Task-Owner", ""), task_id)
