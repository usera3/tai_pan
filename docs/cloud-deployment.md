# Cloud Deployment Runbook

This runbook deploys only `cloud.claudcode.xyz` under
`/home/ubuntu/tai-pan-cloud`. It must not change another Compose project,
Docker network, Nginx site, database, or MQTT service.

## Architecture and fixed boundaries

- Compose project: `tai-pan-cloud`.
- Host listener: `127.0.0.1:18765` only.
- Persistent bind: `/home/ubuntu/tai-pan-cloud/data` mounted at `/data`.
- SQLite: `/data/app.db`; permanent files: `/data/files`; backups:
  `/data/backups`.
- Docker network: `10.203.187.0/28`, gateway `10.203.187.1`.
- Uvicorn runs one worker and trusts proxy headers only from that gateway.
- Nginx overwrites `X-Forwarded-For` with `$remote_addr` and also sets
  `X-Real-IP`. It never appends an inbound forwarding chain.
- There is no PostgreSQL or other database service in this deployment.

## Preflight

Run read-only checks before copying or replacing anything:

```bash
getent ahostsv4 cloud.claudcode.xyz
df -BG /home/ubuntu
ss -ltn '( sport = :18765 )'
docker compose version
docker network inspect tai-pan-cloud-internal 2>/dev/null || true
sudo nginx -t
docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'
curl --fail --silent --show-error https://wd.claudcode.xyz/
curl --fail --silent --show-error https://api.claudcode.xyz/
systemctl is-active mosquitto
```

The DNS result must include `43.153.137.20`, at least 23 GiB must be free,
and port 18765 must either be unused or already owned by this project. Confirm
that no existing Docker network overlaps `10.203.187.0/28`; do not change the
subnet without changing Uvicorn's fixed trusted peer at the same time. Record
the existing container, Nginx, and MQTT status for the post-deploy comparison.

## Release and server-generated secrets

Place the reviewed release directly at `/home/ubuntu/tai-pan-cloud`, owned by
root and not writable by other users. Do not include `.env`, `data`, or a
credential file in the release archive. Then run:

```bash
cd /home/ubuntu/tai-pan-cloud
sudo ./deploy/deploy.sh
curl --fail http://127.0.0.1:18765/health
```

`deploy.sh` refuses another target, creates the data directories with the
container UID, and creates `/home/ubuntu/tai-pan-cloud/.env` directly with
mode `0600`. `SESSION_SECRET` and `KEY_ENCRYPTION_KEY` are generated on the
server and are never printed. Existing secret files are validated, not
replaced. The script builds and operates only this project:

```bash
docker compose --project-name tai-pan-cloud --file deploy/docker-compose.yml ps
```

The tracked `deploy/env.example` contains placeholders, not usable secrets.

## TLS staging

The tracked Nginx site references certificate files, so do not install it
before the certificate exists. On a first deployment, `deploy.sh` validates
the local application and leaves Nginx unchanged. Create a dedicated ACME
webroot and an HTTP-only staging site after backing up any same-name file:

```bash
sudo install -d -m 0755 /var/www/letsencrypt/.well-known/acme-challenge
sudo test ! -e /etc/nginx/sites-available/cloud.claudcode.xyz || \
  sudo cp -a /etc/nginx/sites-available/cloud.claudcode.xyz \
    /home/ubuntu/tai-pan-cloud/rollback/cloud.claudcode.xyz.pre-tls
sudo sh -c 'printf "%s\n" \
"server {" \
"    listen 80;" \
"    listen [::]:80;" \
"    server_name cloud.claudcode.xyz;" \
"    location /.well-known/acme-challenge/ { root /var/www/letsencrypt; }" \
"    location / { return 503; }" \
"}" > /etc/nginx/sites-available/cloud.claudcode.xyz'
sudo ln -sfn /etc/nginx/sites-available/cloud.claudcode.xyz \
  /etc/nginx/sites-enabled/cloud.claudcode.xyz
sudo nginx -t
sudo systemctl reload nginx
sudo certbot certonly --webroot -w /var/www/letsencrypt \
  -d cloud.claudcode.xyz
```

