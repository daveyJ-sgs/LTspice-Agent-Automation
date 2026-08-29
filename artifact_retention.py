#!/usr/bin/env python3
"""Inspect and safely prune generated LTspice run artifacts.

Only direct children with a valid, terminal manifest are managed.  The CLI is
dry-run by default; deletion requires an explicit ``--apply`` flag.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Literal


Scope = Literal["runs", "experiments", "cache"]
TERMINAL_STATUSES = {"completed", "failed", "error", "cancelled", "timeout"}
REFERENCE_FILENAMES = {
    "adaptive_manifest.json",
    "comparison.json",
    "optimization_comparison.json",
    "optimization_job.json",
    "optimization_results.json",
    "robust_selection_results.json",
}
EXPERIMENT_ID_PATTERN = re.compile(r"mcp-experiment-[A-Za-z0-9-]+")
MAX_REFERENCE_FILE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class ArtifactEntry:
    scope: Scope
    path: Path
    timestamp: datetime
    size_bytes: int
    manifest_sha256: str


@dataclass(frozen=True)
class PrunePlan:
    entries: tuple[ArtifactEntry, ...]
    reclaimed_bytes: int


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _directory_size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        if child.is_file() and not child.is_symlink():
            total += child.stat().st_size
    return total


def _load_manifest(path: Path) -> tuple[dict[str, object], str] | None:
    if not path.is_file() or path.is_symlink():
        return None
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    return value, hashlib.sha256(raw).hexdigest()


def _classify(directory: Path, runs_dir: Path) -> ArtifactEntry | None:
    """Return a managed entry only when its manifest proves it is terminal."""
    if directory.is_symlink() or not directory.is_dir():
        return None
    if directory.parent.resolve() != runs_dir.resolve():
        return None

    candidates: tuple[tuple[Scope, str], ...] = (
        ("runs", "run_manifest.json"),
        ("experiments", "experiment_manifest.json"),
    )
    for scope, filename in candidates:
        loaded = _load_manifest(directory / filename)
        if loaded is None:
            continue
        manifest, digest = loaded
        status = manifest.get("status")
        timestamp = _parse_timestamp(
            manifest.get("finished_at") or manifest.get("completed_at")
        )
        if status not in TERMINAL_STATUSES or timestamp is None:
            return None
        if scope == "experiments" and manifest.get("experiment_id") != directory.name:
            return None
        return ArtifactEntry(
            scope=scope,
            path=directory,
            timestamp=timestamp,
            size_bytes=_directory_size(directory),
            manifest_sha256=digest,
        )
    return None


def inventory(runs_dir: Path) -> tuple[tuple[ArtifactEntry, ...], int]:
    """Inventory managed terminal entries and count everything left unmanaged."""
    root = runs_dir.resolve()
    if runs_dir.is_symlink() or not root.is_dir():
        raise ValueError(f"runs directory must be a real directory: {runs_dir}")

    entries: list[ArtifactEntry] = []
    unmanaged = 0
    for child in runs_dir.iterdir():
        if child.name == "cache":
            if child.is_symlink() or not child.is_dir():
                unmanaged += 1
                continue
            for cache_entry in child.iterdir():
                loaded = _load_manifest(cache_entry / "cache_manifest.json")
                if (
                    cache_entry.is_symlink()
                    or not cache_entry.is_dir()
                    or not cache_entry.name.startswith("simulation-")
                    or loaded is None
                ):
                    unmanaged += 1
                    continue
                manifest, digest = loaded
                timestamp = _parse_timestamp(manifest.get("created_at"))
                if manifest.get("status") != "completed" or timestamp is None:
                    unmanaged += 1
                    continue
                entries.append(
                    ArtifactEntry(
                        scope="cache",
                        path=cache_entry,
                        timestamp=timestamp,
                        size_bytes=_directory_size(cache_entry),
                        manifest_sha256=digest,
                    )
                )
            continue

        entry = _classify(child, root)
        if entry is None:
            unmanaged += 1
        else:
            entries.append(entry)
    return tuple(entries), unmanaged


def _referenced_experiments(runs_dir: Path) -> set[str]:
    """Find experiment IDs retained by non-experiment study evidence."""
    referenced: set[str] = set()
    for path in runs_dir.rglob("*.json"):
        if path.name not in REFERENCE_FILENAMES:
            continue
        if path.is_symlink():
            raise ValueError(f"reference evidence must not be a symlink: {path}")
        try:
            if path.stat().st_size > MAX_REFERENCE_FILE_BYTES:
                raise ValueError(f"reference evidence is too large to inspect: {path}")
            referenced.update(EXPERIMENT_ID_PATTERN.findall(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"reference evidence cannot be inspected: {path}") from exc
    return referenced


def plan_prune(
    runs_dir: Path,
    *,
    scopes: Iterable[Scope],
    older_than_days: float,
    keep_recent: int,
    now: datetime | None = None,
) -> PrunePlan:
    if older_than_days < 0:
        raise ValueError("older_than_days must be non-negative")
    if keep_recent < 0:
        raise ValueError("keep_recent must be non-negative")
    selected_scopes = set(scopes)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=older_than_days)
    entries, _ = inventory(runs_dir)
    referenced_experiments = _referenced_experiments(runs_dir)
    targets: list[ArtifactEntry] = []
    for scope in sorted(selected_scopes):
        scoped = sorted(
            (entry for entry in entries if entry.scope == scope),
            key=lambda entry: (entry.timestamp, entry.path.name),
            reverse=True,
        )
        for entry in scoped[keep_recent:]:
            if (
                entry.timestamp < cutoff
                and not (
                    entry.scope == "experiments"
                    and entry.path.name in referenced_experiments
                )
            ):
                targets.append(entry)
    targets.sort(key=lambda entry: (entry.scope, entry.timestamp, entry.path.name))
    return PrunePlan(tuple(targets), sum(entry.size_bytes for entry in targets))


def apply_prune(runs_dir: Path, plan: PrunePlan) -> None:
    """Revalidate every manifest and boundary immediately before deletion."""
    root = runs_dir.resolve()
    current, _ = inventory(runs_dir)
    valid = {
        (entry.path.resolve(), entry.manifest_sha256): entry for entry in current
    }
    referenced_experiments = _referenced_experiments(runs_dir)
    cache_root = (root / "cache").resolve()
    for planned in plan.entries:
        resolved = planned.path.resolve()
        if (resolved, planned.manifest_sha256) not in valid:
            raise RuntimeError(f"artifact changed after planning: {planned.path}")
        if (
            planned.scope == "experiments"
            and planned.path.name in referenced_experiments
        ):
            raise RuntimeError(f"experiment became referenced after planning: {planned.path}")
        expected_parent = cache_root if planned.scope == "cache" else root
        if resolved.parent != expected_parent or planned.path.is_symlink():
            raise RuntimeError(f"refusing path outside managed boundary: {planned.path}")
    for entry in plan.entries:
        shutil.rmtree(entry.path)


def _human_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if amount < 1024 or unit == "GiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, default=Path(__file__).parent / "runs")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("inspect", help="summarize managed and unmanaged artifacts")
    prune = subparsers.add_parser("prune", help="plan cleanup; delete only with --apply")
    prune.add_argument(
        "--scope",
        action="append",
        choices=("runs", "experiments", "cache"),
        dest="scopes",
        help="managed class to prune; repeat as needed (default: all)",
    )
    prune.add_argument("--older-than-days", type=float, default=30)
    prune.add_argument("--keep-recent", type=int, default=10)
    prune.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "inspect":
        entries, unmanaged = inventory(args.runs_dir)
        by_scope = {
            scope: {
                "entries": sum(entry.scope == scope for entry in entries),
                "bytes": sum(entry.size_bytes for entry in entries if entry.scope == scope),
            }
            for scope in ("runs", "experiments", "cache")
        }
        print(json.dumps({"managed": by_scope, "unmanaged_entries": unmanaged}, indent=2))
        return 0

    plan = plan_prune(
        args.runs_dir,
        scopes=args.scopes or ("runs", "experiments", "cache"),
        older_than_days=args.older_than_days,
        keep_recent=args.keep_recent,
    )
    action = "deleted" if args.apply else "would delete"
    print(f"{action} {len(plan.entries)} entries ({_human_bytes(plan.reclaimed_bytes)})")
    for entry in plan.entries:
        print(f"{entry.scope}\t{entry.timestamp.isoformat()}\t{entry.path}")
    if args.apply:
        apply_prune(args.runs_dir, plan)
    else:
        print("Dry run only. Repeat with --apply to delete these exact eligible classes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
