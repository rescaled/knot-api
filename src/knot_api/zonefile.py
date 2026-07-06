"""Zonefile storage: staging, validation with kzonecheck, atomic installation."""

import contextlib
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from .errors import KnotOperationError, ZonefileInvalid, ZonefileTooLarge
from .naming import to_fqdn, zonefile_path

logger = logging.getLogger(__name__)

_VALIDATOR_OUTPUT_LIMIT = 4000


class ZonefileStore:
    def __init__(
        self, *, zones_dir: Path, kzonecheck_bin: str, kzonecheck_timeout: int, max_bytes: int
    ) -> None:
        self._zones_dir = zones_dir
        self._kzonecheck_bin = kzonecheck_bin
        self._kzonecheck_timeout = kzonecheck_timeout
        self._max_bytes = max_bytes

    def path_for(self, name: str) -> Path:
        return zonefile_path(self._zones_dir, name)

    def stage(self, name: str, text: str) -> Path:
        """Write the content to a temporary file next to its final location."""
        if not text.endswith("\n"):
            text += "\n"
        data = text.encode("utf-8")
        if len(data) > self._max_bytes:
            raise ZonefileTooLarge(
                f"zonefile is {len(data)} bytes, limit is {self._max_bytes} bytes"
            )
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self._zones_dir, prefix=f".{name}.", suffix=".tmp", delete=False
            ) as tmp:
                tmp.write(data)
                tmp.flush()
                os.fsync(tmp.fileno())
                os.fchmod(tmp.fileno(), 0o640)
                return Path(tmp.name)
        except OSError as exc:
            raise KnotOperationError(
                f"cannot write to zones directory {self._zones_dir}: {exc}"
            ) from exc

    def validate(self, name: str, path: Path) -> None:
        """Run kzonecheck against the staged file — same parser knotd uses."""
        argv = [self._kzonecheck_bin, "-o", to_fqdn(name), str(path)]
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=self._kzonecheck_timeout
            )
        except FileNotFoundError as exc:
            raise KnotOperationError(
                f"zonefile validator {self._kzonecheck_bin!r} not found — install the knot package"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ZonefileInvalid(
                f"kzonecheck timed out after {self._kzonecheck_timeout}s"
            ) from exc
        if result.returncode != 0:
            output = (result.stdout + result.stderr).strip()[:_VALIDATOR_OUTPUT_LIMIT]
            raise ZonefileInvalid(f"zonefile failed validation: {output or 'kzonecheck error'}")

    def install(self, staged: Path, name: str) -> Path:
        """Atomically move the staged file into place."""
        final = self.path_for(name)
        try:
            os.replace(staged, final)
        except OSError as exc:
            raise KnotOperationError(f"cannot install zonefile {final}: {exc}") from exc
        self._fsync_dir()
        return final

    def discard(self, staged: Path) -> None:
        staged.unlink(missing_ok=True)

    def remove(self, name: str) -> None:
        self.path_for(name).unlink(missing_ok=True)
        self._fsync_dir()

    def _fsync_dir(self) -> None:
        # Durability of the rename/unlink itself; best effort.
        with contextlib.suppress(OSError):
            fd = os.open(self._zones_dir, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
