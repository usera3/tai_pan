from __future__ import annotations

from pathlib import Path
import time
from typing import Any, Callable

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.client import (
    ALLOWED_STORAGE_MODELS,
    TmpLinkBusinessError,
    TmpLinkClient,
    TmpLinkConnectionError,
    TmpLinkTimeoutError,
)
from app.config import SettingsStore
from app.download_registry import DownloadLinkRegistry
from app.models import ServiceResult


class SettingsUpdate(BaseModel):
    api_key: str = ""
    custom_domain: str


class LinkCreate(BaseModel):
    ukey: str = Field(min_length=1)
    valid_time: int | None = Field(default=None, ge=1)
    download_limit: int | None = Field(default=None, ge=1)


class LocalApiError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def envelope(data: Any = None, message: str = "", ok: bool = True) -> dict[str, Any]:
    return {"ok": ok, "data": data, "message": message}


def create_app(
    settings_path: Path | None = None,
    client_factory: Callable[[str], TmpLinkClient] | None = None,
) -> FastAPI:
    application = FastAPI(title="TMP Link Manager", docs_url=None, redoc_url=None)
    settings_file = settings_path or Path(".local/settings.json")
    store = SettingsStore(settings_file)
    download_registry = DownloadLinkRegistry(
        Path(settings_file).with_name("download_links.json")
    )
    make_client = client_factory or (lambda api_key: TmpLinkClient(api_key))

    def remote_client():
        api_key = store.load().api_key
        if not api_key:
            raise LocalApiError(400, "API Key is not configured")
        return make_client(api_key)

    @application.exception_handler(LocalApiError)
    async def handle_local_error(request: Request, exc: LocalApiError):
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope(ok=False, message=str(exc)),
        )

    @application.exception_handler(TmpLinkBusinessError)
    @application.exception_handler(TmpLinkConnectionError)
    async def handle_remote_error(request: Request, exc: Exception):
        return JSONResponse(
            status_code=502,
            content=envelope(ok=False, message=str(exc)),
        )

    @application.exception_handler(TmpLinkTimeoutError)
    async def handle_timeout(request: Request, exc: TmpLinkTimeoutError):
        return JSONResponse(
            status_code=504,
            content=envelope(ok=False, message=str(exc)),
        )

    @application.get("/health")
    async def health():
        return {"status": "ok"}

    @application.get("/api/settings")
    async def get_settings():
        return envelope(store.public_settings())

    @application.put("/api/settings")
    async def put_settings(payload: SettingsUpdate):
        try:
            store.update(payload.api_key, payload.custom_domain)
        except ValueError as exc:
            raise LocalApiError(422, str(exc)) from exc
        return envelope(store.public_settings(), "Settings saved")

    @application.delete("/api/settings/key")
    async def clear_settings_key():
        store.clear_key()
        return envelope(store.public_settings(), "API Key cleared")

    @application.post("/api/settings/test")
    async def test_settings():
        result = await remote_client().quota()
        return result_envelope(result, "Connection successful")

    @application.get("/api/quota")
    async def quota():
        return result_envelope(await remote_client().quota())

    @application.get("/api/files")
    async def files(page: int = Query(default=1, ge=1)):
        return result_envelope(await remote_client().list_files(page))

    @application.post("/api/files/{ukey}/download")
    async def download_file(ukey: str):
        cached = download_registry.active_for(ukey)
        if cached:
            return result_envelope(
                ServiceResult(ok=True, data=cached.as_remote_data())
            )

        result = await remote_client().create_download_link(ukey)
        if isinstance(result.data, dict):
            dkey = result.data.get("dkey") or result.data.get("direct_key")
            link = result.data.get("link") or result.data.get("url")
            if dkey and link:
                download_registry.remember(
                    ukey,
                    str(dkey),
                    str(link),
                    expires_at=time.time() + 1440 * 60,
                )
        return result_envelope(result)

    @application.delete("/api/files/{ukey}")
    async def delete_file(ukey: str):
        result = await remote_client().delete_file(ukey)
        download_registry.forget_ukey(ukey)
        return result_envelope(result, "File deleted")

    @application.get("/api/links")
    async def links(page: int = Query(default=1, ge=1)):
        result = await remote_client().list_links(page)
        hidden = download_registry.hidden_dkeys()
        data = result.data
        if isinstance(data, list):
            data = [
                link
                for link in data
                if not isinstance(link, dict) or link.get("dkey") not in hidden
            ]
        return result_envelope(
            ServiceResult(ok=result.ok, data=data, message=result.message)
        )

    @application.post("/api/links")
    async def create_link(payload: LinkCreate):
        result = await remote_client().create_link(
            payload.ukey,
            valid_time=payload.valid_time,
            download_limit=payload.download_limit,
        )
        return result_envelope(result, "Direct link created")

    @application.delete("/api/links/{dkey}")
    async def delete_link(dkey: str, delete_file: bool = Query(default=False)):
        result = await remote_client().delete_link(dkey, delete_file=delete_file)
        return result_envelope(result, "Direct link deleted")

    @application.post("/api/uploads")
    async def upload(file: UploadFile = File(...), model: int = Form(...)):
        if model not in ALLOWED_STORAGE_MODELS:
            raise LocalApiError(
                422, f"storage model must be one of {sorted(ALLOWED_STORAGE_MODELS)}"
            )
        content = await file.read()
        if not content:
            raise LocalApiError(422, "Uploaded file must not be empty")
        result = await remote_client().upload(file.filename or "upload.bin", content, model)
        return result_envelope(result, "File uploaded")

    static_dir = Path(__file__).parent / "static"

    @application.get("/", include_in_schema=False)
    async def index():
        return FileResponse(static_dir / "index.html")

    @application.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(status_code=204)

    application.mount("/static", StaticFiles(directory=static_dir), name="static")

    return application


def result_envelope(result: ServiceResult, default_message: str = "") -> dict[str, Any]:
    return envelope(
        data=result.data,
        message=result.message or default_message,
        ok=result.ok,
    )


app = create_app()
