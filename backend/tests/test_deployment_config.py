"""部署静态契约：仅使用合成环境，不连接 Docker daemon 或读取真实 .env。"""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def cli(name):
    executable = shutil.which(name)
    if not executable:
        pytest.skip(f"本地未安装 {name}，不能替代实际语法验收")
    return executable


@pytest.mark.parametrize("source", ["docker-compose.yml", "docker-images/docker-compose.yml"])
@pytest.mark.parametrize("password_present", [True, False])
def test_compose_with_synthetic_environment(tmp_path, source, password_present):
    config = tmp_path / "docker-compose.yml"
    config.write_bytes((ROOT / source).read_bytes())
    env_file = tmp_path / "backend/.env"
    env_file.parent.mkdir()
    env_file.write_text("GRAFANA_ADMIN_PASSWORD=Synthetic-Only-2026!\n" if password_present else "")
    # Docker Desktop 的用户级插件发现依赖 HOME；优先直接调用已安装的 Compose。
    executable = shutil.which("docker-compose")
    command = [executable] if executable else [cli("docker"), "compose"]
    result = subprocess.run(command + [
        "--project-directory", str(tmp_path),
        "--env-file", str(env_file), "-f", str(config), "-p", "ab-static",
        "config", "--format", "json",
    ], env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "DOCKER_CONFIG": str(tmp_path / "docker")},
        capture_output=True, text=True, timeout=30)
    if not password_present:
        assert result.returncode != 0 and "GRAFANA_ADMIN_PASSWORD" in result.stderr
        return
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert set(services) == {"redis", "agent", "llm-proxy", "frontend", "prometheus", "grafana"}
    for item in services.values():
        assert all(port["host_ip"] == "127.0.0.1" for port in item.get("ports", []))
    for name, endpoint in [("agent", "/api/ready"), ("llm-proxy", "/ready")]:
        item = services[name]
        assert endpoint in " ".join(item["healthcheck"]["test"])
        hosts = item["extra_hosts"]
        if isinstance(hosts, list):
            hosts = dict(value.replace(":", "=", 1).split("=", 1) for value in hosts)
        assert hosts["host.docker.internal"] == "host-gateway"
        assert item["environment"]["TRUSTED_PROXY_CIDRS"] == "172.30.0.10"
        assert not {"agent", "llm-proxy"} & set(item.get("depends_on", {}))
        assert item.get("deploy", {}).get("replicas", 1) == 1
    assert "--no-proxy-headers" in services["llm-proxy"]["command"]
    assert services["grafana"]["environment"]["GF_SECURITY_ADMIN_PASSWORD"] == "Synthetic-Only-2026!"
    assert services["frontend"]["networks"]["jianziruyang-net"]["ipv4_address"] == "172.30.0.10"


def test_nginx_substitution_keeps_runtime_variables(tmp_path):
    template = (ROOT / "nginx.conf").read_text()
    names = "${BACKEND_HOST} ${LLM_PROXY_HOST} ${EDGE_PROXY_CIDR}"
    result = subprocess.run([cli("envsubst"), names], input=template, text=True, capture_output=True,
        env={"PATH": os.environ["PATH"], "BACKEND_HOST": "agent", "LLM_PROXY_HOST": "llm-proxy",
             "EDGE_PROXY_CIDR": "172.30.0.1/32"}, timeout=10, check=True)
    rendered = result.stdout
    assert "${" not in rendered
    assert "set_real_ip_from 172.30.0.1/32;" in rendered
    assert "proxy_pass http://agent:8000;" in rendered and "proxy_pass http://llm-proxy:8001;" in rendered
    for variable in ("$remote_addr", "$binary_remote_addr", "$realip_remote_addr", "$forwarded_scheme", "$uri"):
        assert variable in rendered
    assert "location /metrics { return 403; }" in rendered
    assert "location /internal { return 404; }" in rendered
    assert "proxy_read_timeout 360s;" in rendered
    assert f"envsubst '{names}'" in (ROOT / "Dockerfile.frontend").read_text()
    external = (ROOT / "scripts/nginx-https.conf").read_text()
    assert "return 308 https://" in external
    assert "proxy_pass http://127.0.0.1:5173;" in external
    assert "proxy_read_timeout 420s;" in external
    assert "TLSv1.2 TLSv1.3" in external