After Certbot succeeds, run `sudo ./deploy/deploy.sh` again. It checks local
health before backing up and replacing only this site's Nginx files, runs
`nginx -t`, and reloads Nginx. A failed certificate request must leave the
HTTP-only staging site in place; it must never install a broken TLS site.

## Bootstrap administrator

Create the initial administrator once. The command writes the random temporary
password to a container-owned `0600` credential file and prints no credential:

```bash
cd /home/ubuntu/tai-pan-cloud
sudo CLOUD_ENV_FILE=/home/ubuntu/tai-pan-cloud/.env \
  docker compose --project-name tai-pan-cloud --file deploy/docker-compose.yml \
  exec -T app python -m app.cloud.admin_cli \
  --database-path /data/app.db \
  --username admin \
  --credentials-file /data/secrets/initial-admin.json
sudo stat -c '%a %u:%g %n' \
  /home/ubuntu/tai-pan-cloud/data/secrets/initial-admin.json
```

The mode must be `0600` and the owner must be UID/GID `10001`. Read
`initial-admin.json` only through an approved secure channel, sign in, and
change the password immediately. Remove the credential file after the change
is confirmed. Never paste its contents into a shell history, ticket, or chat.

## Backups

The `backup` service takes a consistent SQLite Backup API snapshot immediately
on startup and every 24 hours. Publication is atomic, concurrent runs are
serialized, and exactly the latest seven `app-*.sqlite3` files are retained.
The environment file and encryption key are not copied into a backup.

Run a one-shot manual backup with the same implementation:

```bash
cd /home/ubuntu/tai-pan-cloud
sudo CLOUD_ENV_FILE=/home/ubuntu/tai-pan-cloud/.env \
  docker compose --project-name tai-pan-cloud --file deploy/docker-compose.yml \
  run --rm --no-deps backup python -m app.cloud.maintenance backup \
  --database-path /data/app.db --backup-dir /data/backups
sudo ls -l /home/ubuntu/tai-pan-cloud/data/backups/app-*.sqlite3
```

Permanent storage means files survive application/container restart. It does
not mean offsite backup: SQLite snapshots do not include `/data/files`, and
seven local snapshots do not protect against host or account loss. Replicate
both database snapshots and `/data/files` to encrypted offsite storage under a
separate retention policy. Keep `.env` in a separately controlled secret
recovery system, never in the data backup set.

## Restore

Choose a snapshot and verify it before replacement. These commands stop and
start only the dedicated Compose services:

```bash
cd /home/ubuntu/tai-pan-cloud
export SNAPSHOT=/home/ubuntu/tai-pan-cloud/data/backups/app-YYYYMMDDTHHMMSSffffffZ.sqlite3
sudo CLOUD_ENV_FILE=/home/ubuntu/tai-pan-cloud/.env \
  docker compose --project-name tai-pan-cloud --file deploy/docker-compose.yml \
  stop app backup
sudo python3 -c 'import sqlite3, sys; c=sqlite3.connect(sys.argv[1]); assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"; c.close()' "$SNAPSHOT"
sudo cp -a data/app.db "data/app.db.before-restore-$(date -u +%Y%m%dT%H%M%SZ)"
sudo install -m 0640 -o 10001 -g 10001 "$SNAPSHOT" data/app.db.restore
sudo mv -f data/app.db.restore data/app.db
sudo rm -f data/app.db-wal data/app.db-shm
sudo CLOUD_ENV_FILE=/home/ubuntu/tai-pan-cloud/.env \
  docker compose --project-name tai-pan-cloud --file deploy/docker-compose.yml \
  up -d app backup
curl --fail http://127.0.0.1:18765/health
```

Restore `/data/files` from the matching offsite generation when the database
and file tree must be recovered together.

## Rollback

