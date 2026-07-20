#!/usr/bin/env bash
set -Eeuo pipefail

TARGET=/home/ubuntu/tai-pan-cloud
PROJECT=tai-pan-cloud
SCRIPT_DIR="$(dirname "$(realpath "$0")")"
COMPOSE_FILE="$TARGET/deploy/docker-compose.yml"
ENV_FILE="$TARGET/.env"
PROXY_TOKEN_FILE="$TARGET/.proxy-secret"
PROXY_AUTH_FILE="$TARGET/runtime-secrets/file-proxy-auth.conf"
NGINX_TLS_SOURCE="$TARGET/deploy/nginx/cloud.claudcode.xyz.conf"
NGINX_HTTP_SOURCE="$TARGET/deploy/nginx/cloud.claudcode.xyz.http.conf"
NGINX_MAINTENANCE_SOURCE="$TARGET/deploy/nginx/cloud.claudcode.xyz.maintenance.conf"
CERTBOT_HOOK_SOURCE="$TARGET/deploy/certbot-nginx-reload.sh"
NGINX_AVAILABLE=/etc/nginx/sites-available/cloud.claudcode.xyz
NGINX_ENABLED=/etc/nginx/sites-enabled/cloud.claudcode.xyz
NGINX_AUTH_SNIPPET=/etc/nginx/snippets/tai-pan-cloud-upstream-auth.conf
CERTBOT_HOOK=/etc/letsencrypt/renewal-hooks/deploy/tai-pan-cloud-nginx-reload
CERTIFICATE=/etc/letsencrypt/live/cloud.claudcode.xyz/fullchain.pem
ROLLBACK_DIR="$TARGET/rollback/$(date -u +%Y%m%dT%H%M%SZ)"
PREDEPLOY_RETENTION=5
PREDEPLOY_SNAPSHOT=
ROLLBACK_TAG=
PROXY_ROLLBACK_TAG=
HAD_RUNNING_APP=0
HAD_RUNNING_PROXY=0
APP_SWITCH_ACTIVE=0
NGINX_BACKUP_READY=0

fail() {
    printf 'Deployment error: %s\n' "$1" >&2
    if [[ ${APP_SWITCH_ACTIVE:-0} -eq 1 || ${NGINX_BACKUP_READY:-0} -eq 1 ]]; then
        trap - ERR
        rollback_deployment || true
    fi
    exit 1
}

compose() {
    local compose_env=$ENV_FILE
    [[ -f "$compose_env" ]] || compose_env="$TARGET/deploy/env.example"
    CLOUD_ENV_FILE="$compose_env" docker compose --project-name "$PROJECT" --file "$COMPOSE_FILE" "$@"
}

reject_legacy_layout() {
    if [[ -e "$TARGET/data/app.db" || -L "$TARGET/data/app.db" || \
          -e "$TARGET/data/secrets" || -L "$TARGET/data/secrets" ]]; then
        fail "unsupported legacy data layout detected; migrate it before running this release"
    fi
}

restore_previous_application() {
    [[ $APP_SWITCH_ACTIVE -eq 1 ]] || return 0
    APP_SWITCH_ACTIVE=0
    compose stop app file_proxy backup || true
    if [[ $HAD_RUNNING_APP -eq 1 ]]; then
        if [[ -n "$PREDEPLOY_SNAPSHOT" ]]; then
            rm -f -- "$TARGET/data/database/.app.db.rollback-restore"
            install -m 0600 -o 10001 -g 10001 "$PREDEPLOY_SNAPSHOT" \
                "$TARGET/data/database/.app.db.rollback-restore"
            mv -f -- "$TARGET/data/database/.app.db.rollback-restore" \
                "$TARGET/data/database/app.db"
            rm -f -- "$TARGET/data/database/app.db-wal" "$TARGET/data/database/app.db-shm"
        fi
        docker image tag "$ROLLBACK_TAG" tai-pan-cloud:latest
        docker image tag "$PROXY_ROLLBACK_TAG" tai-pan-file-proxy:latest
        if ! compose up -d --force-recreate app file_proxy backup; then
            printf '%s\n' "Deployment rollback failed to restart the prior services." >&2
            return 1
        fi
        for _ in $(seq 1 60); do
            if curl --fail --silent --show-error http://127.0.0.1:18765/health >/dev/null; then
                return 0
            fi
            sleep 2
        done
        printf '%s\n' "Deployment rollback health check failed." >&2
        return 1
    fi
    return 0
}

