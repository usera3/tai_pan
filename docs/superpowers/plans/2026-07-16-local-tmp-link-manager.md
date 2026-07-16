# Local TMP Link Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a localhost-only web application for uploading TMP.link files, listing workspace files, and creating, copying, and deleting direct links.

**Architecture:** FastAPI owns local settings, protects the API key, proxies requests to the two TMP.link endpoints, and serves a static single-page interface. The browser never receives the saved key; all remote behavior is wrapped behind a typed client and tested with HTTPX mock transports.

**Tech Stack:** Python 3.10+, FastAPI, Uvicorn, HTTPX, Pydantic, pytest, vanilla HTML/CSS/JavaScript, Lucide, Playwright

## Global Constraints

- Bind only to `127.0.0.1:8765`.
- Store the API key only in ignored `.local/settings.json`; never commit, log, or return it.
- Use `pan.cloudcode.xyz` as the default custom domain.
- Support batch selection, with files uploaded one at a time for independent status and retry.
- Never claim upload-to-remote percentage after the local browser upload completes; show an indeterminate processing state.
- Keep the UI work-focused, responsive, and free of nested cards or overlapping controls.
- Do not use the API key exposed in screenshots or conversation.

---

### Task 1: Project Scaffold and Secure Settings Store

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `app/__init__.py`
- Create: `app/config.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Produces: `AppSettings(api_key: str, custom_domain: str)`
- Produces: `SettingsStore(path: Path)` with `load()`, `update(api_key, custom_domain)`, and `clear_key()`
- Default custom domain: `pan.cloudcode.xyz`

- [ ] **Step 1: Write failing settings tests**

```python
from pathlib import Path

from app.config import SettingsStore


def test_settings_store_never_exposes_saved_key(tmp_path: Path):
    store = SettingsStore(tmp_path / "settings.json")
    store.update(api_key="secret-value", custom_domain="pan.cloudcode.xyz")

    public = store.public_settings()

    assert public == {"key_configured": True, "custom_domain": "pan.cloudcode.xyz"}
    assert "secret-value" not in repr(public)


def test_empty_key_update_preserves_existing_key(tmp_path: Path):
    store = SettingsStore(tmp_path / "settings.json")
    store.update(api_key="first-key", custom_domain="pan.cloudcode.xyz")
    store.update(api_key="", custom_domain="files.example.com")

    assert store.load().api_key == "first-key"
    assert store.load().custom_domain == "files.example.com"


def test_clear_key_removes_only_key(tmp_path: Path):
    store = SettingsStore(tmp_path / "settings.json")
    store.update(api_key="secret-value", custom_domain="files.example.com")

    store.clear_key()

    assert store.load().api_key == ""
    assert store.load().custom_domain == "files.example.com"
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest tests/test_config.py -q`

Expected: FAIL because the project package and settings store do not exist.

- [ ] **Step 3: Add package metadata and dependencies**

Create `pyproject.toml` with Python `>=3.10` and dependencies `fastapi`, `uvicorn[standard]`, `httpx`, and `python-multipart`. Add dev dependencies `pytest`, `pytest-asyncio`, and `playwright`. Configure pytest with `pythonpath = ["."]` and `asyncio_mode = "auto"`.

Create `.gitignore` containing:

```text
.venv/
.local/
__pycache__/
*.py[cod]
.pytest_cache/
test-results/
playwright-report/
```

- [ ] **Step 4: Implement the settings store**

Use an immutable dataclass, atomic temporary-file replacement, UTF-8 JSON, and `chmod(0o600)` when supported. Reject custom domains containing a URL scheme, slash, whitespace, or an empty value. `public_settings()` returns only `key_configured` and `custom_domain`.

- [ ] **Step 5: Install and verify GREEN**

Run: `python3 -m venv .venv`

Run: `.venv/bin/python -m pip install -e '.[dev]'`

Run: `.venv/bin/python -m pytest tests/test_config.py -q`

Expected: 3 tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add pyproject.toml .gitignore app/__init__.py app/config.py tests/test_config.py
git commit -m "feat: add secure local settings store"
```

