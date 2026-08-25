from __future__ import annotations

import json
import hashlib
import statistics
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import statistical_engine


class StatisticalEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.runs = Path(self.temporary_directory.name) / "runs"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def variables() -> list[statistical_engine.StatisticalVariable]:
        return [
            {
                "name": "R",
                "distribution": "uniform",
                "minimum": 9_000.0,
                "maximum": 11_000.0,
                "nominal": 10_000.0,
                "unit": "ohm",
            },
            {
                "name": "C",
                "distribution": "uniform",
                "minimum": 9e-7,
                "maximum": 1.1e-6,
                "nominal": 1e-6,
                "unit": "F",
            },
        ]

    def test_uniform_plan_matches_golden_values_and_preserves_pairs(self) -> None:
        plan = statistical_engine.build_statistical_plan(
            self.variables(), 3, 20260824
        )

        self.assertEqual(plan["schema_version"], 1)
        self.assertEqual(plan["generator_version"], "sha256-counter-uniform-v1")
        self.assertEqual(
            plan["definition_hash"],
            "6f0be87ecee638e66c8d09cd8e7a0c0e896251752a839c3d8e091f1e123c333f",
        )
        self.assertEqual(plan["parameter_order"], ["R", "C"])
        self.assertEqual(
            [point["parameters"] for point in plan["points"]],
            [
                {"R": "10405.863857967846", "C": "0.00000106897441448035"},
                {"R": "9481.4855010878273", "C": "9.9330653105920143e-7"},
                {"R": "9003.8076717381499", "C": "0.0000010261437035727548"},
            ],
        )
        self.assertEqual(len(plan["points"]), 3)
        self.assertEqual(
            hashlib.sha256(statistical_engine._artifact_bytes(plan)).hexdigest(),
            "d76f4c9d1381ed738526e056144b82ff88c9829a0d20b8057cd24f044231c587",
        )
        self.assertEqual(
            plan,
            statistical_engine.build_statistical_plan(
                self.variables(), 3, 20260824, correlations=[]
            ),
        )
        self.assertEqual(
            plan,
            statistical_engine.build_statistical_plan(
                self.variables(),
                3,
                20260824,
                correlations=[],
                corner_axes=[],
                corner_aggregate=False,
            ),
        )
        self.assertEqual(
            plan,
            statistical_engine.build_statistical_plan(
                self.variables(), 3, 20260824, sampling_method="independent"
            ),
        )

    def test_latin_hypercube_uses_every_stratum_once_and_matches_golden(self) -> None:
        plan = statistical_engine.build_statistical_plan(
            self.variables(),
            8,
            20260824,
            sampling_method="latin_hypercube",
        )

        self.assertEqual(plan["generator_version"], "sha256-stratified-halton-v6")
        self.assertEqual(plan["definition"]["sampling_method"], "latin_hypercube")
        self.assertEqual(
            plan["definition_hash"],
            "badb6215a9fe597854488f74ed2d1c9cd5c3262757e834f9cce3531ad276d902",
        )
        self.assertEqual(
            hashlib.sha256(statistical_engine._artifact_bytes(plan)).hexdigest(),
            "fdfd287c28e439cc20cba55b12c6665653c86f70ed1c16dc5450281cc85ddc5d",
        )
        for name, minimum, maximum in (("R", 9000, 11000), ("C", 9e-7, 1.1e-6)):
            strata = {
                int(
                    (float(point["parameters"][name]) - minimum)
                    / (maximum - minimum)
                    * 8
                )
                for point in plan["points"]
            }
            self.assertEqual(strata, set(range(8)))
        self.assertEqual(
            [point["parameters"] for point in plan["points"][:3]],
            [
                {"R": "10266.003649594882", "C": "9.6614642410280936e-7"},
                {"R": "9449.9216849581659", "C": "0.0000010986707784386026"},
                {"R": "10530.513831815364", "C": "9.2615859546493145e-7"},
            ],
        )
        saved = statistical_engine.save_statistical_plan(self.runs, plan)
        self.assertEqual(saved["sampling_method"], "latin_hypercube")
        self.assertEqual(
            statistical_engine.load_statistical_plan(self.runs, saved["plan_id"]),
            plan,
        )

    def test_halton_is_deterministic_reorder_stable_and_well_spread(self) -> None:
        variables = self.variables()
        plan = statistical_engine.build_statistical_plan(
            variables, 16, 42, sampling_method="halton"
        )
        repeated = statistical_engine.build_statistical_plan(
            variables, 16, 42, sampling_method="halton"
        )
        reordered = statistical_engine.build_statistical_plan(
            list(reversed(variables)), 16, 42, sampling_method="halton"
        )

        self.assertEqual(plan, repeated)
        self.assertEqual(plan["definition"]["sampling_method"], "halton")
        self.assertEqual(
            hashlib.sha256(statistical_engine._artifact_bytes(plan)).hexdigest(),
            "e3367eb82679e9c70a28c4dccfafec4c640448cd3d5a9b9bc29efeedd55811bd",
        )
        self.assertEqual(
            [point["parameters"]["R"] for point in plan["points"]],
            [point["parameters"]["R"] for point in reordered["points"]],
        )
        normalized = sorted(
            (float(point["parameters"]["R"]) - 9000) / 2000
            for point in plan["points"]
        )
        gaps = [
            right - left
            for left, right in zip([0.0, *normalized], [*normalized, 1.0])
        ]
        self.assertLess(max(gaps), 0.13)

    def test_stratified_sampling_maps_discrete_empirical_and_corners(self) -> None:
        plan = statistical_engine.build_statistical_plan(
            [
                {
                    "name": "GRADE",
                    "distribution": "discrete",
                    "values": ["A", "B"],
                    "weights": [1, 3],
                    "nominal": "B",
                },
                {
                    "name": "LOT",
                    "distribution": "empirical",
                    "values": [10, 20, 30, 40],
                },
            ],
            8,
            7,
            corner_axes=[
                {
                    "name": "load",
                    "parameter": "RLOAD",
                    "values": [
                        {"name": "light", "value": "100k"},
                        {"name": "heavy", "value": "1k"},
                    ],
                }
            ],
            sampling_method="latin_hypercube",
        )

        base_points = plan["points"][::2]
        self.assertEqual(
            sorted(point["parameters"]["GRADE"] for point in base_points),
            ["A", "A", "B", "B", "B", "B", "B", "B"],
        )
        self.assertEqual(
            sorted(point["parameters"]["LOT"] for point in base_points),
            [
                "1e+1",
                "1e+1",
                "2e+1",
                "2e+1",
                "3e+1",
                "3e+1",
                "4e+1",
                "4e+1",
            ],
        )
        self.assertEqual(len(plan["points"]), 16)
        self.assertEqual(plan["points"][0]["corners"], {"load": "light"})
        self.assertEqual(plan["points"][1]["corners"], {"load": "heavy"})

    def test_stratified_sampling_rejects_invalid_methods_and_gaussians(self) -> None:
        with self.assertRaisesRegex(ValueError, "sampling_method must be"):
            statistical_engine.build_statistical_plan(
                self.variables(), 4, 1, sampling_method="sobol"
            )
        with self.assertRaisesRegex(ValueError, "does not support gaussian"):
            statistical_engine.build_statistical_plan(
                [
                    {
                        "name": "R",
                        "distribution": "gaussian",
                        "minimum": 9000,
                        "maximum": 11000,
                        "nominal": 10000,
                        "sigma": 100,
                    }
                ],
                4,
                1,
                sampling_method="halton",
            )

    def test_mixed_plan_matches_gaussian_and_discrete_golden_values(self) -> None:
        plan = statistical_engine.build_statistical_plan(
            [
                {
                    "name": "R",
                    "distribution": "uniform",
                    "minimum": 9_000,
                    "maximum": 11_000,
                    "nominal": 10_000,
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
                    "name": "GRADE",
                    "distribution": "discrete",
                    "values": ["A", "B", "C"],
                    "weights": [1, 3, 1],
                    "nominal": "B",
                },
            ],
            5,
            20260824,
        )

        self.assertEqual(
            plan["generator_version"], "sha256-counter-distributions-v2"
        )
        self.assertEqual(
            plan["definition_hash"],
            "cb648d9877d1ceb96531336a6222a4f849dd297c71012cf76a20b0d615639b28",
        )
        self.assertEqual(
            plan["definition"]["variables"][2]["weights"],
            ["0.2", "0.6", "0.2"],
        )
        self.assertEqual(
            [point["parameters"] for point in plan["points"]],
            [
                {"R": "10405.863857967846", "C": "0.0000011081569630639257", "GRADE": "B"},
                {"R": "9481.4855010878273", "C": "9.6467201742930863e-7", "GRADE": "A"},
                {"R": "9003.8076717381499", "C": "9.7659836912315381e-7", "GRADE": "B"},
                {"R": "9371.7335773366397", "C": "9.0301480365263195e-7", "GRADE": "A"},
                {"R": "10201.302697503848", "C": "0.0000010441196255102357", "GRADE": "C"},
            ],
        )

    def test_empirical_inline_plan_is_deterministic_and_self_contained(self) -> None:
        plan = statistical_engine.build_statistical_plan(
            [
                {
                    "name": "R",
                    "distribution": "empirical",
                    "values": [9980, "10020", 10075.0, 9950],
                    "unit": "ohm",
                }
            ],
            6,
            20260824,
        )

        self.assertEqual(plan["generator_version"], "sha256-counter-empirical-v4")
        variable = plan["definition"]["variables"][0]
        self.assertEqual(
            variable["values"], ["9.98e+3", "1.002e+4", "10075", "9.95e+3"]
        )
        self.assertEqual(
            variable["source"],
            {
                "kind": "inline",
                "sha256": "e20545664020aa577486c4a65be0c64c2fee1b085590b8cb366af64144d5fc7f",
                "observation_count": 4,
                "resampling": "with_replacement",
            },
        )
        self.assertEqual(
            [point["parameters"]["R"] for point in plan["points"]],
            ["1.002e+4", "9.98e+3", "9.95e+3", "10075", "9.98e+3", "9.98e+3"],
        )
        self.assertEqual(
            plan["definition_hash"],
            "3ef8f085302821a7d2b01699130f105d191f2c51fc4034fd036d563c7b8704f4",
        )
        self.assertEqual(
            hashlib.sha256(statistical_engine._artifact_bytes(plan)).hexdigest(),
            "423dc97d96882e26cd673b756c3d2adb3b93cc4a6524706070610b70de4bd59f",
        )
        saved = statistical_engine.save_statistical_plan(self.runs, plan)
        self.assertEqual(
            saved["empirical_sources"],
            [{"name": "R", "unit": "ohm", **variable["source"]}],
        )
        self.assertEqual(
            statistical_engine.load_statistical_plan(self.runs, saved["plan_id"]),
            plan,
        )

    def test_empirical_csv_matches_inline_draws_and_records_raw_source(self) -> None:
        source_root = Path(self.temporary_directory.name) / "source"
        source_root.mkdir()
        csv_path = source_root / "lot.csv"
        csv_path.write_text(
            "serial,resistance_ohm\nA,9980\nB,10020\nC,10075\nD,9950\n",
            encoding="utf-8",
        )
        csv_plan = statistical_engine.build_statistical_plan(
            [
                {
                    "name": "R",
                    "distribution": "empirical",
                    "csv_path": "lot.csv",
                    "column": "resistance_ohm",
                    "unit": "ohm",
                }
            ],
            20,
            7,
            source_root=source_root,
        )
        inline_plan = statistical_engine.build_statistical_plan(
            [
                {
                    "name": "R",
                    "distribution": "empirical",
                    "values": [9980, 10020, 10075, 9950],
                    "unit": "ohm",
                }
            ],
            20,
            7,
        )

        self.assertEqual(csv_plan["points"], inline_plan["points"])
        source = csv_plan["definition"]["variables"][0]["source"]
        self.assertEqual(source["kind"], "csv")
        self.assertEqual(source["path"], "lot.csv")
        self.assertEqual(source["column"], "resistance_ohm")
        self.assertEqual(source["observation_count"], 4)
        self.assertEqual(
            source["sha256"], hashlib.sha256(csv_path.read_bytes()).hexdigest()
        )
        csv_path.write_text(
            "serial,resistance_ohm\nA,9980\nB,10020\nC,10075\nD,9960\n",
            encoding="utf-8",
        )
        changed = statistical_engine.build_statistical_plan(
            [
                {
                    "name": "R",
                    "distribution": "empirical",
                    "csv_path": "lot.csv",
                    "column": "resistance_ohm",
                    "unit": "ohm",
                }
            ],
            20,
            7,
            source_root=source_root,
        )
        self.assertNotEqual(changed["definition_hash"], csv_plan["definition_hash"])
        csv_path.unlink()
        saved = statistical_engine.save_statistical_plan(self.runs, csv_plan)
        self.assertEqual(
            statistical_engine.load_statistical_plan(self.runs, saved["plan_id"]),
            csv_plan,
        )

    def test_empirical_population_preserves_observed_frequencies(self) -> None:
        plan = statistical_engine.build_statistical_plan(
            [
                {
                    "name": "LOT",
                    "distribution": "empirical",
                    "values": [1, 1, 1, 2],
                }
            ],
            1_000,
            91,
        )
        values = [point["parameters"]["LOT"] for point in plan["points"]]
        self.assertGreater(values.count("1"), 700)
        self.assertLess(values.count("1"), 800)

    def test_empirical_draws_are_stable_under_variable_reordering(self) -> None:
        variables = [
            {"name": "R", "distribution": "empirical", "values": [1, 2, 3]},
            {"name": "C", "distribution": "empirical", "values": [4, 5, 6]},
        ]
        original = statistical_engine.build_statistical_plan(variables, 20, 33)
        reordered = statistical_engine.build_statistical_plan(
            list(reversed(variables)), 20, 33
        )
        for index in range(20):
            for name in ("R", "C"):
                self.assertEqual(
                    original["points"][index]["parameters"][name],
                    reordered["points"][index]["parameters"][name],
                )

    def test_named_corner_axes_expand_in_sample_major_order(self) -> None:
        plan = statistical_engine.build_statistical_plan(
            [
                {
                    "name": "R",
                    "distribution": "discrete",
                    "values": ["10k"],
                    "weights": [1],
                }
            ],
            2,
            20260824,
            corner_axes=[
                {
                    "name": "temperature",
                    "parameter": "TEMP",
                    "unit": "degC",
                    "values": [
                        {"name": "cold", "value": -40},
                        {"name": "hot", "value": "125"},
                    ],
                },
                {
                    "name": "supply",
                    "parameter": "VCC",
                    "unit": "V",
                    "values": [
                        {"name": "low", "value": 3.0},
                        {"name": "high", "value": "3.6"},
                    ],
                },
            ],
            corner_aggregate=True,
        )

        self.assertEqual(plan["generator_version"], "sha256-counter-corners-v5")
        self.assertEqual(plan["sample_count"], 2)
        self.assertEqual(plan["point_count"], 8)
        self.assertEqual(plan["parameter_order"], ["R", "TEMP", "VCC"])
        self.assertEqual(
            plan["parameter_units"], {"R": "", "TEMP": "degC", "VCC": "V"}
        )
        self.assertEqual(
            [
                {
                    "sample_index": point["sample_index"],
                    "corners": point["corners"],
                    "parameters": point["parameters"],
                }
                for point in plan["points"]
            ],
            [
                {
                    "sample_index": sample_index,
                    "corners": {"temperature": temperature, "supply": supply},
                    "parameters": {"R": "10k", "TEMP": temp, "VCC": vcc},
                }
                for sample_index in range(2)
                for temperature, temp in (("cold", "-4e+1"), ("hot", "125"))
                for supply, vcc in (("low", "3"), ("high", "3.6"))
            ],
        )
        self.assertTrue(plan["definition"]["corner_aggregate"])
        self.assertEqual(
            plan["definition_hash"],
            "dade0fb17feba012bbb235f0cf5e1ccc659921f4430b2d67644dfea856c57f6c",
        )
        self.assertEqual(
            hashlib.sha256(statistical_engine._artifact_bytes(plan)).hexdigest(),
            "5f359e32ce274c0cc75e6046f840a7378e77c710dbce7ab010ef767a8dda9448",
        )
        saved = statistical_engine.save_statistical_plan(self.runs, plan)
        self.assertEqual(saved["point_count"], 8)
        self.assertTrue(saved["corner_aggregate"])
        self.assertEqual(saved["corner_axes"], plan["definition"]["corner_axes"])
        self.assertEqual(
            statistical_engine.load_statistical_plan(self.runs, saved["plan_id"]),
            plan,
        )

    def test_corner_axis_validation_and_expansion_bounds_fail_closed(self) -> None:
        base_axis = {
            "name": "temperature",
            "parameter": "TEMP",
            "unit": "degC",
            "values": [
                {"name": "cold", "value": "-40"},
                {"name": "hot", "value": "125"},
            ],
        }
        invalid = [
            ({}, "list"),
            ([{"name": "temperature"}], "only"),
            ([{**base_axis, "name": "bad-name"}], "axis names"),
            ([{**base_axis, "parameter": "bad-name"}], "parameter names"),
            ([{**base_axis, "values": []}], "values"),
            (
                [{**base_axis, "values": [{"name": "cold", "value": "-40\n.end"}]}],
                "single SPICE token",
            ),
            (
                [
                    {
                        **base_axis,
                        "values": [
                            {"name": "same", "value": "-40"},
                            {"name": "same", "value": "125"},
                        ],
                    }
                ],
                "unique",
            ),
            ([base_axis, {**base_axis, "parameter": "VCC"}], "duplicate axis"),
            (
                [base_axis, {**base_axis, "name": "supply"}],
                "duplicate corner parameter",
            ),
        ]
        for axes, message in invalid:
            with self.subTest(message=message), self.assertRaisesRegex(
                ValueError, message
            ):
                statistical_engine.build_statistical_plan(
                    self.variables(),
                    2,
                    1,
                    corner_axes=axes,  # type: ignore[arg-type]
                )

        with self.assertRaisesRegex(ValueError, "collides"):
            statistical_engine.build_statistical_plan(
                self.variables(),
                2,
                1,
                corner_axes=[{**base_axis, "parameter": "R"}],
            )
        with self.assertRaisesRegex(ValueError, "corner_aggregate"):
            statistical_engine.build_statistical_plan(
                self.variables(), 2, 1, corner_aggregate=True
            )
        with self.assertRaisesRegex(ValueError, "boolean"):
            statistical_engine.build_statistical_plan(
                self.variables(),
                2,
                1,
                corner_axes=[base_axis],
                corner_aggregate=1,  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "1,000 expanded points"):
            statistical_engine.build_statistical_plan(
                self.variables(),
                501,
                1,
                corner_axes=[base_axis],
            )
        many_variables = [
            {
                "name": f"P{index}",
                "distribution": "uniform",
                "minimum": 0,
                "maximum": 1,
            }
            for index in range(statistical_engine.MAX_STATISTICAL_VARIABLES)
        ]
        with self.assertRaisesRegex(ValueError, "10,000 values"):
            statistical_engine.build_statistical_plan(
                many_variables,
                300,
                1,
                corner_axes=[base_axis],
            )

    def test_empirical_validation_and_csv_confinement_fail_closed(self) -> None:
        source_root = Path(self.temporary_directory.name) / "source"
        source_root.mkdir()
        outside = Path(self.temporary_directory.name) / "outside.csv"
        outside.write_text("value\n1\n", encoding="utf-8")
        invalid_inline = [
            ({"values": []}, "observations"),
            ({"values": [1, float("nan")]}, "finite"),
            ({"values": [1], "csv_path": "lot.csv", "column": "value"}, "either"),
            ({"csv_path": "lot.csv"}, "column"),
            ({"source": {}}, "not valid"),
        ]
        for fields, message in invalid_inline:
            with self.subTest(fields=fields), self.assertRaisesRegex(ValueError, message):
                statistical_engine.build_statistical_plan(
                    [{"name": "E", "distribution": "empirical", **fields}],
                    2,
                    1,
                    source_root=source_root,
                )

        with self.assertRaisesRegex(ValueError, "inside source_root"):
            statistical_engine.build_statistical_plan(
                [
                    {
                        "name": "E",
                        "distribution": "empirical",
                        "csv_path": "../outside.csv",
                        "column": "value",
                    }
                ],
                2,
                1,
                source_root=source_root,
            )
        linked = source_root / "linked.csv"
        linked.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            statistical_engine.build_statistical_plan(
                [
                    {
                        "name": "E",
                        "distribution": "empirical",
                        "csv_path": "linked.csv",
                        "column": "value",
                    }
                ],
                2,
                1,
                source_root=source_root,
            )

        malformed = {
            "missing.csv": ("other\n1\n", "column"),
            "empty.csv": ("value\n", "observations"),
            "nonfinite.csv": ("value\nNaN\n", "finite"),
            "ragged.csv": ("value\n1,2\n", "row"),
        }
        for filename, (contents, message) in malformed.items():
            (source_root / filename).write_text(contents, encoding="utf-8")
            with self.subTest(filename=filename), self.assertRaisesRegex(
                ValueError, message
            ):
                statistical_engine.build_statistical_plan(
                    [
                        {
                            "name": "E",
                            "distribution": "empirical",
                            "csv_path": filename,
                            "column": "value",
                        }
                    ],
                    2,
                    1,
                    source_root=source_root,
                )

        bounded = source_root / "bounded.csv"
        bounded.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
        definition = [
            {
                "name": "E",
                "distribution": "empirical",
                "csv_path": "bounded.csv",
                "column": "a",
            }
        ]
        with patch.object(
            statistical_engine, "MAX_EMPIRICAL_CSV_BYTES", 4
        ), self.assertRaisesRegex(ValueError, "exceeds 4 bytes"):
            statistical_engine.build_statistical_plan(
                definition, 2, 1, source_root=source_root
            )
        with patch.object(
            statistical_engine, "MAX_EMPIRICAL_CSV_COLUMNS", 1
        ), self.assertRaisesRegex(ValueError, "header"):
            statistical_engine.build_statistical_plan(
                definition, 2, 1, source_root=source_root
            )
        with patch.object(
            statistical_engine, "MAX_EMPIRICAL_OBSERVATIONS", 1
        ), self.assertRaisesRegex(ValueError, "exceeds 1 observations"):
            statistical_engine.build_statistical_plan(
                definition, 2, 1, source_root=source_root
            )

    def test_draws_are_stable_per_variable_and_change_with_seed(self) -> None:
        original = statistical_engine.build_statistical_plan(
            self.variables(), 3, 20260824
        )
        reordered = statistical_engine.build_statistical_plan(
            list(reversed(self.variables())), 3, 20260824
        )
        changed = statistical_engine.build_statistical_plan(
            self.variables(), 3, 20260825
        )

        for index in range(3):
            self.assertEqual(
                original["points"][index]["parameters"],
                {
                    name: reordered["points"][index]["parameters"][name]
                    for name in ("R", "C")
                },
            )
        self.assertNotEqual(original["points"], changed["points"])

    def test_mixed_draws_are_stable_per_variable_when_reordered(self) -> None:
        variables = [
            {
                "name": "G",
                "distribution": "gaussian",
                "minimum": -3.0,
                "maximum": 3.0,
                "nominal": 0.0,
                "sigma": 1.0,
            },
            {
                "name": "D",
                "distribution": "discrete",
                "values": ["10k", "11k"],
                "weights": [4, 1],
            },
        ]
        original = statistical_engine.build_statistical_plan(variables, 20, 41)
        reordered = statistical_engine.build_statistical_plan(
            list(reversed(variables)), 20, 41
        )
        for index in range(20):
            for name in ("G", "D"):
                self.assertEqual(
                    original["points"][index]["parameters"][name],
                    reordered["points"][index]["parameters"][name],
                )

    def test_correlated_gaussian_plan_is_stable_under_equivalent_reordering(
        self,
    ) -> None:
        variables = [
            {
                "name": "R1",
                "distribution": "gaussian",
                "minimum": -5.0,
                "maximum": 5.0,
                "nominal": 0.0,
                "sigma": 1.0,
            },
            {
                "name": "R2",
                "distribution": "gaussian",
                "minimum": -10.0,
                "maximum": 10.0,
                "nominal": 0.0,
                "sigma": 2.0,
            },
        ]
        original = statistical_engine.build_statistical_plan(
            variables,
            4,
            20260824,
            correlations=[
                {"variables": ["R1", "R2"], "matrix": [[1, 0.75], [0.75, 1]]}
            ],
        )
        reordered = statistical_engine.build_statistical_plan(
            list(reversed(variables)),
            4,
            20260824,
            correlations=[
                {"variables": ["R2", "R1"], "matrix": [[1, 0.75], [0.75, 1]]}
            ],
        )

        self.assertEqual(
            original["generator_version"], "sha256-counter-correlations-v3"
        )
        self.assertEqual(
            original["definition"]["correlations"],
            [{"variables": ["R1", "R2"], "matrix": [["1", "0.75"], ["0.75", "1"]]}],
        )
        self.assertEqual(
            original["definition_hash"],
            "d5c3120fbac545f3dc7ad2e2de81d0890b2d2694dcd7f9532e07b14540290646",
        )
        self.assertEqual(
            [point["parameters"] for point in original["points"]],
            [
                {"R1": "-2.9516371952559719", "R2": "-5.6651716855255113"},
                {"R1": "-0.75042367881680089", "R2": "-0.74642184300600231"},
                {"R1": "-0.0630219704527577", "R2": "-1.5764784558766531"},
                {"R1": "-0.84334084519936672", "R2": "-1.3870394390606106"},
            ],
        )
        self.assertEqual(
            hashlib.sha256(statistical_engine._artifact_bytes(original)).hexdigest(),
            "4d12fec907fa5dddf9bfee9c6bc0f0a495d58249e8f8632f450a9f7dbb7d9883",
        )
        for index in range(4):
            for name in ("R1", "R2"):
                self.assertEqual(
                    original["points"][index]["parameters"][name],
                    reordered["points"][index]["parameters"][name],
                )
        saved = statistical_engine.save_statistical_plan(self.runs, original)
        self.assertEqual(saved["generator_version"], "sha256-counter-correlations-v3")
        self.assertEqual(saved["correlations"], original["definition"]["correlations"])
        self.assertEqual(
            statistical_engine.load_statistical_plan(self.runs, saved["plan_id"]),
            original,
        )

    def test_correlated_population_tracks_requested_relationship(self) -> None:
        plan = statistical_engine.build_statistical_plan(
            [
                {
                    "name": name,
                    "distribution": "gaussian",
                    "minimum": -10.0,
                    "maximum": 10.0,
                    "nominal": 0.0,
                    "sigma": 1.0,
                }
                for name in ("A", "B")
            ],
            1_000,
            17,
            correlations=[
                {"variables": ["A", "B"], "matrix": [[1, 0.8], [0.8, 1]]}
            ],
        )
        first = [float(point["parameters"]["A"]) for point in plan["points"]]
        second = [float(point["parameters"]["B"]) for point in plan["points"]]
        measured = statistics.correlation(first, second)
        self.assertGreater(measured, 0.76)
        self.assertLess(measured, 0.84)

    def test_correlation_matrix_permutation_is_canonicalized_by_name(self) -> None:
        variables = [
            {
                "name": name,
                "distribution": "gaussian",
                "minimum": -5.0,
                "maximum": 5.0,
                "nominal": 0.0,
                "sigma": 1.0,
            }
            for name in ("A", "B", "C")
        ]
        original = statistical_engine.build_statistical_plan(
            variables,
            5,
            12,
            correlations=[
                {
                    "variables": ["A", "B", "C"],
                    "matrix": [[1, 0.2, 0.6], [0.2, 1, -0.1], [0.6, -0.1, 1]],
                }
            ],
        )
        permuted = statistical_engine.build_statistical_plan(
            [variables[2], variables[0], variables[1]],
            5,
            12,
            correlations=[
                {
                    "variables": ["C", "A", "B"],
                    "matrix": [[1, 0.6, -0.1], [0.6, 1, 0.2], [-0.1, 0.2, 1]],
                }
            ],
        )
        for index in range(5):
            for name in ("A", "B", "C"):
                self.assertEqual(
                    original["points"][index]["parameters"][name],
                    permuted["points"][index]["parameters"][name],
                )

    def test_positive_semidefinite_perfect_correlation_is_supported(self) -> None:
        plan = statistical_engine.build_statistical_plan(
            [
                {
                    "name": name,
                    "distribution": "gaussian",
                    "minimum": -5.0,
                    "maximum": 5.0,
                    "nominal": 0.0,
                    "sigma": 1.0,
                }
                for name in ("A", "B")
            ],
            10,
            9,
            correlations=[
                {"variables": ["A", "B"], "matrix": [[1, 1], [1, 1]]}
            ],
        )
        self.assertEqual(
            [point["parameters"]["A"] for point in plan["points"]],
            [point["parameters"]["B"] for point in plan["points"]],
        )

    def test_correlation_validation_is_fail_closed(self) -> None:
        variables = [
            {
                "name": name,
                "distribution": "gaussian",
                "minimum": -5.0,
                "maximum": 5.0,
                "nominal": 0.0,
                "sigma": 1.0,
            }
            for name in ("A", "B", "C")
        ]
        invalid = [
            ("list", {}),
            ("at least two", [{"variables": ["A"], "matrix": [[1]]}]),
            ("unknown", [{"variables": ["A", "Z"], "matrix": [[1, 0], [0, 1]]}]),
            ("duplicate", [{"variables": ["A", "A"], "matrix": [[1, 0], [0, 1]]}]),
            ("square", [{"variables": ["A", "B"], "matrix": [[1, 0]]}]),
            (
                "symmetric",
                [{"variables": ["A", "B"], "matrix": [[1, 0.2], [0.3, 1]]}],
            ),
            ("diagonal", [{"variables": ["A", "B"], "matrix": [[0.9, 0], [0, 1]]}]),
            ("between -1 and 1", [{"variables": ["A", "B"], "matrix": [[1, 2], [2, 1]]}]),
            (
                "positive semidefinite",
                [
                    {
                        "variables": ["A", "B", "C"],
                        "matrix": [
                            [1, 0.9, 0.9],
                            [0.9, 1, -0.9],
                            [0.9, -0.9, 1],
                        ],
                    }
                ],
            ),
            (
                "overlapping",
                [
                    {"variables": ["A", "B"], "matrix": [[1, 0], [0, 1]]},
                    {"variables": ["B", "C"], "matrix": [[1, 0], [0, 1]]},
                ],
            ),
            (
                "finite",
                [
                    {
                        "variables": ["A", "B"],
                        "matrix": [[1, float("nan")], [float("nan"), 1]],
                    }
                ],
            ),
        ]
        for message, correlations in invalid:
            with self.subTest(message=message), self.assertRaisesRegex(
                ValueError, message
            ):
                statistical_engine.build_statistical_plan(
                    variables,
                    2,
                    1,
                    correlations=correlations,  # type: ignore[arg-type]
                )

        with self.assertRaisesRegex(ValueError, "must use gaussian"):
            statistical_engine.build_statistical_plan(
                self.variables(),
                2,
                1,
                correlations=[
                    {"variables": ["R", "C"], "matrix": [[1, 0.5], [0.5, 1]]}
                ],
            )

    def test_correlated_gaussian_rejection_work_is_bounded(self) -> None:
        with patch.object(
            statistical_engine,
            "_correlation_fraction",
            return_value=Decimal("0.5"),
        ), self.assertRaisesRegex(ValueError, "within 4096 attempts"):
            statistical_engine.build_statistical_plan(
                [
                    {
                        "name": name,
                        "distribution": "gaussian",
                        "minimum": -1.0,
                        "maximum": 1.0,
                        "nominal": 0.0,
                        "sigma": 1.0,
                    }
                    for name in ("A", "B")
                ],
                1,
                0,
                correlations=[
                    {"variables": ["A", "B"], "matrix": [[1, 0], [0, 1]]}
                ],
            )

    def test_discrete_cumulative_boundaries_choose_the_next_bin(self) -> None:
        variable = {
            "name": "D",
            "values": ["A", "B", "C"],
            "weights": ["1", "2", "1"],
        }
        with patch.object(
            statistical_engine, "_distribution_fraction", return_value=Decimal("0.25")
        ):
            self.assertEqual(statistical_engine._weighted_discrete(variable, 0, 0), "B")
        with patch.object(
            statistical_engine, "_distribution_fraction", return_value=Decimal("0.75")
        ):
            self.assertEqual(statistical_engine._weighted_discrete(variable, 0, 0), "C")

    def test_equivalent_discrete_weights_normalize_to_the_same_plan(self) -> None:
        first = {
            "name": "D",
            "distribution": "discrete",
            "values": ["A", "B", "C"],
            "weights": [1, 3, 1],
        }
        scaled = {**first, "weights": [2, 6, 2]}
        self.assertEqual(
            statistical_engine.build_statistical_plan([first], 20, 7),
            statistical_engine.build_statistical_plan([scaled], 20, 7),
        )

    def test_distribution_population_is_statistically_sane_and_bounded(self) -> None:
        plan = statistical_engine.build_statistical_plan(
            [
                {
                    "name": "G",
                    "distribution": "gaussian",
                    "minimum": -4.0,
                    "maximum": 4.0,
                    "nominal": 0.0,
                    "sigma": 1.0,
                },
                {
                    "name": "D",
                    "distribution": "discrete",
                    "values": ["A", "B", "C"],
                    "weights": [1, 3, 1],
                },
            ],
            1_000,
            7,
        )
        gaussian = [float(point["parameters"]["G"]) for point in plan["points"]]
        discrete = [point["parameters"]["D"] for point in plan["points"]]
        self.assertTrue(all(-4.0 <= value <= 4.0 for value in gaussian))
        self.assertLess(abs(statistics.mean(gaussian)), 0.1)
        self.assertGreater(statistics.pstdev(gaussian), 0.9)
        self.assertLess(statistics.pstdev(gaussian), 1.1)
        self.assertGreater(discrete.count("B"), discrete.count("A") * 2)
        self.assertGreater(discrete.count("B"), discrete.count("C") * 2)

    def test_gaussian_rejection_work_is_bounded(self) -> None:
        with patch.object(
            statistical_engine,
            "_distribution_fraction",
            return_value=Decimal("0.5"),
        ), self.assertRaisesRegex(ValueError, "within 4096 attempts"):
            statistical_engine.build_statistical_plan(
                [
                    {
                        "name": "G",
                        "distribution": "gaussian",
                        "minimum": -1.0,
                        "maximum": 1.0,
                        "nominal": 0.0,
                        "sigma": 1.0,
                    }
                ],
                1,
                0,
            )

    def test_definition_validation_is_fail_closed(self) -> None:
        invalid = [
            ([], 1, 0, "non-empty"),
            (self.variables(), 0, 0, "sample_count"),
            (self.variables(), 1, True, "seed"),
            (self.variables(), 1, -1, "seed"),
            (
                [
                    {
                        "name": "U",
                        "distribution": "uniform",
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "sigma": 0.1,
                    }
                ],
                1,
                0,
                "not valid for uniform",
            ),
            (
                [
                    {
                        "name": "R",
                        "distribution": "lognormal",
                        "minimum": 1.0,
                        "maximum": 2.0,
                    }
                ],
                1,
                0,
                "distribution",
            ),
            (
                [
                    {
                        "name": "G",
                        "distribution": "gaussian",
                        "minimum": -1.0,
                        "maximum": 1.0,
                        "sigma": 0.2,
                    }
                ],
                1,
                0,
                "nominal is required",
            ),
            (
                [
                    {
                        "name": "G",
                        "distribution": "gaussian",
                        "minimum": -1.0,
                        "maximum": 1.0,
                        "nominal": 0.0,
                        "sigma": 0.0,
                    }
                ],
                1,
                0,
                "positive",
            ),
            (
                [
                    {
                        "name": "G",
                        "distribution": "gaussian",
                        "minimum": -0.01,
                        "maximum": 0.01,
                        "nominal": 0.0,
                        "sigma": 1.0,
                    }
                ],
                1,
                0,
                "span",
            ),
            (
                [
                    {
                        "name": "D",
                        "distribution": "discrete",
                        "values": ["A", "B"],
                        "weights": [1],
                    }
                ],
                1,
                0,
                "weights must match",
            ),
            (
                [
                    {
                        "name": "D",
                        "distribution": "discrete",
                        "values": ["A", "B"],
                        "weights": [1, 0],
                    }
                ],
                1,
                0,
                "positive",
            ),
            (
                [
                    {
                        "name": "D",
                        "distribution": "discrete",
                        "values": ["A", "A"],
                        "weights": [1, 1],
                    }
                ],
                1,
                0,
                "unique",
            ),
            (
                [
                    {
                        "name": "D",
                        "distribution": "discrete",
                        "values": ["A", "B"],
                        "weights": [1, 1],
                        "nominal": "C",
                    }
                ],
                1,
                0,
                "one of",
            ),
            (
                [
                    {
                        "name": "R",
                        "distribution": "uniform",
                        "minimum": 2.0,
                        "maximum": 1.0,
                    }
                ],
                1,
                0,
                "less than",
            ),
            (
                [
                    {
                        "name": "R",
                        "distribution": "uniform",
                        "minimum": 1.0,
                        "maximum": 2.0,
                        "nominal": 3.0,
                    }
                ],
                1,
                0,
                "within",
            ),
            (
                [
                    {
                        "name": "bad-name",
                        "distribution": "uniform",
                        "minimum": 1.0,
                        "maximum": 2.0,
                    }
                ],
                1,
                0,
                "names",
            ),
            (
                [self.variables()[0], self.variables()[0]],
                1,
                0,
                "duplicate",
            ),
        ]
        for variables, sample_count, seed, message in invalid:
            with self.subTest(message=message), self.assertRaisesRegex(
                ValueError, message
            ):
                statistical_engine.build_statistical_plan(
                    variables, sample_count, seed  # type: ignore[arg-type]
                )

    def test_cell_limit_bounds_plan_payload(self) -> None:
        variables = [
            {
                "name": f"P{index}",
                "distribution": "uniform",
                "minimum": 0.0,
                "maximum": 1.0,
            }
            for index in range(statistical_engine.MAX_STATISTICAL_VARIABLES)
        ]
        with self.assertRaisesRegex(ValueError, "values"):
            statistical_engine.build_statistical_plan(variables, 313, 0)

    def test_plan_publication_is_idempotent_and_content_addressed(self) -> None:
        first = statistical_engine.generate_statistical_plan(
            self.runs, self.variables(), 3, 20260824
        )
        second = statistical_engine.generate_statistical_plan(
            self.runs, self.variables(), 3, 20260824
        )

        self.assertEqual(first, second)
        self.assertRegex(first["plan_id"], r"^statistical-plan-[0-9a-f]{16}$")
        plan_file = Path(first["plan_file"])
        self.assertTrue(plan_file.is_file())
        loaded = statistical_engine.load_statistical_plan(
            self.runs, first["plan_id"]
        )
        self.assertEqual(loaded["points"], first["points"])
        self.assertEqual(json.loads(plan_file.read_text()), loaded)

    def test_load_rejects_tampering_and_symlinks(self) -> None:
        result = statistical_engine.generate_statistical_plan(
            self.runs, self.variables(), 2, 4
        )
        plan_file = Path(result["plan_file"])
        plan_file.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "content address"):
            statistical_engine.load_statistical_plan(self.runs, result["plan_id"])

        other_runs = Path(self.temporary_directory.name) / "other-runs"
        other_runs.mkdir()
        outside = Path(self.temporary_directory.name) / "outside"
        outside.mkdir()
        (other_runs / "statistical-plans").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            statistical_engine.generate_statistical_plan(
                other_runs, self.variables(), 1, 0
            )


if __name__ == "__main__":
    unittest.main()
