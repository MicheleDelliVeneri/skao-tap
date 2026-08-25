"""Bearer-token verification against an OpenID Connect provider (INDIGO IAM).

Authenticity is always established here, by this service, whatever
authorisation plugin is active: the token's signature is checked against the
issuer's published JWKS, and its issuer, expiry and audience are checked
against the deployment's configuration. No authorisation decision — local or
delegated — is ever made on an unverified token.
"""

import threading
import time

import httpx
import jwt
from jwt import PyJWKClient

from ..config import settings
from ..errors import AuthenticationError, ServiceError

# Claims INDIGO IAM (and the wider WLCG profile) use for group membership.
# Deliberately excludes `entitlements`/`eduperson_entitlement`: those can be
# passed through from a federated home IdP, so treating them as group names
# would let an attribute asserted elsewhere match a local policy group. A
# deployment that does trust them can add them via TAP_IAM_GROUP_CLAIMS.
DEFAULT_GROUP_CLAIMS = ("groups", "wlcg.groups")


class Principal:
    """The verified identity behind a request.

    ``anonymous`` principals carry no claims: they are what an unauthenticated
    request gets on an endpoint that does not require a token.
    """

    __slots__ = ("claims", "groups", "scopes", "subject", "token")

    def __init__(
        self,
        subject: str | None = None,
        groups: tuple[str, ...] = (),
        scopes: tuple[str, ...] = (),
        token: str | None = None,
        claims: dict | None = None,
    ):
        self.subject = subject
        self.groups = groups
        self.scopes = scopes
        self.token = token
        self.claims = claims or {}

    @property
    def is_anonymous(self) -> bool:
        return self.subject is None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        if self.is_anonymous:
            return "Principal(anonymous)"
        return (
            f"Principal(subject={self.subject!r}, groups={len(self.groups)},"
            f" scopes={len(self.scopes)})"
        )


ANONYMOUS = Principal()


