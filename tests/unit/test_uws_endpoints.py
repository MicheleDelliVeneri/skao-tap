"""Unit tests for the UWS 1.1 REST resources (tap_api.endpoints.uws_api) on the fake pool."""

import os

QUERY = "SELECT source_id, ra FROM ska.continuum_sources"


def _create_job(client, **extra):
    data = {"QUERY": QUERY, "LANG": "ADQL", **extra}
    response = client.post("/tap/async", data=data, follow_redirects=False)
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[-1]


def test_create_job_is_pending(client, fake_db):
    job_id = _create_job(client)
    assert fake_db.jobs[job_id]["phase"] == "PENDING"
    summary = client.get(f"/tap/async/{job_id}")
    assert summary.status_code == 200
    assert f"<uws:jobId>{job_id}</uws:jobId>" in summary.text
    assert "<uws:phase>PENDING</uws:phase>" in summary.text


def test_create_job_with_phase_run_queues(client, fake_db):
    job_id = _create_job(client, PHASE="RUN")
    job = fake_db.jobs[job_id]
    assert job["phase"] == "QUEUED"
    assert "ska.continuum_sources" in job["query_sql"]


def test_job_list_and_phase_filter(client, fake_db):
    _create_job(client)
    running = fake_db.add_job(phase="EXECUTING")
    listing = client.get("/tap/async")
    assert listing.status_code == 200
    assert listing.text.count("<uws:jobref") == 2
    filtered = client.get("/tap/async", params={"PHASE": "EXECUTING"})
    assert filtered.text.count("<uws:jobref") == 1
    assert running["job_id"] in filtered.text


def test_job_list_validates_parameters(client):
    assert client.get("/tap/async", params={"PHASE": "SLEEPING"}).status_code == 400
    assert client.get("/tap/async", params={"LAST": "zero"}).status_code == 400
    assert client.get("/tap/async", params={"LAST": "0"}).status_code == 400
    assert client.get("/tap/async", params={"LAST": "1"}).status_code == 200


def test_phase_endpoint_run_and_abort(client, fake_db):
    job_id = _create_job(client)
    assert client.get(f"/tap/async/{job_id}/phase").text == "PENDING"
    run = client.post(f"/tap/async/{job_id}/phase", data={"PHASE": "RUN"}, follow_redirects=False)
    assert run.status_code == 303
    assert fake_db.jobs[job_id]["phase"] == "QUEUED"
    client.post(f"/tap/async/{job_id}/phase", data={"PHASE": "ABORT"}, follow_redirects=False)
    assert fake_db.jobs[job_id]["phase"] == "ABORTED"
    # aborting a final-phase job is a no-op, unknown phases are rejected
    client.post(f"/tap/async/{job_id}/phase", data={"PHASE": "ABORT"}, follow_redirects=False)
    assert fake_db.jobs[job_id]["phase"] == "ABORTED"
    bad = client.post(f"/tap/async/{job_id}/phase", data={"PHASE": "PAUSE"})
    assert bad.status_code == 400


def test_run_rejected_unless_pending_or_held(client, fake_db):
    job = fake_db.add_job(phase="EXECUTING", parameters={"QUERY": QUERY})
    response = client.post(f"/tap/async/{job['job_id']}/phase", data={"PHASE": "RUN"})
    assert response.status_code == 400
    assert "cannot start job in phase EXECUTING" in response.text


def test_execution_duration_roundtrip(client, fake_db):
    job_id = _create_job(client)
    assert client.get(f"/tap/async/{job_id}/executionduration").text == "600"
    client.post(
        f"/tap/async/{job_id}/executionduration",
        data={"EXECUTIONDURATION": "120"},
        follow_redirects=False,
    )
    assert fake_db.jobs[job_id]["execution_duration"] == 120
    bad = client.post(f"/tap/async/{job_id}/executionduration", data={"EXECUTIONDURATION": "soon"})
    assert bad.status_code == 400
    fake_db.jobs[job_id]["phase"] = "QUEUED"
    locked = client.post(f"/tap/async/{job_id}/executionduration", data={"EXECUTIONDURATION": "10"})
    assert locked.status_code == 400


