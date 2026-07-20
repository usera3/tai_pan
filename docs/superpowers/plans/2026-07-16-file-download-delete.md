# File Download and Delete Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add reliable file download and irreversible source-file deletion to the file list using only TMP.link's documented API Key endpoints.

**Architecture:** Extend `TmpLinkClient` with a 24-hour download-link helper and a composed delete operation that creates a direct link to obtain a DKEY before deleting the link and source file. Expose two local FastAPI routes, then add icon actions and an explicit confirmation dialog to the static SPA.

**Download registry addendum:** Persist automatically generated download links in `.local/download_links.json`. Reuse an entry with at least one hour remaining, filter all registered DKEY values from the direct-link list, and never register user-created links.

**Tech Stack:** Python 3.10, FastAPI, HTTPX, vanilla JavaScript, Lucide, pytest, Playwright.

## Global Constraints

- Download links use `valid_time=1440` and omit `download_limit`.
- Deletion invalidates the source file and all related direct links and requires explicit confirmation.
- Use documented API Key actions only; do not use login tokens or internal folder APIs.
- Never expose the API Key to the browser, logs, tests, commits, or exceptions.
- Preserve localhost-only binding on `127.0.0.1:8765`.

---

### Task 1: TMP.link Client Operations

**Files:**
- Modify: `app/client.py`
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: `create_link(ukey, valid_time, download_limit)` and `delete_link(dkey, delete_file)`.
- Produces: `create_download_link(ukey: str) -> ServiceResult` and `delete_file(ukey: str) -> ServiceResult`.

- [ ] **Step 1: Write failing client tests**

Test that `create_download_link("FILE-UKEY")` sends `link_add`, `valid_time=1440`, and no `download_limit`. Test that `delete_file("FILE-UKEY")` sends `link_add` followed by `link_del` with `delete=1`. Cover DKEY returned as `{"dkey": "D1"}`, as `{"direct_key": "D1"}`, and as `"D1"`, plus missing-DKEY failure.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_client.py`

Expected: failure because the two methods do not exist.

- [ ] **Step 3: Implement minimal client methods**

```python
async def create_download_link(self, ukey: str) -> ServiceResult:
    return await self.create_link(ukey, valid_time=1440)

async def delete_file(self, ukey: str) -> ServiceResult:
    created = await self.create_link(ukey)
    dkey = self._extract_dkey(created.data)
    if not dkey:
        raise TmpLinkBusinessError("钛盘未返回删除文件所需的 DKEY")
    return await self.delete_link(dkey, delete_file=True)
```

Keep `_extract_dkey` private and accept a non-empty string or object keys `dkey` and `direct_key`.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `.venv/bin/python -m pytest -q tests/test_client.py`

Expected: all client tests pass.

- [ ] **Step 5: Commit**

Run: `git add app/client.py tests/test_client.py && git commit -m "feat: add file download and deletion operations"`

### Task 2: Local File Routes

**Files:**
- Modify: `app/main.py`
- Test: `tests/test_routes.py`

**Interfaces:**
- Consumes: the two client methods from Task 1.
- Produces: `POST /api/files/{ukey}/download` and `DELETE /api/files/{ukey}`.

- [ ] **Step 1: Write failing route tests**

Extend the fake remote with recorded `create_download_link` and `delete_file` methods. Assert encoded UKEY values reach the correct method, responses use the standard envelope, and business errors remain redacted `502` responses.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_routes.py`

Expected: new route tests return 404 or 405.

- [ ] **Step 3: Add routes**

```python
@application.post("/api/files/{ukey}/download")
async def download_file(ukey: str):
    return result_envelope(await remote_client().create_download_link(ukey))

@application.delete("/api/files/{ukey}")
async def delete_file(ukey: str):
    return result_envelope(await remote_client().delete_file(ukey), "File deleted")
```

- [ ] **Step 4: Run tests and verify GREEN**

Run: `.venv/bin/python -m pytest -q tests/test_routes.py`

Expected: all route tests pass.

- [ ] **Step 5: Commit**

Run: `git add app/main.py tests/test_routes.py && git commit -m "feat: expose file download and delete routes"`

### Task 3: File Actions and Browser Workflow

**Files:**
- Modify: `app/static/index.html`
- Modify: `app/static/app.js`
- Modify: `app/static/app.css`
- Modify: `tests/test_frontend_contract.py`
- Modify: `tests/e2e/app.spec.js`

**Interfaces:**
- Consumes: the two routes from Task 2 plus existing `linkUrl`, `loadFiles`, `loadLinks`, and `refreshDashboard`.
- Produces: background iframe download action, delete icon action, and `#file-delete-dialog` confirmation workflow.

- [ ] **Step 1: Write failing frontend and Playwright tests**

Assert source contains both encoded file routes, the delete dialog, filename warning, and hidden iframe handling. In Playwright, return an attachment from the direct URL, verify a browser download event while the management page URL remains unchanged; open delete for a named file, verify the warning, submit, and assert the DELETE route was called.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_frontend_contract.py && npx playwright test`

Expected: failures because controls and dialog do not exist.

- [ ] **Step 3: Implement UI**

Add pending-file state, append `download` and danger `trash-2` icon buttons in `renderFiles`, and implement background download:

```javascript
async function downloadFile(file) {
  try {
    const result = await api(`/api/files/${encodeURIComponent(file.ukey)}/download`, { method: "POST" });
    const url = linkUrl(result && (result.link || result.url || result));
    if (!url) throw new Error("钛盘未返回下载链接");
    const frame = document.createElement("iframe");
    frame.className = "download-frame";
    frame.hidden = true;
    frame.src = url;
    document.body.append(frame);
  } catch (error) {
    toast(error.message);
  }
}
```

The delete handler calls the encoded DELETE route, closes only on success, then refreshes files, links, and dashboard. Render the filename with `textContent`; do not add browser storage.

- [ ] **Step 4: Run targeted tests and verify GREEN**

Run: `.venv/bin/python -m pytest -q tests/test_frontend_contract.py && npx playwright test`

Expected: all targeted tests pass.

- [ ] **Step 5: Run complete verification**

Run: `.venv/bin/python -m pytest -q`

Run: `npx playwright test`

Run: `git diff --check`

Expected: all tests pass and diff check is empty.

- [ ] **Step 6: Commit**

Run: `git add app/static tests/test_frontend_contract.py tests/e2e/app.spec.js && git commit -m "feat: add file download and delete controls"`

- [ ] **Step 7: Restart and verify**

Restart Uvicorn on `127.0.0.1:8765`, verify `/health`, and verify a real download-link request returns a URL without printing the API Key. Leave destructive deletion for explicit browser action.
