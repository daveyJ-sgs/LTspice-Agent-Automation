from __future__ import annotations

import asyncio
import cmath
import csv
import json
import math
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import mcp_server
    from raw_parser import RawData
except ModuleNotFoundError as exc:
    if exc.name != "mcp":
        raise
    mcp_server = None
    RawData = None


@unittest.skipIf(mcp_server is None, "optional mcp package is not installed")
class MCPServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.runs = self.root / "runs"
        self.examples = self.root / "examples"
        self.runs.mkdir()
        self.examples.mkdir()
        self.runs_patch = patch.object(mcp_server, "RUNS_DIR", self.runs)
        self.examples_patch = patch.object(mcp_server, "EXAMPLES_DIR", self.examples)
        self.runs_patch.start()
        self.examples_patch.start()

    def tearDown(self) -> None:
        self.examples_patch.stop()
        self.runs_patch.stop()
        self.temporary_directory.cleanup()

    def make_run(self, name: str = "run-001") -> Path:
        run_dir = self.runs / name
        run_dir.mkdir(parents=True)
        return run_dir

    def make_experiment_result(
        self,
        experiment_id: str,
        points: list[dict[str, object]],
        status: str = "completed",
    ) -> Path:
        experiment_dir = self.runs / experiment_id
        experiment_dir.mkdir(parents=True, exist_ok=True)
        parameter_names = list(points[0]["parameters"]) if points else ["R"]
        parameters = [
            {
                "name": name,
                "values": list(
                    dict.fromkeys(str(point["parameters"][name]) for point in points)
                )
                or ["1k"],
            }
            for name in parameter_names
        ]
        definition = {
            "netlist_template": ".end\n",
            "parameters": parameters,
            "parameter_order": parameter_names,
            "derived_parameters": [],
            "derived_parameter_order": [],
            "parameter_units": {name: "" for name in parameter_names},
            "waveform_analyses": [],
            "filename": "circuit.cir",
            "ascii_raw": False,
            "timeout_seconds": 120,
            "reuse_cache": False,
            "execution_mode": "independent",
        }
        passed_points = sum(bool(point["all_passed"]) for point in points)
        manifest = {
            "schema_version": 2,
            "engine_version": 1,
            "experiment_id": experiment_id,
            "status": status,
            "definition": definition,
            "definition_hash": mcp_server.experiment_index._definition_hash(definition),
            "created_at": "2026-08-24T12:00:00-07:00",
            "updated_at": "2026-08-24T12:00:01-07:00",
            "point_count": max(1, len(points)),
            "finished_points": len(points),
            "completed_points": len(points),
            "error_points": 0,
            "passed_points": passed_points,
            "failed_points": len(points) - passed_points,
            "all_passed": status == "completed" and passed_points == len(points),
        }
        mcp_server._write_json(
            experiment_dir / "experiment_manifest.json", manifest
        )
        path = experiment_dir / "results.json"
        if status == "completed":
            mcp_server._write_json(path, {
                "experiment_id": experiment_id,
                "status": status,
                "execution_mode": "independent",
                "parameter_order": parameter_names,
                "derived_parameter_order": [],
                "parameter_units": {name: "" for name in parameter_names},
                "point_count": len(points),
                "completed_points": len(points),
                "error_points": 0,
                "passed_points": passed_points,
                "failed_points": len(points) - passed_points,
                "all_passed": all(bool(point["all_passed"]) for point in points),
                "points": points,
                "native_batch": None,
            })
        return path

    @staticmethod
    def experiment_point(
        index: int,
        parameters: dict[str, str],
        measurements: dict[str, float],
        checks: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "index": index,
            "parameters": parameters,
            "run_dir": "",
            "simulation_status": "completed",
            "duration_seconds": 0.1,
            "measurements": measurements,
            "analyses": [
                {
                    "name": "bandwidth|check `<img src=x>",
                    "status": "completed",
                    "error": None,
                    "analysis": {
                        "all_passed": all(check["passed"] for check in checks),
                        "results": checks,
                    },
                }
            ],
            "all_passed": all(check["passed"] for check in checks),
            "error": None,
        }

    @staticmethod
    def experiment_check(value: float, passed: bool) -> dict[str, object]:
        return {
            "metric": "cutoff_frequency",
            "value": value,
            "unit": "Hz",
            "threshold": {"operator": ">=", "target": 1000.0, "unit": "Hz"},
            "passed": passed,
            "evidence": {},
            "parameters": {"reference_frequency": 10.0},
        }

    @staticmethod
    def native_batch_result(
        combinations: list[dict[str, str]],
        batch_dir: Path,
        *,
        all_passed: bool = True,
        error: str | None = None,
        execution_source: str = "simulator",
        cache_key: str | None = None,
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        batch_dir.mkdir(parents=True, exist_ok=True)
        points = [
            {
                "index": index,
                "parameters": combination,
                "run_dir": str(batch_dir),
                "simulation_status": "completed",
                "duration_seconds": None,
                "measurements": {"index": float(index)},
                "analyses": [],
                "all_passed": all_passed,
                "error": error,
                "native_step_index": index,
            }
            for index, combination in enumerate(combinations)
        ]
        batch: dict[str, object] = {
            "run_dir": str(batch_dir),
            "status": "completed",
            "step_count": len(combinations),
            "validated_step_order": list(range(len(combinations))),
            "execution_source": execution_source,
        }
        if cache_key is not None:
            batch.update(cache_hit=True, cache_key=cache_key)
        return points, batch

    def test_run_netlist(self) -> None:
        output_dir = self.make_run()
        summary = {"run_dir": str(output_dir), "status": "completed"}
        with (
            patch.object(mcp_server, "_run_netlist_text", return_value=output_dir) as execute,
            patch.object(mcp_server, "_summarize_run", return_value=summary),
        ):
            result = mcp_server.run_netlist("* test\n.end\n", "demo.cir", True, 30)

        self.assertEqual(result, summary)
        args = execute.call_args.args
        self.assertEqual(args[:4], ("* test\n.end\n", "demo.cir", True, 30))
        self.assertEqual(args[4].parent, self.runs)
        with self.assertRaises(ValueError):
            mcp_server.run_netlist(".end", "../escape.cir")
        with self.assertRaises(ValueError):
            mcp_server.run_netlist(".end", "..\\escape.cir")

    def test_run_netlist_propagates_opt_in_cache(self) -> None:
        output_dir = self.make_run()
        summary = {
            "run_dir": str(output_dir),
            "status": "completed",
            "cache": {"hit": True, "key": "abc"},
        }
        with (
            patch.object(mcp_server, "_run_netlist_text", return_value=output_dir) as execute,
            patch.object(mcp_server, "_summarize_run", return_value=summary),
        ):
            result = mcp_server.run_netlist(".end\n", reuse_cache=True)

        self.assertEqual(result, summary)
        self.assertTrue(execute.call_args.args[-1])
        with self.assertRaisesRegex(ValueError, "reuse_cache must be a boolean"):
            mcp_server.run_netlist(".end\n", reuse_cache=1)  # type: ignore[arg-type]

    def test_simulation_cache_path_remains_inside_runs(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        try:
            os.symlink(outside, self.runs / "cache", target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")

        with self.assertRaisesRegex(ValueError, "inside the runs directory"):
            mcp_server.run_netlist(".end\n", reuse_cache=True)
        self.assertEqual(list(outside.iterdir()), [])

    def test_run_netlist_file(self) -> None:
        source = self.root / "source.cir"
        source.write_text(".end\n")
        output_dir = self.runs / "custom"
        summary = {"run_dir": str(output_dir), "status": "completed"}
        with (
            patch.object(mcp_server.wrapper, "run_netlist", return_value=output_dir) as execute,
            patch.object(mcp_server, "_summarize_run", return_value=summary),
        ):
            result = mcp_server.run_netlist_file(str(source), str(output_dir), timeout_seconds=15)

        self.assertEqual(result, summary)
        self.assertEqual(execute.call_args.args[0], source)
        self.assertEqual(execute.call_args.kwargs["output_dir"], output_dir.resolve())
        with self.assertRaises(ValueError):
            mcp_server.run_netlist_file(str(source), str(self.root / "outside"))

    def test_get_measurements(self) -> None:
        run_dir = self.make_run()
        (run_dir / "a.log").touch()
        (run_dir / "b.log").touch()
        with patch.object(
            mcp_server.wrapper,
            "parse_measurements",
            side_effect=({"gain": 2.0}, {"delay": 3e-6}),
        ):
            result = mcp_server.get_measurements(str(run_dir))

        self.assertEqual(result, {"gain": 2.0, "delay": 3e-6})
        with self.assertRaises(ValueError):
            mcp_server.get_measurements(str(self.root))

    def test_raw_and_log_discovery_reject_symlink_escapes(self) -> None:
        run_dir = self.make_run()
        outside_raw = self.root / "outside.raw"
        outside_log = self.root / "outside.log"
        outside_raw.touch()
        outside_log.touch()
        try:
            (run_dir / "escape.raw").symlink_to(outside_raw)
            (run_dir / "escape.log").symlink_to(outside_log)
        except OSError as exc:
            self.skipTest(f"file symlinks unavailable: {exc}")

        with self.assertRaisesRegex(ValueError, "inside the run directory"):
            mcp_server.get_waveform(str(run_dir))
        with self.assertRaisesRegex(ValueError, "inside the run directory"):
            mcp_server.get_measurements(str(run_dir))

    def test_get_waveform(self) -> None:
        run_dir = self.make_run()
        raw_path = run_dir / "result.raw"
        raw_path.touch()
        data = RawData(
            flags="complex forward",
            variables=["frequency", "V(out)"],
            values={
                "frequency": [10.0, 20.0, 30.0, 40.0],
                "V(out)": [complex(1, -1), complex(2, -2), complex(3, -3), complex(4, -4)],
            },
        )
        with patch.object(mcp_server.raw_parser, "parse_raw", return_value=data):
            result = mcp_server.get_waveform(
                str(run_dir), variables=["V(out)"], max_points=2
            )

        self.assertEqual(result["returned_points"], 2)
        self.assertEqual(result["data"]["V(out)"], {"real": [1.0, 4.0], "imag": [-1.0, -4.0]})
        with self.assertRaises(ValueError):
            mcp_server.get_waveform(str(run_dir), max_points=0)
        with self.assertRaises(ValueError):
            mcp_server.get_waveform(
                str(run_dir), max_points=mcp_server.MAX_WAVEFORM_RESPONSE_POINTS + 1
            )
        with self.assertRaises(ValueError):
            mcp_server.get_waveform(str(run_dir), raw_filename="../result.raw")

    def test_export_waveform_csv(self) -> None:
        run_dir = self.make_run()
        raw_path = run_dir / "result.raw"
        raw_path.touch()
        data = RawData(flags="real", variables=["time"], values={"time": [0.0, 1.0]})
        def export_csv(_: RawData, path: Path) -> None:
            path.touch()

        with (
            patch.object(mcp_server.raw_parser, "parse_raw", return_value=data),
            patch.object(mcp_server.raw_parser, "export_csv", side_effect=export_csv) as export,
        ):
            result = mcp_server.export_waveform_csv(str(run_dir))

        export_path = export.call_args.args[1]
        self.assertEqual(export.call_args.args[0], data)
        self.assertEqual(export_path.parent, run_dir.resolve())
        self.assertTrue(export_path.name.startswith(".result.csv."))
        self.assertTrue(export_path.name.endswith(".tmp"))
        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["csv_path"], str(run_dir.resolve() / "result.csv"))
        self.assertTrue((run_dir / "result.csv").is_file())

    def test_export_waveform_csv_does_not_follow_an_output_symlink(self) -> None:
        run_dir = self.make_run()
        (run_dir / "result.raw").touch()
        outside = self.root / "outside.csv"
        outside.write_text("sentinel", encoding="utf-8")
        try:
            (run_dir / "result.csv").symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"file symlinks unavailable: {exc}")
        data = RawData(flags="real", variables=["time"], values={"time": [0.0]})

        with (
            patch.object(mcp_server.raw_parser, "parse_raw", return_value=data),
            patch.object(mcp_server.raw_parser, "export_csv") as export,
            self.assertRaisesRegex(ValueError, "must not be a symlink"),
        ):
            mcp_server.export_waveform_csv(str(run_dir))

        export.assert_not_called()
        self.assertEqual(outside.read_text(encoding="utf-8"), "sentinel")

    def test_analyze_waveform_uses_full_selected_step(self) -> None:
        run_dir = self.make_run()
        raw_path = run_dir / "stepped.raw"
        raw_path.touch()
        data = RawData(
            flags="real stepped",
            variables=["time", "V(in)", "V(out)"],
            values={
                "time": [0, 1, 2, 0, 1, 2, 3],
                "V(in)": [0, 1, 2, 0, 0, 2, 2],
                "V(out)": [0, 1, 2, 0, 4, 5.5, 5],
            },
            step_count=2,
            points_per_step=None,
        )
        requirements = [
            {"metric": "maximum", "operator": "<=", "target": 6.0},
            {
                "metric": "overshoot",
                "operator": "<=",
                "target": 11.0,
                "initial_value": 0.0,
                "final_value": 5.0,
            },
        ]
        with patch.object(mcp_server.raw_parser, "parse_raw", return_value=data):
            result = mcp_server.analyze_waveform(
                str(run_dir),
                "V(out)",
                requirements,
                step_index=1,
                signal_unit="V",
            )

        self.assertEqual(result["source_points"], 4)
        self.assertEqual(result["analysis_resolution"], "full")
        self.assertEqual(len(result["raw_sha256"]), 64)
        self.assertEqual(result["raw_size_bytes"], 0)
        self.assertTrue(result["all_passed"])
        self.assertEqual(result["results"][0]["evidence"]["index"], 2)
        self.assertEqual(result["results"][1]["value"], 10.0)
        with patch.object(mcp_server.raw_parser, "parse_raw", return_value=data):
            with self.assertRaisesRegex(ValueError, "step_index is required"):
                mcp_server.analyze_waveform(str(run_dir), "V(out)", requirements)
        with patch.object(mcp_server.raw_parser, "parse_raw", return_value=data):
            with self.assertRaisesRegex(IndexError, "step_index"):
                mcp_server.analyze_waveform(
                    str(run_dir), "V(out)", requirements, step_index=1.5
                )
        with patch.object(mcp_server.raw_parser, "parse_raw", return_value=data):
            paired = mcp_server.analyze_waveform(
                str(run_dir),
                "V(in)",
                [
                    {
                        "metric": "propagation_delay",
                        "operator": "<=",
                        "target": 1,
                        "primary_threshold": 1,
                        "secondary_threshold": 5,
                        "primary_edge": "rising",
                        "secondary_edge": "rising",
                    }
                ],
                secondary_variable="V(out)",
                step_index=1,
            )

        self.assertEqual(paired["source_points"], 4)
        self.assertAlmostEqual(paired["results"][0]["value"], 1 / 6)
        self.assertEqual(paired["results"][0]["evidence"]["primary_index_before"], 1)

    def test_analyze_waveform_does_not_miss_a_narrow_spike(self) -> None:
        run_dir = self.make_run()
        (run_dir / "result.raw").touch()
        values = [0.0] * 401
        values[199] = 9.0
        data = RawData(
            flags="real forward",
            variables=["time", "V(out)"],
            values={"time": list(range(401)), "V(out)": values},
        )
        with patch.object(mcp_server.raw_parser, "parse_raw", return_value=data):
            result = mcp_server.analyze_waveform(
                str(run_dir),
                "V(out)",
                [{"metric": "maximum", "operator": ">=", "target": 9.0}],
                signal_unit="V",
            )

        self.assertEqual(result["source_points"], 401)
        self.assertIsNone(result["secondary_variable"])
        self.assertTrue(result["all_passed"])
        self.assertEqual(result["results"][0]["evidence"]["index"], 199)

    def test_analyze_waveform_supports_windows_and_paired_signals(self) -> None:
        run_dir = self.make_run()
        (run_dir / "paired.raw").touch()
        data = RawData(
            flags="real forward",
            variables=["time", "V(in)", "V(out)"],
            values={
                "time": [0, 1, 2, 3, 4, 5],
                "V(in)": [99, 0, 0, 2, 2, 99],
                "V(out)": [0, 2, 2, 2, 0, 0],
            },
        )
        requirements = [
            {
                "metric": "maximum",
                "operator": "<=",
                "target": 2,
                "window_start": 1,
                "window_end": 4,
            },
            {
                "metric": "propagation_delay",
                "operator": "<=",
                "target": 2,
                "window_start": 1,
                "window_end": 4,
                "primary_threshold": 1,
                "secondary_threshold": 1,
                "primary_edge": "rising",
                "secondary_edge": "falling",
            },
            {
                "metric": "forbidden_region_samples",
                "operator": "<=",
                "target": 0,
                "forbidden_min": 1.5,
                "forbidden_max": 2.5,
                "secondary_forbidden_min": 3.5,
                "secondary_forbidden_max": 4.0,
            },
        ]
        with patch.object(mcp_server.raw_parser, "parse_raw", return_value=data):
            result = mcp_server.analyze_waveform(
                str(run_dir),
                "V(in)",
                requirements,
                secondary_variable="V(out)",
                signal_unit="V",
            )

        self.assertEqual(result["source_points"], 6)
        self.assertEqual(result["secondary_variable"], "V(out)")
        self.assertTrue(result["all_passed"])
        self.assertEqual(result["results"][0]["evidence"]["index"], 3)
        self.assertEqual(result["results"][1]["value"], 1.0)
        self.assertEqual(result["results"][2]["value"], 0.0)

    def test_analyze_waveform_schema_advertises_phase_1_contract(self) -> None:
        tools = asyncio.run(mcp_server.mcp.list_tools())
        tool = next(tool for tool in tools if tool.name == "analyze_waveform")
        schema = tool.input_schema
        requirement = schema["$defs"]["WaveformRequirement"]["properties"]

        self.assertIn("secondary_variable", schema["properties"])
        self.assertIn("window_start", requirement)
        self.assertIn("propagation_delay", requirement["metric"]["enum"])
        self.assertIn("spectral_peak", requirement["metric"]["enum"])
        self.assertIn("phase_margin", requirement["metric"]["enum"])
        self.assertEqual(requirement["maximum_harmonic"]["type"], "integer")
        self.assertIn("frequency_min", requirement)
        self.assertEqual(requirement["primary_edge"]["enum"], ["rising", "falling"])
        self.assertIsNone(schema["properties"]["axis_unit"]["default"])
        secondary_output = tool.output_schema["properties"]["secondary_variable"]
        self.assertIn({"type": "null"}, secondary_output["anyOf"])

    def test_analyze_waveform_through_mcp_protocol(self) -> None:
        run_dir = self.make_run()
        (run_dir / "result.raw").touch()
        data = RawData(
            flags="real forward",
            variables=["time", "V(out)"],
            values={"time": [0, 1, 2], "V(out)": [0, 2, 1]},
        )
        arguments = {
            "run_dir": str(run_dir),
            "variable": "V(out)",
            "requirements": [
                {"metric": "maximum", "operator": ">=", "target": 2}
            ],
        }
        with patch.object(mcp_server.raw_parser, "parse_raw", return_value=data):
            result = asyncio.run(mcp_server.mcp.call_tool("analyze_waveform", arguments))

        self.assertFalse(result.is_error)
        self.assertTrue(result.structured_content["all_passed"])
        self.assertIsNone(result.structured_content["secondary_variable"])

    def test_analyze_waveform_supports_spectral_metrics(self) -> None:
        run_dir = self.make_run()
        (run_dir / "transient.raw").touch()
        data = RawData(
            flags="real forward",
            variables=["time", "V(out)"],
            values={
                "time": [0, 0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2],
                "V(out)": [-1, 0, 1, 0, -1, 0, 1, 0, -1],
            },
        )
        with patch.object(mcp_server.raw_parser, "parse_raw", return_value=data):
            result = mcp_server.analyze_waveform(
                str(run_dir),
                "V(out)",
                [
                    {
                        "metric": "frequency",
                        "operator": ">=",
                        "target": 1,
                        "threshold_value": 0,
                        "edge": "rising",
                    }
                ],
            )

        self.assertTrue(result["all_passed"])
        self.assertEqual(result["results"][0]["value"], 1.0)
        self.assertEqual(result["results"][0]["unit"], "Hz")

    def test_analyze_waveform_supports_complex_ac_metrics(self) -> None:
        run_dir = self.make_run()
        (run_dir / "ac.raw").touch()
        frequency = [10, 100, 1000, 10000]
        gain_db = [0, 3, 0, -10]
        reference = [complex(2, 0)] * len(frequency)
        primary = [
            2 * cmath.rect(10 ** (gain / 20), 0.0) for gain in gain_db
        ]
        data = RawData(
            flags="complex forward log stepped",
            variables=["frequency", "V(in)", "V(out)"],
            values={
                "frequency": [
                    *(complex(value, 0) for value in frequency),
                    *(complex(value, 0) for value in frequency),
                ],
                "V(in)": [*reference, *reference],
                "V(out)": [complex(4, 0)] * len(frequency) + primary,
            },
            step_count=2,
            points_per_step=4,
        )
        requirements = [
            {
                "metric": "ac_gain_db",
                "operator": ">=",
                "target": 1.4,
                "frequency_value": math.sqrt(1000),
            },
            {
                "metric": "cutoff_frequency",
                "operator": "<=",
                "target": 2001,
                "reference_frequency": 10,
            },
        ]
        with patch.object(mcp_server.raw_parser, "parse_raw", return_value=data):
            result = mcp_server.analyze_waveform(
                str(run_dir),
                "V(out)",
                requirements,
                secondary_variable="V(in)",
                step_index=1,
            )

        self.assertEqual(result["source_points"], 4)
        self.assertTrue(result["all_passed"])
        self.assertAlmostEqual(result["results"][0]["value"], 1.5)
        self.assertAlmostEqual(result["results"][1]["value"], 2000.0)
        self.assertEqual(result["results"][1]["unit"], "Hz")

    def test_run_parameter_sweep(self) -> None:
        def execute(netlist: str, filename: str, ascii_raw: bool, timeout: int, dest: Path) -> Path:
            dest.mkdir(parents=True)
            return dest

        def summarize(output_dir: Path) -> dict[str, object]:
            return {
                "run_dir": str(output_dir),
                "status": "completed",
                "duration_seconds": 0.1,
                "measurements": {"gain": float(output_dir.name[-1])},
            }

        with (
            patch.object(mcp_server, "_run_netlist_text", side_effect=execute),
            patch.object(mcp_server, "_summarize_run", side_effect=summarize),
        ):
            result = mcp_server.run_parameter_sweep("R1 in out {value}", ["1k", "2k"])

        self.assertEqual(len(result["points"]), 2)
        self.assertTrue(Path(result["results_csv"]).is_file())
        with self.assertRaises(ValueError):
            mcp_server.run_parameter_sweep("R1 in out 1k", ["1k"])
        with self.assertRaisesRegex(ValueError, "limited"):
            mcp_server.run_parameter_sweep(
                "R1 in out {value}",
                ["1k"] * (mcp_server.MAX_LEGACY_SWEEP_POINTS + 1),
            )

    def test_run_experiment_expands_cartesian_points_and_reuses_requirements(self) -> None:
        rendered_netlists: list[str] = []

        def execute(
            netlist: str,
            filename: str,
            ascii_raw: bool,
            timeout: int,
            dest: Path,
        ) -> Path:
            rendered_netlists.append(netlist)
            dest.mkdir(parents=True)
            return dest

        def summarize(output_dir: Path) -> dict[str, object]:
            index = int(output_dir.name.rsplit("-", 1)[1])
            return {
                "status": "completed",
                "duration_seconds": 0.1,
                "measurements": {"gain": float(index)},
            }

        requirements = [
            {"metric": "maximum", "operator": "<=", "target": 3.3}
        ]

        def analyze(
            run_dir: str,
            variable: str,
            point_requirements: list[dict[str, object]],
            **options: object,
        ) -> dict[str, object]:
            index = int(Path(run_dir).name.rsplit("-", 1)[1])
            self.assertIs(point_requirements, requirements)
            self.assertEqual(options, {"signal_unit": "V"})
            return {"all_passed": index != 2, "results": [{"index": index}]}

        with (
            patch.object(mcp_server, "_run_netlist_text", side_effect=execute),
            patch.object(mcp_server, "_summarize_run", side_effect=summarize),
            patch.object(mcp_server, "analyze_waveform", side_effect=analyze) as analysis,
        ):
            result = mcp_server.run_experiment(
                "R1 in out {R}\nC1 out 0 {C}\n.end\n",
                [
                    {"name": "R", "values": ["1k", "2k"], "unit": "ohm"},
                    {"name": "C", "values": ["10n", "22n"], "unit": "F"},
                ],
                [
                    {
                        "name": "output_limit",
                        "variable": "V(out)",
                        "signal_unit": "V",
                        "requirements": requirements,
                    }
                ],
            )

        self.assertEqual(
            rendered_netlists,
            [
                "R1 in out 1k\nC1 out 0 10n\n.end\n",
                "R1 in out 1k\nC1 out 0 22n\n.end\n",
                "R1 in out 2k\nC1 out 0 10n\n.end\n",
                "R1 in out 2k\nC1 out 0 22n\n.end\n",
            ],
        )
        self.assertEqual(result["parameter_order"], ["R", "C"])
        self.assertEqual(result["parameter_units"], {"R": "ohm", "C": "F"})
        self.assertEqual(result["point_count"], 4)
        self.assertEqual(result["completed_points"], 4)
        self.assertEqual(result["error_points"], 0)
        self.assertEqual(result["passed_points"], 3)
        self.assertEqual(result["failed_points"], 1)
        self.assertFalse(result["all_passed"])
        self.assertEqual(analysis.call_count, 4)
        self.assertEqual(
            [point["parameters"] for point in result["points"]],
            [
                {"R": "1k", "C": "10n"},
                {"R": "1k", "C": "22n"},
                {"R": "2k", "C": "10n"},
                {"R": "2k", "C": "22n"},
            ],
        )

        persisted = json.loads(Path(result["results_json"]).read_text())
        manifest = json.loads(Path(result["manifest"]).read_text())
        with Path(result["results_csv"]).open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(persisted["points"], result["points"])
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["definition"]["parameter_units"], {"R": "ohm", "C": "F"})
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[1]["parameter.C"], "22n")
        self.assertEqual(rows[3]["measurement.gain"], "3.0")

    def test_statistical_plan_runs_paired_points_without_cartesian_expansion(self) -> None:
        variables = [
            {
                "name": "R",
                "distribution": "uniform",
                "minimum": 9_000.0,
                "maximum": 11_000.0,
                "unit": "ohm",
            },
            {
                "name": "C",
                "distribution": "gaussian",
                "minimum": 8e-7,
                "maximum": 1.2e-6,
                "nominal": 1e-6,
                "sigma": 5e-8,
                "unit": "F",
            },
            {
                "name": "GAIN",
                "distribution": "discrete",
                "values": ["0.9", "1", "1.1"],
                "weights": [1, 3, 1],
                "nominal": "1",
            },
        ]
        plan = mcp_server.generate_statistical_plan(variables, 3, 20260824)
        rendered_netlists: list[str] = []

        def execute(
            netlist: str,
            filename: str,
            ascii_raw: bool,
            timeout: int,
            dest: Path,
        ) -> Path:
            rendered_netlists.append(netlist)
            dest.mkdir(parents=True)
            return dest

        with (
            patch.object(mcp_server, "_run_netlist_text", side_effect=execute),
            patch.object(
                mcp_server,
                "_summarize_run",
                return_value={"status": "completed", "measurements": {}},
            ),
        ):
            result = mcp_server.run_statistical_experiment(
                plan["plan_id"],
                "* Mixed statistical study\nV1 in 0 AC {GAIN}\n"
                "R1 in out {R}\nC1 out 0 {C}\n.end\n",
            )

        self.assertEqual(result["point_count"], 3)
        self.assertEqual(result["parameter_order"], ["R", "C", "GAIN"])
        self.assertEqual(
            [point["parameters"] for point in result["points"]],
            [point["parameters"] for point in plan["points"]],
        )
        self.assertEqual(
            rendered_netlists,
            [
                "* Mixed statistical study\n"
                f"V1 in 0 AC {point['parameters']['GAIN']}\n"
                f"R1 in out {point['parameters']['R']}\n"
                f"C1 out 0 {point['parameters']['C']}\n.end\n"
                for point in plan["points"]
            ],
        )
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["definition"]["point_plan"]["source"]["plan_id"],
            plan["plan_id"],
        )
        index_result = mcp_server.experiment_index.build_experiment_index(self.runs)
        self.assertEqual(index_result["indexed_experiments"], 1)
        self.assertEqual(index_result["issues"], [])
        report = mcp_server.experiment_report.build_experiment_report(
            self.runs, result["experiment_id"]
        )
        self.assertTrue(Path(report["report_html"]).is_file())
        report_html = Path(report["report_html"]).read_text(encoding="utf-8")
        self.assertIn("Statistical yield", report_html)
        self.assertIn("statistics.json", report_html)
        self.assertIn("Wilson 95% interval", report_html)
        statistics = mcp_server.summarize_statistical_experiment(
            result["experiment_id"]
        )
        self.assertEqual(statistics["evaluated_points"], 3)
        self.assertEqual(statistics["observed_yield"], 1.0)
        self.assertTrue(Path(statistics["statistics_json"]).is_file())
        self.assertTrue(Path(statistics["statistics_csv"]).is_file())
        comparison = mcp_server.compare_experiments(
            result["experiment_id"], result["experiment_id"]
        )
        self.assertEqual(comparison["matched_points"], 3)
        self.assertEqual(comparison["added_points"], 0)
        self.assertEqual(comparison["removed_points"], 0)

    def test_statistical_plan_tools_are_exposed_through_mcp(self) -> None:
        tools = asyncio.run(mcp_server.mcp.list_tools())
        by_name = {tool.name: tool for tool in tools}
        self.assertIn("generate_statistical_plan", by_name)
        self.assertIn("get_statistical_plan", by_name)
        self.assertIn("run_statistical_experiment", by_name)
        self.assertIn("define_statistical_study", by_name)
        self.assertIn("summarize_statistical_experiment", by_name)
        properties = by_name["generate_statistical_plan"].input_schema["properties"]
        self.assertEqual(properties["sample_count"]["type"], "integer")
        self.assertEqual(properties["seed"]["type"], "integer")

    def test_durable_statistical_study_executes_frozen_plan_without_resampling(
        self,
    ) -> None:
        plan_result = mcp_server.generate_statistical_plan(
            [
                {
                    "name": "R",
                    "distribution": "uniform",
                    "minimum": 9_000.0,
                    "maximum": 11_000.0,
                }
            ],
            3,
            31,
        )
        expected_points = [point["parameters"] for point in plan_result["points"]]
        executed_points: list[dict[str, str]] = []
        manager = mcp_server.ExperimentJobManager(self.runs, workers=1)

        def execute_point(
            index: int,
            combination: dict[str, str],
            point_dir: Path,
            *args: object,
        ) -> dict[str, object]:
            executed_points.append(combination)
            point_dir.mkdir(parents=True)
            return {
                "index": index,
                "parameters": combination,
                "run_dir": str(point_dir),
                "simulation_status": "completed",
                "duration_seconds": 0.01,
                "measurements": {},
                "analyses": [],
                "all_passed": True,
                "error": None,
            }

        try:
            with patch.object(mcp_server, "_get_experiment_manager", return_value=manager):
                defined = mcp_server.define_statistical_study(
                    plan_result["plan_id"],
                    "R1 in 0 {R}\n.end\n",
                    max_concurrency=1,
                )
            manifest = json.loads(Path(defined["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["definition"]["point_plan"]["points"], expected_points
            )
            with (
                patch.object(
                    mcp_server.statistical_engine,
                    "load_statistical_plan",
                    side_effect=AssertionError("durable execution must not reload a plan"),
                ),
                patch.object(
                    mcp_server,
                    "_execute_experiment_point",
                    side_effect=execute_point,
                ),
            ):
                manager.start(defined["experiment_id"])
                finished = manager.wait(defined["experiment_id"])
        finally:
            manager.shutdown()

        self.assertEqual(finished["status"], "completed")
        self.assertEqual(executed_points, expected_points)

    def test_durable_statistical_resume_keeps_checkpointed_duplicate_samples(
        self,
    ) -> None:
        plan = mcp_server.generate_statistical_plan(
            [
                {
                    "name": "R",
                    "distribution": "discrete",
                    "values": ["10k"],
                    "weights": [1],
                }
            ],
            3,
            43,
        )
        source = {
            "kind": "statistical",
            "plan_id": plan["plan_id"],
            "plan_sha256": plan["plan_sha256"],
        }
        first_manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
        defined = first_manager.define_explicit(
            "R1 in 0 {R}\n.end\n",
            ["R"],
            [point["parameters"] for point in plan["points"]],
            {"R": ""},
            source,
            max_concurrency=1,
        )
        first_manager.shutdown()
        experiment_dir = Path(defined["experiment_dir"])
        point_zero = experiment_dir / "point-0000"
        point_zero.mkdir()
        mcp_server._write_json(
            point_zero / "point_result.json",
            {
                "index": 0,
                "parameters": {"R": "10k"},
                "run_dir": str(point_zero / "attempt-0000"),
                "simulation_status": "completed",
                "duration_seconds": 0.01,
                "measurements": {},
                "analyses": [],
                "all_passed": True,
                "error": None,
            },
        )
        manifest_path = Path(defined["manifest"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = "running"
        mcp_server._write_json(manifest_path, manifest)
        calls: list[int] = []

        def execute_point(
            index: int,
            combination: dict[str, str],
            point_dir: Path,
            *args: object,
        ) -> dict[str, object]:
            calls.append(index)
            return {
                "index": index,
                "parameters": combination,
                "run_dir": str(point_dir),
                "simulation_status": "completed",
                "duration_seconds": 0.01,
                "measurements": {},
                "analyses": [],
                "all_passed": True,
                "error": None,
            }

        with patch.object(mcp_server, "_execute_experiment_point", side_effect=execute_point):
            resumed_manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
            try:
                finished = resumed_manager.wait(defined["experiment_id"])
            finally:
                resumed_manager.shutdown()

        self.assertEqual(finished["status"], "completed")
        self.assertEqual(calls, [1, 2])
        results = json.loads(Path(finished["results_json"]).read_text(encoding="utf-8"))
        self.assertEqual(
            [point["parameters"] for point in results["points"]],
            [{"R": "10k"}, {"R": "10k"}, {"R": "10k"}],
        )

    def test_cancelled_statistical_study_reports_unfinished_points_separately(
        self,
    ) -> None:
        manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
        started = threading.Event()
        release = threading.Event()

        def execute_point(
            index: int,
            combination: dict[str, str],
            point_dir: Path,
            *args: object,
        ) -> dict[str, object]:
            started.set()
            release.wait(2)
            return {
                "index": index,
                "parameters": combination,
                "run_dir": str(point_dir),
                "simulation_status": "completed",
                "duration_seconds": 0.01,
                "measurements": {},
                "analyses": [],
                "all_passed": True,
                "error": None,
            }

        try:
            with patch.object(mcp_server, "_execute_experiment_point", side_effect=execute_point):
                defined = manager.define_explicit(
                    "R1 in 0 {R}\n.end\n",
                    ["R"],
                    [{"R": "1k"}, {"R": "2k"}, {"R": "3k"}],
                    {"R": ""},
                    {"kind": "statistical", "plan_id": "statistical-plan-test"},
                    max_concurrency=1,
                )
                manager.start(defined["experiment_id"])
                self.assertTrue(started.wait(2))
                manager.cancel(defined["experiment_id"])
                release.set()
                finished = manager.wait(defined["experiment_id"])
            summary = mcp_server.summarize_statistical_experiment(
                defined["experiment_id"]
            )
            data = json.loads(Path(summary["statistics_json"]).read_text(encoding="utf-8"))
        finally:
            release.set()
            manager.shutdown()

        self.assertEqual(finished["status"], "cancelled")
        self.assertEqual(data["classifications"]["electrical_pass"], 1)
        self.assertEqual(data["classifications"]["unfinished"], 2)
        self.assertEqual(summary["invalid_points"], 2)
        self.assertEqual(summary["observed_yield"], 1.0)

    def test_discrete_duplicate_samples_remain_distinct_by_ordinal(self) -> None:
        plan = mcp_server.generate_statistical_plan(
            [
                {
                    "name": "R",
                    "distribution": "discrete",
                    "values": ["10k"],
                    "weights": [1],
                }
            ],
            3,
            19,
        )

        def execute(
            netlist: str,
            filename: str,
            ascii_raw: bool,
            timeout: int,
            dest: Path,
        ) -> Path:
            dest.mkdir(parents=True)
            return dest

        with (
            patch.object(mcp_server, "_run_netlist_text", side_effect=execute),
            patch.object(
                mcp_server,
                "_summarize_run",
                return_value={"status": "completed", "measurements": {}},
            ),
        ):
            result = mcp_server.run_statistical_experiment(
                plan["plan_id"], "* Discrete duplicate proof\nR1 in 0 {R}\n.end\n"
            )

        self.assertEqual(
            [point["parameters"] for point in result["points"]],
            [{"R": "10k"}, {"R": "10k"}, {"R": "10k"}],
        )
        indexed = mcp_server.experiment_index.build_experiment_index(self.runs)
        self.assertEqual(indexed["indexed_experiments"], 1)
        self.assertEqual(indexed["issues"], [])
        comparison = mcp_server.compare_experiments(
            result["experiment_id"], result["experiment_id"]
        )
        self.assertEqual(comparison["matched_points"], 3)
        report = mcp_server.experiment_report.build_experiment_report(
            self.runs, result["experiment_id"]
        )
        self.assertTrue(Path(report["report_html"]).is_file())

    def test_run_experiment_native_maps_one_validated_batch(self) -> None:
        rendered_netlists: list[str] = []

        def execute(
            netlist: str,
            filename: str,
            ascii_raw: bool,
            timeout: int,
            dest: Path,
        ) -> Path:
            rendered_netlists.append(netlist)
            dest.mkdir(parents=True)
            (dest / "circuit.log").write_text(
                "\n".join(
                    [
                        ".step __mcp_step_index=0",
                        ".step __mcp_step_index=1",
                        ".step __mcp_step_index=2",
                        ".step __mcp_step_index=3",
                        "Measurement: gain",
                        " step value",
                        " 1 -1.0",
                        " 2 -2.0",
                        " 3 -3.0",
                        " 4 -4.0",
                    ]
                ),
                encoding="utf-16le",
            )
            (dest / "circuit.raw").write_bytes(b"raw")
            return dest

        raw_data = RawData(
            flags="real stepped",
            variables=["time", "V(out)"],
            values={
                "time": [0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0],
                "V(out)": [0.0] * 8,
            },
            step_count=4,
            points_per_step=2,
        )
        analyzed_steps: list[int] = []

        def analyze(*args: object, **kwargs: object) -> dict[str, object]:
            analyzed_steps.append(int(kwargs["step_index"]))
            return {"all_passed": True, "results": []}

        with (
            patch.object(mcp_server, "_run_netlist_text", side_effect=execute) as run,
            patch.object(
                mcp_server,
                "_summarize_run",
                return_value={
                    "status": "completed",
                    "duration_seconds": 0.2,
                    "execution_source": "simulator",
                    "measurements": {"tnom": 27.0},
                },
            ),
            patch.object(mcp_server.raw_parser, "parse_raw", return_value=raw_data),
            patch.object(mcp_server, "analyze_waveform", side_effect=analyze),
        ):
            result = mcp_server.run_experiment(
                "R1 in out {R}\nC1 out 0 {C}\n.param tau={TAU}\n.end\n",
                [
                    {"name": "R", "values": ["1k", "2k"]},
                    {"name": "C", "values": ["10n", "20n"]},
                ],
                [
                    {
                        "name": "output",
                        "variable": "V(out)",
                        "requirements": [
                            {"metric": "maximum", "operator": "<=", "target": 1}
                        ],
                    }
                ],
                derived_parameters=[{"name": "TAU", "template": "({R})*({C})"}],
                execution_mode="native",
            )

        self.assertEqual(run.call_count, 1)
        self.assertEqual(analyzed_steps, [0, 1, 2, 3])
        self.assertEqual(result["execution_mode"], "native")
        self.assertEqual(result["native_batch"]["validated_step_order"], [0, 1, 2, 3])
        self.assertEqual(
            [point["measurements"]["gain"] for point in result["points"]],
            [-1.0, -2.0, -3.0, -4.0],
        )
        self.assertTrue(
            all(point["measurements"]["tnom"] == 27.0 for point in result["points"])
        )
        self.assertEqual(
            [point["parameters"] for point in result["points"]],
            [
                {"R": "1k", "C": "10n", "TAU": "(1k)*(10n)"},
                {"R": "1k", "C": "20n", "TAU": "(1k)*(20n)"},
                {"R": "2k", "C": "10n", "TAU": "(2k)*(10n)"},
                {"R": "2k", "C": "20n", "TAU": "(2k)*(20n)"},
            ],
        )
        self.assertTrue(all(point["duration_seconds"] is None for point in result["points"]))
        deck = rendered_netlists[0]
        self.assertIn("R1 in out {__mcp_value_0}", deck)
        self.assertIn(".param tau={__mcp_value_2}\n", deck)
        self.assertIn(".param __mcp_value_2=table(__mcp_step_index", deck)
        self.assertIn(".step param __mcp_step_index 0 3 1", deck)
        self.assertLess(deck.index(".step param"), deck.index(".end"))

    def test_run_experiment_native_fails_closed_on_step_order_mismatch(self) -> None:
        def execute(
            netlist: str,
            filename: str,
            ascii_raw: bool,
            timeout: int,
            dest: Path,
        ) -> Path:
            dest.mkdir(parents=True)
            (dest / "circuit.log").write_text(
                ".step __mcp_step_index=1\n.step __mcp_step_index=0\n",
                encoding="utf-16le",
            )
            return dest

        with (
            patch.object(mcp_server, "_run_netlist_text", side_effect=execute),
            patch.object(
                mcp_server,
                "_summarize_run",
                return_value={
                    "status": "completed",
                    "duration_seconds": 0.1,
                    "execution_source": "cache",
                    "cache": {"hit": True, "key": "native-cache-key"},
                },
            ),
            patch.object(mcp_server, "analyze_waveform") as analyze,
        ):
            result = mcp_server.run_experiment(
                "R1 in out {R}\n.end\n",
                [{"name": "R", "values": ["1k", "2k"]}],
                execution_mode="native",
            )

        analyze.assert_not_called()
        self.assertEqual(result["completed_points"], 0)
        self.assertEqual(result["error_points"], 2)
        self.assertIn("step order mismatch", result["points"][0]["error"])
        self.assertEqual(result["native_batch"]["execution_source"], "cache")
        self.assertTrue(result["native_batch"]["cache_hit"])
        self.assertEqual(result["native_batch"]["cache_key"], "native-cache-key")

        def execute_bad_row(
            netlist: str,
            filename: str,
            ascii_raw: bool,
            timeout: int,
            dest: Path,
        ) -> Path:
            dest.mkdir(parents=True)
            (dest / "circuit.log").write_text(
                ".step __mcp_step_index=0\n.step __mcp_step_index=1\n"
                "Measurement: gain\n step value\n 3 -3.0\n",
                encoding="utf-16le",
            )
            return dest

        with (
            patch.object(mcp_server, "_run_netlist_text", side_effect=execute_bad_row),
            patch.object(
                mcp_server,
                "_summarize_run",
                return_value={"status": "completed", "duration_seconds": 0.1},
            ),
        ):
            bad_row = mcp_server.run_experiment(
                "R1 in out {R}\n.end\n",
                [{"name": "R", "values": ["1k", "2k"]}],
                execution_mode="native",
            )
        self.assertIn("invalid step rows", bad_row["points"][0]["error"])

    def test_run_experiment_native_validates_each_selected_raw_file(self) -> None:
        def execute(
            netlist: str,
            filename: str,
            ascii_raw: bool,
            timeout: int,
            dest: Path,
        ) -> Path:
            dest.mkdir(parents=True)
            (dest / "circuit.log").write_text(
                ".step __mcp_step_index=0\n.step __mcp_step_index=1\n",
                encoding="utf-16le",
            )
            (dest / "circuit.raw").write_bytes(b"default")
            (dest / "custom.raw").write_bytes(b"custom")
            return dest

        valid = RawData(
            flags="real stepped",
            variables=["time", "V(out)"],
            values={"time": [0.0, 1.0, 0.0, 1.0], "V(out)": [0.0] * 4},
            step_count=2,
            points_per_step=2,
        )
        invalid = RawData(
            flags="real",
            variables=["time", "V(out)"],
            values={"time": [0.0, 1.0], "V(out)": [0.0, 0.0]},
            step_count=1,
            points_per_step=2,
        )

        with (
            patch.object(mcp_server, "_run_netlist_text", side_effect=execute),
            patch.object(
                mcp_server,
                "_summarize_run",
                return_value={"status": "completed", "duration_seconds": 0.1},
            ),
            patch.object(
                mcp_server.raw_parser,
                "parse_raw",
                side_effect=lambda path: invalid if path.name == "custom.raw" else valid,
            ),
            patch.object(mcp_server, "analyze_waveform") as analyze,
        ):
            result = mcp_server.run_experiment(
                "R1 in out {R}\n.end\n",
                [{"name": "R", "values": ["1k", "2k"]}],
                [
                    {
                        "name": "custom",
                        "variable": "V(out)",
                        "raw_filename": "custom.raw",
                        "requirements": [
                            {"metric": "maximum", "operator": "<=", "target": 1}
                        ],
                    }
                ],
                execution_mode="native",
            )

        analyze.assert_not_called()
        self.assertIn("custom.raw", result["points"][0]["error"])
        self.assertEqual(result["completed_points"], 0)

    def test_native_step_range_and_parameter_tables_stay_within_line_limit(self) -> None:
        values = [str(1000 + index) for index in range(40)]
        _, _, combinations, _ = mcp_server._prepare_experiment(
            "R1 in out {R}\n.end\n",
            [{"name": "R", "values": values}],
            [],
        )
        deck = mcp_server._render_native_experiment_netlist(
            "R1 in out {R}\n.end\n", ["R"], [], combinations
        )
        lines = deck.splitlines()
        step_line = next(line for line in lines if line.startswith(".step"))
        self.assertEqual(step_line, ".step param __mcp_step_index 0 39 1")
        self.assertLessEqual(max(map(len, lines)), 78)
        crlf_deck = mcp_server._render_native_experiment_netlist(
            "R1 in out {R}\r\n.end\r\n", ["R"], [], combinations
        )
        self.assertNotIn("\n", crlf_deck.replace("\r\n", ""))

    def test_run_experiment_native_rejects_unsafe_decks_before_output(self) -> None:
        invalid = [
            ("R1 in out {R}\n.step param X 1 2\n.end\n", "existing .step"),
            ("R1 in out {R}\n.param __MCP_X=1\n.end\n", "reserves"),
            (".include {R}\n.end\n", "directive context"),
            ("R1 in out {R}\n", "exactly one"),
            ("R1 in out {R}\n.end\n.end\n", "exactly one"),
        ]
        for template, message in invalid:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                mcp_server.run_experiment(
                    template,
                    [{"name": "R", "values": ["1k"]}],
                    execution_mode="native",
                )
        with self.assertRaisesRegex(ValueError, "safe numeric expressions"):
            mcp_server.run_experiment(
                "R1 in out {R}\n.end\n",
                [{"name": "R", "values": ["1k,2k"]}],
                execution_mode="native",
            )
        with self.assertRaisesRegex(ValueError, "expression is too long"):
            mcp_server.run_experiment(
                "R1 in out {R}\n.end\n",
                [{"name": "R", "values": ["1" * 80]}],
                execution_mode="native",
            )
        with self.assertRaisesRegex(ValueError, "execution_mode"):
            mcp_server.run_experiment(
                "R1 in out {R}\n.end\n",
                [{"name": "R", "values": ["1k"]}],
                execution_mode="automatic",
            )
        self.assertEqual(list(self.runs.iterdir()), [])

    def test_run_experiment_substitutes_once_and_writes_utf8_artifacts(self) -> None:
        rendered_netlists: list[str] = []

        def execute(
            netlist: str,
            filename: str,
            ascii_raw: bool,
            timeout: int,
            dest: Path,
        ) -> Path:
            rendered_netlists.append(netlist)
            dest.mkdir(parents=True)
            return dest

        with (
            patch.object(mcp_server, "_run_netlist_text", side_effect=execute),
            patch.object(
                mcp_server,
                "_summarize_run",
                return_value={"status": "completed", "measurements": {}},
            ),
        ):
            result = mcp_server.run_experiment(
                "A={A} B={B}\n.end\n",
                [
                    {"name": "A", "values": ["{B}µ"], "unit": "Ω"},
                    {"name": "B", "values": ["10n"]},
                ],
            )

        self.assertEqual(rendered_netlists, ["A={B}µ B=10n\n.end\n"])
        persisted = json.loads(Path(result["results_json"]).read_text(encoding="utf-8"))
        with Path(result["results_csv"]).open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(persisted["parameter_units"]["A"], "Ω")
        self.assertEqual(rows[0]["parameter.A"], "{B}µ")

    def test_run_experiment_resolves_derived_parameters_topologically(self) -> None:
        rendered_netlists: list[str] = []

        def execute(
            netlist: str,
            filename: str,
            ascii_raw: bool,
            timeout: int,
            dest: Path,
        ) -> Path:
            rendered_netlists.append(netlist)
            dest.mkdir(parents=True)
            return dest

        with (
            patch.object(mcp_server, "_run_netlist_text", side_effect=execute),
            patch.object(
                mcp_server,
                "_summarize_run",
                return_value={"status": "completed", "measurements": {}},
            ),
        ):
            result = mcp_server.run_experiment(
                ".param doubled {DOUBLE}\n.end\n",
                [
                    {"name": "R", "values": ["1k", "2k"], "unit": "ohm"},
                    {"name": "C", "values": ["10n", "22n"], "unit": "F"},
                ],
                derived_parameters=[
                    {"name": "DOUBLE", "template": "2*{RC}", "unit": "s"},
                    {"name": "RC", "template": "({R})*({C})", "unit": "s"},
                ],
            )

        self.assertEqual(
            rendered_netlists,
            [
                ".param doubled 2*(1k)*(10n)\n.end\n",
                ".param doubled 2*(1k)*(22n)\n.end\n",
                ".param doubled 2*(2k)*(10n)\n.end\n",
                ".param doubled 2*(2k)*(22n)\n.end\n",
            ],
        )
        self.assertEqual(result["point_count"], 4)
        self.assertEqual(result["parameter_order"], ["R", "C"])
        self.assertEqual(result["derived_parameter_order"], ["DOUBLE", "RC"])
        self.assertEqual(
            result["points"][0]["parameters"],
            {"R": "1k", "C": "10n", "DOUBLE": "2*(1k)*(10n)", "RC": "(1k)*(10n)"},
        )
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        with Path(result["results_csv"]).open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(
            manifest["definition"]["derived_parameters"][0]["name"],
            "DOUBLE",
        )
        self.assertEqual(rows[0]["parameter.DOUBLE"], "2*(1k)*(10n)")
        self.assertEqual(rows[0]["parameter.RC"], "(1k)*(10n)")

    def test_derived_parameter_validation_precedes_execution(self) -> None:
        invalid_definitions = [
            (
                [{"name": "D", "template": "{UNKNOWN}"}],
                "derived parameter D references unknown parameter UNKNOWN",
            ),
            (
                [{"name": "R", "template": "{R}"}],
                "duplicate experiment parameter name: R",
            ),
            (
                [
                    {"name": "A", "template": "{B}"},
                    {"name": "B", "template": "{A}"},
                ],
                "derived parameter cycle: A -> B -> A",
            ),
            (
                [{"name": "D", "template": "{D}"}],
                "derived parameter cycle: D -> D",
            ),
            (
                [{"name": "D", "template": "{R}"}],
                "experiment parameter D is not reachable",
            ),
        ]
        with patch.object(mcp_server, "_run_netlist_text") as execution:
            for derived_parameters, message in invalid_definitions:
                netlist = "R1 in out {R}\nA={A}\n.end\n"
                with self.subTest(message=message), self.assertRaisesRegex(
                    ValueError, message
                ):
                    mcp_server.run_experiment(
                        netlist,
                        [{"name": "R", "values": ["1k"]}],
                        derived_parameters=derived_parameters,
                    )
        execution.assert_not_called()

        with patch.object(mcp_server, "_run_netlist_text") as execution:
            with self.assertRaisesRegex(ValueError, "parameter C is not reachable"):
                mcp_server.run_experiment(
                    "X={D}\n.end\n",
                    [
                        {"name": "R", "values": ["1k"]},
                        {"name": "C", "values": ["1u"]},
                    ],
                    derived_parameters=[{"name": "D", "template": "{R}"}],
                )
        execution.assert_not_called()

    def test_derived_values_preserve_inserted_placeholder_text(self) -> None:
        _, derived_order, points, _ = mcp_server._prepare_experiment(
            "X={D}\n.end\n",
            [{"name": "R", "values": ["{LITERAL}"]}],
            [],
            [{"name": "D", "template": "value={R}"}],
        )
        self.assertEqual(derived_order, ["D"])
        self.assertEqual(points[0]["D"], "value={LITERAL}")
        self.assertEqual(
            mcp_server._render_experiment_netlist("X={D}", points[0]),
            "X=value={LITERAL}",
        )

    def test_run_experiment_isolates_simulation_and_analysis_errors(self) -> None:
        def execute(
            netlist: str,
            filename: str,
            ascii_raw: bool,
            timeout: int,
            dest: Path,
        ) -> Path:
            if dest.name == "point-0001":
                raise RuntimeError("LTspice rejected point")
            dest.mkdir(parents=True)
            return dest

        def summarize(output_dir: Path) -> dict[str, object]:
            return {"status": "completed", "measurements": {}}

        def analyze(
            run_dir: str, variable: str, *args: object, **kwargs: object
        ) -> dict[str, object]:
            if Path(run_dir).name == "point-0000" and variable == "V(bad)":
                raise ValueError("missing crossing")
            return {"all_passed": True, "results": []}

        with (
            patch.object(mcp_server, "_run_netlist_text", side_effect=execute) as execution,
            patch.object(mcp_server, "_summarize_run", side_effect=summarize),
            patch.object(mcp_server, "analyze_waveform", side_effect=analyze) as analysis,
        ):
            result = mcp_server.run_experiment(
                "R1 in out {R}\n.end\n",
                [{"name": "R", "values": ["1k", "2k", "3k"]}],
                [
                    {
                        "name": "bad_gain",
                        "variable": "V(bad)",
                        "requirements": [
                            {"metric": "maximum", "operator": "<=", "target": 1}
                        ],
                    },
                    {
                        "name": "gain",
                        "variable": "V(out)",
                        "requirements": [
                            {"metric": "maximum", "operator": "<=", "target": 1}
                        ],
                    }
                ],
            )

        self.assertEqual(execution.call_count, 3)
        self.assertEqual(analysis.call_count, 4)
        self.assertEqual(result["completed_points"], 2)
        self.assertEqual(result["error_points"], 2)
        self.assertEqual(result["passed_points"], 1)
        self.assertEqual(result["failed_points"], 2)
        self.assertEqual(result["points"][0]["analyses"][0]["status"], "error")
        self.assertEqual(result["points"][0]["analyses"][1]["status"], "completed")
        self.assertEqual(result["points"][1]["simulation_status"], "error")
        self.assertTrue(result["points"][2]["all_passed"])

    def test_run_experiment_validates_the_definition_before_execution(self) -> None:
        valid_analysis = [
            {
                "name": "output",
                "variable": "V(out)",
                "requirements": [
                    {"metric": "maximum", "operator": "<=", "target": 1}
                ],
            }
        ]
        oversized_values = [str(index) for index in range(32)]
        invalid_definitions = [
            ([], valid_analysis, "non-empty"),
            ([{"name": "1R", "values": ["1k"]}], valid_analysis, "names"),
            (
                [
                    {"name": "R", "values": ["1k"]},
                    {"name": "R", "values": ["2k"]},
                ],
                valid_analysis,
                "duplicate parameter",
            ),
            ([{"name": "R", "values": []}], valid_analysis, "non-empty"),
            ([{"name": "R", "values": ["1k", "1k"]}], valid_analysis, "unique"),
            ([{"name": "L", "values": ["1m"]}], valid_analysis, "placeholder"),
            (
                [
                    {"name": "R", "values": oversized_values},
                    {"name": "C", "values": oversized_values},
                ],
                valid_analysis,
                "1000",
            ),
            (
                [{"name": "R", "values": ["1k"]}],
                [{"name": "output", "variable": "V(out)", "requirements": []}],
                "requirements",
            ),
            (
                [{"name": "R", "values": ["1k"]}],
                [*valid_analysis, *valid_analysis],
                "duplicate waveform analysis",
            ),
            (
                [{"name": "R", "values": ["1k"]}],
                [
                    {
                        "name": "output",
                        "variable": "V(out)",
                        "requirements": [
                            {"metric": "maximum", "operator": "<=", "target": 1}
                        ]
                        * (mcp_server.experiment_engine.MAX_REQUIREMENTS_PER_EXPERIMENT + 1),
                    }
                ],
                "256 waveform requirements",
            ),
        ]
        with patch.object(mcp_server, "_run_netlist_text") as execution:
            for parameters, analyses, message in invalid_definitions:
                template = "R1 in out {R}\nC1 out 0 {C}\n.end\n"
                with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                    mcp_server.run_experiment(template, parameters, analyses)
        execution.assert_not_called()

    def test_run_experiment_schema_advertises_ordered_parameters(self) -> None:
        tools = asyncio.run(mcp_server.mcp.list_tools())
        tool = next(tool for tool in tools if tool.name == "run_experiment")
        parameter = tool.input_schema["$defs"]["ExperimentParameter"]
        derived = tool.input_schema["$defs"]["ExperimentDerivedParameter"]
        analysis = tool.input_schema["$defs"]["ExperimentWaveformAnalysis"]
        requirement = tool.input_schema["$defs"]["WaveformRequirement"]

        self.assertEqual(tool.input_schema["properties"]["parameters"]["type"], "array")
        self.assertEqual(parameter["properties"]["name"]["type"], "string")
        self.assertEqual(parameter["properties"]["values"]["items"]["type"], "string")
        self.assertEqual(parameter["properties"]["unit"]["type"], "string")
        self.assertIn("derived_parameters", tool.input_schema["properties"])
        self.assertEqual(derived["required"], ["name", "template"])
        self.assertEqual(derived["properties"]["name"]["type"], "string")
        self.assertEqual(derived["properties"]["template"]["type"], "string")
        self.assertEqual(derived["properties"]["unit"]["type"], "string")
        self.assertIn("requirements", analysis["properties"])
        self.assertEqual(
            requirement["properties"]["maximum_harmonic"]["type"],
            "integer",
        )
        self.assertIn("point_count", tool.output_schema["properties"])
        self.assertIn("derived_parameter_order", tool.output_schema["properties"])
        self.assertEqual(
            tool.input_schema["properties"]["execution_mode"]["default"],
            "independent",
        )
        self.assertEqual(
            tool.input_schema["properties"]["execution_mode"]["enum"],
            ["independent", "native"],
        )
        self.assertIn("native_batch", tool.output_schema["properties"])

    def test_run_experiment_through_mcp_protocol(self) -> None:
        arguments = {
            "netlist_template": ".param doubled {DOUBLE}\n.end\n",
            "parameters": [{"name": "R", "values": ["1k"]}],
            "derived_parameters": [
                {"name": "DOUBLE", "template": "2*{R}", "unit": "ohm"}
            ],
        }
        with (
            patch.object(
                mcp_server,
                "_run_netlist_text",
                side_effect=lambda netlist, filename, ascii_raw, timeout, dest: dest,
            ),
            patch.object(
                mcp_server,
                "_summarize_run",
                return_value={"status": "completed", "measurements": {}},
            ),
        ):
            result = asyncio.run(mcp_server.mcp.call_tool("run_experiment", arguments))

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["point_count"], 1)
        self.assertEqual(
            result.structured_content["points"][0]["parameters"]["DOUBLE"],
            "2*1k",
        )
        self.assertTrue(result.structured_content["all_passed"])

    def test_run_experiment_propagates_cache_and_records_provenance(self) -> None:
        def execute(
            netlist: str,
            filename: str,
            ascii_raw: bool,
            timeout: int,
            dest: Path,
            reuse_cache: bool,
        ) -> Path:
            self.assertTrue(reuse_cache)
            dest.mkdir(parents=True)
            return dest

        summary = {
            "status": "completed",
            "duration_seconds": 0.01,
            "measurements": {},
            "cache": {"hit": True, "key": "cache-key"},
        }
        with (
            patch.object(mcp_server, "_run_netlist_text", side_effect=execute),
            patch.object(mcp_server, "_summarize_run", return_value=summary),
        ):
            result = mcp_server.run_experiment(
                "R1 in out {R}\n.end\n",
                [{"name": "R", "values": ["1k"]}],
                reuse_cache=True,
            )

        self.assertTrue(result["points"][0]["cache_hit"])
        self.assertEqual(result["points"][0]["cache_key"], "cache-key")
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertTrue(manifest["definition"]["reuse_cache"])

    def test_durable_experiment_persists_and_forwards_cache_policy(self) -> None:
        manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
        calls: list[bool] = []

        def execute_point(
            index: int,
            combination: dict[str, str],
            point_dir: Path,
            netlist_template: str,
            filename: str,
            ascii_raw: bool,
            timeout_seconds: int,
            analyses: list[dict[str, object]],
            cancel_event: threading.Event,
            reuse_cache: bool,
        ) -> dict[str, object]:
            calls.append(reuse_cache)
            return {
                "index": index,
                "parameters": combination,
                "run_dir": str(point_dir),
                "simulation_status": "completed",
                "duration_seconds": 0.01,
                "measurements": {},
                "analyses": [],
                "all_passed": True,
                "error": None,
                "cache_hit": True,
                "cache_key": "cache-key",
            }

        try:
            with patch.object(
                mcp_server,
                "_execute_experiment_point",
                side_effect=execute_point,
            ):
                defined = manager.define(
                    "R1 in out {R}\n.end\n",
                    [{"name": "R", "values": ["1k"]}],
                    max_concurrency=1,
                    reuse_cache=True,
                )
                manager.start(defined["experiment_id"])
                finished = manager.wait(defined["experiment_id"])
        finally:
            manager.shutdown()

        self.assertEqual(finished["status"], "completed")
        self.assertEqual(calls, [True])
        manifest = json.loads(Path(defined["manifest"]).read_text(encoding="utf-8"))
        self.assertTrue(manifest["definition"]["reuse_cache"])

    def test_cache_option_is_exposed_on_all_mcp_simulation_tools(self) -> None:
        tools = asyncio.run(mcp_server.mcp.list_tools())
        by_name = {tool.name: tool for tool in tools}
        for name in (
            "run_netlist",
            "run_netlist_file",
            "run_parameter_sweep",
            "run_experiment",
            "define_experiment",
        ):
            with self.subTest(tool=name):
                cache_property = by_name[name].input_schema["properties"]["reuse_cache"]
                self.assertEqual(cache_property["type"], "boolean")
                self.assertFalse(cache_property["default"])

    def test_compare_experiments_reports_deltas_and_requirement_changes(self) -> None:
        baseline_id = "mcp-experiment-20260824-180000-000000-a1b2c3d4"
        candidate_id = "mcp-experiment-20260824-180100-000000-b1c2d3e4"
        self.make_experiment_result(
            baseline_id,
            [
                self.experiment_point(
                    0,
                    {"R": "1k"},
                    {"gain": 2.0, "baseline_only": 4.0},
                    [self.experiment_check(1200.0, True)],
                ),
                self.experiment_point(
                    1,
                    {"R": "2k"},
                    {},
                    [self.experiment_check(900.0, False)],
                ),
                self.experiment_point(2, {"R": "3k"}, {}, []),
            ],
        )
        self.make_experiment_result(
            candidate_id,
            [
                self.experiment_point(
                    0,
                    {"R": "1k"},
                    {"gain": 1.5},
                    [self.experiment_check(900.0, False)],
                ),
                self.experiment_point(
                    1,
                    {"R": "2k"},
                    {},
                    [self.experiment_check(1200.0, True)],
                ),
                self.experiment_point(
                    2,
                    {"R": "1000"},
                    {"candidate_only": 3.0},
                    [],
                ),
            ],
        )

        result = mcp_server.compare_experiments(baseline_id, candidate_id)

        self.assertEqual(
            (result["matched_points"], result["added_points"], result["removed_points"]),
            (2, 1, 1),
        )
        self.assertEqual(
            [point["status"] for point in result["points"]],
            ["matched", "matched", "removed", "added"],
        )
        matched = result["points"][0]
        gain = next(item for item in matched["measurements"] if item["name"] == "gain")
        self.assertEqual(gain["delta"], -0.5)
        self.assertEqual(result["matched_measurements"], 1)
        self.assertEqual(result["removed_measurements"], 1)
        self.assertEqual(result["added_measurements"], 1)
        self.assertEqual(result["requirement_regressions"], 1)
        self.assertEqual(result["requirement_improvements"], 1)
        self.assertEqual(matched["requirements"][0]["status"], "regression")
        self.assertEqual(result["points"][1]["requirements"][0]["status"], "improvement")
        first_json = Path(result["comparison_json"]).read_bytes()
        first_markdown = Path(result["comparison_markdown"]).read_bytes()
        repeated = mcp_server.compare_experiments(baseline_id, candidate_id)
        self.assertEqual(repeated["comparison_id"], result["comparison_id"])
        self.assertEqual(Path(repeated["comparison_json"]).read_bytes(), first_json)
        self.assertEqual(Path(repeated["comparison_markdown"]).read_bytes(), first_markdown)
        self.assertIn(b"bandwidth\\|check \\`&lt;img src=x&gt;", first_markdown)
        self.assertNotIn(b"<img", first_markdown)

    def test_compare_experiments_rejects_invalid_artifacts_before_output(self) -> None:
        baseline_id = "mcp-experiment-20260824-180000-000000-a1b2c3d4"
        candidate_id = "mcp-experiment-20260824-180100-000000-b1c2d3e4"
        duplicate = self.experiment_point(
            0,
            {"R": "1k"},
            {},
            [self.experiment_check(1200.0, True)],
        )
        self.make_experiment_result(baseline_id, [duplicate, {**duplicate, "index": 1}])
        self.make_experiment_result(candidate_id, [], status="running")

        with self.assertRaisesRegex(ValueError, "Cartesian definition"):
            mcp_server.compare_experiments(baseline_id, candidate_id)
        self.assertFalse((self.runs / "comparisons").exists())

        self.make_experiment_result(baseline_id, [duplicate])
        with self.assertRaisesRegex(ValueError, "not completed"):
            mcp_server.compare_experiments(baseline_id, candidate_id)
        self.assertFalse((self.runs / "comparisons").exists())

        duplicated_checks = self.experiment_point(
            0,
            {"R": "1k"},
            {},
            [self.experiment_check(1200.0, True), self.experiment_check(900.0, False)],
        )
        self.make_experiment_result(baseline_id, [duplicated_checks])
        self.make_experiment_result(candidate_id, [])
        with self.assertRaisesRegex(ValueError, "duplicate requirement identity"):
            mcp_server.compare_experiments(baseline_id, candidate_id)
        self.assertFalse((self.runs / "comparisons").exists())

    def test_compare_experiments_rejects_nonfinite_deltas_before_output(self) -> None:
        baseline_id = "mcp-experiment-20260824-180000-000000-a1b2c3d4"
        candidate_id = "mcp-experiment-20260824-180100-000000-b1c2d3e4"
        self.make_experiment_result(
            baseline_id,
            [self.experiment_point(0, {"R": "1k"}, {"extreme": -1e308}, [])],
        )
        self.make_experiment_result(
            candidate_id,
            [self.experiment_point(0, {"R": "1k"}, {"extreme": 1e308}, [])],
        )
        with self.assertRaisesRegex(ValueError, "measurement extreme delta"):
            mcp_server.compare_experiments(baseline_id, candidate_id)
        self.assertFalse((self.runs / "comparisons").exists())

        self.make_experiment_result(
            baseline_id,
            [self.experiment_point(0, {"R": "1k"}, {}, [
                self.experiment_check(-1e308, False)
            ])],
        )
        self.make_experiment_result(
            candidate_id,
            [self.experiment_point(0, {"R": "1k"}, {}, [
                self.experiment_check(1e308, True)
            ])],
        )
        with self.assertRaisesRegex(ValueError, "requirement .* delta"):
            mcp_server.compare_experiments(baseline_id, candidate_id)
        self.assertFalse((self.runs / "comparisons").exists())

    def test_compare_experiments_confines_comparison_output(self) -> None:
        baseline_id = "mcp-experiment-20260824-180000-000000-a1b2c3d4"
        candidate_id = "mcp-experiment-20260824-180100-000000-b1c2d3e4"
        point = self.experiment_point(0, {"R": "1k"}, {}, [])
        self.make_experiment_result(baseline_id, [point])
        self.make_experiment_result(candidate_id, [point])
        outside = self.root / "outside"
        outside.mkdir()
        try:
            os.symlink(outside, self.runs / "comparisons", target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")

        with self.assertRaisesRegex(ValueError, "inside the runs directory"):
            mcp_server.compare_experiments(baseline_id, candidate_id)
        self.assertEqual(list(outside.iterdir()), [])

    def test_compare_experiments_works_through_mcp_protocol(self) -> None:
        baseline_id = "mcp-experiment-20260824-180000-000000-a1b2c3d4"
        candidate_id = "mcp-experiment-20260824-180100-000000-b1c2d3e4"
        point = self.experiment_point(
            0, {"R": "1k"}, {"gain": 2.0}, [self.experiment_check(1200.0, True)]
        )
        self.make_experiment_result(baseline_id, [point])
        self.make_experiment_result(candidate_id, [point])

        result = asyncio.run(
            mcp_server.mcp.call_tool(
                "compare_experiments",
                {
                    "baseline_experiment_id": baseline_id,
                    "candidate_experiment_id": candidate_id,
                },
            )
        )

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["matched_points"], 1)
        self.assertEqual(result.structured_content["unchanged_requirements"], 1)
        tools = asyncio.run(mcp_server.mcp.list_tools())
        tool = next(tool for tool in tools if tool.name == "compare_experiments")
        self.assertIn("requirement_regressions", tool.output_schema["properties"])

    def test_experiment_index_tools_work_through_mcp_protocol(self) -> None:
        manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
        try:
            defined = manager.define(
                "R1 in out {R}\n.end\n",
                [{"name": "R", "values": ["1k"]}],
                max_concurrency=1,
            )
        finally:
            manager.shutdown()

        built = asyncio.run(
            mcp_server.mcp.call_tool("build_experiment_index", {})
        )
        queried = asyncio.run(
            mcp_server.mcp.call_tool(
                "query_experiments",
                {"status": "defined", "parameters": {"R": "1k"}},
            )
        )

        self.assertFalse(built.is_error)
        self.assertEqual(built.structured_content["indexed_experiments"], 1)
        self.assertFalse(queried.is_error)
        self.assertEqual(queried.structured_content["total"], 0)
        unfiltered = mcp_server.query_experiments(status="defined")
        self.assertEqual(unfiltered["total"], 1)
        self.assertEqual(
            unfiltered["experiments"][0]["experiment_id"],
            defined["experiment_id"],
        )

        tools = asyncio.run(mcp_server.mcp.list_tools())
        by_name = {tool.name: tool for tool in tools}
        self.assertIn("build_experiment_index", by_name)
        query_schema = by_name["query_experiments"].input_schema["properties"]
        self.assertEqual(query_schema["limit"]["default"], 50)
        self.assertEqual(query_schema["offset"]["default"], 0)
        self.assertIn("parameters", query_schema)
        self.assertEqual(
            query_schema["execution_mode"]["anyOf"][0]["enum"],
            ["independent", "native"],
        )
        self.assertIn("completed", query_schema["status"]["anyOf"][0]["enum"])

    def test_only_one_durable_manager_can_own_a_runs_directory(self) -> None:
        first = mcp_server.ExperimentJobManager(self.runs, workers=1)
        try:
            with self.assertRaisesRegex(RuntimeError, "already owns"):
                mcp_server.ExperimentJobManager(self.runs, workers=1)
        finally:
            first.shutdown()

        replacement = mcp_server.ExperimentJobManager(self.runs, workers=1)
        replacement.shutdown()

    def test_experiment_report_works_through_mcp_protocol(self) -> None:
        expected = {
            "experiment_id": "mcp-experiment-20260824-180000-000000-a1b2c3d4",
            "report_html": str(self.runs / "report.html"),
            "plot_count": 1,
            "trace_count": 2,
            "source_points": 1202,
            "displayed_points": 800,
        }
        with patch.object(
            mcp_server.experiment_report,
            "build_experiment_report",
            return_value=expected,
        ) as build:
            result = asyncio.run(
                mcp_server.mcp.call_tool(
                    "build_experiment_report",
                    {"experiment_id": expected["experiment_id"]},
                )
            )

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content, expected)
        build.assert_called_once_with(self.runs, expected["experiment_id"])
        tools = asyncio.run(mcp_server.mcp.list_tools())
        tool = next(tool for tool in tools if tool.name == "build_experiment_report")
        self.assertEqual(
            tool.input_schema["required"],
            ["experiment_id"],
        )
        self.assertIn("report_html", tool.output_schema["properties"])

    def test_c3_visualization_tools_work_through_mcp_protocol(self) -> None:
        comparison = {
            "comparison_id": "0123456789abcdef",
            "comparison_html": str(self.runs / "comparisons" / "comparison.html"),
            "plot_count": 1,
            "trace_count": 2,
            "requirement_regressions": 1,
            "requirement_improvements": 0,
        }
        dashboard = {
            "dashboard_html": str(self.runs / "dashboard.html"),
            "experiment_count": 2,
            "comparison_count": 1,
            "issue_count": 0,
        }
        with (
            patch.object(
                mcp_server.experiment_visualization,
                "build_comparison_report",
                return_value=comparison,
            ) as build_comparison,
            patch.object(
                mcp_server.experiment_visualization,
                "build_experiment_dashboard",
                return_value=dashboard,
            ) as build_dashboard,
        ):
            comparison_result = asyncio.run(
                mcp_server.mcp.call_tool(
                    "build_comparison_report",
                    {
                        "baseline_experiment_id": "mcp-experiment-baseline",
                        "candidate_experiment_id": "mcp-experiment-candidate",
                    },
                )
            )
            dashboard_result = asyncio.run(
                mcp_server.mcp.call_tool("build_experiment_dashboard", {})
            )

        self.assertFalse(comparison_result.is_error)
        self.assertEqual(comparison_result.structured_content, comparison)
        self.assertFalse(dashboard_result.is_error)
        self.assertEqual(dashboard_result.structured_content, dashboard)
        build_comparison.assert_called_once_with(
            self.runs, "mcp-experiment-baseline", "mcp-experiment-candidate"
        )
        build_dashboard.assert_called_once_with(self.runs)
        tools = {tool.name: tool for tool in asyncio.run(mcp_server.mcp.list_tools())}
        self.assertIn("build_comparison_report", tools)
        self.assertIn("build_experiment_dashboard", tools)
        self.assertIn(
            "comparison_html",
            tools["build_comparison_report"].output_schema["properties"],
        )
        self.assertIn(
            "dashboard_html",
            tools["build_experiment_dashboard"].output_schema["properties"],
        )

    def test_experiment_job_bounds_concurrency_and_sorts_results(self) -> None:
        manager = mcp_server.ExperimentJobManager(self.runs, workers=3)
        lock = threading.Lock()
        active = 0
        maximum_active = 0
        completion_order: list[int] = []
        first_pair = threading.Barrier(2)
        point_one_finished = threading.Event()

        def execute_point(
            index: int,
            combination: dict[str, str],
            point_dir: Path,
            *args: object,
        ) -> dict[str, object]:
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                if index < 2:
                    first_pair.wait()
                if index == 0 and not point_one_finished.wait(2):
                    raise RuntimeError("point 1 did not finish before point 0")
                time.sleep(0.01 * (4 - index))
                point_dir.mkdir(parents=True)
                completion_order.append(index)
                if index == 1:
                    point_one_finished.set()
                return {
                    "index": index,
                    "parameters": combination,
                    "run_dir": str(point_dir),
                    "simulation_status": "completed",
                    "duration_seconds": 0.01,
                    "measurements": {"index": float(index)},
                    "analyses": [],
                    "all_passed": True,
                    "error": None,
                }
            finally:
                with lock:
                    active -= 1

        try:
            with patch.object(mcp_server, "_execute_experiment_point", side_effect=execute_point):
                defined = manager.define(
                    "R1 in out {R}\n.end\n",
                    [{"name": "R", "values": ["1k", "2k", "3k", "4k"]}],
                    max_concurrency=2,
                )
                self.assertEqual(defined["status"], "defined")
                manager.start(defined["experiment_id"])
                manager.start(defined["experiment_id"])
                finished = manager.wait(defined["experiment_id"])
        finally:
            manager.shutdown()

        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["finished_points"], 4)
        self.assertEqual(maximum_active, 2)
        self.assertNotEqual(completion_order, [0, 1, 2, 3])
        results = json.loads(Path(finished["results_json"]).read_text(encoding="utf-8"))
        self.assertEqual([point["index"] for point in results["points"]], [0, 1, 2, 3])
        self.assertEqual(
            len(list(Path(finished["experiment_dir"]).glob("point-*/point_result.json"))),
            4,
        )

    def test_durable_native_experiment_materializes_one_batch(self) -> None:
        manager = mcp_server.ExperimentJobManager(self.runs, workers=2)
        calls: list[Path] = []

        def execute_native(
            combinations: list[dict[str, str]],
            batch_dir: Path,
            *args: object,
        ) -> tuple[list[dict[str, object]], dict[str, object]]:
            calls.append(batch_dir)
            return self.native_batch_result(combinations, batch_dir)

        try:
            with patch.object(
                mcp_server, "_execute_native_experiment", side_effect=execute_native
            ):
                defined = manager.define(
                    "R1 in out {R}\n.end\n",
                    [{"name": "R", "values": ["1k", "2k", "3k"]}],
                    execution_mode="native",
                )
                self.assertEqual(defined["execution_mode"], "native")
                manager.start(defined["experiment_id"])
                finished = manager.wait(defined["experiment_id"])
        finally:
            manager.shutdown()

        self.assertEqual(finished["status"], "completed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "attempt-0000")
        results = json.loads(Path(finished["results_json"]).read_text(encoding="utf-8"))
        self.assertEqual(results["execution_mode"], "native")
        self.assertEqual(results["native_batch"]["validated_step_order"], [0, 1, 2])
        self.assertEqual([point["index"] for point in results["points"]], [0, 1, 2])
        self.assertEqual(
            len(list(Path(finished["experiment_dir"]).glob("point-*/point_result.json"))),
            3,
        )
        self.assertTrue(
            (Path(finished["experiment_dir"]) / "native-batch" / "batch_result.json").is_file()
        )
        comparison = mcp_server.compare_experiments(
            finished["experiment_id"], finished["experiment_id"]
        )
        self.assertEqual(comparison["matched_points"], 3)

    def test_durable_native_recovery_repairs_point_materialization_without_rerun(
        self,
    ) -> None:
        first_manager = mcp_server.ExperimentJobManager(self.runs, workers=1)

        def execute_native(
            combinations: list[dict[str, str]],
            batch_dir: Path,
            *args: object,
        ) -> tuple[list[dict[str, object]], dict[str, object]]:
            self.assertTrue(args[-2])
            self.assertIsInstance(args[-1], threading.Event)
            return self.native_batch_result(
                combinations,
                batch_dir,
                execution_source="cache",
                cache_key="native-key",
            )

        with patch.object(
            mcp_server, "_execute_native_experiment", side_effect=execute_native
        ):
            defined = first_manager.define(
                "R1 in out {R}\n.end\n",
                [{"name": "R", "values": ["1k", "2k"]}],
                reuse_cache=True,
                execution_mode="native",
            )
            first_manager.start(defined["experiment_id"])
            first_manager.wait(defined["experiment_id"])
        first_manager.shutdown()

        experiment_dir = Path(defined["experiment_dir"])
        missing_checkpoint = experiment_dir / "point-0001" / "point_result.json"
        missing_checkpoint.unlink()
        (experiment_dir / "results.json").unlink()
        (experiment_dir / "results.csv").unlink()
        manifest = json.loads(Path(defined["manifest"]).read_text(encoding="utf-8"))
        manifest["status"] = "running"
        mcp_server._write_json(Path(defined["manifest"]), manifest)

        with patch.object(mcp_server, "_execute_native_experiment") as execution:
            resumed_manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
            try:
                finished = resumed_manager.wait(defined["experiment_id"])
            finally:
                resumed_manager.shutdown()

        execution.assert_not_called()
        self.assertEqual(finished["status"], "completed")
        self.assertTrue(missing_checkpoint.is_file())
        results = json.loads(Path(finished["results_json"]).read_text(encoding="utf-8"))
        self.assertEqual(results["native_batch"]["execution_source"], "cache")
        self.assertEqual(results["native_batch"]["cache_key"], "native-key")

    def test_durable_native_recovers_cancelling_batch_without_rerun(self) -> None:
        first_manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
        with patch.object(
            mcp_server,
            "_execute_native_experiment",
            side_effect=lambda combinations, batch_dir, *args: self.native_batch_result(
                combinations, batch_dir
            ),
        ):
            defined = first_manager.define(
                "R1 in out {R}\n.end\n",
                [{"name": "R", "values": ["1k", "2k"]}],
                execution_mode="native",
            )
            first_manager.start(defined["experiment_id"])
            first_manager.wait(defined["experiment_id"])
        first_manager.shutdown()

        experiment_dir = Path(defined["experiment_dir"])
        missing_checkpoint = experiment_dir / "point-0001" / "point_result.json"
        missing_checkpoint.unlink()
        (experiment_dir / "results.json").unlink()
        (experiment_dir / "results.csv").unlink()
        manifest = json.loads(Path(defined["manifest"]).read_text(encoding="utf-8"))
        manifest["status"] = "cancelling"
        mcp_server._write_json(Path(defined["manifest"]), manifest)

        with patch.object(mcp_server, "_execute_native_experiment") as execution:
            resumed_manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
            try:
                finished = resumed_manager.wait(defined["experiment_id"])
            finally:
                resumed_manager.shutdown()

        execution.assert_not_called()
        self.assertEqual(finished["status"], "cancelled")
        self.assertTrue(missing_checkpoint.is_file())
        results = json.loads(Path(finished["results_json"]).read_text(encoding="utf-8"))
        self.assertEqual(results["status"], "cancelled")

    def test_durable_native_queued_recovery_preserves_cancel_intent(self) -> None:
        first_manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
        with patch.object(
            mcp_server,
            "_execute_native_experiment",
            side_effect=lambda combinations, batch_dir, *args: self.native_batch_result(
                combinations, batch_dir
            ),
        ):
            defined = first_manager.define(
                "R1 in out {R}\n.end\n",
                [{"name": "R", "values": ["1k"]}],
                execution_mode="native",
            )
            first_manager.start(defined["experiment_id"])
            first_manager.wait(defined["experiment_id"])
        first_manager.shutdown()

        experiment_dir = Path(defined["experiment_dir"])
        (experiment_dir / "results.json").unlink()
        (experiment_dir / "results.csv").unlink()
        manifest = json.loads(Path(defined["manifest"]).read_text(encoding="utf-8"))
        manifest.update(status="queued", cancel_requested=True)
        mcp_server._write_json(Path(defined["manifest"]), manifest)

        with patch.object(mcp_server, "_execute_native_experiment") as execution:
            resumed_manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
            try:
                finished = resumed_manager.wait(defined["experiment_id"])
            finally:
                resumed_manager.shutdown()

        execution.assert_not_called()
        self.assertEqual(finished["status"], "cancelled")
        results = json.loads(Path(finished["results_json"]).read_text(encoding="utf-8"))
        self.assertEqual(results["status"], "cancelled")

    def test_durable_native_rechecks_cancellation_before_launch(self) -> None:
        manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
        original_persist = manager._persist_progress

        def cancel_after_progress(*args: object, **kwargs: object) -> None:
            original_persist(*args, **kwargs)
            manager._events[str(args[0])].set()

        try:
            with (
                patch.object(
                    manager,
                    "_persist_progress",
                    side_effect=cancel_after_progress,
                ),
                patch.object(mcp_server, "_execute_native_experiment") as execution,
            ):
                defined = manager.define(
                    "R1 in out {R}\n.end\n",
                    [{"name": "R", "values": ["1k"]}],
                    execution_mode="native",
                )
                manager.start(defined["experiment_id"])
                finished = manager.wait(defined["experiment_id"])
        finally:
            manager.shutdown()

        execution.assert_not_called()
        self.assertEqual(finished["status"], "cancelled")
        manifest = json.loads(Path(finished["manifest"]).read_text(encoding="utf-8"))
        self.assertNotIn("native-batch/batch_result.json", manifest["artifacts"])

    def test_durable_native_rejects_corrupt_batch_checkpoint(self) -> None:
        manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
        defined = manager.define(
            "R1 in out {R}\n.end\n",
            [{"name": "R", "values": ["1k"]}],
            execution_mode="native",
        )
        manager.shutdown()
        experiment_dir = Path(defined["experiment_dir"])
        native_root = experiment_dir / "native-batch"
        native_root.mkdir()
        (native_root / "batch_result.json").write_text("not json", encoding="utf-8")
        manifest = json.loads(Path(defined["manifest"]).read_text(encoding="utf-8"))
        manifest["status"] = "running"
        mcp_server._write_json(Path(defined["manifest"]), manifest)

        with patch.object(mcp_server, "_execute_native_experiment") as execution:
            resumed_manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
            try:
                finished = resumed_manager.wait(defined["experiment_id"])
            finally:
                resumed_manager.shutdown()

        execution.assert_not_called()
        self.assertEqual(finished["status"], "failed")
        self.assertIn("invalid native batch checkpoint", finished["error"])

    def test_durable_native_cancellation_preserves_validated_batch(self) -> None:
        manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
        started = threading.Event()
        release = threading.Event()

        def execute_native(
            combinations: list[dict[str, str]],
            batch_dir: Path,
            *args: object,
        ) -> tuple[list[dict[str, object]], dict[str, object]]:
            started.set()
            release.wait(2)
            return self.native_batch_result(
                combinations,
                batch_dir,
                all_passed=False,
                error="experiment cancelled before waveform analysis",
            )

        try:
            with patch.object(
                mcp_server, "_execute_native_experiment", side_effect=execute_native
            ):
                defined = manager.define(
                    "R1 in out {R}\n.end\n",
                    [{"name": "R", "values": ["1k", "2k"]}],
                    execution_mode="native",
                )
                manager.start(defined["experiment_id"])
                self.assertTrue(started.wait(2))
                cancelling = manager.cancel(defined["experiment_id"])
                self.assertEqual(cancelling["status"], "cancelling")
                release.set()
                finished = manager.wait(defined["experiment_id"])
        finally:
            release.set()
            manager.shutdown()

        self.assertEqual(finished["status"], "cancelled")
        self.assertEqual(finished["finished_points"], 2)
        self.assertTrue(
            (Path(finished["experiment_dir"]) / "native-batch" / "batch_result.json").is_file()
        )

    def test_durable_native_resume_starts_a_fresh_attempt_without_checkpoint(
        self,
    ) -> None:
        first_manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
        defined = first_manager.define(
            "R1 in out {R}\n.end\n",
            [{"name": "R", "values": ["1k"]}],
            execution_mode="native",
        )
        first_manager.shutdown()
        experiment_dir = Path(defined["experiment_dir"])
        interrupted_attempt = experiment_dir / "native-batch" / "attempt-0000"
        interrupted_attempt.mkdir(parents=True)
        manifest = json.loads(Path(defined["manifest"]).read_text(encoding="utf-8"))
        manifest["status"] = "running"
        mcp_server._write_json(Path(defined["manifest"]), manifest)
        calls: list[Path] = []

        def execute_native(
            combinations: list[dict[str, str]],
            batch_dir: Path,
            *args: object,
        ) -> tuple[list[dict[str, object]], dict[str, object]]:
            calls.append(batch_dir)
            return self.native_batch_result(combinations, batch_dir)

        with patch.object(
            mcp_server, "_execute_native_experiment", side_effect=execute_native
        ):
            resumed_manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
            try:
                finished = resumed_manager.wait(defined["experiment_id"])
            finally:
                resumed_manager.shutdown()

        self.assertEqual(finished["status"], "completed")
        self.assertEqual([path.name for path in calls], ["attempt-0001"])

    def test_active_native_shutdown_recovers_completed_batch_without_rerun(
        self,
    ) -> None:
        manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
        started = threading.Event()
        release = threading.Event()

        def execute_native(
            combinations: list[dict[str, str]],
            batch_dir: Path,
            *args: object,
        ) -> tuple[list[dict[str, object]], dict[str, object]]:
            started.set()
            release.wait(2)
            return self.native_batch_result(combinations, batch_dir)

        with patch.object(
            mcp_server, "_execute_native_experiment", side_effect=execute_native
        ):
            defined = manager.define(
                "R1 in out {R}\n.end\n",
                [{"name": "R", "values": ["1k", "2k"]}],
                execution_mode="native",
            )
            manager.start(defined["experiment_id"])
            self.assertTrue(started.wait(2))
            shutdown_thread = threading.Thread(target=manager.shutdown)
            shutdown_thread.start()
            release.set()
            shutdown_thread.join(2)

        self.assertFalse(shutdown_thread.is_alive())
        paused = manager.snapshot(defined["experiment_id"])
        self.assertEqual(paused["status"], "queued")
        self.assertEqual(paused["finished_points"], 2)

        with patch.object(mcp_server, "_execute_native_experiment") as execution:
            resumed_manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
            try:
                finished = resumed_manager.wait(defined["experiment_id"])
            finally:
                resumed_manager.shutdown()

        execution.assert_not_called()
        self.assertEqual(finished["status"], "completed")

    def test_durable_native_rejects_invalid_callback_payload_before_checkpoint(
        self,
    ) -> None:
        manager = mcp_server.ExperimentJobManager(self.runs, workers=1)

        def execute_native(
            combinations: list[dict[str, str]],
            batch_dir: Path,
            *args: object,
        ) -> tuple[list[dict[str, object]], dict[str, object]]:
            batch_dir.mkdir(parents=True)
            return [], {
                "run_dir": str(batch_dir),
                "status": "completed",
                "step_count": 1,
                "validated_step_order": [0],
                "execution_source": "simulator",
            }

        try:
            with patch.object(
                mcp_server, "_execute_native_experiment", side_effect=execute_native
            ):
                defined = manager.define(
                    "R1 in out {R}\n.end\n",
                    [{"name": "R", "values": ["1k"]}],
                    execution_mode="native",
                )
                manager.start(defined["experiment_id"])
                finished = manager.wait(defined["experiment_id"])
        finally:
            manager.shutdown()

        self.assertEqual(finished["status"], "failed")
        self.assertIn("checkpoint point count", finished["error"])
        self.assertFalse(
            (Path(finished["experiment_dir"]) / "native-batch" / "batch_result.json").exists()
        )

    def test_define_durable_native_rejects_unsafe_deck_before_output(self) -> None:
        manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
        try:
            before = set(self.runs.iterdir())
            with self.assertRaisesRegex(ValueError, "existing \\.step"):
                manager.define(
                    ".step param existing 1 2 1\nR1 in out {R}\n.end\n",
                    [{"name": "R", "values": ["1k"]}],
                    execution_mode="native",
                )
            after = set(self.runs.iterdir())
        finally:
            manager.shutdown()

        self.assertEqual(after, before)

    def test_define_experiment_schema_advertises_durable_native_mode(self) -> None:
        tools = asyncio.run(mcp_server.mcp.list_tools())
        tool = next(tool for tool in tools if tool.name == "define_experiment")

        self.assertEqual(
            tool.input_schema["properties"]["execution_mode"]["default"],
            "independent",
        )
        self.assertEqual(
            tool.input_schema["properties"]["execution_mode"]["enum"],
            ["independent", "native"],
        )
        self.assertIn("execution_mode", tool.output_schema["properties"])

    def test_experiment_job_cancellation_stops_new_points(self) -> None:
        manager = mcp_server.ExperimentJobManager(self.runs, workers=2)
        started = threading.Event()
        release = threading.Event()
        calls: list[int] = []
        lock = threading.Lock()

        def execute_point(
            index: int,
            combination: dict[str, str],
            point_dir: Path,
            *args: object,
        ) -> dict[str, object]:
            with lock:
                calls.append(index)
                if len(calls) == 2:
                    started.set()
            release.wait(2)
            return {
                "index": index,
                "parameters": combination,
                "run_dir": str(point_dir),
                "simulation_status": "completed",
                "duration_seconds": 0.01,
                "measurements": {},
                "analyses": [],
                "all_passed": True,
                "error": None,
            }

        try:
            with patch.object(mcp_server, "_execute_experiment_point", side_effect=execute_point):
                defined = manager.define(
                    "R1 in out {R}\n.end\n",
                    [{"name": "R", "values": [str(index) for index in range(6)]}],
                    max_concurrency=2,
                )
                manager.start(defined["experiment_id"])
                self.assertTrue(started.wait(2))
                for _ in range(100):
                    if manager.snapshot(defined["experiment_id"])["running_points"] == 2:
                        break
                    time.sleep(0.005)
                cancelling = manager.cancel(defined["experiment_id"])
                self.assertEqual(cancelling["status"], "cancelling")
                self.assertEqual(cancelling["running_points"], 2)
                self.assertEqual(cancelling["pending_points"], 4)
                release.set()
                finished = manager.wait(defined["experiment_id"])
                manager.cancel(defined["experiment_id"])
        finally:
            release.set()
            manager.shutdown()

        self.assertEqual(finished["status"], "cancelled")
        self.assertEqual(finished["finished_points"], 2)
        self.assertEqual(finished["pending_points"], 4)
        self.assertEqual(sorted(calls), [0, 1])

    def test_cancel_after_queue_claim_cannot_be_lost(self) -> None:
        manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
        claimed = threading.Event()
        release = threading.Event()
        original_run_job = manager._run_job

        def paused_run_job(experiment_id: str) -> None:
            claimed.set()
            release.wait(2)
            original_run_job(experiment_id)

        try:
            with (
                patch.object(manager, "_run_job", side_effect=paused_run_job),
                patch.object(mcp_server, "_execute_experiment_point") as execution,
            ):
                defined = manager.define(
                    "R1 in out {R}\n.end\n",
                    [{"name": "R", "values": ["1k"]}],
                    max_concurrency=1,
                )
                manager.start(defined["experiment_id"])
                self.assertTrue(claimed.wait(2))
                cancelling = manager.cancel(defined["experiment_id"])
                self.assertEqual(cancelling["status"], "cancelling")
                release.set()
                finished = manager.wait(defined["experiment_id"])
                execution.assert_not_called()
        finally:
            release.set()
            manager.shutdown()

        self.assertEqual(finished["status"], "cancelled")
        self.assertEqual(finished["finished_points"], 0)

    def test_experiment_job_resumes_only_uncheckpointed_points(self) -> None:
        first_manager = mcp_server.ExperimentJobManager(self.runs, workers=2)
        defined = first_manager.define(
            "R1 in out {R}\n.end\n",
            [{"name": "R", "values": ["1k", "2k", "3k"]}],
            max_concurrency=2,
        )
        first_manager.shutdown()
        experiment_dir = Path(defined["experiment_dir"])
        completed_point = {
            "index": 0,
            "parameters": {"R": "1k"},
            "run_dir": str(experiment_dir / "point-0000" / "attempt-0000"),
            "simulation_status": "completed",
            "duration_seconds": 0.01,
            "measurements": {},
            "analyses": [],
            "all_passed": True,
            "error": None,
        }
        point_zero = experiment_dir / "point-0000"
        point_zero.mkdir()
        mcp_server._write_json(point_zero / "point_result.json", completed_point)
        interrupted_attempt = experiment_dir / "point-0001" / "attempt-0000"
        interrupted_attempt.mkdir(parents=True)
        manifest_path = experiment_dir / "experiment_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = "running"
        mcp_server._write_json(manifest_path, manifest)
        calls: list[tuple[int, Path]] = []

        def execute_point(
            index: int,
            combination: dict[str, str],
            point_dir: Path,
            *args: object,
        ) -> dict[str, object]:
            calls.append((index, point_dir))
            return {
                "index": index,
                "parameters": combination,
                "run_dir": str(point_dir),
                "simulation_status": "completed",
                "duration_seconds": 0.01,
                "measurements": {},
                "analyses": [],
                "all_passed": True,
                "error": None,
            }

        with patch.object(mcp_server, "_execute_experiment_point", side_effect=execute_point):
            resumed_manager = mcp_server.ExperimentJobManager(self.runs, workers=2)
            try:
                finished = resumed_manager.wait(defined["experiment_id"])
            finally:
                resumed_manager.shutdown()

        self.assertEqual(finished["status"], "completed")
        self.assertEqual([index for index, _ in calls], [1, 2])
        self.assertEqual(calls[0][1].name, "attempt-0001")

    def test_active_job_shutdown_leaves_unfinished_work_resumable(self) -> None:
        first_manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
        started = threading.Event()
        release = threading.Event()

        def slow_point(
            index: int,
            combination: dict[str, str],
            point_dir: Path,
            *args: object,
        ) -> dict[str, object]:
            started.set()
            release.wait(2)
            return {
                "index": index,
                "parameters": combination,
                "run_dir": str(point_dir),
                "simulation_status": "completed",
                "duration_seconds": 0.01,
                "measurements": {},
                "analyses": [],
                "all_passed": True,
                "error": None,
            }

        with patch.object(mcp_server, "_execute_experiment_point", side_effect=slow_point):
            defined = first_manager.define(
                "R1 in out {R}\n.end\n",
                [{"name": "R", "values": ["1k", "2k", "3k"]}],
                max_concurrency=1,
            )
            first_manager.start(defined["experiment_id"])
            self.assertTrue(started.wait(2))
            shutdown_thread = threading.Thread(target=first_manager.shutdown)
            shutdown_thread.start()
            release.set()
            shutdown_thread.join(2)
            self.assertFalse(shutdown_thread.is_alive())
            self.assertFalse(first_manager._coordinator.is_alive())

        paused = first_manager.snapshot(defined["experiment_id"])
        self.assertEqual(paused["status"], "queued")
        self.assertEqual(paused["finished_points"], 1)
        resumed_calls: list[int] = []

        def resumed_point(
            index: int,
            combination: dict[str, str],
            point_dir: Path,
            *args: object,
        ) -> dict[str, object]:
            resumed_calls.append(index)
            return {
                "index": index,
                "parameters": combination,
                "run_dir": str(point_dir),
                "simulation_status": "completed",
                "duration_seconds": 0.01,
                "measurements": {},
                "analyses": [],
                "all_passed": True,
                "error": None,
            }

        with patch.object(mcp_server, "_execute_experiment_point", side_effect=resumed_point):
            resumed_manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
            try:
                finished = resumed_manager.wait(defined["experiment_id"])
            finally:
                resumed_manager.shutdown()

        self.assertEqual(finished["status"], "completed")
        self.assertEqual(resumed_calls, [1, 2])

    def test_shutdown_cannot_race_with_point_submission(self) -> None:
        manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
        submitting = threading.Event()
        release = threading.Event()
        original_submit = manager._executor.submit

        def blocked_submit(*args: object, **kwargs: object):
            submitting.set()
            release.wait(2)
            return original_submit(*args, **kwargs)

        def execute_point(
            index: int,
            combination: dict[str, str],
            point_dir: Path,
            *args: object,
        ) -> dict[str, object]:
            return {
                "index": index,
                "parameters": combination,
                "run_dir": str(point_dir),
                "simulation_status": "completed",
                "duration_seconds": 0.01,
                "measurements": {},
                "analyses": [],
                "all_passed": True,
                "error": None,
            }

        with (
            patch.object(manager._executor, "submit", side_effect=blocked_submit),
            patch.object(mcp_server, "_execute_experiment_point", side_effect=execute_point),
        ):
            defined = manager.define(
                "R1 in out {R}\n.end\n",
                [{"name": "R", "values": ["1k", "2k"]}],
                max_concurrency=1,
            )
            manager.start(defined["experiment_id"])
            self.assertTrue(submitting.wait(2))
            shutdown_thread = threading.Thread(target=manager.shutdown)
            shutdown_thread.start()
            release.set()
            shutdown_thread.join(2)

        self.assertFalse(shutdown_thread.is_alive())
        paused = manager.snapshot(defined["experiment_id"])
        self.assertEqual(paused["status"], "queued")
        self.assertEqual(paused["finished_points"], 1)
        self.assertIsNone(paused["error"])

    def test_experiment_job_rejects_a_corrupt_checkpoint(self) -> None:
        first_manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
        defined = first_manager.define(
            "R1 in out {R}\n.end\n",
            [{"name": "R", "values": ["1k"]}],
            max_concurrency=1,
        )
        first_manager.shutdown()
        experiment_dir = Path(defined["experiment_dir"])
        point_dir = experiment_dir / "point-0000"
        point_dir.mkdir()
        (point_dir / "point_result.json").write_text("not json", encoding="utf-8")
        manifest_path = experiment_dir / "experiment_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = "running"
        mcp_server._write_json(manifest_path, manifest)

        resumed_manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
        try:
            finished = resumed_manager.wait(defined["experiment_id"])
        finally:
            resumed_manager.shutdown()

        self.assertEqual(finished["status"], "failed")
        self.assertIn("invalid point checkpoint", finished["error"])

    def test_experiment_job_rejects_a_symlinked_directory_outside_runs(self) -> None:
        experiment_id = "mcp-experiment-20260824-120000-000000-deadbeef"
        outside = self.root / "outside" / experiment_id
        outside.mkdir(parents=True)
        (outside / "experiment_manifest.json").write_text(
            json.dumps({"experiment_id": experiment_id, "status": "defined"}),
            encoding="utf-8",
        )
        try:
            (self.runs / experiment_id).symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")

        manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
        try:
            with self.assertRaisesRegex(ValueError, "inside the runs directory"):
                manager.snapshot(experiment_id)
        finally:
            manager.shutdown()

    def test_experiment_job_rejects_an_unsupported_engine_version(self) -> None:
        first_manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
        defined = first_manager.define(
            "R1 in out {R}\n.end\n",
            [{"name": "R", "values": ["1k"]}],
            max_concurrency=1,
        )
        first_manager.shutdown()
        manifest_path = Path(defined["manifest"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(status="running", engine_version=999)
        mcp_server._write_json(manifest_path, manifest)

        resumed_manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
        try:
            finished = resumed_manager.wait(defined["experiment_id"])
        finally:
            resumed_manager.shutdown()

        self.assertEqual(finished["status"], "failed")
        self.assertIn("engine version", finished["error"])

    def test_coordinator_continues_after_one_job_crashes(self) -> None:
        manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
        first = manager.define(
            "R1 in out {R}\n.end\n",
            [{"name": "R", "values": ["1k"]}],
            max_concurrency=1,
        )
        second = manager.define(
            "R1 in out {R}\n.end\n",
            [{"name": "R", "values": ["2k"]}],
            max_concurrency=1,
        )
        original_run_job = manager._run_job

        def run_job(experiment_id: str) -> None:
            if experiment_id == first["experiment_id"]:
                raise RuntimeError("coordinator fixture failure")
            original_run_job(experiment_id)

        def execute_point(
            index: int,
            combination: dict[str, str],
            point_dir: Path,
            *args: object,
        ) -> dict[str, object]:
            return {
                "index": index,
                "parameters": combination,
                "run_dir": str(point_dir),
                "simulation_status": "completed",
                "duration_seconds": 0.01,
                "measurements": {},
                "analyses": [],
                "all_passed": True,
                "error": None,
            }

        try:
            with (
                patch.object(manager, "_run_job", side_effect=run_job),
                patch.object(mcp_server, "_execute_experiment_point", side_effect=execute_point),
            ):
                manager.start(first["experiment_id"])
                manager.start(second["experiment_id"])
                failed = manager.wait(first["experiment_id"])
                completed = manager.wait(second["experiment_id"])
        finally:
            manager.shutdown()

        self.assertEqual(failed["status"], "failed")
        self.assertIn("fixture failure", failed["error"])
        self.assertEqual(completed["status"], "completed")

    def test_cancel_defined_experiment_is_idempotent(self) -> None:
        manager = mcp_server.ExperimentJobManager(self.runs, workers=1)
        try:
            defined = manager.define(
                "R1 in out {R}\n.end\n",
                [{"name": "R", "values": ["1k"]}],
                max_concurrency=1,
            )
            cancelled = manager.cancel(defined["experiment_id"])
            cancelled_again = manager.cancel(defined["experiment_id"])
        finally:
            manager.shutdown()

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled_again["status"], "cancelled")
        self.assertEqual(cancelled["finished_points"], 0)

    def test_experiment_lifecycle_tools_work_through_mcp_protocol(self) -> None:
        manager = mcp_server.ExperimentJobManager(self.runs, workers=2)

        def execute_point(
            index: int,
            combination: dict[str, str],
            point_dir: Path,
            *args: object,
        ) -> dict[str, object]:
            return {
                "index": index,
                "parameters": combination,
                "run_dir": str(point_dir),
                "simulation_status": "completed",
                "duration_seconds": 0.01,
                "measurements": {},
                "analyses": [],
                "all_passed": True,
                "error": None,
            }

        arguments = {
            "netlist_template": "R1 in out {R}\n.end\n",
            "parameters": [{"name": "R", "values": ["1k"]}],
        }
        try:
            with (
                patch.object(mcp_server, "_get_experiment_manager", return_value=manager),
                patch.object(mcp_server, "_execute_experiment_point", side_effect=execute_point),
            ):
                defined = asyncio.run(
                    mcp_server.mcp.call_tool("define_experiment", arguments)
                )
                experiment_id = defined.structured_content["experiment_id"]
                started = asyncio.run(
                    mcp_server.mcp.call_tool(
                        "start_experiment", {"experiment_id": experiment_id}
                    )
                )
                self.assertFalse(started.is_error)
                manager.wait(experiment_id)
                fetched = asyncio.run(
                    mcp_server.mcp.call_tool(
                        "get_experiment", {"experiment_id": experiment_id}
                    )
                )
        finally:
            manager.shutdown()

        self.assertFalse(defined.is_error)
        self.assertEqual(fetched.structured_content["status"], "completed")
        tools = asyncio.run(mcp_server.mcp.list_tools())
        names = {tool.name for tool in tools}
        self.assertTrue(
            {"define_experiment", "start_experiment", "get_experiment", "cancel_experiment"}
            <= names
        )

    def test_history_dashboard_and_examples(self) -> None:
        (self.examples / "a.cir").write_text("* AC example\n.end\n")
        (self.examples / "b.net").write_text("* Transient example\n.end\n")
        records = [{"run": "new"}, {"run": "old"}]
        with patch.object(mcp_server.report_runs, "collect_records", return_value=records):
            self.assertEqual(mcp_server.list_runs(1), records[:1])

        html_path = self.runs / "index.html"
        json_path = self.runs / "index.json"
        with patch.object(
            mcp_server.report_runs, "write_dashboard", return_value=(html_path, json_path)
        ):
            self.assertEqual(
                mcp_server.build_dashboard(),
                {"html": str(html_path), "json": str(json_path)},
            )

        examples = mcp_server.list_examples()
        self.assertEqual([item["name"] for item in examples], ["a.cir", "b.net"])
        self.assertEqual(examples[0]["description"], "AC example")


if __name__ == "__main__":
    unittest.main()
