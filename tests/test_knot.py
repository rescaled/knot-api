"""LibknotClient tests against a scripted KnotCtl double (no libknot.so needed)."""

from pathlib import Path
from typing import Any, ClassVar

import libknot.control as libknot_control
import pytest

from knot_api.errors import (
    KnotOperationError,
    KnotTxnBusy,
    KnotUnavailable,
    ZoneNotFound,
)
from knot_api.knot import LibknotClient

Outcome = dict[str, Any] | Exception


class CtlHub:
    """Per-test script: queues of replies (dict) or exceptions, keyed by command."""

    def __init__(self) -> None:
        self.instances: list[ScriptedCtl] = []
        self.blocks: list[dict[str, str]] = []
        self.connect_error = False
        self.responses: dict[str, list[Outcome]] = {}

    def handle(self, kwargs: dict[str, str]) -> Outcome:
        queue = self.responses.get(kwargs["cmd"])
        if queue:
            return queue.pop(0)
        return {}

    def commands(self) -> list[str]:
        return [block["cmd"] for block in self.blocks]


class ScriptedCtl:
    """Stands in for libknot.control.KnotCtl."""

    hub: ClassVar[CtlHub]

    def __init__(self) -> None:
        self.hub.instances.append(self)
        self.timeout: int | None = None
        self.finalizers: list[object] = []
        self._pending: Outcome = {}

    def set_timeout(self, timeout: int) -> None:
        self.timeout = timeout

    def connect(self, path: str) -> None:
        if self.hub.connect_error:
            raise libknot_control.KnotCtlErrorConnect("not exists")

    def send_block(self, **kwargs: str) -> None:
        block = {key: value for key, value in kwargs.items() if value is not None}
        self.hub.blocks.append(block)
        self._pending = self.hub.handle(block)

    def receive_block(self) -> dict[str, Any]:
        if isinstance(self._pending, Exception):
            raise self._pending
        return self._pending

    def send(self, data_type: object, data: object = None) -> None:
        self.finalizers.append(data_type)

    def close(self) -> None:
        self.finalizers.append("close")


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch) -> CtlHub:
    ctl_hub = CtlHub()
    monkeypatch.setattr(ScriptedCtl, "hub", ctl_hub, raising=False)
    monkeypatch.setattr(libknot_control, "KnotCtl", ScriptedCtl)
    return ctl_hub


@pytest.fixture
def knot_client() -> LibknotClient:
    return LibknotClient(
        socket_path=Path("/tmp/knot.sock"),
        timeout=5,
        reload_timeout=30,
        txn_retries=3,
        txn_retry_base_delay=0.001,
    )


def remote_error(message: str) -> libknot_control.KnotCtlErrorRemote:
    return libknot_control.KnotCtlErrorRemote(message)


def test_add_zone_runs_whole_txn_on_one_connection(hub: CtlHub, knot_client: LibknotClient) -> None:
    knot_client.add_zone("example.com.", "member")
    assert hub.blocks == [
        {"cmd": "conf-begin"},
        {"cmd": "conf-set", "section": "zone", "identifier": "example.com."},
        {
            "cmd": "conf-set",
            "section": "zone",
            "identifier": "example.com.",
            "item": "template",
            "data": "member",
        },
        {"cmd": "conf-commit"},
    ]
    assert len(hub.instances) == 1
    ctl = hub.instances[0]
    assert libknot_control.KnotCtlType.END in ctl.finalizers
    assert "close" in ctl.finalizers


def test_txn_aborts_when_conf_set_fails(hub: CtlHub, knot_client: LibknotClient) -> None:
    hub.responses["conf-set"] = [{}, remote_error("invalid item")]
    with pytest.raises(KnotOperationError, match="invalid item"):
        knot_client.add_zone("example.com.", "member")
    assert hub.commands() == ["conf-begin", "conf-set", "conf-set", "conf-abort"]


def test_txn_aborts_when_commit_fails(hub: CtlHub, knot_client: LibknotClient) -> None:
    hub.responses["conf-commit"] = [remote_error("semantic check")]
    with pytest.raises(KnotOperationError, match="semantic check"):
        knot_client.add_zone("example.com.", "member")
    assert hub.commands()[-2:] == ["conf-commit", "conf-abort"]


