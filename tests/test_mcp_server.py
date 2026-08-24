from __future__ import annotations

import asyncio
import cmath
import csv
import json
import math
import tempfile
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
        self.assertEqual(result["data"]["V(out)"], {"real": [1.0, 3.0], "imag": [-1.0, -3.0]})
        with self.assertRaises(ValueError):
            mcp_server.get_waveform(str(run_dir), max_points=0)
        with self.assertRaises(ValueError):
            mcp_server.get_waveform(str(run_dir), raw_filename="../result.raw")

    def test_export_waveform_csv(self) -> None:
        run_dir = self.make_run()
        raw_path = run_dir / "result.raw"
        raw_path.touch()
        data = RawData(flags="real", variables=["time"], values={"time": [0.0, 1.0]})
        with (
            patch.object(mcp_server.raw_parser, "parse_raw", return_value=data),
            patch.object(mcp_server.raw_parser, "export_csv") as export,
        ):
            result = mcp_server.export_waveform_csv(str(run_dir))

        export.assert_called_once_with(data, run_dir.resolve() / "result.csv")
        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["csv_path"], str(run_dir.resolve() / "result.csv"))

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
        analysis = tool.input_schema["$defs"]["ExperimentWaveformAnalysis"]
        requirement = tool.input_schema["$defs"]["WaveformRequirement"]

        self.assertEqual(tool.input_schema["properties"]["parameters"]["type"], "array")
        self.assertEqual(parameter["properties"]["name"]["type"], "string")
        self.assertEqual(parameter["properties"]["values"]["items"]["type"], "string")
        self.assertEqual(parameter["properties"]["unit"]["type"], "string")
        self.assertIn("derived_parameters", tool.input_schema["properties"])
        self.assertIn("requirements", analysis["properties"])
        self.assertEqual(
            requirement["properties"]["maximum_harmonic"]["type"],
            "integer",
        )
        self.assertIn("point_count", tool.output_schema["properties"])
        self.assertIn("derived_parameter_order", tool.output_schema["properties"])

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
