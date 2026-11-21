"""
见字如面 - Agent→Proxy 签名调用上下文（HMAC-SHA256）

业务侧（LLMFactory.create_for_role / rerank 等）把调用归属用网关内部密钥的
派生密钥签名后注入 _gw_ctx/_gw_sig；代理侧验签通过才信任其中的用户/trace
归属，出网前随其他 _gw_* 字段一并剥离（upstream.build_request 统一处理）。

- 派生密钥：sha256(internal_key 明文)，不外发、不落库、不出现在请求体；
- 有效期 60 秒：防止旧归属重放；验签失败不阻断请求本身（密钥仍走正常鉴权），
  仅表示「不信任其声明的用户归属」，避免伪造 _gw_user_id 串用他人配额。
"""
import hashlib
import hmac
import json
import time

_TTL_SECONDS = 60


def _secret() -> bytes:
    from app.services.gateway import keys as key_svc
    return hashlib.sha256(
        key_svc.ensure_internal_key().encode("utf-8")).hexdigest().encode("utf-8")


def sign_payload(payload: dict) -> tuple[str, str]:
    """签名归属载荷，返回 (_gw_ctx JSON, _gw_sig)；ts 自动注入。"""
    data = {k: str(v or "") for k, v in payload.items()}
    data["ts"] = str(int(time.time()))
    ctx_json = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sig = hmac.new(_secret(), ctx_json.encode("utf-8"), hashlib.sha256).hexdigest()
    return ctx_json, sig


def verify_payload(ctx_json: str, sig: str) -> dict:
    """验签成功返回载荷 dict；篡改/过期/格式错误一律返回 {}（不抛异常）。"""
    if not ctx_json or not sig:
        return {}
    try:
        expect = hmac.new(_secret(), str(ctx_json).encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expect, str(sig)):
            return {}
        data = json.loads(ctx_json)
        if not isinstance(data, dict):
            return {}
        if abs(time.time() - int(data.get("ts") or 0)) > _TTL_SECONDS:
            return {}
        return data
    except Exception:
        return {}


def signed_extra(user_id: str, role: str, rule_id: str = "") -> dict:
    """构造 _gw_user_id/_gw_ctx/_gw_sig 注入字段（用户为空返回 {}）。

    trace/job/origin 沿用当前 CallContext（请求入口激活），使网关日志与
    可观测性 trace 同源关联；后台任务在派生上下文中同样携带触发用户。
    """
    user_id = str(user_id or "")
    if not user_id:
        return {}
    from app.core import call_context
    cc = call_context.current()
    payload = {
        "user_id": user_id,
        "billing_user_id": cc.billing_user_id or user_id,
        "trace_id": cc.trace_id,
        "span_id": cc.span_id,
        "job_id": cc.job_id,
        "origin": cc.origin or "chat",
        "role": role,
        "rule_id": rule_id,
    }
    ctx_json, sig = sign_payload(payload)
    return {"_gw_user_id": user_id, "_gw_ctx": ctx_json, "_gw_sig": sig}