def test_abort_falls_back_to_fresh_connection(hub: CtlHub, knot_client: LibknotClient) -> None:
    hub.responses["conf-commit"] = [remote_error("semantic check")]
    hub.responses["conf-abort"] = [remote_error("resource busy"), {}]
    with pytest.raises(KnotOperationError, match="semantic check"):
        knot_client.add_zone("example.com.", "member")
    assert hub.commands().count("conf-abort") == 2
    assert len(hub.instances) == 2  # txn connection + fresh abort connection


def test_begin_busy_retries_then_gives_up(hub: CtlHub, knot_client: LibknotClient) -> None:
    hub.responses["conf-begin"] = [remote_error("too many transactions")] * 3
    with pytest.raises(KnotTxnBusy):
        knot_client.add_zone("example.com.", "member")
    assert hub.commands() == ["conf-begin"] * 3  # never opened -> nothing to abort


def test_begin_busy_then_succeeds(hub: CtlHub, knot_client: LibknotClient) -> None:
    hub.responses["conf-begin"] = [remote_error("too many transactions"), {}]
    knot_client.add_zone("example.com.", "member")
    assert hub.commands() == ["conf-begin", "conf-begin", "conf-set", "conf-set", "conf-commit"]


def test_remove_zone_txn(hub: CtlHub, knot_client: LibknotClient) -> None:
    knot_client.remove_zone("example.com.")
    assert hub.blocks == [
        {"cmd": "conf-begin"},
        {"cmd": "conf-unset", "section": "zone", "identifier": "example.com."},
        {"cmd": "conf-commit"},
    ]


def test_zone_exists_uses_conf_read(hub: CtlHub, knot_client: LibknotClient) -> None:
    hub.responses["conf-read"] = [
        remote_error("invalid identifier"),
        {"zone": {"example.com.": {}}},
    ]
    assert knot_client.zone_exists("example.com.") is False
    assert knot_client.zone_exists("example.com.") is True
    assert hub.blocks[0] == {"cmd": "conf-read", "section": "zone", "identifier": "example.com."}


def test_list_zones(hub: CtlHub, knot_client: LibknotClient) -> None:
    hub.responses["conf-read"] = [{"zone": {"b.com.": {}, "a.com.": {}}}]
    assert knot_client.list_zones() == ["a.com", "b.com"]
    assert hub.blocks[0] == {"cmd": "conf-read", "section": "zone", "item": "domain"}


def test_zone_reload_is_blocking_and_never_forced(
    hub: CtlHub, knot_client: LibknotClient
) -> None:
    knot_client.zone_reload("example.com.")
    assert hub.blocks == [{"cmd": "zone-reload", "zone": "example.com.", "flags": "B"}]
    assert hub.instances[0].timeout == 30  # reload_timeout, not the default op timeout


def test_zone_purge_orphan_flags(hub: CtlHub, knot_client: LibknotClient) -> None:
    knot_client.zone_purge_orphan("example.com.")
    assert hub.blocks == [
        {"cmd": "zone-purge", "zone": "example.com.", "filters": "o", "flags": "F"}
    ]


def test_zone_status_parses_reply(hub: CtlHub, knot_client: LibknotClient) -> None:
    hub.responses["zone-status"] = [{"example.com.": {"serial": "7", "role": "master"}}]
    assert knot_client.zone_status("example.com.") == {"serial": "7", "role": "master"}


def test_zone_status_unknown_zone(hub: CtlHub, knot_client: LibknotClient) -> None:
    hub.responses["zone-status"] = [remote_error("no such zone found")]
    with pytest.raises(ZoneNotFound):
        knot_client.zone_status("example.com.")


def test_connect_failure_maps_to_unavailable(hub: CtlHub, knot_client: LibknotClient) -> None:
    hub.connect_error = True
    with pytest.raises(KnotUnavailable, match="control socket"):
        knot_client.status()


def test_transport_failure_maps_to_unavailable(hub: CtlHub, knot_client: LibknotClient) -> None:
    hub.responses["status"] = [libknot_control.KnotCtlErrorReceive("connection timeout")]
    with pytest.raises(KnotUnavailable, match="connection timeout"):
        knot_client.status()


def test_abort_stale_txn_swallows_no_active_transaction(
    hub: CtlHub, knot_client: LibknotClient
) -> None:
    hub.responses["conf-abort"] = [remote_error("no active transaction")]
    knot_client.abort_stale_txn()  # must not raise
    assert hub.commands() == ["conf-abort"]
