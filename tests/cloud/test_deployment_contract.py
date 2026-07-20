from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
GITIGNORE = ROOT / ".gitignore"
DOCKERIGNORE = ROOT / ".dockerignore"
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "deploy" / "docker-compose.yml"
ENV_EXAMPLE = ROOT / "deploy" / "env.example"
NGINX = ROOT / "deploy" / "nginx" / "cloud.claudcode.xyz.conf"
FILE_PROXY_NGINX = ROOT / "deploy" / "nginx" / "file-proxy.conf"
FILE_PROXY_DOCKERFILE = ROOT / "deploy" / "nginx" / "Dockerfile"
HTTP_NGINX = ROOT / "deploy" / "nginx" / "cloud.claudcode.xyz.http.conf"
MAINTENANCE_NGINX = ROOT / "deploy" / "nginx" / "cloud.claudcode.xyz.maintenance.conf"
CERTBOT_HOOK = ROOT / "deploy" / "certbot-nginx-reload.sh"
DEPLOY = ROOT / "deploy" / "deploy.sh"
DOCS = ROOT / "docs" / "cloud-deployment.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _run_deploy_functions(body: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    command = f"""
set -Eeuo pipefail
export TAI_PAN_DEPLOY_SOURCE_ONLY=1
source {shlex.quote(str(DEPLOY))}
TRACE={shlex.quote(str(tmp_path / 'trace'))}
{body}
"""
    return subprocess.run(
        ["bash", "-c", command],
        text=True,
        capture_output=True,
        check=False,
    )


def test_container_is_nonroot_single_worker_and_health_checked():
    dockerfile = _read(DOCKERFILE)
    compose = _read(COMPOSE)

    assert re.search(r"^USER\s+(?!root\b)\S+", dockerfile, re.MULTILINE)
    assert "HEALTHCHECK" in dockerfile
    assert "--workers=1" in dockerfile or "--workers 1" in dockerfile
    assert "healthcheck:" in compose
    assert "127.0.0.1:18765:8080" in compose
    assert "127.0.0.1:18765:8000" not in compose
    assert "0.0.0.0:18765" not in compose
    assert "type: bind" in compose
    assert "source: /home/ubuntu/tai-pan-cloud/data/database" in compose
    assert "source: /home/ubuntu/tai-pan-cloud/data/files" in compose
    assert "source: /home/ubuntu/tai-pan-cloud/data/backups" in compose
    assert "source: /home/ubuntu/tai-pan-cloud/data/credentials" in compose
    assert "source: /home/ubuntu/tai-pan-cloud/data\n" not in compose
    assert "image: tai-pan-file-proxy:latest" in compose
    assert "dockerfile: Dockerfile" in compose
    assert "target: /etc/nginx/nginx.conf" not in compose


def test_compose_is_an_explicit_isolated_project_with_fixed_proxy_peer():
    compose = _read(COMPOSE)

    assert re.search(r"^name:\s*tai-pan-cloud\s*$", compose, re.MULTILINE)
    assert re.search(r"^\s+name:\s*tai-pan-cloud-internal\s*$", compose, re.MULTILINE)
    assert "10.203.187.0/28" in compose
    assert "10.203.187.1" in compose
    assert "--forwarded-allow-ips=10.203.187.3" in compose
    assert "--forwarded-allow-ips=*" not in compose
    assert "external: true" not in compose
    assert "network_mode: host" not in compose
    assert "privileged:" not in compose
    assert not re.search(r"postgres(?:ql)?", compose, re.IGNORECASE)
    assert re.search(r"^\s{2}app:\s*$", compose, re.MULTILINE)
    assert re.search(r"^\s{2}backup:\s*$", compose, re.MULTILINE)
    assert re.search(r"^\s{2}file_proxy:\s*$", compose, re.MULTILINE)
    assert re.search(r"^\s{2}bootstrap:\s*$", compose, re.MULTILINE)
    assert "ipv4_address: 10.203.187.3" in compose
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
    assert env_lines["DATABASE_PATH"] == "/data/database/app.db"
    assert env_lines["STORAGE_PATH"] == "/data/files"
    assert env_lines["SESSION_SECRET"] == "<generated-on-server>"
    assert env_lines["KEY_ENCRYPTION_KEY"] == "<generated-on-server>"

    tracked_assets = "\n".join(
        _read(path)
        for path in (
            DOCKERFILE,
            COMPOSE,
            ENV_EXAMPLE,
            NGINX,
            FILE_PROXY_NGINX,
            FILE_PROXY_DOCKERFILE,
            HTTP_NGINX,
            MAINTENANCE_NGINX,
            CERTBOT_HOOK,
            DOCS,
        )
    )
    assert "sk-proj-" not in tracked_assets
    assert not re.search(r"KEY_ENCRYPTION_KEY=[A-Za-z0-9_-]{40,}={0,2}", tracked_assets)
    assert not re.search(r"SESSION_SECRET=[A-Za-z0-9_-]{32,}", tracked_assets)

    for ignore_file in (GITIGNORE, DOCKERIGNORE):
        ignored = _read(ignore_file).splitlines()
        assert ".proxy-secret" in ignored
        assert "runtime-secrets/" in ignored or "runtime-secrets" in ignored


def test_nginx_site_has_bounded_uploads_internal_files_and_safe_forwarding():
    nginx = _read(NGINX)
    file_proxy = _read(FILE_PROXY_NGINX)

    assert "server_name cloud.claudcode.xyz;" in nginx
    assert "client_max_body_size 210m;" in nginx
    assert "proxy_pass http://127.0.0.1:18765;" in nginx
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in nginx
    assert "proxy_set_header X-Real-IP $remote_addr;" in nginx
    assert "$proxy_add_x_forwarded_for" not in nginx
    assert "alias /home/ubuntu/tai-pan-cloud/data/files/;" not in nginx
    assert "include /etc/nginx/snippets/tai-pan-cloud-upstream-auth.conf;" in nginx
    assert re.search(
        r"location\s+/_protected_files/\s*\{[^}]*\binternal;",
        file_proxy,
        re.DOTALL,
    )
    assert "alias /protected/;" in file_proxy
    assert "include /etc/nginx/proxy-auth.conf;" in file_proxy
    assert "map_hash_bucket_size 128;" in file_proxy
    assert "proxy_pass http://app:8000;" in file_proxy
    assert 'proxy_set_header X-Tai-Pan-Proxy-Secret "";' in file_proxy
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
    assert "validate_data_directories" in script
    assert "O_DIRECTORY" in script
    assert "compose stop app file_proxy backup" in script
    assert "reject_legacy_layout" in script
    main_stop = script.rindex("\ncompose stop app file_proxy backup\n")
    assert script.index("\nreject_legacy_layout\n") < main_stop
    assert script.index("compose build app") < main_stop
    assert main_stop < script.index("\nvalidate_data_directories\n")
    assert "pre-deploy" in script
    assert "PRAGMA integrity_check" in script
    assert ".pending-" in script
    assert "os.replace" in script
    assert "PREDEPLOY_RETENTION" in script
    assert "rollback_deployment" in script
    assert "--force-recreate app file_proxy backup" in script
    assert "tai-pan-file-proxy:rollback-" in script
    assert "CURRENT_PROXY_CONTAINER" in script
    assert "compose build app file_proxy" in script
    assert "compose pull file_proxy" not in script
    assert "enter_maintenance_site" in script
    assert script.index("\nenter_maintenance_site\n") < main_stop
    main_health = script.rindex(
        "if curl --fail --silent --show-error http://127.0.0.1:18765/health"
    )
    main_commit = script.rindex('commit_nginx_site "$NGINX_TLS_SOURCE"')
    assert main_health < main_commit
    assert "rollback health check failed" in script
    assert "rollback failed to stop the replacement services" in script
    assert "rollback failed to restore the database" in script
    assert "rollback failed to retag the prior images" in script
    assert "rollback failed to restore the prior Nginx files" in script
    assert "rollback failed to validate the prior Nginx configuration" in script
    assert "compose up -d --force-recreate app file_proxy backup || true" not in script
    assert "compose stop app file_proxy backup || true" not in script
    assert "tai-pan-cloud-upstream-auth.conf" in script
    assert "certbot-nginx-reload" in script
    assert 'PROJECT=tai-pan-cloud' in script
    assert 'docker compose --project-name "$PROJECT"' in script
    assert "curl --fail --silent --show-error http://127.0.0.1:18765/health" in script
    assert "docker prune" not in script
    assert "docker system" not in script
    assert "docker stop" not in script
    assert "systemctl restart docker" not in script
    assert "mktemp" not in script


def test_tls_assets_are_staged_and_renewed_with_validation():
    deploy = _read(DEPLOY)
    http_nginx = _read(HTTP_NGINX)
    maintenance_nginx = _read(MAINTENANCE_NGINX)
    hook = _read(CERTBOT_HOOK)

    assert "listen 80;" in http_nginx
    assert "ssl_certificate" not in http_nginx
    assert "listen 443 ssl;" in maintenance_nginx
    assert "return 503;" in maintenance_nginx
    assert "restore_nginx_files" in deploy
    assert "trap" in deploy
    assert "nginx -t" in hook
    assert "systemctl reload nginx" in hook


@pytest.mark.parametrize("failure", ["stop", "database", "retag", "startup", "health"])
def test_application_rollback_fails_closed_under_or_list(
    failure: str,
    tmp_path: Path,
):
    result = _run_deploy_functions(
        f"""
