# Cloud Multi-User Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy an invitation-only multi-user TMP.link manager at `cloud.claudcode.xyz`, with encrypted per-user TMP keys and quota-limited private cloud storage, while preserving the current local single-user application.

**Architecture:** Keep `app.main:create_app` as the local entry point and add an isolated `app.cloud` package with its own FastAPI factory, SQLite WAL database, authentication, tenant-aware TMP client, cloud file storage, static UI, and deployment assets. Production selects `APP_MODE=cloud`; Nginx terminates TLS and serves authorized large-file downloads through `X-Accel-Redirect`.

**Tech Stack:** Python 3.10, FastAPI, HTTPX, SQLite, Argon2id, Fernet, vanilla JavaScript, Lucide, pytest, Playwright, Docker Compose, Nginx.

## Global Constraints

- Preserve all local-mode routes, behavior, launchers, and tests.
- Invitation-only registration; invitation codes are single-use and stored only as hashes.
- One encrypted TMP.link API Key per user; never expose plaintext keys in browser responses or logs.
- Permanent cloud files: 200 MiB per file, 1 GiB per user, 15 GiB global, and 8 GiB minimum free disk reserve.
- Permanent cloud files are owner-only and have no public sharing in this version.
- Automatic TMP download links are tenant-scoped, reused, and hidden; user-created links remain visible.
- Production binds only to `127.0.0.1:18765` and must not modify unrelated Docker projects.
- Deployment requires public DNS `cloud.claudcode.xyz -> 43.153.137.20`.

---

### Task 1: Cloud Configuration and Database Foundation

**Files:**
- Modify: `pyproject.toml`
- Create: `app/cloud/__init__.py`
- Create: `app/cloud/config.py`
- Create: `app/cloud/db.py`
- Test: `tests/cloud/test_config.py`
- Test: `tests/cloud/test_db.py`

**Interfaces:**
- Produces `CloudConfig.from_env(environ) -> CloudConfig`.
- Produces `Database(path).initialize()`, `.connection()`, and `.backup(destination)`.

- [ ] **Step 1: Add failing configuration tests**

Test that cloud mode rejects missing `SESSION_SECRET`, `KEY_ENCRYPTION_KEY`, database path, storage path, or public origin; parses exact quota defaults; and local mode does not require cloud secrets.

- [ ] **Step 2: Add failing schema tests**

Initialize a temporary database and assert WAL mode, foreign keys, schema version, and the tables specified in the design: users, invitations, sessions, user_settings, cloud_files, automatic_download_links, audit_events, and auth_attempts.

- [ ] **Step 3: Verify RED**

Run: `.venv/bin/python -m pytest -q tests/cloud/test_config.py tests/cloud/test_db.py`

Expected: import failures because `app.cloud` does not exist.

- [ ] **Step 4: Add dependencies and implementation**

Add bounded dependencies:

```toml
"argon2-cffi>=23,<26",
"cryptography>=42,<47",
```

Implement an immutable config with byte constants:

```python
MAX_FILE_BYTES = 200 * 1024 * 1024
USER_QUOTA_BYTES = 1024 * 1024 * 1024
GLOBAL_QUOTA_BYTES = 15 * 1024 * 1024 * 1024
MIN_FREE_BYTES = 8 * 1024 * 1024 * 1024
```

Use ordered SQL migrations under `Database.initialize()`, `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`, busy timeout, and explicit transactions.

- [ ] **Step 5: Verify GREEN and local regression**

Run: `.venv/bin/python -m pytest -q tests/cloud/test_config.py tests/cloud/test_db.py tests/test_config.py`

- [ ] **Step 6: Commit**

Run: `git add pyproject.toml app/cloud tests/cloud && git commit -m "feat: add cloud configuration and database schema"`

### Task 2: Security Primitives and Repositories

**Files:**
- Create: `app/cloud/security.py`
- Create: `app/cloud/repository.py`
- Test: `tests/cloud/test_security.py`
- Test: `tests/cloud/test_repository.py`

**Interfaces:**
- Produces `PasswordService`, `KeyCipher`, `TokenService`, and `hash_secret(value)`.
- Produces `CloudRepository` methods for users, invitations, sessions, settings, files, automatic links, audit events, and rate-limit attempts.

- [ ] **Step 1: Write failing security tests**

Cover Argon2 password verification, random session/CSRF tokens, SHA-256 token hashes, Fernet round trips, wrong-key failures, and exception redaction.

