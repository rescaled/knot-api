"""HTTP endpoints. Handlers are sync on purpose: libknot is blocking ctypes,
so FastAPI runs them in its threadpool."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse

from .auth import require_token
from .dependencies import get_zone_service
from .models import (
    HealthResponse,
    ZoneListResponse,
    ZonePutRequest,
    ZoneStatus,
    ZoneUpsertResponse,
)
from .service import ZoneService

router = APIRouter(prefix="/v1")

ServiceDep = Annotated[ZoneService, Depends(get_zone_service)]


@router.get("/healthz", response_model=HealthResponse, tags=["health"])
def healthz(service: ServiceDep) -> JSONResponse:
    health = service.health()
    return JSONResponse(status_code=200 if health.knotd else 503, content=health.model_dump())


zones = APIRouter(prefix="/zones", dependencies=[Depends(require_token)], tags=["zones"])


@zones.put(
    "/{zone_name}",
    response_model=ZoneUpsertResponse,
    responses={200: {"description": "Zone updated"}, 201: {"description": "Zone created"}},
)
def upsert_zone(
    zone_name: str, body: ZonePutRequest, response: Response, service: ServiceDep
) -> ZoneUpsertResponse:
    created, zone_status = service.upsert_zone(zone_name, body.zonefile)
    response.status_code = 201 if created else 200
    return ZoneUpsertResponse(created=created, **zone_status.model_dump())


@zones.delete("/{zone_name}", status_code=204)
def delete_zone(zone_name: str, service: ServiceDep) -> None:
    service.delete_zone(zone_name)


@zones.get("", response_model=ZoneListResponse)
def list_zones(service: ServiceDep) -> ZoneListResponse:
    return ZoneListResponse(zones=service.list_zones())


@zones.get("/{zone_name}", response_model=ZoneStatus)
def get_zone(zone_name: str, service: ServiceDep) -> ZoneStatus:
    return service.get_zone(zone_name)


router.include_router(zones)
