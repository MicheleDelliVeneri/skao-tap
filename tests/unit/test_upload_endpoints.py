"""Unit tests for UPLOAD over the HTTP endpoints and the executor."""

import io
import os
from dataclasses import replace

import pytest
from egernia_core.errors import UsageError

from tests.unit.test_upload import VOTABLE


def _settings(monkeypatch, **changes):
    from egernia_api.queries import params, uploads

    changed = replace(uploads.settings, **changes)
    monkeypatch.setattr(uploads, "settings", changed)
    monkeypatch.setattr(params, "settings", changed)


JOIN_QUERY = (
    "SELECT a.source_id, u.ra FROM ska.continuum_sources AS a"
    " JOIN TAP_UPLOAD.t1 AS u ON a.source_id = u.id"
)


def _multipart(query=JOIN_QUERY, upload="t1,param:t1", part="t1"):
    return {
        "data": {"QUERY": query, "LANG": "ADQL", "UPLOAD": upload},
        "files": {part: ("t1.vot", io.BytesIO(VOTABLE), "application/x-votable+xml")},
    }


def test_sync_inline_upload(client, fake_db):
    response = client.post("/tap/sync", **_multipart())
    assert response.status_code == 200
    assert "<TR><TD>1</TD><TD>62.1</TD></TR>" in response.text
    creates = [s for s in fake_db.statements if s.startswith("CREATE TEMP TABLE")]
    assert creates == [
        "CREATE TEMP TABLE tap_upload_t1 (id bigint, ra double precision, ok boolean,"
        " seen timestamp, label text) ON COMMIT DROP"
    ]
    assert any(s.startswith("GRANT SELECT ON pg_temp.tap_upload_t1") for s in fake_db.statements)
    executed = [s for s in fake_db.statements if s.startswith("SELECT * FROM (")]
    assert executed and "pg_temp.tap_upload_t1" in executed[-1]
    assert "TAP_UPLOAD" not in executed[-1]


def test_sync_upload_missing_inline_part(client):
    body = _multipart(part="other")
    response = client.post("/tap/sync", **body)
    assert response.status_code == 400
    assert "missing inline part" in response.text


def test_sync_upload_unsupported_scheme(client):
    response = client.post("/tap/sync", **_multipart(upload="t1,ftp://ex.org/t.vot"))
    assert response.status_code == 400
    assert "unsupported uri scheme" in response.text


def test_sync_query_referencing_missing_upload(client):
    response = client.post(
        "/tap/sync",
        data={"QUERY": "SELECT u.id FROM TAP_UPLOAD.other AS u", "LANG": "ADQL"},
    )
    assert response.status_code == 400
    assert "was not uploaded" in response.text


def test_repeated_upload_parameters_accumulate(client, fake_db):
    query = "SELECT u.id, v.id FROM TAP_UPLOAD.t1 AS u JOIN TAP_UPLOAD.t2 AS v ON u.id = v.id"
    response = client.post(
        "/tap/sync?UPLOAD=t1,param:t1",
        data={"QUERY": query, "LANG": "ADQL", "UPLOAD": "t2,param:t2"},
        files={
            "t1": ("t1.vot", io.BytesIO(VOTABLE), "application/x-votable+xml"),
            "t2": ("t2.vot", io.BytesIO(VOTABLE), "application/x-votable+xml"),
        },
    )
    assert response.status_code == 200
    creates = [s for s in fake_db.statements if s.startswith("CREATE TEMP TABLE")]
    assert len(creates) == 2


def test_http_uri_upload_fetch(client, fake_db, monkeypatch):
    from egernia_api.queries import uploads

    monkeypatch.setattr(uploads, "_fetch", lambda uri: VOTABLE)
    response = client.post(
        "/tap/sync",
        data={"QUERY": JOIN_QUERY, "LANG": "ADQL", "UPLOAD": "t1,https://ex.org/t.vot"},
    )
    assert response.status_code == 200


def test_remote_upload_host_is_denied_by_default(monkeypatch):
    from egernia_api.queries import uploads

    _settings(monkeypatch, upload_allowed_hosts="")
    with pytest.raises(UsageError, match="not allowed"):
        uploads._fetch("https://ex.org/table.vot")


def test_remote_upload_rejects_credentials(monkeypatch):
    from egernia_api.queries import uploads

    _settings(monkeypatch, upload_allowed_hosts="ex.org")
    with pytest.raises(UsageError, match="not an allowed"):
        uploads._fetch("https://user:password@ex.org/table.vot")


def test_fetch_size_cap(monkeypatch):
    import urllib.request

    from egernia_api.queries import uploads
    from egernia_core.config import settings

    class Huge:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n):
            return b"x" * n

    class FakeOpener:
        def open(self, request, timeout):
            return Huge()

    _settings(monkeypatch, upload_allowed_hosts="ex.org")
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: FakeOpener())
    with pytest.raises(UsageError, match="byte limit"):
        uploads._fetch("https://ex.org/huge.vot")
    assert settings.upload_max_bytes > 0


def test_redirect_target_must_be_allowed(monkeypatch):
    from egernia_api.queries import uploads

    _settings(monkeypatch, upload_allowed_hosts="ex.org")
    with pytest.raises(UsageError, match="not allowed"):
        uploads._validate_remote_uri("http://127.0.0.1/internal")


def test_upload_source_count_is_bounded(monkeypatch):
    from egernia_api.queries import uploads

    _settings(monkeypatch, upload_max_sources=1)
    with pytest.raises(UsageError, match="source limit"):
        uploads.resolve_upload_sources("t1,param:a;t2,param:b", {"a": b"", "b": b""})


