"""Resolution of UPLOAD sources: inline multipart parts and http(s) URIs."""

import urllib.error
import urllib.request

from egernia_core.config import settings
from egernia_core.errors import UsageError
from egernia_core.query.upload import UploadedTable, parse_upload_param, parse_votable
from fastapi import Request
from starlette.datastructures import UploadFile

FETCH_TIMEOUT_S = 15


async def gather_upload_files(request: Request) -> dict[str, bytes]:
    """The multipart file parts of a POST, keyed by field name (the target
    of ``param:`` UPLOAD references)."""
    content_type = request.headers.get("content-type", "")
    if request.method != "POST" or "multipart/form-data" not in content_type:
        return {}
    files: dict[str, bytes] = {}
    form = await request.form()
    for key, value in form.multi_items():
        if isinstance(value, UploadFile):
            data = await value.read()
            if len(data) > settings.upload_max_bytes:
                raise UsageError(
                    f"inline upload {key} exceeds the {settings.upload_max_bytes} byte limit"
                )
            files[key] = data
    return files


def _fetch(uri: str) -> bytes:
    request = urllib.request.Request(uri, headers={"User-Agent": "egernia"})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S) as response:
            data = response.read(settings.upload_max_bytes + 1)
    except urllib.error.URLError as exc:
        raise UsageError(f"failed to retrieve UPLOAD uri {uri}: {exc.reason}") from None
    if len(data) > settings.upload_max_bytes:
        raise UsageError(f"upload from {uri} exceeds the {settings.upload_max_bytes} byte limit")
    return data


def resolve_upload_sources(upload_param: str | None, files: dict[str, bytes]) -> dict[str, bytes]:
    """The raw VOTable bytes per uploaded table name."""
    if not upload_param:
        return {}
    sources: dict[str, bytes] = {}
    for name, uri in parse_upload_param(upload_param):
        if uri.startswith("param:"):
            ref = uri.removeprefix("param:")
            if ref not in files:
                raise UsageError(f"UPLOAD {name} references missing inline part {ref!r}")
            sources[name] = files[ref]
        elif uri.startswith(("http://", "https://")):
            sources[name] = _fetch(uri)
        else:
            raise UsageError(
                f"UPLOAD {name}: unsupported uri scheme {uri!r}"
                " (supported: param:<part>, http, https)"
            )
    return sources


async def gather_upload_sources(request: Request, params: dict) -> dict[str, bytes]:
    """The request's UPLOAD sources: multipart parts and fetched URIs."""
    files = await gather_upload_files(request)
    return resolve_upload_sources(params.get("UPLOAD"), files)


def parse_uploads(sources: dict[str, bytes]) -> list[UploadedTable]:
    return [
        parse_votable(name, data, settings.upload_max_rows, settings.upload_max_bytes)
        for name, data in sources.items()
    ]
