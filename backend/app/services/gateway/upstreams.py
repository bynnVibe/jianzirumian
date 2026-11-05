"""
见字如面 - 模型网关「上游管理」（璇玑式）

上游 = 一个可出网的模型服务端点，自身持有：协议 / Base URL / API Key / 成本层级 /
模型清单 / 权重 / 健康度 / 延迟。路由规则（rules.py）按模型模式选中一组上游并做负载均衡；
本模块只负责上游实体的 CRUD、生效出网配置解析、连通性诊断与健康/延迟维护。

- tier=paid|free|subscription；tier=free 且未填 Base URL/Key 时回退系统 OpenRouter 配置。
- 模型清单（models）为该上游可服务的模型名列表；请求命中规则后按原名出网（无模型映射）。
- 健康度：连续失败 >=3 次熔断为 down，1~2 次 degraded，成功一次恢复 healthy 并 EMA 更新延迟。
"""
import json
import logging
import time
import uuid
from datetime import datetime
from typing import List, Optional

import httpx

from app.core import db

logger = logging.getLogger("jianziruyang.gateway.upstreams")

VALID_PROTOCOLS = ("openai", "anthropic", "ollama")
TIERS = ("paid", "free", "subscription")
# 上游分类：llm(语言模型，默认) | ocr | embedding | rerank。
# 多模态上游（OCR/Embedding/ReRank）从系统设置收纳进网关后按此分类；
# 业务路由按 role→category 过滤候选上游（rules.ROLE_CATEGORY），防跨类误选。
CATEGORIES = ("llm", "ocr", "embedding", "rerank")
CATEGORY_LABELS = {"llm": "LLM", "ocr": "OCR", "embedding": "Embedding", "rerank": "ReRank"}
# 连续失败达到该阈值即熔断为 down
_DOWN_THRESHOLD = 3
# 延迟 EMA 平滑系数（新样本权重）
_LATENCY_ALPHA = 0.3
_TEST_TIMEOUT = httpx.Timeout(connect=8, read=30, write=15, pool=5)


def _now() -> str:
    return datetime.now().isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _parse_json(raw, fallback):
    try:
        val = json.loads(raw or "")
        return val if isinstance(val, (list, dict)) else fallback
    except (json.JSONDecodeError, TypeError):
        return fallback


def _mask_secret(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}{'*' * 6}{secret[-4:]}"


def _normalize_models(value) -> List[str]:
    """模型清单归一化：支持列表或逗号分隔字符串（图2「模型列表」输入），去重去空。"""
    if isinstance(value, str):
        value = value.replace("，", ",").split(",")
    if not isinstance(value, list):
        return []
    seen: List[str] = []
    for m in value:
        s = str(m).strip()
        if s and s not in seen:
            seen.append(s)
    return seen


def _normalize_tier(value) -> str:
    s = (value or "").strip().lower()
    return s if s in TIERS else "paid"


def _normalize_category(value) -> str:
    s = (value or "").strip().lower()
    return s if s in CATEGORIES else "llm"


def _system_openrouter_conn() -> dict:
    """系统 OpenRouter 配置（系统设置 custom provider 指向 openrouter.ai）；未配置返回 {}。"""
    from app.services.config_manager import effective_provider_config
    try:
        cfg = effective_provider_config("llm", "custom") or {}
    except Exception as e:
        logger.warning("读取系统 OpenRouter 配置失败: %s", e)
        return {}
    if "openrouter.ai" not in (cfg.get("base_url") or ""):
        return {}
    return {"base_url": (cfg.get("base_url") or "").rstrip("/"),
            "api_key": (cfg.get("api_key") or "").strip()}


# ---------------------------------------------------------------------------
# 生效出网配置解析 + URL 拼装
# ---------------------------------------------------------------------------