- [ ] **Step 2: Write failing repository tests**

Cover normalized unique usernames, transactional invitation consumption, opaque session lookup, session revocation, encrypted setting storage, user-scoped file queries, aggregate quota sums, and user-scoped automatic-link lookup/filtering.

- [ ] **Step 3: Verify RED**

Run: `.venv/bin/python -m pytest -q tests/cloud/test_security.py tests/cloud/test_repository.py`

- [ ] **Step 4: Implement security and repository boundaries**

Use Argon2id defaults from `argon2.PasswordHasher`, `secrets.token_urlsafe(32)`, `hashlib.sha256`, and Fernet authenticated encryption. Repository methods must take `user_id` explicitly for tenant data and return typed dataclasses, never raw rows.

- [ ] **Step 5: Verify GREEN**

Run: `.venv/bin/python -m pytest -q tests/cloud/test_security.py tests/cloud/test_repository.py`

- [ ] **Step 6: Commit**

Run: `git add app/cloud/security.py app/cloud/repository.py tests/cloud && git commit -m "feat: add cloud security and tenant repositories"`

### Task 3: Authentication, CSRF, and Invitation APIs

**Files:**
- Create: `app/cloud/dependencies.py`
- Create: `app/cloud/routes/__init__.py`
- Create: `app/cloud/routes/auth.py`
- Create: `app/cloud/schemas.py`
- Create: `app/cloud/app.py`
- Modify: `app/main.py`
- Test: `tests/cloud/test_auth_routes.py`
- Test: `tests/cloud/test_mode_selection.py`

**Interfaces:**
- Produces `create_cloud_app(config, database) -> FastAPI`.
- Produces `/api/auth/register`, `/login`, `/logout`, `/me`, and `/change-password`.
- Produces dependencies `current_user`, `active_user`, `admin_user`, and `verify_csrf`.

- [ ] **Step 1: Write failing auth route tests**

Cover one-time invitation registration, duplicate usernames, login success/failure and throttling, Secure/HttpOnly cookie attributes, CSRF rejection, logout revocation, disabled-user rejection, forced password change, and no credential leakage.

- [ ] **Step 2: Write mode-selection tests**

Assert default/local imports retain the existing app and `APP_MODE=cloud` constructs the cloud app only with complete production configuration.

- [ ] **Step 3: Verify RED**

Run: `.venv/bin/python -m pytest -q tests/cloud/test_auth_routes.py tests/cloud/test_mode_selection.py`

- [ ] **Step 4: Implement the cloud factory and auth routes**

Use an opaque `session` cookie and return the CSRF token only in authenticated JSON. State-changing endpoints require matching Origin and `X-CSRF-Token`. Use generic login errors and store rate-limit state in SQLite.

- [ ] **Step 5: Verify GREEN and local routes**

Run: `.venv/bin/python -m pytest -q tests/cloud/test_auth_routes.py tests/cloud/test_mode_selection.py tests/test_routes.py`

- [ ] **Step 6: Commit**

Run: `git add app/cloud app/main.py tests/cloud && git commit -m "feat: add invitation-based cloud authentication"`

### Task 4: Administrator Workflows

**Files:**
- Create: `app/cloud/admin_cli.py`
- Create: `app/cloud/routes/admin.py`
- Test: `tests/cloud/test_admin.py`

**Interfaces:**
- Produces initial-admin CLI and admin APIs for invitations, users, disable/restore, and forced password reset.

- [ ] **Step 1: Write failing admin tests**

