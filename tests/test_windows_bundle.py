from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import system_builder_windows

ROOT = Path(__file__).resolve().parents[1]


class WindowsBundleTests(unittest.TestCase):
    def test_default_workspace_starts_empty_without_copying_daq_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            with mock.patch.dict("os.environ", {"USERPROFILE": str(profile)}), \
                 mock.patch.object(
                     system_builder_windows, "_known_documents_folder", return_value=None
                 ):
                arguments = system_builder_windows.packaged_arguments(["--no-browser"])
                workspace = profile / "Documents" / system_builder_windows.WORKSPACE_NAME
                self.assertEqual(arguments[:2], ["--workspace", str(workspace)])
                self.assertEqual(list(workspace.iterdir()), [])
                protected = workspace / "user.cir"
                protected.write_text("user edit\n", encoding="utf-8")
                system_builder_windows.packaged_arguments([])
                self.assertEqual(protected.read_text(encoding="utf-8"), "user edit\n")

    def test_default_workspace_follows_redirected_documents_folder(self) -> None:
        redirected = Path("D:/OneDrive/Documents")
        with mock.patch.object(
            system_builder_windows, "_known_documents_folder", return_value=redirected
        ), mock.patch.dict("os.environ", {"USERPROFILE": "C:/Users/Dave"}):
            self.assertEqual(
                system_builder_windows.default_workspace(),
                redirected / system_builder_windows.WORKSPACE_NAME,
            )

    def test_known_documents_folder_queries_the_shell_on_windows(self) -> None:
        import ctypes

        calls: list[str] = []

        def get_known_folder_path(
            folder_id: object, flags: int, token: object, path_out: object
        ) -> int:
            guid = getattr(folder_id, "_obj")
            self.assertEqual(
                bytes(guid), system_builder_windows._FOLDERID_DOCUMENTS.bytes_le
            )
            getattr(path_out, "_obj").value = "D:\\OneDrive\\Documents"
            calls.append("get")
            return 0

        windll = mock.Mock()
        windll.shell32.SHGetKnownFolderPath.side_effect = get_known_folder_path
        with mock.patch.object(system_builder_windows.sys, "platform", "win32"), \
             mock.patch.object(ctypes, "windll", windll, create=True):
            folder = system_builder_windows._known_documents_folder()
        self.assertEqual(folder, Path("D:\\OneDrive\\Documents"))
        self.assertEqual(calls, ["get"])
        windll.ole32.CoTaskMemFree.assert_called_once()

    def test_known_documents_folder_falls_back_when_the_shell_fails(self) -> None:
        import ctypes

        windll = mock.Mock()
        windll.shell32.SHGetKnownFolderPath.return_value = -2147024894
        with mock.patch.object(system_builder_windows.sys, "platform", "win32"), \
             mock.patch.object(ctypes, "windll", windll, create=True):
            self.assertIsNone(system_builder_windows._known_documents_folder())
        with mock.patch.object(system_builder_windows.sys, "platform", "linux"):
            self.assertIsNone(system_builder_windows._known_documents_folder())

    def test_explicit_workspace_is_left_untouched(self) -> None:
        arguments = ["--workspace", "C:\\Circuits", "--no-browser"]
        with mock.patch.object(system_builder_windows, "default_workspace") as default:
            self.assertEqual(system_builder_windows.packaged_arguments(arguments), arguments)
        default.assert_not_called()

    def test_packaging_contract_builds_and_smokes_the_exact_archive(self) -> None:
        workflow = (
            ROOT / ".github/workflows/system-builder-windows-package.yml"
        ).read_text(encoding="utf-8")
        specification = (
            ROOT / "packaging/system_builder_windows.spec"
        ).read_text(encoding="utf-8")
        smoke = (
            ROOT / "tests/windows_system_builder_bundle_smoke.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("requirements-package.txt", workflow)
        self.assertIn("steps.bundle.outputs.archive", workflow)
        self.assertIn("windows_system_builder_bundle_smoke.ps1", workflow)
        self.assertIn("actions/upload-artifact@v6", workflow)
        self.assertIn("system_builder_static", specification)
        self.assertIn("mixed_signal_daq.ltstudy.json", specification)
        self.assertIn("mixed-signal-daq-schematic.png", specification)
        self.assertIn('Filter "python.exe"', smoke)
        self.assertIn("rc_lowpass_starter", specification)
        self.assertIn('Invoke-RestMethod -Uri ($url + "health")', smoke)

    def test_spec_bundles_every_starter_project_seeded_at_startup(self) -> None:
        import project_scaffold

        specification = ROOT / "packaging/system_builder_windows.spec"
        captured: dict[str, object] = {}

        def analysis(*_args: object, **kwargs: object) -> mock.Mock:
            captured.update(kwargs)
            return mock.Mock()

        namespace: dict[str, object] = {
            "SPECPATH": str(specification.parent),
            "Analysis": analysis,
            "PYZ": mock.Mock(),
            "EXE": mock.Mock(),
            "COLLECT": mock.Mock(),
        }
        exec(compile(specification.read_text(encoding="utf-8"), str(specification), "exec"), namespace)
        datas = captured["datas"]
        assert isinstance(datas, list)
        bundled = {Path(source).resolve(): destination for source, destination in datas}
        for name, source in project_scaffold.STARTER_PROJECTS.items():
            with self.subTest(starter=name):
                resolved = source.resolve()
                self.assertIn(resolved, bundled)
                self.assertTrue(resolved.is_dir())
                # The frozen layout must mirror the source tree so that
                # project_scaffold's examples-relative path still resolves.
                self.assertEqual(
                    bundled[resolved],
                    resolved.relative_to(ROOT).as_posix(),
                )