def effective(up: dict) -> dict:
    """上游生效出网配置（proxy 出网与测试连接的唯一解析出口）。

    tier=free 且未填 Base URL/Key 时回退系统 OpenRouter 配置（Base URL 再兜底官方地址）。
    返回 {protocol, base_url, api_key, name}，供协议适配层 build_request/build_url 使用。
    """
    from app.core import openrouter as or_svc
    protocol = (up.get("protocol") or "openai").strip().lower()
    base_url = (up.get("base_url") or "").strip().rstrip("/")
    api_key = (up.get("api_key") or "").strip()
    tier = (up.get("tier") or "paid").strip().lower()
    if tier == "free" and (not base_url or not api_key):
        sys_conn = _system_openrouter_conn()
        if not base_url:
            base_url = (sys_conn.get("base_url") or or_svc.OPENROUTER_BASE_URL).rstrip("/")
        if not api_key:
            api_key = sys_conn.get("api_key") or ""
    return {"protocol": protocol, "base_url": base_url, "api_key": api_key,
            "name": up.get("name") or ""}


def build_upstream_url(eff: dict, kind: str = "chat") -> str:
    """按协议与请求类型拼装上游请求 URL。

    kind: chat（默认）/ embeddings / rerank。embeddings、rerank 为透传型 JSON 端点，
    与 chat 通道隔离（chat 主链路 URL 拼装零改动）。
    """
    base = (eff.get("base_url") or "").rstrip("/")
    protocol = eff.get("protocol") or "openai"
    if kind == "embeddings":
        # ollama 走 /api/embeddings；openai 兼容走 /embeddings（base 可已含 /v1）
        if protocol == "ollama":
            return base if base.endswith("/api/embeddings") else f"{base}/api/embeddings"
        return base if base.endswith("/embeddings") else f"{base}/embeddings"
    if kind == "rerank":
        # 镜像原生 reranker：OpenAI 兼容 /reranks
        return base if base.endswith("/reranks") else f"{base}/reranks"
    if protocol == "ollama":
        return f"{base}/api/chat"
    if protocol == "anthropic":
        return f"{base}/v1/messages" if not base.endswith("/messages") else base
    # openai 兼容：允许 base_url 已含 /v1 或 /chat/completions
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def _row_to_dict(row: dict, with_secret: bool = False) -> dict:
    models = _parse_json(row.get("models"), [])
    out = {
        "id": row["id"],
        "name": row["name"],
        "protocol": row.get("protocol") or "openai",
        "base_url": row.get("base_url") or "",
        "tier": row.get("tier") or "paid",
        "category": row.get("category") or "llm",
        "models": models,
        "model_count": len(models),
        "weight": int(row.get("weight") or 100),
        "enabled": bool(row.get("enabled")),
        "health": row.get("health") or "healthy",
        "fail_count": int(row.get("fail_count") or 0),
        "success_count": int(row.get("success_count") or 0),
        "last_error": row.get("last_error") or "",
        "latency_ms": int(row.get("latency_ms") or 0),
        "remark": row.get("remark") or "",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        # api_key 脱敏展示（仅保留尾 4 位），避免前端泄露上游密钥
        "api_key_masked": _mask_secret(row.get("api_key") or ""),
        "has_api_key": bool(row.get("api_key")),
    }
    if with_secret:
        out["api_key"] = row.get("api_key") or ""
    return out


def list_upstreams(category: Optional[str] = None) -> List[dict]:
    """上游列表；传 category 时仅返回该分类（llm/ocr/embedding/rerank）。"""
    cat = _normalize_category(category) if category else None
    if cat:
        rows = db.query("SELECT * FROM gateway_upstreams WHERE category=? ORDER BY created_at ASC",
                        (cat,))
    else:
        rows = db.query("SELECT * FROM gateway_upstreams ORDER BY created_at ASC")
    return [_row_to_dict(r) for r in rows]


def get_upstream(upstream_id: str, with_secret: bool = False) -> Optional[dict]:
    row = db.query_one("SELECT * FROM gateway_upstreams WHERE id=?", (upstream_id,))
    return _row_to_dict(row, with_secret) if row else None


