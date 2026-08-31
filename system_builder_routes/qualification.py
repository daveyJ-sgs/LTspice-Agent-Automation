"""Qualification preview, publication, launch, job, and result routes."""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

import qualification_recipe

from .common import Authorization, JsonBodyReader, json_error


def create_qualification_router(
    *,
    workspace: Path,
    authorize_read: Authorization,
    authorize_mutation: Authorization,
    read_json_body: JsonBodyReader,
    optimization_experiments: Callable[..., object],
    get_qualification_manager: Callable[[], object],
    qualification_job_payload: Callable[..., dict[str, object]],
    qualification_results_payload: Callable[..., dict[str, object]],
    execution_lock: threading.Lock,
    frozen_qualification_launches: dict[str, dict[str, object]],
) -> APIRouter:
    router = APIRouter()

    @router.post("/api/qualification/preview")
    async def preview_qualification(request: Request) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        payload, error = await read_json_body(request, maximum=16384)
        if error is not None:
            return error
        if not isinstance(payload, dict):
            return json_error(
                400,
                "invalid_qualification_preview",
                "preview request must be an object",
            )
        try:
            return JSONResponse(
                qualification_recipe.preview_qualification(
                    workspace / "runs",
                    str(payload.get("study_id")),
                    payload.get("candidate_index"),
                    payload.get(
                        "sample_count", qualification_recipe.DEFAULT_SAMPLE_COUNT
                    ),
                    payload.get("seed", qualification_recipe.DEFAULT_SEED),
                )
            )
        except (OSError, TypeError, ValueError) as exc:
            return json_error(422, "invalid_qualification", str(exc))

    @router.post("/api/qualification/freeze")
    async def freeze_qualification(request: Request) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        payload, error = await read_json_body(request, maximum=16384)
        if error is not None:
            return error
        if not isinstance(payload, dict):
            return json_error(
                400, "invalid_qualification_freeze", "freeze request must be an object"
            )
        required = (
            "study_id",
            "candidate_index",
            "sample_count",
            "seed",
            "expected_qualification_id",
            "expected_statistical_plan_id",
            "expected_total_run_count",
        )
        if any(name not in payload for name in required):
            return json_error(
                400,
                "invalid_qualification_freeze",
                "freeze requires the exact preview identities and workload",
            )
        try:
            preview, published = qualification_recipe.publish_qualification(
                workspace / "runs",
                str(payload["study_id"]),
                payload["candidate_index"],
                payload["sample_count"],
                payload["seed"],
                str(payload["expected_qualification_id"]),
                str(payload["expected_statistical_plan_id"]),
                payload["expected_total_run_count"],
            )
            experiments, execution, execution_sha256 = optimization_experiments()
        except (OSError, TypeError, ValueError) as exc:
            return json_error(409, "qualification_freeze_failed", str(exc))
        launch_token = secrets.token_urlsafe(32)
        with execution_lock:
            while len(frozen_qualification_launches) >= 32:
                frozen_qualification_launches.pop(
                    next(iter(frozen_qualification_launches))
                )
            frozen_qualification_launches[launch_token] = {
                "state": "ready",
                "study_id": payload["study_id"],
                "candidate_index": payload["candidate_index"],
                "sample_count": payload["sample_count"],
                "seed": payload["seed"],
                "qualification_id": preview["qualification_id"],
                "plan_id": published["plan_id"],
                "total_run_count": payload["expected_total_run_count"],
                "execution_sha256": execution_sha256,
            }
        return JSONResponse(
            {
                "status": "frozen",
                "launch_token": launch_token,
                "plan": {
                    "plan_id": published["plan_id"],
                    "plan_sha256": published["plan_sha256"],
                    "point_count": published["point_count"],
                    "artifact": f"runs/robust-selection-plans/{published['plan_id']}/robust_selection_plan.json",
                },
                "execution": {
                    **preview["execution"],
                    "max_concurrency": execution.get("max_concurrency", 2),
                    "reuse_cache": execution.get("reuse_cache", False),
                },
            }
        )

    @router.post("/api/qualification/start")
    async def start_qualification(request: Request) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        payload, error = await read_json_body(request, maximum=8192)
        if error is not None:
            return error
        if (
            not isinstance(payload, dict)
            or payload.get("acknowledged") is not True
            or not isinstance(payload.get("launch_token"), str)
        ):
            return json_error(
                400,
                "invalid_qualification_start",
                "start requires the launch token and explicit acknowledgement",
            )
        token = payload["launch_token"]
        with execution_lock:
            frozen = frozen_qualification_launches.get(token)
            if (
                isinstance(frozen, dict)
                and frozen.get("state") == "started"
                and isinstance(frozen.get("response"), dict)
            ):
                return JSONResponse(frozen["response"])
            if not isinstance(frozen, dict) or frozen.get("state") != "ready":
                return json_error(
                    409,
                    "qualification_freeze_required",
                    "publish a fresh immutable qualification plan before starting",
                )
            if payload.get("confirmed_total_run_count") != frozen.get(
                "total_run_count"
            ):
                return json_error(
                    409,
                    "qualification_workload_changed",
                    "confirmed workload does not match the frozen plan",
                )
            frozen["state"] = "starting"
        try:
            experiments, _execution, execution_sha256 = optimization_experiments()
            if execution_sha256 != frozen["execution_sha256"]:
                raise ValueError("paired circuit definitions changed after publication")
            manager = get_qualification_manager()
            defined = manager.define(str(frozen["plan_id"]), experiments)
            started = manager.start(defined["qualification_job_id"])
            response = qualification_job_payload(started)
            frozen["state"] = "started"
            frozen["response"] = response
            return JSONResponse(response, status_code=202)
        except (OSError, RuntimeError, ValueError) as exc:
            frozen["state"] = "failed"
            return json_error(409, "qualification_launch_failed", str(exc))

    @router.get("/api/qualification/jobs")
    def qualification_jobs(request: Request, limit: int = 8) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        if not isinstance(limit, int) or not 1 <= limit <= 32:
            return json_error(400, "qualification_job_limit", "limit must be 1 to 32")
        root = workspace / "runs" / "qualification-jobs"
        if not root.is_dir() or root.is_symlink():
            return JSONResponse({"jobs": []})
        paths = sorted(
            (
                path
                for path in root.glob("qualification-job-*")
                if path.is_dir() and not path.is_symlink()
            ),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )[:limit]
        jobs = []
        for path in paths:
            try:
                jobs.append(
                    qualification_job_payload(
                        get_qualification_manager().snapshot(path.name)
                    )
                )
            except (FileNotFoundError, OSError, RuntimeError, ValueError):
                continue
        return JSONResponse({"jobs": jobs})

    @router.get("/api/qualification/jobs/{job_id}")
    def qualification_job(request: Request, job_id: str) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        try:
            return JSONResponse(
                qualification_job_payload(get_qualification_manager().snapshot(job_id))
            )
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            return json_error(404, "qualification_job_not_found", str(exc))

    @router.get("/api/qualification/jobs/{job_id}/results")
    def qualification_results(request: Request, job_id: str) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        try:
            snapshot = get_qualification_manager().snapshot(job_id)
            return JSONResponse(qualification_results_payload(snapshot))
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            return json_error(404, "qualification_results_not_found", str(exc))

    @router.post("/api/qualification/jobs/{job_id}/cancel")
    def cancel_qualification(request: Request, job_id: str) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        try:
            return JSONResponse(
                qualification_job_payload(get_qualification_manager().cancel(job_id))
            )
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            return json_error(409, "qualification_cancel_failed", str(exc))

    @router.post("/api/qualification/jobs/{job_id}/resume")
    def resume_qualification(request: Request, job_id: str) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        try:
            return JSONResponse(
                qualification_job_payload(get_qualification_manager().resume(job_id)),
                status_code=202,
            )
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            return json_error(409, "qualification_resume_failed", str(exc))

    return router
