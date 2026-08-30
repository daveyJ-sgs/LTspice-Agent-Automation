"""Shared route-layer types and error responses."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse

Authorization = Callable[[Request], JSONResponse | None]
JsonBodyReader = Callable[..., Awaitable[tuple[object | None, JSONResponse | None]]]


def json_error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
    )
