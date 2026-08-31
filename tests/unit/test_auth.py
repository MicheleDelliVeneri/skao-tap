"""Unit tests for token verification (egernia_core.auth) and the shipped
authorisation plugins, exercised with real RSA-signed JWTs against a stub
IAM: the point of this layer is that a forged or stale token is rejected, so
the signature path must be real rather than mocked out."""

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import httpx
import jwt
import pytest
from egernia_core.auth import Principal
from egernia_core.auth.tokens import IAMTokenVerifier
from egernia_core.errors import AuthenticationError, AuthorizationError, ServiceError


@pytest.fixture
def iam_verifier(stub_iam, iam_issuer, iam_audience):
    return IAMTokenVerifier(issuer=iam_issuer, audience=iam_audience, jwks_cache_s=300)


# -- token verification -----------------------------------------------------


def test_valid_token_yields_a_principal(iam_verifier, make_token):
    who = iam_verifier.verify(make_token())
    assert who.subject == "user-1"
    assert who.groups == ("/ska/science-metadata/oper",)
    assert "science-metadata:write" in who.scopes
    assert not who.is_anonymous


def test_token_signed_by_another_key_is_rejected(iam_verifier, forged_keypair, make_token):
    """The whole point: a well-formed token the IAM never signed is not valid."""
    forged = forged_keypair[0]
    with pytest.raises(AuthenticationError):
        iam_verifier.verify(make_token(forged))


def test_unknown_kid_refresh_is_coalesced_and_negatively_cached(
    iam_verifier, stub_iam, forged_keypair, make_token
):
    iam_verifier.verify(make_token())  # warm the current JWKS
    stub_iam["jwks_calls"] = 0
    unknown = make_token(forged_keypair[0], kid="missing")

    def rejected(_):
        with pytest.raises(AuthenticationError):
            iam_verifier.verify(unknown)
        return True

    with ThreadPoolExecutor(max_workers=16) as pool:
        assert all(pool.map(rejected, range(32)))

    assert stub_iam["jwks_calls"] == 1
    with pytest.raises(AuthenticationError):
        iam_verifier.verify(unknown)
    refreshed_at = iam_verifier._jwks_refreshed_at
    with pytest.raises(AuthenticationError):
        iam_verifier.verify(make_token(forged_keypair[0], kid="another-missing"))
    assert stub_iam["jwks_calls"] == 1
    assert iam_verifier._jwks_refreshed_at == refreshed_at


def test_new_signing_key_is_found_after_one_refresh(
    iam_verifier, stub_iam, forged_keypair, make_token
):
    iam_verifier.verify(make_token())
    stub_iam["jwks_calls"] = 0
    private, jwk = forged_keypair
    stub_iam["keys"].append(jwk | {"kid": "k2"})

    assert iam_verifier.verify(make_token(private, kid="k2")).subject == "user-1"
    assert stub_iam["jwks_calls"] == 1


def test_expired_token_is_rejected(iam_verifier, make_token):
    with pytest.raises(AuthenticationError, match="expired"):
        iam_verifier.verify(make_token(exp=int(time.time()) - 60))


def test_token_for_another_audience_is_rejected(iam_verifier, make_token):
    with pytest.raises(AuthenticationError, match="audience"):
        iam_verifier.verify(make_token(aud="some-other-service"))


def test_token_from_another_issuer_is_rejected(iam_verifier, make_token):
    with pytest.raises(AuthenticationError):
        iam_verifier.verify(make_token(iss="https://evil.example.org"))


def test_a_trailing_slash_on_the_issuer_is_forgiven(iam_verifier, make_token):
    """An IAM that advertises `https://iam.test/` mints tokens carrying it.

    The configured issuer has its trailing slash stripped, and so does the
    discovery document's, so those two agree — but PyJWT compares the `iss`
    claim verbatim, so handing it the stripped form rejected every token from a
    correctly configured deployment. The error named two strings that looked
    identical ("bearer token was not issued by https://iam.test"), which is a
    slow way to find one character.

    Both directions, because a deployment may configure either spelling.
    """
    with_slash = make_token(iss="https://iam.example.org/")
    without_slash = make_token(iss="https://iam.example.org")
    assert iam_verifier.verify(with_slash).subject == "user-1"
    assert iam_verifier.verify(without_slash).subject == "user-1"


