"""固定模板只读 API 执行器：公网 DNS 绑定、逐跳校验、限额与最小字段映射。"""
import asyncio
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import aiohttp

from app.core.json_schema import check_json, validate
from app.services.personal_tool_specs import assert_result, canonical, pointer
from app.services.url_fetch import PublicResolver, FetchBudget, bounded_body, normalize_url, ssl_context, UrlImportError


class ToolExecutionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ToolExecutionError("invalid_response")
        out[key] = value
    return out


def provider_ttl(headers):
    value = headers.get("Cache-Control", "").lower()
    if "no-store" in value or "no-cache" in value or "private" in value:
        return 0
    match = re.search(r"(?:^|,)\s*max-age=(\d+)", value)
    try:
        return max(0, int(match[1]) - max(0, int(headers.get("Age", "0")))) if match else 0
    except ValueError:
        return 0


async def request_json(url, *, method="GET", params=None, body=None, headers=None):
    endpoint = normalize_url(url)
    allowed = urlsplit(endpoint)
    if allowed.scheme != "https" or allowed.query or method not in ("GET", "POST"):
        raise ToolExecutionError("unsafe_template")
    budget = FetchBudget(deadline=time.monotonic() + 10)
    current = endpoint
    outgoing = {"Accept": "application/json", "Accept-Encoding": "gzip, deflate", "User-Agent": "Jianzirumian-ReadOnly-Connector/1"}
    outgoing.update(headers or {})
    try:
        async with asyncio.timeout(10):
            for hop in range(4):
                target = urlsplit(normalize_url(current))
                if (target.scheme, target.netloc, target.path) != (allowed.scheme, allowed.netloc, allowed.path):
                    raise ToolExecutionError("redirect_not_approved")
                connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False, force_close=True)
                async with aiohttp.ClientSession(connector=connector, trust_env=False, auto_decompress=False,
                        cookie_jar=aiohttp.DummyCookieJar(), timeout=aiohttp.ClientTimeout(total=10)) as client:
                    async with client.request(method, current, params=params, json=body, headers=outgoing,
                                              ssl=ssl_context(), allow_redirects=False) as response:
                        if response.status in (301, 302, 303, 307, 308):
                            location = response.headers.get("Location")
                            if not location or hop == 3 or (method == "POST" and response.status not in (307, 308)):
                                raise ToolExecutionError("redirect_not_approved")
                            next_url = normalize_url(urljoin(str(response.url), location))
                            # 不允许重定向改写获准参数或把凭据带往另一接口。
                            if urlsplit(next_url).query != urlsplit(str(response.url)).query:
                                raise ToolExecutionError("redirect_not_approved")
                            current, params = next_url, None
                            continue
                        if response.status != 200:
                            raise ToolExecutionError("upstream_http_error")
                        mime = response.headers.get("Content-Type", "").split(";")[0].lower().strip()
                        if mime != "application/json" and not mime.endswith("+json"):
                            raise ToolExecutionError("invalid_content_type")
                        raw = await bounded_body(response, budget, 2 * 1024 * 1024)
                        value = json.loads(raw, object_pairs_hook=_pairs)
                        check_json(value, max_bytes=2 * 1024 * 1024)
                        if not isinstance(value, dict):
                            raise ToolExecutionError("invalid_response")
                        return value, provider_ttl(response.headers)
    except asyncio.CancelledError:
        raise
    except ToolExecutionError:
        raise
    except UrlImportError as exc:
        raise ToolExecutionError(exc.code) from None
    except (TimeoutError, aiohttp.ClientError, OSError):
        raise ToolExecutionError("upstream_unavailable") from None
    except (ValueError, TypeError, RecursionError):
        raise ToolExecutionError("invalid_response") from None
    raise ToolExecutionError("redirect_not_approved")


def _mapped(mapping, arguments):
    output = {}
    for field, entry in mapping.items():
        if "input" in entry and entry["input"] not in arguments:
            continue
        value = arguments[entry["input"]] if "input" in entry else entry["literal"]
        if not isinstance(value, (str, int, float, bool)):
            raise ToolExecutionError("invalid_parameter_type")
        output[field] = value
    return output


