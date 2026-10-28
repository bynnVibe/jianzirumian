#!/usr/bin/env python3
"""单机部署保护：只操作明确 Compose 项目；迁移及恢复均需显式入口。"""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import time


class DeploymentError(RuntimeError):
    pass


def run(args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        # Compose 输出可能包含插值后的密钥，禁止透传。
        raise DeploymentError(f"命令失败：{args[0]} {args[1]}（退出码 {result.returncode}）；已停止后续步骤")
    return result.stdout


def compose(root, env, config, project=None):
    args = ["docker", "compose", "--project-directory", str(root), "--env-file", str(env), "-f", str(config)]
    return args + (["-p", project] if project else [])


def configuration(args):
    return json.loads(run(args + ["config", "--format", "json"]))


def containers(project):
    ids = run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"]).split()
    return json.loads(run(["docker", "inspect", *ids])) if ids else []


def service(container):
    return container["Config"]["Labels"].get("com.docker.compose.service", "")


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def protected_json(path, data):
    with Path(path).open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
    Path(path).chmod(0o600)


def security_check(config):
    services = config["services"]
    password = services.get("grafana", {}).get("environment", {}).get("GF_SECURITY_ADMIN_PASSWORD", "")
    if len(password) < 16 or password.lower() in {"admin", "password", "changeme", "change-me-please"} or len(set(password)) < 6:
        raise DeploymentError("Grafana 必须配置至少16字符的非默认强密码；已有数据卷须在 Grafana 内另行轮换")
    for name, item in services.items():
        for port in item.get("ports", []):
            address = ipaddress.ip_address(port.get("host_ip") or "0.0.0.0")
            approved = name == "frontend" and address.version == 4 and address.is_private and not address.is_unspecified and not address.is_multicast
            if not address.is_loopback and not approved:
                raise DeploymentError(f"{name} 禁止公开宿主端口；仅 frontend 可显式绑定批准的内网地址")
    env = services["frontend"].get("environment", {})
    edge = ipaddress.ip_network(env.get("EDGE_PROXY_CIDR", "127.0.0.1/32"), strict=False)
    if edge.prefixlen == 0:
        raise DeploymentError("可信边缘代理禁止全网 CIDR")
    for name in ("agent", "llm-proxy"):
        for cidr in services[name].get("environment", {}).get("TRUSTED_PROXY_CIDRS", "").split(","):
            if not cidr or ipaddress.ip_network(cidr.strip(), strict=False).prefixlen == 0:
                raise DeploymentError("后端必须配置显式可信代理 IP/CIDR")
        if int(services[name].get("deploy", {}).get("replicas", 1)) != 1:
            raise DeploymentError("本部署只支持单进程单副本")


def reject_legacy(project):
    if any(service(item) == "backend" for item in containers(project)):
        raise DeploymentError("发现同项目旧 backend 容器；必须使用 server-migrate-backend 并提供旧配置备份")


def data_mounts(item):
    return {m["target"]: Path(m["source"]).resolve() for m in item.get("volumes", [])
            if m.get("type") == "bind" and m["target"] in ("/app/data", "/app/backend/data")}


def migration_preflight(root, new, old_compose, old_env):
    if not old_compose or not old_env or not old_compose.is_file() or not old_env.is_file():
        raise DeploymentError("迁移必须提供旧 Compose 与旧环境文件备份")
    if old_compose.resolve() == (root / "docker-compose.yml").resolve() or old_env.resolve() == (root / "backend/.env").resolve():
        raise DeploymentError("旧配置必须为升级前独立备份，不能引用已被覆盖的当前配置")
    project = new["name"]
    old = configuration(compose(root, old_env, old_compose, project))
    if "backend" not in old["services"] or "agent" in old["services"]:
        raise DeploymentError("所提供备份不是旧 backend 拓扑")
    current = containers(project)
    if not current or not any(service(c) == "backend" for c in current):
        raise DeploymentError("没有找到同项目旧 backend 容器")
    names = [service(c) for c in current]
    if len(names) != len(set(names)) or set(names) != set(old["services"]):
        raise DeploymentError("旧项目服务清单与备份不符，或存在多副本/未知写者")
    old_data = data_mounts(old["services"]["backend"])
    if set(old_data) != {"/app/data", "/app/backend/data"} or old_data != data_mounts(new["services"]["agent"]):
        raise DeploymentError("新旧数据挂载不一致；不自动进行全量存储路径迁移")
    paths = sorted(set(old_data.values()), key=lambda p: len(p.parts))
    roots = [p for p in paths if not any(p != parent and p.is_relative_to(parent) for parent in paths)]
    for path in roots:
        if not path.is_relative_to(root) or path == root or not path.is_dir():
            raise DeploymentError("数据目录必须为当前部署目录内的既有子目录")
        if path.is_symlink() or any(p.is_symlink() for p in path.rglob("*")):
            raise DeploymentError("数据快照不支持符号链接，请先人工核对存储布局")
    assets = {}
    for container in current:
        name = service(container)
        item = old["services"][name]
        actual = {m["Destination"]: str(Path(m["Source"]).resolve()) for m in container.get("Mounts", []) if m["Type"] == "bind"}
        expected = {m["target"]: str(Path(m["source"]).resolve()) for m in item.get("volumes", []) if m.get("type") == "bind"}
        if actual != expected:
            raise DeploymentError(f"{name} 真实挂载与旧配置备份不符")
        bindings = container.get("HostConfig", {}).get("PortBindings") or {}
        expected_ports = {(f"{p['target']}/{p.get('protocol', 'tcp')}", str(p['published']), p.get('host_ip') or '0.0.0.0') for p in item.get("ports", [])}
        actual_ports = {(target, p["HostPort"], p.get("HostIp") or "0.0.0.0") for target, values in bindings.items() for p in (values or []) if p.get("HostIp") != "::" or any(port[2] == "::" for port in expected_ports)}
        if actual_ports != expected_ports:
            raise DeploymentError(f"{name} 端口与旧配置不符")
        actual_env = dict(value.split("=", 1) for value in container.get("Config", {}).get("Env", []) if "=" in value)
        if any(actual_env.get(key) != str(value) for key, value in item.get("environment", {}).items() if value is not None):
            raise DeploymentError(f"{name} 环境配置与旧备份不符；拒绝生成不可靠的回滚产物")
        image_id = container["Image"]
        run(["docker", "image", "inspect", image_id])
        item["image"] = image_id
        item.pop("build", None)
        item.pop("env_file", None)
        for mount in item.get("volumes", []):
            if mount.get("type") != "bind":
                continue
            source = Path(mount["source"]).resolve()
            if any(source == p or source.is_relative_to(p) for p in roots) or source.is_relative_to(root / "logs"):
                continue
            if not source.is_relative_to(root):
                raise DeploymentError("旧配置包含部署目录之外的配置挂载，需人工处理")
            backup = old_env if mount["target"] == "/app/backend/.env" else old_compose.parent / source.relative_to(root)
            if not backup.exists() or backup.resolve() == source:
                raise DeploymentError(f"缺少旧配置挂载备份：{source.relative_to(root)}")
            assets[str(source)] = backup
    for archive in ("jianzirumian-backend.tar.gz", "jianzirumian-frontend.tar.gz"):
        if not (root / archive).is_file():
            raise DeploymentError(f"缺少新镜像产物：{archive}")
    return old, current, roots, assets


def stop_project(project):
    items = containers(project)
    if items:
        run(["docker", "stop", "--time", "60", *[c["Id"] for c in items]])
    if any(c["State"]["Running"] for c in containers(project)):
        raise DeploymentError("项目仍有运行中的容器，禁止数据快照或恢复")


def clear_stopped_topology(project):
    """仅替换已停止的本项目容器/网络；不删除任何数据卷或其他项目资源。"""
    items = containers(project)
    if any(item["State"]["Running"] for item in items):
        raise DeploymentError("必须先停止项目才能替换拓扑")
    ids = {item["Id"] for item in items}
    networks = run(["docker", "network", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"]).split()
    for network in networks:
        details = json.loads(run(["docker", "network", "inspect", network]))[0]
        if set((details.get("Containers") or {}).keys()) - ids:
            raise DeploymentError("项目网络被其他容器占用，拒绝修改")
    if ids:
        run(["docker", "container", "rm", *sorted(ids)])
    if networks:
        run(["docker", "network", "rm", *networks])


def snapshot(root, roots, archive):
    with tarfile.open(archive, "w:gz") as stream:
        for path in roots:
            stream.add(path, arcname=str(path.relative_to(root)))
    with tarfile.open(archive, "r:gz") as stream:
        for member in stream.getmembers():
            if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                raise DeploymentError("快照包含不支持的文件类型")
    archive.chmod(0o600)


def load_images(root):
    for name in ("backend", "frontend"):
        run(["docker", "load", "-i", str(root / f"jianzirumian-{name}.tar.gz")])


def start(args):
    run(args + ["up", "-d", "--no-build", "--wait", "--wait-timeout", "180"])


def migrate(root, args, new, old_compose, old_env, bundle):
    old, current, roots, assets = migration_preflight(root, new, old_compose, old_env)
    bundle = bundle.resolve()
    if bundle.exists() or any(bundle.is_relative_to(p) for p in roots):
        raise DeploymentError("回滚目录必须不存在且位于数据目录之外")
    bundle.mkdir(parents=True, mode=0o700)
    shutil.copy2(old_compose, bundle / "original-compose.yml")
    shutil.copy2(old_env, bundle / "original.env")
    for item in old["services"].values():
        for mount in item.get("volumes", []):
            backup = assets.get(mount.get("source"))
            if not backup:
                continue
            destination = bundle / "config" / Path(mount["source"]).relative_to(root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if backup.is_dir():
                shutil.copytree(backup, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(backup, destination)
            mount["source"] = str(destination)
    protected_json(bundle / "rollback-compose.json", old)
    ids = sorted({c["Image"] for c in current})
    run(["docker", "image", "save", "-o", str(bundle / "images.tar"), *ids])
    manifest = {"version": 1, "project": new["name"], "root": str(root), "images": ids,
                "data_roots": [str(p.relative_to(root)) for p in roots], "phase": "prepared"}
    manifest["checksums"] = {str(p.relative_to(bundle)): digest(p) for p in bundle.rglob("*") if p.is_file()}
    protected_json(bundle / "manifest.json", manifest)
    stop_project(new["name"])
    try:
        snapshot(root, roots, bundle / "data.tar.gz")
        manifest["checksums"]["data.tar.gz"] = digest(bundle / "data.tar.gz")
        manifest["phase"] = "snapshot_complete"
        protected_json(bundle / "manifest.json", manifest)
        clear_stopped_topology(new["name"])
        load_images(root)
        start(args)
        manifest["phase"] = "ready"
        protected_json(bundle / "manifest.json", manifest)
    except Exception:
        stop_project(new["name"])
        raise DeploymentError(f"迁移未完成，项目已停止；保留回滚产物于 {bundle}，不会自动覆盖数据")
    print(f"迁移完成；回滚产物：{bundle}")


def rollback(root, bundle, confirmed):
    if not confirmed:
        raise DeploymentError("回滚恢复会替换升级后数据；必须显式传入 --confirm-overwrite-new-data")
    bundle = bundle.resolve()
    manifest = json.loads((bundle / "manifest.json").read_text())
    if manifest["root"] != str(root) or "data.tar.gz" not in manifest["checksums"]:
        raise DeploymentError("回滚清单路径不符或缺少完整数据快照")
    for name, expected in manifest["checksums"].items():
        path = (bundle / name).resolve()
        if not path.is_relative_to(bundle) or digest(path) != expected:
            raise DeploymentError("回滚产物校验失败")
    data_roots = [root / p for p in manifest["data_roots"]]
    if not data_roots or any(p.resolve() == root or not p.resolve().is_relative_to(root) for p in data_roots):
        raise DeploymentError("回滚数据路径不合法")
    with tarfile.open(bundle / "data.tar.gz", "r:gz") as stream:
        members = stream.getmembers()
        for member in members:
            target = (root / member.name).resolve()
            if member.issym() or member.islnk() or not (member.isfile() or member.isdir()) or not any(target == p or target.is_relative_to(p) for p in data_roots):
                raise DeploymentError("回滚快照包含非法路径或链接")
        stop_project(manifest["project"])
        clear_stopped_topology(manifest["project"])
        displaced = bundle / f"displaced-{time.time_ns()}"
        displaced.mkdir(mode=0o700)
        for path in data_roots:
            if path.exists():
                destination = displaced / path.relative_to(root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(destination))
        stream.extractall(root, members=members)
    old_args = compose(root, bundle / "original.env", bundle / "rollback-compose.json", manifest["project"])
    try:
        run(["docker", "load", "-i", str(bundle / "images.tar")])
        start(old_args)
        # 旧版可能没有 healthcheck；至少在旧 backend 容器内验证应用存活。
        backend = next(c for c in containers(manifest["project"]) if service(c) == "backend")
        run(["docker", "exec", backend["Id"], "python", "-c",
             "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=10)"])
    except Exception:
        stop_project(manifest["project"])
        raise
    print(f"已恢复固定旧镜像及数据；升级后的数据保留于 {displaced}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["preflight", "start", "update", "migrate-backend", "rollback"])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--old-compose", type=Path)
    parser.add_argument("--old-env", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-overwrite-new-data", action="store_true")
    options = parser.parse_args()
    os.umask(0o077)
    root = options.root.resolve()
    try:
        if options.action == "rollback":
            if not options.bundle or options.dry_run:
                raise DeploymentError("回滚要求 --bundle 和显式数据恢复确认")
            rollback(root, options.bundle, options.confirm_overwrite_new_data)
            return
        env = root / "backend/.env"
        if not env.is_file():
            raise DeploymentError("请先人工创建 backend/.env 并配置密钥；脚本不生成或覆盖该文件")
        args = compose(root, env, root / "docker-compose.yml")
        new = configuration(args)
        security_check(new)
        args = compose(root, env, root / "docker-compose.yml", new["name"])
        if options.action == "migrate-backend" or options.old_compose:
            migration_preflight(root, new, options.old_compose, options.old_env)
            if options.dry_run or options.action == "preflight":
                print("旧拓扑只读预检通过；未停止容器、加载镜像或改写数据")
                return
            if not options.bundle:
                raise DeploymentError("迁移必须指定 --bundle 回滚产物目录")
            migrate(root, args, new, options.old_compose, options.old_env, options.bundle)
            return
        reject_legacy(new["name"])
        if options.action == "preflight" or options.dry_run:
            print("只读预检通过；未修改配置或运行状态")
            return
        if options.action == "update":
            load_images(root)
        start(args)
        print("服务已就绪；入口默认 http://127.0.0.1:5173，生产流量请经外部 HTTPS 代理")
    except (DeploymentError, ValueError, OSError, KeyError) as exc:
        raise SystemExit(f"部署被阻断：{exc}")


if __name__ == "__main__":
    main()
