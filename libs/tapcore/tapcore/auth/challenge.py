"""IVOA AuthVO challenges: telling a client how to get a token.

A bare ``401`` leaves a client guessing which IAM to talk to. SRCNet services
answer instead with an ``ivoa_bearer`` challenge naming the OIDC discovery
document, so a client that arrives with nothing can find the issuer, run a
device flow and come back with a token — without its operator having been
told the IAM URL out of band.

The same text goes in the ``WWW-Authenticate`` header and in the error body.
The header is where a standards-following client looks; the body is where the
SRCNet reference client (the DM product streamer) reads it, and matching both
means one implementation talks to either.
"""

from ..config import settings

#: the scheme IVOA AuthVO defines for this challenge
SCHEME = "ivoa_bearer"


def discovery_url() -> str:
    """The OIDC discovery document a client should start from."""
    if settings.iam_well_known_url.strip():
        return settings.iam_well_known_url.strip()
    issuer = settings.iam_issuer.strip().rstrip("/")
    return f"{issuer}/.well-known/openid-configuration" if issuer else ""


def _quote(value: str) -> str:
    """Escape a value for an auth-param quoted-string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def challenge(error: str, description: str, **params: str) -> str:
    """One ``ivoa_bearer`` challenge, ready for a WWW-Authenticate header."""
    parts = [f'error="{_quote(error)}"', f'error_description="{_quote(description)}"']
    parts += [f'{name}="{_quote(value)}"' for name, value in params.items() if value]
    return f"{SCHEME} {', '.join(parts)}"


def missing_token_challenge(description: str = "Missing access token") -> str:
    """The challenge for a request that carried no usable credential."""
    return challenge("invalid_request", description, discovery_url=discovery_url())


def invalid_token_challenge(description: str) -> str:
    """The challenge for a credential that was presented but did not verify."""
    return challenge("invalid_token", description, discovery_url=discovery_url())