async def execute(spec, arguments, secret=""):
    validate(spec["input_schema"], arguments, max_bytes=65536)
    config, kind = spec["config"], spec["executor"]
    meta = {"fetched_at": datetime.now(timezone.utc).isoformat(), "observed_at": None, "published_at": None}
    ttl = 0
    if kind == "http":
        params, body = _mapped(config.get("query", {}), arguments), _mapped(config.get("body", {}), arguments)
        headers, auth = {}, config.get("auth", {})
        if auth:
            if not secret:
                raise ToolExecutionError("credentials_required")
            if auth["kind"] == "bearer":
                headers["Authorization"] = "Bearer " + secret
            elif auth["kind"] == "header":
                headers[auth["name"]] = secret
            else:
                params[auth["name"]] = secret
        raw, _ = await request_json(config["url"], method=config["method"], params=params,
                                    body=body if config["method"] == "POST" else None, headers=headers)
        output = {field: pointer(raw, path) for field, path in config["output_map"].items()}
        meta["sources"] = [{"title": spec["name"], "url": config["url"]}]
    elif kind == "connector":
        connector = config["connector"]
        if connector == "news_search":
            from app.services.web_search import web_search
            async with asyncio.timeout(10):
                results = await web_search(arguments["query"], max_results=5)
            output = {"results": [{"title": r.title, "url": normalize_url(r.url), "snippet": r.snippet[:2000],
                                    "published_at": None} for r in results]}
            meta["sources"] = [{"title": r["title"], "url": r["url"], "published_at": None} for r in output["results"]]
            meta["entity"] = arguments["query"]
        elif connector == "weather":
            zone = ZoneInfo(arguments["timezone"])
            url = "https://api.open-meteo.com/v1/forecast"
            raw, cap = await request_json(url, params={**arguments, "current": "temperature_2m,weather_code", "temperature_unit": "celsius"})
            current, units = raw.get("current", {}), raw.get("current_units", {})
            when = datetime.fromisoformat(current.get("time", ""))
            when = when.replace(tzinfo=zone) if when.tzinfo is None else when.astimezone(zone)
            if (raw.get("timezone") != arguments["timezone"] or units.get("temperature_2m") != "°C"
                    or abs((datetime.now(timezone.utc) - when).total_seconds()) > 3600
                    or abs(raw.get("latitude", 999) - arguments["latitude"]) > .25
                    or abs(raw.get("longitude", 999) - arguments["longitude"]) > .25):
                raise ToolExecutionError("data_mismatch")
            output = {"latitude": raw["latitude"], "longitude": raw["longitude"], "timezone": raw["timezone"],
                      "valid_at": when.isoformat(), "temperature": current.get("temperature_2m"),
                      "unit": units.get("temperature_2m"), "weather_code": current.get("weather_code")}
            meta.update(timezone=raw["timezone"], unit="°C", valid_at=when.isoformat(),
                        entity={k: arguments[k] for k in ("latitude", "longitude")},
                        sources=[{"title": "Open-Meteo 天气模型数据 · CC-BY 4.0", "url": url}])
            ttl = min(300, cap)
        elif connector == "elevation":
            url = "https://api.open-meteo.com/v1/elevation"
            raw, _ = await request_json(url, params=arguments)
            values = raw.get("elevation")
            if not isinstance(values, list) or len(values) != 1:
                raise ToolExecutionError("data_mismatch")
            output = {**arguments, "elevation": values[0], "unit": "m"}
            meta.update(unit="m", entity=dict(arguments), sources=[{"title": "Open-Meteo 地形高程 · CC-BY 4.0", "url": url}])
            # 响应没有可绑定的数据集版本时不启用长期缓存。
        else:
            raise ToolExecutionError("unsupported_connector")
    else:
        raise ToolExecutionError("sandbox_unavailable")
    validate(spec["output_schema"], output)
    assert_result(output, arguments, spec.get("assertions", []))
    if secret and secret in canonical(output):
        raise ToolExecutionError("credential_echo_blocked")
    meta["fetched_at"] = datetime.now(timezone.utc).isoformat()
    # HTTP 通用字段或搜索摘要通过 Schema 并不证明本轮需求已满足。
    satisfaction = "satisfied" if kind == "connector" and config["connector"] in ("weather", "elevation") else "unverified"
    return {"output": output, **meta, "cache_ttl": ttl, "execution_status": "completed",
            "validation_status": "passed", "requirement_status": satisfaction}
