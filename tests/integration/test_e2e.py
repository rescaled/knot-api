"""End-to-end tests against the docker compose harness (deploy/docker-compose.yml).

Run with:
    docker compose -f deploy/docker-compose.yml up --build -d
    KNOT_API_E2E=1 .venv/bin/pytest -m integration
"""

import os
import time
import uuid

import httpx
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("KNOT_API_E2E"), reason="KNOT_API_E2E not set"),
]

BASE_URL = os.environ.get("KNOT_API_E2E_URL", "http://127.0.0.1:8080")
TOKEN = os.environ.get("KNOT_API_E2E_TOKEN", "e2e-secret")
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def zonefile(zone: str, extra: str = "") -> str:
    return (
        f"{zone}. 3600 SOA ns1.{zone}. hostmaster.{zone}. 1 86400 900 691200 3600\n"
        f"{zone}. 3600 NS ns1.{zone}.\n"
        f"ns1.{zone}. 3600 A 192.0.2.1\n" + extra
    )


@pytest.fixture(scope="module")
def api() -> httpx.Client:
    client = httpx.Client(base_url=BASE_URL, timeout=30)
    deadline = time.monotonic() + 60
    while True:
        try:
            if client.get("/v1/healthz").status_code == 200:
                break
        except httpx.TransportError:
            pass
        if time.monotonic() > deadline:
            pytest.fail("API/knotd stack did not become healthy within 60s")
        time.sleep(1)
    return client


def test_full_zone_lifecycle(api: httpx.Client) -> None:
    zone = f"e2e-{uuid.uuid4().hex[:8]}.test"

    created = api.put(f"/v1/zones/{zone}", json={"zonefile": zonefile(zone)}, headers=AUTH)
    assert created.status_code == 201, created.text
    first_serial = created.json()["serial"]
    assert first_serial is not None

    listing = api.get("/v1/zones", headers=AUTH)
    assert zone in listing.json()["zones"]

    time.sleep(1.1)  # serial-policy unixtime needs a second to tick
    updated = api.put(
        f"/v1/zones/{zone}",
        json={"zonefile": zonefile(zone, f"www.{zone}. 3600 A 192.0.2.2\n")},
        headers=AUTH,
    )
    assert updated.status_code == 200, updated.text
    assert int(updated.json()["serial"]) > int(first_serial)  # knot manages the serial

    deleted = api.delete(f"/v1/zones/{zone}", headers=AUTH)
    assert deleted.status_code == 204, deleted.text
    assert api.get(f"/v1/zones/{zone}", headers=AUTH).status_code == 404
    assert api.delete(f"/v1/zones/{zone}", headers=AUTH).status_code == 404


def test_invalid_zonefile_rejected(api: httpx.Client) -> None:
    response = api.put(
        "/v1/zones/broken.test", json={"zonefile": "utter garbage\n"}, headers=AUTH
    )
    assert response.status_code == 422
    assert api.get("/v1/zones/broken.test", headers=AUTH).status_code == 404


def test_catalog_zone_is_protected(api: httpx.Client) -> None:
    response = api.put(
        "/v1/zones/catalog", json={"zonefile": zonefile("catalog")}, headers=AUTH
    )
    assert response.status_code == 403


def test_auth_enforced(api: httpx.Client) -> None:
    assert api.get("/v1/zones").status_code == 401
