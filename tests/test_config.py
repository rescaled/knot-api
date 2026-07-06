from pathlib import Path

import pytest
from pydantic import ValidationError

from knot_api.config import Settings


def test_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KNOT_API_TOKEN", "s3cret")
    monkeypatch.setenv("KNOT_API_ZONES_DIR", "/srv/zones")
    monkeypatch.setenv("KNOT_API_PROTECTED_ZONES", "a.com, b.com,")
    monkeypatch.setenv("KNOT_API_CATALOG_ZONE", "catz")
    monkeypatch.setenv("KNOT_API_ABORT_STALE_TXN_ON_STARTUP", "true")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.token.get_secret_value() == "s3cret"
    assert settings.zones_dir == Path("/srv/zones")
    assert settings.protected_zones == ["a.com", "b.com"]
    assert settings.catalog_zone == "catz"
    assert settings.abort_stale_txn_on_startup is True


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KNOT_API_TOKEN", "s3cret")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.zones_dir == Path("/var/lib/knot/zones")
    assert settings.knot_socket == Path("/run/knot/knot.sock")
    assert settings.zone_template == "member"
    assert settings.txn_retries == 5
    assert settings.protected_zones == []
    assert settings.abort_stale_txn_on_startup is False


def test_token_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KNOT_API_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_token_not_leaked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KNOT_API_TOKEN", "s3cret")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert "s3cret" not in repr(settings)
