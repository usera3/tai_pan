from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.cloud.config import CloudConfig
from app.cloud.db import Database
from app.cloud.repository import CloudRepository
from app.cloud.routes import auth_router
from app.cloud.security import KeyCipher, PasswordService, TokenService


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
    application.state.password_service = password_service
    application.state.token_service = token_service
    application.state.dummy_password_hash = password_service.hash(
        token_service.generate_session_token()
    )

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
    return application
