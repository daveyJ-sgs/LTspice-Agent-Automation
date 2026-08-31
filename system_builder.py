#!/usr/bin/env python3
"""Local-only web entry point for LTspice System Builder."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import secrets
import socket
import threading
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import experiment_report
import optimization_engine
import optimization_study
import qualification_study
import robust_selection
import schematic_capture
from github_remote import GitHubRemote
from study_recipe import (
    MAX_RECIPE_BYTES,
    load_recipe_experiments,
    load_study_recipe,
)
from system_builder_history import evidence_file
from system_builder_routes import (
    create_core_router,
    create_optimization_router,
    create_qualification_router,
    create_remote_router,
    create_schematic_router,
    create_study_router,
    json_error,
)

PROJECT_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = PROJECT_ROOT / "system_builder_static"
EXAMPLE_RECIPE = PROJECT_ROOT / "examples/mixed_signal_daq.ltstudy.json"
EXAMPLE_OPTIMIZATION_RECIPE = PROJECT_ROOT / "examples/mixed_signal_daq.ltopt.json"
SESSION_COOKIE = "ltspice_system_builder_session"
HOST_PATTERN = re.compile(r"(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?")
FONT_ASSETS = {
    "IBMPlexSans-Regular.woff2",
    "IBMPlexSans-SemiBold.woff2",
    "IBMPlexMono-Regular.woff2",
    "IBMPlexMono-SemiBold.woff2",
}
MAX_MUTATION_BYTES = MAX_RECIPE_BYTES + 8192


def _default_manager_factory(runs_dir: Path) -> object:
    import mcp_server

    return mcp_server.ExperimentJobManager(runs_dir)


def _json_error(status: int, code: str, message: str) -> JSONResponse:
    return json_error(status, code, message)


def create_app(
    workspace_root: Path = PROJECT_ROOT,
    *,
    testing: bool = False,
    manager_factory: Callable[[Path], object] | None = None,
    schematic_capturer: Callable[[Path, object], dict[str, object]] | None = None,
    remote_client: object | None = None,
) -> FastAPI:
    """Create one session-scoped loopback application."""
    workspace = workspace_root.resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("workspace_root must be a directory")
    session_token = secrets.token_urlsafe(32)
    manager_builder = manager_factory or _default_manager_factory
    capture_builder = schematic_capturer or schematic_capture.capture_schematic
    github_remote = remote_client or GitHubRemote(workspace / "runs")
    execution_manager: object | None = None
    optimization_manager: optimization_study.OptimizationStudyManager | None = None
    qualification_manager: qualification_study.QualificationStudyManager | None = None
    execution_lock = threading.Lock()
    report_lock = threading.Lock()
    postprocess_stop = threading.Event()
    postprocess_thread: threading.Thread | None = None
    postprocess_states: dict[str, dict[str, str]] = {}
    managed_jobs: set[str] = set()
    frozen_launches: dict[str, dict[str, object]] = {}
    frozen_optimization_launches: dict[str, dict[str, object]] = {}
    frozen_qualification_launches: dict[str, dict[str, object]] = {}

    def get_execution_manager() -> object:
        nonlocal execution_manager
        if execution_manager is None:
            execution_manager = manager_builder(workspace / "runs")
        return execution_manager

    def get_optimization_manager() -> optimization_study.OptimizationStudyManager:
        nonlocal optimization_manager
        if optimization_manager is None:
            optimization_manager = optimization_study.OptimizationStudyManager(
                workspace / "runs", get_execution_manager()  # type: ignore[arg-type]
            )
        return optimization_manager

    def get_qualification_manager() -> qualification_study.QualificationStudyManager:
        nonlocal qualification_manager
        if qualification_manager is None:
            qualification_manager = qualification_study.QualificationStudyManager(
                workspace / "runs", get_execution_manager()  # type: ignore[arg-type]
            )
        return qualification_manager

    def shutdown_execution_manager() -> None:
        nonlocal execution_manager
        if execution_manager is not None:
            shutdown = getattr(execution_manager, "shutdown", None)
            if callable(shutdown):
                shutdown()
            execution_manager = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
        nonlocal postprocess_thread
        try:
            if not testing:
                get_execution_manager()
                discover_managed_jobs()
                postprocess_thread = threading.Thread(
                    target=postprocess_managed_jobs,
                    name="ltspice-system-builder-postprocess",
                    daemon=True,
                )
                postprocess_thread.start()
            yield
        finally:
            postprocess_stop.set()
            if postprocess_thread is not None:
                postprocess_thread.join()
            shutdown_execution_manager()

    app = FastAPI(
        title="LTspice System Builder",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
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

    async def read_json_body(
        request: Request,
        *,
        maximum: int = MAX_MUTATION_BYTES,
    ) -> tuple[object | None, JSONResponse | None]:
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            return None, _json_error(
                415, "json_required", "Content-Type must be application/json"
            )
        try:
            content_length = int(request.headers.get("content-length", "0"))
        except ValueError:
            content_length = 0
        if not 1 <= content_length <= maximum:
            return None, _json_error(
                413,
                "request_size",
                f"request must contain 1 to {maximum} bytes",
            )
        body = await request.body()
        if len(body) != content_length:
            return None, _json_error(
                400,
                "body_length_mismatch",
                "request body does not match Content-Length",
            )
        try:
            value = json.loads(
                body,
                parse_constant=lambda constant: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON value: {constant}")
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None, _json_error(
                400, "invalid_json", "body must be finite UTF-8 JSON"
            )
        return value, None

    def system_builder_report_context(experiment_id: str) -> dict[str, str]:
        path = evidence_file(
            workspace / "runs", f"{experiment_id}/experiment_manifest.json"
        )
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("experiment manifest exceeds the read budget")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        definition = manifest.get("definition") if isinstance(manifest, dict) else None
        point_plan = (
            definition.get("point_plan") if isinstance(definition, dict) else None
        )
        source = point_plan.get("source") if isinstance(point_plan, dict) else None
        builder = source.get("system_builder") if isinstance(source, dict) else None
        context = builder.get("report_context") if isinstance(builder, dict) else None
        if not isinstance(context, dict):
            return {}
        return {
            str(name): str(value)
            for name, value in context.items()
            if name in experiment_report.ReportContext.__annotations__
            and isinstance(value, str)
        }

    def discover_managed_jobs() -> None:
        runs = workspace / "runs"
        if not runs.is_dir() or runs.is_symlink():
            return
        for manifest_path in runs.glob("mcp-experiment-*/experiment_manifest.json"):
            experiment_id = manifest_path.parent.name
            try:
                context = system_builder_report_context(experiment_id)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                continue
            if context:
                managed_jobs.add(experiment_id)

    def build_completed_report(experiment_id: str) -> dict[str, object]:
        manager = get_execution_manager()
        snapshot = manager.snapshot(experiment_id)  # type: ignore[attr-defined]
        if snapshot["status"] != "completed":
            raise ValueError("only completed experiments can be finalized")
        with report_lock:
            postprocess_states[experiment_id] = {"state": "building"}
            context = system_builder_report_context(experiment_id)
            result = experiment_report.build_experiment_report(
                workspace / "runs",
                experiment_id,
                context or None,
            )
            postprocess_states[experiment_id] = {"state": "complete"}
        return result

    def postprocess_managed_jobs() -> None:
        while not postprocess_stop.wait(0.5):
            for experiment_id in tuple(managed_jobs):
                state = postprocess_states.get(experiment_id, {}).get("state")
                report = workspace / "runs" / experiment_id / "report.html"
                if state in {"building", "complete", "failed"}:
                    continue
                if report.is_file() and not report.is_symlink():
                    postprocess_states[experiment_id] = {"state": "complete"}
                    continue
                try:
                    snapshot = get_execution_manager().snapshot(experiment_id)  # type: ignore[attr-defined]
                    if snapshot["status"] == "completed":
                        build_completed_report(experiment_id)
                except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
                    postprocess_states[experiment_id] = {
                        "state": "failed",
                        "error": str(exc),
                    }

    def job_payload(snapshot: dict[str, object]) -> dict[str, object]:
        experiment_id = str(snapshot["experiment_id"])
        report = workspace / "runs" / experiment_id / "report.html"
        postprocess = postprocess_states.get(experiment_id, {"state": "pending"})
        return {
            "experiment_id": experiment_id,
            "status": snapshot["status"],
            "point_count": snapshot["point_count"],
            "finished_points": snapshot.get("finished_points", 0),
            "running_points": snapshot.get("running_points", 0),
            "pending_points": snapshot.get("pending_points", 0),
            "passed_points": snapshot.get("passed_points", 0),
            "failed_points": snapshot.get("failed_points", 0),
            "all_passed": snapshot.get("all_passed"),
            "report_available": report.is_file() and not report.is_symlink(),
            "report_url": f"/evidence/{experiment_id}/report.html"
            if report.is_file() and not report.is_symlink()
            else None,
            "resumable": snapshot["status"] == "cancelled",
            "postprocess": postprocess,
        }

    def optimization_experiments() -> tuple[dict[str, dict[str, object]], dict[str, object], str]:
        from examples.mixed_signal_daq_study import TRANSIENT_ANALYSES
        from examples.optimize_mixed_signal_daq import AC_ANALYSES

        execution_recipe = load_study_recipe(EXAMPLE_RECIPE)
        definitions = load_recipe_experiments(execution_recipe, workspace)
        execution = execution_recipe.get("execution", {})
        if not isinstance(execution, dict):
            raise ValueError("optimization execution settings are invalid")
        experiments = {
            str(definition["name"]): {
                "netlist_template": definition["netlist_template"],
                "waveform_analyses": (
                    AC_ANALYSES
                    if definition["name"] == "ac"
                    else TRANSIENT_ANALYSES
                ),
                "filename": definition["filename"],
                "max_concurrency": execution.get("max_concurrency", 2),
                "reuse_cache": execution.get("reuse_cache", False),
            }
            for definition in definitions
        }
        artifact = json.dumps(
            experiments,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return experiments, execution, hashlib.sha256(artifact).hexdigest()

    def validate_optimization_experiments(
        recipe: object, experiments: dict[str, dict[str, object]]
    ) -> None:
        if not isinstance(recipe, dict):
            raise ValueError("optimization recipe must be an object")
        selectors = [*recipe.get("objectives", []), *recipe.get("constraints", [])]
        for selector in selectors:
            if not isinstance(selector, dict):
                raise ValueError("optimization selectors must be objects")
            experiment_name = selector.get("experiment")
            analysis_name = selector.get("analysis")
            experiment = experiments.get(str(experiment_name))
            analyses = experiment.get("waveform_analyses") if experiment else None
            available = {
                analysis.get("name")
                for analysis in analyses
                if isinstance(analysis, dict)
            } if isinstance(analyses, list) else set()
            if analysis_name not in available:
                raise ValueError(
                    f"analysis {experiment_name}.{analysis_name} is not defined by "
                    "the paired circuit study"
                )

    def optimization_job_payload(snapshot: dict[str, object]) -> dict[str, object]:
        experiments = snapshot.get("experiments", {})
        if not isinstance(experiments, dict):
            raise ValueError("optimization job experiments are invalid")
        children = []
        totals = {
            "total_runs": 0,
            "finished_points": 0,
            "running_points": 0,
            "pending_points": 0,
            "passed_points": 0,
            "failed_points": 0,
            "error_points": 0,
        }
        for name, child in sorted(experiments.items()):
            if not isinstance(child, dict):
                raise ValueError("optimization child snapshot is invalid")
            item = {
                "name": name,
                "experiment_id": child.get("experiment_id"),
                "status": child.get("status"),
                "point_count": child.get("point_count", 0),
                "finished_points": child.get("finished_points", 0),
                "running_points": child.get("running_points", 0),
                "pending_points": child.get("pending_points", 0),
                "passed_points": child.get("passed_points", 0),
                "failed_points": child.get("failed_points", 0),
                "error_points": child.get("error_points", 0),
            }
            children.append(item)
            totals["total_runs"] += int(item["point_count"])
            for key in set(totals) - {"total_runs"}:
                value = item.get(key, 0)
                if isinstance(value, int) and not isinstance(value, bool):
                    totals[key] += value
        plan = optimization_engine.load_optimization_plan(
            workspace / "runs", str(snapshot["plan_id"])
        )
        candidate_count = int(plan["candidate_count"])
        point_count = int(plan["point_count"])
        study_id = snapshot.get("optimization_study_id")
        return {
            "optimization_job_id": snapshot["optimization_job_id"],
            "plan_id": snapshot["plan_id"],
            "status": snapshot["status"],
            "experiments": children,
            "progress": {
                **totals,
                "candidate_count": candidate_count,
                "corner_count": point_count // candidate_count,
                "point_count": point_count,
                "evaluation": (
                    "complete"
                    if snapshot.get("optimization_study_id")
                    else "failed"
                    if snapshot["status"] == "failed"
                    else "pending"
                ),
            },
            "optimization_study_id": study_id,
            "results_url": (
                f"/api/optimization/jobs/{snapshot['optimization_job_id']}/results"
                if study_id
                else None
            ),
            "resumable": snapshot["status"] == "cancelled",
            "error": snapshot.get("error"),
        }

    def optimization_results_payload(snapshot: dict[str, object]) -> dict[str, object]:
        study_id = snapshot.get("optimization_study_id")
        if snapshot.get("status") != "completed" or not isinstance(study_id, str):
            raise ValueError("optimization results are not complete")
        results_file = evidence_file(
            workspace / "runs",
            f"optimization-studies/{study_id}/optimization_results.json",
        )
        if results_file.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("optimization results exceed the read budget")
        try:
            results = json.loads(
                results_file.read_text(encoding="utf-8"),
                parse_constant=lambda constant: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON value: {constant}")
                ),
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("optimization results are invalid") from exc
        if (
            not isinstance(results, dict)
            or results.get("study_id") != study_id
            or results.get("plan_id") != snapshot.get("plan_id")
            or not isinstance(results.get("candidates"), list)
        ):
            raise ValueError("optimization result identity does not match the job")
        plan = optimization_engine.load_optimization_plan(
            workspace / "runs", str(snapshot["plan_id"])
        )
        candidates = results["candidates"]
        if len(candidates) != plan["candidate_count"] or not all(
            isinstance(candidate, dict) for candidate in candidates
        ):
            raise ValueError("optimization candidate results are incomplete")
        sanitized_candidates = []
        for candidate in candidates:
            parameters = candidate.get("parameters")
            objectives = candidate.get("objectives")
            constraints = candidate.get("constraints")
            errors = candidate.get("errors")
            if not all(
                isinstance(value, dict)
                for value in (parameters, objectives, constraints)
            ) or not isinstance(errors, list):
                raise ValueError("optimization candidate result is invalid")
            sanitized_candidates.append(
                {
                    "candidate_index": candidate.get("candidate_index"),
                    "status": candidate.get("status"),
                    "parameters": parameters,
                    "objectives": {
                        name: {
                            key: record.get(key)
                            for key in ("value", "unit", "worst_point_index")
                        }
                        for name, record in objectives.items()
                        if isinstance(name, str) and isinstance(record, dict)
                    },
                    "constraints": {
                        name: {
                            key: record.get(key)
                            for key in (
                                "passed",
                                "worst_value",
                                "unit",
                                "worst_point_index",
                                "margin",
                                "operator",
                                "target",
                            )
                        }
                        for name, record in constraints.items()
                        if isinstance(name, str) and isinstance(record, dict)
                    },
                    "errors": [error for error in errors if isinstance(error, str)],
                    "pareto": candidate.get("pareto"),
                    "selected": candidate.get("selected"),
                    "selection_score": candidate.get("selection_score"),
                }
            )
        definition = plan["definition"]
        return {
            "study_id": study_id,
            "plan_id": snapshot["plan_id"],
            "selection_policy": results.get("selection_policy"),
            "selection_explanation": results.get("selection_explanation"),
            "candidate_count": results.get("candidate_count"),
            "feasible_candidates": results.get("feasible_candidates"),
            "constraint_failed_candidates": results.get(
                "constraint_failed_candidates"
            ),
            "invalid_candidates": results.get("invalid_candidates"),
            "pareto_candidates": results.get("pareto_candidates"),
            "selected_candidate_index": results.get("selected_candidate_index"),
            "parameter_units": plan.get("parameter_units", {}),
            "objectives": definition.get("objectives", []),
            "candidates": sanitized_candidates,
            "evidence": {
                "report": f"/evidence/optimization-studies/{study_id}/report.html",
                "json": (
                    f"/evidence/optimization-studies/{study_id}/"
                    "optimization_results.json"
                ),
                "csv": (
                    f"/evidence/optimization-studies/{study_id}/"
                    "optimization_results.csv"
                ),
                "plan": (
                    f"/evidence/optimization-plans/{snapshot['plan_id']}/"
                    "optimization_plan.json"
                ),
            },
        }

    def qualification_job_payload(snapshot: dict[str, object]) -> dict[str, object]:
        experiments = snapshot.get("experiments", {})
        if not isinstance(experiments, dict):
            raise ValueError("qualification job experiments are invalid")
        children: list[dict[str, object]] = []
        finished = running = pending = total = 0
        for name, child in sorted(experiments.items()):
            if not isinstance(child, dict):
                raise ValueError("qualification child snapshot is invalid")
            point_count = int(child.get("point_count", 0))
            item = {
                "name": name, "experiment_id": child.get("experiment_id"),
                "status": child.get("status"), "point_count": point_count,
                "finished_points": child.get("finished_points", 0),
                "running_points": child.get("running_points", 0),
                "pending_points": child.get("pending_points", 0),
            }
            children.append(item)
            total += point_count
            finished += int(item["finished_points"])
            running += int(item["running_points"])
            pending += int(item["pending_points"])
        study_id = snapshot.get("qualification_study_id")
        plan = robust_selection.load_robust_selection_plan(
            workspace / "runs", str(snapshot["plan_id"])
        )
        plan_definition = plan["definition"]
        assert isinstance(plan_definition, dict)
        finalists = plan_definition["finalists"]
        assert isinstance(finalists, list) and len(finalists) == 1
        source = finalists[0]
        assert isinstance(source, dict)
        return {
            "qualification_job_id": snapshot["qualification_job_id"], "plan_id": snapshot["plan_id"],
            "status": snapshot["status"], "experiments": children,
            "progress": {"total_runs": total, "finished_points": finished, "running_points": running, "pending_points": pending},
            "qualification_study_id": study_id,
            "source_study_id": source["source_study_id"],
            "source_candidate_index": source["source_candidate_index"],
            "results_url": f"/api/qualification/jobs/{snapshot['qualification_job_id']}/results" if study_id else None,
            "resumable": snapshot["status"] == "cancelled", "error": snapshot.get("error"),
        }

    def qualification_results_payload(snapshot: dict[str, object]) -> dict[str, object]:
        study_id = snapshot.get("qualification_study_id")
        if snapshot.get("status") != "completed" or not isinstance(study_id, str):
            raise ValueError("qualification results are not complete")
        path = evidence_file(workspace / "runs", f"robust-selection-studies/{study_id}/robust_selection_results.json")
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("qualification results exceed the read budget")
        result = json.loads(path.read_text(encoding="utf-8"))
        finalists = result.get("finalists") if isinstance(result, dict) else None
        if not isinstance(finalists, list) or len(finalists) != 1 or not isinstance(finalists[0], dict):
            raise ValueError("qualification result is invalid")
        finalist = finalists[0]
        return {
            "study_id": study_id, "plan_id": snapshot["plan_id"],
            "complete_evidence": finalist.get("complete_evidence"),
            "worst_corner_yield": finalist.get("worst_corner_yield"),
            "worst_corner_confidence_low": finalist.get("worst_corner_confidence_low"),
            "corner_results": finalist.get("corner_results", []),
            "worst_requirements": finalist.get("worst_requirements", []),
            "dominant_sensitivities": finalist.get("dominant_sensitivities", []),
            "failed_points": [point for point in finalist.get("points", []) if isinstance(point, dict) and point.get("classification") != "pass"],
            "evidence": {
                "report": f"/evidence/robust-selection-studies/{study_id}/report.html",
                "json": f"/evidence/robust-selection-studies/{study_id}/robust_selection_results.json",
                "csv": f"/evidence/robust-selection-studies/{study_id}/robust_selection_results.csv",
                "plan": f"/evidence/robust-selection-plans/{snapshot['plan_id']}/robust_selection_plan.json",
            },
        }

    @app.middleware("http")
    async def local_security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        if not valid_host(request):
            return _json_error(400, "host_rejected", "host must be loopback-only")
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        if request.url.path.startswith("/evidence/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; "
                "form-action 'none'"
            )
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'"
            )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    app.include_router(
        create_core_router(
            workspace=workspace,
            session_token=session_token,
            static_root=STATIC_ROOT,
            project_root=PROJECT_ROOT,
            session_cookie=SESSION_COOKIE,
            font_assets=FONT_ASSETS,
            example_recipe=EXAMPLE_RECIPE,
            example_optimization_recipe=EXAMPLE_OPTIMIZATION_RECIPE,
            authorize_read=authorize_read,
        )
    )
    app.include_router(
        create_schematic_router(
            workspace=workspace,
            authorize_read=authorize_read,
            authorize_mutation=authorize_mutation,
            read_json_body=read_json_body,
            capture_builder=capture_builder,
        )
    )
    app.include_router(
        create_study_router(
            workspace=workspace,
            authorize_read=authorize_read,
            authorize_mutation=authorize_mutation,
            read_json_body=read_json_body,
            get_execution_manager=get_execution_manager,
            job_payload=job_payload,
            build_completed_report=build_completed_report,
            postprocess_states=postprocess_states,
            execution_lock=execution_lock,
            frozen_launches=frozen_launches,
            managed_jobs=managed_jobs,
        )
    )
    app.include_router(
        create_remote_router(
            workspace=workspace,
            authorize_read=authorize_read,
            authorize_mutation=authorize_mutation,
            read_json_body=read_json_body,
            execution_lock=execution_lock,
            frozen_launches=frozen_launches,
            remote_client=github_remote,
        )
    )
    app.include_router(
        create_optimization_router(
            workspace=workspace,
            authorize_read=authorize_read,
            authorize_mutation=authorize_mutation,
            read_json_body=read_json_body,
            optimization_experiments=optimization_experiments,
            validate_optimization_experiments=validate_optimization_experiments,
            get_optimization_manager=get_optimization_manager,
            optimization_job_payload=optimization_job_payload,
            optimization_results_payload=optimization_results_payload,
            execution_lock=execution_lock,
            frozen_optimization_launches=frozen_optimization_launches,
        )
    )
    app.include_router(
        create_qualification_router(
            workspace=workspace,
            authorize_read=authorize_read,
            authorize_mutation=authorize_mutation,
            read_json_body=read_json_body,
            optimization_experiments=optimization_experiments,
            get_qualification_manager=get_qualification_manager,
            qualification_job_payload=qualification_job_payload,
            qualification_results_payload=qualification_results_payload,
            execution_lock=execution_lock,
            frozen_qualification_launches=frozen_qualification_launches,
        )
    )

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
    print(f"LTspice System Builder: {url}", flush=True)
    print(f"Workspace: {args.workspace.resolve()}", flush=True)
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
