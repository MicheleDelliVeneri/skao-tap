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


class AuthenticationError(TAPError):
    """No usable credential: missing, malformed, expired or unverifiable token.

    Rendered with a ``WWW-Authenticate`` header carrying both the RFC 6750
    ``Bearer`` challenge and the IVOA ``ivoa_bearer`` one, which names the
    IAM's discovery document so a client can go and get a token.

    ``challenge`` is the ``ivoa_bearer`` value to advertise. Left unset, the
    renderer assumes a credential was presented and did not verify, and says
    so — the caller only has to pass one for the "nothing was presented" case,
    where the distinction matters to the client.
    """

    http_status = 401

    def __init__(self, message: str, challenge: str | None = None):
        super().__init__(message)
        self.challenge = challenge


class AuthorizationError(TAPError):
    """The caller is authenticated but not permitted to perform the operation."""

    http_status = 403