rollback_deployment() {
    local application_restored=0
    restore_previous_application || application_restored=$?
    if [[ $application_restored -ne 0 ]]; then
        printf '%s\n' "Nginx remains in maintenance mode because application recovery failed." >&2
        return "$application_restored"
    fi
    restore_nginx_files
    if [[ $NGINX_BACKUP_READY -eq 1 ]]; then
        nginx -t && systemctl reload nginx
    fi
    NGINX_BACKUP_READY=0
}

backup_replaced_file() {
    local source=$1
    local name=$2
    if [[ -e "$source" || -L "$source" ]]; then
        install -d -m 0700 "$ROLLBACK_DIR"
        cp -a -- "$source" "$ROLLBACK_DIR/$name"
    fi
}

validate_nginx_targets() {
    if [[ -L "$NGINX_AVAILABLE" || (-e "$NGINX_AVAILABLE" && ! -f "$NGINX_AVAILABLE") ]]; then
        fail "$NGINX_AVAILABLE must be absent or a regular file"
    fi
    if [[ -e "$NGINX_ENABLED" && ! -f "$NGINX_ENABLED" && ! -L "$NGINX_ENABLED" ]]; then
        fail "$NGINX_ENABLED must be absent, a regular file, or a symlink"
    fi
    if [[ -L "$NGINX_AUTH_SNIPPET" || (-e "$NGINX_AUTH_SNIPPET" && ! -f "$NGINX_AUTH_SNIPPET") ]]; then
        fail "$NGINX_AUTH_SNIPPET must be absent or a regular file"
    fi
}

prepare_nginx_backup() {
    validate_nginx_targets
    backup_replaced_file "$NGINX_AVAILABLE" nginx-site-available
    backup_replaced_file "$NGINX_ENABLED" nginx-site-enabled
    backup_replaced_file "$NGINX_AUTH_SNIPPET" nginx-auth-snippet
    NGINX_BACKUP_READY=1
}

restore_nginx_files() {
    [[ $NGINX_BACKUP_READY -eq 1 ]] || return 0
    rm -f -- "$NGINX_AVAILABLE" "$NGINX_ENABLED" "$NGINX_AUTH_SNIPPET"
    [[ ! -e "$ROLLBACK_DIR/nginx-site-available" && ! -L "$ROLLBACK_DIR/nginx-site-available" ]] || \
        cp -a -- "$ROLLBACK_DIR/nginx-site-available" "$NGINX_AVAILABLE"
    [[ ! -e "$ROLLBACK_DIR/nginx-site-enabled" && ! -L "$ROLLBACK_DIR/nginx-site-enabled" ]] || \
        cp -a -- "$ROLLBACK_DIR/nginx-site-enabled" "$NGINX_ENABLED"
    [[ ! -e "$ROLLBACK_DIR/nginx-auth-snippet" && ! -L "$ROLLBACK_DIR/nginx-auth-snippet" ]] || \
        cp -a -- "$ROLLBACK_DIR/nginx-auth-snippet" "$NGINX_AUTH_SNIPPET"
}

rollback_nginx_on_error() {
    local status=$?
    trap - ERR
    rollback_deployment || true
    exit "$status"
}

