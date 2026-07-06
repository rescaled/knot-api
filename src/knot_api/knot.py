"""knotd control-socket client built on the official libknot bindings.

Design constraints (verified against Knot 3.5.5 daemon sources):

- Configuration transactions are daemon-global: only one can be open at a
  time, they persist across control connections, and they never expire.
  While one is open, knotd routes *all* control connections to the thread
  that owns it — a long transaction stalls every other control client.
  Therefore: a process-wide lock serializes transactions, the whole
  transaction runs on a single connection, the begin→commit window contains
  nothing but the conf-set/conf-unset calls, and every failure path aborts.
- ``zone-reload`` needs the blocking flag ``"B"`` but never ``"F"``:
  force switches to reloading zone *modules* without touching zone data.
- ``zone-purge`` of an unconfigured zone needs ``filters="o"`` (orphan);
  the zonefile itself cannot be purged in that mode and is unlinked by the
  caller.
"""

import contextlib
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

import libknot
import libknot.control as libknot_control

from .errors import KnotApiError, KnotOperationError, KnotTxnBusy, KnotUnavailable, ZoneNotFound

logger = logging.getLogger(__name__)

# Daemon error strings (knot_strerror), pinned to Knot 3.5.x src/libknot/error.c.
# The control protocol transports no numeric codes, only these strings.
ERR_TXN_EXISTS = "too many transactions"
ERR_TXN_NOT_EXISTS = "no active transaction"
ERR_NO_SUCH_ZONE = "no such zone found"
ERR_INVALID_IDENTIFIER = "invalid identifier"
ERR_NOT_EXISTS = "not exists"

FLAG_FORCE = "F"
FLAG_BLOCKING = "B"
PURGE_FILTER_ORPHAN = "o"


class KnotControl(Protocol):
    """The subset of knotd control operations the service layer needs.

    All ``zone`` arguments are fully qualified names with a trailing dot.
    """

    def status(self) -> None: ...

    def zone_exists(self, zone: str) -> bool: ...

    def list_zones(self) -> list[str]: ...

    def zone_status(self, zone: str) -> dict[str, str]: ...

    def zone_reload(self, zone: str) -> None: ...

    def zone_purge_orphan(self, zone: str) -> None: ...

    def add_zone(self, zone: str, template: str) -> None: ...

    def remove_zone(self, zone: str) -> None: ...

    def abort_stale_txn(self) -> None: ...


