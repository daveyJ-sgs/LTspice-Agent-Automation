"""Pure GitHub Actions execution-preview contracts for System Builder."""

from __future__ import annotations

import base64
import json
import re
import zlib

from artifacts import canonical_bytes, content_address, sha256_digest

REMOTE_PREVIEW_SCHEMA_VERSION = 1
DEFAULT_REPOSITORY = "daveyJ-sgs/LTspice-Agent-Automation"
DEFAULT_REF = "main"
WORKFLOW_FILE = ".github/workflows/ltspice-windows-real.yml"
RUNNER = "windows-latest"
EVIDENCE_RETENTION_DAYS = 7
EVIDENCE_FORMATS = ["RAW", "LOG", "manifest", "JSON", "CSV", "HTML"]
REMOTE_ENVELOPE_SCHEMA_VERSION = 1
MAX_REMOTE_ENVELOPE_BYTES = 3 * 1024 * 1024
MAX_REMOTE_ENVELOPE_ENCODED_BYTES = 24 * 1024
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


def build_remote_envelope(
    *,
    preview: object,
    recipe: object,
    plan_artifact: bytes,
    experiments: object,
) -> dict[str, str | int]:
    """Encode one exact, bounded statistical study for workflow dispatch."""
    if not isinstance(preview, dict) or preview.get("status") != "preview":
        raise ValueError("a valid remote preview is required")
    if not isinstance(recipe, dict):
        raise ValueError("recipe must be an object")
    if not isinstance(experiments, list) or not experiments:
        raise ValueError("resolved experiments are required")
    plan = preview.get("plan")
    workload = preview.get("workload")
    if not isinstance(plan, dict) or not isinstance(workload, dict):
        raise ValueError("remote preview identity is incomplete")
    recipe_sha256 = sha256_digest(canonical_bytes(recipe))
    if recipe_sha256 != plan.get("recipe_sha256"):
        raise ValueError("recipe does not match the remote preview")
    plan_sha256 = sha256_digest(plan_artifact)
    if plan_sha256 != plan.get("plan_sha256"):
        raise ValueError("plan artifact does not match the remote preview")
    try:
        plan_value = json.loads(plan_artifact)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("plan artifact must be valid UTF-8 JSON") from exc
    if not isinstance(plan_value, dict):
        raise ValueError("plan artifact must contain an object")
    if plan.get("plan_id") != f"statistical-plan-{plan_sha256[:16]}":
        raise ValueError("plan content address does not match the remote preview")
    points = plan_value.get("points")
    if not isinstance(points, list) or len(points) != workload.get("point_count"):
        raise ValueError("plan point count does not match the remote preview")
    if len(experiments) != workload.get("experiment_count"):
        raise ValueError("experiment count does not match the remote preview")
    if workload.get("total_run_count") != len(points) * len(experiments):
        raise ValueError("total run count does not match the remote preview")
    for experiment in experiments:
        if (
            not isinstance(experiment, dict)
            or not isinstance(experiment.get("name"), str)
            or not isinstance(experiment.get("filename"), str)
            or not isinstance(experiment.get("netlist_template"), str)
            or not isinstance(experiment.get("waveform_analyses"), list)
        ):
            raise ValueError("resolved experiment is invalid")

    document: dict[str, object] = {
        "schema_version": REMOTE_ENVELOPE_SCHEMA_VERSION,
        "preview": preview,
        "recipe": recipe,
        "plan": plan_value,
        "experiments": experiments,
    }
    content = canonical_bytes(document)
    if len(content) > MAX_REMOTE_ENVELOPE_BYTES:
        raise ValueError("remote study envelope exceeds the decoded size limit")
    envelope_id, envelope_sha256 = content_address("remote-envelope", content)
    encoded = base64.b64encode(zlib.compress(content, level=9)).decode("ascii")
    if len(encoded) > MAX_REMOTE_ENVELOPE_ENCODED_BYTES:
        raise ValueError("remote study envelope exceeds the workflow input limit")
    return {
        "schema_version": REMOTE_ENVELOPE_SCHEMA_VERSION,
        "envelope_id": envelope_id,
        "envelope_sha256": envelope_sha256,
        "encoded": encoded,
        "decoded_bytes": len(content),
        "encoded_bytes": len(encoded),
    }


def decode_remote_envelope(
    encoded: object,
    expected_sha256: object,
) -> dict[str, object]:
    """Decode and verify one bounded workflow input without writing files."""
    if (
        not isinstance(encoded, str)
        or not encoded
        or len(encoded) > MAX_REMOTE_ENVELOPE_ENCODED_BYTES
    ):
        raise ValueError("remote study envelope input is invalid")
    if not isinstance(expected_sha256, str) or not SHA256_PATTERN.fullmatch(
        expected_sha256
    ):
        raise ValueError("remote study envelope SHA-256 is invalid")
    try:
        compressed = base64.b64decode(encoded, validate=True)
        decompressor = zlib.decompressobj()
        content = decompressor.decompress(compressed, MAX_REMOTE_ENVELOPE_BYTES + 1)
    except (ValueError, zlib.error) as exc:
        raise ValueError("remote study envelope is not valid compressed data") from exc
    if (
        len(content) > MAX_REMOTE_ENVELOPE_BYTES
        or not decompressor.eof
        or decompressor.unconsumed_tail
        or decompressor.unused_data
    ):
        raise ValueError("remote study envelope exceeds its decoded size limit")
    actual_sha256 = sha256_digest(content)
    if actual_sha256 != expected_sha256:
        raise ValueError("remote study envelope SHA-256 does not match")
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("remote study envelope is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("remote study envelope must contain an object")
    if document.get("schema_version") != REMOTE_ENVELOPE_SCHEMA_VERSION:
        raise ValueError("unsupported remote study envelope schema_version")
    if canonical_bytes(document) != content:
        raise ValueError("remote study envelope is not canonical")
    return document
