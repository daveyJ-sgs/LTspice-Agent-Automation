#!/usr/bin/env python3
"""Build a static HTML/JSON index of LTspice run manifests."""

from __future__ import annotations

import html
import json
import os
import uuid
from pathlib import Path
from urllib.parse import quote

from ltspice_wrapper import RUNS_DIR, parse_measurements


def _inside(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("dashboard artifact must remain inside the runs directory") from exc
    return resolved


def _write_text(path: Path, value: str) -> None:
    if path.is_symlink():
        raise ValueError("dashboard output path must not be a symlink")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(value, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def collect_records(root: Path = RUNS_DIR) -> list[dict[str, object]]:
    root = root.resolve()
    records: list[dict[str, object]] = []
    for manifest_path in root.rglob("run_manifest.json"):
        try:
            manifest_path = _inside(manifest_path, root)
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        run_dir = manifest_path.parent
        measurements: dict[str, float] = {}
        for log_path in sorted(run_dir.glob("*.log")):
            try:
                log_path = _inside(log_path, run_dir)
                measurements.update(parse_measurements(log_path))
            except (OSError, UnicodeError, ValueError):
                pass
        relative_run = run_dir.relative_to(root)
        artifacts = []
        result_files = manifest.get("result_files", [])
        if not isinstance(result_files, list):
            result_files = []
        for name in result_files:
            try:
                path = _inside(run_dir / str(name), run_dir)
            except ValueError:
                continue
            if path.is_file():
                relative_artifact = path.relative_to(root)
                artifacts.append(
                    {"name": str(name), "href": quote(relative_artifact.as_posix(), safe="/")}
                )
        records.append(
            {
                "run": str(relative_run),
                "status": manifest.get("status", "unknown"),
                "started_at": manifest.get("started_at", ""),
                "duration_seconds": manifest.get("duration_seconds"),
                "source_netlist": manifest.get("source_netlist", ""),
                "netlist_sha256": manifest.get("netlist_sha256", ""),
                "measurements": measurements,
                "artifacts": artifacts,
            }
        )
    return sorted(records, key=lambda record: str(record["started_at"]), reverse=True)


def write_dashboard(root: Path = RUNS_DIR) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    json_candidate = root / "index.json"
    html_candidate = root / "index.html"
    if json_candidate.is_symlink() or html_candidate.is_symlink():
        raise ValueError("dashboard output path must not be a symlink")
    json_path = _inside(json_candidate, root)
    html_path = _inside(html_candidate, root)
    records = collect_records(root)
    _write_text(json_path, json.dumps(records, indent=2, sort_keys=True) + "\n")

    rows = []
    for record in records:
        measurement_text = ", ".join(
            f"{html.escape(name)}={float(value):.6g}" for name, value in record["measurements"].items()
        ) or "-"
        links = " ".join(
            f'<a href="{artifact["href"]}">{html.escape(artifact["name"])}</a>'
            for artifact in record["artifacts"]
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(record['status']))}</td>"
            f"<td>{html.escape(str(record['started_at']))}</td>"
            f"<td>{html.escape(str(record['run']))}</td>"
            f"<td>{html.escape(str(record['duration_seconds']))}</td>"
            f"<td>{measurement_text}</td>"
            f"<td>{links}</td>"
            "</tr>"
        )
    _write_text(
        html_path,
        """<!doctype html>
<html lang="en"><meta charset="utf-8"><title>LTspice runs</title>
<style>body{font:15px system-ui;margin:2rem}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccc;padding:.45rem;text-align:left;vertical-align:top}th{background:#eee}code{font-size:.85em}</style>
<h1>LTspice runs</h1>
<p>Generated from <code>run_manifest.json</code> files. Machine-readable data: <a href="index.json">index.json</a>.</p>
<table><thead><tr><th>Status</th><th>Started</th><th>Run</th><th>Seconds</th><th>Measurements</th><th>Artifacts</th></tr></thead>
<tbody>"""
        + "\n".join(rows)
        + "</tbody></table></html>\n",
    )
    return html_path, json_path


def main() -> None:
    html_path, json_path = write_dashboard()
    print(f"HTML dashboard: {html_path}")
    print(f"JSON index: {json_path}")


if __name__ == "__main__":
    main()
