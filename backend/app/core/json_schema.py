"""受限资源预算的 JSON 与 Schema 校验；禁止远程引用和不受控正则求值。"""
import json
import math

from jsonschema import Draft202012Validator, FormatChecker


class SchemaError(ValueError):
    pass


def check_json(value, *, max_bytes=262144):
    pending, nodes = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 24 or nodes > 20000:
            raise SchemaError("JSON 嵌套或元素数量超出限制")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise SchemaError("JSON 对象键必须为字符串")
            pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise SchemaError("包含非 JSON 类型")
        elif type(item) is float and not math.isfinite(item):
            raise SchemaError("JSON 数字必须有限")
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > max_bytes:
        raise SchemaError("JSON 大小超出限制")


def parse_object(text):
    if not isinstance(text, str) or len(text.encode()) > 65536:
        raise SchemaError("工具参数必须是限量 JSON 文本")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise SchemaError("工具参数包含重复字段")
            result[key] = value
        return result
    try:
        value = json.loads(text, object_pairs_hook=pairs)
    except (ValueError, RecursionError) as exc:
        raise SchemaError("工具参数不是唯一完整的 JSON 对象") from exc
    check_json(value, max_bytes=65536)
    if not isinstance(value, dict):
        raise SchemaError("工具参数必须为 JSON 对象")
    return value


def checked_schema(schema):
    if not isinstance(schema, dict):
        raise SchemaError("Schema 必须是对象")
    check_json(schema, max_bytes=32768)
    pending = [(schema, 0)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, dict):
            if any(k in item for k in ("$ref", "$dynamicRef", "$recursiveRef", "pattern", "patternProperties")):
                raise SchemaError("首版 Schema 不支持引用或正则约束，请使用内联字段、枚举及范围约束")
            if depth > 16:
                raise SchemaError("Schema 嵌套过深")
            pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise SchemaError("Schema 定义无效") from exc
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate(schema, value, *, max_bytes=262144):
    check_json(value, max_bytes=max_bytes)
    error = next(checked_schema(schema).iter_errors(value), None)
    if error:
        path = ".".join(str(p) for p in error.absolute_path)[:160] or "$"
        raise SchemaError(f"字段 {path} 不满足 {error.validator} 约束")
