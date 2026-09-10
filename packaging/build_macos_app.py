#!/usr/bin/env python3
"""Build a native macOS launcher for this checkout (requires Xcode Command Line Tools)."""
import argparse
import plistlib
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path.home() / "Applications/LTspice System Builder.app")
    args = parser.parse_args()
    app = args.output.resolve()
    if app.exists():
        parser.error(f"Output already exists: {app}; choose a new --output location")
    contents = app / "Contents"
    binary = contents / "MacOS/LSB"
    resources = contents / "Resources"
    binary.parent.mkdir(parents=True)
    resources.mkdir()
    subprocess.run(["swiftc", str(ROOT / "packaging/macos/Launcher.swift"), "-o", str(binary)], check=True)
    with tempfile.TemporaryDirectory() as temporary:
        iconset = Path(temporary) / "LSB.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                name = f"icon_{size}x{size}" + ("@2x" if scale == 2 else "") + ".png"
                subprocess.run(["sips", "-z", str(size * scale), str(size * scale),
                                str(ROOT / "packaging/macos/LSB.png"), "--out", str(iconset / name)],
                               check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(resources / "LSB.icns")], check=True)
    info = {
        "CFBundleName": "LTspice System Builder", "CFBundleDisplayName": "LTspice System Builder",
        "CFBundleIdentifier": "com.ltspice-system-builder.launcher", "CFBundleVersion": "1",
        "CFBundleShortVersionString": "1.0", "CFBundleExecutable": "LSB",
        "CFBundlePackageType": "APPL", "CFBundleIconFile": "LSB.icns",
        "NSHighResolutionCapable": True, "LSBRepositoryPath": str(ROOT),
    }
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    subprocess.run(["codesign", "--force", "--sign", "-", str(app)], check=True)
    print(app)


if __name__ == "__main__":
    main()