class LibknotClient:
    """One fresh control connection per operation; transactions serialized."""

    def __init__(
        self,
        *,
        socket_path: Path,
        timeout: int,
        reload_timeout: int,
        txn_retries: int,
        txn_retry_base_delay: float,
        libknot_so: str | None = None,
    ) -> None:
        if libknot_so:
            libknot.Knot(libknot_so)
        self._socket = str(socket_path)
        self._timeout = timeout
        self._reload_timeout = reload_timeout
        self._txn_retries = txn_retries
        self._txn_retry_base_delay = txn_retry_base_delay
        self._txn_lock = threading.Lock()

    # -- public operations ---------------------------------------------------

    def status(self) -> None:
        with self._connect() as ctl:
            self._command(ctl, cmd="status")

    def zone_exists(self, zone: str) -> bool:
        with self._connect() as ctl:
            try:
                self._command(ctl, cmd="conf-read", section="zone", identifier=zone)
            except ZoneNotFound:
                return False
        return True

    def list_zones(self) -> list[str]:
        with self._connect() as ctl:
            reply = self._command(ctl, cmd="conf-read", section="zone", item="domain")
        zones = reply.get("zone", {})
        return sorted(name.rstrip(".") for name in zones)

    def zone_status(self, zone: str) -> dict[str, str]:
        with self._connect() as ctl:
            reply = self._command(ctl, cmd="zone-status", zone=zone)
        status = reply.get(zone)
        if status is None and reply:  # daemon echoed a differently-canonicalized name
            status = next(iter(reply.values()))
        return {str(key): str(value) for key, value in (status or {}).items()}

    def zone_reload(self, zone: str) -> None:
        with self._connect(timeout=self._reload_timeout) as ctl:
            self._command(ctl, cmd="zone-reload", zone=zone, flags=FLAG_BLOCKING)

    def zone_purge_orphan(self, zone: str) -> None:
        with self._connect(timeout=self._reload_timeout) as ctl:
            self._command(
                ctl, cmd="zone-purge", zone=zone, filters=PURGE_FILTER_ORPHAN, flags=FLAG_FORCE
            )

    def add_zone(self, zone: str, template: str) -> None:
        with self._txn() as ctl:
            self._command(ctl, cmd="conf-set", section="zone", identifier=zone)
            self._command(
                ctl, cmd="conf-set", section="zone", identifier=zone, item="template", data=template
            )

    def remove_zone(self, zone: str) -> None:
        with self._txn() as ctl:
            self._command(ctl, cmd="conf-unset", section="zone", identifier=zone)

    def abort_stale_txn(self) -> None:
        with self._connect() as ctl:
            if self._conf_abort(ctl):
                logger.warning("aborted a stale knotd configuration transaction at startup")

    # -- plumbing --------------------------------------------------------------

    @contextmanager
    def _connect(self, timeout: int | None = None) -> Iterator[libknot_control.KnotCtl]:
        ctl = libknot_control.KnotCtl()
        ctl.set_timeout(timeout if timeout is not None else self._timeout)
        try:
            ctl.connect(self._socket)
        except libknot_control.KnotCtlError as exc:
            raise KnotUnavailable(
                f"cannot connect to knotd control socket {self._socket}: {exc.message}"
            ) from exc
        try:
            yield ctl
        finally:
            with contextlib.suppress(Exception):
                ctl.send(libknot_control.KnotCtlType.END)
            with contextlib.suppress(Exception):
                ctl.close()

    @contextmanager
    def _txn(self) -> Iterator[libknot_control.KnotCtl]:
        """A whole conf transaction: lock → begin (with retry) → yield → commit.

        Aborts on every failure path, including a failed commit; a dangling
        transaction would block all other control clients indefinitely.
        """
        with self._txn_lock, self._connect() as ctl:
            self._conf_begin_with_retry(ctl)
            try:
                yield ctl
                self._command(ctl, cmd="conf-commit")
            except BaseException:
                self._abort_best_effort(ctl)
                raise

    def _command(self, ctl: libknot_control.KnotCtl, **kwargs: str) -> dict[str, Any]:
        try:
            ctl.send_block(**kwargs)
            reply = ctl.receive_block()
        except libknot_control.KnotCtlErrorRemote as exc:
            raise self._translate_remote(exc) from exc
        except libknot_control.KnotCtlError as exc:
            raise KnotUnavailable(
                f"control I/O with knotd failed during {kwargs.get('cmd')!r}: {exc.message}"
            ) from exc
        return reply or {}

    @staticmethod
    def _translate_remote(exc: libknot_control.KnotCtlErrorRemote) -> KnotApiError:
        message = exc.message or "unknown control error"
        if message == ERR_TXN_EXISTS:
            return KnotTxnBusy("another configuration transaction is open on knotd")
        if message in (ERR_NO_SUCH_ZONE, ERR_INVALID_IDENTIFIER, ERR_NOT_EXISTS):
            return ZoneNotFound(f"knotd: {message}")
        return KnotOperationError(f"knotd: {message}")

    def _conf_begin_with_retry(self, ctl: libknot_control.KnotCtl) -> None:
        delay = self._txn_retry_base_delay
        for attempt in range(1, self._txn_retries + 1):
            try:
                self._command(ctl, cmd="conf-begin")
                return
            except KnotTxnBusy:
                if attempt == self._txn_retries:
                    raise
                logger.info(
                    "knotd has an open configuration transaction; retry %d/%d in %.2fs",
                    attempt,
                    self._txn_retries,
                    delay,
                )
                time.sleep(delay)
                delay *= 2

    def _conf_abort(self, ctl: libknot_control.KnotCtl) -> bool:
        """Roll back the open transaction. Returns False if none was open."""
        try:
            ctl.send_block(cmd="conf-abort")
            ctl.receive_block()
        except libknot_control.KnotCtlErrorRemote as exc:
            if (exc.message or "") == ERR_TXN_NOT_EXISTS:
                return False
            raise self._translate_remote(exc) from exc
        except libknot_control.KnotCtlError as exc:
            raise KnotUnavailable(f"control I/O during conf-abort failed: {exc.message}") from exc
        return True

    def _abort_best_effort(self, ctl: libknot_control.KnotCtl) -> None:
        try:
            self._conf_abort(ctl)
            return
        except KnotApiError:
            pass  # the transaction's connection may be broken — try a fresh one
        try:
            with self._connect() as fresh:
                self._conf_abort(fresh)
        except KnotApiError:
            logger.warning(
                "could not abort the open knotd configuration transaction; "
                "if one is stuck, run `knotc conf-abort`"
            )
