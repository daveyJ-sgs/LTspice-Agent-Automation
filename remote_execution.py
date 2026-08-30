"""Pure GitHub Actions execution-preview contracts for System Builder."""

from __future__ import annotations

import re

from artifacts import canonical_bytes, content_address

REMOTE_PREVIEW_SCHEMA_VERSION = 1
DEFAULT_REPOSITORY = "daveyJ-sgs/LTspice-Agent-Automation"
DEFAULT_REF = "main"
WORKFLOW_FILE = ".github/workflows/ltspice-windows-real.yml"
RUNNER = "windows-latest"
EVIDENCE_RETENTION_DAYS = 7
EVIDENCE_FORMATS = ["RAW", "LOG", "manifest", "JSON", "CSV", "HTML"]
REPOSITORY_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})"
)
PLAN_ID_PATTERN = re.compile(r"statistical-plan-[0-9a-f]{16}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
INVALID_REF_CHARACTERS = set(" ~^:?*[\\")


def _validated_repository(value: object) -> str:
    if not isinstance(value, str) or not REPOSITORY_PATTERN.fullmatch(value):
        raise ValueError("repository must be a GitHub owner/name slug")
    return value


def _validated_ref(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 255:
        raise ValueError("ref must contain between 1 and 255 characters")
    if (
        value == "@"
        or value.startswith(("/", "."))
        or value.endswith(("/", ".", ".lock"))
        or ".." in value
        or "//" in value
        or "@{" in value
        or any(character in INVALID_REF_CHARACTERS for character in value)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("ref is not a valid Git reference")
    return value


def build_remote_preview(
    *,
    repository: object,
    ref: object,
    plan_id: object,
    plan_sha256: object,
    recipe_sha256: object,
    plan_artifact: object,
    point_count: object,
    experiment_count: object,
    total_run_count: object,
) -> dict[str, object]:
    """Build a deterministic, non-dispatchable GitHub execution preview."""
    repository_value = _validated_repository(repository)
    ref_value = _validated_ref(ref)
    if not isinstance(plan_id, str) or not PLAN_ID_PATTERN.fullmatch(plan_id):
        raise ValueError("plan ID is not a statistical plan identity")
    for value, label in (
        (plan_sha256, "plan SHA-256"),
        (recipe_sha256, "recipe SHA-256"),
    ):
        if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
            raise ValueError(f"{label} is invalid")
    if not isinstance(plan_artifact, str) or not plan_artifact.startswith(
        "runs/statistical-plans/"
    ):
        raise ValueError("plan artifact is not a statistical-plan path")
    for value, label in (
        (point_count, "point count"),
        (experiment_count, "experiment count"),
        (total_run_count, "total run count"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{label} must be a positive integer")

    contract: dict[str, object] = {
        "schema_version": REMOTE_PREVIEW_SCHEMA_VERSION,
        "status": "preview",
        "provider": "github_actions",
        "target": {
            "repository": repository_value,
            "ref": ref_value,
            "workflow": WORKFLOW_FILE,
            "runner": RUNNER,
            "workflow_status": "requires_gui_d4_plan_input",
        },
        "plan": {
            "plan_id": plan_id,
            "plan_sha256": plan_sha256,
            "recipe_sha256": recipe_sha256,
            "artifact": plan_artifact,
        },
        "workload": {
            "point_count": point_count,
            "experiment_count": experiment_count,
            "total_run_count": total_run_count,
        },
        "evidence": {
            "artifact_name": "real-ltspice-windows-${run_id}",
            "formats": list(EVIDENCE_FORMATS),
            "retention_days": EVIDENCE_RETENTION_DAYS,
        },
        "safety": {
            "dispatch_enabled": False,
            "external_request_made": False,
            "credentials_requested": False,
            "local_plan_modified": False,
        },
    }
    preview_id, preview_sha256 = content_address(
        "remote-preview", canonical_bytes(contract)
    )
    return {
        **contract,
        "preview_id": preview_id,
        "preview_sha256": preview_sha256,
    }
