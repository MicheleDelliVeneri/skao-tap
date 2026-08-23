"""The VOResource record served at /tap/registry.

The record is generated rather than kept as a file, so what these tests care
about is that it describes *this* service: the same capabilities /capabilities
advertises, the configured identifier, and nothing at all until a deployment
has said who it is.
"""

import xml.etree.ElementTree as ET

import pytest
from tap_api.endpoints import vosi
from tapcore.errors import ServiceError

COMPLETE = {
    "registry_enabled": True,
    "registry_identifier": "ivo://skao.int/srcnet/tap",
    "registry_title": "SKAO SRCNet TAP service",
    "registry_short_name": "SKAO TAP",
    "registry_description": "Science metadata and data products, queryable with ADQL.",
    "registry_reference_url": "https://srcnet.example.org/tap",
    "registry_publisher": "SKA Observatory",
    "registry_creator": "SKA Observatory",
    "registry_contact_name": "SRCNet operations",
    "registry_contact_email": "srcnet-support@example.org",
    "registry_subjects": "radio astronomy, surveys",
    "registry_created": "2026-08-23",
}


@pytest.fixture
def published(auth_settings):
    """auth_settings is the generic frozen-Settings override helper."""

    def apply(**overrides):
        auth_settings(**{**COMPLETE, **overrides})

    return apply


def _text(root, path):
    found = root.find(path)
    return None if found is None else found.text


def test_no_record_until_a_deployment_says_who_it_is(client):
    response = client.get("/tap/registry")
    assert response.status_code == 404
    # not the DALI VOTable the TAP endpoints answer with: a harvester asking
    # for a record would read that as a malformed one rather than a missing one
    assert not response.headers["content-type"].startswith("application/x-votable")
    assert "voRegistry.enabled" in response.text


def test_an_incomplete_record_is_a_plain_error_too(client, auth_settings):
    auth_settings(registry_enabled=True)  # nothing else configured
    response = client.get("/tap/registry")
    assert response.status_code == 500
    assert not response.headers["content-type"].startswith("application/x-votable")
    assert "voRegistry.title" in response.text


def test_the_record_carries_the_configured_identity(published):
    published()
    root = ET.fromstring(vosi.voresource_xml())
    assert _text(root, "identifier") == "ivo://skao.int/srcnet/tap"
    assert _text(root, "title") == "SKAO SRCNet TAP service"
    assert _text(root, "shortName") == "SKAO TAP"
    assert _text(root, "curation/publisher") == "SKA Observatory"
    assert _text(root, "curation/contact/email") == "srcnet-support@example.org"
    assert [s.text for s in root.findall("content/subject")] == ["radio astronomy", "surveys"]
    assert root.get("created") == "2026-08-23"
    assert root.get("updated") == "2026-08-23"  # defaults to created
    assert root.get("status") == "active"
    assert root.get(f"{{{'http://www.w3.org/2001/XMLSchema-instance'}}}type") == "vs:CatalogService"


def test_the_record_advertises_the_same_capabilities_as_vosi(published):
    """A record that disagreed with /capabilities would be worse than none."""
    published()
    record = ET.fromstring(vosi.voresource_xml())
    capabilities = ET.fromstring(vosi.capabilities_xml())
    in_record = [c.get("standardID") for c in record.findall("capability")]
    in_vosi = [c.get("standardID") for c in capabilities.findall("capability")]
    assert in_record == in_vosi
    assert "ivo://ivoa.net/std/TAP" in in_record


def test_the_endpoint_serves_the_record(client, published):
    published()
    response = client.get("/tap/registry")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    ET.fromstring(response.text)


def test_updated_can_differ_from_created(published):
    published(registry_updated="2026-09-01")
    root = ET.fromstring(vosi.voresource_xml())
    assert root.get("created") == "2026-08-23"
    assert root.get("updated") == "2026-09-01"


def test_the_optional_curation_fields_are_omitted_when_unset(published):
    published(registry_creator="", registry_contact_name="", registry_contact_email="")
    root = ET.fromstring(vosi.voresource_xml())
    assert root.find("curation/creator") is None
    assert root.find("curation/contact") is None
    assert _text(root, "curation/publisher") == "SKA Observatory"


@pytest.mark.parametrize(
    ("unset", "named"),
    [
        ("registry_identifier", "authorityId"),
        ("registry_title", "title"),
        ("registry_short_name", "shortName"),
        ("registry_description", "description"),
        ("registry_reference_url", "referenceUrl"),
        ("registry_publisher", "publisher"),
        ("registry_created", "created"),
    ],
)
def test_an_incomplete_record_names_the_helm_value_to_set(published, unset, named):
    """Better here than days later in someone else's ingest log — and named as
    the value an operator would go and edit, not as the internal setting."""
    published(**{unset: ""})
    with pytest.raises(ServiceError, match=f"voRegistry.{named}"):
        vosi.voresource_xml()


def test_a_record_with_no_subject_is_refused(published):
    published(registry_subjects="  , ")
    with pytest.raises(ServiceError, match="subjects"):
        vosi.voresource_xml()


def test_an_identifier_that_is_not_an_ivoa_uri_is_refused(published):
    published(registry_identifier="https://example.org/tap")
    with pytest.raises(ServiceError, match="ivo://"):
        vosi.voresource_xml()


def test_a_short_name_over_the_voresource_limit_is_refused(published):
    published(registry_short_name="a-very-long-short-name")
    with pytest.raises(ServiceError, match="16 characters"):
        vosi.voresource_xml()


def test_xml_special_characters_are_escaped(published):
    published(registry_title="Ampersands & <angles>")
    document = vosi.voresource_xml()
    root = ET.fromstring(document)  # would raise if the raw & reached the output
    assert "&amp;" in document
    assert _text(root, "title") == "Ampersands & <angles>"
