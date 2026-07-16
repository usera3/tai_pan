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
