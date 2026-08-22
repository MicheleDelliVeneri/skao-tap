"""TAP/DALI error hierarchy."""


class TAPError(Exception):
    """Base error, rendered as a DALI VOTable error document."""

    http_status = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class UsageError(TAPError):
    """Bad or missing request parameters (UsageFault)."""


class QueryParseError(TAPError):
    """ADQL query failed to parse or translate."""


class NotFoundError(TAPError):
    http_status = 404


class ServiceError(TAPError):
    http_status = 500