def create_upstream(payload: dict) -> dict:
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("上游名称不能为空")
    protocol = (payload.get("protocol") or "openai").strip().lower()
    if protocol not in VALID_PROTOCOLS:
        raise ValueError(f"不支持的协议：{protocol}（可选 {'/'.join(VALID_PROTOCOLS)}）")
    tier = _normalize_tier(payload.get("tier"))
    base_url = (payload.get("base_url") or "").strip().rstrip("/")
    # free 层级允许空 Base URL（回退系统 OpenRouter）；付费/订阅需显式填写
    if tier != "free" and not base_url:
        raise ValueError("付费/订阅上游需填写 Base URL")
    models = _normalize_models(payload.get("models"))
    category = _normalize_category(payload.get("category"))
    uid = _new_id()
    db.execute(
        "INSERT INTO gateway_upstreams (id, name, protocol, base_url, api_key, remark,"
        " tier, category, models, weight, enabled, health, fail_count, success_count,"
        " last_error, latency_ms, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,'healthy',0,0,'',0,?,?)",
        (uid, name, protocol, base_url, (payload.get("api_key") or "").strip(),
         (payload.get("remark") or "").strip(), tier, category,
         json.dumps(models, ensure_ascii=False), max(1, int(payload.get("weight") or 100)),
         1 if payload.get("enabled", True) else 0, _now(), _now()),
    )
    return get_upstream(uid)


def update_upstream(upstream_id: str, patch: dict) -> Optional[dict]:
    cur = db.query_one("SELECT * FROM gateway_upstreams WHERE id=?", (upstream_id,))
    if not cur:
        return None
    name = (patch.get("name") or cur["name"]).strip()
    protocol = (patch.get("protocol") or cur["protocol"]).strip().lower()
    if protocol not in VALID_PROTOCOLS:
        raise ValueError(f"不支持的协议：{protocol}（可选 {'/'.join(VALID_PROTOCOLS)}）")
    tier = _normalize_tier(patch.get("tier") if "tier" in patch else cur.get("tier"))
    category = _normalize_category(
        patch.get("category") if "category" in patch else cur.get("category"))
    base_url = (patch.get("base_url", cur["base_url"]) or "").strip().rstrip("/")
    if tier != "free" and not base_url:
        raise ValueError("付费/订阅上游需填写 Base URL")
    # api_key 为空字符串表示不修改（前端脱敏展示不回传明文）
    api_key = cur["api_key"]
    if "api_key" in patch and str(patch.get("api_key") or "").strip():
        api_key = str(patch["api_key"]).strip()
    models = _normalize_models(patch["models"]) if "models" in patch \
        else _parse_json(cur["models"], [])
    weight = max(1, int(patch.get("weight", cur["weight"]) or 100))
    enabled = 1 if patch.get("enabled", bool(cur["enabled"])) else 0
    remark = (patch.get("remark", cur["remark"]) or "").strip()
    db.execute(
        "UPDATE gateway_upstreams SET name=?, protocol=?, base_url=?, api_key=?, remark=?,"
        " tier=?, category=?, models=?, weight=?, enabled=?, updated_at=? WHERE id=?",
        (name, protocol, base_url, api_key, remark, tier, category,
         json.dumps(models, ensure_ascii=False), weight, enabled, _now(), upstream_id),
    )
    return get_upstream(upstream_id)


def clone_upstream(upstream_id: str) -> Optional[dict]:
    """克隆上游（图1「克隆」操作）：复制连接配置与模型清单，健康度重置，名称加「副本」。"""
    src = db.query_one("SELECT * FROM gateway_upstreams WHERE id=?", (upstream_id,))
    if not src:
        return None
    uid = _new_id()
    db.execute(
        "INSERT INTO gateway_upstreams (id, name, protocol, base_url, api_key, remark,"
        " tier, category, models, weight, enabled, health, fail_count, success_count,"
        " last_error, latency_ms, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,'healthy',0,0,'',0,?,?)",
        (uid, f"{src['name']} 副本", src.get("protocol") or "openai",
         src.get("base_url") or "", src.get("api_key") or "", src.get("remark") or "",
         src.get("tier") or "paid", src.get("category") or "llm", src.get("models") or "[]",
         int(src.get("weight") or 100), 1, _now(), _now()),
    )
    return get_upstream(uid)


def delete_upstream(upstream_id: str) -> bool:
    ok = db.execute("DELETE FROM gateway_upstreams WHERE id=?", (upstream_id,)) > 0
    if ok:
        # 从所有路由规则的 upstreams 列表中剔除该上游，避免悬空引用
        for r in db.query("SELECT id, upstreams FROM gateway_rules"):
            ups = _parse_json(r.get("upstreams"), [])
            if upstream_id in ups:
                kept = [u for u in ups if u != upstream_id]
                db.execute("UPDATE gateway_rules SET upstreams=?, updated_at=? WHERE id=?",
                           (json.dumps(kept, ensure_ascii=False), _now(), r["id"]))
    return ok


