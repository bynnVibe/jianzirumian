"""工具结果仅按精确参数和凭据作用域复用；未知连接器、无供应商有效期默认不缓存。"""
import json
from datetime import datetime, timedelta, timezone

from app.core import db
from app.core.json_schema import check_json, validate
from app.services.personal_tool_specs import SCAN_VERSION, fingerprint


def key(row, arguments):
    return fingerprint({"owner": row["owner_id"], "tool": row["id"], "version": row["version"],
                        "credential_scope": row["credential_id"], "terms": row["terms_accepted"],
                        "scan": SCAN_VERSION, "arguments": arguments})


def permitted(spec):
    # 海拔响应不含稳定数据集版本，搜索源/地理编码缺少供应商有效期，暂不启用其结果缓存。
    return spec["executor"] == "connector" and spec["config"].get("connector") in ("weather", "weather_forecast")


def get(row, spec, arguments):
    if not permitted(spec):
        return None
    cache_key = key(row, arguments)
    item = db.query_one("SELECT * FROM tool_result_cache WHERE cache_key=? AND owner_id=?", (cache_key, row["owner_id"]))
    if not item:
        return None
    try:
        created, expires = datetime.fromisoformat(item["generated_at"]), datetime.fromisoformat(item["expires_at"])
        current = datetime.now(timezone.utc)
        if not created.tzinfo or not expires.tzinfo or not created <= current < expires or not 0 < (expires - created).total_seconds() <= 300:
            return None
        value = json.loads(item["result_json"])
        check_json(value)
        validate(spec["output_schema"], value["output"])
        if any(value.get(k) != expected for k, expected in (("execution_status", "completed"),
                ("validation_status", "passed"), ("requirement_status", "satisfied"))):
            return None
        db.execute("UPDATE tool_result_cache SET hit_count=hit_count+1,last_access_at=? WHERE cache_key=?",
                   (current.isoformat(), cache_key))
        return {**value, "cache": {"hit": True, "generated_at": item["generated_at"], "expires_at": item["expires_at"]}}
    except (ValueError, TypeError, KeyError):
        return None


def put(row, spec, arguments, value):
    ttl = value.get("cache_ttl", 0)
    if not permitted(spec) or type(ttl) not in (int, float) or not 0 < ttl <= 300:
        return
    if any(value.get(k) != expected for k, expected in (("execution_status", "completed"),
            ("validation_status", "passed"), ("requirement_status", "satisfied"))):
        return
    check_json(value)
    current = datetime.now(timezone.utc)
    expiry = (current + timedelta(seconds=ttl)).isoformat()
    db.execute("INSERT INTO tool_result_cache(cache_key,owner_id,tool_id,version,result_json,generated_at,expires_at,last_access_at) "
               "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET result_json=excluded.result_json,generated_at=excluded.generated_at,"
               "expires_at=excluded.expires_at,last_access_at=excluded.last_access_at",
               (key(row, arguments), row["owner_id"], row["id"], row["version"], json.dumps(value, ensure_ascii=False),
                current.isoformat(), expiry, current.isoformat()))
    db.execute("DELETE FROM tool_result_cache WHERE owner_id=? AND (expires_at<=? OR cache_key NOT IN "
               "(SELECT cache_key FROM tool_result_cache WHERE owner_id=? ORDER BY last_access_at DESC LIMIT 100))",
               (row["owner_id"], current.isoformat(), row["owner_id"]))
