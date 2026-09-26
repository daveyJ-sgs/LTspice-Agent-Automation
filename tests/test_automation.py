from __future__ import annotations

import math
import struct
import tempfile
import unittest
import json
import platform
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from checks import assert_between, assert_close, floor, peak
import ltspice_wrapper
import experiment_engine
from ltspice_wrapper import (
    LTSPICE,
    parse_measurements,
    parse_step_values,
    parse_stepped_measurement_rows,
    parse_stepped_measurements,
    run_netlist,
)
from raw_parser import RawData, parse_raw, step_slices
from report_runs import collect_records, write_dashboard
from examples.design_search_rc import choose_best


class AutomationTests(unittest.TestCase):
    def test_all_measurement_parsers_reject_nonfinite_numeric_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.log"
            for value in ("(1.#INF,0°)", "1e+", "1.2.3"):
                path.write_text(f"gain={value}\n", encoding="utf-8")
                self.assertEqual(parse_measurements(path), {})
                path.write_text(f"Measurement: gain\n step value\n 1 {value}\n", encoding="utf-8")
                self.assertEqual(parse_stepped_measurements(path, "gain"), [])
                self.assertEqual(parse_stepped_measurement_rows(path), {"gain": {}})
            for scalar in (True, False):
                path.write_text("gain=1e999\n" if scalar else "Measurement: gain\n step value\n 1 1e999\n")
                with self.assertRaises(ValueError):
                    if scalar:
                        parse_measurements(path)
                    else:
                        parse_stepped_measurements(path, "gain")

    def test_stepped_operating_points_are_separate_single_point_blocks(self) -> None:
        header = "Title: stepped OP\nPlotname: Operating Point\nFlags: real stepped\nNo. Variables: 2\nNo. Points: 3\nVariables:\n0 r param\n1 V(out) voltage\n"
        for axis in ([1000, 2000, 3000], [3000, 1000, 2000]):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "op.raw"
                for binary in (False, True):
                    with self.subTest(axis=axis, binary=binary):
                        payload = (b"".join(struct.pack("<df", value, index) for index, value in enumerate(axis))
                                   if binary else "".join(f"{index} {value}\n {index}\n" for index, value in enumerate(axis)).encode())
                        path.write_bytes((header + ("Binary:\n" if binary else "Values:\n")).encode() + payload)
                        data = parse_raw(path)
                        self.assertEqual((data.step_count, data.points_per_step), (3, 1))
                        self.assertEqual([data.values["V(out)"][part] for part in step_slices(data)], [[0], [1], [2]])

    def test_raw_rejects_invalid_dimensions_duplicate_vectors_and_rows(self) -> None:
        header = "Title: test\nFlags: real\nNo. Variables: 2\nNo. Points: 2\nVariables:\n0 time time\n1 V(out) voltage\n"
        values = "Values:\n0 0\n 1\n1 1\n 2\n"
        malformed = [
            (header + values).replace("No. Points: 2", "No. Points: 0"),
            (header + values).replace("No. Points: 2", "No. Points: -1"),
            (header + values).replace("V(out)", "time"),
            (header + values).replace("1 V(out)", "3 V(out)"),
            header + values.replace("1 1", "9 1"),
            header + values + "2 2\n 3\n",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid.raw"
            for document in malformed:
                with self.subTest(document=document):
                    path.write_text(document)
                    with self.assertRaises(ValueError):
                        parse_raw(path)
            # A partially written double-precision payload must not be
            # silently decoded as compact float data.
            path.write_bytes((header + "Binary:\n").encode() + struct.pack("<dddd", 0, 1, 1, 2)[:-1])
            with self.assertRaises(ValueError):
                parse_raw(path)

    def test_descending_dc_preserves_single_and_multiple_sweeps(self) -> None:
        for stepped in (False, True):
            axis = [2, 1, 0] * (2 if stepped else 1)
            header = (f"Title: DC\nFlags: real{' stepped' if stepped else ''}\n"
                      f"No. Variables: 2\nNo. Points: {len(axis)}\n"
                      "Variables:\n0 V(in) voltage\n1 V(out) voltage\n")
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "dc.raw"
                for binary in (False, True):
                    payload = (b"".join(struct.pack("<df", x, x / 2) for x in axis) if binary else
                               "".join(f"{i} {x}\n{x / 2}\n" for i, x in enumerate(axis)).encode())
                    path.write_bytes((header + ("Binary:\n" if binary else "Values:\n")).encode() + payload)
                    data = parse_raw(path)
                    self.assertEqual(data.step_count, 2 if stepped else 1)
                    self.assertEqual(data.points_per_step, 3)
                    self.assertEqual([data.values["V(in)"][part] for part in step_slices(data)],
                                     [[2, 1, 0]] * data.step_count)

    def test_netlist_staging_decodes_utf16_and_keeps_include_paths(self) -> None:
        text = '* Circuit µ\n.include "model.inc"\n.end\n'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "model.inc").write_text("R1 out 0 1k\n")
            for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-16le"):
                with self.subTest(encoding=encoding):
                    source = root / "source.cir"
                    source.write_bytes(text.encode(encoding))
                    staged = root / "staged.cir"
                    ltspice_wrapper._stage_netlist(source, staged)
                    result = staged.read_text(encoding="utf-8")
                    self.assertNotIn("\0", result)
                    self.assertIn("µ", result)
                    self.assertIn(str((root / "model.inc").resolve()), result)

    def _slow_simulator(self, root: Path) -> Path:
        executable = root / "slow-ltspice"
        executable.write_text(
            f"#!{sys.executable}\nimport time\ntime.sleep(30)\n", encoding="utf-8"
        )
        executable.chmod(0o755)
        return executable

    @unittest.skipIf(sys.platform == "win32", "uses a POSIX script as the simulator")
    def test_cancel_stops_a_running_simulator_promptly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "circuit.cir"
            source.write_text("* Test\nR1 in 0 1k\n.end\n", encoding="utf-8")
            cancel = threading.Event()
            timer = threading.Timer(0.3, cancel.set)
            timer.start()
            started = time.monotonic()
            try:
                with (
                    patch.object(ltspice_wrapper, "LTSPICE", self._slow_simulator(root)),
                    self.assertRaises(ltspice_wrapper.SimulationCancelled),
                ):
                    run_netlist(source, root / "run", cancel_event=cancel)
            finally:
                timer.cancel()
            self.assertLess(time.monotonic() - started, 10)
            manifest = json.loads((root / "run" / "run_manifest.json").read_text())
            self.assertEqual(manifest["status"], "cancelled")
            self.assertIn("finished_at", manifest)

    @unittest.skipIf(sys.platform == "win32", "uses a POSIX script as the simulator")
    def test_timeout_kills_the_simulator_instead_of_waiting_for_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "circuit.cir"
            source.write_text("* Test\nR1 in 0 1k\n.end\n", encoding="utf-8")
            started = time.monotonic()
            with (
                patch.object(ltspice_wrapper, "LTSPICE", self._slow_simulator(root)),
                self.assertRaisesRegex(RuntimeError, "exceeded 1 seconds"),
            ):
                run_netlist(source, root / "run", timeout_seconds=1)
            self.assertLess(time.monotonic() - started, 10)
            manifest = json.loads((root / "run" / "run_manifest.json").read_text())
            self.assertEqual(manifest["status"], "timeout")

    def test_json_publication_retries_a_briefly_locked_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "experiment_manifest.json"
            real_replace = experiment_engine.os.replace
            failures = [PermissionError("locked"), PermissionError("locked")]

            def flaky_replace(source: object, destination: object) -> None:
                if failures:
                    raise failures.pop()
                real_replace(source, destination)

            with patch.object(experiment_engine.os, "replace", side_effect=flaky_replace):
                experiment_engine._write_json(target, {"status": "running"})
            self.assertEqual(json.loads(target.read_text())["status"], "running")
            self.assertEqual(list(Path(tmp).iterdir()), [target])

    def test_process_launch_os_errors_write_terminal_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "circuit.cir"
            source.write_text("* Test\nR1 in 0 1k\n.end\n")
            for index, error in enumerate((PermissionError("denied"), FileNotFoundError("removed"))):
                output = root / f"run-{index}"
                with patch.object(ltspice_wrapper, "LTSPICE", Path(sys.executable)), patch.object(
                    ltspice_wrapper, "_run_simulator", side_effect=error
                ), self.assertRaisesRegex(RuntimeError, "could not be launched"):
                    run_netlist(source, output)
                manifest = json.loads((output / "run_manifest.json").read_text())
                self.assertEqual(manifest["status"], "failed")
                self.assertIn("finished_at", manifest)
                self.assertGreaterEqual(manifest["duration_seconds"], 0)


    def test_experiment_manager_lock_excludes_another_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manager.lock"
            lock = experiment_engine._RunsProcessLock(path)
            probe = (
                "from pathlib import Path\n"
                "from experiment_engine import _RunsProcessLock\n"
                f"path = Path({str(path)!r})\n"
                "try:\n"
                "    lock = _RunsProcessLock(path)\n"
                "except RuntimeError:\n"
                "    raise SystemExit(0)\n"
                "lock.release()\n"
                "raise SystemExit(1)\n"
            )
            try:
                blocked = subprocess.run(
                    [sys.executable, "-c", probe],
                    cwd=Path(__file__).parents[1],
                    check=False,
                )
            finally:
                lock.release()
            self.assertEqual(blocked.returncode, 0)

            available = subprocess.run(
                [sys.executable, "-c", probe.replace("raise SystemExit(0)", "raise SystemExit(2)"),],
                cwd=Path(__file__).parents[1],
                check=False,
            )
            self.assertEqual(available.returncode, 1)

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
                patch.object(ltspice_wrapper, "_run_simulator", side_effect=simulate),
            ):
                first = run_netlist(source, root / "first", cache_dir=cache)
                run_netlist(source, root / "second", cache_dir=cache)

            manifest = json.loads((first / "run_manifest.json").read_text())
            self.assertEqual(calls, 2)
            self.assertFalse(manifest["cache"]["requested"])
            self.assertFalse(cache.exists())
            self.assertEqual(manifest["result_artifacts"][0]["name"], "filter.log")
            self.assertEqual(len(manifest["result_artifacts"][0]["sha256"]), 64)

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

    def test_run_rejects_oversized_output_before_hashing_or_cache_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LTspice.exe"
            executable.write_bytes(b"simulator")
            source = root / "filter.cir"
            source.write_text("R1 in out 1k\n.end\n", encoding="utf-8")
            cache = root / "cache"

            def simulate(
                command: list[str], **kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                run_netlist_path = Path(command[-1])
                run_netlist_path.with_suffix(".raw").write_bytes(b"too large")
                run_netlist_path.with_suffix(".log").write_text(
                    "gain=1\n", encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            ltspice_wrapper._simulator_metadata_cached.cache_clear()
            with (
                patch.object(ltspice_wrapper, "LTSPICE", executable),
                patch.object(ltspice_wrapper, "MAX_RUN_OUTPUT_BYTES", 8),
                patch.object(ltspice_wrapper, "_run_simulator", side_effect=simulate),
                patch.object(
                    ltspice_wrapper,
                    "_result_artifacts",
                    side_effect=AssertionError("oversized artifacts must not be hashed"),
                ),
                self.assertRaisesRegex(RuntimeError, "artifacts exceed 8 bytes"),
            ):
                run_netlist(
                    source,
                    root / "oversized",
                    reuse_cache=True,
                    cache_dir=cache,
                )

            manifest = json.loads(
                (root / "oversized" / "run_manifest.json").read_text()
            )
            self.assertEqual(manifest["status"], "failed")
            self.assertGreater(manifest["output_size_bytes"], 8)
            self.assertFalse(cache.exists())

    def test_cache_integrity_rejects_oversized_artifacts_before_hashing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache_entry = Path(directory) / "simulation-key"
            artifact_dir = cache_entry / "artifacts"
            artifact_dir.mkdir(parents=True)
            artifact = artifact_dir / "filter.log"
            artifact.write_bytes(b"too large")
            request = {"netlist_filename": "filter.cir"}
            (cache_entry / "cache_manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": ltspice_wrapper.CACHE_SCHEMA_VERSION,
                        "status": "completed",
                        "cache_key": "key",
                        "request": request,
                        "artifacts": [
                            {
                                "name": "filter.log",
                                "sha256": "unused",
                                "size_bytes": artifact.stat().st_size,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(ltspice_wrapper, "MAX_RUN_OUTPUT_BYTES", 8),
                patch.object(
                    ltspice_wrapper,
                    "_sha256_file",
                    side_effect=AssertionError("oversized cache must not be hashed"),
                ),
            ):
                validated = ltspice_wrapper._validated_cache_artifacts(
                    cache_entry, "key", request
                )

            self.assertIsNone(validated)

    def test_compression_control_stages_utf16_decks_without_changing_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "LTspice.exe"
            executable.write_bytes(b"simulator")
            for ending in (".END ; done\n", ""):
                source = root / "input.cir"
                original = ("* title\n.options plotwinsize=300\nV1 in 0 1\n" + ending).encode("utf-16")
                source.write_bytes(original)
                output = root / ("with-end" if ending else "without-end")

                def simulate(command, **kwargs):
                    staged = Path(command[-1])
                    text = staged.read_text(encoding="utf-8")
                    self.assertIn(".options plotwinsize=300\n", text)
                    self.assertTrue(text.startswith("* title\n.options plotwinsize=0\n.options plotwinsize=300\n"))
                    self.assertTrue(text.endswith(ending))
                    staged.with_suffix(".log").write_text("gain=1\n", encoding="utf-8")
                    return subprocess.CompletedProcess(command, 0, "", "")

                with patch.object(ltspice_wrapper, "LTSPICE", executable), patch.object(
                    ltspice_wrapper, "_run_simulator", side_effect=simulate
                ):
                    run_netlist(source, output, disable_compression=True)
                self.assertEqual(source.read_bytes(), original)
                manifest = json.loads((output / "run_manifest.json").read_text())
                self.assertTrue(manifest["disable_compression"])
            with self.assertRaisesRegex(ValueError, "disable_compression"):
                run_netlist(source, disable_compression="yes")

    def test_parameters_are_declared_after_the_title_of_the_staged_deck(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "LTspice.exe"
            executable.write_bytes(b"simulator")
            source = root / "input.cir"
            source.write_text("* title\nR1 in out {R1_VAL}\n.end\n", encoding="utf-8")

            def simulate(command, **kwargs):
                text = Path(command[-1]).read_text(encoding="utf-8")
                self.assertTrue(text.startswith("* title\n.param R1_VAL=1000\nR1 in out {R1_VAL}\n"))
                Path(command[-1]).with_suffix(".log").write_text("", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.object(ltspice_wrapper, "LTSPICE", executable), patch.object(
                ltspice_wrapper, "_run_simulator", side_effect=simulate
            ):
                run_netlist(source, root / "out", parameters={"R1_VAL": "1000"})
            manifest = json.loads((root / "out" / "run_manifest.json").read_text())
            self.assertEqual(manifest["parameters"], {"R1_VAL": "1000"})
            self.assertEqual(source.read_text(encoding="utf-8"), "* title\nR1 in out {R1_VAL}\n.end\n")

    def test_parameters_cannot_inject_netlist_lines(self) -> None:
        for name, value in (
            ("R1", "1k\n.include /etc/passwd"),
            ("R1", "1 k"),
            ("R1", "{x}"),
            ("R1", ""),
            ("1R", "1k"),
            ("R-1", "1k"),
        ):
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                ltspice_wrapper._parameter_directives({name: value})

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
                patch.object(ltspice_wrapper, "_run_simulator", side_effect=simulate),
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

                uncompressed = run_netlist(
                    source, root / "uncompressed", reuse_cache=True,
                    cache_dir=cache, disable_compression=True,
                )
                repeated = run_netlist(
                    source, root / "repeated", reuse_cache=True,
                    cache_dir=cache, disable_compression=True,
                )

            first_manifest = json.loads((first / "run_manifest.json").read_text())
            second_manifest = json.loads((second / "run_manifest.json").read_text())
            self.assertEqual(calls, 2)
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

            new_manifest = json.loads((uncompressed / "run_manifest.json").read_text())
            repeated_manifest = json.loads((repeated / "run_manifest.json").read_text())
            self.assertNotEqual(first_manifest["cache"]["key"], new_manifest["cache"]["key"])
            self.assertFalse(new_manifest["cache"]["hit"])
            self.assertTrue(repeated_manifest["cache"]["hit"])

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
                patch.object(ltspice_wrapper, "_run_simulator", side_effect=simulate),
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
                patch.object(ltspice_wrapper, "_run_simulator", side_effect=simulate),
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
                patch.object(ltspice_wrapper, "_run_simulator", side_effect=simulate),
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

    def test_when_measurements_report_the_abscissa_and_skip_log_statistics(self) -> None:
        # RC = 1 ms step: v(out) crosses 0.5 at RC*ln(2) and is 1 - 1/e at RC.
        t50 = 1e-3 * math.log(2)
        log = (
            "Circuit: * rc\n\n"
            "tnom = 27\n"
            "temp = 27\n"
            "method = modified trap\n"
            f"t50: v(out)=0.5 AT {t50:.9g}\n"
            f"vtau: v(out)={1 - math.exp(-1):.9g} at 0.001\n"
            f"vx: v(out)={1 - math.exp(-1):.9g} AT 0.001\n"
            "tr=0.00219722 FROM 0.000105361 TO 0.00230259\n"
            "vmax: MAX(v(out))=0.999955 FROM 0 TO 0.01\n"
            "totiter = 2135\n"
            "traniter = 2000\n"
            "matrix size = 5\n"
            "Total elapsed time: 0.035 seconds.\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rc.log"
            path.write_text(log, encoding="utf-16le")
            # Without a netlist the kind of measurement is unknown, so the value
            # before AT is kept as before; log statistics are still skipped.
            measurements = parse_measurements(path)
            self.assertEqual(
                set(measurements), {"t50", "vtau", "vx", "tr", "vmax"}
            )
            self.assertEqual(measurements["t50"], 0.5)
            self.assertAlmostEqual(measurements["vtau"], 1 - math.exp(-1), places=9)
            self.assertEqual(measurements["tr"], 0.00219722)
            self.assertEqual(measurements["vmax"], 0.999955)
            # The run netlist beside the log says which lines are WHEN results.
            (Path(directory) / "rc.cir").write_text(
                "* rc\n"
                ".meas tran t50 WHEN v(out)=0.5\n"
                ".meas tran vtau FIND v(out) AT=1m\n"
                ".MEAS TRAN VX FIND v(out)\n+ WHEN v(in)=1 CROSS=1\n",
                encoding="utf-8",
            )
            measurements = parse_measurements(path)
            self.assertAlmostEqual(measurements["t50"], t50, places=12)
            self.assertAlmostEqual(measurements["vx"], 1 - math.exp(-1), places=9)
            other = Path(directory) / "other.net"
            other.write_text(".meas tran t50 FIND v(out) WHEN v(in)=1\n")
            self.assertEqual(parse_measurements(path, other)["t50"], 0.5)

    def test_stepped_when_measurements_use_the_at_column(self) -> None:
        log = (
            ".step r=1000\n.step r=2000\n"
            "Measurement: t50\n"
            "  step\tv(out)\tat\n"
            "     1\t0.5\t0.000693147\n"
            "     2\t0.5\t0.00138629\n"
            "Measurement: vtau\n"
            "  step\tv(out)\tat\n"
            "     1\t0.632121\t0.001\n"
            "     2\t0.393469\t0.001\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "step.log"
            path.write_text(log, encoding="utf-16le")
            (Path(directory) / "step.net").write_text(
                ".meas tran t50 when v(out)=0.5\n.meas tran vtau find v(out) at=1m\n"
            )
            self.assertEqual(
                parse_stepped_measurement_rows(path),
                {
                    "t50": {1: 0.000693147, 2: 0.00138629},
                    "vtau": {1: 0.632121, 2: 0.393469},
                },
            )

    def test_raw_and_log_parsers_reject_oversized_artifacts_before_read(self) -> None:
        import ltspice_wrapper
        import raw_parser

        with tempfile.TemporaryDirectory() as directory:
            raw_path = Path(directory) / "oversized.raw"
            log_path = Path(directory) / "oversized.log"
            with raw_path.open("wb") as handle:
                handle.truncate(raw_parser.MAX_RAW_FILE_BYTES + 1)
            with log_path.open("wb") as handle:
                handle.truncate(ltspice_wrapper.MAX_LOG_FILE_BYTES + 1)
            with self.assertRaisesRegex(ValueError, "RAW file exceeds"):
                parse_raw(raw_path)
            with self.assertRaisesRegex(ValueError, "LTspice log exceeds"):
                parse_measurements(log_path)

    def test_log_parsers_accept_utf8_and_utf16le_with_or_without_bom(self) -> None:
        log = ".step rval=1000\nMeasurement: gain\n step value\n 1 2.5\ngain=2.5\n"
        encoded_logs = {
            "utf8": log.encode("utf-8"),
            "utf8-bom": b"\xef\xbb\xbf" + log.encode("utf-8"),
            "utf16le": log.encode("utf-16le"),
            "utf16le-bom": b"\xff\xfe" + log.encode("utf-16le"),
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "encoding.log"
            for name, payload in encoded_logs.items():
                with self.subTest(encoding=name):
                    path.write_bytes(payload)
                    self.assertEqual(parse_measurements(path), {"gain": 2.5})
                    self.assertEqual(parse_step_values(path, "rval"), [1000.0])
                    self.assertEqual(
                        parse_stepped_measurement_rows(path), {"gain": {1: 2.5}}
                    )

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

    def test_repeated_axis_sample_does_not_create_a_false_step(self) -> None:
        data = RawData(
            flags="real",
            variables=["time", "V(out)"],
            values={"time": [0, 1, 1, 2], "V(out)": [0, 1, 1.5, 2]},
            step_count=1,
            points_per_step=4,
        )

        segments = step_slices(data)

        self.assertEqual([(part.start, part.stop) for part in segments], [(0, 4)])

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

    def test_parse_ascii_raw_preserves_utf8_or_utf16le_values_encoding(self) -> None:
        document = """Title: test
Flags: real forward
No. Variables: 2
No. Points: 2
Variables:
\t0\ttime\ttime
\t1\tV(out)\tvoltage
Values:
0\t0.0
\t1.5
1\t0.1
\t2.5
"""
        payloads = {
            "utf8": document.encode("utf-8"),
            "utf8-bom": b"\xef\xbb\xbf" + document.encode("utf-8"),
            "utf16le": document.encode("utf-16le"),
            "utf16le-bom": b"\xff\xfe" + document.encode("utf-16le"),
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ascii.raw"
            for name, payload in payloads.items():
                with self.subTest(encoding=name):
                    path.write_bytes(payload)
                    data = parse_raw(path)
                    self.assertEqual(data.values["time"], [0.0, 0.1])
                    self.assertEqual(data.values["V(out)"], [1.5, 2.5])

    def test_parse_complex_ascii_raw_values(self) -> None:
        document = """Title: test
Flags: complex forward
No. Variables: 2
No. Points: 2
Variables:
\t0\tfrequency\tfrequency
\t1\tV(out)\tvoltage
Values:
0\t10,0
\t1,-2
1\t20,0
\t3,-4
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "complex-ascii.raw"
            path.write_bytes(document.encode("utf-8"))
            data = parse_raw(path)

        self.assertEqual(data.values["frequency"], [complex(10, 0), complex(20, 0)])
        self.assertEqual(data.values["V(out)"], [complex(1, -2), complex(3, -4)])

    def test_run_netlist_rejects_existing_explicit_output_before_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LTspice.exe"
            executable.write_bytes(b"simulator")
            source = root / "filter.cir"
            source.write_text("R1 in out 1k\n.end\n", encoding="utf-8")
            output = root / "run"
            output.mkdir()
            sentinel = output / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")

            with (
                patch.object(ltspice_wrapper, "LTSPICE", executable),
                patch.object(ltspice_wrapper, "_run_simulator") as simulate,
                self.assertRaisesRegex(ValueError, "must not already exist"),
            ):
                run_netlist(source, output_dir=output)

            simulate.assert_not_called()
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertEqual(list(output.iterdir()), [sentinel])

    def test_concurrent_runs_cannot_share_an_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LTspice.exe"
            executable.write_bytes(b"simulator")
            source = root / "filter.cir"
            source.write_text("R1 in out 1k\n.end\n", encoding="utf-8")
            output = root / "run"
            barrier = threading.Barrier(2)

            def simulate(
                command: list[str], **kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                Path(command[-1]).with_suffix(".log").write_text(
                    "gain=1\n", encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            def invoke() -> str:
                barrier.wait()
                try:
                    run_netlist(source, output_dir=output)
                except ValueError as exc:
                    return str(exc)
                return "completed"

            with (
                patch.object(ltspice_wrapper, "LTSPICE", executable),
                patch.object(ltspice_wrapper, "_run_simulator", side_effect=simulate),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                outcomes = sorted(executor.map(lambda _: invoke(), range(2)))

            self.assertEqual(
                outcomes,
                ["completed", "output directory must not already exist"],
            )

    def test_run_netlist_preserves_relative_include_and_lib_paths_when_staged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LTspice.exe"
            executable.write_bytes(b"simulator")
            source_dir = root / "source"
            models = source_dir / "models"
            models.mkdir(parents=True)
            include = models / "values.inc"
            library = models / "device.lib"
            include.write_text(".param RV=1k\n", encoding="utf-8")
            library.write_text(".model DTEST D\n", encoding="utf-8")
            source = source_dir / "filter.cir"
            source.write_text(
                '.include "models/values.inc"\n.lib \'models/device.lib\' DTEST\n.end\n',
                encoding="utf-8",
            )

            def simulate(
                command: list[str], **kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                staged = Path(command[-1])
                text = staged.read_text(encoding="utf-8")
                self.assertIn(f'.include "{include.resolve()}"', text)
                self.assertIn(f'.lib "{library.resolve()}" DTEST', text)
                staged.with_suffix(".log").write_text("gain=1\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch.object(ltspice_wrapper, "LTSPICE", executable),
                patch.object(ltspice_wrapper, "_run_simulator", side_effect=simulate),
            ):
                result = run_netlist(source, output_dir=root / "run")

            self.assertTrue((result / "filter.cir").is_file())

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

    def test_binary_transient_time_sign_bits_do_not_create_false_steps(self) -> None:
        header = """Title: compressed transient
Flags: real forward
No. Variables: 2
No. Points: 3
Variables:
\t0\ttime\ttime
\t1\tV(out)\tvoltage
Binary:
"""
        binary = struct.pack("<dfdfdf", 0.0, 1.0, -0.1, 2.0, -0.2, 3.0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "compressed.raw"
            path.write_bytes(header.encode("utf-16le") + binary)
            data = parse_raw(path)

        self.assertEqual(data.values["time"], [0.0, 0.1, 0.2])
        self.assertEqual(data.step_count, 1)
        self.assertEqual(data.points_per_step, 3)

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

    def test_binary_decoders_match_per_value_reference_for_every_layout(self) -> None:
        def reference(data: bytes, points: int, count: int, is_complex: bool, double: bool,
                      fast_access: bool) -> list[list[float | complex]]:
            # Straightforward per-value decoder the vectorized paths replace.
            columns: list[list[float | complex]] = [[] for _ in range(count)]
            row_bytes = count * 16 if is_complex else count * 8 if double else 8 + (count - 1) * 4
            for point in range(points):
                for index in range(count):
                    if fast_access:
                        offset = sum(points * (16 if is_complex else 8 if double or prior == 0 else 4)
                                     for prior in range(index))
                        offset += point * (16 if is_complex else 8 if double or index == 0 else 4)
                    else:
                        offset = point * row_bytes + (index * 16 if is_complex else index * 8 if double
                                                      else 0 if index == 0 else 8 + (index - 1) * 4)
                    if is_complex:
                        columns[index].append(complex(*struct.unpack_from("<dd", data, offset)))
                    elif double or index == 0:
                        columns[index].append(struct.unpack_from("<d", data, offset)[0])
                    else:
                        columns[index].append(struct.unpack_from("<f", data, offset)[0])
            return columns

        def bits(value: float | complex) -> tuple[type, bytes]:
            parts = (value.real, value.imag) if isinstance(value, complex) else (value,)
            return type(value), struct.pack(f"<{len(parts)}d", *parts)

        specials = [0.0, -0.0, 1.5, -2.25, 5e-324, -1e-310, 1e300, float("inf"), float("-inf"),
                    3.4028234663852886e38, 1.401298464324817e-45, 0.1, -123456.789]
        points = 2 * 4096 + 37  # crosses the point-major chunk boundary
        layouts = [
            ("real forward", 1, False, False),
            ("real forward", 5, False, False),
            ("real forward", 4, False, True),
            ("complex forward", 3, True, True),
            ("real forward FastAccess", 5, False, False),
            ("real forward FastAccess", 4, False, True),
            ("complex forward FastAccess", 3, True, True),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "layout.raw"
            for flags, count, is_complex, double in layouts:
                with self.subTest(flags=flags, count=count, double=double):
                    fast_access = "FastAccess" in flags
                    axis = "frequency" if is_complex else "time"
                    names = [axis, *[f"V(n{index})" for index in range(1, count)]]
                    header = (f"Title: layout\nFlags: {flags}\nNo. Variables: {count}\n"
                              f"No. Points: {points}\nVariables:\n"
                              + "".join(f"\t{index}\t{name}\tvoltage\n" for index, name in enumerate(names))
                              + "Binary:\n")
                    width = 2 if is_complex else 1

                    def sample(point: int, index: int, part: int) -> float:
                        value = specials[(point * 7 + index * 3 + part) % len(specials)]
                        return value * (1 + point * 1e-7) if point % 5 else value

                    def pack(point: int, index: int) -> bytes:
                        if is_complex:
                            return struct.pack("<dd", *(sample(point, index, part) for part in range(width)))
                        if double or index == 0:
                            return struct.pack("<d", sample(point, index, 0))
                        # Round to float32 range; LTspice traces are single precision.
                        return struct.pack("<f", max(-3.0e38, min(3.0e38, sample(point, index, 0))))

                    if fast_access:
                        payload = b"".join(pack(point, index) for index in range(count) for point in range(points))
                    else:
                        payload = b"".join(pack(point, index) for point in range(points) for index in range(count))
                    path.write_bytes(header.encode("utf-16le") + payload)
                    data = parse_raw(path)
                    expected = reference(payload, points, count, is_complex, double, fast_access)
                    if axis == "time":  # binary transient-axis sign-bit rule
                        expected[0] = [abs(value) for value in expected[0]]
                    self.assertEqual(data.variables, names)
                    for name, column in zip(names, expected):
                        actual = data.values[name]
                        self.assertIs(type(actual), list)
                        self.assertEqual([bits(value) for value in actual], [bits(value) for value in column])

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

    def test_manifest_dashboard_rejects_escaped_artifact_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runs"
            run_dir = root / "completed-run"
            run_dir.mkdir(parents=True)
            (run_dir / "result.raw").touch()
            outside = root.parent / "outside.raw"
            outside.touch()
            try:
                (run_dir / "escape.raw").symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"file symlinks unavailable: {exc}")
            (run_dir / "run_manifest.json").write_text(
                json.dumps(
                    {
                        "status": "completed",
                        "result_files": [
                            "result.raw",
                            "../../outside.raw",
                            "escape.raw",
                        ],
                    }
                ),
                encoding="utf-8",
            )

            records = collect_records(root)

        self.assertEqual(
            records[0]["artifacts"],
            [{"name": "result.raw", "href": "completed-run/result.raw"}],
        )

    def test_manifest_dashboard_does_not_follow_output_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runs"
            root.mkdir()
            outside_html = root.parent / "outside.html"
            outside_json = root.parent / "outside.json"
            outside_html.write_text("html sentinel", encoding="utf-8")
            outside_json.write_text("json sentinel", encoding="utf-8")
            try:
                (root / "index.html").symlink_to(outside_html)
                (root / "index.json").symlink_to(outside_json)
            except OSError as exc:
                self.skipTest(f"file symlinks unavailable: {exc}")

            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                write_dashboard(root)

            self.assertEqual(outside_html.read_text(encoding="utf-8"), "html sentinel")
            self.assertEqual(outside_json.read_text(encoding="utf-8"), "json sentinel")

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

    def test_cache_staging_name_is_no_longer_than_the_published_entry(self) -> None:
        cache_key = "f" * 64
        staged: list[Path] = []
        replace = ltspice_wrapper.os.replace

        def recording_replace(source: Path, destination: Path) -> None:
            if Path(destination).name.startswith("simulation-"):
                staged.append(Path(source))
            replace(source, destination)

        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "run"
            output_dir.mkdir()
            (output_dir / "deck.log").write_text("done\n", encoding="utf-8")
            cache_dir = Path(temporary) / "cache"
            with patch.object(ltspice_wrapper.os, "replace", recording_replace):
                ltspice_wrapper._publish_cache_entry(
                    cache_dir, cache_key, {}, output_dir, "deck.cir"
                )
            final = cache_dir / f"simulation-{cache_key}"
            self.assertTrue(final.is_dir())
            self.assertEqual(len(staged), 1)
            self.assertLessEqual(len(staged[0].name), len(final.name))
            self.assertEqual([path.name for path in cache_dir.iterdir()], [final.name])


if __name__ == "__main__":
    unittest.main()