---

### Task 2: Typed TMP.link API Client

**Files:**
- Create: `app/client.py`
- Create: `app/models.py`
- Create: `tests/test_client.py`

**Interfaces:**
- Produces: `TmpLinkClient(api_key, transport=None, timeout=30.0)`
- Produces async methods: `quota()`, `list_files(page)`, `list_links(page)`, `upload(file_name, content, model)`, `create_link(ukey, valid_time, download_limit)`, `delete_link(dkey, delete_file)`
- Produces exceptions: `TmpLinkBusinessError`, `TmpLinkTimeoutError`, `TmpLinkConnectionError`
- Produces normalized `ServiceResult(ok, data, message)`

- [ ] **Step 1: Write failing request-mapping tests**

Use `httpx.MockTransport` to capture requests. Assert:

```python
assert form == {"action": "quota", "key": "test-key"}
assert create_form == {
    "action": "link_add",
    "key": "test-key",
    "ukey": "FILE-UKEY",
    "valid_time": "60",
    "download_limit": "3",
}
assert delete_form == {
    "action": "link_del",
    "key": "test-key",
    "dkey": "DIRECT-DKEY",
    "delete": "1",
}
```

For upload, assert the request targets `https://tmp-cli.vx-cdn.com/app/upload_cli`, contains multipart fields `key`, `model`, and the original filename, and rejects models outside `{0, 1, 2, 99}`.

- [ ] **Step 2: Write failing response/error tests**

Cover successful `{"status": 1, "data": ...}`, business failure `status != 1`, `httpx.TimeoutException`, and `httpx.ConnectError`. Verify exception strings never contain `test-key`.

- [ ] **Step 3: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_client.py -q`

Expected: FAIL because `app.client` and models are missing.

- [ ] **Step 4: Implement the remote client**

Use one private `_direct(action, **fields)` method for form actions. Omit optional fields when `None` or empty. Decode JSON defensively, accept numeric or string status `1`, and use documented status codes as fallback messages. Do not include request form data in exceptions.

- [ ] **Step 5: Run tests and verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_client.py -q`

Expected: all client mapping and error tests pass.

- [ ] **Step 6: Commit Task 2**

```bash
git add app/client.py app/models.py tests/test_client.py
git commit -m "feat: add tmp link api client"
```

---

### Task 3: Local FastAPI Routes and Error Translation

**Files:**
- Create: `app/main.py`
- Create: `tests/test_routes.py`

**Interfaces:**
- Produces: `create_app(settings_path: Path | None = None, client_factory=None) -> FastAPI`
- Produces routes from the design: `/health`, settings, quota, uploads, files, and links
- Local JSON envelope: `{"ok": bool, "data": object, "message": str}`

- [ ] **Step 1: Write failing health and settings route tests**

```python
def test_health_and_settings_do_not_expose_key(client):
    assert client.get("/health").json() == {"status": "ok"}
    response = client.put("/api/settings", json={
        "api_key": "route-secret",
        "custom_domain": "pan.cloudcode.xyz",
    })
    assert response.status_code == 200
    assert response.json()["data"]["key_configured"] is True
    assert "route-secret" not in response.text
    assert "route-secret" not in client.get("/api/settings").text
```

- [ ] **Step 2: Write failing proxy route tests**

Inject a fake client and assert quota, list files, list links, create link, delete link, and multipart upload call the expected fake methods. Verify missing keys return HTTP 400 before the fake client is called.

- [ ] **Step 3: Write failing error translation tests**

Assert business and connection errors become HTTP 502, timeout becomes HTTP 504, invalid upload model becomes HTTP 422, and response bodies do not expose the configured key.