TARGET={shlex.quote(str(tmp_path))}
mkdir -p "$TARGET/data/database"
printf snapshot > "$TARGET/snapshot.sqlite3"
APP_SWITCH_ACTIVE=1
HAD_RUNNING_APP=1
PREDEPLOY_SNAPSHOT="$TARGET/snapshot.sqlite3"
ROLLBACK_TAG=old-app
PROXY_ROLLBACK_TAG=old-proxy
ROLLBACK_HEALTH_ATTEMPTS=1
ROLLBACK_HEALTH_SLEEP_SECONDS=0
compose() {{
    if [[ $1 == stop ]]; then [[ {shlex.quote(failure)} != stop ]]; return; fi
    [[ {shlex.quote(failure)} != startup ]]
}}
install() {{
    [[ {shlex.quote(failure)} != database ]] || return 1
    command cp "${{@: -2:1}}" "${{@: -1}}"
}}
docker() {{ [[ {shlex.quote(failure)} != retag ]]; }}
curl() {{ [[ {shlex.quote(failure)} != health ]]; }}
sleep() {{ :; }}
restore_previous_application || status=$?
[[ ${{status:-0}} -eq 1 ]]
""",
        tmp_path,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("failure", ["app", "nginx_files", "nginx_test", "nginx_reload"])
def test_deployment_rollback_preserves_maintenance_on_every_failure(
    failure: str,
    tmp_path: Path,
):
    result = _run_deploy_functions(
        f"""
