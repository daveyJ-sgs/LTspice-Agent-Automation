"""Real Windows LTspice schematic-capture qualification for the manual workflow."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image

from schematic_capture import capture_schematic


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("real LTspice schematic capture requires Windows")
    result = capture_schematic(PROJECT_ROOT, "examples/mixed_signal_daq.asc")
    image_path = PROJECT_ROOT / result["schematic_path"]
    with Image.open(image_path) as image:
        image.load()
        if image.size != (result["width"], result["height"]):
            raise RuntimeError("captured PNG dimensions do not match its metadata")
        if image.width < 800 or image.height < 500:
            raise RuntimeError("captured LTspice window is unexpectedly small")
        sample = image.convert("RGB")
        sample.thumbnail((1000, 1000))
        blue_pixels = sum(
            1
            for red, green, blue in sample.getdata()
            if blue > 100 and blue > red * 1.15 and blue > green * 1.15
        )
        if blue_pixels < 50:
            raise RuntimeError("captured window lacks recognizable LTspice schematic ink")
    summary = {**result, "blue_schematic_pixels": blue_pixels}
    summary_path = image_path.with_name("windows-capture-summary.json")
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