def test_only_the_trailing_slash_is_forgiven(iam_verifier, make_token):
    """The normalisation must not become a prefix match.

    A host that merely starts with the configured issuer, or adds a path, is a
    different issuer and has to stay refused — otherwise forgiving a slash
    becomes a way in.
    """
    for hostile in (
        "https://iam.example.org.evil.test",
        "https://iam.example.org/../other",
        "https://iam.example.org/tenant2",
        "http://iam.example.org",
    ):
        with pytest.raises(AuthenticationError):
            iam_verifier.verify(make_token(iss=hostile))


def test_unsigned_token_is_rejected(iam_verifier):
    none_token = jwt.encode(
        {"sub": "x", "iss": "https://iam.example.org"},
        key=cast(Any, None),
        algorithm="none",
    )
    with pytest.raises(AuthenticationError):
        iam_verifier.verify(none_token)


def test_garbage_is_rejected(iam_verifier):
    with pytest.raises(AuthenticationError):
        iam_verifier.verify("not-a-jwt")


def test_discovery_document_naming_another_issuer_is_refused(
    stub_iam, make_token, iam_issuer, iam_audience
):
    """A mistyped well-known URL must not silently move trust to another IAM."""
    stub_iam["issuer"] = "https://someone-else.example.org"
    verifier = IAMTokenVerifier(issuer=iam_issuer, audience=iam_audience)
    with pytest.raises(ServiceError, match="declares issuer"):
        verifier.verify(make_token())


def test_discovery_is_cached(iam_verifier, make_token):
    for _ in range(3):
        iam_verifier.verify(make_token())
    assert iam_verifier._discovered_at > 0


def test_verifier_refuses_to_run_without_an_audience(iam_issuer):
    """One IAM serves many services: no audience check means cross-service replay."""
    with pytest.raises(ServiceError, match="audience is required"):
        IAMTokenVerifier(issuer=iam_issuer, audience=None)


def test_any_audience_is_possible_but_must_be_explicit(stub_iam, make_token, iam_issuer):
    verifier = IAMTokenVerifier(issuer=iam_issuer, audience=None, allow_any_audience=True)
    assert verifier.verify(make_token(aud="some-other-service")).subject == "user-1"


def test_entitlement_claims_are_not_groups_by_default(
    stub_iam, make_token, iam_issuer, iam_audience
):
    """A federated home IdP can assert entitlements; they must not be policy groups."""
    verifier = IAMTokenVerifier(issuer=iam_issuer, audience=iam_audience)
    token = make_token(groups=None, entitlements=["/ska/science-metadata/admin"])
    assert verifier.verify(token).groups == ()


def test_entitlement_claims_can_be_opted_into(stub_iam, make_token, iam_issuer, iam_audience):
    verifier = IAMTokenVerifier(
        issuer=iam_issuer,
        audience=iam_audience,
        group_claims=("groups", "entitlements"),
    )
    token = make_token(groups=None, entitlements=["/ska/x"])
    assert verifier.verify(token).groups == ("/ska/x",)


def test_group_claims_are_normalised(stub_iam, make_token, iam_issuer, iam_audience):
    verifier = IAMTokenVerifier(issuer=iam_issuer, audience=iam_audience)
    token = make_token(groups=None, **{"wlcg.groups": ["ska/oper", "/ska/user"]})
    assert verifier.verify(token).groups == ("/ska/oper", "/ska/user")


# -- iam-groups plugin ------------------------------------------------------


def _principal(groups=(), scopes=(), subject="user-1"):
    return Principal(subject=subject, groups=tuple(groups), scopes=tuple(scopes), token="t")


def test_iam_groups_allows_a_configured_group():
    from egernia_api.auth_plugins.iam_groups import IAMGroupsPlugin

    plugin = IAMGroupsPlugin(roles={"metadata.delete": {"groups": ["/ska/oper"]}})
    assert plugin.authorize(_principal(groups=["/ska/oper"]), "metadata.delete", {})
    assert not plugin.authorize(_principal(groups=["/ska/user"]), "metadata.delete", {})


def test_iam_groups_allows_a_configured_scope():
    from egernia_api.auth_plugins.iam_groups import IAMGroupsPlugin

    plugin = IAMGroupsPlugin(roles={"metadata.ingest": {"scopes": ["sm:write"]}})
    assert plugin.authorize(_principal(scopes=["sm:write"]), "metadata.ingest", {})
    assert not plugin.authorize(_principal(scopes=["sm:read"]), "metadata.ingest", {})