- [ ] **Step 4: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_routes.py -q`

Expected: FAIL because `create_app` does not exist.

- [ ] **Step 5: Implement the FastAPI application**

Use Pydantic request models for settings and link creation. Read the key server-side for every remote request. Add a single exception-to-envelope converter. Mount `app/static` only after the API routes, serve `index.html` for `/`, and never include the key in model serialization.

- [ ] **Step 6: Run backend tests and verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_config.py tests/test_client.py tests/test_routes.py -q`

Expected: all backend tests pass.

- [ ] **Step 7: Commit Task 3**

```bash
git add app/main.py tests/test_routes.py
git commit -m "feat: expose local management api"
```

---

### Task 4: Application Shell, Dashboard, and Settings UI

**Files:**
- Create: `app/static/index.html`
- Create: `app/static/app.css`
- Create: `app/static/app.js`
- Create: `app/static/vendor/lucide.min.js`
- Create: `tests/test_static.py`

**Interfaces:**
- Four hash-routed views: `#dashboard`, `#files`, `#links`, `#settings`
- Frontend helper: `api(path, options) -> Promise<data>`
- Frontend never receives a stored API key

- [ ] **Step 1: Write failing static-page tests**

Assert `/` includes one application root, four navigation commands, settings form labels, upload input with `multiple`, and local Lucide script path. Assert no remote CDN URL and no API key-shaped value appears in HTML or JavaScript.

- [ ] **Step 2: Run static tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_static.py -q`

Expected: FAIL because static assets do not exist.

- [ ] **Step 3: Build the shell and navigation**

Create a restrained light interface with a 224px sidebar, compact top bar, full-width content bands, 36px icon buttons, 6px or smaller radii, stable table columns, visible focus states, and responsive navigation below 760px. Use CSS variables with neutral surfaces, green success, amber warning, red destructive, and blue action colors; avoid gradients.

- [ ] **Step 4: Implement dashboard and settings behavior**

On load, fetch public settings. Dashboard refresh requests quota, files page 1, and links page 1 concurrently. Settings submission sends a new key only when the input is non-empty. Add test connection and clear-key confirmation. Render all remote text through `textContent`, never `innerHTML`.

- [ ] **Step 5: Vendor Lucide locally**

Download a pinned browser build into `app/static/vendor/lucide.min.js`, record the version in README, call `lucide.createIcons()` after navigation and dynamic rendering, and keep the app functional if icon initialization fails.

- [ ] **Step 6: Run tests and verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_static.py tests/test_routes.py -q`

Expected: static shell and backend route tests pass.

- [ ] **Step 7: Commit Task 4**

```bash
git add app/static tests/test_static.py
git commit -m "feat: add dashboard and settings interface"
```

---

### Task 5: Batch Upload and Workspace File Management UI

**Files:**
- Modify: `app/static/index.html`
- Modify: `app/static/app.css`
- Modify: `app/static/app.js`
- Create: `tests/test_frontend_contract.py`

**Interfaces:**
- Upload queue item states: `queued`, `uploading`, `processing`, `complete`, `failed`
- Uses local `POST /api/uploads`
- Uses local `GET /api/files?page=N`

- [ ] **Step 1: Write failing frontend contract tests**

Assert static JavaScript contains explicit allowed storage modes `{99, 0, 1, 2}`, uses `FormData`, uploads queue items sequentially, retains failed items, and has no code that stores API keys in `localStorage`, `sessionStorage`, IndexedDB, cookies, or URL parameters.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_frontend_contract.py -q`

Expected: FAIL because upload queue behavior is absent.

- [ ] **Step 3: Implement upload queue**

Add drag-enter/leave/drop and file-input handling. Prevent duplicate queue entries by name, size, and last-modified tuple. Use `XMLHttpRequest` for browser-to-local upload progress, switch to an indeterminate `processing` state at 100%, parse the normalized JSON envelope, and expose retry/remove/create-link commands per item.

- [ ] **Step 4: Implement workspace table**

Normalize service rows using documented keys `ukey`, `name`, and `size`, while rendering `-` for absent values. Add previous/next pagination, refresh, copy UKEY, and open-create-link commands. Keep the existing table visible when refresh fails.

- [ ] **Step 5: Run tests and verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_frontend_contract.py tests/test_static.py -q`

