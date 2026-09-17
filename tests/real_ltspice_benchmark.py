#!/usr/bin/env python3
"""Identical real AC/transient smoke workload on hosted Mac and Windows runners."""
from __future__ import annotations

import json
import math
import os
import platform
import statistics
import time
from pathlib import Path

from real_ltspice_smoke import main as smoke


def main() -> None:
    root = Path(os.environ["REAL_LTSPICE_EVIDENCE_DIR"])
    root.mkdir(parents=True, exist_ok=True)
    samples = []
    # First pair warms filesystem/library caches; retain it but exclude from median.
    for index in range(6):
        directory = root / f"trial-{index}"
        os.environ["REAL_LTSPICE_EVIDENCE_DIR"] = str(directory)
        started = time.perf_counter()
        smoke()
        wall_seconds = time.perf_counter() - started
        summary = json.loads((directory / "smoke_summary.json").read_text())
        manifests = [json.loads(path.read_text()) for path in (
            directory / "run_manifest.json",
            directory / "compression-probe" / "run_manifest.json",
        )]
        if any(m["execution_source"] != "simulator" or m["cache"]["hit"] for m in manifests):
            raise AssertionError("benchmark requires real, uncached simulations")
        # The existing deck starts at 10 Hz: cutoff is -3 dB relative to 10 Hz,
        # not relative to DC. Keep that distinction explicit in the evidence.
        pole_hz = 1 / (2 * math.pi * 10000 * 1e-6)
        expected_cutoff = math.sqrt(10 ** (3 / 10) * (pole_hz**2 + 10**2) - pole_hz**2)
        if not math.isclose(summary["cutoff_frequency_hz"], expected_cutoff, rel_tol=0.002):
            raise AssertionError("RC cutoff disagrees with analytical transfer function")
        samples.append({
            "warmup": index == 0,
            "wall_seconds": wall_seconds,
            "ac_wrapper_seconds": manifests[0]["duration_seconds"],
            "transient_wrapper_seconds": manifests[1]["duration_seconds"],
            "netlist_sha256": [m["netlist_sha256"] for m in manifests],
            "cutoff_relative_to_10hz": summary["cutoff_frequency_hz"],
            "expected_cutoff_relative_to_10hz": expected_cutoff,
            "gain_at_1k_db": summary["gain_at_1k_db"],
        })
    measured = samples[1:]
    metrics = {
        key: {"median": statistics.median(s[key] for s in measured),
              "min": min(s[key] for s in measured),
              "max": max(s[key] for s in measured)}
        for key in ("wall_seconds", "ac_wrapper_seconds", "transient_wrapper_seconds")
    }
    report = {
        "platform": platform.platform(), "machine": platform.machine(),
        "logical_cpus": os.cpu_count(), "python": platform.python_version(),
        "runner_image": os.environ.get("ImageOS"),
        "runner_image_version": os.environ.get("ImageVersion"),
        "commit": os.environ.get("GITHUB_SHA"), "simulator": summary["simulator"],
        "samples": samples, "timings_seconds": metrics,
        "scope": "Launch, simulation and wrapper I/O; not isolated solver CPU time. "
                 "Different hardware and simulator versions; small RC workload only.",
    }
    (root / "benchmark.json").write_text(json.dumps(report, indent=2) + "\n")
    markdown = "### Real LTspice RC benchmark\n\n"
    markdown += f"{report['platform']} / {report['machine']} / {report['logical_cpus']} logical CPUs\n\n"
    markdown += f"Simulator: `{json.dumps(summary['simulator'], sort_keys=True)}`\n\n"
    markdown += "One warmup pair excluded; five measured AC/transient pairs.\n\n"
    markdown += "| Timing (seconds) | Median | Min | Max |\n|---|---:|---:|---:|\n"
    for name, values in metrics.items():
        markdown += f"| {name} | {values['median']:.4f} | {values['min']:.4f} | {values['max']:.4f} |\n"
    markdown += "\n" + report["scope"] + "\n"
    (root / "benchmark.md").write_text(markdown)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(markdown)
    print(markdown)


if __name__ == "__main__":
    main()
