"""Domain errors raised by the service layer.

HTTP status mapping happens exclusively in :mod:`knot_api.app`.
"""


class KnotApiError(Exception):
    """Base class for all knot-api domain errors."""


class ZoneNameInvalid(KnotApiError):
    """The zone name is syntactically invalid."""


class ZoneProtected(KnotApiError):
    """The zone is protected from modification through this API."""


class ZoneNotFound(KnotApiError):
    """The zone is not configured on the server."""


class ZonefileInvalid(KnotApiError):
    """The submitted zonefile failed validation; carries the validator output."""


class ZonefileTooLarge(KnotApiError):
    """The submitted zonefile exceeds the configured size limit."""


class KnotUnavailable(KnotApiError):
    """knotd cannot be reached over its control socket."""


class KnotTxnBusy(KnotApiError):
    """Another configuration transaction is open and retries were exhausted."""


class KnotOperationError(KnotApiError):
    """knotd rejected or failed an operation."""
