from __future__ import annotations

import struct
import tempfile
import unittest
import json
import platform
import subprocess
from pathlib import Path
from unittest.mock import patch

from checks import assert_between, assert_close, floor, peak
import ltspice_wrapper
from ltspice_wrapper import (
    LTSPICE,
    parse_measurements,
    parse_step_values,
    parse_stepped_measurement_rows,
    parse_stepped_measurements,
    run_netlist,
)
from raw_parser import RawData, parse_raw, step_slices
from report_runs import collect_records
from examples.design_search_rc import choose_best


class AutomationTests(unittest.TestCase):
    def test_simulation_cache_is_disabled_by_default_and_never_stores_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LTspice.exe"
            executable.write_bytes(b"simulator")
            source = root / "filter.cir"
            source.write_text("R1 in out 1k\n.end\n", encoding="utf-8")
            cache = root / "cache"
            calls = 0

            def simulate(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                nonlocal calls
                calls += 1
                run_netlist_path = Path(command[-1])
                run_netlist_path.with_suffix(".log").write_text("gain=1\n")
                return subprocess.CompletedProcess(command, 0, "", "")

            ltspice_wrapper._simulator_metadata_cached.cache_clear()
            with (
                patch.object(ltspice_wrapper, "LTSPICE", executable),
                patch.object(ltspice_wrapper.subprocess, "run", side_effect=simulate),
            ):
                first = run_netlist(source, root / "first", cache_dir=cache)
                run_netlist(source, root / "second", cache_dir=cache)

            manifest = json.loads((first / "run_manifest.json").read_text())
            self.assertEqual(calls, 2)
            self.assertFalse(manifest["cache"]["requested"])
            self.assertFalse(cache.exists())

            with (
                patch.object(ltspice_wrapper, "LTSPICE", executable),
                patch.object(
                    ltspice_wrapper.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 1, "", "failed"),
                ),
                self.assertRaises(RuntimeError),
            ):
                run_netlist(
                    source,
                    root / "failed",
                    reuse_cache=True,
                    cache_dir=cache,
                )
            self.assertFalse(cache.exists())

    def test_simulation_cache_reuses_verified_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LTspice.exe"
            executable.write_bytes(b"simulator")
            source = root / "filter.cir"
            source.write_text("R1 in out 1k\n.end\n", encoding="utf-8")
            cache = root / "cache"
            calls = 0

            def simulate(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                nonlocal calls
                calls += 1
                run_netlist_path = Path(command[-1])
                run_netlist_path.with_suffix(".raw").write_bytes(b"waveform")
                run_netlist_path.with_suffix(".log").write_text(
                    "gain=1\n", encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            ltspice_wrapper._simulator_metadata_cached.cache_clear()
            with (
                patch.object(ltspice_wrapper, "LTSPICE", executable),
                patch.object(ltspice_wrapper.subprocess, "run", side_effect=simulate),
            ):
                first = run_netlist(
                    source,
                    root / "first",
                    reuse_cache=True,
                    cache_dir=cache,
                )
                second = run_netlist(
                    source,
                    root / "second",
                    reuse_cache=True,
                    cache_dir=cache,
                )

            first_manifest = json.loads((first / "run_manifest.json").read_text())
            second_manifest = json.loads((second / "run_manifest.json").read_text())
            self.assertEqual(calls, 1)
            self.assertFalse(first_manifest["cache"]["hit"])
            self.assertTrue(first_manifest["cache"]["stored"])
            self.assertEqual(first_manifest["execution_source"], "simulator")
            self.assertTrue(second_manifest["cache"]["hit"])
            self.assertFalse(second_manifest["cache"]["stored"])
            self.assertEqual(second_manifest["execution_source"], "cache")
            self.assertEqual((second / "filter.raw").read_bytes(), b"waveform")
            self.assertEqual(
                first_manifest["cache"]["key"], second_manifest["cache"]["key"]
            )

    def test_simulation_cache_invalidates_changed_inputs_and_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LTspice.exe"
            executable.write_bytes(b"simulator")
            model = root / "device.lib"
            model.write_text(".param RV=1k\n", encoding="utf-8")
            source = root / "filter.cir"
            source.write_text(
                f'.include "{model}"\nR1 in out {{RV}}\n.end\n', encoding="utf-8"
            )
            cache = root / "cache"
            calls = 0

            def simulate(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                nonlocal calls
                calls += 1
                run_netlist_path = Path(command[-1])
                run_netlist_path.with_suffix(".raw").write_bytes(str(calls).encode())
                run_netlist_path.with_suffix(".log").write_text("gain=1\n")
                return subprocess.CompletedProcess(command, 0, "", "")

            ltspice_wrapper._simulator_metadata_cached.cache_clear()
            with (
                patch.object(ltspice_wrapper, "LTSPICE", executable),
                patch.object(ltspice_wrapper.subprocess, "run", side_effect=simulate),
            ):
                run_netlist(source, root / "one", reuse_cache=True, cache_dir=cache)
                model.write_text(".param RV=2k\n", encoding="utf-8")
                run_netlist(source, root / "two", reuse_cache=True, cache_dir=cache)
                run_netlist(
                    source,
                    root / "three",
                    ascii_raw=True,
                    reuse_cache=True,
                    cache_dir=cache,
                )
                executable.write_bytes(b"simulator-version-two")
                run_netlist(
                    source,
                    root / "four",
                    reuse_cache=True,
                    cache_dir=cache,
                )

            self.assertEqual(calls, 4)
            self.assertEqual(len(list(cache.glob("simulation-*"))), 4)

    def test_simulation_cache_bypasses_unresolved_or_corrupt_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LTspice.exe"
            executable.write_bytes(b"simulator")
            source = root / "filter.cir"
            cache = root / "cache"
            calls = 0

            def simulate(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                nonlocal calls
                calls += 1
                run_netlist_path = Path(command[-1])
                run_netlist_path.with_suffix(".raw").write_bytes(b"waveform")
                run_netlist_path.with_suffix(".log").write_text("gain=1\n")
                return subprocess.CompletedProcess(command, 0, "", "")

            ltspice_wrapper._simulator_metadata_cached.cache_clear()
            with (
                patch.object(ltspice_wrapper, "LTSPICE", executable),
                patch.object(ltspice_wrapper.subprocess, "run", side_effect=simulate),
            ):
                source.write_text('.include "missing.lib"\n.end\n', encoding="utf-8")
                unresolved = run_netlist(
                    source,
                    root / "unresolved",
                    reuse_cache=True,
                    cache_dir=cache,
                )
                source.write_text("D1 in 0 DTEST\n.end\n", encoding="utf-8")
                model_dependent = run_netlist(
                    source,
                    root / "model-dependent",
                    reuse_cache=True,
                    cache_dir=cache,
                )
                source.write_text("R1 in out 1k\n.end\n", encoding="utf-8")
                first = run_netlist(
                    source,
                    root / "first",
                    reuse_cache=True,
                    cache_dir=cache,
                )
                first_manifest = json.loads((first / "run_manifest.json").read_text())
                cache_entry = cache / f"simulation-{first_manifest['cache']['key']}"
                next((cache_entry / "artifacts").iterdir()).write_bytes(b"corrupt")
                corrupt = run_netlist(
                    source,
                    root / "corrupt",
                    reuse_cache=True,
                    cache_dir=cache,
                )

            unresolved_manifest = json.loads(
                (unresolved / "run_manifest.json").read_text()
            )
            model_manifest = json.loads(
                (model_dependent / "run_manifest.json").read_text()
            )
            corrupt_manifest = json.loads((corrupt / "run_manifest.json").read_text())
            self.assertEqual(calls, 4)
            self.assertFalse(unresolved_manifest["cache"]["eligible"])
            self.assertIn("cannot be resolved", unresolved_manifest["cache"]["reason"])
            self.assertFalse(model_manifest["cache"]["eligible"])
            self.assertIn("model-dependent", model_manifest["cache"]["reason"])
            self.assertFalse(corrupt_manifest["cache"]["hit"])
            self.assertIn("integrity", corrupt_manifest["cache"]["reason"])

    def test_simulation_cache_rechecks_inputs_after_staging_a_hit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LTspice.exe"
            executable.write_bytes(b"simulator")
            dependency = root / "values.inc"
            dependency.write_text(".param RV=1k\n", encoding="utf-8")
            source = root / "filter.cir"
            source.write_text(
                f'.include "{dependency}"\nR1 in out {{RV}}\n.end\n',
                encoding="utf-8",
            )
            cache = root / "cache"
            calls = 0

            def simulate(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                nonlocal calls
                calls += 1
                run_netlist_path = Path(command[-1])
                run_netlist_path.with_suffix(".raw").write_bytes(str(calls).encode())
                run_netlist_path.with_suffix(".log").write_text("gain=1\n")
                return subprocess.CompletedProcess(command, 0, "", "")

            ltspice_wrapper._simulator_metadata_cached.cache_clear()
            with (
                patch.object(ltspice_wrapper, "LTSPICE", executable),
                patch.object(ltspice_wrapper.subprocess, "run", side_effect=simulate),
            ):
                run_netlist(source, root / "first", reuse_cache=True, cache_dir=cache)
                original_copy = ltspice_wrapper.shutil.copy2
                dependency_changed = False

                def copy_and_change_dependency(
                    source_path: Path, destination_path: Path
                ) -> Path:
                    nonlocal dependency_changed
                    copied = original_copy(source_path, destination_path)
                    if "artifacts" in Path(source_path).parts and not dependency_changed:
                        dependency.write_text(".param RV=2k\n", encoding="utf-8")
                        dependency_changed = True
                    return copied

                with patch.object(
                    ltspice_wrapper.shutil,
                    "copy2",
                    side_effect=copy_and_change_dependency,
                ):
                    second = run_netlist(
                        source,
                        root / "second",
                        reuse_cache=True,
                        cache_dir=cache,
                    )

            manifest = json.loads((second / "run_manifest.json").read_text())
            self.assertTrue(dependency_changed)
            self.assertEqual(calls, 2)
            self.assertEqual(manifest["execution_source"], "simulator")
            self.assertFalse(manifest["cache"]["hit"])

    def test_parse_complex_raw_file(self) -> None:
        header = """Title: test
Flags: complex forward
No. Variables: 2
No. Points: 2
Variables:
\t0\tfrequency\tfrequency
\t1\tV(out)\tvoltage
Binary:
"""
        binary = b"".join(
            struct.pack("<dd", *pair)
            for pair in ((10.0, 0.0), (1.0, -2.0), (20.0, 0.0), (3.0, -4.0))
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.raw"
            path.write_bytes(header.encode("utf-16le") + binary)
            data = parse_raw(path)

        self.assertEqual(data.points, 2)
        self.assertEqual(data.variables, ["frequency", "V(out)"])
        self.assertEqual(data.values["V(out)"], [complex(1, -2), complex(3, -4)])

    def test_parse_measurements(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.log"
            path.write_text(
                "gain: mag(v(out))=(-35.5dB,0°) at 1000\n"
                "tpd=2.5e-9 FROM 1e-6 TO 1.0025e-6\n",
                encoding="utf-16le",
            )
            self.assertEqual(parse_measurements(path), {"gain": -35.5, "tpd": 2.5e-9})

    def test_parse_stepped_log(self) -> None:
        log = """.step rval=1000
.step rval=2200
Measurement: gain_at_1k
  step\tmag(v(out))\tat
     1\t(-16.0722dB,0°)\t1000
     2\t(-22.8347dB,0°)\t1000
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "step.log"
            path.write_text(log, encoding="utf-16le")
            self.assertEqual(parse_step_values(path, "rval"), [1000.0, 2200.0])
            self.assertEqual(parse_stepped_measurements(path, "gain_at_1k"), [-16.0722, -22.8347])
            self.assertEqual(
                parse_stepped_measurement_rows(path),
                {"gain_at_1k": {1: -16.0722, 2: -22.8347}},
            )

    def test_step_slices_preserve_nonuniform_blocks(self) -> None:
        data = RawData(
            flags="real stepped",
            variables=["time", "V(out)"],
            values={
                "time": [0, 1, 2, 0, 0.5, 1.5, 2.5],
                "V(out)": [0, 1, 2, 10, 11, 12, 13],
            },
            step_count=2,
            points_per_step=None,
        )

        segments = step_slices(data)
        self.assertEqual([(part.start, part.stop) for part in segments], [(0, 3), (3, 7)])

    def test_stepped_measurement_rows_never_accept_numeric_prefixes_or_infinity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "step.log"
            path.write_text(
                "Measurement: gain\n step value\n 1 (1.#INF,0°)\n",
                encoding="utf-16le",
            )
            self.assertEqual(parse_stepped_measurement_rows(path), {"gain": {}})
            path.write_text(
                "Measurement: gain\n step value\n 1 1e999\n",
                encoding="utf-16le",
            )
            with self.assertRaisesRegex(ValueError, "Non-finite row 1"):
                parse_stepped_measurement_rows(path)

    def test_parse_real_compact_raw_file(self) -> None:
        header = """Title: test
Flags: real forward
No. Variables: 2
No. Points: 2
Variables:
\t0\ttime\ttime
\t1\tV(out)\tvoltage
Binary:
"""
        binary = struct.pack("<dfdf", 0.0, 1.5, 0.1, 2.5)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.raw"
            path.write_bytes(header.encode("utf-16le") + binary)
            data = parse_raw(path)

        self.assertEqual(data.values["time"], [0.0, 0.1])
        self.assertAlmostEqual(data.values["V(out)"][1], 2.5)

    def test_parse_ascii_raw_file(self) -> None:
        header = """Title: test
Flags: real forward
No. Variables: 2
No. Points: 2
Variables:
\t0\ttime\ttime
\t1\tV(out)\tvoltage
Values:
"""
        rows = "0\t0.0\n\t1.5\n1\t0.1\n\t2.5\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ascii.raw"
            path.write_bytes((header + rows).encode("utf-16le"))
            data = parse_raw(path)

        self.assertEqual(data.variables, ["time", "V(out)"])
        self.assertEqual(data.values["time"], [0.0, 0.1])
        self.assertEqual(data.values["V(out)"], [1.5, 2.5])

    def test_parse_real_double_precision_raw_file(self) -> None:
        header = """Title: test
Flags: real forward
No. Variables: 2
No. Points: 2
Variables:
\t0\ttime\ttime
\t1\tV(out)\tvoltage
Binary:
"""
        binary = struct.pack("<dddd", 0.0, 1.5, 0.1, 2.5)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.raw"
            path.write_bytes(header.encode("utf-16le") + binary)
            data = parse_raw(path)

        self.assertEqual(data.values["time"], [0.0, 0.1])
        self.assertEqual(data.values["V(out)"], [1.5, 2.5])

    def test_parse_fast_access_raw_file(self) -> None:
        header = """Title: test
Flags: real forward FastAccess
No. Variables: 2
No. Points: 2
Variables:
\t0\ttime\ttime
\t1\tV(out)\tvoltage
Binary:
"""
        binary = struct.pack("<ddff", 0.0, 0.1, 1.5, 2.5)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.raw"
            path.write_bytes(header.encode("utf-16le") + binary)
            data = parse_raw(path)

        self.assertEqual(data.values["time"], [0.0, 0.1])
        self.assertEqual(data.values["V(out)"], [1.5, 2.5])

    def test_checks(self) -> None:
        assert_close("gain", -35.0, -35.0, 0.1)
        assert_between("peak", 5.0, 4.9, 5.1)
        self.assertEqual(peak([1, 5, 2]), 5.0)
        self.assertEqual(floor([1, 5, 2]), 1.0)

    def test_choose_best_design_trial(self) -> None:
        rows = [
            {"gain_at_1k_db": -29.0},
            {"gain_at_1k_db": -30.1},
            {"gain_at_1k_db": -31.0},
        ]
        self.assertEqual(choose_best(rows, -30.0), rows[1])

    def test_manifest_dashboard_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "completed-run"
            run_dir.mkdir()
            (run_dir / "run_manifest.json").write_text(
                json.dumps(
                    {
                        "status": "completed",
                        "started_at": "2026-08-23T12:00:00+00:00",
                        "duration_seconds": 0.1,
                        "result_files": [],
                    }
                )
            )
            records = collect_records(Path(directory))

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "completed")

    @unittest.skipUnless(LTSPICE.is_file(), "LTspice integration test requires an installed simulator")
    def test_failed_run_writes_failed_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "invalid.cir"
            output = Path(directory) / "run"
            source.write_text("this is not a valid LTspice deck\n")
            with self.assertRaises(RuntimeError):
                run_netlist(source, output_dir=output)
            manifest = json.loads((output / "run_manifest.json").read_text())
            self.assertEqual(manifest["status"], "failed")
            # LTspice's failure exit code is platform-specific: 255 on the
            # macOS build, 1 on LTspice 26.0.2 for Windows. The contract worth
            # asserting is "non-zero", not one platform's particular value.
            self.assertNotEqual(manifest["returncode"], 0)
            self.assertEqual(manifest["runtime"]["operating_system"]["system"], platform.system())
            self.assertIn("python", manifest["runtime"])
            self.assertEqual(manifest["simulator"]["executable"], str(LTSPICE))
            self.assertEqual(len(manifest["simulator"]["executable_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
