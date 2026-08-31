"""Explicit GitHub dispatch, recovery, and evidence routes."""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

import artifacts
import statistical_engine
from remote_execution import build_remote_envelope, build_remote_preview
from study_recipe import load_recipe_experiments, preview_study_recipe

from .common import Authorization, JsonBodyReader, json_error


def create_remote_router(
    *,
    workspace: Path,
    authorize_read: Authorization,
    authorize_mutation: Authorization,
    read_json_body: JsonBodyReader,
    execution_lock: threading.Lock,
    frozen_launches: dict[str, dict[str, object]],
    remote_client: object,
) -> APIRouter:
    router = APIRouter()

    @router.post("/api/remote/auth")
    def remote_auth(request: Request) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        try:
            return JSONResponse(remote_client.auth_status())  # type: ignore[attr-defined]
        except (OSError, RuntimeError, ValueError) as exc:
            return json_error(409, "github_auth_unavailable", str(exc))

    @router.get("/api/remote/jobs")
    def remote_jobs(request: Request) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        try:
            return JSONResponse({"jobs": remote_client.list_jobs()})  # type: ignore[attr-defined]
        except (OSError, RuntimeError, ValueError) as exc:
            return json_error(409, "remote_jobs_unavailable", str(exc))

    @router.post("/api/remote/dispatch")
    async def remote_dispatch(request: Request) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        payload, error = await read_json_body(request)
        if error is not None:
            return error
        if not isinstance(payload, dict) or payload.get("acknowledged") is not True:
            return json_error(
                400,
                "remote_acknowledgement_required",
                "remote dispatch requires explicit acknowledgement",
            )
        launch_token = payload.get("launch_token")
        confirmed_plan_id = payload.get("confirmed_plan_id")
        confirmed_run_count = payload.get("confirmed_run_count")
        confirmed_preview_id = payload.get("confirmed_preview_id")
        confirmed_preview_sha256 = payload.get("confirmed_preview_sha256")
        recipe = payload.get("recipe")
        if (
            not isinstance(launch_token, str)
            or not isinstance(confirmed_plan_id, str)
            or isinstance(confirmed_run_count, bool)
            or not isinstance(confirmed_run_count, int)
            or not isinstance(confirmed_preview_id, str)
            or not isinstance(confirmed_preview_sha256, str)
        ):
            return json_error(
                400,
                "invalid_remote_dispatch",
                "remote dispatch confirmation is incomplete",
            )
        with execution_lock:
            frozen = frozen_launches.get(launch_token)
            if frozen is None:
                return json_error(
                    409,
                    "freeze_required",
                    "create a fresh immutable plan before remote dispatch",
                )
            if frozen["state"] == "remote_started":
                response = frozen.get("response")
                assert isinstance(response, dict)
                return JSONResponse(response)
            if frozen["state"] != "ready":
                return json_error(
                    409,
                    "remote_dispatch_unavailable",
                    "this frozen plan is no longer available for remote dispatch",
                )
            frozen_snapshot = dict(frozen)
        current = preview_study_recipe(recipe, workspace)
        if not current.get("valid"):
            return JSONResponse(current, status_code=422)
        current_recipe = current["recipe"]
        current_plan = current["plan"]
        current_execution = current["execution"]
        assert isinstance(current_recipe, dict)
        assert isinstance(current_plan, dict)
        assert isinstance(current_execution, dict)
        if (
            current_recipe.get("sha256") != frozen_snapshot["recipe_sha256"]
            or current_plan.get("plan_id") != frozen_snapshot["plan_id"]
            or confirmed_plan_id != frozen_snapshot["plan_id"]
            or confirmed_run_count != frozen_snapshot["total_run_count"]
            or current_execution.get("total_run_count")
            != frozen_snapshot["total_run_count"]
        ):
            return json_error(
                409,
                "remote_frozen_study_changed",
                "the recipe, plan, or workload changed after freezing",
            )
        try:
            preview = build_remote_preview(
                repository=payload.get("repository"),
                ref=payload.get("ref"),
                plan_id=frozen_snapshot["plan_id"],
                plan_sha256=frozen_snapshot["plan_sha256"],
                recipe_sha256=frozen_snapshot["recipe_sha256"],
                plan_artifact=frozen_snapshot["plan_artifact"],
                point_count=frozen_snapshot["point_count"],
                experiment_count=frozen_snapshot["experiment_count"],
                total_run_count=frozen_snapshot["total_run_count"],
            )
            if (
                preview["preview_id"] != confirmed_preview_id
                or preview["preview_sha256"] != confirmed_preview_sha256
            ):
                return json_error(
                    409,
                    "remote_preview_changed",
                    "repository, ref, or frozen workload changed after preview",
                )
            plan_path = workspace.joinpath(*str(frozen_snapshot["plan_artifact"]).split("/"))
            plan_artifact = artifacts.read_verified(
                plan_path, str(frozen_snapshot["plan_sha256"])
            )
            statistical_engine.load_statistical_plan(
                workspace / "runs", str(frozen_snapshot["plan_id"])
            )
            experiments = load_recipe_experiments(recipe, workspace)
            envelope = build_remote_envelope(
                preview=preview,
                recipe=recipe,
                plan_artifact=plan_artifact,
                experiments=experiments,
            )
            remote_client.auth_status()  # type: ignore[attr-defined]
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            return json_error(409, "remote_dispatch_preflight_failed", str(exc))
        with execution_lock:
            current_frozen = frozen_launches.get(launch_token)
            if current_frozen is None or current_frozen["state"] != "ready":
                return json_error(
                    409,
                    "remote_dispatch_unavailable",
                    "the frozen launch changed during remote preflight",
                )
            current_frozen["state"] = "remote_starting"
        try:
            result = remote_client.dispatch(preview, envelope)  # type: ignore[attr-defined]
        except (OSError, RuntimeError, ValueError) as exc:
            with execution_lock:
                frozen_launches[launch_token]["state"] = "remote_unknown"
            return json_error(409, "remote_dispatch_unknown", str(exc))
        response = {"status": "submitted", "job": result}
        with execution_lock:
            frozen_launches[launch_token]["state"] = "remote_started"
            frozen_launches[launch_token]["response"] = response
        return JSONResponse(response, status_code=202)

    @router.post("/api/remote/jobs/{remote_job_id}/refresh")
    def refresh_remote_job(request: Request, remote_job_id: str) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        try:
            return JSONResponse(remote_client.refresh(remote_job_id))  # type: ignore[attr-defined]
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            return json_error(409, "remote_refresh_failed", str(exc))

    @router.post("/api/remote/jobs/{remote_job_id}/download")
    def download_remote_job(request: Request, remote_job_id: str) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        try:
            return JSONResponse(remote_client.download(remote_job_id))  # type: ignore[attr-defined]
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            return json_error(409, "remote_download_failed", str(exc))

    return router
