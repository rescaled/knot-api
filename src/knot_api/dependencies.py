"""FastAPI dependency accessors for objects wired up in the app factory."""

from fastapi import Request

from .config import Settings
from .service import ZoneService


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_zone_service(request: Request) -> ZoneService:
    service: ZoneService = request.app.state.zone_service
    return service