def test_dashboard_uses_instant_up_and_rates_only_counters():
    dashboard = json.loads((ROOT / "monitoring/grafana/dashboards/jzrm-overview.json").read_text())
    panels = dashboard["panels"]
    live = next(p for p in panels if any(t["expr"].startswith("up{") for t in p["targets"]))
    assert live["targets"][0]["instant"] is True
    assert live["fieldConfig"]["defaults"]["noValue"] == "无数据"
    assert live["options"]["reduceOptions"]["calcs"] == ["last"]
    expressions = [t["expr"] for p in panels for t in p["targets"]]
    assert any("rate(jzrm_gateway_requests_total" in e for e in expressions)
    assert any("rate(jzrm_agent_traces_total" in e for e in expressions)
    assert any("jzrm_ready" == e for e in expressions)
    for expr in expressions:
        for name in re.findall(r"rate\((\w+)", expr):
            assert name.endswith("_total"), expr
        assert "jzrm_up" not in expr


def test_alerts_and_scrape_are_separate_local_services():
    config = yaml.safe_load((ROOT / "monitoring/prometheus.yml").read_text())
    assert config["rule_files"] == ["/etc/prometheus/alerts.yml"]
    jobs = {job["job_name"]: job for job in config["scrape_configs"]}
    for name, address in [("agent", "agent:8000"), ("llm-proxy", "llm-proxy:8001")]:
        assert jobs[name]["metrics_path"] == "/metrics"
        assert jobs[name]["static_configs"][0]["targets"] == [address]
    rules = yaml.safe_load((ROOT / "monitoring/alerts.yml").read_text())["groups"][0]["rules"]
    assert len(rules) == 4
    for rule in rules:
        assert rule["for"] == "1m" and "absent(" in rule["expr"]
        assert rule["labels"]["service"] in jobs
        assert f'job="{rule["labels"]["service"]}"' in rule["expr"]


def test_deployment_snapshots_are_byte_identical():
    spec = importlib.util.spec_from_file_location("sync_deploy", ROOT / "scripts/sync_deploy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for source, target in module.files():
        assert source.read_bytes() == target.read_bytes(), str(target)


@pytest.mark.parametrize("source", ["scripts/deploy-aliyun.sh", "docker-images/deploy-aliyun.sh", "scripts/dev-daemon.sh", "start.sh"])
def test_shell_syntax_only(source):
    subprocess.run([cli("bash"), "-n", str(ROOT / source)], check=True, capture_output=True, timeout=10)


@pytest.mark.skipif(os.getenv("AB_NGINX_IMAGE") is None, reason="需显式提供已有本地 Nginx 镜像才执行隔离容器语法验收")
@pytest.mark.parametrize("external", [False, True])
def test_nginx_syntax_in_isolated_local_container(tmp_path, external):
    docker = cli("docker")
    inspected = subprocess.run([docker, "image", "inspect", os.environ["AB_NGINX_IMAGE"], "--format", "{{.Id}}"],
                               capture_output=True, text=True, timeout=10)
    assert inspected.returncode == 0, inspected.stderr
    image = inspected.stdout.strip()
    if external:
        subprocess.run([cli("openssl"), "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=notes.example.com", "-keyout", str(tmp_path / "key.pem"),
                        "-out", str(tmp_path / "cert.pem")], check=True, capture_output=True, timeout=30)
        (tmp_path / "key.pem").chmod(0o600)
        config = (ROOT / "scripts/nginx-https.conf").read_text().replace(
            "/etc/nginx/certs/fullchain.pem", "/check/cert.pem").replace("/etc/nginx/certs/privkey.pem", "/check/key.pem")
    else:
        config = (ROOT / "nginx.conf").read_text()
        for name, value in {"BACKEND_HOST": "127.0.0.1", "LLM_PROXY_HOST": "127.0.0.1", "EDGE_PROXY_CIDR": "172.30.0.1/32"}.items():
            config = config.replace("${" + name + "}", value)
    (tmp_path / "site.conf").write_text(config)
    (tmp_path / "nginx.conf").write_text("user root; pid /tmp/nginx.pid; error_log stderr; events {} http { access_log off; include /check/site.conf; }\n")
    result = subprocess.run([docker, "run", "--rm", "--pull=never", "--network=none", "--read-only",
        "--cap-drop=ALL", "--security-opt=no-new-privileges", "--memory=128m", "--pids-limit=32",
        "--tmpfs", "/tmp", "--tmpfs", "/var/cache/nginx", "--mount", f"type=bind,src={tmp_path},dst=/check,readonly",
        "--entrypoint", "nginx", image, "-t", "-c", "/check/nginx.conf"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "test is successful" in result.stderr
