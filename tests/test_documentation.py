from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = (
    ROOT / "README.md",
    ROOT / "ROADMAP.md",
    ROOT / "LEARNINGS.md",
    ROOT / "docs" / "MCP_REFERENCE.md",
    ROOT / "docs" / "SYSTEM_BUILDER.md",
    ROOT / "docs" / "WINDOWS.md",
    ROOT / "docs" / "WORKFLOWS.md",
)
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
HTML_LINK = re.compile(r"(?:href|src)=[\"']([^\"']+)[\"']")


class DocumentationTests(unittest.TestCase):
    def test_front_door_relative_links_resolve(self) -> None:
        missing: list[str] = []
        for document in DOCUMENTS:
            text = document.read_text(encoding="utf-8")
            targets = MARKDOWN_LINK.findall(text) + HTML_LINK.findall(text)
            for target in targets:
                parsed = urlsplit(target.strip().strip("<>"))
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                resolved = (document.parent / unquote(parsed.path)).resolve()
                if not resolved.exists():
                    missing.append(f"{document.relative_to(ROOT)} -> {target}")

        self.assertEqual(missing, [], "Broken documentation links:\n" + "\n".join(missing))
