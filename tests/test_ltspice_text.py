from __future__ import annotations

import unittest

from ltspice_text import decode_text


class DecodeTextTests(unittest.TestCase):
    def test_utf8_and_utf16_are_decoded_and_bom_removed(self) -> None:
        text = "* 1µF\nC1 in 0 1u\n"
        self.assertEqual(decode_text(text.encode("utf-8")), text)
        self.assertEqual(decode_text(b"\xef\xbb\xbf" + text.encode("utf-8")), text)
        self.assertEqual(decode_text(b"\xff\xfe" + text.encode("utf-16-le")), text)
        self.assertEqual(decode_text(text.encode("utf-16-le")), text)

    def test_windows_ansi_netlist_falls_back_to_cp1252(self) -> None:
        text = "* 1µF – ±5% tolerance\nC1 in 0 1u\n"
        self.assertEqual(decode_text(text.encode("cp1252")), text)

    def test_bytes_undefined_in_cp1252_fall_back_to_latin1(self) -> None:
        self.assertEqual(decode_text(b"* \x81\xb5\n"), "* \x81µ\n")

    def test_bom_marked_utf8_stays_strict(self) -> None:
        with self.assertRaises(UnicodeDecodeError):
            decode_text(b"\xef\xbb\xbf* \xb5\n")

    def test_binary_content_is_still_rejected(self) -> None:
        with self.assertRaises(UnicodeDecodeError):
            decode_text(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff")


if __name__ == "__main__":
    unittest.main()
