"""Every URL the service prints must be one the client that asked can fetch.

A deployment is reachable by more names than the one its operator configured:
an SSH tunnel, a port-forward, a second ingress. Printing the configured host
into a job document hands that client a URL it cannot resolve — which is how
a colleague running the demo behind a tunnel got a working sync query and then
a DNS failure on their first async job.
"""

import httpx
import pytest

pytestmark = pytest.mark.component

TRUSTED = {"Host": "egernia.test"}


def test_async_job_url_names_the_host_the_client_used(tap_service):
    """The reported bug: the Location of a new job must be fetchable."""
    response = httpx.post(
        f"{tap_service}/async",
        data={"LANG": "ADQL", "QUERY": "SELECT TOP 1 ra FROM ska.continuum_sources"},
        headers=TRUSTED,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("http://egernia.test/tap/async/")


def test_job_document_and_capabilities_agree_with_it(tap_service):
    """The Location is not the only URL a client follows: the job XML carries
    the result href, and the capabilities the accessURL every subsequent call
    is built from. One of them naming another host is the same bug again."""
    created = httpx.post(
        f"{tap_service}/async",
        data={"LANG": "ADQL", "QUERY": "SELECT TOP 1 ra FROM ska.continuum_sources"},
        headers=TRUSTED,
        follow_redirects=False,
    )
    job_id = created.headers["location"].rsplit("/", 1)[-1]

    job_xml = httpx.get(f"{tap_service}/async/{job_id}", headers=TRUSTED).text
    assert "127.0.0.1" not in job_xml

    joblist = httpx.get(f"{tap_service}/async", headers=TRUSTED).text
    assert f"http://egernia.test/tap/async/{job_id}" in joblist

    capabilities = httpx.get(f"{tap_service}/capabilities", headers=TRUSTED).text
    assert '<accessURL use="base">http://egernia.test/tap</accessURL>' in capabilities


def test_an_untrusted_host_cannot_choose_the_urls_we_print(tap_service):
    """Host is client-controlled. Echoing an unvetted one would let a caller
    decide the links this service puts in its own documents, so anything not
    configured as trusted falls back to the configured base URL rather than
    being reflected."""
    response = httpx.post(
        f"{tap_service}/async",
        data={"LANG": "ADQL", "QUERY": "SELECT TOP 1 ra FROM ska.continuum_sources"},
        headers={"Host": "evil.example"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "evil.example" not in response.headers["location"]
    assert response.headers["location"].startswith(f"{tap_service}/async/")


def test_forwarded_proto_decides_the_scheme(tap_service):
    """TLS terminates at the ingress, so this process only ever sees http; a
    client that arrived over https must not be sent back to http."""
    response = httpx.post(
        f"{tap_service}/async",
        data={"LANG": "ADQL", "QUERY": "SELECT TOP 1 ra FROM ska.continuum_sources"},
        headers={**TRUSTED, "X-Forwarded-Proto": "https"},
        follow_redirects=False,
    )
    assert response.headers["location"].startswith("https://egernia.test/tap/async/")
