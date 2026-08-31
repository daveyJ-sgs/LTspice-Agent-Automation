"""Verified execution and evidence contracts for remote statistical studies."""

from __future__ import annotations

import json
import os
from pathlib import Path

import artifacts
import experiment_engine
import experiment_report
import mcp_server
import sensitivity_analysis
import statistical_engine
import statistical_results
import worst_case_analysis
from remote_execution import build_remote_preview

REMOTE_EVIDENCE_MANIFEST_SCHEMA_VERSION = 1
MAX_REMOTE_EVIDENCE_FILES = 4096
MAX_REMOTE_EVIDENCE_BYTES = 512 * 1024 * 1024


def validate_remote_document(document: object) -> dict[str, object]:
    """Validate identities and executable definitions without running LTspice."""
    if not isinstance(document, dict):
        raise ValueError("remote study envelope must contain an object")
    preview = document.get("preview")
    recipe = document.get("recipe")
    plan = document.get("plan")
    experiments = document.get("experiments")
    if not all(isinstance(value, dict) for value in (preview, recipe, plan)):
        raise ValueError("remote study identity is incomplete")
    if not isinstance(experiments, list) or not experiments:
        raise ValueError("remote study experiments are missing")
    assert isinstance(preview, dict)
    assert isinstance(recipe, dict)
    assert isinstance(plan, dict)
    plan_identity = preview.get("plan")
    target = preview.get("target")
    workload = preview.get("workload")
    if not all(isinstance(value, dict) for value in (plan_identity, target, workload)):
        raise ValueError("remote preview contract is incomplete")
    assert isinstance(plan_identity, dict)
    assert isinstance(target, dict)
    assert isinstance(workload, dict)

    rebuilt_preview = build_remote_preview(
        repository=target.get("repository"),
        ref=target.get("ref"),
        plan_id=plan_identity.get("plan_id"),
        plan_sha256=plan_identity.get("plan_sha256"),
        recipe_sha256=plan_identity.get("recipe_sha256"),
        plan_artifact=plan_identity.get("artifact"),
        point_count=workload.get("point_count"),
        experiment_count=workload.get("experiment_count"),
        total_run_count=workload.get("total_run_count"),
    )
    if rebuilt_preview != preview:
        raise ValueError("remote preview identity does not match its content")
    if artifacts.definition_hash(recipe) != plan_identity["recipe_sha256"]:
        raise ValueError("remote recipe SHA-256 does not match")
    plan_artifact = statistical_engine._artifact_bytes(plan)  # type: ignore[arg-type]
    if artifacts.sha256_digest(plan_artifact) != plan_identity["plan_sha256"]:
        raise ValueError("remote statistical plan SHA-256 does not match")
    if plan_identity["plan_id"] != (
        f"statistical-plan-{str(plan_identity['plan_sha256'])[:16]}"
    ):
        raise ValueError("remote statistical plan content address does not match")

    points = plan.get("points")
    parameter_order = plan.get("parameter_order")
    parameter_units = plan.get("parameter_units")
    if (
        not isinstance(points, list)
        or not isinstance(parameter_order, list)
        or not isinstance(parameter_units, dict)
        or len(points) != workload.get("point_count")
        or len(experiments) != workload.get("experiment_count")
        or len(points) * len(experiments) != workload.get("total_run_count")
    ):
        raise ValueError("remote workload does not match the submitted study")
    point_parameters: list[dict[str, object]] = []
    for point in points:
        if not isinstance(point, dict) or not isinstance(point.get("parameters"), dict):
            raise ValueError("remote statistical point is invalid")
        point_parameters.append(point["parameters"])

    recipe_experiments = recipe.get("experiments")
    execution = recipe.get("execution", {})
    if not isinstance(recipe_experiments, list) or not isinstance(execution, dict):
        raise ValueError("remote recipe execution definition is invalid")
    if len(recipe_experiments) != len(experiments):
        raise ValueError("remote recipe and resolved experiments differ")
    max_concurrency = execution.get("max_concurrency", 2)
    reuse_cache = execution.get("reuse_cache", False)
    if (
        isinstance(max_concurrency, bool)
        or not isinstance(max_concurrency, int)
        or not 1 <= max_concurrency <= 8
        or not isinstance(reuse_cache, bool)
    ):
        raise ValueError("remote execution settings are invalid")

    names: set[str] = set()
    for recipe_experiment, experiment in zip(recipe_experiments, experiments):
        if not isinstance(recipe_experiment, dict) or not isinstance(experiment, dict):
            raise ValueError("remote experiment is invalid")
        name = experiment.get("name")
        filename = experiment.get("filename")
        netlist = experiment.get("netlist_template")
        analyses = experiment.get("waveform_analyses")
        if (
            not isinstance(name, str)
            or not name
            or name in names
            or not isinstance(filename, str)
            or Path(filename).name != filename
            or Path(filename).suffix.lower() not in {".cir", ".net"}
            or not isinstance(netlist, str)
            or len(netlist.encode("utf-8")) > 2 * 1024 * 1024
            or not isinstance(analyses, list)
        ):
            raise ValueError("remote experiment definition is invalid")
        if (
            recipe_experiment.get("name") != name
            or recipe_experiment.get("filename") != filename
            or recipe_experiment.get("waveform_analyses") != analyses
        ):
            raise ValueError("remote experiment differs from its recipe")
        experiment_engine._prepare_explicit_experiment(
            netlist,
            parameter_order,  # type: ignore[arg-type]
            point_parameters,  # type: ignore[arg-type]
            parameter_units,  # type: ignore[arg-type]
            analyses,  # type: ignore[arg-type]
        )
        names.add(name)
    return document