write_host_proxy_auth() {
    python3 - "$PROXY_TOKEN_FILE" "$NGINX_AUTH_SNIPPET" <<'PY'
import os
import stat
import sys

def write_all(descriptor, payload):
    offset = 0
    while offset < len(payload):
        offset += os.write(descriptor, payload[offset:])

token_path, destination = sys.argv[1:]
token_fd = os.open(token_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
try:
    metadata = os.fstat(token_fd)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise SystemExit("proxy token ownership or mode is unsafe")
    token = os.read(token_fd, 256).decode("ascii").strip()
finally:
    os.close(token_fd)
if not token or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in token):
    raise SystemExit("proxy token is invalid")

if os.path.lexists(destination):
    metadata = os.lstat(destination)
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit("proxy auth snippet is not a regular file")
flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(destination, flags, 0o600)
try:
    os.fchmod(descriptor, 0o600)
    write_all(descriptor, f'proxy_set_header X-Tai-Pan-Proxy-Secret "{token}";\n'.encode("ascii"))
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
}

replace_nginx_site() {
    local source=$1
    install -m 0644 "$source" "$NGINX_AVAILABLE"
    rm -f -- "$NGINX_ENABLED"
    ln -s "$NGINX_AVAILABLE" "$NGINX_ENABLED"
    nginx -t
    systemctl reload nginx
}

enter_maintenance_site() {
    prepare_nginx_backup
    trap rollback_nginx_on_error ERR
    if [[ -f "$CERTIFICATE" ]]; then
        replace_nginx_site "$NGINX_MAINTENANCE_SOURCE"
    else
        replace_nginx_site "$NGINX_HTTP_SOURCE"
    fi
}

commit_nginx_site() {
    local source=$1
    write_host_proxy_auth
    replace_nginx_site "$source"
}

validate_data_directories() {
    python3 - "$TARGET" <<'PY'
import os
import stat
import sys

target = sys.argv[1]
flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
target_fd = os.open(target, flags)

def ensure(parent_fd, name, mode, uid, gid):
    try:
        os.mkdir(name, mode, dir_fd=parent_fd)
    except FileExistsError:
        pass
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise SystemExit(f"unsafe deployment directory: {name}")
    os.fchown(descriptor, uid, gid)
    os.fchmod(descriptor, mode)
    return descriptor

try:
    data_fd = ensure(target_fd, "data", 0o755, 0, 0)
    runtime_fd = ensure(target_fd, "runtime-secrets", 0o750, 0, 0)
    os.close(runtime_fd)
    try:
        for name in ("database", "files", "backups", "credentials"):
            os.close(ensure(data_fd, name, 0o700, 10001, 10001))
        os.close(ensure(data_fd, "pre-deploy", 0o700, 0, 0))
    finally:
        os.close(data_fd)
finally:
    os.close(target_fd)
PY
}