def test_multipart_parser_rejects_too_many_files(client, monkeypatch):
    _settings(monkeypatch, upload_max_sources=1)
    response = client.post(
        "/tap/sync",
        data={"QUERY": JOIN_QUERY, "LANG": "ADQL", "UPLOAD": "t1,param:a"},
        files={"a": ("a.vot", VOTABLE), "b": ("b.vot", VOTABLE)},
    )
    assert response.status_code == 400
    assert "Too many files" in response.text


def test_upload_aggregate_bytes_are_bounded(monkeypatch):
    from egernia_api.queries import uploads

    _settings(monkeypatch, upload_max_total_bytes=3)
    with pytest.raises(UsageError, match="aggregate byte limit"):
        uploads.resolve_upload_sources("t1,param:a;t2,param:b", {"a": b"aa", "b": b"bb"})


def test_inline_upload_is_read_in_bounded_chunks(client, monkeypatch):
    from egernia_api.queries import uploads

    monkeypatch.setattr(uploads, "READ_CHUNK_BYTES", 7)
    response = client.post("/tap/sync", **_multipart())
    assert response.status_code == 200


def test_async_upload_persisted_and_executed(client, fake_db, results_dir):
    response = client.post("/tap/async", follow_redirects=False, **_multipart())
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[-1]
    saved = os.path.join(results_dir, job_id, "uploads", "t1.vot")
    with open(saved, "rb") as fh:
        assert fh.read() == VOTABLE

    client.post(f"/tap/async/{job_id}/phase", data={"PHASE": "RUN"}, follow_redirects=False)
    assert fake_db.jobs[job_id]["phase"] == "QUEUED"
    assert "TAP_UPLOAD.t1" in fake_db.jobs[job_id]["query_sql"]

    from egernia_executor import worker

    job = worker.claim_job()
    assert job is not None
    worker.execute_job(job)
    assert fake_db.jobs[job_id]["phase"] == "COMPLETED"
    assert os.path.isfile(os.path.join(results_dir, job_id, "result.vot"))
    creates = [s for s in fake_db.statements if s.startswith("CREATE TEMP TABLE tap_upload_t1")]
    assert creates
    # the executed statement leads with the job tag, which is what the abort
    # path uses to recognise it in pg_stat_activity
    executed = [
        s
        for s in fake_db.statements
        if s.startswith(f"/* tap_job_{job_id} */") and "SELECT * FROM (" in s
    ]
    assert "pg_temp.tap_upload_t1" in executed[-1]


def test_async_malformed_upload_rejected_before_job_creation(client, fake_db):
    response = client.post(
        "/tap/async",
        data={"QUERY": JOIN_QUERY, "LANG": "ADQL", "UPLOAD": "t1,param:t1"},
        files={"t1": ("t1.vot", io.BytesIO(b"<root/>"), "application/xml")},
    )
    assert response.status_code == 400
    assert fake_db.jobs == {}


def test_executor_missing_upload_file_marks_job_error(fake_db, results_dir):
    from egernia_executor import worker

    fake_db.add_job(
        phase="QUEUED",
        parameters={"QUERY": JOIN_QUERY, "UPLOAD": "t1,param:t1"},
        query_sql="SELECT u.id FROM TAP_UPLOAD.t1 AS u",
    )
    claimed = worker.claim_job()
    assert claimed is not None
    worker.execute_job(claimed)
    (job,) = fake_db.jobs.values()
    assert job["phase"] == "ERROR"
    assert "missing" in job["error_message"]


def test_capabilities_only_declare_enabled_upload_methods(client):
    text = client.get("/tap/capabilities").text
    assert "ivo://ivoa.net/std/TAPRegExt#upload-inline" in text
    assert "ivo://ivoa.net/std/TAPRegExt#upload-http" not in text
    assert "<uploadLimit>" in text


def test_capabilities_declare_allowed_remote_upload(client, monkeypatch):
    from egernia_api.endpoints import vosi

    monkeypatch.setattr(
        vosi, "settings", replace(vosi.settings, upload_allowed_hosts="data.example.org")
    )
    text = client.get("/tap/capabilities").text
    assert "ivo://ivoa.net/std/TAPRegExt#upload-http" in text
    assert "ivo://ivoa.net/std/TAPRegExt#upload-https" in text


def test_parameter_update_persists_new_upload_sources(client, fake_db, results_dir):
    """POST /{job_id}/parameters with an UPLOAD must save the files it names,
    or the stored parameters would reference uploads the executor cannot
    load when the job runs."""
    from egernia_core.query.upload import uploads_dir

    job = fake_db.add_job(phase="PENDING", parameters={"QUERY": JOIN_QUERY})
    response = client.post(
        f"/tap/async/{job['job_id']}/parameters", follow_redirects=False, **_multipart()
    )
    assert response.status_code == 303
    assert fake_db.jobs[job["job_id"]]["parameters"]["UPLOAD"] == "t1,param:t1"
    assert os.path.exists(os.path.join(uploads_dir(job["job_id"]), "t1.vot"))


def test_parameter_update_rejects_unresolvable_upload(client, fake_db):
    """An UPLOAD naming a part the request did not carry is the client's
    mistake: a 400 now, never an ERROR job later — and the job's stored
    parameters stay untouched."""
    job = fake_db.add_job(phase="PENDING", parameters={"QUERY": JOIN_QUERY})
    response = client.post(f"/tap/async/{job['job_id']}/parameters", **_multipart(part="other"))
    assert response.status_code == 400
    assert "missing inline part" in response.text
    assert "UPLOAD" not in fake_db.jobs[job["job_id"]]["parameters"]
