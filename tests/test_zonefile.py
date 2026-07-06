import subprocess
from pathlib import Path

import pytest

from conftest import SAMPLE_ZONEFILE
from knot_api import zonefile as zonefile_module
from knot_api.errors import KnotOperationError, ZonefileInvalid, ZonefileTooLarge
from knot_api.zonefile import ZonefileStore


def test_stage_writes_tmp_in_zones_dir(store: ZonefileStore, zones_dir: Path) -> None:
    staged = store.stage("example.com", SAMPLE_ZONEFILE)
    assert staged.parent == zones_dir
    assert staged.read_text() == SAMPLE_ZONEFILE
    assert (staged.stat().st_mode & 0o777) == 0o640


def test_stage_appends_missing_trailing_newline(store: ZonefileStore) -> None:
    staged = store.stage("example.com", "example.com. 3600 SOA a. b. 1 2 3 4 5")
    assert staged.read_text().endswith("\n")


def test_stage_enforces_size_limit(zones_dir: Path, fake_kzonecheck: Path) -> None:
    store = ZonefileStore(
        zones_dir=zones_dir, kzonecheck_bin=str(fake_kzonecheck), kzonecheck_timeout=5, max_bytes=16
    )
    with pytest.raises(ZonefileTooLarge):
        store.stage("example.com", "x" * 64)


def test_validate_accepts_good_zonefile(store: ZonefileStore) -> None:
    staged = store.stage("example.com", SAMPLE_ZONEFILE)
    store.validate("example.com", staged)


def test_validate_rejects_bad_zonefile_with_output(store: ZonefileStore) -> None:
    staged = store.stage("example.com", "INVALID GARBAGE\n")
    with pytest.raises(ZonefileInvalid, match="record parse error"):
        store.validate("example.com", staged)


def test_validate_missing_binary(zones_dir: Path) -> None:
    store = ZonefileStore(
        zones_dir=zones_dir, kzonecheck_bin="/nonexistent/kzonecheck", kzonecheck_timeout=5,
        max_bytes=1024,
    )
    staged = store.stage("example.com", SAMPLE_ZONEFILE)
    with pytest.raises(KnotOperationError, match="not found"):
        store.validate("example.com", staged)


def test_validate_passes_origin_flag(
    store: ZonefileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(zonefile_module.subprocess, "run", fake_run)
    staged = store.stage("example.com", SAMPLE_ZONEFILE)
    store.validate("example.com", staged)
    assert captured["argv"][1:3] == ["-o", "example.com."]
    assert captured["argv"][-1] == str(staged)


def test_install_moves_atomically(store: ZonefileStore, zones_dir: Path) -> None:
    staged = store.stage("example.com", SAMPLE_ZONEFILE)
    final = store.install(staged, "example.com")
    assert final == zones_dir / "example.com.zone"
    assert final.read_text() == SAMPLE_ZONEFILE
    assert not staged.exists()


def test_discard_and_remove_are_idempotent(store: ZonefileStore, zones_dir: Path) -> None:
    staged = store.stage("example.com", SAMPLE_ZONEFILE)
    store.discard(staged)
    store.discard(staged)
    assert not staged.exists()
    store.install(store.stage("example.com", SAMPLE_ZONEFILE), "example.com")
    store.remove("example.com")
    store.remove("example.com")
    assert not (zones_dir / "example.com.zone").exists()
