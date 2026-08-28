#!/usr/bin/env python3
"""Local-only web entry point for LTspice System Builder."""

from __future__ import annotations

import argparse
import hmac
import json
import re
import secrets
import socket
import threading
import webbrowser
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from study_recipe import MAX_RECIPE_BYTES, load_study_recipe, preview_study_recipe


PROJECT_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = PROJECT_ROOT / "system_builder_static"
EXAMPLE_RECIPE = PROJECT_ROOT / "examples/mixed_signal_daq.ltstudy.json"
SESSION_COOKIE = "ltspice_system_builder_session"
HOST_PATTERN = re.compile(r"(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?")


def _json_error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
    )


def create_app(
    workspace_root: Path = PROJECT_ROOT,
    *,
    testing: bool = False,
) -> FastAPI:
    """Create one session-scoped loopback application."""
    workspace = workspace_root.resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("workspace_root must be a directory")
    session_token = secrets.token_urlsafe(32)
    app = FastAPI(
        title="LTspice System Builder",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def valid_host(request: Request) -> bool:
        host = request.headers.get("host", "")
        return bool(HOST_PATTERN.fullmatch(host) or (testing and host == "testserver"))

    def has_session(request: Request) -> bool:
        supplied = request.cookies.get(SESSION_COOKIE, "")
        return bool(supplied) and hmac.compare_digest(supplied, session_token)

    def authorize_read(request: Request) -> JSONResponse | None:
        if not has_session(request):
            return _json_error(401, "session_required", "open System Builder first")
        return None

    def authorize_mutation(request: Request) -> JSONResponse | None:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        host = request.headers.get("host", "")
        expected_origin = f"http://{host}"
        if request.headers.get("origin") != expected_origin:
            return _json_error(403, "origin_rejected", "request origin is not local")
        if request.headers.get("x-ltspice-system-builder") != "1":
            return _json_error(
                403,
                "request_marker_required",
                "System Builder request marker is missing",
            )
        return None

    @app.middleware("http")
    async def local_security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        if not valid_host(request):
            return _json_error(400, "host_rejected", "host must be loopback-only")
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/")
    def index() -> Response:
        body = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        response = HTMLResponse(body)
        response.set_cookie(
            SESSION_COOKIE,
            session_token,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/assets/app.css")
    def stylesheet() -> FileResponse:
        return FileResponse(STATIC_ROOT / "app.css", media_type="text/css")

    @app.get("/assets/app.js")
    def javascript() -> FileResponse:
        return FileResponse(
            STATIC_ROOT / "app.js", media_type="text/javascript; charset=utf-8"
        )

    @app.get("/assets/daq-schematic.png")
    def schematic() -> FileResponse:
        return FileResponse(
            PROJECT_ROOT / "docs/images/mixed-signal-daq-schematic.png",
            media_type="image/png",
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "mode": "local-only"}

    @app.get("/api/session")
    def session(request: Request) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        return JSONResponse(
            {
                "product": "LTspice System Builder",
                "mode": "local-only",
                "remote_execution": False,
                "workspace": str(workspace),
            }
        )

    @app.get("/api/examples/mixed-signal-daq")
    def mixed_signal_daq(request: Request) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        return JSONResponse(load_study_recipe(EXAMPLE_RECIPE))

    @app.post("/api/preview")
    async def preview(request: Request) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            return _json_error(415, "json_required", "Content-Type must be application/json")
        try:
            content_length = int(request.headers.get("content-length", "0"))
        except ValueError:
            content_length = 0
        if not 1 <= content_length <= MAX_RECIPE_BYTES:
            return _json_error(
                413,
                "recipe_size",
                f"recipe must contain 1 to {MAX_RECIPE_BYTES} bytes",
            )
        body = await request.body()
        if len(body) != content_length:
            return _json_error(
                400,
                "body_length_mismatch",
                "request body does not match Content-Length",
            )
        try:
            recipe = json.loads(
                body,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON value: {value}")
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return _json_error(400, "invalid_json", "body must be finite UTF-8 JSON")
        return JSONResponse(preview_study_recipe(recipe, workspace))

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=PROJECT_ROOT,
        help="allowed circuit workspace (defaults to this project)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="start without opening the default browser",
    )
    args = parser.parse_args()

    import uvicorn

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}/"
    app = create_app(args.workspace)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    print(f"LTspice System Builder: {url}")
    print(f"Workspace: {args.workspace.resolve()}")
    try:
        uvicorn.Server(
            uvicorn.Config(app, log_level="info", access_log=False)
        ).run(sockets=[listener])
    except KeyboardInterrupt:
        pass
    finally:
        listener.close()


if __name__ == "__main__":
    main()
