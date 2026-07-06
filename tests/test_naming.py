from pathlib import Path

import pytest

from knot_api.errors import ZoneNameInvalid, ZoneProtected
from knot_api.naming import assert_not_protected, normalize_zone_name, to_fqdn, zonefile_path


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("example.com", "example.com"),
        ("example.com.", "example.com"),
        ("Example.COM.", "example.com"),
        (" example.com ", "example.com"),
        ("_acme-challenge.foo.bar", "_acme-challenge.foo.bar"),
        ("xn--mnchen-3ya.de", "xn--mnchen-3ya.de"),
        ("internal", "internal"),
        ("123.example.com", "123.example.com"),
    ],
)
def test_normalize_valid(raw: str, expected: str) -> None:
    assert normalize_zone_name(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        ".",
        "..",
        "a..b.com",
        ".com",
        "a.com..",
        "-bad.com",
        "bad-.com",
        "a" * 64 + ".com",
        "b." + "a" * 250 + ".com",
        "exa mple.com",
        "café.com",
        "../etc",
        "a/b.com",
        "a\\b.com",
        "*.example.com",
    ],
)
def test_normalize_invalid(raw: str) -> None:
    with pytest.raises(ZoneNameInvalid):
        normalize_zone_name(raw)


def test_to_fqdn() -> None:
    assert to_fqdn("example.com") == "example.com."


def test_zonefile_path() -> None:
    zones = Path("/var/lib/knot/zones")
    assert zonefile_path(zones, "example.com") == zones / "example.com.zone"


def test_assert_not_protected() -> None:
    assert_not_protected("example.com", {"catalog.example"})
    with pytest.raises(ZoneProtected):
        assert_not_protected("catalog.example", {"catalog.example"})
