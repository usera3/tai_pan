from app.cloud.routes.admin import router as admin_router
from app.cloud.routes.auth import router as auth_router
from app.cloud.routes.links import router as links_router
from app.cloud.routes.settings import router as settings_router
from app.cloud.routes.tmp_files import router as tmp_files_router


__all__ = [
    "admin_router",
    "auth_router",
    "links_router",
    "settings_router",
    "tmp_files_router",
]
