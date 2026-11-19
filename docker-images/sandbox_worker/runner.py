"""只随固定镜像运行；禁止在业务后端或 Worker 主进程导入执行用户代码。"""
import json
import sys


def run():
    if sys.version_info[:2] != (3, 11):
        raise ValueError("unsupported_python_version")
    raw = sys.stdin.buffer.read(200001)
    if len(raw) > 200000:
        raise ValueError("input_limit")
    payload = json.loads(raw)
    if set(payload) != {"code", "data"} or not isinstance(payload["data"], dict):
        raise ValueError("invalid_input")
    namespace = {"__name__": "personal_tool"}
    exec(compile(payload["code"], "<approved-tool>", "exec"), namespace)
    output = namespace["main"](payload["data"])
    if not isinstance(output, dict):
        raise ValueError("invalid_output")
    encoded = json.dumps(output, ensure_ascii=False, allow_nan=False).encode()
    if len(encoded) > 262144:
        raise ValueError("output_limit")
    sys.stdout.buffer.write(encoded)


if __name__ == "__main__":
    try:
        run()
    except BaseException:
        sys.stderr.write("script_failed\n")
        sys.exit(1)
