"""DALI parameter handling: TAP parameters are case-insensitive in name and
may arrive via query string (GET) or form body (POST)."""

from egernia_core.config import settings
from egernia_core.errors import UsageError
from fastapi import Request


def _set(params: dict[str, str], key: str, value: str) -> None:
    key = key.upper()
    if key == "UPLOAD" and key in params:
        # DALI: UPLOAD may be given several times; the pairs accumulate
        params[key] = f"{params[key]};{value}"
    else:
        params[key] = value


async def gather_params(request: Request) -> dict[str, str]:
    params: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        _set(params, key, value)
    content_type = request.headers.get("content-type", "")
    if request.method == "POST" and (
        "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type
    ):
        form = await request.form(max_files=settings.upload_max_sources)
        for key, value in form.multi_items():
            if isinstance(value, str):
                _set(params, key, value)
    return params


def require(params: dict[str, str], name: str) -> str:
    value = params.get(name)
    if not value:
        raise UsageError(f"missing required parameter {name}")
    return value
