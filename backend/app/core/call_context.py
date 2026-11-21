"""
见字如面 - 请求级调用上下文（CallContext）

全链路用户归属的单一事实来源：
- actor_user_id  ：链路发起人（真实用户 id；游客/系统为空或 guest）；
- billing_user_id：计费归属用户（默认=发起人；后台任务沿用触发用户）；
- trace_id/span_id：关联可观测性 trace 与网关日志；
- job_id         ：后台任务标识（入库/摘要/评测等）；
- origin         ：链路来源 chat|guest|background|system|api。

基于 contextvars 在同一 asyncio 任务内传递；asyncio.to_thread 会自动复制上下文，
线程池中的同步模块（embedding/rerank/OCR）无需改签名即可取到发起用户。
上下文缺失时返回空 CallContext（兼容旧调用与测试桩），绝不抛异常。
"""
import contextvars
import uuid
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CallContext:
    actor_user_id: str = ""
    billing_user_id: str = ""
    trace_id: str = ""
    span_id: str = ""
    job_id: str = ""
    origin: str = ""

    @property
    def user_id(self) -> str:
        """出网凭据解析用的归属用户（计费归属优先）。"""
        return self.billing_user_id or self.actor_user_id


_current: contextvars.ContextVar[Optional[CallContext]] = contextvars.ContextVar(
    "jzrm_call_context", default=None)


def current() -> CallContext:
    return _current.get() or CallContext()


def current_user_id() -> str:
    return current().user_id


def activate(*, actor_user_id: str = "", billing_user_id: str = "", trace_id: str = "",
             job_id: str = "", origin: str = "") -> contextvars.Token:
    """在当前任务内激活调用上下文，返回 token 供 deactivate 恢复。

    与 observability.start_trace 同模式：在请求入口（chat_stream 等）设置，
    finally 中恢复；嵌套调用（后台任务）可覆盖为任务级上下文。
    """
    actor = str(actor_user_id or "")
    ctx = CallContext(
        actor_user_id=actor,
        billing_user_id=str(billing_user_id or "") or actor,
        trace_id=str(trace_id or ""),
        span_id=uuid.uuid4().hex[:16],
        job_id=str(job_id or ""),
        origin=str(origin or ""),
    )
    return _current.set(ctx)


def deactivate(token: contextvars.Token) -> None:
    try:
        _current.reset(token)
    except (ValueError, LookupError):
        pass


def derive(**overrides) -> CallContext:
    """派生子上下文（后台任务沿用归属用户、换新 span_id）。"""
    base = current()
    fields = {
        "actor_user_id": base.actor_user_id,
        "billing_user_id": base.billing_user_id,
        "trace_id": base.trace_id,
        "span_id": uuid.uuid4().hex[:16],
        "job_id": base.job_id,
        "origin": base.origin,
    }
    fields.update({k: v for k, v in overrides.items() if v is not None})
    return CallContext(**fields)