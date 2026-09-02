"""Read-only workspace history for LTspice System Builder."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from experiment_index import MAX_QUERY_LIMIT, query_experiments, query_studies


EXPERIMENT_ID = re.compile(
    r"mcp-experiment-\d{8}-\d{6}-\d{6}(?:-[0-9a-fA-F]{8})?"
)
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
ACTIVE_STATUSES = {"queued", "running", "cancelling"}
KNOWN_STATUSES = {
    "defined",
    "queued",
    "running",
    "cancelling",
    *TERMINAL_STATUSES,
}
MAX_HISTORY_ITEMS = 50


def _integer(value: object, default: int = 0) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return default


def _timestamp(manifest: dict[str, object], experiment_id: str) -> str:
    for field in ("updated_at", "finished_at", "started_at", "created_at"):
        value = manifest.get(field)
        if isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc).isoformat()
            except ValueError:
                continue
    match = re.search(r"(\d{8})-(\d{6})-(\d{6})", experiment_id)
    if match is None:
        return "1970-01-01T00:00:00+00:00"
    date, clock, microseconds = match.groups()
    return (
        f"{date[:4]}-{date[4:6]}-{date[6:]}T{clock[:2]}:{clock[2:4]}:"
        f"{clock[4:]}.{microseconds}+00:00"
    )


def _read_manifest(path: Path) -> dict[str, object]:
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > 2 * 1024 * 1024
    ):
        raise ValueError("manifest is not a bounded regular file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("manifest must contain an object")
    return value


def workspace_history(workspace: Path, *, limit: int = 12) -> dict[str, object]:
    """Return recent durable jobs and indexed studies without changing artifacts."""
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= MAX_HISTORY_ITEMS
    ):
        raise ValueError(f"limit must be between 1 and {MAX_HISTORY_ITEMS}")
    workspace = workspace.resolve(strict=True)
    runs = workspace / "runs"
    if not runs.is_dir() or runs.is_symlink():
        return {
            "summary": {"total_jobs": 0, "active_jobs": 0, "reports": 0},
            "jobs": [],
            "studies": [],
            "index": {
                "available": False,
                "current": False,
                "unindexed_jobs": 0,
                "message": "No runs index is available yet.",
            },
            "issues": [],
        }

    indexed: dict[str, dict[str, object]] = {}
    studies: list[dict[str, object]] = []
    index_message = "No runs index is available yet."
    index_available = False
    database = runs / "experiments.sqlite3"
    if database.is_file() and not database.is_symlink():
        try:
            indexed_result = query_experiments(runs, limit=MAX_QUERY_LIMIT, offset=0)
            indexed = {
                str(record["experiment_id"]): record
                for record in indexed_result["experiments"]
            }
            study_result = query_studies(runs)
            studies = [
                {
                    **study,
                    "report_url": f"/evidence/{study['report_path']}",
                }
                for study in reversed(study_result["studies"][-limit:])
            ]
            index_available = True
            index_message = "Experiment index is available."
        except (OSError, ValueError) as exc:
            index_message = f"Experiment index could not be read: {exc}"

    jobs: list[dict[str, object]] = []
    issues: list[dict[str, str]] = []
    for manifest_path in runs.glob("mcp-experiment-*/experiment_manifest.json"):
        experiment_dir = manifest_path.parent
        experiment_id = experiment_dir.name
        try:
            if EXPERIMENT_ID.fullmatch(experiment_id) is None:
                raise ValueError("experiment directory name is invalid")
            if (
                experiment_dir.is_symlink()
                or experiment_dir.resolve().parent != runs.resolve()
            ):
                raise ValueError("experiment directory is not a direct regular child")
            manifest = _read_manifest(manifest_path)
            if manifest.get("experiment_id") != experiment_id:
                raise ValueError("manifest identity does not match its directory")
            status = manifest.get("status")
            if not isinstance(status, str) or status not in KNOWN_STATUSES:
                raise ValueError("manifest status is invalid")
            point_count = _integer(manifest.get("point_count"))
            running_points = _integer(manifest.get("running_points"))
            completed_points = _integer(manifest.get("completed_points"))
            error_points = _integer(manifest.get("error_points"))
            finished_points = _integer(
                manifest.get("finished_points"), completed_points + error_points
            )
            finished_points = min(point_count, finished_points)
            indexed_record = indexed.get(experiment_id, {})
            definition = manifest.get("definition")
            point_plan = (
                definition.get("point_plan")
                if isinstance(definition, dict)
                else None
            )
            source = point_plan.get("source") if isinstance(point_plan, dict) else None
            report_path = experiment_dir / "report.html"
            report_available = report_path.is_file() and not report_path.is_symlink()
            jobs.append(
                {
                    "experiment_id": experiment_id,
                    "status": status,
                    "recorded_at": _timestamp(manifest, experiment_id),
                    "point_count": point_count,
                    "finished_points": finished_points,
                    "running_points": min(running_points, point_count),
                    "pending_points": max(
                        0, point_count - finished_points - running_points
                    ),
                    "passed_points": _integer(manifest.get("passed_points")),
                    "failed_points": _integer(manifest.get("failed_points")),
                    "all_passed": manifest.get("all_passed")
                    if isinstance(manifest.get("all_passed"), bool)
                    else None,
                    "execution_mode": indexed_record.get(
                        "execution_mode",
                        manifest.get("definition", {}).get(
                            "execution_mode", "independent"
                        )
                        if isinstance(manifest.get("definition"), dict)
                        else "independent",
                    ),
                    "statistical": bool(indexed_record.get("statistical", False))
                    or (isinstance(source, dict) and source.get("kind") == "statistical"),
                    "observed_yield": indexed_record.get("observed_yield"),
                    "indexed": experiment_id in indexed,
                    "report_available": report_available,
                    "report_url": f"/evidence/{experiment_id}/report.html"
                    if report_available
                    else None,
                }
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            issues.append(
                {
                    "artifact": manifest_path.relative_to(runs).as_posix(),
                    "message": str(exc),
                }
            )

    jobs.sort(
        key=lambda item: (str(item["recorded_at"]), str(item["experiment_id"])),
        reverse=True,
    )
    reports = sum(bool(item["report_available"]) for item in jobs) + len(studies)
    active = sum(item["status"] in ACTIVE_STATUSES for item in jobs)
    unindexed_jobs = sum(not item["indexed"] for item in jobs)
    index_current = index_available and unindexed_jobs == 0
    if index_available and unindexed_jobs:
        job_word = "job" if unindexed_jobs == 1 else "jobs"
        index_message = (
            f"Index is readable but {unindexed_jobs} durable {job_word} are not indexed yet."
        )
    return {
        "summary": {"total_jobs": len(jobs), "active_jobs": active, "reports": reports},
        "jobs": jobs[:limit],
        "studies": studies,
        "index": {
            "available": index_available,
            "current": index_current,
            "unindexed_jobs": unindexed_jobs,
            "message": index_message,
        },
        "issues": issues[:10],
    }


def evidence_file(runs: Path, relative: str) -> Path:
    """Resolve one regular evidence file inside runs without following symlinks."""
    if not relative or "\\" in relative:
        raise ValueError("evidence path is invalid")
    try:
        runs = runs.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise ValueError("runs directory is missing") from exc
    if not runs.is_dir():
        raise ValueError("runs path is not a directory")
    candidate = runs / relative
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(runs)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise ValueError("evidence path is outside the runs directory or missing") from exc
    current = runs
    for part in candidate.relative_to(runs).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("symlinked evidence is not served")
    if not resolved.is_file():
        raise ValueError("evidence path is not a regular file")
    return resolved
