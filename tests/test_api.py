from pathlib import Path

from fastapi.testclient import TestClient

from conftest import (
    CATALOG_ZONE,
    SAMPLE_ZONEFILE,
    UPDATED_ZONEFILE,
    FakeKnot,
    MakeClient,
)
from knot_api.errors import KnotTxnBusy

ZONE_URL = "/v1/zones/example.com"
PUT_BODY = {"zonefile": SAMPLE_ZONEFILE}


def test_zones_require_token(client: TestClient) -> None:
    response = client.put(ZONE_URL, json=PUT_BODY)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    response = client.put(ZONE_URL, json=PUT_BODY, headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


def test_healthz_needs_no_token(client: TestClient, fake_knot: FakeKnot) -> None:
    response = client.get("/v1/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "knotd": True}
    fake_knot.down = True
    assert client.get("/v1/healthz").status_code == 503


def test_create_update_delete_cycle(
    client: TestClient, auth: dict[str, str], fake_knot: FakeKnot, zones_dir: Path
) -> None:
    created = client.put(ZONE_URL, json=PUT_BODY, headers=auth)
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "example.com"
    assert body["created"] is True
    assert body["serial"] == str(FakeKnot.INITIAL_SERIAL)
    assert (zones_dir / "example.com.zone").read_text() == SAMPLE_ZONEFILE

    updated = client.put(ZONE_URL, json={"zonefile": UPDATED_ZONEFILE}, headers=auth)
    assert updated.status_code == 200
    assert updated.json()["created"] is False
    assert updated.json()["serial"] == str(FakeKnot.INITIAL_SERIAL + 1)

    listing = client.get("/v1/zones", headers=auth)
    assert listing.status_code == 200
    assert listing.json() == {"zones": ["example.com"]}

    status = client.get(ZONE_URL, headers=auth)
    assert status.status_code == 200
    assert status.json()["serial"] == str(FakeKnot.INITIAL_SERIAL + 1)

    deleted = client.delete(ZONE_URL, headers=auth)
    assert deleted.status_code == 204
    assert not (zones_dir / "example.com.zone").exists()
    assert client.get(ZONE_URL, headers=auth).status_code == 404
    assert client.delete(ZONE_URL, headers=auth).status_code == 404


def test_zone_name_is_normalized(client: TestClient, auth: dict[str, str]) -> None:
    response = client.put("/v1/zones/Example.COM.", json=PUT_BODY, headers=auth)
    assert response.status_code == 201
    assert response.json()["name"] == "example.com"


def test_invalid_zone_name(client: TestClient, auth: dict[str, str]) -> None:
    response = client.put("/v1/zones/bad..name", json=PUT_BODY, headers=auth)
    assert response.status_code == 422
    assert "label" in response.json()["detail"]


def test_invalid_zonefile_returns_validator_output(
    client: TestClient, auth: dict[str, str]
) -> None:
    response = client.put(ZONE_URL, json={"zonefile": "INVALID GARBAGE"}, headers=auth)
    assert response.status_code == 422
    assert "record parse error" in response.json()["detail"]


def test_empty_zonefile_rejected(client: TestClient, auth: dict[str, str]) -> None:
    response = client.put(ZONE_URL, json={"zonefile": ""}, headers=auth)
    assert response.status_code == 422


def test_protected_zone_returns_403(client: TestClient, auth: dict[str, str]) -> None:
    response = client.put(f"/v1/zones/{CATALOG_ZONE}", json=PUT_BODY, headers=auth)
    assert response.status_code == 403
    response = client.delete(f"/v1/zones/{CATALOG_ZONE}", headers=auth)
    assert response.status_code == 403


def test_oversized_zonefile_returns_413(make_client: MakeClient, auth: dict[str, str]) -> None:
    small_client, _ = make_client(max_zonefile_bytes=16)
    response = small_client.put(ZONE_URL, json={"zonefile": "x" * 64}, headers=auth)
    assert response.status_code == 413


def test_txn_busy_returns_503_with_retry_after(
    client: TestClient, auth: dict[str, str], fake_knot: FakeKnot
) -> None:
    fake_knot.fail_on["add_zone"] = KnotTxnBusy("another configuration transaction is open")
    response = client.put(ZONE_URL, json=PUT_BODY, headers=auth)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "30"


def test_knotd_down_returns_503(
    client: TestClient, auth: dict[str, str], fake_knot: FakeKnot
) -> None:
    fake_knot.down = True
    response = client.put(ZONE_URL, json=PUT_BODY, headers=auth)
    assert response.status_code == 503
    assert "knotd" in response.json()["detail"]


def test_stale_txn_abort_on_startup(make_client: MakeClient) -> None:
    _, fake = make_client(abort_stale_txn_on_startup=True)
    assert fake.stale_txn_aborted is True
