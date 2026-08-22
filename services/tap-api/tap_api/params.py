"""DALI parameter handling: TAP parameters are case-insensitive in name and
may arrive via query string (GET) or form body (POST)."""

from fastapi import Request
from tapcore.errors import UsageError


async def gather_params(request: Request) -> dict[str, str]:
    params: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        params[key.upper()] = value
    content_type = request.headers.get("content-type", "")
    if request.method == "POST" and (
        "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type
    ):
        form = await request.form()
        for key, value in form.multi_items():
            if isinstance(value, str):
                params[key.upper()] = value
    return params


def require(params: dict[str, str], name: str) -> str:
    value = params.get(name)
    if not value:
        raise UsageError(f"missing required parameter {name}")
    return value
