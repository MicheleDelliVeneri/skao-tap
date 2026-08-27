"""Getting a token for a deployed egernia, the way the other SRC demos do.

No marimo dependency, so the notebook and anything else can both import it.

The shape is the computing broker's demo (`demo/notebooks/common_lib.py`):
prefer a token from the environment, and otherwise mint one through the
Authentication API's device flow and exchange it for the audience this
service's Permissions API route policy requires.

egernia has no client package of its own to hide that in, so the exchange is
here rather than in a `<Service>IntegrationClient`. The steps are the same
ones `BrokerIntegrationClient` performs.
"""

from __future__ import annotations

import os

# The service name registered with the Permissions API — the directory under
# its etc/permissions/<env>/ — and therefore also the audience the exchanged
# token carries. `science-metadata`, without the suffix, is not registered and
# the exchange fails with a policy lookup error rather than a 401, which is a
# confusing way to find out.
EGERNIA_SERVICE = "science-metadata-api"
EGERNIA_SERVICE_VERSION = "1"

# Read in order; the first non-empty one wins. EGERNIA_TOKEN is the one to set
# by hand, the others exist so a token minted elsewhere in a session can be
# reused without renaming it.
TOKEN_ENV_KEYS = ("EGERNIA_TOKEN", "EGERNIA_BEARER_TOKEN", "EGERNIA_TEST_TOKEN")


def base_url() -> str:
    """The deployment to talk to, without a trailing slash."""
    return os.environ.get("EGERNIA_BASE_URL", "https://egernia.test").rstrip("/")


def aapi_v1_url() -> str:
    """AAPI API root including /v1, as the auth integration client expects."""
    return os.environ.get("AAPI_URL", "https://aapi.test/api").rstrip("/") + "/v1"


def test_credentials() -> tuple[str, str]:
    """The seeded IAM user the integration environment provides."""
    return (
        os.environ.get("TEST_USER", "test1"),
        os.environ.get("TEST_USER_PASSWORD", "test"),
    )


def mint_token(aapi_url: str, username: str, password: str) -> str:
    """A `science-metadata-api`-audience token via the AAPI device flow.

    Two steps, both the auth client's: the device flow yields a token whose
    audience is the Authentication API itself, and the exchange turns it into
    one this service's Permissions API policy will accept. Skipping the
    exchange gets a valid token that egernia refuses, which reads like a
    broken deployment rather than a wrong audience.
    """
    from ska_src_auth_api.client.integration import AuthenticationIntegrationClient

    with AuthenticationIntegrationClient(aapi_url, username, password) as flow:
        flow.authorize()
        access_token = flow.fetch_token()["token"]["access_token"]
        exchanged = flow.exchange_token(
            service=EGERNIA_SERVICE,
            version=EGERNIA_SERVICE_VERSION,
            access_token=access_token,
        )
    return exchanged.json()["access_token"]


def resolve_token() -> tuple[str, str]:
    """Return (token, human-readable provenance).

    The provenance is returned rather than logged because a notebook should
    say on the page where its credential came from — a demo that silently
    falls back to a stale environment token is one nobody can debug from the
    back of a room.
    """
    for key in TOKEN_ENV_KEYS:
        token = os.environ.get(key, "").strip()
        if token:
            return token, f"using `{key}` from the environment"

    username, password = test_credentials()
    url = aapi_v1_url()
    try:
        return (
            mint_token(url, username, password),
            f"minted via the AAPI device flow at `{url}` as `{username}`",
        )
    except ImportError as exc:
        raise RuntimeError(
            f"No token in {'/'.join(TOKEN_ENV_KEYS)} and ska-src-auth-api's "
            "integration client is not installed. Either set EGERNIA_TOKEN, or "
            "install it from the SKAO index (it is not on PyPI): "
            "`uv pip install --index "
            "https://artefact.skao.int/repository/pypi-internal/simple "
            "'ska-src-auth-api[integration]'`. In the deployment stack's test "
            "image it is already present, installed from the submodule."
        ) from exc
    except Exception as exc:
        raise RuntimeError(
            f"No token in {'/'.join(TOKEN_ENV_KEYS)} and the AAPI device login "
            f"at {url} failed ({type(exc).__name__}: {exc}). The seeded IAM user "
            f"is {username}; if IAM was redeployed, restart aapi so it reloads "
            "iam-client-credentials."
        ) from exc


def auth_header() -> tuple[dict[str, str], str]:
    """Ready-to-send Authorization header, and where the token came from."""
    token, provenance = resolve_token()
    return {"Authorization": f"Bearer {token}"}, provenance