class IAMTokenVerifier:
    """Verifies IAM-issued JWT access tokens against the issuer's JWKS.

    The OIDC discovery document and the signing keys are fetched once and
    cached; an unknown ``kid`` triggers a refresh (rate-limited, so a stream
    of bogus tokens cannot turn into a stream of requests to the IAM).
    """

    def __init__(
        self,
        issuer: str,
        audience: str | None = None,
        jwks_cache_s: int = 300,
        timeout_s: float = 5.0,
        well_known_url: str | None = None,
        allow_any_audience: bool = False,
        group_claims: tuple[str, ...] = ("groups", "wlcg.groups"),
    ):
        if not issuer:
            raise ServiceError("an IAM issuer is required to verify tokens (TAP_IAM_ISSUER)")
        if not audience and not allow_any_audience:
            # One IAM issues tokens to many services. Without an audience
            # check, a token minted for any other client of the same issuer
            # is accepted here as its bearer's credential, so skipping the
            # check has to be a deliberate, recorded choice.
            raise ServiceError(
                "an expected token audience is required (TAP_IAM_AUDIENCE);"
                " set TAP_IAM_ALLOW_ANY_AUDIENCE=true to accept any token"
                " from the issuer, which allows tokens issued to other"
                " services to be replayed against this one"
            )
        self.issuer = issuer.rstrip("/")
        self.audience = audience or None
        self.jwks_cache_s = jwks_cache_s
        self.timeout_s = timeout_s
        self.well_known_url = well_known_url or f"{self.issuer}/.well-known/openid-configuration"
        self.group_claims = tuple(group_claims)
        self._lock = threading.Lock()
        self._jwks_client: PyJWKClient | None = None
        self._jwks_uri: str | None = None
        self._discovered_at = 0.0

    # -- discovery ----------------------------------------------------------

    def _discover(self) -> str:
        """The issuer's jwks_uri, cached for jwks_cache_s."""
        with self._lock:
            fresh = time.monotonic() - self._discovered_at < self.jwks_cache_s
            if self._jwks_uri is not None and fresh:
                return self._jwks_uri
            try:
                response = httpx.get(self.well_known_url, timeout=self.timeout_s)
                response.raise_for_status()
                document = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise ServiceError(f"IAM discovery failed at {self.well_known_url}") from exc
            issuer = str(document.get("issuer", "")).rstrip("/")
            if issuer != self.issuer:
                # a discovery document that names another issuer would let a
                # misconfigured URL move trust to a different IAM
                raise ServiceError(
                    f"IAM discovery document declares issuer {issuer!r},"
                    f" configured issuer is {self.issuer!r}"
                )
            jwks_uri = document.get("jwks_uri")
            if not jwks_uri:
                raise ServiceError("IAM discovery document has no jwks_uri")
            if jwks_uri != self._jwks_uri:
                self._jwks_client = PyJWKClient(
                    jwks_uri, cache_keys=True, lifespan=self.jwks_cache_s
                )
                self._jwks_uri = jwks_uri
            self._discovered_at = time.monotonic()
            return jwks_uri

    def _signing_key(self, token: str):
        self._discover()
        assert self._jwks_client is not None  # set by _discover
        try:
            return self._jwks_client.get_signing_key_from_jwt(token).key
        except jwt.PyJWKClientError as exc:
            # unknown kid: the IAM may have rotated. _discover's cache window
            # rate-limits how often a bogus token can force a refetch.
            raise AuthenticationError("token is not signed by a known IAM key") from exc
        except jwt.DecodeError as exc:
            raise AuthenticationError("bearer token is not a well-formed JWT") from exc

    # -- verification -------------------------------------------------------

    def verify(self, token: str) -> Principal:
        """Verify a bearer token and return the principal it represents."""
        if not token:
            raise AuthenticationError("no bearer token supplied")
        key = self._signing_key(token)
        options = {"require": ["exp", "iss", "sub"], "verify_aud": self.audience is not None}
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
                issuer=self.issuer,
                audience=self.audience,
                options=options,
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError("bearer token has expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthenticationError(
                "bearer token is not addressed to this service"
                f" (expected audience {self.audience!r})"
            ) from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthenticationError(f"bearer token was not issued by {self.issuer}") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthenticationError(f"bearer token is not valid: {exc}") from exc
        return Principal(
            subject=str(claims["sub"]),
            groups=_groups(claims, self.group_claims),
            scopes=tuple(str(claims.get("scope", "")).split()),
            token=token,
            claims=claims,
        )


def _groups(claims: dict, names: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Group membership, from the claims this deployment trusts."""
    found: list[str] = []
    for name in names or DEFAULT_GROUP_CLAIMS:
        value = claims.get(name)
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, (list, tuple)):
            found.extend(str(item) for item in value)
    # IAM group names are conventionally rooted; normalize so that a policy
    # written with a leading slash matches a claim without one and vice versa
    return tuple(dict.fromkeys(f"/{group.lstrip('/')}" for group in found))


_VERIFIER: IAMTokenVerifier | None = None
_VERIFIER_LOCK = threading.Lock()


def verifier() -> IAMTokenVerifier:
    """The process-wide verifier, built from settings on first use."""
    global _VERIFIER
    with _VERIFIER_LOCK:
        if _VERIFIER is None:
            claims = tuple(c.strip() for c in settings.iam_group_claims.split(",") if c.strip())
            _VERIFIER = IAMTokenVerifier(
                issuer=settings.iam_issuer,
                audience=settings.iam_audience or None,
                jwks_cache_s=settings.iam_jwks_cache_s,
                well_known_url=settings.iam_well_known_url or None,
                allow_any_audience=settings.iam_allow_any_audience,
                group_claims=claims or DEFAULT_GROUP_CLAIMS,
            )
        return _VERIFIER


def reset_verifier() -> None:
    """Drop the cached verifier (configuration changed; used by tests)."""
    global _VERIFIER
    with _VERIFIER_LOCK:
        _VERIFIER = None
