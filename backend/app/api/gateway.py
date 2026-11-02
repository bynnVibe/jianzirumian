"""
见字如面 - 模型网关管理 API（管理员 / 运维角色）

覆盖后台管理与可视化数据：
- 上游管理：/api/gateway/upstreams（增删改查 + 克隆 + 连通性测试 + 健康重置）
- 路由规则：/api/gateway/routing-rules（模型模式 → 上游列表 + 负载均衡）
- 业务路由：/api/gateway/role-routes（role → 规则 + 模型）
- 密钥与配额：/api/gateway/keys（增删改查 + 重置用量）
- 日志与审计：/api/gateway/logs（搜索/筛选/分页 + 展开明细含链路）
- 安全审计中心：/api/gateway/settings、/api/gateway/rules
- 仪表盘：/api/gateway/overview、/api/gateway/stats

所有端点均通过 require_ops 校验（admin 与 ops 均可访问）；/my-rules 普通登录用户可访问。
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from app.api.deps import require_ops, get_current_user
from app.services.gateway import keys as key_svc
from app.services.gateway import logs as log_svc
from app.services.gateway import rules as rule_svc
from app.services.gateway import security
from app.services.gateway import settings as settings_svc
from app.services.gateway import upstreams as up_svc

logger = logging.getLogger("jianziruyang.gateway.api")

router = APIRouter(prefix="/api/gateway", tags=["gateway"])


# ---------------------------------------------------------------------------
# 请求体模型
# ---------------------------------------------------------------------------

class UpstreamBody(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    name: str
    protocol: str = "openai"
    base_url: str = ""
    api_key: str = ""
    # 成本层级：paid|free|subscription（free 且空 Base URL/Key 回退系统 OpenRouter）
    tier: str = "paid"
    # 模型清单（该上游可服务的模型名，图2「模型列表」）
    models: List[str] = []
    weight: int = 100
    enabled: bool = True
    remark: str = ""


class UpstreamPatch(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    name: Optional[str] = None
    protocol: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    tier: Optional[str] = None
    models: Optional[List[str]] = None
    weight: Optional[int] = None
    enabled: Optional[bool] = None
    remark: Optional[str] = None


class UpstreamTestBody(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    model: str = ""


class RuleBody(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    model_pattern: str
    upstreams: List[str] = []
    lb_strategy: str = "weighted"
    priority: int = 0
    is_default: bool = False
    allowed_users: List[str] = []
    enabled: bool = True


class RulePatch(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    model_pattern: Optional[str] = None
    upstreams: Optional[List[str]] = None
    lb_strategy: Optional[str] = None
    priority: Optional[int] = None
    is_default: Optional[bool] = None
    allowed_users: Optional[List[str]] = None
    enabled: Optional[bool] = None


class RoleRouteBody(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    role: str
    rule_id: str = ""
    model_name: str = ""


class KeyBody(BaseModel):
    name: str
    token_quota: int = 0
    request_quota: int = 0
    allowed_models: List[str] = []
    expires_at: Optional[str] = None


class KeyPatch(BaseModel):
    name: Optional[str] = None
    token_quota: Optional[int] = None
    request_quota: Optional[int] = None
    allowed_models: Optional[List[str]] = None
    enabled: Optional[bool] = None
    expires_at: Optional[str] = None


class SettingsBody(BaseModel):
    audit_mode: Optional[str] = None
    unicode_detection: Optional[bool] = None
    tool_cmd_detection: Optional[bool] = None
    outbound_detection: Optional[bool] = None
    retry_count: Optional[int] = None
    disabled_rules: Optional[List[str]] = None
    blacklist: Optional[List[str]] = None
    whitelist: Optional[List[str]] = None
    log_retention_days: Optional[int] = None
    # 网关默认模型（业务路由未指定模型名时的兜底出网模型；空串=清除回退 .env）
    default_model: Optional[str] = None


# ---------------------------------------------------------------------------
# 仪表盘
# ---------------------------------------------------------------------------

@router.get("/overview")
async def overview(_ops: dict = Depends(require_ops)):
    """网关总览：上游/规则/密钥计数 + 近 7 天调用统计 + 最近风险日志。"""
    stats = log_svc.stats(7)
    ups = up_svc.list_upstreams()
    rules = rule_svc.list_rules()
    keys = key_svc.list_keys()
    risky = log_svc.query_logs(risk_level="high", limit=5)
    risky_crit = log_svc.query_logs(risk_level="critical", limit=5)
    health = {"healthy": 0, "degraded": 0, "down": 0}
    for u in ups:
        h = u.get("health", "healthy")
        health[h] = health.get(h, 0) + 1
    return {
        "success": True,
        "upstream_count": len(ups),
        "upstream_enabled": sum(1 for u in ups if u.get("enabled")),
        "upstream_health": health,
        "rule_count": len(rules),
        "rule_enabled": sum(1 for r in rules if r.get("enabled")),
        "key_count": len(keys),
        "key_enabled": sum(1 for k in keys if k.get("enabled")),
        "stats": stats,
        "recent_risks": (risky_crit["items"] + risky["items"])[:8],
    }


@router.get("/stats")
async def stats(days: int = 7, _ops: dict = Depends(require_ops)):
    days = max(1, min(days, 90))
    return {"success": True, **log_svc.stats(days)}


# ---------------------------------------------------------------------------
# 上游管理（协议 + Base URL + Key + 成本层级 + 模型清单 + 权重/健康/延迟）
# ---------------------------------------------------------------------------

@router.get("/upstreams")
async def list_upstreams(_ops: dict = Depends(require_ops)):
    return {"success": True, "upstreams": up_svc.list_upstreams(),
            "protocols": list(up_svc.VALID_PROTOCOLS),
            "tiers": list(up_svc.TIERS),
            "models": up_svc.all_model_names()}


@router.post("/upstreams")
async def create_upstream(body: UpstreamBody, _ops: dict = Depends(require_ops)):
    try:
        up = up_svc.create_upstream(body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "upstream": up}


@router.put("/upstreams/{upstream_id}")
async def update_upstream(upstream_id: str, body: UpstreamPatch,
                          _ops: dict = Depends(require_ops)):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        up = up_svc.update_upstream(upstream_id, patch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not up:
        raise HTTPException(status_code=404, detail="上游不存在")
    return {"success": True, "upstream": up}


@router.delete("/upstreams/{upstream_id}")
async def delete_upstream(upstream_id: str, _ops: dict = Depends(require_ops)):
    if not up_svc.delete_upstream(upstream_id):
        raise HTTPException(status_code=404, detail="上游不存在")
    return {"success": True}


@router.post("/upstreams/{upstream_id}/clone")
async def clone_upstream(upstream_id: str, _ops: dict = Depends(require_ops)):
    up = up_svc.clone_upstream(upstream_id)
    if not up:
        raise HTTPException(status_code=404, detail="上游不存在")
    return {"success": True, "upstream": up}


@router.post("/upstreams/{upstream_id}/reset-health")
async def reset_upstream_health(upstream_id: str, _ops: dict = Depends(require_ops)):
    up_svc.reset_health(upstream_id)
    return {"success": True, "upstream": up_svc.get_upstream(upstream_id)}


@router.post("/upstreams/{upstream_id}/test")
async def test_upstream(upstream_id: str, body: UpstreamTestBody = UpstreamTestBody(),
                        _ops: dict = Depends(require_ops)):
    """对上游生效配置发一次最小请求验证连通性（可能产生上游费用）。"""
    if not up_svc.get_upstream(upstream_id):
        raise HTTPException(status_code=404, detail="上游不存在")
    result = await up_svc.test_connection(upstream_id, body.model)
    return {"success": True, **result}


@router.get("/openrouter-free")
async def openrouter_free(_ops: dict = Depends(require_ops)):
    """实时拉取 OpenRouter 免费模型列表（供快速导入为 free 层级上游）。"""
    ids = await up_svc.fetch_openrouter_free()
    return {"success": True, "models": ids}


# ---------------------------------------------------------------------------
# 路由规则（模型模式 → 上游列表 + 负载均衡策略）
# ---------------------------------------------------------------------------

@router.get("/routing-rules")
async def list_routing_rules(_ops: dict = Depends(require_ops)):
    return {"success": True, "rules": rule_svc.list_rules(),
            "lb_strategies": list(rule_svc.LB_STRATEGIES),
            "upstreams": up_svc.list_upstreams(),
            "models": up_svc.all_model_names()}


@router.post("/routing-rules")
async def create_routing_rule(body: RuleBody, _ops: dict = Depends(require_ops)):
    try:
        rule = rule_svc.create_rule(body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "rule": rule}


@router.put("/routing-rules/{rule_id}")
async def update_routing_rule(rule_id: str, body: RulePatch, _ops: dict = Depends(require_ops)):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        rule = rule_svc.update_rule(rule_id, patch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not rule:
        raise HTTPException(status_code=404, detail="路由规则不存在")
    return {"success": True, "rule": rule}


@router.delete("/routing-rules/{rule_id}")
async def delete_routing_rule(rule_id: str, _ops: dict = Depends(require_ops)):
    if not rule_svc.delete_rule(rule_id):
        raise HTTPException(status_code=404, detail="路由规则不存在")
    return {"success": True}


# ---------------------------------------------------------------------------
# 业务路由（role → 规则 + 模型）
# ---------------------------------------------------------------------------

@router.get("/role-routes")
async def list_role_routes(_ops: dict = Depends(require_ops)):
    return {"success": True, "routes": rule_svc.list_role_routes(),
            "roles": list(rule_svc.ROLES)}


@router.put("/role-routes/{role}")
async def upsert_role_route(role: str, body: RoleRouteBody, _ops: dict = Depends(require_ops)):
    try:
        route = rule_svc.upsert_role_route(role, body.rule_id, body.model_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "route": route}


@router.delete("/role-routes/{role}")
async def delete_role_route(role: str, _ops: dict = Depends(require_ops)):
    rule_svc.delete_role_route(role)
    return {"success": True}


# ---------------------------------------------------------------------------
# 当前用户可用规则（供聊天页路由下拉，普通登录用户即可访问）
# ---------------------------------------------------------------------------

@router.get("/my-rules")
async def my_rules(model: str = "", user: dict = Depends(get_current_user)):
    user_id = str(user.get("id") or user.get("username") or "")
    rules = rule_svc.list_rules_for_user(user_id, model=model or None)
    # 只回传下拉需要的字段，不暴露上游配置细节
    slim = [{"id": r["id"], "model_pattern": r["model_pattern"],
             "is_default": r.get("is_default", False),
             "lb_strategy": r.get("lb_strategy", "weighted")} for r in rules]
    return {"success": True, "rules": slim}


# ---------------------------------------------------------------------------
# 密钥与配额管理
# ---------------------------------------------------------------------------

@router.get("/keys")
async def list_keys(_ops: dict = Depends(require_ops)):
    return {"success": True, "keys": key_svc.list_keys(), "models": up_svc.all_model_names()}


@router.post("/keys")
async def create_key(body: KeyBody, ops: dict = Depends(require_ops)):
    try:
        info = key_svc.create_key(
            body.name, created_by=ops.get("username") or ops.get("id") or "",
            token_quota=body.token_quota, request_quota=body.request_quota,
            allowed_models=body.allowed_models, expires_at=body.expires_at)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 明文 key 仅此次返回
    return {"success": True, "key": info}


@router.put("/keys/{key_id}")
async def update_key(key_id: str, body: KeyPatch, _ops: dict = Depends(require_ops)):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    info = key_svc.update_key(key_id, patch)
    if not info:
        raise HTTPException(status_code=404, detail="密钥不存在")
    return {"success": True, "key": info}


@router.delete("/keys/{key_id}")
async def delete_key(key_id: str, _ops: dict = Depends(require_ops)):
    if not key_svc.delete_key(key_id):
        raise HTTPException(status_code=404, detail="密钥不存在")
    return {"success": True}


@router.post("/keys/{key_id}/reset")
async def reset_key(key_id: str, _ops: dict = Depends(require_ops)):
    info = key_svc.reset_usage(key_id)
    if not info:
        raise HTTPException(status_code=404, detail="密钥不存在")
    return {"success": True, "key": info}


# ---------------------------------------------------------------------------
# 请求日志与审计
# ---------------------------------------------------------------------------

@router.get("/logs")
async def query_logs(
    key_id: str = "", model: str = "", channel_id: str = "", status: str = "",
    risk_level: str = "", keyword: str = "", start: str = "", end: str = "",
    limit: int = 30, offset: int = 0, _ops: dict = Depends(require_ops),
):
    result = log_svc.query_logs(
        key_id=key_id, model=model, channel_id=channel_id, status=status,
        risk_level=risk_level, keyword=keyword, start=start, end=end,
        limit=limit, offset=offset)
    return {"success": True, **result}


@router.get("/logs/{log_id}")
async def get_log(log_id: str, _ops: dict = Depends(require_ops)):
    entry = log_svc.get_log(log_id)
    if not entry:
        raise HTTPException(status_code=404, detail="日志不存在")
    return {"success": True, "log": entry}


@router.delete("/logs")
async def purge_logs(_ops: dict = Depends(require_ops)):
    """按当前保留天数清理过期日志（手动触发）。"""
    days = int(settings_svc.get_settings().get("log_retention_days") or 30)
    n = log_svc.purge_old(days)
    return {"success": True, "deleted": n}


# ---------------------------------------------------------------------------
# 安全审计中心：规则 + 配置
# ---------------------------------------------------------------------------

@router.get("/rules")
async def list_rules(_ops: dict = Depends(require_ops)):
    """内置安全规则清单（含当前启用状态）+ 分类标签。"""
    settings = settings_svc.get_settings()
    disabled = set(settings.get("disabled_rules") or [])
    rules = security.list_builtin_rules()
    for r in rules:
        r["enabled"] = r["id"] not in disabled
        # 检测开关关闭时，该门控下的规则实际不生效
        gate = r.get("gate")
        r["effective"] = r["enabled"] and (not gate or bool(int(settings.get(gate, 1) or 0)))
    return {"success": True, "rules": rules,
            "category_labels": security.CATEGORY_LABELS,
            "audit_modes": list(security.AUDIT_MODES)}


@router.get("/settings")
async def get_settings(_ops: dict = Depends(require_ops)):
    return {"success": True, "settings": settings_svc.get_settings()}


@router.put("/settings")
async def save_settings(body: SettingsBody, _ops: dict = Depends(require_ops)):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    return {"success": True, "settings": settings_svc.save_settings(patch)}


@router.post("/scan-preview")
async def scan_preview(payload: dict, _ops: dict = Depends(require_ops)):
    """安全审计试跑：对给定文本/结构即时扫描，返回命中风险（不入库、不转发）。"""
    text = payload.get("text")
    settings = settings_svc.get_settings()
    if text is not None:
        result = security.scan_text(str(text), settings)
        return {"success": True, **result}
    obj = payload.get("body") or {}
    _, findings, risk_level, risk_score = security.scan_payload(obj, settings, do_mask=False)
    return {"success": True, "findings": findings,
            "risk_level": risk_level, "risk_score": risk_score}