Before a deploy, preserve the current release outside the target's `data`,
`.env`, and `rollback` directories. `deploy.sh` also tags the prior dedicated
image as `tai-pan-cloud:rollback-<UTC timestamp>` and backs up only a replaced
same-name Nginx site under `/home/ubuntu/tai-pan-cloud/rollback`.

To roll back the application, stop this project, retag the selected dedicated
image as `tai-pan-cloud:latest`, restore the matching release files and
database snapshot if schema compatibility requires it, then start and test:

```bash
sudo CLOUD_ENV_FILE=/home/ubuntu/tai-pan-cloud/.env \
  docker compose --project-name tai-pan-cloud --file deploy/docker-compose.yml stop app backup
sudo docker image tag tai-pan-cloud:rollback-YYYYMMDDTHHMMSSZ tai-pan-cloud:latest
sudo CLOUD_ENV_FILE=/home/ubuntu/tai-pan-cloud/.env \
  docker compose --project-name tai-pan-cloud --file deploy/docker-compose.yml up -d app backup
curl --fail http://127.0.0.1:18765/health
```

Restore a backed-up Nginx site only to its original same-name path, run
`sudo nginx -t`, and reload. Do not stop, restart, prune, or recreate unrelated
containers, networks, images, sites, or services during rollback.

## Quotas and capacity

- Maximum uploaded file content: 200 MiB; Nginx allows 210m for multipart
  overhead.
- Per-user permanent storage quota: 1 GiB.
- Global permanent storage quota: 15 GiB.
- Uploads stop when host free space would fall below 8 GiB.

Quota rejection is expected and must not leave a partial file or database row.
Capacity increases require a reviewed application configuration change and a
new free-space assessment; editing Nginx alone does not change quotas.

## Proxy trust verification

Verify the fixed network after every Docker/network change:

```bash
docker network inspect tai-pan-cloud-internal \
  --format '{{(index .IPAM.Config 0).Subnet}} {{(index .IPAM.Config 0).Gateway}}'
sudo nginx -T | sed -n '/server_name cloud.claudcode.xyz/,/^}/p'
```

The output must be `10.203.187.0/28 10.203.187.1`; Compose must pass
`--forwarded-allow-ips=10.203.187.1`, never `*`. Nginx must set
`X-Forwarded-For $remote_addr` and `X-Real-IP $remote_addr`. From a second
client IP, make failed login attempts and confirm the rate-limit/audit source
is the actual client, not an injected header or the Docker gateway.

## Smoke tests and isolation ledger

Run these exact transport checks first:

```bash
curl --fail http://127.0.0.1:18765/health
curl --fail --silent --show-error https://cloud.claudcode.xyz/ >/dev/null
curl --fail --silent --show-error \
  https://cloud.claudcode.xyz/_protected_files/not-authorized || test $? -eq 22
curl --fail --silent --show-error https://wd.claudcode.xyz/ >/dev/null
curl --fail --silent --show-error https://api.claudcode.xyz/ >/dev/null
systemctl is-active mosquitto
docker compose --project-name tai-pan-cloud --file deploy/docker-compose.yml ps
```

Then use two test accounts and record pass/fail evidence for every item:

1. HTTPS sets a Secure session cookie; bootstrap admin must change password.
2. Create one invitation, register once, and confirm reuse is rejected.
3. Each user sees only their own settings, TMP files, links, and permanent files.
4. Save a TMP key and confirm APIs return only configured status, never the key.
5. Upload/list/download/delete one temporary file.
6. Upload/list/download/delete one permanent file through the internal alias.
7. Exceed file and user quota and confirm rejection leaves no partial artifact.
8. Create an automatic download link and confirm it is hidden from normal links.
9. Restart only `app` and confirm sessions/data persist; then create a manual backup.
10. Compare Wireless Debug, `wd.claudcode.xyz`, `api.claudcode.xyz`, and MQTT
    status with the preflight record. Any unrelated change blocks completion.