NGINX_BACKUP_READY=1
restore_previous_application() {{ [[ {shlex.quote(failure)} != app ]]; }}
persist_maintenance_site() {{ printf maintenance > "$TRACE"; }}
restore_nginx_files() {{ [[ {shlex.quote(failure)} != nginx_files ]]; }}
nginx() {{ [[ {shlex.quote(failure)} != nginx_test ]]; }}
systemctl() {{ [[ {shlex.quote(failure)} != nginx_reload ]]; }}
rollback_deployment || status=$?
[[ ${{status:-0}} -eq 1 ]]
if [[ {shlex.quote(failure)} == app ]]; then
    [[ $(cat "$TRACE") == maintenance ]]
fi
""",
        tmp_path,
    )

    assert result.returncode == 0, result.stderr


def test_first_deploy_enters_http_maintenance_without_existing_site_or_certificate(
    tmp_path: Path,
):
    result = _run_deploy_functions(
        f"""
NGINX_AVAILABLE={shlex.quote(str(tmp_path / 'available'))}
NGINX_ENABLED={shlex.quote(str(tmp_path / 'enabled'))}
NGINX_AUTH_SNIPPET={shlex.quote(str(tmp_path / 'auth'))}
CERTIFICATE={shlex.quote(str(tmp_path / 'missing-certificate'))}
ROLLBACK_DIR={shlex.quote(str(tmp_path / 'rollback'))}
NGINX_HTTP_SOURCE={shlex.quote(str(HTTP_NGINX))}
nginx() {{ :; }}
systemctl() {{ :; }}
enter_maintenance_site
cmp "$NGINX_AVAILABLE" "$NGINX_HTTP_SOURCE"
[[ -L "$NGINX_ENABLED" ]]
[[ $(readlink "$NGINX_ENABLED") == "$NGINX_AVAILABLE" ]]
[[ $NGINX_BACKUP_READY -eq 1 ]]
trap - ERR
""",
        tmp_path,
    )

    assert result.returncode == 0, result.stderr


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
    assert "certbot renew --dry-run --run-deploy-hooks" in docs
    assert "data/backups/manual/app-" in docs
    assert ".proxy-secret" in docs
    assert "runtime-secrets" in docs
