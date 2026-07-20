from pathlib import Path


def frontend_source() -> str:
    return Path("app/static/app.js").read_text(encoding="utf-8")


def test_upload_queue_contract_is_explicit():
    source = frontend_source()

    assert "STORAGE_MODES" in source
    for model in (99, 0, 1, 2):
        assert str(model) in source
    for status in ("queued", "uploading", "processing", "complete", "failed"):
        assert f'"{status}"' in source
    assert "new FormData" in source
    assert "new XMLHttpRequest" in source
    assert "await uploadQueueItem" in source
    assert "retryUpload" in source


def test_file_management_contract_includes_pagination_and_actions():
    source = frontend_source()

    assert 'api(`/api/files?page=${state.filesPage}`)' in source
    assert "files-prev" in source
    assert "files-next" in source
    assert "copyText" in source
    assert "openLinkDialog" in source


def test_frontend_never_persists_api_key():
    source = frontend_source()

    for forbidden in (
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "document.cookie",
        "URLSearchParams",
    ):
        assert forbidden not in source


def test_direct_link_contract_includes_create_copy_and_delete():
    source = frontend_source()
    html = Path("app/static/index.html").read_text(encoding="utf-8")

    assert 'api(`/api/links?page=${state.linksPage}`)' in source
    assert 'api("/api/links", {' in source
    assert "delete_file" in source
    assert "copyText" in source
    for element_id in (
        "link-dialog",
        "link-form",
        "link-ukey",
        "link-valid-time",
        "link-download-limit",
        "delete-dialog",
        "delete-file",
    ):
        assert f'id="{element_id}"' in html


def test_file_actions_include_download_and_confirmed_source_deletion():
    source = frontend_source()
    html = Path("app/static/index.html").read_text(encoding="utf-8")

    assert 'api(`/api/files/${encodeURIComponent(file.ukey)}/download`' in source
    assert 'api(`/api/files/${encodeURIComponent(state.pendingFile.ukey)}`' in source
    assert 'frame.className = "download-frame"' in source
    assert 'window.open("about:blank", "_blank")' not in source
    assert 'id="file-delete-dialog"' in html
    assert 'id="file-delete-name"' in html
    assert 'id="file-delete-form"' in html
    assert "全部相关直链" in html