def test_destruction_roundtrip(client, fake_db):
    job_id = _create_job(client)
    assert client.get(f"/tap/async/{job_id}/destruction").text.endswith("Z")
    client.post(
        f"/tap/async/{job_id}/destruction",
        data={"DESTRUCTION": "2030-01-02T03:04:05Z"},
        follow_redirects=False,
    )
    assert fake_db.jobs[job_id]["destruction"].year == 2030
    bad = client.post(f"/tap/async/{job_id}/destruction", data={"DESTRUCTION": "tomorrow"})
    assert bad.status_code == 400


def test_quote_and_owner(client, fake_db):
    job_id = _create_job(client)
    assert client.get(f"/tap/async/{job_id}/quote").text == ""
    assert client.get(f"/tap/async/{job_id}/owner").text == ""


def test_parameters_update_only_while_pending(client, fake_db):
    job_id = _create_job(client)
    assert "<uws:parameters>" in client.get(f"/tap/async/{job_id}/parameters").text
    client.post(f"/tap/async/{job_id}/parameters", data={"MAXREC": "7"}, follow_redirects=False)
    assert fake_db.jobs[job_id]["parameters"]["MAXREC"] == "7"
    fake_db.jobs[job_id]["phase"] = "QUEUED"
    locked = client.post(f"/tap/async/{job_id}/parameters", data={"MAXREC": "9"})
    assert locked.status_code == 400


def test_results_and_error_documents(client, fake_db, results_dir):
    job = fake_db.add_job(phase="COMPLETED", parameters={"QUERY": QUERY}, result_mime="text/csv")
    job_id = job["job_id"]
    assert "<uws:results>" in client.get(f"/tap/async/{job_id}/results").text
    os.makedirs(os.path.join(results_dir, job_id))
    with open(os.path.join(results_dir, job_id, "result.csv"), "wb") as fh:
        fh.write(b"source_id,ra\n1,62.1\n")
    result = client.get(f"/tap/async/{job_id}/results/result")
    assert result.status_code == 200
    assert result.headers["content-type"].startswith("text/csv")
    assert result.content.startswith(b"source_id,ra")
    error = client.get(f"/tap/async/{job_id}/error")
    assert "no error" in error.text


def test_result_missing_cases(client, fake_db):
    pending = fake_db.add_job(phase="PENDING")
    assert client.get(f"/tap/async/{pending['job_id']}/results/result").status_code == 404
    done = fake_db.add_job(phase="COMPLETED")
    assert client.get(f"/tap/async/{done['job_id']}/results/result").status_code == 404


def test_error_document_for_failed_job(client, fake_db):
    job = fake_db.add_job(phase="ERROR", error_type="fatal", error_message="query exploded")
    error = client.get(f"/tap/async/{job['job_id']}/error")
    assert "query exploded" in error.text
    summary = client.get(f"/tap/async/{job['job_id']}")
    assert 'type="fatal"' in summary.text
    assert "query exploded" in summary.text


def test_delete_job_via_action_and_method(client, fake_db):
    first = _create_job(client)
    response = client.post(f"/tap/async/{first}", data={"ACTION": "DELETE"}, follow_redirects=False)
    assert response.status_code == 303
    assert first not in fake_db.jobs
    second = _create_job(client)
    deleted = client.delete(f"/tap/async/{second}", follow_redirects=False)
    assert deleted.status_code == 303
    assert second not in fake_db.jobs
    bad = client.post(f"/tap/async/{_create_job(client)}", data={"ACTION": "PAUSE"})
    assert bad.status_code == 400


def test_unknown_job_is_404(client):
    assert client.get("/tap/async/0123456789abcdef").status_code == 404
