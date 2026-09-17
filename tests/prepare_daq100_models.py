#!/usr/bin/env python3
"""Fetch pinned TI inputs and reproduce the existing DAQ project's adaptations."""
from __future__ import annotations

import hashlib
import io
import os
import re
import urllib.request
import zipfile
from pathlib import Path

MODEL_HASHES = {
    "OPA817_ltspice.lib": "ee7660aa53e837403f57794feb3c752baf69ca3ac8c6f7150d7e708787705379",
    "LMH5401.lib": "8502d521e24dd2cd25370aa9fe0deaab1c1f29f01c0917269a1566dd6d210400",
    "LMH6401_ltspice.lib": "603e4d92d5eb510b3cd724256603706d22830c8a67ed5f1aec2674084078aa98",
}


def fetch(url: str, expected: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"Source checksum changed: {url}")
    return data


def prepare(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    vga_zip = fetch("https://www.ti.com/lit/zip/SBOM938",
                    "a9358d223fc4baafa0be7619d83077d66b2221bc4bce57c0b2e52b12f6941897")
    fda_zip = fetch("https://www.ti.com/lit/zip/SBOM920",
                    "e5df09dabcc202a3247661f0c5ee1f4163c1c9c9d63c1ae6172b25081ce1d063")
    tsc = fetch("https://www.ti.com/lit/tsc/sbomcb7",
                "ea214813ef7ccfa616387d3bacfd199ae21d2ed6aba1b011a25667c9011f8511")
    vga = zipfile.ZipFile(io.BytesIO(vga_zip)).read("LMH6401.LIB").decode("cp1252")
    vga = vga.replace("\r\n", "\n")
    pattern = r"^G(R[AC])\s+(\d+) (\d+) VALUE = \{\s*V\(\2,\3\)/1e6\s*\}"
    vga, count = re.subn(pattern, r"R\1 \2 \3 1Meg noiseless", vga, flags=re.MULTILINE)
    if count != 4:
        raise ValueError(f"Expected four equivalent conductances, found {count}")
    vga = "* LTspice adaptation: four I=V/1Meg sources represented by noiseless 1Meg resistors.\n" + vga
    start = tsc.index(b"* OPA817 - Rev. A")
    last = b".ENDS  VOS_DRIFT_OPA817"
    stop = tsc.index(last, start) + len(last)
    records = tsc[start:stop].split(b"\0" * 5)
    lines = [records[0].decode("ascii")]
    for record in records[1:]:
        if record[0] != len(record[1:]) + 1:
            raise ValueError("OPA817 TINA record length mismatch")
        lines.append(record[1:].decode("ascii"))
    converted = []
    for line in lines:
        if line.startswith('.MODEL R_NOISELESS '):
            converted.append('* LTspice: R_NOISELESS resistors use the noiseless flag below.')
        elif line.startswith('R') and ' R_NOISELESS ' in line:
            converted.append(line.replace(' R_NOISELESS ', ' ') + ' noiseless')
        else:
            converted.append(line)
    models = {
        "OPA817_ltspice.lib": ("\n".join(converted) + "\n").encode(),
        "LMH5401.lib": zipfile.ZipFile(io.BytesIO(fda_zip)).read("LMH5401.lib"),
        "LMH6401_ltspice.lib": vga.encode("cp1252"),
    }
    for name, data in models.items():
        if hashlib.sha256(data).hexdigest() != MODEL_HASHES[name]:
            raise ValueError(f"Adapted model differs from original DAQ project: {name}")
        (destination / name).write_bytes(data)
        print(f"Verified {name}: {MODEL_HASHES[name]}", flush=True)


if __name__ == "__main__":
    prepare(Path(os.environ["REAL_LTSPICE_EVIDENCE_DIR"]) / "inputs" / "models")