def _report_context(recipe: dict[str, object], experiment_name: str) -> dict[str, str]:
    raw = recipe.get("report_context", {})
    if not isinstance(raw, dict):
        raise ValueError("remote report context is invalid")
    context = {
        str(name): str(value)
        for name, value in raw.items()
        if name in experiment_report.ReportContext.__annotations__
        and isinstance(value, str)
    }
    context.setdefault(
        "simulation_summary",
        f"The {experiment_name} analysis evaluates every submitted statistical "
        "point and named operating corner on the GitHub Windows runner.",
    )
    context.setdefault(
        "mcp_context",
        "System Builder dispatched the same immutable plan and portable evidence "
        "contract used by local and MCP execution.",
    )
    return context


def run_remote_study(document: object, evidence_dir: Path) -> dict[str, object]:
    """Run a verified remote document and produce portable statistical evidence."""
    validated = validate_remote_document(document)
    preview = validated["preview"]
    recipe = validated["recipe"]
    plan = validated["plan"]
    experiments = validated["experiments"]
    assert isinstance(preview, dict)
    assert isinstance(recipe, dict)
    assert isinstance(plan, dict)
    assert isinstance(experiments, list)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    if evidence_dir.is_symlink():
        raise ValueError("remote evidence directory must not be a symlink")

    mcp_server.RUNS_DIR = evidence_dir
    mcp_server._experiment_manager = None
    published = statistical_engine.save_statistical_plan(
        evidence_dir, plan  # type: ignore[arg-type]
    )
    statistical_engine.load_statistical_plan(evidence_dir, str(published["plan_id"]))
    plan_identity = preview["plan"]
    assert isinstance(plan_identity, dict)
    if (
        published["plan_id"] != plan_identity["plan_id"]
        or published["plan_sha256"] != plan_identity["plan_sha256"]
    ):
        raise ValueError("published remote plan differs from the submitted plan")

    execution = recipe.get("execution", {})
    assert isinstance(execution, dict)
    outputs: list[dict[str, object]] = []
    for experiment in experiments:
        assert isinstance(experiment, dict)
        result = mcp_server.run_statistical_experiment(
            str(published["plan_id"]),
            str(experiment["netlist_template"]),
            experiment["waveform_analyses"],  # type: ignore[arg-type]
            filename=str(experiment["filename"]),
            reuse_cache=bool(execution.get("reuse_cache", False)),
        )
        experiment_id = result["experiment_id"]
        summary = statistical_results.summarize_statistical_experiment(
            evidence_dir, experiment_id
        )
        worst = worst_case_analysis.analyze_statistical_worst_cases(
            evidence_dir, experiment_id
        )
        sensitivity = sensitivity_analysis.analyze_statistical_sensitivity(
            evidence_dir, experiment_id
        )
        report = experiment_report.build_experiment_report(
            evidence_dir,
            experiment_id,
            _report_context(recipe, str(experiment["name"])),
        )
        outputs.append(
            {
                "name": experiment["name"],
                "experiment_id": experiment_id,
                "status": result["status"],
                "point_count": result["point_count"],
                "completed_points": result["completed_points"],
                "error_points": result["error_points"],
                "invalid_points": summary["invalid_points"],
                "all_passed": result["all_passed"],
                "report_html": str(
                    Path(report["report_html"]).relative_to(evidence_dir)
                ),
                "worst_cases": str(
                    Path(worst["worst_cases_json"]).relative_to(evidence_dir)
                ),
                "sensitivity": str(
                    Path(sensitivity["sensitivity_json"]).relative_to(evidence_dir)
                ),
            }
        )

    summary_document: dict[str, object] = {
        "schema_version": 1,
        "provider": "github_actions",
        "github_run_id": os.environ.get("GITHUB_RUN_ID", "local-test"),
        "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
        "preview_id": preview["preview_id"],
        "preview_sha256": preview["preview_sha256"],
        "plan_id": plan_identity["plan_id"],
        "plan_sha256": plan_identity["plan_sha256"],
        "recipe_sha256": plan_identity["recipe_sha256"],
        "workload": preview["workload"],
        "experiments": outputs,
    }
    summary_path = evidence_dir / "remote_execution_summary.json"
    summary_path.write_bytes(
        artifacts.canonical_bytes(
            summary_document, pretty=True, trailing_newline=True
        )
    )
    write_remote_evidence_manifest(evidence_dir, summary_document)
    return summary_document


