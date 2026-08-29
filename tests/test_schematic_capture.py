import json
import shutil
import tempfile
import unittest
from pathlib import Path

import schematic_capture


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_IMAGE = PROJECT_ROOT / "docs/images/mixed-signal-daq-schematic.png"


class SchematicCaptureTests(unittest.TestCase):
    def test_windows_title_check_accepts_ltspice_stem_only_titles(self) -> None:
        self.assertIn(
            "GetFileNameWithoutExtension($Source)",
            schematic_capture._WINDOWS_CAPTURE_SCRIPT,
        )
        self.assertIn("GetDpiForWindow", schematic_capture._WINDOWS_CAPTURE_SCRIPT)
        self.assertIn("ShowWindow($process.MainWindowHandle, 3)", schematic_capture._WINDOWS_CAPTURE_SCRIPT)
        self.assertIn("UIAutomationClient", schematic_capture._WINDOWS_CAPTURE_SCRIPT)
        self.assertIn("LTspice Tool Change Log", schematic_capture._WINDOWS_CAPTURE_SCRIPT)

    def test_captures_content_addressed_asset_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source = workspace / "examples/circuit.asc"
            source.parent.mkdir()
            source.write_text("Version 4\nSHEET 1 880 680\n", encoding="utf-8")

            def capture(_source: Path, output: Path, _executable: Path) -> str:
                shutil.copyfile(REFERENCE_IMAGE, output)
                return "test-native-window"

            result = schematic_capture.capture_schematic(
                workspace,
                "examples/circuit.asc",
                executable=Path("/test/LTspice"),
                native_capture=capture,
            )

            image = workspace / result["schematic_path"]
            metadata = image.with_suffix(".json")
            self.assertTrue(image.is_file())
            self.assertTrue(metadata.is_file())
            self.assertEqual(result["source_path"], "examples/circuit.asc")
            self.assertEqual(result["capture_method"], "test-native-window")
            self.assertGreaterEqual(result["width"], 320)
            self.assertEqual(
                json.loads(metadata.read_text(encoding="utf-8"))["source_sha256"],
                result["source_sha256"],
            )

    def test_sources_and_images_are_confined_and_symlinks_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            outside = workspace.parent / "outside.asc"
            outside.write_text("Version 4\n", encoding="utf-8")
            (workspace / "escape.asc").symlink_to(outside)
            (workspace / "wrong.txt").write_text("not a schematic", encoding="utf-8")

            for path in ("../outside.asc", "escape.asc", "wrong.txt"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    schematic_capture.capture_schematic(
                        workspace,
                        path,
                        native_capture=lambda *_args: "unused",
                    )

            with self.assertRaises(ValueError):
                schematic_capture.resolve_schematic_image(workspace, "../outside.png")
            outside.unlink()

    def test_file_listing_is_bounded_and_includes_managed_captures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "examples").mkdir()
            (workspace / "examples/design.asc").write_text("Version 4\n", encoding="utf-8")
            shutil.copyfile(REFERENCE_IMAGE, workspace / "examples/reference.png")
            assets = workspace / "runs/system-builder-assets"
            assets.mkdir(parents=True)
            shutil.copyfile(REFERENCE_IMAGE, assets / "captured.png")
            ignored = workspace / "runs/job"
            ignored.mkdir()
            shutil.copyfile(REFERENCE_IMAGE, ignored / "waveform.png")

            result = schematic_capture.list_schematic_files(workspace)

            self.assertEqual(result["sources"], ["examples/design.asc"])
            self.assertEqual(
                result["images"],
                [
                    "examples/reference.png",
                    "runs/system-builder-assets/captured.png",
                ],
            )


if __name__ == "__main__":
    unittest.main()