Assert ordinary users receive 403, invitation plaintext is returned once, used invitations cannot be revoked/reused, disabling revokes sessions, password resets force a change, and admins cannot read encrypted or plaintext TMP keys.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest -q tests/cloud/test_admin.py`

- [ ] **Step 3: Implement admin service and CLI**

The bootstrap command creates a random temporary password, stores only its hash, writes plaintext once to a caller-specified `0600` file, and emits no password to logs. API responses expose invitation plaintext only at creation.

- [ ] **Step 4: Verify GREEN**

Run: `.venv/bin/python -m pytest -q tests/cloud/test_admin.py`

- [ ] **Step 5: Commit**

Run: `git add app/cloud tests/cloud && git commit -m "feat: add cloud invitation and user administration"`

### Task 5: Tenant TMP.link Operations

**Files:**
- Modify: `app/client.py`
- Create: `app/cloud/tmp_service.py`
- Create: `app/cloud/routes/settings.py`
- Create: `app/cloud/routes/tmp_files.py`
- Create: `app/cloud/routes/links.py`
- Test: `tests/cloud/test_tmp_tenants.py`
- Modify: `tests/test_client.py`

**Interfaces:**
- Consumes current `TmpLinkClient` actions.
- Produces tenant-scoped settings, temporary upload/file/download/delete, and direct-link APIs.

- [ ] **Step 1: Write failing tenant-isolation tests**

Use two users with different encrypted keys. Assert each remote client receives only that user's key, user A cannot access user B automatic links, auto links are reused/hidden per user, and manually created links remain visible.

- [ ] **Step 2: Write streaming-client tests**

Add an upload interface accepting a staged file object instead of requiring a full `bytes` value, while preserving the existing local bytes API.

- [ ] **Step 3: Verify RED**

Run: `.venv/bin/python -m pytest -q tests/cloud/test_tmp_tenants.py tests/test_client.py`

- [ ] **Step 4: Implement tenant services and routes**

Decrypt keys only inside request scope. Cloud mode accepts TMP models 0, 1, and 2, stages uploads with exact size checks, and never sends model 99 to TMP.link. Automatic links move from local JSON to the database table keyed by `user_id`.

- [ ] **Step 5: Verify GREEN**

Run: `.venv/bin/python -m pytest -q tests/cloud/test_tmp_tenants.py tests/test_client.py`

- [ ] **Step 6: Commit**

Run: `git add app/client.py app/cloud tests && git commit -m "feat: add tenant-scoped tmp link operations"`

### Task 6: Permanent Cloud File Storage

**Files:**
- Create: `app/cloud/storage.py`
- Create: `app/cloud/routes/cloud_files.py`
- Test: `tests/cloud/test_storage.py`
- Test: `tests/cloud/test_cloud_files.py`

**Interfaces:**
- Produces stream staging, quota validation, atomic finalize/delete, combined file listing, and authorized `X-Accel-Redirect` responses.

- [ ] **Step 1: Write failing storage tests**

Cover 200 MiB exact boundary, oversized streams, 1 GiB user quota, 15 GiB global quota, mocked 8 GiB disk reserve, interrupted upload cleanup, UUID disk names, SHA-256, and path traversal filenames.

- [ ] **Step 2: Write failing authorization tests**

Assert owner download/delete succeeds; another user and an admin receive 403; deleted or missing files do not leak paths; `Content-Disposition` handles Unicode safely; permanent upload never calls TMP.link.

- [ ] **Step 3: Verify RED**

Run: `.venv/bin/python -m pytest -q tests/cloud/test_storage.py tests/cloud/test_cloud_files.py`

- [ ] **Step 4: Implement storage and routes**

Stream in fixed chunks to a user-scoped temporary directory, compute SHA-256, perform final quota checks in a transaction, and atomically rename to UUID storage. Authorized download returns `X-Accel-Redirect: /_protected_files/<relative-id>`.

- [ ] **Step 5: Verify GREEN**

Run: `.venv/bin/python -m pytest -q tests/cloud/test_storage.py tests/cloud/test_cloud_files.py`

- [ ] **Step 6: Commit**

Run: `git add app/cloud tests/cloud && git commit -m "feat: add quota-limited permanent cloud files"`

### Task 7: Cloud User Interface

**Files:**
- Create: `app/static/cloud.html`
- Create: `app/static/cloud.css`
- Create: `app/static/cloud.js`
- Test: `tests/cloud/test_cloud_static.py`
- Create: `tests/e2e/cloud.spec.js`
- Modify: `playwright.config.js`

**Interfaces:**
- Consumes cloud auth, settings, files, links, and admin APIs.
- Produces login/register, forced-password-change, user dashboard, mixed-source files, settings, links, and admin views.

- [ ] **Step 1: Write failing static and Playwright tests**

Cover invitation registration, login, forced password change, logout, encrypted-Key status, permanent versus temporary upload, source labels, background download without navigation, owner delete, admin-only navigation, invitation creation, disabled user handling, desktop/mobile overflow, and no browser credential storage.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest -q tests/cloud/test_cloud_static.py && npx playwright test tests/e2e/cloud.spec.js`

- [ ] **Step 3: Implement the cloud UI**

