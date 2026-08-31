"""Shared route-layer types and error responses."""

from __future__ import annotations

import secrets
import threading
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

Authorization = Callable[[Request], JSONResponse | None]
JsonBodyReader = Callable[..., Awaitable[tuple[object | None, JSONResponse | None]]]

#: Every job manager (experiment, optimization, qualification) raises this
#: same exception family from snapshot/cancel/resume for "the job id is bad,
#: unreadable, or the manager rejected the action" — the three domains had
#: drifted to catch slightly different subsets of it by accident of
#: copy-paste (see docs/ltspicecodebasereview.md), not by design.
JOB_ACTION_EXCEPTIONS: tuple[type[BaseException], ...] = (
    FileNotFoundError,
    OSError,
    RuntimeError,
    ValueError,
)


def json_error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
    )


def mint_launch_token(
    launches: dict[str, dict[str, object]],
    record: dict[str, object],
    lock: threading.Lock,
    *,
    max_entries: int = 32,
) -> str:
    """Generate a launch token and store its frozen-plan record, evicting
    the oldest entry once the bound is reached.

    This is the one piece of freeze/freeze_optimization/freeze_qualification
    that was already character-for-character identical (per
    docs/ltspicecodebasereview.md) — everything else about those three
    handlers (body budget, extra expectation fields, staleness-check
    placement, lock scope) is a genuine behavioral difference between
    domains, not incidental duplication, so it's deliberately NOT collapsed
    here. Extracting only the truly-identical part keeps this a pure,
    zero-behavior-change refactor.
    """
    token = secrets.token_urlsafe(32)
    with lock:
        while len(launches) >= max_entries:
            launches.pop(next(iter(launches)))
        launches[token] = record
    return token


def add_job_crud_routes(
    router: APIRouter,
    *,
    prefix: str,
    id_param: str,
    authorize_read: Authorization,
    authorize_mutation: Authorization,
    manager_getter: Callable[[], object],
    payload_builder: Callable[[object], dict[str, object]],
    not_found_code: str,
    cancel_failed_code: str,
    resume_failed_code: str,
    results_payload_builder: Callable[[object], dict[str, object]] | None = None,
    results_not_found_code: str | None = None,
    exceptions: tuple[type[BaseException], ...] = JOB_ACTION_EXCEPTIONS,
) -> None:
    """Register the get / cancel / resume (/ results) route quartet shared
    by every durable job kind.

    Before this factory, each of study.py / optimization.py / qualification.py
    hand-rolled its own copy of this quartet — measured at 70-84% textually
    identical across the three, with the divergence being accidental
    (missing exception types, missing symlink filtering) rather than
    deliberate. This is the single implementation; callers supply only what
    is genuinely domain-specific: the path, the manager, the payload shape,
    and the existing error codes (kept as-is per domain so this is a pure
    refactor, not an API change).

    A plain ``Request`` is used instead of a typed path-parameter argument so
    the exact original path template (and therefore its parameter name, e.g.
    ``{experiment_id}`` vs ``{job_id}``) is preserved without needing every
    call site to declare a differently-named function per domain.
    """
    item_path = f"{prefix}/{{{id_param}}}"

    def get_item(request: Request) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        item_id = request.path_params[id_param]
        try:
            snapshot = manager_getter().snapshot(item_id)  # type: ignore[attr-defined]
            return JSONResponse(payload_builder(snapshot))
        except exceptions as exc:
            return json_error(404, not_found_code, str(exc))

    def cancel_item(request: Request) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        item_id = request.path_params[id_param]
        try:
            snapshot = manager_getter().cancel(item_id)  # type: ignore[attr-defined]
            return JSONResponse(payload_builder(snapshot))
        except exceptions as exc:
            return json_error(409, cancel_failed_code, str(exc))

    def resume_item(request: Request) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        item_id = request.path_params[id_param]
        try:
            snapshot = manager_getter().resume(item_id)  # type: ignore[attr-defined]
            return JSONResponse(payload_builder(snapshot), status_code=202)
        except exceptions as exc:
            return json_error(409, resume_failed_code, str(exc))

    router.add_api_route(item_path, get_item, methods=["GET"])
    router.add_api_route(f"{item_path}/cancel", cancel_item, methods=["POST"])
    router.add_api_route(f"{item_path}/resume", resume_item, methods=["POST"])

    if results_payload_builder is not None:
        assert results_not_found_code is not None

        def get_results(request: Request) -> Response:
            denied = authorize_read(request)
            if denied is not None:
                return denied
            item_id = request.path_params[id_param]
            try:
                snapshot = manager_getter().snapshot(item_id)  # type: ignore[attr-defined]
                return JSONResponse(results_payload_builder(snapshot))
            except exceptions as exc:
                return json_error(404, results_not_found_code, str(exc))

        router.add_api_route(f"{item_path}/results", get_results, methods=["GET"])