# ---------------------------------------------------------------------------
# 健康度 + 延迟
# ---------------------------------------------------------------------------

def mark_success(upstream_id: str, latency_ms: int = 0) -> None:
    row = db.query_one("SELECT latency_ms FROM gateway_upstreams WHERE id=?", (upstream_id,))
    if not row:
        return
    prev = int(row.get("latency_ms") or 0)
    lat = prev
    if latency_ms and latency_ms > 0:
        lat = int(prev * (1 - _LATENCY_ALPHA) + latency_ms * _LATENCY_ALPHA) if prev \
            else int(latency_ms)
    db.execute(
        "UPDATE gateway_upstreams SET fail_count=0, health='healthy',"
        " success_count=success_count+1, last_error='', latency_ms=?, updated_at=? WHERE id=?",
        (lat, _now(), upstream_id),
    )


def mark_failure(upstream_id: str, error: str = "") -> None:
    row = db.query_one("SELECT fail_count FROM gateway_upstreams WHERE id=?", (upstream_id,))
    if not row:
        return
    fails = int(row.get("fail_count") or 0) + 1
    health = "down" if fails >= _DOWN_THRESHOLD else "degraded"
    db.execute(
        "UPDATE gateway_upstreams SET fail_count=?, health=?, last_error=?, updated_at=?"
        " WHERE id=?",
        (fails, health, (error or "")[:300], _now(), upstream_id),
    )
    if health == "down":
        logger.warning("上游 %s 连续失败 %d 次，已熔断(down)", upstream_id, fails)


def reset_health(upstream_id: str) -> None:
    db.execute(
        "UPDATE gateway_upstreams SET fail_count=0, health='healthy', last_error='', updated_at=?"
        " WHERE id=?", (_now(), upstream_id),
    )


# ---------------------------------------------------------------------------
# 聚合模型清单 + OpenRouter 免费模型
# ---------------------------------------------------------------------------

def all_model_names(category: Optional[str] = None) -> List[str]:
    """汇总启用上游的模型清单（供 /v1/models 与前端下拉/规则模型提示）；可按 category 过滤。"""
    cat = _normalize_category(category) if category else None
    if cat:
        rows = db.query("SELECT models FROM gateway_upstreams WHERE enabled=1 AND category=?",
                        (cat,))
    else:
        rows = db.query("SELECT models FROM gateway_upstreams WHERE enabled=1")
    seen = set()
    for r in rows:
        for m in _parse_json(r.get("models"), []):
            if str(m).strip():
                seen.add(str(m).strip())
    return sorted(seen)


async def fetch_openrouter_free() -> List[str]:
    """实时拉取 OpenRouter 免费模型 id 列表（失败返回空列表）。"""
    from app.core import openrouter
    return await openrouter.fetch_free_model_ids()


# ---------------------------------------------------------------------------
# 存量网络 provider 种子化：系统设置已配置的网络 OCR/Embedding/ReRank → 网关上游
# ---------------------------------------------------------------------------

# (config 类别, provider, 上游 protocol, 业务 role)；仅网络 provider。
# local 引擎（ocr local / rerank local）是进程内运行、非网络端点，不能成为网关上游，不在此列。
_SEED_NETWORK_PROVIDERS = (
    ("embedding", "ollama", "ollama", "embed"),
    ("embedding", "openai", "openai", "embed"),
    ("embedding", "custom", "openai", "embed"),
    ("rerank", "dashscope", "openai", "rerank"),
    ("ocr", "aliyun", "openai", "ocr"),
    ("ocr", "custom_api", "openai", "ocr"),
)
_seeded = False


def _seed_conn(cfg: dict) -> dict:
    """从生效配置抽取连接字段（provider 字段名不一，统一为 base_url/api_key/model）。"""
    return {
        "base_url": (cfg.get("base_url") or cfg.get("api_url") or "").strip().rstrip("/"),
        "api_key": (cfg.get("api_key") or "").strip(),
        "model": (cfg.get("model") or "").strip(),
    }


