"""Zone name normalization and validation.

The character rules double as path-traversal safety: a validated name can
never contain a path separator, ``..`` sequence, or hidden-file prefix.
IDN zones must be submitted as A-labels (punycode).
"""

import re
from collections.abc import Collection
from pathlib import Path

from .errors import ZoneNameInvalid, ZoneProtected

_MAX_NAME_LENGTH = 253
_LABEL_RE = re.compile(r"^(?!-)[a-z0-9_-]{1,63}(?<!-)$")


def normalize_zone_name(raw: str) -> str:
    """Return the canonical form: lowercase, no trailing dot."""
    name = raw.strip().lower().removesuffix(".")
    if not name:
        raise ZoneNameInvalid("zone name must not be empty or the root zone")
    if len(name) > _MAX_NAME_LENGTH:
        raise ZoneNameInvalid(f"zone name exceeds {_MAX_NAME_LENGTH} characters")
    for label in name.split("."):
        if not _LABEL_RE.match(label):
            raise ZoneNameInvalid(f"invalid label {label!r} in zone name {raw!r}")
    return name


def to_fqdn(name: str) -> str:
    """knotd talks fully qualified names with a trailing dot."""
    return f"{name}."


def zonefile_path(zones_dir: Path, name: str) -> Path:
    path = zones_dir / f"{name}.zone"
    if path.parent != zones_dir:  # unreachable after validation; defense in depth
        raise ZoneNameInvalid(f"zone name {name!r} escapes the zones directory")
    return path


def assert_not_protected(name: str, protected: Collection[str]) -> None:
    if name in protected:
        raise ZoneProtected(f"zone {name} is protected and cannot be managed through this API")
