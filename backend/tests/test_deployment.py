"""部署状态机的离线回归；所有 docker 命令均由内存替身处理。"""
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("deploy_runtime", ROOT / "scripts/deploy_runtime.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


def safe_config(root):
    return {"name": "synthetic", "services": {
        "agent": {"environment": {"TRUSTED_PROXY_CIDRS": "172.30.0.10"}, "volumes": [
            {"type": "bind", "source": str(root / "data"), "target": "/app/data"},
            {"type": "bind", "source": str(root / "data/db"), "target": "/app/backend/data"}]},
        "llm-proxy": {"environment": {"TRUSTED_PROXY_CIDRS": "172.30.0.10"}},
        "frontend": {"environment": {"EDGE_PROXY_CIDR": "127.0.0.1/32"}, "ports": [
            {"host_ip": "127.0.0.1", "target": 80, "published": "5173"}]},
        "grafana": {"environment": {"GF_SECURITY_ADMIN_PASSWORD": "synthetic-test-only-123456"}},
    }}


@pytest.fixture
def environment(tmp_path, monkeypatch):
    root = tmp_path / "app"
    (root / "data/db").mkdir(parents=True)
    (root / "data/db/fixture.txt").write_text("旧业务数据")
    (root / "backend").mkdir()
    (root / "backend/.env").write_text("SYNTHETIC=1")
    backup = tmp_path / "old"
    backup.mkdir()
    old_file, old_env = backup / "docker-compose.yml", backup / "backend.env"
    old_file.write_text("services: {}")
    old_env.write_text("SYNTHETIC=1")
    for name in ("backend", "frontend"):
        (root / f"jianzirumian-{name}.tar.gz").write_bytes(b"synthetic image")
    new = safe_config(root)
    old = {"name": "synthetic", "services": {"backend": copy.deepcopy(new["services"]["agent"])}}
    old["services"]["backend"]["environment"] = {"SYNTHETIC": "1"}
    item = {"Id": "old-id", "Image": "sha256:fixed-old-image", "State": {"Running": True},
            "Config": {"Labels": {"com.docker.compose.service": "backend"}, "Env": ["SYNTHETIC=1"]},
            "Mounts": [{"Type": "bind", "Source": m["source"], "Destination": m["target"]}
                       for m in old["services"]["backend"]["volumes"]]}
    state = {"containers": [item], "calls": [], "fail_up": False}

    def run(args):
        state["calls"].append(args)
        if args[:3] == ["docker", "ps", "-aq"]:
            return " ".join(c["Id"] for c in state["containers"])
        if args[:2] == ["docker", "inspect"]:
            return json.dumps(state["containers"])
        if args[:3] == ["docker", "image", "inspect"]:
            return "[]"
        if args[:3] == ["docker", "image", "save"]:
            Path(args[args.index("-o") + 1]).write_bytes(b"fixed old images")
            return ""
        if args[:2] == ["docker", "stop"]:
            for c in state["containers"]:
                c["State"]["Running"] = False
            return ""
        if args[:3] == ["docker", "network", "ls"]:
            return ""
        if args[:3] == ["docker", "container", "rm"]:
            assert all(not c["State"]["Running"] for c in state["containers"])
            state["containers"] = []
            return ""
        if args[:2] == ["docker", "load"] or args[:2] == ["docker", "exec"]:
            return ""
        if "up" in args:
            if state["fail_up"]:
                raise deploy.DeploymentError("合成 readiness 失败")
            if "rollback-compose.json" in " ".join(args):
                state["containers"] = [copy.deepcopy(item)]
            return ""
        raise AssertionError(f"未配置的 docker 替身命令：{args}")

    monkeypatch.setattr(deploy, "run", run)
    monkeypatch.setattr(deploy, "configuration", lambda args: copy.deepcopy(old) if str(old_file) in args else copy.deepcopy(new))
    return root, new, old, old_file, old_env, state


@pytest.mark.parametrize("fault", ["password", "public_agent", "all_proxy", "replicas"])
def test_security_defaults_reject_unsafe_configuration(tmp_path, fault):
    config = safe_config(tmp_path)
    if fault == "password":
        config["services"]["grafana"]["environment"]["GF_SECURITY_ADMIN_PASSWORD"] = "admin"
    elif fault == "public_agent":
        config["services"]["agent"]["ports"] = [{"host_ip": "0.0.0.0"}]
    elif fault == "all_proxy":
        config["services"]["frontend"]["environment"]["EDGE_PROXY_CIDR"] = "0.0.0.0/0"
    else:
        config["services"]["agent"]["deploy"] = {"replicas": 2}
    with pytest.raises(deploy.DeploymentError):
        deploy.security_check(config)


def test_update_refuses_old_topology_before_loading(environment, monkeypatch):
    root, _, _, _, _, state = environment
    monkeypatch.setattr(sys, "argv", ["deploy", "update", "--root", str(root)])
    with pytest.raises(SystemExit, match="server-migrate-backend"):
        deploy.main()
    assert not any("up" in call or "load" in call or "stop" in call for call in state["calls"])


@pytest.mark.parametrize("fault", ["missing_backup", "mount", "environment", "port", "project"])
def test_migration_preflight_rejects_inconsistent_backup(environment, fault):
    root, new, _, old_file, old_env, state = environment
    if fault == "missing_backup":
        old_file = root / "absent.yml"
    elif fault == "mount":
        state["containers"][0]["Mounts"][0]["Source"] = str(root / "other")
    elif fault == "environment":
        state["containers"][0]["Config"]["Env"] = ["SYNTHETIC=changed"]
    elif fault == "port":
        state["containers"][0]["HostConfig"] = {"PortBindings": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8000"}]}}
    else:
        state["containers"] = []
    with pytest.raises(deploy.DeploymentError):
        deploy.migration_preflight(root, new, old_file, old_env)
    assert not any("stop" in call or "load" in call for call in state["calls"])


def test_dry_run_is_read_only(environment, monkeypatch):
    root, _, _, old_file, old_env, state = environment
    monkeypatch.setattr(sys, "argv", ["deploy", "migrate-backend", "--root", str(root),
                                      "--old-compose", str(old_file), "--old-env", str(old_env), "--dry-run"])
    deploy.main()
    assert not any("stop" in call or "load" in call or "up" in call or "save" in call for call in state["calls"])


@pytest.mark.parametrize("failure", ["snapshot", "readiness"])
def test_migration_failure_stops_project_and_preserves_bundle(environment, monkeypatch, failure):
    root, new, _, old_file, old_env, state = environment
    bundle = root.parent / "rollback"
    if failure == "snapshot":
        def fail(*args):
            assert all(not c["State"]["Running"] for c in state["containers"])
            raise OSError("合成快照失败")
        monkeypatch.setattr(deploy, "snapshot", fail)
    else:
        state["fail_up"] = True
    with pytest.raises(deploy.DeploymentError, match="迁移未完成"):
        deploy.migrate(root, ["docker", "compose"], new, old_file, old_env, bundle)
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["images"] == ["sha256:fixed-old-image"]
    assert (bundle / "images.tar").exists()
    assert all(not c["State"]["Running"] for c in state["containers"])
    if failure == "snapshot":
        assert not any("load" in call or "up" in call or "rm" in call for call in state["calls"])
    else:
        assert manifest["phase"] == "snapshot_complete"


def test_migration_and_explicit_rollback_restore_fixed_images_and_data(environment):
    root, new, _, old_file, old_env, state = environment
    bundle = root.parent / "rollback"
    deploy.migrate(root, ["docker", "compose"], new, old_file, old_env, bundle)
    assert json.loads((bundle / "manifest.json").read_text())["phase"] == "ready"
    fixed = json.loads((bundle / "rollback-compose.json").read_text())
    assert fixed["services"]["backend"]["image"] == "sha256:fixed-old-image"
    assert bundle.stat().st_mode & 0o777 == 0o700
    (root / "data/db/fixture.txt").write_text("升级后数据")
    with pytest.raises(deploy.DeploymentError, match="confirm-overwrite"):
        deploy.rollback(root, bundle, False)
    deploy.rollback(root, bundle, True)
    assert (root / "data/db/fixture.txt").read_text() == "旧业务数据"
    assert next(bundle.glob("displaced-*/data/db/fixture.txt")).read_text() == "升级后数据"
    assert any(call[:2] == ["docker", "exec"] for call in state["calls"])
    assert all("--env-file" in call for call in state["calls"] if call[:2] == ["docker", "compose"] and "rollback-compose.json" in " ".join(call))


@pytest.mark.parametrize("fault", ["checksum", "traversal", "symlink"])
def test_rollback_rejects_bad_artifacts_before_stopping(environment, fault):
    root, new, _, old_file, old_env, state = environment
    bundle = root.parent / "rollback"
    deploy.migrate(root, ["docker", "compose"], new, old_file, old_env, bundle)
    state["calls"].clear()
    archive = bundle / "data.tar.gz"
    if fault == "checksum":
        archive.write_bytes(b"tampered")
    else:
        with tarfile.open(archive, "w:gz") as stream:
            member = tarfile.TarInfo("../escape" if fault == "traversal" else "data/link")
            if fault == "symlink":
                member.type = tarfile.SYMTYPE
                member.linkname = "../../escape"
            stream.addfile(member, io.BytesIO())
        manifest = json.loads((bundle / "manifest.json").read_text())
        manifest["checksums"]["data.tar.gz"] = deploy.digest(archive)
        deploy.protected_json(bundle / "manifest.json", manifest)
    with pytest.raises(deploy.DeploymentError):
        deploy.rollback(root, bundle, True)
    assert state["calls"] == []


def test_network_with_foreign_container_is_never_removed(monkeypatch):
    calls = []
    monkeypatch.setattr(deploy, "containers", lambda p: [])
    def run(args):
        calls.append(args)
        if "ls" in args:
            return "network-id"
        return json.dumps([{"Containers": {"unrelated-id": {}}}])
    monkeypatch.setattr(deploy, "run", run)
    with pytest.raises(deploy.DeploymentError, match="其他容器"):
        deploy.clear_stopped_topology("synthetic")
    assert not any("rm" in call for call in calls)
