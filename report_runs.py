#!/usr/bin/env python3
"""Build a static HTML/JSON index of LTspice run manifests."""

from __future__ import annotations

import html
import json
from pathlib import Path
from urllib.parse import quote

from ltspice_wrapper import RUNS_DIR, parse_measurements


def collect_records(root: Path = RUNS_DIR) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for manifest_path in root.rglob("run_manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        run_dir = manifest_path.parent
        measurements: dict[str, float] = {}
        for log_path in sorted(run_dir.glob("*.log")):
            try:
                measurements.update(parse_measurements(log_path))
            except (OSError, UnicodeError, ValueError):
                pass
        relative_run = run_dir.relative_to(root)
        artifacts = []
        for name in manifest.get("result_files", []):
            path = run_dir / str(name)
            if path.exists():
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
    records = collect_records(root)
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "index.json"
    json_path.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n")

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
    html_path = root / "index.html"
    html_path.write_text(
        """<!doctype html>
<html lang="en"><meta charset="utf-8"><title>LTspice runs</title>
<style>body{font:15px system-ui;margin:2rem}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccc;padding:.45rem;text-align:left;vertical-align:top}th{background:#eee}code{font-size:.85em}</style>
<h1>LTspice runs</h1>
<p>Generated from <code>run_manifest.json</code> files. Machine-readable data: <a href="index.json">index.json</a>.</p>
<table><thead><tr><th>Status</th><th>Started</th><th>Run</th><th>Seconds</th><th>Measurements</th><th>Artifacts</th></tr></thead>
<tbody>"""
        + "\n".join(rows)
        + "</tbody></table></html>\n"
    )
    return html_path, json_path


def main() -> None:
    html_path, json_path = write_dashboard()
    print(f"HTML dashboard: {html_path}")
    print(f"JSON index: {json_path}")


if __name__ == "__main__":
    main()