Expected: upload and file-list contract tests pass.

- [ ] **Step 6: Commit Task 5**

```bash
git add app/static tests/test_frontend_contract.py
git commit -m "feat: add batch upload and file management"
```

---

### Task 6: Direct-Link UI, Launchers, Documentation, and End-to-End Verification

**Files:**
- Modify: `app/static/index.html`
- Modify: `app/static/app.css`
- Modify: `app/static/app.js`
- Create: `scripts/start.sh`
- Create: `scripts/wait-and-open.ps1`
- Create: `start.bat`
- Create: `README.md`
- Create: `tests/test_launchers.py`
- Create: `tests/e2e/app.spec.js`
- Create: `playwright.config.js`
- Create: `package.json`

**Interfaces:**
- Uses `GET /api/links`, `POST /api/links`, and `DELETE /api/links/{dkey}`
- Double-click entry point: `start.bat`
- Final URL: `http://127.0.0.1:8765`

- [ ] **Step 1: Write failing direct-link and launcher tests**

Assert frontend contract includes create fields `ukey`, optional `valid_time`, optional `download_limit`, copy/open commands, and delete confirmation with `delete_file`. Assert `start.bat` and `scripts/start.sh` contain the fixed loopback host and port, and no `0.0.0.0` binding.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_launchers.py tests/test_frontend_contract.py -q`

Expected: FAIL because link-management UI and launchers are incomplete.

- [ ] **Step 3: Implement direct-link management**

Render documented fields `dkey`, `link`, `name`, `size`, and `etime`. When the service returns an absolute link, display it unchanged; when it returns a path, join it to `https://<custom_domain>`. Create-link and delete dialogs must disable submit while pending, preserve user input after errors, and refresh both file/link summaries after success.

- [ ] **Step 4: Add launch scripts**

`scripts/start.sh` creates `.venv` when missing, installs the editable project, verifies port 8765 is free, and executes:

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

`start.bat` opens the WSL server in a separate window and invokes `wait-and-open.ps1`. The PowerShell script polls `/health` for up to 60 seconds, opens the browser only after HTTP 200, and prints a useful timeout error otherwise.

- [ ] **Step 5: Write README**

Document double-click startup, manual WSL startup, first-run dependency installation, setting the key in the browser, the plaintext local-secret limitation, API operations, custom domain behavior, troubleshooting for port conflicts, and the fact that the screenshot-exposed key was never embedded.

- [ ] **Step 6: Add Playwright fixtures and tests**

Use `page.route('**/api/**')` to supply deterministic settings, quota, file, upload, and link responses. Test navigation, settings status, batch queue, create-link dialog, link copy affordance, delete confirmation, and no horizontal overflow at `1440x900` and `390x844`.

- [ ] **Step 7: Run all automated verification**

Run: `.venv/bin/python -m pytest -q`

Expected: all Python tests pass.

Run: `npm install`

Run: `npx playwright install chromium`

Run: `npx playwright test`

Expected: desktop and mobile tests pass with no horizontal overflow.

- [ ] **Step 8: Start the real local service and inspect screenshots**

Run: `.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8765`

Verify `curl http://127.0.0.1:8765/health` returns `{"status":"ok"}`. Capture dashboard, files, links, and settings screenshots at both viewports; inspect them for clipping, blank areas, text overlap, and broken icons.

- [ ] **Step 9: Commit Task 6**

```bash
git add app/static scripts start.bat README.md tests package.json playwright.config.js
git commit -m "feat: complete local tmp link manager"
```

