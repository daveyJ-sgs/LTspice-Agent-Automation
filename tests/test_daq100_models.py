from __future__ import annotations

import unittest

from prepare_daq100_models import namespace_fda


class DaqModelCompatibilityTests(unittest.TestCase):
    def test_namespaces_definitions_and_calls_without_changing_equations(self):
        source = (
            b"* Copyright and VNSE comment remain intact\r\n"
            b"X1 a b vnse\r\nX2 c d femt\r\nX3 e f FEMT\r\n"
            b".SUBCKT VNSE 1 2\r\n.PARAM NVR=1.3\r\n.ENDS\r\n"
            b".SUBCKT FEMT 1 2\r\n.PARAM NVRF=2900\r\n.ENDS\r\n"
        )
        result = namespace_fda(source)
        self.assertEqual(result, (
            b"* Copyright and VNSE comment remain intact\r\n"
            b"X1 a b VNSE_LMH5401\r\nX2 c d FEMT_LMH5401\r\nX3 e f FEMT_LMH5401\r\n"
            b".SUBCKT VNSE_LMH5401 1 2\r\n.PARAM NVR=1.3\r\n.ENDS\r\n"
            b".SUBCKT FEMT_LMH5401 1 2\r\n.PARAM NVRF=2900\r\n.ENDS\r\n"
        ))

    def test_rejects_unexpected_model_structure(self):
        with self.assertRaises(ValueError):
            namespace_fda(b".SUBCKT VNSE 1 2\n.ENDS\n")
