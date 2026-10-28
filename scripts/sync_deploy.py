#!/usr/bin/env python3
"""同步部署配置快照，不读写真实 .env、不执行 Docker、不删除其他文件。"""
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def files():
    mappings = {
        "docker-compose.yml": "docker-compose.yml",
        "nginx.conf": "nginx.conf",
        "backend/.env.example": ".env.example",
        "scripts/deploy-aliyun.sh": "deploy-aliyun.sh",
        "scripts/deploy_runtime.py": "deploy_runtime.py",
        "scripts/nginx-https.conf": "nginx-https.conf",
    }
    for source in sorted((ROOT / "monitoring").rglob("*")):
        if source.is_file() and source.suffix in (".yml", ".yaml", ".json"):
            name = str(source.relative_to(ROOT))
            mappings[name] = name
    return [(ROOT / source, ROOT / "docker-images" / target) for source, target in mappings.items()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只检查字节一致性")
    options = parser.parse_args()
    mismatches = []
    for source, target in files():
        content = source.read_bytes()
        if not target.is_file() or target.read_bytes() != content:
            mismatches.append(str(target.relative_to(ROOT)))
            if not options.check:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
    if options.check and mismatches:
        raise SystemExit("部署快照不一致：" + ", ".join(mismatches))
    print(f"部署快照{'检查通过' if options.check else '同步完成'}：{len(files())} 个配置文件")


if __name__ == "__main__":
    main()
