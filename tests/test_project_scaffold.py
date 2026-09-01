from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import project_scaffold


class ProjectScaffoldTests(unittest.TestCase):
    def test_slugify_normalizes_and_rejects_bad_names(self) -> None:
        self.assertEqual(
            project_scaffold.slugify_project_name("Sensor Front-End v2"),
            "sensor-front-end-v2",
        )
        self.assertEqual(project_scaffold.slugify_project_name("  a b  "), "a-b")
        for bad in ("", "   ", "***", "runs", "examples", "x" * 81, 123, None):
            with self.assertRaises(ValueError):
                project_scaffold.slugify_project_name(bad)

    def test_list_projects_finds_recipes_and_skips_non_projects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runs").mkdir()
            (root / ".hidden").mkdir()
            (root / "no-recipe").mkdir()
            (root / "no-recipe" / "notes.txt").write_text("not a recipe")
            good = root / "good-project"
            good.mkdir()
            (good / "good-project.ltstudy.json").write_text(
                json.dumps({"name": "Good", "description": "d", "kind": "statistical"})
            )
            broken = root / "broken-project"
            broken.mkdir()
            (broken / "broken-project.ltstudy.json").write_text("not json")

            projects = project_scaffold.list_projects(root)

        slugs = {project["slug"] for project in projects}
        self.assertEqual(slugs, {"good-project", "broken-project"})
        good_summary = next(p for p in projects if p["slug"] == "good-project")
        self.assertEqual(good_summary["name"], "Good")
        self.assertEqual(good_summary["kind"], "statistical")
        self.assertTrue(good_summary["valid"])
        broken_summary = next(p for p in projects if p["slug"] == "broken-project")
        self.assertFalse(broken_summary["valid"])

    def test_list_projects_skips_symlinked_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real-project"
            real.mkdir()
            (real / "real-project.ltstudy.json").write_text(json.dumps({"name": "Real"}))
            (root / "linked").symlink_to(real, target_is_directory=True)

            projects = project_scaffold.list_projects(root)

        self.assertEqual([project["slug"] for project in projects], ["real-project"])

    def test_create_project_scaffolds_an_empty_shell_recipe_and_rejects_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            created = project_scaffold.create_project(root, "My New Filter")

            self.assertEqual(created["slug"], "my-new-filter")
            project_dir = root / "my-new-filter"
            recipe_path = project_dir / "my-new-filter.ltstudy.json"
            self.assertTrue(recipe_path.is_file())
            # No netlist is scaffolded -- bring your own via the netlist picker.
            self.assertEqual(list(project_dir.glob("*.cir")), [])
            self.assertEqual(list(project_dir.glob("*.net")), [])
            recipe = json.loads(recipe_path.read_text())
            self.assertEqual(recipe["name"], "My New Filter")
            self.assertEqual(recipe["experiments"][0]["netlist_path"], "")

            with self.assertRaises(project_scaffold.ProjectExistsError):
                project_scaffold.create_project(root, "my new filter")

    def test_create_project_rejects_reserved_and_invalid_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                project_scaffold.create_project(root, "runs")
            with self.assertRaises(ValueError):
                project_scaffold.create_project(root, "   ")

    def test_project_recipe_round_trips_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            created = project_scaffold.create_project(root, "Round Trip")
            loaded = project_scaffold.project_recipe(root, created["slug"])
            self.assertEqual(loaded["name"], "Round Trip")

            for bad_slug in ("..", ".", "a/b", "a\\b", "", "does-not-exist"):
                with self.assertRaises((ValueError, FileNotFoundError)):
                    project_scaffold.project_recipe(root, bad_slug)

    def test_delete_project_removes_the_directory_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            created = project_scaffold.create_project(root, "Delete Me")
            project_dir = root / created["slug"]
            self.assertTrue(project_dir.is_dir())

            project_scaffold.delete_project(root, created["slug"])

            self.assertFalse(project_dir.exists())
            with self.assertRaises(FileNotFoundError):
                project_scaffold.delete_project(root, created["slug"])

            (root / "runs").mkdir()
            with self.assertRaises(ValueError):
                project_scaffold.delete_project(root, "runs")
            self.assertTrue((root / "runs").is_dir())

            for bad_slug in ("..", ".", "a/b", "a\\b", ""):
                with self.assertRaises((ValueError, FileNotFoundError)):
                    project_scaffold.delete_project(root, bad_slug)

            outside = root.parent / "outside-sibling"
            outside.mkdir()
            try:
                escape_link = root / "escape"
                escape_link.symlink_to(outside, target_is_directory=True)
                with self.assertRaises(ValueError):
                    project_scaffold.delete_project(root, "escape")
                self.assertTrue(outside.is_dir())
            finally:
                shutil.rmtree(outside, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
