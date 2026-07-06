"""Request and response models."""

from typing import Literal

from pydantic import BaseModel, Field


class ZonePutRequest(BaseModel):
    zonefile: str = Field(min_length=1, description="Complete BIND-format zonefile")


class ZoneStatus(BaseModel):
    name: str = Field(description="Normalized zone name without trailing dot")
    serial: str | None = Field(
        default=None, description="Live SOA serial as reported by knotd; null while loading"
    )
    knot: dict[str, str] = Field(
        default_factory=dict, description="Raw zone-status map from knotd"
    )


class ZoneUpsertResponse(ZoneStatus):
    created: bool = Field(description="True if the zone was created, false if updated")


class ZoneListResponse(BaseModel):
    zones: list[str]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    knotd: bool