create_predeploy_snapshot() {
    python3 - "$TARGET/data/database/app.db" "$TARGET/data/pre-deploy" "$PREDEPLOY_RETENTION" <<'PY'
import os
import sqlite3
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

source = Path(sys.argv[1])
destination_dir = Path(sys.argv[2])
retention = int(sys.argv[3])
if not os.path.lexists(source):
    raise SystemExit(0)
metadata = source.lstat()
if not stat.S_ISREG(metadata.st_mode):
    raise SystemExit("database path is not a regular file")
destination = destination_dir / (
    "app-pre-deploy-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".sqlite3"
)
staging = destination_dir / f".{destination.name}.pending-{uuid4().hex}"
old_umask = os.umask(0o077)
try:
    for abandoned in destination_dir.glob(".app-pre-deploy-*.sqlite3.pending-*"):
        abandoned.unlink()
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as current:
        with sqlite3.connect(staging) as snapshot:
            current.backup(snapshot)
    os.chmod(staging, 0o600)
    with sqlite3.connect(f"file:{staging}?mode=ro", uri=True) as snapshot:
        result = snapshot.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise SystemExit("pre-deploy database snapshot failed integrity_check")
    descriptor = os.open(staging, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(staging, destination)
    snapshots = sorted(destination_dir.glob("app-pre-deploy-*.sqlite3"))
    for obsolete in snapshots[:-retention]:
        obsolete.unlink()
    directory_fd = os.open(destination_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    print(destination)
finally:
    staging.unlink(missing_ok=True)
    os.umask(old_umask)
PY
}

generate_runtime_secrets() {
    python3 - "$PROXY_TOKEN_FILE" "$PROXY_AUTH_FILE" <<'PY'
import os
import secrets
import stat
import sys

def write_all(descriptor, payload):
    offset = 0
    while offset < len(payload):
        offset += os.write(descriptor, payload[offset:])

token_path, config_path = sys.argv[1:]
if not os.path.lexists(token_path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(token_path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        write_all(descriptor, (secrets.token_urlsafe(48) + "\n").encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

descriptor = os.open(token_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
try:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != 0:
        raise SystemExit("proxy token ownership or mode is unsafe")
    token = os.read(descriptor, 256).decode("ascii").strip()
finally:
    os.close(descriptor)
if not token or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in token):
    raise SystemExit("proxy token is invalid")

if os.path.lexists(config_path) and not stat.S_ISREG(os.lstat(config_path).st_mode):
    raise SystemExit("file proxy auth config is not a regular file")
flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(config_path, flags, 0o440)
try:
    os.fchown(descriptor, 0, 10001)
    os.fchmod(descriptor, 0o440)
    payload = (
        "map $http_x_tai_pan_proxy_secret $tai_pan_proxy_authorized {\n"
        "    default 0;\n"
        f'    "{token}" 1;\n'
        "}\n"
    ).encode("ascii")
    write_all(descriptor, payload)
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
}

[[ $EUID -eq 0 ]] || fail "run as root so ownership and Nginx checks are deterministic"
[[ "$SCRIPT_DIR" == "$TARGET/deploy" ]] || fail "deploy.sh must run from $TARGET/deploy"
[[ "$(realpath "$TARGET")" == "$TARGET" ]] || fail "target path must be exactly $TARGET"
[[ -f "$COMPOSE_FILE" && -f "$NGINX_TLS_SOURCE" && -f "$NGINX_HTTP_SOURCE" && -f "$NGINX_MAINTENANCE_SOURCE" && -f "$CERTBOT_HOOK_SOURCE" ]] || fail "deployment assets are incomplete"
command -v docker >/dev/null || fail "docker is required"
docker compose version >/dev/null || fail "Docker Compose v2 is required"
command -v curl >/dev/null || fail "curl is required"
command -v nginx >/dev/null || fail "nginx is required"

reject_legacy_layout

if [[ ! -e "$ENV_FILE" && ! -L "$ENV_FILE" ]]; then
    umask 077
    python3 - "$ENV_FILE" <<'PY'
import base64
import os
import secrets
import sys

def write_all(descriptor, payload):
    offset = 0
    while offset < len(payload):
        offset += os.write(descriptor, payload[offset:])

path = sys.argv[1]
payload = "\n".join(
    (
        "APP_MODE=cloud",
        "PUBLIC_ORIGIN=https://cloud.claudcode.xyz",
        "DATABASE_PATH=/data/database/app.db",
        "STORAGE_PATH=/data/files",
        f"SESSION_SECRET={secrets.token_urlsafe(48)}",
        "KEY_ENCRYPTION_KEY=" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"),
        "",
    )
).encode("ascii")
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(path, flags, 0o600)
try:
    os.fchmod(descriptor, 0o600)
    write_all(descriptor, payload)
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
fi

python3 - "$ENV_FILE" <<'PY'
import base64
import os
import stat
import sys

path = sys.argv[1]
descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
try:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != 0:
        raise SystemExit("production environment file ownership or mode is unsafe")
    raw = os.read(descriptor, 16 * 1024)
    if os.read(descriptor, 1):
        raise SystemExit("production environment file is unexpectedly large")
finally:
    os.close(descriptor)

values = dict(line.split("=", 1) for line in raw.decode("ascii").splitlines())
expected = {"APP_MODE", "PUBLIC_ORIGIN", "DATABASE_PATH", "STORAGE_PATH", "SESSION_SECRET", "KEY_ENCRYPTION_KEY"}
if set(values) != expected:
    raise SystemExit("production environment file has unexpected keys")
if values["APP_MODE"] != "cloud" or values["PUBLIC_ORIGIN"] != "https://cloud.claudcode.xyz":
    raise SystemExit("cloud mode or public origin is invalid")
if values["DATABASE_PATH"] != "/data/database/app.db" or values["STORAGE_PATH"] != "/data/files":
    raise SystemExit("database or storage path is invalid")
if len(values["SESSION_SECRET"]) < 48:
    raise SystemExit("SESSION_SECRET is invalid")
try:
    decoded_key = base64.urlsafe_b64decode(values["KEY_ENCRYPTION_KEY"].encode("ascii"))
except ValueError:
    raise SystemExit("KEY_ENCRYPTION_KEY is invalid") from None
if len(decoded_key) != 32:
    raise SystemExit("KEY_ENCRYPTION_KEY is invalid")
PY

CURRENT_APP_CONTAINER="$(compose ps -q app)"
CURRENT_PROXY_CONTAINER="$(compose ps -q file_proxy)"
if [[ -n "$CURRENT_APP_CONTAINER" && \
      "$(docker inspect --format '{{.State.Running}}' "$CURRENT_APP_CONTAINER")" == true ]]; then
    HAD_RUNNING_APP=1
    if [[ -z "$CURRENT_PROXY_CONTAINER" || \
          "$(docker inspect --format '{{.State.Running}}' "$CURRENT_PROXY_CONTAINER")" != true ]]; then
        fail "running app has no running file proxy; refusing an unsafe upgrade"
    fi
    HAD_RUNNING_PROXY=1
    ROLLBACK_TAG="tai-pan-cloud:rollback-$(date -u +%Y%m%dT%H%M%SZ)"
    PROXY_ROLLBACK_TAG="tai-pan-file-proxy:rollback-$(date -u +%Y%m%dT%H%M%SZ)"
    docker image tag \
        "$(docker inspect --format '{{.Image}}' "$CURRENT_APP_CONTAINER")" \
        "$ROLLBACK_TAG"
    docker image tag \
        "$(docker inspect --format '{{.Image}}' "$CURRENT_PROXY_CONTAINER")" \
        "$PROXY_ROLLBACK_TAG"
fi

compose build app file_proxy

install -d -m 0700 "$TARGET/rollback"
install -d -m 0755 /var/www/letsencrypt/.well-known/acme-challenge
install -d -m 0755 /etc/nginx/snippets
if [[ -f "$CERTIFICATE" ]]; then
    install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
    install -m 0755 "$CERTBOT_HOOK_SOURCE" "$CERTBOT_HOOK"
fi

enter_maintenance_site

APP_SWITCH_ACTIVE=1
compose stop app file_proxy backup
compose --profile bootstrap stop bootstrap
validate_data_directories
generate_runtime_secrets
PREDEPLOY_SNAPSHOT="$(create_predeploy_snapshot)"

compose up -d app file_proxy

healthy=0
for _ in $(seq 1 60); do
    if curl --fail --silent --show-error http://127.0.0.1:18765/health >/dev/null; then
        healthy=1
        break
    fi
    sleep 2
done
[[ $healthy -eq 1 ]] || fail "application did not become healthy"

compose up -d backup

if [[ ! -f "$CERTIFICATE" ]]; then
    commit_nginx_site "$NGINX_HTTP_SOURCE"
    APP_SWITCH_ACTIVE=0
    NGINX_BACKUP_READY=0
    trap - ERR
    printf '%s\n' "Application is healthy and the HTTP-only ACME site is installed."
    printf '%s\n' "Obtain the TLS certificate as documented, then run this script again."
    exit 0
fi

commit_nginx_site "$NGINX_TLS_SOURCE"
APP_SWITCH_ACTIVE=0
NGINX_BACKUP_READY=0
trap - ERR

printf '%s\n' "tai-pan-cloud is healthy and its dedicated Nginx TLS site is installed."
