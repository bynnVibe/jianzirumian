"""可信后端访问专用 Worker；仅发送已审批版本的限量 JSON，无主机执行降级。"""
import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from app.core.json_schema import validate
from app.services.personal_tool_specs import fingerprint, assert_result
from app.services.tool_http import ToolExecutionError


class CancellationUnconfirmed(asyncio.CancelledError):
    """控制连接不可用时不能把取消请求宣称为容器已经终止。"""


def runtime_profile():
    image = os.getenv("SANDBOX_PYTHON_IMAGE", "")
    seccomp_hash = os.getenv("SANDBOX_SECCOMP_SHA256", "")
    if (not re.fullmatch(r"[a-z0-9][a-z0-9._:/-]*@sha256:[a-f0-9]{64}", image)
            or not re.fullmatch(r"[a-f0-9]{64}", seccomp_hash)):
        return {}
    return {"protocol": "isolated-python-1", "image": image, "python": "3.11", "dependencies": "stdlib",
            "network": "none", "seccomp": "python-stdlib-1", "seccomp_sha256": seccomp_hash,
            "cpus": 1, "memory": 268435456, "pids": 32, "seconds": 30,
            "tmp_bytes": 33554432, "output_bytes": 262144, "user": "65532:65532"}


def configuration():
    if os.getenv("SANDBOX_PYTHON_ENABLED", "false").lower() != "true":
        raise ToolExecutionError("sandbox_unavailable")
    url = os.getenv("SANDBOX_WORKER_URL", "").rstrip("/")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path or not runtime_profile()):
        raise ToolExecutionError("sandbox_unavailable")
    path = os.getenv("SANDBOX_WORKER_TOKEN_FILE", "")
    try:
        token = Path(path).read_text().strip() if path else ""
    except (OSError, UnicodeError):
        token = ""
    if len(token) < 32 or len(token) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ToolExecutionError("sandbox_unavailable")
    return url, token


def configured():
    try:
        configuration()
        return True
    except (ValueError, OSError):
        return False


async def request(method, path, owner="", body=None, *, timeout=10):
    url, token = configuration()
    headers = {"Authorization": "Bearer " + token, "X-Task-Owner": hashlib.sha256(owner.encode()).hexdigest()}
    try:
        async with aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar(),
                timeout=aiohttp.ClientTimeout(total=timeout)) as client:
            async with client.request(method, url + path, json=body, headers=headers, allow_redirects=False) as response:
                if response.status != 200:
                    raise ToolExecutionError("sandbox_busy" if response.status == 429 else "sandbox_unavailable")
                raw = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    raw.extend(chunk)
                    if len(raw) > 300000:
                        raise ToolExecutionError("sandbox_invalid_response")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ToolExecutionError("sandbox_invalid_response")
                return result
    except asyncio.CancelledError:
        raise
    except ToolExecutionError:
        raise
    except Exception:
        raise ToolExecutionError("sandbox_unavailable") from None


async def health():
    expected = runtime_profile()
    actual = await request("GET", "/health")
    if (actual.get("available") is not True or actual.get("profile") != expected
            or actual.get("profile_hash") != fingerprint(expected)):
        raise ToolExecutionError("sandbox_unavailable")
    return actual


async def cancel(owner, execution_id):
    if not re.fullmatch(r"[a-f0-9]{32}", execution_id):
        raise ToolExecutionError("sandbox_invalid_identity")
    result = await request("DELETE", "/tasks/" + execution_id, owner, timeout=35)
    return result.get("id") == execution_id and result.get("status") in ("cancelled", "completed", "failed", "timeout", "interrupted")


async def _terminate(owner, execution_id):
    cleanup = asyncio.create_task(cancel(owner, execution_id))
    interrupted = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            interrupted = True
        except Exception:
            break
    try:
        return cleanup.result(), interrupted
    except (Exception, asyncio.CancelledError):
        return False, interrupted


async def execute(row, spec, arguments, execution_id):
    if not re.fullmatch(r"[a-f0-9]{32}", execution_id):
        raise ToolExecutionError("sandbox_invalid_identity")
    profile = runtime_profile()
    assessment = json.loads(row["review_json"])
    if not row["approved_by"] or not profile or assessment.get("runtime_profile") != profile:
        raise ToolExecutionError("admin_review_required")
    validate(spec["input_schema"], arguments, max_bytes=65536)
    await health()
    body = {"code": spec["config"]["code"], "code_hash": hashlib.sha256(spec["config"]["code"].encode()).hexdigest(),
            "version": row["version"], "data": arguments, "profile_hash": fingerprint(profile), "expires_at": time.time() + 90}
    submitted, terminal = False, False
    try:
        async with asyncio.timeout(65):
            submitted = True
            result = await request("PUT", "/tasks/" + execution_id, row["owner_id"], body)
            while result.get("status") in ("queued", "running", "cancelling"):
                await asyncio.sleep(.25)
                result = await request("GET", "/tasks/" + execution_id, row["owner_id"])
            if result.get("id") != execution_id:
                raise ToolExecutionError("sandbox_invalid_response")
            terminal = result.get("status") in ("completed", "failed", "timeout", "cancelled", "interrupted")
            if result.get("status") != "completed":
                raise ToolExecutionError("sandbox_" + result["status"] if terminal else "sandbox_cleanup_unconfirmed")
            output = result.get("output")
            validate(spec["output_schema"], output, max_bytes=262144)
            assert_result(output, arguments, spec.get("assertions", []))
            return {"output": output, "execution_status": "completed", "validation_status": "passed",
                    "requirement_status": "unverified", "runtime_profile": profile, "cache_ttl": 0, "sources": []}
    except asyncio.CancelledError:
        if submitted and not terminal:
            confirmed, _ = await _terminate(row["owner_id"], execution_id)
            if not confirmed:
                raise CancellationUnconfirmed("sandbox_cancellation_unconfirmed") from None
        raise
    except Exception:
        if submitted and not terminal:
            confirmed, interrupted = await _terminate(row["owner_id"], execution_id)
            if interrupted:
                if not confirmed:
                    raise CancellationUnconfirmed("sandbox_cancellation_unconfirmed") from None
                raise asyncio.CancelledError()
            if not confirmed:
                raise ToolExecutionError("sandbox_cleanup_unconfirmed") from None
        raise
