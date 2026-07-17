from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).parents[2]
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "deploy" / "docker-compose.yml"
ENV_EXAMPLE = ROOT / "deploy" / "env.example"
NGINX = ROOT / "deploy" / "nginx" / "cloud.claudcode.xyz.conf"
DEPLOY = ROOT / "deploy" / "deploy.sh"
DOCS = ROOT / "docs" / "cloud-deployment.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_container_is_nonroot_single_worker_and_health_checked():
    dockerfile = _read(DOCKERFILE)
    compose = _read(COMPOSE)

    assert re.search(r"^USER\s+(?!root\b)\S+", dockerfile, re.MULTILINE)
    assert "HEALTHCHECK" in dockerfile
    assert "--workers=1" in dockerfile or "--workers 1" in dockerfile
    assert "healthcheck:" in compose
    assert "127.0.0.1:18765:8000" in compose
    assert "0.0.0.0:18765" not in compose
    assert "type: bind" in compose
    assert "source: /home/ubuntu/tai-pan-cloud/data" in compose
    assert "target: /data" in compose


def test_compose_is_an_explicit_isolated_project_with_fixed_proxy_peer():
    compose = _read(COMPOSE)

    assert re.search(r"^name:\s*tai-pan-cloud\s*$", compose, re.MULTILINE)
    assert re.search(r"^\s+name:\s*tai-pan-cloud-internal\s*$", compose, re.MULTILINE)
    assert "10.203.187.0/28" in compose
    assert "10.203.187.1" in compose
    assert "--forwarded-allow-ips=10.203.187.1" in compose
    assert "--forwarded-allow-ips=*" not in compose
    assert "external: true" not in compose
    assert "network_mode: host" not in compose
    assert "privileged:" not in compose
    assert not re.search(r"postgres(?:ql)?", compose, re.IGNORECASE)
    assert re.search(r"^\s{2}app:\s*$", compose, re.MULTILINE)
    assert re.search(r"^\s{2}backup:\s*$", compose, re.MULTILINE)
    assert "app.cloud.maintenance" in compose
    assert "daily" in compose


def test_deployment_assets_contain_no_embedded_secrets():
    env_lines = {
        key: value
        for key, value in (
            line.split("=", 1)
            for line in _read(ENV_EXAMPLE).splitlines()
            if line and not line.startswith("#")
        )
    }

    assert env_lines["APP_MODE"] == "cloud"
    assert env_lines["PUBLIC_ORIGIN"] == "https://cloud.claudcode.xyz"
    assert env_lines["DATABASE_PATH"] == "/data/app.db"
    assert env_lines["STORAGE_PATH"] == "/data/files"
    assert env_lines["SESSION_SECRET"] == "<generated-on-server>"
    assert env_lines["KEY_ENCRYPTION_KEY"] == "<generated-on-server>"

    tracked_assets = "\n".join(
        _read(path)
        for path in (DOCKERFILE, COMPOSE, ENV_EXAMPLE, NGINX, DOCS)
    )
    assert "sk-proj-" not in tracked_assets
    assert not re.search(r"KEY_ENCRYPTION_KEY=[A-Za-z0-9_-]{40,}={0,2}", tracked_assets)
    assert not re.search(r"SESSION_SECRET=[A-Za-z0-9_-]{32,}", tracked_assets)


def test_nginx_site_has_bounded_uploads_internal_files_and_safe_forwarding():
    nginx = _read(NGINX)

    assert "server_name cloud.claudcode.xyz;" in nginx
    assert "client_max_body_size 210m;" in nginx
    assert "proxy_pass http://127.0.0.1:18765;" in nginx
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in nginx
    assert "proxy_set_header X-Real-IP $remote_addr;" in nginx
    assert "$proxy_add_x_forwarded_for" not in nginx
    assert re.search(r"location\s+/_protected_files/\s*\{[^}]*\binternal;", nginx, re.DOTALL)
    assert "alias /home/ubuntu/tai-pan-cloud/data/files/;" in nginx
    assert "wd.claudcode.xyz" not in nginx
    assert "api.claudcode.xyz" not in nginx


def test_deploy_script_is_target_locked_secret_safe_and_project_scoped():
    script = _read(DEPLOY)

    assert "TARGET=/home/ubuntu/tai-pan-cloud" in script
    assert "realpath" in script
    assert "umask 077" in script
    assert "os.fchmod(descriptor, 0o600)" in script
    assert "secrets.token_urlsafe" in script
    assert "urlsafe_b64encode" in script
    assert "urlsafe_b64decode" in script
    assert "os.O_EXCL" in script
    assert "O_NOFOLLOW" in script
    assert "backup_replaced_file" in script
    assert "docker compose --project-name tai-pan-cloud" in script
    assert "curl --fail --silent --show-error http://127.0.0.1:18765/health" in script
    assert script.index("127.0.0.1:18765/health") < script.index("nginx -t")
    assert "docker prune" not in script
    assert "docker system" not in script
    assert "docker stop" not in script
    assert "systemctl restart docker" not in script
    assert "mktemp" not in script


def test_operations_guide_covers_required_production_procedures():
    docs = _read(DOCS)
    required_text = (
        "23 GiB",
        "43.153.137.20",
        "bootstrap",
        "initial-admin.json",
        "0600",
        "TLS",
        "backup",
        "restore",
        "rollback",
        "1 GiB",
        "15 GiB",
        "offsite",
        "10.203.187.1",
        "X-Forwarded-For",
        "curl --fail http://127.0.0.1:18765/health",
        "docker compose --project-name tai-pan-cloud",
        "wd.claudcode.xyz",
        "api.claudcode.xyz",
        "MQTT",
    )
    for text in required_text:
        assert text in docs
