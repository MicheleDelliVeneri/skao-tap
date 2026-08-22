"""Unit tests for DALI parameter handling (case-insensitive names, GET/POST)."""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from tap_api.queries.params import gather_params, require
from tapcore.errors import UsageError

app = FastAPI()


@app.get("/echo")
@app.post("/echo")
async def echo(request: Request):
    return await gather_params(request)


client = TestClient(app)


def test_query_params_are_uppercased():
    assert client.get("/echo?lang=ADQL&Query=SELECT 1").json() == {
        "LANG": "ADQL",
        "QUERY": "SELECT 1",
    }


def test_form_params_are_uppercased():
    response = client.post("/echo", data={"lang": "ADQL", "maxrec": "5"})
    assert response.json() == {"LANG": "ADQL", "MAXREC": "5"}


def test_form_overrides_query_string():
    response = client.post("/echo?LANG=ADQL", data={"lang": "ADQL-2.0"})
    assert response.json() == {"LANG": "ADQL-2.0"}


def test_require_missing_parameter():
    with pytest.raises(UsageError):
        require({}, "QUERY")
    assert require({"QUERY": "SELECT 1"}, "QUERY") == "SELECT 1"
