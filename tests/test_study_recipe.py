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
        }
        preview = study_recipe.preview_study_recipe(recipe, PROJECT_ROOT)

        self.assertFalse(preview["valid"])
        self.assertEqual(
            {error["path"] for error in preview["errors"]},
            {"report_context.surprise", "report_context.schematic_path"},
        )


if __name__ == "__main__":
    unittest.main()
