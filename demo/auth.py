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
import time

# The policy egernia is authorised against in the Permissions API — the `name`
# inside etc/permissions/<env>/egernia/v1/, which is what PAPI resolves by, and
# also the audience the exchanged token carries.
#
# Not `science-metadata-api`: that policy's file says `"name":
# "science-metadata"` while its directory says otherwise, so PAPI answers "The
# permission policy for this service does not exist" for either spelling, and
# its routes are CAOM-shaped rather than egernia's.
EGERNIA_SERVICE = "egernia"
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


def test_username() -> str:
    """The seeded IAM user the integration environment provides.

    The username only, and deliberately not the password. Nothing here submits
    one any more -- without a browser there is no login form to fill in -- so
    the module has no reason to read `TEST_USER_PASSWORD`, and every reason not
    to: this prompt is printed into notebook output that gets saved, committed
    and shared. CodeQL flagged the earlier version for exactly that
    (code-scanning alert 502), and it was right rather than pedantic, because
    the value is an environment variable and a deployment that is not a dev
    cluster can put a real credential in it.

    Whoever approves the device login types their own password into IAM's own
    form, which is the only place it belongs.
    """
    return os.environ.get("TEST_USER", "test1")


def _verify() -> bool:
    """TLS verification for the *auth* calls, which is not the same question.

    Deliberately **not** `EGERNIA_INSECURE_TLS`, the switch `scaling_demo.py`
    uses for its own requests. That one exists because egernia's ingress on a
    dev cluster has no `tls:` section at all, so nginx answers 443 with its
    default self-signed certificate and httpx refuses it. AAPI and IAM are a
    different matter: they are behind the cluster's own CA, which the notebook
    trusts (`REQUESTS_CA_BUNDLE` is set), so they verify fine.

    Reading the egernia switch here would mean a bad certificate on the *data*
    endpoint silently turning off verification on the leg that carries the
    token exchange -- the one request in this module where a MITM gets
    something worth having. So this defaults to verifying, and has its own
    escape hatch for the case where AAPI itself is untrusted.
    """
    return os.environ.get("EGERNIA_AAPI_INSECURE_TLS", "0") not in ("1", "true", "yes")


def _login_timeout() -> float:
    """How long to wait for someone to approve the device code."""
    return float(os.environ.get("EGERNIA_DEVICE_LOGIN_TIMEOUT", "300"))


def request_device_code(client, aapi_url: str) -> dict:
    """Device flow step 1: ask AAPI for a code and the URL that approves it."""
    response = client.get(f"{aapi_url}/login/device")
    response.raise_for_status()
    return response.json()


def poll_for_token(client, aapi_url: str, device_code: str, deadline: float) -> str:
    """Device flow step 2: poll until the approval lands, or the code expires.

    A code nobody has approved yet answers 400, and one just approved can
    answer 500 while IAM finishes processing it, so both are "not yet" and the
    deadline is what ends the wait. Anything else is a real failure and is
    raised with its body, because "device login failed" without the status is
    the error this module used to give.
    """
    while True:
        response = client.get(f"{aapi_url}/token", params={"device_code": device_code})
        if response.status_code == 200:
            return response.json()["token"]["access_token"]
        if response.status_code not in (400, 500):
            raise RuntimeError(
                f"AAPI answered {response.status_code} polling for the device token: "
                f"{response.text[:200]}"
            )
        if time.monotonic() > deadline:
            raise TimeoutError(
                "the device code was never approved. Open the URL above and "
                "approve it, or set EGERNIA_DEVICE_LOGIN_TIMEOUT higher than "
                f"{_login_timeout():.0f}s."
            )
        time.sleep(2)


def exchange_token(client, aapi_url: str, access_token: str) -> str:
    """The audience exchange, which is not optional.

    The device flow yields a token whose audience is the Authentication API
    itself. egernia's Permissions API policy refuses it, which reads like a
    broken deployment rather than a wrong audience -- so this step is the
    difference between a demo that works and a confusing 403.
    """
    response = client.get(
        f"{aapi_url}/token/exchange/{EGERNIA_SERVICE}",
        params={"version": EGERNIA_SERVICE_VERSION, "access_token": access_token},
    )
    response.raise_for_status()
    return response.json()["access_token"]


def mint_token(aapi_url: str) -> str:
    """An `egernia`-audience token via the AAPI device flow, approved by a human.

    This used to go through `ska_src_auth_api.client.integration`, which drives
    a real Chrome through the IAM login form with Selenium so the flow needs
    nobody present. That is right for the integration suite -- which has its
    own copy of it in `tests/integration/conftest.py` -- and it cannot work
    here: the notebook image has no `fire`, no `selenium`, no `fastapi` (so not
    even the *base* client imports) and no browser of any kind, and this
    repository's own venv has neither `selenium` nor `fire` either. The import
    was unreachable in every environment the demo actually runs in.

    The device grant does not need a browser *on the client*, which is the
    whole point of it: the client shows a URL, a person opens it anywhere and
    approves, and the client polls. A demo has a person in front of it, so the
    step Selenium was automating is one the audience can watch happen -- and
    the only dependency is `httpx`, which the notebook has.
    """
    import httpx

    with httpx.Client(verify=_verify(), timeout=30, follow_redirects=True) as client:
        device = request_device_code(client, aapi_url)
        print(
            "\nEgernia needs a token. Open this and approve the request:\n\n"
            f"    {device['verification_uri_complete']}\n\n"
            f"    (code {device['user_code']}; on a dev cluster, log in as"
            f" {test_username()} -- IAM's own seed sets the password, see the"
            " indigo-iam dev overlay)\n\n"
            f"Waiting up to {_login_timeout():.0f}s ...",
            flush=True,
        )
        expires_in = float(device.get("expires_in") or _login_timeout())
        deadline = time.monotonic() + min(_login_timeout(), expires_in)
        access_token = poll_for_token(client, aapi_url, device["device_code"], deadline)
        print("Approved.", flush=True)
        return exchange_token(client, aapi_url, access_token)


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

    url = aapi_v1_url()
    try:
        return mint_token(url), f"minted via the AAPI device flow at `{url}`"
    except Exception as exc:
        raise RuntimeError(
            f"No token in {'/'.join(TOKEN_ENV_KEYS)} and the AAPI device login at "
            f"{url} failed ({type(exc).__name__}: {exc}). Set EGERNIA_TOKEN to skip "
            "the flow entirely. If AAPI itself is the problem: the seeded IAM user "
            f"is {test_username()}, and if IAM was redeployed, restart aapi so "
            "it reloads iam-client-credentials."
        ) from exc


def auth_header() -> tuple[dict[str, str], str]:
    """Ready-to-send Authorization header, and where the token came from."""
    token, provenance = resolve_token()
    return {"Authorization": f"Bearer {token}"}, provenance
