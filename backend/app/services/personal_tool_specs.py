"""个人工具声明审查；声明不等于权限，未知 API 与脚本必须由管理员审批版本。"""
import ast
import hashlib
import json
import re
from urllib.parse import parse_qsl, urlsplit

from app.core.json_schema import check_json, checked_schema, validate
from app.services.url_fetch import normalize_url

SCAN_VERSION = "personal-tools-2"
_SENSITIVE_FIELD = re.compile(r"(?:password|passwd|secret|authorization|cookie|(?:api|access|refresh|auth)[_-]?(?:key|token)|credential)", re.I)
_SECRET_VALUE = re.compile(r"(?:\bBearer\s+[A-Za-z0-9._~-]{8,}|\bsk-[A-Za-z0-9_-]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)", re.I)


def check_no_credentials(value):
    """声明和模型入参不得携带凭据；秘密仅来自受保护表单。"""
    if isinstance(value, dict):
        for key, child in value.items():
            if _SENSITIVE_FIELD.search(key):
                raise ValueError("凭据字段不能出现在工具声明或模型参数中，请使用受保护凭据表单")
            check_no_credentials(child)
    elif isinstance(value, list):
        for child in value:
            check_no_credentials(child)
    elif isinstance(value, str) and _SECRET_VALUE.search(value):
        raise ValueError("检测到凭据内容，未保存工具声明")


def check_pointer(path):
    if not isinstance(path, str) or len(path) > 240 or (path and not path.startswith("/")) or re.search(r"~(?![01])", path):
        raise ValueError("无效 JSON Pointer")
SAFE_IMPORTS = frozenset({"math", "statistics", "decimal", "fractions", "json", "datetime", "calendar",
                          "collections", "itertools", "functools", "operator", "re", "string", "unicodedata"})


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def version_fingerprint(spec):
    """抓取时间属于事件元信息；内容、来源和全部执行权限仍参与版本。"""
    versioned = dict(spec)
    if "provenance" in versioned:
        versioned["provenance"] = [{k: v for k, v in item.items() if k != "fetched_at"}
                                  for item in spec["provenance"]]
    return fingerprint(versioned)


def object_schema(properties, required=None):
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required,
            "additionalProperties": False}


def connector_spec(kind):
    coordinates = {"latitude": {"type": "number", "minimum": -90, "maximum": 90},
                   "longitude": {"type": "number", "minimum": -180, "maximum": 180}}
    if kind == "news_search":
        return {"name": "新闻资料搜索", "description": "搜索新闻资料并提供来源；摘要不保证包含发布时间，不等同实时事实核验。",
                "executor": "connector", "config": {"connector": kind},
                "input_schema": object_schema({"query": {"type": "string", "minLength": 2, "maxLength": 200}}),
                "output_schema": object_schema({"results": {"type": "array", "minItems": 1, "maxItems": 5,
                    "items": object_schema({"title": {"type": "string", "minLength": 1}, "url": {"type": "string", "format": "uri"},
                                             "snippet": {"type": "string"}, "published_at": {"type": ["string", "null"]}})}}),
                "tests": [{"input": {"query": "科技 新闻"}, "assertions": [{"path": "/results/0/url", "op": "exists"}]}],
                "sources": [], "limitations": "复用系统已配置搜索源；没有发布时间时明确未知。"}
    if kind not in ("weather", "elevation"):
        raise ValueError("未审核的内置连接器")
    if kind == "weather":
        coordinates["timezone"] = {"type": "string", "minLength": 3, "maxLength": 80}
        output = object_schema({"latitude": {"type": "number"}, "longitude": {"type": "number"},
            "timezone": {"type": "string"}, "valid_at": {"type": "string", "minLength": 10},
            "temperature": {"type": "number", "minimum": -100, "maximum": 70},
            "unit": {"const": "°C"}, "weather_code": {"type": "integer", "minimum": 0, "maximum": 99}})
        sample = {"latitude": 22.5431, "longitude": 114.0579, "timezone": "Asia/Shanghai"}
        assertions = [{"path": "/unit", "op": "equals", "value": "°C"},
                      {"path": "/timezone", "op": "input", "input": "/timezone"}]
    else:
        output = object_schema({"latitude": {"type": "number"}, "longitude": {"type": "number"},
            "elevation": {"type": "number", "minimum": -500, "maximum": 10000}, "unit": {"const": "m"}})
        sample = {"latitude": 22.5431, "longitude": 114.0579}
        assertions = [{"path": "/unit", "op": "equals", "value": "m"},
                      {"path": "/latitude", "op": "input", "input": "/latitude"}]
    return {"name": "当前天气" if kind == "weather" else "坐标海拔", "description": "按明确坐标读取天气或地形高程，保留单位和来源。",
            "executor": "connector", "config": {"connector": kind}, "input_schema": object_schema(coordinates),
            "output_schema": output, "tests": [{"input": sample, "assertions": assertions}],
            "sources": ["https://open-meteo.com/en/docs" if kind == "weather" else "https://open-meteo.com/en/docs/elevation-api",
                        "https://open-meteo.com/en/terms"],
            "limitations": "Open-Meteo 免费接口仅限非商业用途，须遵守 CC-BY 4.0 署名和限额。天气为模型数据适用时刻，不冒充现场观测。"}