def test_iam_groups_denies_unconfigured_operations():
    """An operation nobody configured must not be open by omission."""
    from egernia_api.auth_plugins.iam_groups import IAMGroupsPlugin

    plugin = IAMGroupsPlugin(roles={"metadata.ingest": {"groups": ["/ska/oper"]}})
    assert not plugin.authorize(_principal(groups=["/ska/oper"]), "metadata.delete", {})


def test_iam_groups_empty_rule_grants_nothing():
    """An unfinished policy entry must not read as "allow everyone".

    Regression for the shipped chart default, which listed every operation
    with empty groups and scopes: an empty rule used to mean "any verified
    token", so enabling auth without writing a policy left ingest, amend and
    delete open to any account at the IAM.
    """
    from egernia_api.auth_plugins.iam_groups import IAMGroupsPlugin

    plugin = IAMGroupsPlugin(roles={"metadata.amend": {}, "metadata.delete": {"groups": []}})
    assert not plugin.authorize(_principal(groups=["/ska/oper"]), "metadata.amend", {})
    assert not plugin.authorize(_principal(groups=["/ska/oper"]), "metadata.delete", {})


def test_iam_groups_any_verified_token_must_be_explicit():
    from egernia_api.auth_plugins.iam_groups import IAMGroupsPlugin

    plugin = IAMGroupsPlugin(roles={"metadata.amend": {"any_verified_token": True}})
    assert plugin.authorize(_principal(), "metadata.amend", {})
    assert not plugin.authorize(Principal(), "metadata.amend", {})


def test_iam_groups_describe_names_what_each_operation_grants():
    """The startup line must not read as reassuring for a rule granting nothing."""
    from egernia_api.auth_plugins.iam_groups import IAMGroupsPlugin

    described = IAMGroupsPlugin(
        roles={
            "metadata.ingest": {"groups": ["/ska/oper"]},
            "metadata.amend": {"any_verified_token": True},
            "metadata.delete": {},
        }
    ).describe()
    assert "metadata.ingest=/ska/oper" in described
    assert "metadata.amend=ANY verified token" in described
    assert "metadata.delete=nobody" in described


def test_iam_groups_never_authorizes_anonymous():
    from egernia_api.auth_plugins.iam_groups import IAMGroupsPlugin

    plugin = IAMGroupsPlugin(roles={"metadata.amend": {}})
    assert not plugin.authorize(Principal(), "metadata.amend", {})


def test_iam_groups_rejects_unknown_operations_and_bad_json():
    from egernia_api.auth_plugins.iam_groups import IAMGroupsPlugin

    with pytest.raises(ServiceError, match="unknown operation"):
        IAMGroupsPlugin(roles={"metadata.nope": {}})
    with pytest.raises(ServiceError, match="not valid JSON"):
        IAMGroupsPlugin(roles=cast(Any, "{oops"))


# -- permissions-api plugin -------------------------------------------------


@pytest.fixture
def permissions_calls(monkeypatch):
    calls = []
    reply = {"status": 200, "json": {"is_authorised": True}}

    def fake_post(url, params=None, json=None, timeout=None):
        calls.append({"url": url, "params": params, "body": json})
        if isinstance(reply.get("exc"), Exception):
            raise reply["exc"]
        return httpx.Response(reply["status"], json=reply.get("json"))

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls, reply


def test_permissions_api_sends_the_documented_contract(permissions_calls):
    from egernia_api.auth_plugins.permissions_api import PermissionsApiPlugin

    calls, _ = permissions_calls
    plugin = PermissionsApiPlugin(url="https://papi.example/api/v1", service="science-metadata")
    context = {
        "method": "DELETE",
        "route": "/api/v1/software/{root_id}",
        "path_params": {"root_id": "ska:demo:1"},
    }
    assert plugin.authorize(_principal(), "metadata.delete", context)
    (call,) = calls
    assert call["url"] == "https://papi.example/api/v1/authorise/route/science-metadata"
    assert call["params"]["route"] == "/api/v1/software/{root_id}"
    assert call["params"]["method"] == "DELETE"
    assert call["params"]["token"] == "t"
    assert call["body"] == {"root_id": "ska:demo:1"}


def test_permissions_api_denial_is_a_denial(permissions_calls):
    from egernia_api.auth_plugins.permissions_api import PermissionsApiPlugin

    _, reply = permissions_calls
    reply["json"] = {"is_authorised": False}
    plugin = PermissionsApiPlugin(url="https://papi.example/api/v1")
    assert not plugin.authorize(_principal(), "metadata.delete", {})