def _ensure_rule_for_upstream(uid: str, pattern: str) -> str:
    """确保存在一条启用规则包含该上游：有则复用首条，无则新建（专用规则，不污染默认 LLM 规则）。"""
    from app.services.gateway import rules as rule_svc
    for r in rule_svc.list_rules():
        if r.get("enabled") and uid in (r.get("upstreams") or []):
            return r["id"]
    rule = rule_svc.create_rule({
        "model_pattern": (pattern or "*").strip() or "*",
        "upstreams": [uid], "lb_strategy": "weighted", "priority": 0,
        "is_default": False, "allowed_users": [], "enabled": True,
    })
    return rule["id"]


def seed_network_providers_from_settings(force: bool = False) -> int:
    """把系统设置里「当前生效」的网络 OCR/Embedding/ReRank provider 幂等迁入网关上游，
    并为其建专用路由规则 + 绑定业务路由（role），保证「网关唯一来源」不丢配置且开箱可用。

    - 幂等：同 name+category 已存在则复用上游；role 已绑定则不覆盖；进程内只跑一次。
    - 只迁当前生效 provider（embedding/rerank/ocr 各一），避免为未启用的空配置建垃圾上游。
    - local 引擎（ocr local / rerank local）非网络端点，不迁入。
    返回新建上游数；异常不抛（种子化失败不应阻断网关启动）。
    """
    global _seeded
    if _seeded and not force:
        return 0
    _seeded = True
    try:
        from app.config import settings as app_settings
        from app.services.config_manager import effective_provider_config, runtime_config
        from app.services.gateway import rules as rule_svc
        active = {
            "embedding": runtime_config.embedding_provider or app_settings.EMBEDDING_PROVIDER,
            "rerank": runtime_config.rerank_provider or app_settings.RERANKER_PROVIDER,
            "ocr": runtime_config.ocr_provider or app_settings.OCR_PROVIDER,
        }
        created = 0
        for category, provider, protocol, role in _SEED_NETWORK_PROVIDERS:
            if active.get(category) != provider:
                continue  # 只迁当前生效的网络 provider
            conn = _seed_conn(effective_provider_config(category, provider))
            if not conn["base_url"]:
                continue  # 未配置连接地址（如 custom_api 无 api_url）→ 跳过
            name = f"{CATEGORY_LABELS.get(category, category)} · {provider}"
            row = db.query_one(
                "SELECT id FROM gateway_upstreams WHERE name=? AND category=?", (name, category))
            if row:
                uid = row["id"]
            else:
                uid = create_upstream({
                    "name": name, "protocol": protocol, "base_url": conn["base_url"],
                    "api_key": conn["api_key"], "tier": "paid",
                    "models": [conn["model"]] if conn["model"] else [],
                    "category": category, "enabled": True, "weight": 100,
                    "remark": f"由系统设置 {category}/{provider} 自动迁入",
                })["id"]
                created += 1
            rule_id = _ensure_rule_for_upstream(uid, conn["model"] or "*")
            cur = rule_svc.get_role_route(role) if role else None
            if role and rule_id and not ((cur or {}).get("rule_id") or "").strip():
                rule_svc.upsert_role_route(role, rule_id=rule_id, model_name=conn["model"] or "")
        if created:
            logger.info("[网关种子化] 从系统设置迁入 %d 个网络多模态上游", created)
        return created
    except Exception as e:
        logger.warning("[网关种子化] 迁入网络 provider 失败（忽略，可稍后手动配置）: %s", e)
        return 0


# ---------------------------------------------------------------------------
# 连通性诊断：对生效配置真实发一次最小 chat 请求
# ---------------------------------------------------------------------------

# 上游分类 → 连通性探测的请求类型与最小请求体。
# embedding/rerank 上游的 Base URL 只暴露 /embeddings、/reranks 端点，不接受
# /chat/completions；若仍按 chat 探测会得到 400/404（改造前系统设置用的是各自原生客户端）。
_PROBE_KIND = {"embedding": "embeddings", "rerank": "rerank"}