def write_remote_evidence_manifest(
    evidence_dir: Path, summary: dict[str, object]
) -> dict[str, object]:
    """Hash every retained regular file except the manifest itself."""
    root = evidence_dir.resolve(strict=True)
    manifest_path = root / "remote_evidence_manifest.json"
    files: list[dict[str, object]] = []
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        if path == manifest_path:
            continue
        if path.is_symlink():
            raise ValueError("remote evidence must not contain symbolic links")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        total_bytes += size
        if len(files) >= MAX_REMOTE_EVIDENCE_FILES:
            raise ValueError("remote evidence contains too many files")
        if total_bytes > MAX_REMOTE_EVIDENCE_BYTES:
            raise ValueError("remote evidence exceeds the retained size limit")
        files.append(
            {
                "path": relative,
                "size_bytes": size,
                "sha256": artifacts.sha256_digest(path.read_bytes()),
            }
        )
    manifest: dict[str, object] = {
        "schema_version": REMOTE_EVIDENCE_MANIFEST_SCHEMA_VERSION,
        "summary_sha256": artifacts.definition_hash(summary),
        "file_count": len(files),
        "total_bytes": total_bytes,
        "files": files,
    }
    manifest_path.write_bytes(
        artifacts.canonical_bytes(manifest, pretty=True, trailing_newline=True)
    )
    return manifest


def verify_remote_evidence(
    evidence_dir: Path,
    *,
    expected_preview_id: str,
    expected_preview_sha256: str,
    expected_run_id: str,
) -> dict[str, object]:
    """Verify a downloaded artifact before it is admitted to a workspace."""
    root = evidence_dir.resolve(strict=True)
    manifest_path = root / "remote_evidence_manifest.json"
    summary_path = root / "remote_execution_summary.json"
    if any(path.is_symlink() for path in root.rglob("*")):
        raise ValueError("downloaded remote evidence contains a symbolic link")
    if not manifest_path.is_file() or not summary_path.is_file():
        raise ValueError("downloaded remote evidence is incomplete")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not isinstance(summary, dict):
        raise ValueError("downloaded remote evidence metadata is invalid")
    if (
        manifest.get("schema_version") != REMOTE_EVIDENCE_MANIFEST_SCHEMA_VERSION
        or manifest.get("summary_sha256") != artifacts.definition_hash(summary)
        or summary.get("preview_id") != expected_preview_id
        or summary.get("preview_sha256") != expected_preview_sha256
        or str(summary.get("github_run_id")) != expected_run_id
    ):
        raise ValueError("downloaded remote evidence identity does not match")
    entries = manifest.get("files")
    if not isinstance(entries, list) or len(entries) > MAX_REMOTE_EVIDENCE_FILES:
        raise ValueError("downloaded remote evidence file list is invalid")
    expected_paths: set[str] = set()
    total_bytes = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("downloaded remote evidence entry is invalid")
        relative = entry.get("path")
        if not isinstance(relative, str) or not relative or "\\" in relative:
            raise ValueError("downloaded remote evidence path is invalid")
        if relative in expected_paths:
            raise ValueError("downloaded remote evidence path is duplicated")
        candidate = root.joinpath(*Path(relative).parts)
        try:
            candidate.resolve(strict=True).relative_to(root)
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise ValueError("downloaded remote evidence path escapes its root") from exc
        if not candidate.is_file() or candidate.is_symlink():
            raise ValueError("downloaded remote evidence file is not regular")
        content = candidate.read_bytes()
        total_bytes += len(content)
        if (
            entry.get("size_bytes") != len(content)
            or entry.get("sha256") != artifacts.sha256_digest(content)
        ):
            raise ValueError("downloaded remote evidence file integrity failed")
        expected_paths.add(relative)
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path != manifest_path
    }
    if (
        expected_paths != actual_paths
        or manifest.get("file_count") != len(entries)
        or manifest.get("total_bytes") != total_bytes
        or total_bytes > MAX_REMOTE_EVIDENCE_BYTES
    ):
        raise ValueError("downloaded remote evidence inventory does not match")
    return summary
