from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.client import TmpLinkClient
from app.cloud.config import CloudConfig
from app.cloud.db import Database
from app.cloud.dependencies import active_user, current_user, verify_csrf
from app.cloud.repository import CloudRepository
from app.cloud.routes import (
    admin_router,
    auth_router,
    links_router,
    settings_router,
    tmp_files_router,
)
from app.cloud.routes.auth import REGISTRATION_LIMIT, REGISTRATION_WINDOW
from app.cloud.security import KeyCipher, PasswordService, TokenService
from app.cloud.storage import CloudStorage


UPLOAD_MULTIPART_OVERHEAD_BYTES = 64 * 1024


class UploadGuardMiddleware:
    """Authorize and bound upload requests before multipart parsing begins."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != "/api/uploads"
        ):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        try:
            user = current_user(request, request.app.state.repository)
            active_user(user)
            verify_csrf(request, user)
        except HTTPException as exc:
            await JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=exc.headers,
            )(scope, receive, send)
            return

        body_limit = (
            request.app.state.config.max_file_bytes
            + UPLOAD_MULTIPART_OVERHEAD_BYTES
        )
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                declared_length = body_limit + 1
            if declared_length < 0 or declared_length > body_limit:
                await self._reject_too_large(scope, receive, send)
                return

        received = 0
        exceeded = False
        pending_response: list[Message] = []

        async def bounded_receive() -> Message:
            nonlocal exceeded, received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > body_limit:
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def buffered_send(message: Message) -> None:
            pending_response.append(message)

        await self.app(scope, bounded_receive, buffered_send)

        if exceeded:
            await self._reject_too_large(scope, receive, send)
            return
        for message in pending_response:
            await send(message)

    @staticmethod
    async def _reject_too_large(scope: Scope, receive: Receive, send: Send) -> None:
        await JSONResponse(
            status_code=413,
            content={"detail": "Upload is too large"},
        )(scope, receive, send)


class RegistrationClaimMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == "/api/auth/register"
        ):
            request = Request(scope)
            now = datetime.now(timezone.utc)
            remote_addr = (
                request.client.host if request.client is not None else "unknown"
            )
            try:
                claimed = request.app.state.repository.claim_registration_submission(
                    remote_addr=remote_addr,
                    since=now - REGISTRATION_WINDOW,
                    limit=REGISTRATION_LIMIT,
                    now=now,
                )
            except Exception:
                await JSONResponse(
                    status_code=503,
                    content={"detail": "Registration is temporarily unavailable"},
                )(scope, receive, send)
                return
            if not claimed:
                await JSONResponse(
                    status_code=429,
                    content={"detail": "Too many registration attempts"},
                )(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _validate_cloud_config(config: CloudConfig) -> None:
    required = {
        "SESSION_SECRET": config.session_secret,
        "KEY_ENCRYPTION_KEY": config.key_encryption_key,
        "DATABASE_PATH": config.database_path,
        "STORAGE_PATH": config.storage_path,
        "PUBLIC_ORIGIN": config.public_origin,
    }
    missing = [name for name, value in required.items() if value is None or value == ""]
    if config.mode != "cloud":
        raise ValueError("cloud application requires APP_MODE=cloud")
    if missing:
        raise ValueError(f"cloud application requires {', '.join(missing)}")


def create_cloud_app(config: CloudConfig, database: Database) -> FastAPI:
    _validate_cloud_config(config)
    assert config.key_encryption_key is not None
    assert config.storage_path is not None

    database.initialize()
    config.storage_path.mkdir(parents=True, exist_ok=True)
    password_service = PasswordService()
    token_service = TokenService()
    repository = CloudRepository(database, KeyCipher(config.key_encryption_key))

    application = FastAPI(
        title="TMP Link Manager Cloud", docs_url=None, redoc_url=None
    )
    application.state.mode = "cloud"
    application.state.config = config
    application.state.database = database
    application.state.repository = repository
    application.state.cloud_storage = CloudStorage(
        config.storage_path,
        repository,
        max_file_bytes=config.max_file_bytes,
        user_quota_bytes=config.user_quota_bytes,
        global_quota_bytes=config.global_quota_bytes,
        min_free_bytes=config.min_free_bytes,
    )
    application.state.tmp_client_factory = TmpLinkClient
    application.state.password_service = password_service
    application.state.token_service = token_service
    application.state.dummy_password_hash = password_service.hash(
        token_service.generate_session_token()
    )

    application.add_middleware(RegistrationClaimMiddleware)
    application.add_middleware(UploadGuardMiddleware)

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": "Request validation failed"},
        )

    @application.get("/health")
    async def health():
        return {"status": "ok"}

    application.include_router(auth_router)
    application.include_router(admin_router)
    application.include_router(settings_router)
    application.include_router(tmp_files_router)
    application.include_router(links_router)

    static_dir = Path(__file__).parents[1] / "static"

    @application.get("/", include_in_schema=False)
    async def cloud_index():
        return FileResponse(static_dir / "cloud.html")

    @application.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(status_code=204)

    application.mount("/static", StaticFiles(directory=static_dir), name="static")
    return application
