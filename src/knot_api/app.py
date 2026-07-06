"""Application factory.

Run with: ``uvicorn --factory knot_api.app:create_app --workers 1``
(single process is required — zone/transaction locks are in-process).
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__, api
from .config import Settings, get_settings
from .errors import (
    KnotApiError,
    KnotOperationError,
    KnotTxnBusy,
    KnotUnavailable,
    ZonefileInvalid,
    ZonefileTooLarge,
    ZoneNameInvalid,
    ZoneNotFound,
    ZoneProtected,
)
from .knot import KnotControl, LibknotClient
from .service import ZoneService
from .zonefile import ZonefileStore

logger = logging.getLogger(__name__)

_ERROR_STATUS: list[tuple[type[KnotApiError], int]] = [
    (ZoneNameInvalid, 422),
    (ZonefileInvalid, 422),
    (ZonefileTooLarge, 413),
    (ZoneProtected, 403),
    (ZoneNotFound, 404),
    (KnotTxnBusy, 503),
    (KnotUnavailable, 503),
    (KnotOperationError, 500),
]


def _error_response(exc: KnotApiError) -> JSONResponse:
    for exc_type, status_code in _ERROR_STATUS:
        if isinstance(exc, exc_type):
            headers = {"Retry-After": "30"} if isinstance(exc, KnotTxnBusy) else None
            return JSONResponse(
                status_code=status_code, content={"detail": str(exc)}, headers=headers
            )
    return JSONResponse(status_code=500, content={"detail": str(exc)})


def create_app(
    settings: Settings | None = None,
    knot: KnotControl | None = None,
    store: ZonefileStore | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    knot = knot or LibknotClient(
        socket_path=settings.knot_socket,
        timeout=settings.knot_timeout,
        reload_timeout=settings.reload_timeout,
        txn_retries=settings.txn_retries,
        txn_retry_base_delay=settings.txn_retry_base_delay,
        libknot_so=settings.libknot_so,
    )
    store = store or ZonefileStore(
        zones_dir=settings.zones_dir,
        kzonecheck_bin=settings.kzonecheck_bin,
        kzonecheck_timeout=settings.kzonecheck_timeout,
        max_bytes=settings.max_zonefile_bytes,
    )
    service = ZoneService(settings, knot, store)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if settings.abort_stale_txn_on_startup:
            try:
                knot.abort_stale_txn()
            except KnotApiError as exc:
                logger.warning("stale-transaction abort at startup failed: %s", exc)
        yield

    app = FastAPI(
        title="knot-api",
        version=__version__,
        summary="Zone management for a local Knot DNS primary",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.zone_service = service
    app.include_router(api.router)

    @app.exception_handler(KnotApiError)
    async def knot_api_error_handler(_: Request, exc: KnotApiError) -> JSONResponse:
        return _error_response(exc)

    return app
