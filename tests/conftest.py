"""Shared fixtures: FakeKnot control double, fake kzonecheck, app/client factories."""

from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from knot_api.app import create_app
from knot_api.config import Settings
from knot_api.errors import KnotOperationError, KnotUnavailable, ZoneNotFound
from knot_api.service import ZoneService
from knot_api.zonefile import ZonefileStore

TEST_TOKEN = "test-token"
CATALOG_ZONE = "catalog.example"

SAMPLE_ZONEFILE = """\
example.com. 3600 SOA ns1.example.com. hostmaster.example.com. 1 86400 900 691200 3600
example.com. 3600 NS ns1.example.com.
ns1.example.com. 3600 A 192.0.2.1
"""

UPDATED_ZONEFILE = SAMPLE_ZONEFILE + "www.example.com. 3600 A 192.0.2.2\n"


class FakeKnot:
    """In-memory stand-in for LibknotClient (implements the KnotControl protocol).

    ``fail_on[op]`` raises the given exception once on the next call of ``op``;
    ``down=True`` makes every call raise KnotUnavailable;
    ``stay_loading=True`` makes freshly added zones report serial "-".
    """

    INITIAL_SERIAL = 1_000_000

    def __init__(self) -> None:
        self.zones: dict[str, str] = {}  # fqdn -> template
        self.serials: dict[str, int] = {}
        self.loading: set[str] = set()
        self.calls: list[tuple[str, ...]] = []
        self.fail_on: dict[str, Exception] = {}
        self.down = False
        self.stay_loading = False
        self.stale_txn_aborted = False

    def _op(self, op: str, *args: str) -> None:
        self.calls.append((op, *args))
        if self.down:
            raise KnotUnavailable("cannot connect to knotd control socket (fake)")
        exc = self.fail_on.pop(op, None)
        if exc is not None:
            raise exc

    def ops(self) -> list[str]:
        return [call[0] for call in self.calls]

    def status(self) -> None:
        self._op("status")

    def zone_exists(self, zone: str) -> bool:
        self._op("zone_exists", zone)
        return zone in self.zones

    def list_zones(self) -> list[str]:
        self._op("list_zones")
        return sorted(zone.rstrip(".") for zone in self.zones)

    def zone_status(self, zone: str) -> dict[str, str]:
        self._op("zone_status", zone)
        if zone not in self.zones:
            raise ZoneNotFound("knotd: no such zone found")
        serial = "-" if zone in self.loading else str(self.serials[zone])
        return {"role": "master", "serial": serial, "transaction": "-"}

    def zone_reload(self, zone: str) -> None:
        self._op("zone_reload", zone)
        if zone not in self.zones:
            raise ZoneNotFound("knotd: no such zone found")
        self.serials[zone] += 1

    def zone_purge_orphan(self, zone: str) -> None:
        self._op("zone_purge_orphan", zone)

    def add_zone(self, zone: str, template: str) -> None:
        self._op("add_zone", zone, template)
        self.zones[zone] = template
        self.serials[zone] = self.INITIAL_SERIAL
        if self.stay_loading:
            self.loading.add(zone)

    def remove_zone(self, zone: str) -> None:
        self._op("remove_zone", zone)
        if zone not in self.zones:
            raise KnotOperationError("knotd: invalid identifier")
        del self.zones[zone]
        self.serials.pop(zone, None)
        self.loading.discard(zone)

    def abort_stale_txn(self) -> None:
        self._op("abort_stale_txn")
        self.stale_txn_aborted = True


def make_settings(zones_dir: Path, kzonecheck_bin: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "token": TEST_TOKEN,
        "zones_dir": zones_dir,
        "knot_socket": Path("/nonexistent/knot.sock"),
        "kzonecheck_bin": str(kzonecheck_bin),
        "catalog_zone": CATALOG_ZONE,
        "txn_retry_base_delay": 0.01,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def make_store(settings: Settings) -> ZonefileStore:
    return ZonefileStore(
        zones_dir=settings.zones_dir,
        kzonecheck_bin=settings.kzonecheck_bin,
        kzonecheck_timeout=settings.kzonecheck_timeout,
        max_bytes=settings.max_zonefile_bytes,
    )


@pytest.fixture
def zones_dir(tmp_path: Path) -> Path:
    path = tmp_path / "zones"
    path.mkdir()
    return path


@pytest.fixture
def fake_kzonecheck(tmp_path: Path) -> Path:
    """Executable stand-in: fails when the zonefile contains 'INVALID'."""
    script = tmp_path / "fake-kzonecheck"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "text = open(sys.argv[-1], encoding='utf-8').read()\n"
        "if 'INVALID' in text:\n"
        "    print('fake kzonecheck: record parse error')\n"
        "    sys.exit(1)\n"
    )
    script.chmod(0o755)
    return script


@pytest.fixture
def settings(zones_dir: Path, fake_kzonecheck: Path) -> Settings:
    return make_settings(zones_dir, fake_kzonecheck)


@pytest.fixture
def fake_knot() -> FakeKnot:
    return FakeKnot()


@pytest.fixture
def store(settings: Settings) -> ZonefileStore:
    return make_store(settings)


@pytest.fixture
def service(settings: Settings, fake_knot: FakeKnot, store: ZonefileStore) -> ZoneService:
    return ZoneService(settings, fake_knot, store)


@pytest.fixture
def app(settings: Settings, fake_knot: FakeKnot, store: ZonefileStore) -> FastAPI:
    return create_app(settings=settings, knot=fake_knot, store=store)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


MakeClient = Callable[..., tuple[TestClient, FakeKnot]]


@pytest.fixture
def make_client(zones_dir: Path, fake_kzonecheck: Path) -> Iterator[MakeClient]:
    """Factory for clients with settings overrides (e.g. size limits)."""
    stack = ExitStack()

    def _make(**overrides: object) -> tuple[TestClient, FakeKnot]:
        custom_settings = make_settings(zones_dir, fake_kzonecheck, **overrides)
        fake = FakeKnot()
        application = create_app(
            settings=custom_settings, knot=fake, store=make_store(custom_settings)
        )
        return stack.enter_context(TestClient(application)), fake

    yield _make
    stack.close()
