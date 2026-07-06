from pathlib import Path

import pytest

from conftest import CATALOG_ZONE, SAMPLE_ZONEFILE, UPDATED_ZONEFILE, FakeKnot
from knot_api.errors import (
    KnotOperationError,
    KnotTxnBusy,
    ZonefileInvalid,
    ZoneNotFound,
    ZoneProtected,
)
from knot_api.service import ZoneService

FQDN = "example.com."


def seed_zone(fake_knot: FakeKnot, zones_dir: Path, serial: int = 41) -> None:
    """Zone already configured on knot with its file on disk."""
    fake_knot.zones[FQDN] = "member"
    fake_knot.serials[FQDN] = serial
    (zones_dir / "example.com.zone").write_text(SAMPLE_ZONEFILE)


def test_create_zone(service: ZoneService, fake_knot: FakeKnot, zones_dir: Path) -> None:
    created, status = service.upsert_zone("example.com", SAMPLE_ZONEFILE)
    assert created is True
    assert status.name == "example.com"
    assert status.serial == str(FakeKnot.INITIAL_SERIAL)
    assert fake_knot.zones[FQDN] == "member"
    assert (zones_dir / "example.com.zone").read_text() == SAMPLE_ZONEFILE
    assert fake_knot.ops() == ["zone_exists", "add_zone", "zone_status"]


def test_create_rolls_back_file_when_config_fails(
    service: ZoneService, fake_knot: FakeKnot, zones_dir: Path
) -> None:
    fake_knot.fail_on["add_zone"] = KnotTxnBusy("busy")
    with pytest.raises(KnotTxnBusy):
        service.upsert_zone("example.com", SAMPLE_ZONEFILE)
    assert FQDN not in fake_knot.zones
    assert list(zones_dir.iterdir()) == []  # installed file rolled back, no tmp leftovers


def test_update_zone_reloads_without_txn(
    service: ZoneService, fake_knot: FakeKnot, zones_dir: Path
) -> None:
    seed_zone(fake_knot, zones_dir)
    created, status = service.upsert_zone("example.com", UPDATED_ZONEFILE)
    assert created is False
    assert status.serial == "42"  # bumped by knot on reload
    assert (zones_dir / "example.com.zone").read_text() == UPDATED_ZONEFILE
    assert "add_zone" not in fake_knot.ops()
    assert fake_knot.ops() == ["zone_exists", "zone_reload", "zone_status"]


def test_update_keeps_new_file_when_reload_fails(
    service: ZoneService, fake_knot: FakeKnot, zones_dir: Path
) -> None:
    seed_zone(fake_knot, zones_dir)
    fake_knot.fail_on["zone_reload"] = KnotOperationError("knotd: invalid zone file")
    with pytest.raises(KnotOperationError):
        service.upsert_zone("example.com", UPDATED_ZONEFILE)
    # knot keeps serving old contents; the new file stays for a retry
    assert (zones_dir / "example.com.zone").read_text() == UPDATED_ZONEFILE


def test_update_recreates_vanished_zonefile(
    service: ZoneService, fake_knot: FakeKnot, zones_dir: Path
) -> None:
    fake_knot.zones[FQDN] = "member"
    fake_knot.serials[FQDN] = 41
    created, _ = service.upsert_zone("example.com", SAMPLE_ZONEFILE)
    assert created is False
    assert (zones_dir / "example.com.zone").exists()


def test_invalid_zonefile_leaves_no_trace(
    service: ZoneService, fake_knot: FakeKnot, zones_dir: Path
) -> None:
    with pytest.raises(ZonefileInvalid):
        service.upsert_zone("example.com", "INVALID GARBAGE")
    assert list(zones_dir.iterdir()) == []
    assert fake_knot.ops() == []  # rejected before any knot call


def test_create_reports_null_serial_while_loading(
    service: ZoneService, fake_knot: FakeKnot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ZoneService, "_LOAD_POLL_BUDGET", 0.05)
    monkeypatch.setattr(ZoneService, "_LOAD_POLL_INTERVAL", 0.01)
    fake_knot.stay_loading = True
    created, status = service.upsert_zone("example.com", SAMPLE_ZONEFILE)
    assert created is True
    assert status.serial is None


def test_delete_zone(service: ZoneService, fake_knot: FakeKnot, zones_dir: Path) -> None:
    seed_zone(fake_knot, zones_dir)
    service.delete_zone("example.com")
    assert FQDN not in fake_knot.zones
    assert not (zones_dir / "example.com.zone").exists()
    assert fake_knot.ops() == ["zone_exists", "remove_zone", "zone_purge_orphan"]


def test_delete_missing_zone(service: ZoneService, fake_knot: FakeKnot) -> None:
    with pytest.raises(ZoneNotFound):
        service.delete_zone("example.com")
    assert "remove_zone" not in fake_knot.ops()


def test_delete_purge_failure_still_removes_file(
    service: ZoneService, fake_knot: FakeKnot, zones_dir: Path
) -> None:
    seed_zone(fake_knot, zones_dir)
    fake_knot.fail_on["zone_purge_orphan"] = KnotOperationError("knotd: resource busy")
    with pytest.raises(KnotOperationError, match="manually"):
        service.delete_zone("example.com")
    assert FQDN not in fake_knot.zones  # conf-unset already happened
    assert not (zones_dir / "example.com.zone").exists()


@pytest.mark.parametrize("operation", ["upsert", "delete"])
def test_protected_zones_are_untouchable(
    service: ZoneService, fake_knot: FakeKnot, operation: str
) -> None:
    with pytest.raises(ZoneProtected):
        if operation == "upsert":
            service.upsert_zone(CATALOG_ZONE, SAMPLE_ZONEFILE)
        else:
            service.delete_zone(CATALOG_ZONE)
    assert fake_knot.ops() == []


def test_get_zone_and_list(service: ZoneService, fake_knot: FakeKnot, zones_dir: Path) -> None:
    seed_zone(fake_knot, zones_dir)
    status = service.get_zone("example.com.")
    assert status.name == "example.com"
    assert status.serial == "41"
    assert service.list_zones() == ["example.com"]
    with pytest.raises(ZoneNotFound):
        service.get_zone("missing.com")


def test_health(service: ZoneService, fake_knot: FakeKnot) -> None:
    assert service.health().knotd is True
    fake_knot.down = True
    assert service.health().status == "degraded"


def test_zone_lock_identity(service: ZoneService) -> None:
    assert service._zone_lock("a.com") is service._zone_lock("a.com")
    assert service._zone_lock("a.com") is not service._zone_lock("b.com")