def pointer(value, path):
    check_pointer(path)
    for part in path.split("/")[1:] if path else []:
        part = part.replace("~1", "/").replace("~0", "~")
        try:
            value = value[int(part)] if isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", part) else value[part]
        except (KeyError, IndexError, ValueError, TypeError):
            raise ValueError("结果缺少声明字段") from None
    return value


def assert_result(output, arguments, assertions):
    for item in assertions:
        value = pointer(output, item["path"])
        op = item["op"]
        if op == "exists":
            ok = value is not None and value != "" and value != [] and value != {}
        elif op in ("equals", "input"):
            expected = pointer(arguments, item["input"]) if op == "input" else item["value"]
            ok = canonical(value) == canonical(expected)
        elif op == "range":
            ok = type(value) in (int, float) and item["min"] <= value <= item["max"]
        else:
            raise ValueError("不支持的验收断言")
        if not ok:
            raise ValueError("工具结果未满足验收断言")


def review(spec):
    check_json(spec, max_bytes=131072)
    if not isinstance(spec, dict) or set(spec) - {"name", "description", "executor", "config", "input_schema",
            "output_schema", "tests", "sources", "limitations", "assertions", "provenance"}:
        raise ValueError("工具声明包含未知字段")
    if _SECRET_VALUE.search(canonical(spec)):
        raise ValueError("检测到凭据内容，未保存工具声明")
    for key, limit in (("name", 60), ("description", 500), ("limitations", 2000)):
        if not isinstance(spec.get(key), str) or not 1 <= len(spec[key]) <= limit:
            raise ValueError("工具名称、说明与限制必须在长度预算内")
    for key in ("input_schema", "output_schema"):
        checked_schema(spec.get(key))
        if spec[key].get("type") != "object" or spec[key].get("additionalProperties") is not False or not spec[key].get("required"):
            raise ValueError("工具输入输出必须为明确必填字段且禁止额外字段的对象")
    sources = spec.get("sources", [])
    if not isinstance(sources, list) or len(sources) > 10:
        raise ValueError("来源列表无效")
    for source in sources:
        normalize_url(source)
        if any(_SENSITIVE_FIELD.search(key) for key, _ in parse_qsl(urlsplit(source).query)):
            raise ValueError("来源链接不能携带凭据查询参数")
    provenance = spec.get("provenance", [])
    if not isinstance(provenance, list) or len(provenance) > 10:
        raise ValueError("来源追踪记录无效")
    for item in provenance:
        if (not isinstance(item, dict) or set(item) != {"url", "content_hash", "fetched_at"}
                or item["url"] not in sources or not re.fullmatch(r"[a-f0-9]{64}", str(item["content_hash"]))
                or not isinstance(item["fetched_at"], str) or len(item["fetched_at"]) > 80):
            raise ValueError("来源追踪记录不完整")
    check_no_credentials(spec["input_schema"])
    tests = spec.get("tests")
    if not isinstance(tests, list) or not 1 <= len(tests) <= 3:
        raise ValueError("需要 1–3 个只读验收样例")
    for test in tests:
        if not isinstance(test, dict) or set(test) != {"input", "assertions"}:
            raise ValueError("样例必须包含输入和断言")
        check_no_credentials(test["input"])
        validate(spec["input_schema"], test.get("input"), max_bytes=65536)
        if not test.get("assertions"):
            raise ValueError("样例必须包含结果断言，不能仅凭 HTTP 200 判定成功")
    for assertions in [spec.get("assertions", [])] + [t["assertions"] for t in tests]:
        if not isinstance(assertions, list) or len(assertions) > 20:
            raise ValueError("验收断言超限")
        for item in assertions:
            if not isinstance(item, dict) or item.get("op") not in ("exists", "equals", "input", "range") or not isinstance(item.get("path"), str):
                raise ValueError("验收断言无效")
            check_pointer(item["path"])
            if item["op"] == "input":
                check_pointer(item.get("input"))
            if item["op"] == "equals" and "value" not in item:
                raise ValueError("断言缺少期望值")
            if item["op"] == "range" and not (type(item.get("min")) in (int, float) and type(item.get("max")) in (int, float) and item["min"] <= item["max"]):
                raise ValueError("断言范围无效")
    config, executor = spec.get("config"), spec.get("executor")
    if not isinstance(config, dict):
        raise ValueError("执行器配置必须是对象")
    reasons, approval = [], True
    if executor == "connector":
        known = connector_spec(config.get("connector"))
        if config != known["config"] or any(spec[k] != known[k] for k in ("input_schema", "output_schema")):
            raise ValueError("内置连接器模板不允许改写")
        approval = False
    elif executor == "http":
        if set(config) - {"url", "method", "query", "body", "output_map", "auth", "read_only_evidence"}:
            raise ValueError("HTTP 模板包含未许可配置")
        url = normalize_url(config.get("url"))
        if urlsplit(url).scheme != "https" or urlsplit(url).query or config.get("method") not in ("GET", "POST"):
            raise ValueError("仅支持固定 HTTPS 地址和待审核的只读 GET／查询 POST")
        if not sources or not isinstance(config.get("read_only_evidence"), str) or not config["read_only_evidence"].strip():
            raise ValueError("API 草稿需要真实文档来源与只读依据，仍须管理员审核")
        for key in ("query", "body"):
            mapping = config.get(key, {})
            if not isinstance(mapping, dict) or len(mapping) > 32:
                raise ValueError("参数模板超限")
            check_no_credentials(mapping)
            for field, entry in mapping.items():
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", field) or not isinstance(entry, dict) or set(entry) not in ({"input"}, {"literal"}):
                    raise ValueError("参数仅允许固定值或获准输入字段映射")
                if "input" in entry and (not isinstance(entry["input"], str) or entry["input"] not in spec["input_schema"].get("properties", {})):
                    raise ValueError("模板引用未声明输入")
                if "literal" in entry and not isinstance(entry["literal"], (str, int, float, bool)):
                    raise ValueError("模板固定值只能是标量")
        if config.get("method") == "GET" and config.get("body"):
            raise ValueError("GET 不允许请求体")
        mapping = config.get("output_map")
        if not isinstance(mapping, dict) or not mapping or len(mapping) > 32 or any(not isinstance(v, str) or not v.startswith("/") for v in mapping.values()):
            raise ValueError("必须通过 JSON Pointer 映射必要输出字段")
        if set(mapping) - set(spec["output_schema"]["properties"]) or set(spec["output_schema"]["required"]) - set(mapping):
            raise ValueError("输出映射必须覆盖声明的必填字段")
        for path in mapping.values():
            check_pointer(path)
        auth = config.get("auth", {})
        if auth and (not isinstance(auth, dict) or set(auth) != {"kind", "name"} or auth["kind"] not in ("bearer", "header", "query")
                     or not isinstance(auth["name"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", auth["name"])
                     or (auth["kind"] == "header" and auth["name"].lower() not in ("x-api-key", "api-key"))):
            raise ValueError("凭据只能使用受保护表单，不能自定义敏感请求头")
        if auth and auth["kind"] == "query" and auth["name"] in config.get("query", {}):
            raise ValueError("凭据不能与模型可填查询字段重叠")
        reasons.append("待管理员核验真实 API 文档、只读语义及参数权限")
    elif executor == "python":
        if set(config) != {"code"} or not isinstance(config["code"], str) or not 1 <= len(config["code"].encode()) <= 65536:
            raise ValueError("脚本只能声明 Python 代码；镜像、挂载和网络由服务端控制")
        try:
            tree = ast.parse(config["code"])
        except (SyntaxError, RecursionError):
            raise ValueError("Python 脚本语法无效") from None
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                if getattr(node, "level", 0) or any(m.split(".")[0] not in SAFE_IMPORTS for m in modules):
                    raise ValueError("脚本含未审核依赖或主机／网络能力")
            if isinstance(node, ast.Name) and (node.id.startswith("__") or node.id in {"eval", "exec", "compile", "open", "input", "getattr", "setattr", "globals", "locals", "vars", "breakpoint"}):
                raise ValueError("脚本含不允许的动态执行／文件访问入口")
            if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
                raise ValueError("脚本含不允许的反射入口")
        if not any(isinstance(n, ast.FunctionDef) and n.name == "main" for n in tree.body):
            raise ValueError("脚本必须定义 main(data) 并返回 JSON 对象")
        reasons.extend(["脚本需管理员按代码 hash 审批", "必须配置独立隔离 Worker，绝不在业务主机执行"])
    else:
        raise ValueError("不支持的执行器类型；首版不支持外部副作用工具")
    return {"status": "needs_config" if approval else "checked", "requires_admin": approval,
            "requires_terms": executor == "connector" and config["connector"] in ("weather", "elevation"),
            "reasons": reasons, "scan_version": SCAN_VERSION, "version": version_fingerprint(spec)}
