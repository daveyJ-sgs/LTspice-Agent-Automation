#!/usr/bin/env python3
"""Bounded real-solver comparison using two retained 100 MHz DAQ circuits."""
from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import shutil
import statistics
import time
from pathlib import Path

import numpy as np
from prepare_daq100_models import MODEL_HASHES

from ltspice_wrapper import run_netlist
from raw_parser import parse_raw

FIXTURES = Path(__file__).parent / "fixtures" / "daq100"


def metrics_for(case, raw):
    if raw.step_count != 1 or raw.points < 1000:
        raise AssertionError("Incomplete DAQ waveform")
    if not all(np.isfinite(values).all() for values in raw.values.values()):
        raise AssertionError("Non-finite DAQ waveform: cannot compare speed")
    if case == "ac":
        f = np.asarray(raw.values["frequency"]).real
        gain = np.asarray(raw.values["V(diff)"]) / raw.values["V(conn)"]
        db = 20 * np.log10(np.maximum(abs(gain), 1e-30))
        norm = db - db[0]
        if f[0] > 1000.01 or f[-1] < 1.99e9 or not np.isfinite(db).all():
            raise AssertionError("Incomplete or invalid AC sweep")
        i = int(np.flatnonzero(norm <= -3)[0])
        bw = float(np.interp(-3, [norm[i], norm[i-1]], [f[i], f[i-1]]))
        stop = np.r_[norm[(f >= 3e8) & (f <= 1e9)], np.interp([3e8, 1e9], f, norm)]
        band = np.r_[norm[(f >= 1e4) & (f <= 1e8)], np.interp([1e4, 1e8], f, norm)]
        if bw < 1e8 or band.min() < -0.5 or band.max() > 0.5 or -stop.max() < 60:
            raise AssertionError("AC engineering checks failed")
        return {"gain_dc_db": float(db[0]), "bandwidth_3db_hz": bw,
                "gain_100mhz_relative_db": float(np.interp(1e8, f, norm)),
                "minimum_rejection_300_to_1000mhz_db": float(-stop.max())}
    t = np.asarray(raw.values["time"])
    y = np.asarray(raw.values["V(diff)"])
    cm = (np.asarray(raw.values["V(adcp)"]) + raw.values["V(adcm)"]) / 2
    if t[0] != 0 or t[-1] < 599e-9 or np.any(np.diff(t) <= 0):
        raise AssertionError("Incomplete or non-monotonic transient")
    if y.min() < -1 or y.max() > 1 or cm.min() < 1.3 or cm.max() > 1.5:
        raise AssertionError("Transient ADC voltage envelope failed")
    keep = t >= 400e-9
    tt, yy = t[keep], y[keep]
    weights = np.gradient(tt)
    matrix = np.column_stack([np.ones_like(tt)] + [
        fun(2 * np.pi * 1e8 * h * tt)
        for h in range(1, 6) for fun in (np.sin, np.cos)])
    coeff = np.linalg.lstsq(matrix * np.sqrt(weights[:, None]), yy * np.sqrt(weights), rcond=None)[0]
    return {"fundamental_peak_V": float(np.hypot(coeff[1], coeff[2])),
            "output_min_V": float(y.min()), "output_max_V": float(y.max()),
            "adc_common_mode_min_V": float(cm.min()), "adc_common_mode_max_V": float(cm.max())}


def main():
    root = Path(os.environ["REAL_LTSPICE_EVIDENCE_DIR"])
    inputs = root / "inputs"
    provenance = json.loads((FIXTURES / "provenance.json").read_text())
    for name, digest in MODEL_HASHES.items():
        if hashlib.sha256((inputs / "models" / name).read_bytes()).hexdigest() != digest:
            raise AssertionError(f"Model mismatch: {name}")
    for case, info in provenance["cases"].items():
        source = FIXTURES / f"{case}.cir"
        if hashlib.sha256(source.read_bytes()).hexdigest() != info["fixture_sha256"]:
            raise AssertionError(f"Netlist mismatch: {case}")
        shutil.copyfile(source, inputs / source.name)
    samples = []
    for trial in range(4):
        for case, info in provenance["cases"].items():
            print(f"Trial {trial} {case} (warmup={trial == 0})", flush=True)
            start = time.perf_counter()
            directory = run_netlist(inputs / f"{case}.cir", root / f"trial-{trial}" / case,
                                   timeout_seconds=180, disable_compression=True)
            raw = parse_raw(directory / f"{case}.raw")
            metrics = metrics_for(case, raw)
            for name, actual in metrics.items():
                expected = info["reference_metrics"][name]
                tolerance = 0.05 if name.endswith("db") else max(abs(expected) * 0.01, 0.001)
                if not math.isclose(actual, expected, abs_tol=tolerance, rel_tol=0):
                    raise AssertionError(f"{case} {name}: {actual} vs reference {expected}")
            wall = time.perf_counter() - start
            manifest = json.loads((directory / "run_manifest.json").read_text())
            if manifest["execution_source"] != "simulator" or manifest["cache"]["hit"]:
                raise AssertionError("Real uncached simulations required")
            samples.append({"case": case, "trial": trial, "warmup": trial == 0,
                            "wrapper_seconds": manifest["duration_seconds"],
                            "wall_seconds": wall, "points": raw.points, "metrics": metrics})
            (root / "samples.json").write_text(json.dumps(samples, indent=2, allow_nan=False) + "\n")
    report = {"platform": platform.platform(), "machine": platform.machine(),
              "logical_cpus": os.cpu_count(), "python": platform.python_version(),
              "runner_image": os.environ.get("ImageOS"),
              "runner_image_version": os.environ.get("ImageVersion"),
              "commit": os.environ.get("GITHUB_SHA"), "simulator": manifest["simulator"],
              "models": MODEL_HASHES, "provenance": provenance, "samples": samples,
              "timings_seconds": {}}
    md = "### 100 MHz DAQ real-solver benchmark\n\n"
    md += f"{report['platform']} / {report['machine']} / {report['logical_cpus']} CPUs; LTspice {manifest['simulator']['version']}\n\n"
    md += "One warmup and three measured runs per case. All numerical checks passed.\n\n"
    md += "| Case | Wrapper median (s) | Min (s) | Max (s) | Including analysis median (s) |\n|---|---:|---:|---:|---:|\n"
    for case in provenance["cases"]:
        selected = [s for s in samples if s["case"] == case and not s["warmup"]]
        times = [s["wrapper_seconds"] for s in selected]
        values = {"median": statistics.median(times), "min": min(times), "max": max(times),
                  "wall_median": statistics.median(s["wall_seconds"] for s in selected)}
        report["timings_seconds"][case] = values
        md += f"| {case} | {values['median']:.4f} | {values['min']:.4f} | {values['max']:.4f} | {values['wall_median']:.4f} |\n"
    md += "\nDifferent hardware and simulator versions; wrapper timing includes launch and I/O. Model-level analog channel only, not complete DAQ hardware qualification.\n"
    (root / "benchmark.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    (root / "benchmark.md").write_text(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(md)
    print(md)


if __name__ == "__main__":
    main()