def test_permissions_api_outage_is_not_a_silent_allow(permissions_calls):
    from egernia_api.auth_plugins.permissions_api import PermissionsApiPlugin

    _, reply = permissions_calls
    reply["exc"] = httpx.ConnectError("boom")
    plugin = PermissionsApiPlugin(url="https://papi.example/api/v1")
    with pytest.raises(ServiceError, match="unreachable"):
        plugin.authorize(_principal(), "metadata.delete", {})


def test_permissions_api_requires_a_url(monkeypatch):
    from egernia_api.auth_plugins.permissions_api import PermissionsApiPlugin

    with pytest.raises(ServiceError, match="TAP_PERMISSIONS_API_URL"):
        PermissionsApiPlugin(url="")


def test_permissions_api_never_calls_out_for_anonymous(permissions_calls):
    from egernia_api.auth_plugins.permissions_api import PermissionsApiPlugin

    calls, _ = permissions_calls
    plugin = PermissionsApiPlugin(url="https://papi.example/api/v1")
    assert not plugin.authorize(Principal(), "metadata.delete", {})
    assert calls == []


# -- plugin selection -------------------------------------------------------


def test_unknown_plugin_selection_is_reported(monkeypatch, auth_settings):
    from egernia_core.auth import active_auth_plugin

    auth_settings(auth_enabled=True, auth_plugin="nope")
    with pytest.raises(LookupError, match="unknown auth plugin"):
        active_auth_plugin()


def test_no_plugin_when_disabled(auth_settings):
    from egernia_core.auth import active_auth_plugin

    auth_settings(auth_enabled=False)
    assert active_auth_plugin() is None


def test_both_shipped_plugins_are_discoverable():
    from egernia_core.auth import discovered_auth_plugins

    assert {"iam-groups", "permissions-api"} <= set(discovered_auth_plugins())


def test_authorization_error_is_403_and_authentication_is_401():
    assert AuthenticationError("x").http_status == 401
    assert AuthorizationError("x").http_status == 403


def test_missing_issuer_is_a_service_error_not_a_bare_valueerror():
    """Configuration faults must render through the service's error handler."""
    from egernia_core.errors import TAPError

    with pytest.raises(ServiceError, match="TAP_IAM_ISSUER"):
        IAMTokenVerifier(issuer="", audience="x")
    assert issubclass(ServiceError, TAPError)


def test_roles_reject_a_string_where_a_list_belongs():
    """A bare string would iterate into characters and grant nothing, quietly."""
    from egernia_api.auth_plugins.iam_groups import IAMGroupsPlugin

    with pytest.raises(ServiceError, match="must be a list"):
        IAMGroupsPlugin(roles={"metadata.ingest": {"groups": "/ska/oper"}})
    with pytest.raises(ServiceError, match="must be a list"):
        IAMGroupsPlugin(roles={"metadata.ingest": {"scopes": "sm:write"}})


def test_a_failed_plugin_resolve_is_not_cached_as_auth_off(auth_settings):
    """A misconfiguration must keep raising, never degrade into an open service."""
    from egernia_api import auth as api_auth

    auth_settings(auth_enabled=True, auth_plugin="nope")
    for _ in range(2):
        with pytest.raises(LookupError):
            api_auth.plugin()


class _DeletingConn:
    def execute(self, statement, params):
        return type("Result", (), {"rowcount": 1})()


def test_deletion_audit_names_the_subject(caplog):
    """A cascading deletion must be traceable to a person, not just a time."""
    import logging

    from egernia_api.plugins.software import PLUGIN
    from egernia_core.metadata import ingest

    with caplog.at_level(logging.INFO, logger="egernia_core"):
        ingest.delete_document(_DeletingConn(), PLUGIN, "ska:demo:1.0.0", actor="alice")
    assert "by 'alice'" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="egernia_core"):
        ingest.delete_document(_DeletingConn(), PLUGIN, "ska:demo:1.0.0")
    assert "by an unauthenticated caller" in caplog.text


def test_deletion_audit_subject_cannot_forge_records(caplog):
    import logging

    from egernia_api.plugins.software import PLUGIN
    from egernia_core.metadata import ingest

    forged = "x\nINFO:egernia_core:deleted everything"
    with caplog.at_level(logging.INFO, logger="egernia_core"):
        ingest.delete_document(_DeletingConn(), PLUGIN, "ska:demo:1", actor=forged)
    assert len(caplog.records) == 1
    assert "\n" not in caplog.records[0].getMessage()
