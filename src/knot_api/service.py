"""Zone management orchestration.

Locking model: a per-zone lock serializes requests for the same zone
(validate → write → configure must not interleave); the config-transaction
lock lives in the knot client. All file I/O and validation happen outside
the transaction window. Locks are in-process — the API must run as a
single process (uvicorn ``--workers 1``).
"""

import logging
import threading
import time

from .config import Settings
from .errors import KnotApiError, KnotOperationError, ZoneNotFound
from .knot import KnotControl
from .models import HealthResponse, ZoneStatus
from .naming import assert_not_protected, normalize_zone_name, to_fqdn
from .zonefile import ZonefileStore

logger = logging.getLogger(__name__)

_UNLOADED_SERIALS = frozenset({"", "-"})


class ZoneService:
    # After a create commit, knotd loads the zone asynchronously; poll briefly
    # so the response can report the initial serial. Class attributes so tests
    # can shrink the budget.
    _LOAD_POLL_BUDGET = 5.0
    _LOAD_POLL_INTERVAL = 0.2

    def __init__(self, settings: Settings, knot: KnotControl, store: ZonefileStore) -> None:
        self._settings = settings
        self._knot = knot
        self._store = store
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        protected = set(settings.protected_zones)
        if settings.catalog_zone:
            protected.add(settings.catalog_zone)
        # Normalize at startup so a misconfigured protected list fails fast.
        self._protected = frozenset(normalize_zone_name(zone) for zone in protected)

    def upsert_zone(self, raw_name: str, zonefile: str) -> tuple[bool, ZoneStatus]:
        name = self._checked_name(raw_name)
        fqdn = to_fqdn(name)
        with self._zone_lock(name):
            staged = self._store.stage(name, zonefile)
            try:
                self._store.validate(name, staged)
                created = not self._knot.zone_exists(fqdn)
                self._store.install(staged, name)
            except BaseException:
                self._store.discard(staged)
                raise

            if created:
                try:
                    self._knot.add_zone(fqdn, self._settings.zone_template)
                except BaseException:
                    self._store.remove(name)  # keep filesystem consistent with confdb
                    raise
                status = self._await_loaded(fqdn)
                logger.info("created zone %s (template %s)", name, self._settings.zone_template)
            else:
                self._knot.zone_reload(fqdn)
                status = self._zone_status_tolerant(fqdn)
                logger.info("updated zone %s", name)
            return created, self._to_status(name, status)

    def delete_zone(self, raw_name: str) -> None:
        name = self._checked_name(raw_name)
        fqdn = to_fqdn(name)
        with self._zone_lock(name):
            if not self._knot.zone_exists(fqdn):
                raise ZoneNotFound(f"zone {name} is not configured")
            self._knot.remove_zone(fqdn)
            try:
                self._knot.zone_purge_orphan(fqdn)
            except KnotApiError as exc:
                raise KnotOperationError(
                    f"zone {name} was removed from the configuration but purging its data "
                    f"failed: {exc}. Run `knotc zone-purge -f {fqdn} +orphan` manually."
                ) from exc
            finally:
                # zone-purge +orphan cannot delete the zonefile (knotd no longer
                # knows its path) — the file is ours to remove.
                self._store.remove(name)
            logger.info("deleted zone %s", name)

    def get_zone(self, raw_name: str) -> ZoneStatus:
        name = normalize_zone_name(raw_name)  # reads of protected zones are fine
        status = self._knot.zone_status(to_fqdn(name))
        return self._to_status(name, status)

    def list_zones(self) -> list[str]:
        return self._knot.list_zones()

    def health(self) -> HealthResponse:
        try:
            self._knot.status()
        except KnotApiError:
            return HealthResponse(status="degraded", knotd=False)
        return HealthResponse(status="ok", knotd=True)

    def _checked_name(self, raw_name: str) -> str:
        name = normalize_zone_name(raw_name)
        assert_not_protected(name, self._protected)
        return name

    def _zone_lock(self, name: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(name, threading.Lock())

    def _await_loaded(self, fqdn: str) -> dict[str, str]:
        deadline = time.monotonic() + self._LOAD_POLL_BUDGET
        while True:
            status = self._zone_status_tolerant(fqdn)
            if _serial_of(status) is not None or time.monotonic() >= deadline:
                return status
            time.sleep(self._LOAD_POLL_INTERVAL)

    def _zone_status_tolerant(self, fqdn: str) -> dict[str, str]:
        """Status is response garnish after a successful mutation — never fail on it."""
        try:
            return self._knot.zone_status(fqdn)
        except KnotApiError as exc:
            logger.warning("zone-status for %s failed after mutation: %s", fqdn, exc)
            return {}

    def _to_status(self, name: str, status: dict[str, str]) -> ZoneStatus:
        return ZoneStatus(name=name, serial=_serial_of(status), knot=status)


def _serial_of(status: dict[str, str]) -> str | None:
    serial = status.get("serial")
    if serial is None or serial in _UNLOADED_SERIALS:
        return None
    return serial
