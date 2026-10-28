"""
见字如面 - 监控层 Prometheus 指标导出（层5）

把 Agent 可观测性（app.core.observability.metrics）聚合出的运行指标，
渲染为 Prometheus 文本格式（text/plain; version=0.0.4），供 Prometheus 抓取、
Grafana 出图。agent 与 llm-proxy 两个服务均暴露 GET /metrics 复用本模块。

设计约束：
- 可观测性本身绝不能影响主流程：渲染过程任何异常都退化为仅输出 up 指标。
- 不引入 prometheus_client 重依赖，直接手工拼装文本（指标量小、结构固定）。
"""
import logging
import time

logger = logging.getLogger("jianziruyang.metrics")

# 进程启动时间，用于 uptime gauge
_PROCESS_START = time.time()


def _line(name: str, help_text: str, type_: str, value, labels: dict = None) -> str:
    """渲染单条指标（HELP + TYPE + 采样行）。"""
    label_str = ""
    if labels:
        inner = ",".join(f'{k}="{v}"' for k, v in labels.items())
        label_str = "{" + inner + "}"
    return (
        f"# HELP {name} {help_text}\n"
        f"# TYPE {name} {type_}\n"
        f"{name}{label_str} {value}\n"
    )


def render_prometheus(service: str = "agent") -> str:
    """渲染 Prometheus 文本格式指标。service 用于区分 agent / llm-proxy。"""
    out = []
    # 进程存活 gauge：抓取成功即为 1
    out.append(_line(
        "jzrm_up", "服务存活标记（1=存活）", "gauge", 1, {"service": service},
    ))
    out.append(_line(
        "jzrm_process_uptime_seconds", "进程已运行秒数", "gauge",
        int(time.time() - _PROCESS_START), {"service": service},
    ))

    from app.core.agent_runtime import readiness
    out.append(_line("jzrm_ready", "本地就绪状态", "gauge",
                     int(readiness(service)["status"] == "ready"), {"service": service}))
    try:
        from app.core.metric_counters import read
        scope = "agent" if service == "agent" else "gateway"
        counters = read(scope)
        labels = {"service": service}
        prefix = f"jzrm_{scope}"
        request_name = "traces" if scope == "agent" else "requests"
        total = sum(value for (metric, _), value in counters.items() if metric == "requests")
        out.append(_line(f"{prefix}_{request_name}_total", "持久化累计请求数", "counter", total, labels))
        for status in ("ok", "error", "blocked", "cancelled", "timeout", "interrupted"):
            out.append(_line(f"{prefix}_{request_name}_{status}_total", f"累计 {status} 请求数", "counter",
                             counters.get(("requests", status), 0), labels))
        fields = ["prompt_tokens", "completion_tokens"]
        if scope == "agent":
            fields += ["llm_calls", "tool_calls"]
            from app.core import observability as obs
            ov = obs.metrics(days=1).get("overview", {})
        else:
            from app.services.gateway.logs import stats
            ov = stats(days=1).get("overview", {})
        for field in fields:
            out.append(_line(f"{prefix}_{field}_total", f"持久化累计 {field}", "counter",
                             counters.get((field, ""), 0), labels))
        for field in ("success_rate", "avg_latency_ms", "p95_latency_ms"):
            if field in ov:
                out.append(_line(f"{prefix}_{field}", f"当天零点以来窗口 {field}", "gauge", ov[field], labels))
    except Exception as e:
        logger.warning("渲染业务指标失败: %s", e)

    return "".join(out)