def _probe_body(kind: str, model: str) -> dict:
    """按探测类型构造最小请求体（与真实出网 payload 同形态）。"""
    if kind == "embeddings":
        return {"model": model, "input": ["ping"]}
    if kind == "rerank":
        return {"model": model, "query": "ping", "documents": ["pong"]}
    return {"model": model, "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8, "temperature": 0, "stream": False}


async def _probe(eff: dict, model: str, category: str = "llm") -> dict:
    """对生效配置发一次最小请求，返回单模型探测结果（不更新健康度）。

    按上游分类选择请求类型：embedding→/embeddings、rerank→/reranks、其余→chat。
    401/403 归因鉴权、404 归因模型/端点不存在、429 归因限流，其余非 2xx 附上游响应预览。
    """
    kind = _PROBE_KIND.get((category or "llm").strip().lower(), "chat")
    url = build_upstream_url(eff, kind)
    body = _probe_body(kind, model)
    t0 = time.monotonic()
    try:
        from app.services.gateway import upstream as transport
        headers, payload = transport.build_request(eff, body, model, stream=False, kind=kind)
        async with httpx.AsyncClient(timeout=_TEST_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as e:
        return {"model": model, "success": False,
                "message": f"连接失败：{e}（请检查 Base URL 是否可达、协议是否匹配）"}

    sc = resp.status_code
    latency = int((time.monotonic() - t0) * 1000)
    preview = (resp.text or "")[:200]
    if sc < 400:
        note = "" if eff["api_key"] else "（未配置上游 API Key，该上游未强制鉴权）"
        return {"model": model, "success": True, "status_code": sc, "latency_ms": latency,
                "preview": preview,
                "message": f"上游返回 HTTP {sc}，模型 {model} 可正常应答，延迟 {latency}ms{note}"}
    if sc in (401, 403):
        key_hint = "（当前未配置上游 API Key，请在编辑中填写）" if not eff["api_key"] \
            else "（请检查上游 API Key 是否有效）"
        msg = f"鉴权失败：上游返回 HTTP {sc}{key_hint}"
    elif sc == 404:
        endpoint = {"embeddings": "/embeddings", "rerank": "/reranks"}.get(kind, "/chat/completions")
        msg = (f"端点或模型不可用：上游返回 HTTP 404。请确认 Base URL 提供 {endpoint} 端点，"
               f"且模型 {model} 存在并已开通")
    elif sc == 429:
        msg = (f"上游限流 HTTP 429：配置完整且鉴权通过，但 {model} 当前被限流，"
               "稍后重试或更换模型")
    else:
        msg = f"调用失败：上游返回 HTTP {sc}"
    return {"model": model, "success": False, "status_code": sc, "message": msg,
            "preview": preview}


async def test_connection(upstream_id: str, model: str = "") -> dict:
    """逐模型探测上游生效配置（验证配置完整性/连通性/可调用性，可能产生上游费用）。

    缺省对模型清单中每个模型各发一次最小 chat 请求（指定 model 时仅测该模型），
    任一模型成功即视为上游可达并更新健康度与延迟，全部失败记一次熔断计数。
    返回 {"success", "message", "results": [逐模型结果], ...}；不抛异常。
    """
    up = get_upstream(upstream_id, with_secret=True)
    if not up:
        raise ValueError("上游不存在")
    eff = effective(up)
    target = (model or "").strip()
    targets = [target] if target else list(up.get("models") or [])
    missing = []
    if not eff["base_url"]:
        missing.append("Base URL")
    if not targets:
        missing.append("模型（请在模型列表中至少填写一个）")
    if missing:
        return {"success": False, "results": [],
                "message": f"配置不全：缺少 {'、'.join(missing)}，请在编辑中补全后再测试"}

    results = [await _probe(eff, m, up.get("category") or "llm") for m in targets]
    ok = [r for r in results if r["success"]]
    if ok:
        lat = [r.get("latency_ms") or 0 for r in ok]
        mark_success(upstream_id, int(sum(lat) / len(lat)))
    else:
        mark_failure(upstream_id, results[0].get("message") or "测试失败")
    all_ok = len(ok) == len(results)
    message = (f"共测试 {len(results)} 个模型，全部成功" if all_ok
               else f"共测试 {len(results)} 个模型：{len(ok)} 个成功、"
                    f"{len(results) - len(ok)} 个失败")
    return {"success": all_ok, "ok_count": len(ok), "total": len(results),
            "results": results, "message": message}
