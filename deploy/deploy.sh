#!/usr/bin/env bash
set -Eeuo pipefail

TARGET=/home/ubuntu/tai-pan-cloud
PROJECT=tai-pan-cloud
SCRIPT_DIR="$(dirname "$(realpath "$0")")"
COMPOSE_FILE="$TARGET/deploy/docker-compose.yml"
ENV_FILE="$TARGET/.env"
NGINX_SOURCE="$TARGET/deploy/nginx/cloud.claudcode.xyz.conf"
NGINX_AVAILABLE=/etc/nginx/sites-available/cloud.claudcode.xyz
NGINX_ENABLED=/etc/nginx/sites-enabled/cloud.claudcode.xyz
CERTIFICATE=/etc/letsencrypt/live/cloud.claudcode.xyz/fullchain.pem
ROLLBACK_DIR="$TARGET/rollback/$(date -u +%Y%m%dT%H%M%SZ)"

fail() {
    printf 'Deployment error: %s\n' "$1" >&2
    exit 1
}

compose() {
    CLOUD_ENV_FILE="$ENV_FILE" docker compose --project-name tai-pan-cloud --file "$COMPOSE_FILE" "$@"
}

backup_replaced_file() {
    local source=$1
    local name=$2
    if [[ -e "$source" || -L "$source" ]]; then
        install -d -m 0700 "$ROLLBACK_DIR"
        cp -a -- "$source" "$ROLLBACK_DIR/$name"
    fi
}

[[ $EUID -eq 0 ]] || fail "run as root so ownership and Nginx checks are deterministic"
[[ "$SCRIPT_DIR" == "$TARGET/deploy" ]] || fail "deploy.sh must run from $TARGET/deploy"
[[ "$(realpath "$TARGET")" == "$TARGET" ]] || fail "target path must be exactly $TARGET"
[[ -f "$COMPOSE_FILE" && -f "$NGINX_SOURCE" ]] || fail "deployment assets are incomplete"
command -v docker >/dev/null || fail "docker is required"
docker compose version >/dev/null || fail "Docker Compose v2 is required"
command -v curl >/dev/null || fail "curl is required"
command -v nginx >/dev/null || fail "nginx is required"

install -d -m 0750 -o 10001 -g 10001 "$TARGET/data" "$TARGET/data/files" "$TARGET/data/backups"
install -d -m 0700 -o 10001 -g 10001 "$TARGET/data/secrets"
install -d -m 0700 "$TARGET/rollback"

if [[ ! -e "$ENV_FILE" && ! -L "$ENV_FILE" ]]; then
    umask 077
    python3 - "$ENV_FILE" <<'PY'
import base64
import os
import secrets
import sys

path = sys.argv[1]
payload = "\n".join(
    (
        "APP_MODE=cloud",
        "PUBLIC_ORIGIN=https://cloud.claudcode.xyz",
        "DATABASE_PATH=/data/app.db",
        "STORAGE_PATH=/data/files",
        f"SESSION_SECRET={secrets.token_urlsafe(48)}",
        "KEY_ENCRYPTION_KEY="
        + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"),
        "",
    )
).encode("ascii")
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(path, flags, 0o600)
try:
    os.fchmod(descriptor, 0o600)
    offset = 0
    while offset < len(payload):
        offset += os.write(descriptor, payload[offset:])
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
flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(path, flags)
try:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != 0
    ):
        raise SystemExit("production environment file ownership or mode is unsafe")
    raw = os.read(descriptor, 16 * 1024)
    if os.read(descriptor, 1):
        raise SystemExit("production environment file is unexpectedly large")
finally:
    os.close(descriptor)

values = dict(line.split("=", 1) for line in raw.decode("ascii").splitlines())
expected = {
    "APP_MODE",
    "PUBLIC_ORIGIN",
    "DATABASE_PATH",
    "STORAGE_PATH",
    "SESSION_SECRET",
    "KEY_ENCRYPTION_KEY",
}
if set(values) != expected:
    raise SystemExit("production environment file has unexpected keys")
if values["APP_MODE"] != "cloud":
    raise SystemExit("APP_MODE must be cloud")
if values["PUBLIC_ORIGIN"] != "https://cloud.claudcode.xyz":
    raise SystemExit("PUBLIC_ORIGIN is invalid")
if values["DATABASE_PATH"] != "/data/app.db" or values["STORAGE_PATH"] != "/data/files":
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

if docker image inspect tai-pan-cloud:latest >/dev/null 2>&1; then
    docker image tag tai-pan-cloud:latest "tai-pan-cloud:rollback-$(date -u +%Y%m%dT%H%M%SZ)"
fi

compose build app
compose up -d app backup

healthy=0
for _ in $(seq 1 60); do
    if curl --fail --silent --show-error http://127.0.0.1:18765/health >/dev/null; then
        healthy=1
        break
    fi
    sleep 2
done
[[ $healthy -eq 1 ]] || fail "application did not become healthy"

if [[ ! -f "$CERTIFICATE" ]]; then
    printf '%s\n' "Application is healthy; TLS certificate is absent, so Nginx was not changed."
    printf '%s\n' "Complete the staged TLS procedure in docs/cloud-deployment.md, then run this script again."
    exit 0
fi

backup_replaced_file "$NGINX_AVAILABLE" nginx-site-available
backup_replaced_file "$NGINX_ENABLED" nginx-site-enabled
install -m 0644 "$NGINX_SOURCE" "$NGINX_AVAILABLE"
ln -sfn "$NGINX_AVAILABLE" "$NGINX_ENABLED"
nginx -t
systemctl reload nginx

printf '%s\n' "tai-pan-cloud is healthy and its dedicated Nginx site is installed."
