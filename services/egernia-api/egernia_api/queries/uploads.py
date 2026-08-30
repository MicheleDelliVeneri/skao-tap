"""Resolution of UPLOAD sources: inline multipart parts and http(s) URIs."""

import urllib.error
import urllib.request
from urllib.parse import urlsplit

from egernia_core.config import settings
from egernia_core.errors import UsageError
from egernia_core.query.upload import UploadedTable, parse_upload_param, parse_votable
from fastapi import Request
from starlette.datastructures import UploadFile

FETCH_TIMEOUT_S = 15
READ_CHUNK_BYTES = 64 * 1024


def _allowed_upload_hosts() -> frozenset[str]:
    return frozenset(
        host.strip().lower().rstrip(".")
        for host in settings.upload_allowed_hosts.split(",")
        if host.strip()
    )


def _validate_remote_uri(uri: str) -> None:
    parsed = urlsplit(uri)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host or parsed.username is not None:
        raise UsageError(f"UPLOAD uri is not an allowed http(s) URL: {uri}")
    if host not in _allowed_upload_hosts():
        raise UsageError(f"UPLOAD uri host {host!r} is not allowed")


class _UploadRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Apply the destination policy again before following a redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_remote_uri(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


async def gather_upload_files(request: Request) -> dict[str, bytes]:
    """The multipart file parts of a POST, keyed by field name (the target
    of ``param:`` UPLOAD references)."""
    content_type = request.headers.get("content-type", "")
    if request.method != "POST" or "multipart/form-data" not in content_type:
        return {}
    files: dict[str, bytes] = {}
    total_bytes = 0
    form = await request.form(max_files=settings.upload_max_sources)
    for key, value in form.multi_items():
        if not isinstance(value, UploadFile):
            continue
        chunks: list[bytes] = []
        file_bytes = 0
        while chunk := await value.read(READ_CHUNK_BYTES):
            file_bytes += len(chunk)
            total_bytes += len(chunk)
            if file_bytes > settings.upload_max_bytes:
                raise UsageError(
                    f"inline upload {key} exceeds the {settings.upload_max_bytes} byte limit"
                )
            if total_bytes > settings.upload_max_total_bytes:
                raise UsageError(
                    "inline uploads exceed the "
                    f"{settings.upload_max_total_bytes} aggregate byte limit"
                )
            chunks.append(chunk)
        files[key] = b"".join(chunks)
    return files


def _fetch(uri: str) -> bytes:
    _validate_remote_uri(uri)
    request = urllib.request.Request(uri, headers={"User-Agent": "egernia"})
    opener = urllib.request.build_opener(_UploadRedirectHandler())
    try:
        with opener.open(request, timeout=FETCH_TIMEOUT_S) as response:
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
    parsed_sources = parse_upload_param(upload_param)
    if len(parsed_sources) > settings.upload_max_sources:
        raise UsageError(f"UPLOAD has more than the {settings.upload_max_sources} source limit")
    sources: dict[str, bytes] = {}
    total_bytes = sum(map(len, files.values()))
    if total_bytes > settings.upload_max_total_bytes:
        raise UsageError(
            f"uploads exceed the {settings.upload_max_total_bytes} aggregate byte limit"
        )
    for name, uri in parsed_sources:
        if uri.startswith("param:"):
            ref = uri.removeprefix("param:")
            if ref not in files:
                raise UsageError(f"UPLOAD {name} references missing inline part {ref!r}")
            sources[name] = files[ref]
        elif uri.startswith(("http://", "https://")):
            data = _fetch(uri)
            total_bytes += len(data)
            if total_bytes > settings.upload_max_total_bytes:
                raise UsageError(
                    f"uploads exceed the {settings.upload_max_total_bytes} aggregate byte limit"
                )
            sources[name] = data
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