Use the existing quiet work-tool visual language and local Lucide assets. Keep auth forms unframed on a focused page, use source badges for `钛盘` and `云端永久`, show quota progress without decorative cards, and use explicit dialogs for destructive actions.

- [ ] **Step 4: Verify GREEN and inspect screenshots**

Run: `.venv/bin/python -m pytest -q tests/cloud/test_cloud_static.py && npx playwright test tests/e2e/cloud.spec.js`

Inspect desktop and 390 px mobile screenshots for overlap, clipping, hidden actions, and blank states.

- [ ] **Step 5: Commit**

Run: `git add app/static tests playwright.config.js && git commit -m "feat: add cloud multi-user interface"`

### Task 8: Backup and Deployment Assets

**Files:**
- Create: `app/cloud/maintenance.py`
- Create: `tests/cloud/test_maintenance.py`
- Create: `Dockerfile`
- Create: `deploy/docker-compose.yml`
- Create: `deploy/env.example`
- Create: `deploy/nginx/cloud.claudcode.xyz.conf`
- Create: `deploy/deploy.sh`
- Create: `docs/cloud-deployment.md`
- Modify: `.gitignore`
- Modify: `README.md`

**Interfaces:**
- Produces daily SQLite backups with seven-file retention, health endpoint, isolated Compose deployment, Nginx template, and rollback instructions.

- [ ] **Step 1: Write failing backup and deployment contract tests**

Assert consistent backup creation/retention, no secrets in tracked deployment files, localhost-only port mapping, non-root container user, persistent data mount, healthcheck, exact PostgreSQL non-use, 210m Nginx body limit, internal protected-file location, and existing-service isolation.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest -q tests/cloud/test_maintenance.py tests/cloud/test_deployment_contract.py`

- [ ] **Step 3: Implement maintenance and deployment files**

Run one Uvicorn worker, create secrets outside Git with mode 0600, back up replaced server files, and make `deploy.sh` operate only in `/home/ubuntu/tai-pan-cloud`. Document bootstrap, restore, rollback, and quota behavior.

- [ ] **Step 4: Build and verify locally**

Run: `docker compose -f deploy/docker-compose.yml config`

Run: `docker build -t tai-pan-cloud:test .`

Run: `.venv/bin/python -m pytest -q`

Run: `npx playwright test`

- [ ] **Step 5: Commit**

Run: `git add .gitignore Dockerfile deploy docs README.md app/cloud tests && git commit -m "feat: prepare isolated cloud deployment"`

### Task 9: Server Deployment and Production Verification

**Files:**
- Server create: `/home/ubuntu/tai-pan-cloud/`
- Server create: `/etc/nginx/sites-available/cloud.claudcode.xyz`
- Server create: `/etc/nginx/sites-enabled/cloud.claudcode.xyz`

**Interfaces:**
- Consumes all prior tasks and public DNS.
- Produces the running HTTPS service at `https://cloud.claudcode.xyz`.

- [ ] **Step 1: Re-run read-only preflight**

Verify DNS resolves to `43.153.137.20`, disk has at least 23 GiB free, port 18765 is unused, existing Compose services are healthy, and `nginx -t` passes.

- [ ] **Step 2: Deploy without exposing secrets**

Copy a release archive to the dedicated directory, generate production secrets directly on the server, build the isolated Compose project, and verify `http://127.0.0.1:18765/health` before touching Nginx.

- [ ] **Step 3: Bootstrap administrator**

Create the initial admin with a one-time password written only to a 0600 credential file. Do not print it in chat or command logs.

- [ ] **Step 4: Install Nginx and TLS safely**

Back up any target site file, install the dedicated config, run `nginx -t`, reload Nginx, obtain the certificate, and re-run `nginx -t` before the final reload.

- [ ] **Step 5: Production smoke tests**

Verify HTTPS, Secure Cookie, invitation registration, user isolation, encrypted TMP Key status, temporary upload, permanent upload/download/delete, quota rejection, automatic-link hiding, restart persistence, and backup creation.

- [ ] **Step 6: Verify unrelated services**

Check Wireless Debug Compose, `wd.claudcode.xyz`, `api.claudcode.xyz`, MQTT, and Nginx remain healthy. Do not perform global Docker cleanup.

- [ ] **Step 7: Publish branch and handoff**

Push `feature/cloud-multi-user`, provide the service URL and credential-file location, and preserve the worktree for follow-up until the PR is merged.
