import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import statistical_engine
import study_recipe
from examples import mixed_signal_daq_study


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RECIPE_PATH = PROJECT_ROOT / "examples/mixed_signal_daq.ltstudy.json"


class StudyRecipeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.recipe = study_recipe.load_study_recipe(RECIPE_PATH)

    def _workspace_recipe(self, root: Path) -> dict[str, object]:
        recipe = copy.deepcopy(self.recipe)
        experiments = recipe["experiments"]
        assert isinstance(experiments, list)
        for experiment in experiments:
            assert isinstance(experiment, dict)
            source = PROJECT_ROOT / str(experiment["netlist_path"])
            target = root / source.name
            shutil.copyfile(source, target)
            experiment["netlist_path"] = source.name
        return recipe

    def test_daq_recipe_matches_agent_authored_statistical_plan(self) -> None:
        preview = study_recipe.preview_study_recipe(self.recipe, PROJECT_ROOT)
        expected = statistical_engine.build_statistical_plan(
            mixed_signal_daq_study.VARIABLES,
            mixed_signal_daq_study.SAMPLE_COUNT,
            mixed_signal_daq_study.SEED,
            mixed_signal_daq_study.CORRELATIONS,
            mixed_signal_daq_study.CORNERS,
            sampling_method="halton",
        )
        expected_sha = hashlib.sha256(
            statistical_engine._artifact_bytes(expected)
        ).hexdigest()

        self.assertTrue(preview["valid"])
        self.assertEqual(preview["plan"]["plan_sha256"], expected_sha)
        self.assertEqual(
            preview["plan"]["plan_id"], f"statistical-plan-{expected_sha[:16]}"
        )
        self.assertEqual(preview["plan"]["point_count"], 24)
        self.assertEqual(preview["execution"]["total_run_count"], 48)
        self.assertEqual(preview["errors"], [])
        self.assertEqual(
            self.recipe["experiments"][0]["waveform_analyses"],
            mixed_signal_daq_study.AC_ANALYSES,
        )
        self.assertEqual(
            self.recipe["experiments"][1]["waveform_analyses"],
            mixed_signal_daq_study.TRANSIENT_ANALYSES,
        )

    def test_preview_does_not_publish_plan_or_run_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recipe = self._workspace_recipe(root)
            before = sorted(path.name for path in root.iterdir())
            preview = study_recipe.preview_study_recipe(recipe, root)
            after = sorted(path.name for path in root.iterdir())

        self.assertTrue(preview["valid"])
        self.assertEqual(after, before)
        self.assertNotIn("statistical-plans", after)
        self.assertNotIn("runs", after)

    def test_preview_accepts_a_recipe_with_no_sampling_method_or_corners(self) -> None:
        """Regression: sampling_method and corner_axes are only written into
        the engine's internal plan definition when non-default (see
        statistical_engine._build_definition). A recipe that never opts into
        either -- i.e. the plain default case -- must still preview cleanly
        instead of KeyErroring on a key the definition never set.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "plain.cir").write_text(
                "V1 in 0 AC 1\nR1 in out {R_VAL}\nC1 out 0 1u\n"
                ".ac dec 10 10 10k\n.end\n"
            )
            recipe = {
                "schema_version": study_recipe.STUDY_RECIPE_SCHEMA_VERSION,
                "kind": "statistical",
                "name": "minimal",
                "description": "minimal recipe with no sampling_method or corners",
                "plan": {
                    "variables": [
                        {
                            "name": "R_VAL",
                            "distribution": "gaussian",
                            "nominal": 1000,
                            "sigma": 10,
                            "minimum": 950,
                            "maximum": 1050,
                            "unit": "ohm",
                        }
                    ],
                    "sample_count": 4,
                    "seed": 1,
                },
                "experiments": [
                    {
                        "name": "ac",
                        "netlist_path": "plain.cir",
                        "filename": "plain.cir",
                        "waveform_analyses": [
                            {
                                "name": "check",
                                "variable": "V(out)",
                                "requirements": [
                                    {
                                        "metric": "ac_gain_db",
                                        "operator": ">=",
                                        "target": -1.0,
                                        "frequency_value": 10,
                                    }
                                ],
                            }
                        ],
                    }
                ],
                "execution": {"max_concurrency": 1, "reuse_cache": False},
            }

            preview = study_recipe.preview_study_recipe(recipe, root)

        self.assertTrue(preview["valid"], preview.get("errors"))
        self.assertEqual(preview["plan"]["sampling_method"], "independent")
        self.assertEqual(preview["plan"]["corner_axes"], [])

    def _minimal_recipe(self, netlist_relative_path: str) -> dict[str, object]:
        return {
            "schema_version": study_recipe.STUDY_RECIPE_SCHEMA_VERSION,
            "kind": "statistical",
            "name": "minimal",
            "description": "minimal recipe for encoding regression coverage",
            "plan": {
                "variables": [
                    {
                        "name": "R_VAL",
                        "distribution": "gaussian",
                        "nominal": 1000,
                        "sigma": 10,
                        "minimum": 950,
                        "maximum": 1050,
                        "unit": "ohm",
                    }
                ],
                "sample_count": 4,
                "seed": 1,
            },
            "experiments": [
                {
                    "name": "ac",
                    "netlist_path": netlist_relative_path,
                    "filename": Path(netlist_relative_path).name,
                    "waveform_analyses": [
                        {
                            "name": "check",
                            "variable": "V(out)",
                            "requirements": [
                                {
                                    "metric": "ac_gain_db",
                                    "operator": ">=",
                                    "target": -1.0,
                                    "frequency_value": 10,
                                }
                            ],
                        }
                    ],
                }
            ],
            "execution": {"max_concurrency": 1, "reuse_cache": False},
        }

    def test_preview_and_execution_accept_a_utf16_netlist_ltspice_actually_exports(
        self,
    ) -> None:
        """Regression: LTspice's own "Create Netlist" command on Windows can
        write the .net file as UTF-16LE with a BOM, not UTF-8. Both the
        preview path and the execution-time loader must accept it via the
        same decode_text() helper already used for LTspice's .log and .raw
        output, instead of a bare read_text(encoding="utf-8") that raises
        UnicodeDecodeError on real LTspice-exported netlists.
        """
        netlist_text = (
            "V1 in 0 AC 1\nR1 in out {R_VAL}\nC1 out 0 1u\n"
            ".ac dec 10 10 10k\n.end\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "exported.net").write_bytes(
                b"\xff\xfe" + netlist_text.encode("utf-16-le")
            )
            recipe = self._minimal_recipe("exported.net")

            preview = study_recipe.preview_study_recipe(recipe, root)
            experiments = study_recipe.load_recipe_experiments(recipe, root)

        self.assertTrue(preview["valid"], preview.get("errors"))
        self.assertEqual(experiments[0]["netlist_template"], netlist_text)

    def test_invalid_sample_count_has_a_field_path(self) -> None:
        recipe = copy.deepcopy(self.recipe)
        recipe["plan"]["sample_count"] = 0
        preview = study_recipe.preview_study_recipe(recipe, PROJECT_ROOT)

        self.assertFalse(preview["valid"])
        self.assertEqual(preview["errors"][0]["path"], "plan.sample_count")
        self.assertEqual(preview["errors"][0]["code"], "invalid_plan")

    def test_netlist_cannot_escape_the_selected_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recipe = self._workspace_recipe(root)
            recipe["experiments"][0]["netlist_path"] = "../outside.cir"
            preview = study_recipe.preview_study_recipe(recipe, root)

        self.assertFalse(preview["valid"])
        self.assertIn(
            "escaped_workspace", {error["code"] for error in preview["errors"]}
        )

    def test_netlist_symlinks_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recipe = self._workspace_recipe(root)
            ac_path = root / "mixed_signal_daq_ac.cir"
            real_path = root / "real_ac.cir"
            ac_path.replace(real_path)
            ac_path.symlink_to(real_path)
            preview = study_recipe.preview_study_recipe(recipe, root)

        self.assertFalse(preview["valid"])
        self.assertIn(
            "symlink_rejected", {error["code"] for error in preview["errors"]}
        )

    def test_netlist_must_use_every_planned_parameter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recipe = self._workspace_recipe(root)
            ac_path = root / "mixed_signal_daq_ac.cir"
            ac_path.write_text(
                ac_path.read_text(encoding="utf-8").replace("{RAA1}", "1000"),
                encoding="utf-8",
            )
            preview = study_recipe.preview_study_recipe(recipe, root)

        self.assertFalse(preview["valid"])
        error = next(
            error for error in preview["errors"] if error["code"] == "invalid_experiment"
        )
        self.assertEqual(error["path"], "experiments[0].netlist_path")
        self.assertIn("{RAA1}", error["message"])

    def test_loader_rejects_nonfinite_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid.ltstudy.json"
            path.write_text('{"schema_version": NaN}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-finite"):
                study_recipe.load_study_recipe(path)

    def test_unknown_fields_fail_closed(self) -> None:
        recipe = copy.deepcopy(self.recipe)
        recipe["surprise"] = True
        preview = study_recipe.preview_study_recipe(recipe, PROJECT_ROOT)

        self.assertFalse(preview["valid"])
        self.assertIn(
            {
                "path": "surprise",
                "code": "unknown_field",
                "message": "unknown study recipe field: surprise",
            },
            preview["errors"],
        )

    def test_report_context_fields_and_paths_fail_closed(self) -> None:
        recipe = copy.deepcopy(self.recipe)
        recipe["report_context"] = {
            "title": "DAQ report",
            "surprise": "no",
            "schematic_path": "../outside.svg",
            "schematic_source_path": "../outside.cir",
        }
        preview = study_recipe.preview_study_recipe(recipe, PROJECT_ROOT)

        self.assertFalse(preview["valid"])
        self.assertEqual(
            {error["path"] for error in preview["errors"]},
            {
                "report_context.surprise",
                "report_context.schematic_path",
                "report_context.schematic_source_path",
            },
        )

    def test_list_netlist_files_finds_cir_and_net_and_skips_symlinks_and_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runs").mkdir()
            (root / "runs" / "hidden.cir").write_text(".end\n")
            project = root / "sensor-front-end"
            project.mkdir()
            (project / "sensor-front-end.cir").write_text(".end\n")
            (project / "legacy.net").write_text(".end\n")
            (project / "notes.txt").write_text("not a netlist")
            (root / "linked.cir").symlink_to(project / "sensor-front-end.cir")

            files = study_recipe.list_netlist_files(root)

        self.assertEqual(
            set(files),
            {"sensor-front-end/sensor-front-end.cir", "sensor-front-end/legacy.net"},
        )

    def test_read_and_write_netlist_text_round_trip_and_stay_confined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "amp"
            project.mkdir()
            (project / "amp.cir").write_bytes(
                b"\xff\xfe" + "R1 in out 4700\n.end\n".encode("utf-16-le")
            )
            (root / "outside.cir").write_text("R1 in out 1\n.end\n")

            original = study_recipe.read_netlist_text(root, "amp/amp.cir")
            self.assertEqual(original, "R1 in out 4700\n.end\n")

            study_recipe.write_netlist_text(root, "amp/amp.cir", "R1 in out {R_VAL}\n.end\n")
            self.assertEqual(
                (project / "amp.cir").read_text(encoding="utf-8"),
                "R1 in out {R_VAL}\n.end\n",
            )
            self.assertEqual(
                study_recipe.read_netlist_text(root, "amp/amp.cir"),
                "R1 in out {R_VAL}\n.end\n",
            )

            with self.assertRaises(ValueError):
                study_recipe.read_netlist_text(root, "../outside.cir")
            with self.assertRaises(ValueError):
                study_recipe.write_netlist_text(root, "../outside.cir", "anything")
            self.assertEqual((root / "outside.cir").read_text(), "R1 in out 1\n.end\n")

            with self.assertRaises(ValueError):
                study_recipe.write_netlist_text(root, "amp/amp.cir", "x" * (study_recipe.MAX_NETLIST_BYTES + 1))

    def test_create_netlist_file_imports_new_files_and_refuses_to_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Resolved up front: on macOS, tempfile's /var/... path is itself
            # a symlink to /private/var/..., and create_netlist_file()
            # correctly returns the resolved destination -- comparing against
            # an unresolved root would fail for that reason alone.
            root = Path(tmp).resolve()
            project = root / "amp"
            project.mkdir()

            created = study_recipe.create_netlist_file(
                root, "amp/imported.net", "R1 in out {R_VAL}\n.end\n"
            )

            self.assertEqual(created, project / "imported.net")
            self.assertEqual(
                created.read_text(encoding="utf-8"), "R1 in out {R_VAL}\n.end\n"
            )

            with self.assertRaises(ValueError):
                study_recipe.create_netlist_file(root, "amp/imported.net", "anything else")
            self.assertEqual(
                created.read_text(encoding="utf-8"), "R1 in out {R_VAL}\n.end\n"
            )

            with self.assertRaises(ValueError):
                study_recipe.create_netlist_file(root, "../escaped.cir", "anything")
            self.assertFalse((root.parent / "escaped.cir").exists())

            with self.assertRaises(ValueError):
                study_recipe.create_netlist_file(root, "amp/notes.txt", "not a netlist")

            with self.assertRaises(ValueError):
                study_recipe.create_netlist_file(root, "missing-folder/x.cir", "anything")


if __name__ == "__main__":
    unittest.main()
