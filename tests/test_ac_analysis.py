from __future__ import annotations

import cmath
import math
import unittest

from ac_analysis import gain_phase, unwrap_degrees
from raw_parser import RawData


def ac_data(
    frequency: list[float],
    out: list[complex],
    reference: list[complex],
    *,
    flags: str = "complex forward",
    step_count: int = 1,
) -> RawData:
    return RawData(
        flags=flags,
        variables=["frequency", "V(out)", "V(in)"],
        values={
            "frequency": [complex(value, 0.0) for value in frequency],
            "V(out)": list(out),
            "V(in)": list(reference),
        },
        step_count=step_count,
        points_per_step=len(frequency) // step_count if step_count else None,
    )


class GainPhaseTests(unittest.TestCase):
    def test_single_step_sweep_matches_hand_computed_points(self) -> None:
        data = ac_data(
            [10.0, 100.0, 1000.0],
            [complex(2.0, 0.0), cmath.rect(0.5, math.radians(-45.0)), complex(0.0, -1.0)],
            [complex(1.0, 0.0)] * 3,
        )

        [block] = gain_phase(data, "V(out)", "V(in)")

        self.assertEqual(block["frequency"], [10.0, 100.0, 1000.0])
        for measured, expected in zip(block["gain_db"], [6.0206, -6.0206, 0.0]):
            self.assertAlmostEqual(measured, expected, places=4)
        for measured, expected in zip(block["phase_deg"], [0.0, -45.0, -90.0]):
            self.assertAlmostEqual(measured, expected, places=9)

    def test_stepped_sweep_splits_on_the_frequency_axis_resets(self) -> None:
        data = ac_data(
            [10.0, 100.0, 10.0, 100.0],
            [complex(1.0, 0.0), complex(1.0, 0.0), complex(10.0, 0.0), complex(10.0, 0.0)],
            [complex(1.0, 0.0)] * 4,
            step_count=2,
        )

        blocks = gain_phase(data, "V(out)", "V(in)")

        self.assertEqual(len(blocks), 2)
        self.assertEqual([block["frequency"] for block in blocks], [[10.0, 100.0]] * 2)
        self.assertEqual([len(block["gain_db"]) for block in blocks], [2, 2])
        for measured, expected in zip([block["gain_db"][0] for block in blocks], [0.0, 20.0]):
            self.assertAlmostEqual(measured, expected, places=9)

    def test_transient_raw_file_is_rejected_rather_than_measured(self) -> None:
        data = RawData(
            flags="real forward",
            variables=["time", "V(out)", "V(in)"],
            values={"time": [0.0, 1.0], "V(out)": [0.0, 1.0], "V(in)": [1.0, 1.0]},
        )

        with self.assertRaisesRegex(ValueError, "Complex flag"):
            gain_phase(data, "V(out)", "V(in)")

    def test_variable_names_resolve_across_a_case_mismatch(self) -> None:
        data = ac_data([10.0], [complex(2.0, 0.0)], [complex(1.0, 0.0)])

        [block] = gain_phase(data, "v(OUT)", "V(In)")

        self.assertAlmostEqual(block["gain_db"][0], 6.0206, places=4)

    def test_missing_variable_names_the_available_vectors(self) -> None:
        data = ac_data([10.0], [complex(1.0, 0.0)], [complex(1.0, 0.0)])

        with self.assertRaisesRegex(ValueError, r"V\(mid\) is not in the RAW file"):
            gain_phase(data, "V(mid)", "V(in)")

    def test_degenerate_points_are_marked_instead_of_raising(self) -> None:
        data = ac_data(
            [10.0, 100.0, 1000.0],
            [complex(1.0, 0.0), complex(1.0, 0.0), complex(0.0, 0.0)],
            [complex(1.0, 0.0), complex(0.0, 0.0), complex(1.0, 0.0)],
        )

        [block] = gain_phase(data, "V(out)", "V(in)")

        self.assertAlmostEqual(block["gain_db"][0], 0.0, places=9)
        self.assertTrue(math.isnan(block["gain_db"][1]))
        self.assertTrue(math.isnan(block["phase_deg"][1]))
        self.assertEqual(block["gain_db"][2], -math.inf)

    def test_unwrapping_removes_the_wrap_in_a_multi_decade_phase_shift(self) -> None:
        phases = [-80.0, -170.0, 170.0, 100.0]
        data = ac_data(
            [10.0, 100.0, 1000.0, 10000.0],
            [cmath.rect(1.0, math.radians(phase)) for phase in phases],
            [complex(1.0, 0.0)] * 4,
        )

        [wrapped] = gain_phase(data, "V(out)", "V(in)")
        [unwrapped] = gain_phase(data, "V(out)", "V(in)", unwrap_phase=True)

        for measured, expected in zip(wrapped["phase_deg"], phases):
            self.assertAlmostEqual(measured, expected, places=9)
        for measured, expected in zip(unwrapped["phase_deg"], [-80.0, -170.0, -190.0, -260.0]):
            self.assertAlmostEqual(measured, expected, places=9)


class UnwrapDegreesTests(unittest.TestCase):
    def test_a_degenerate_point_does_not_strand_the_rest_of_the_sweep(self) -> None:
        unwrapped = unwrap_degrees([-170.0, math.nan, 170.0, 100.0])

        self.assertAlmostEqual(unwrapped[0], -170.0, places=9)
        self.assertTrue(math.isnan(unwrapped[1]))
        self.assertAlmostEqual(unwrapped[2], -190.0, places=9)
        self.assertAlmostEqual(unwrapped[3], -260.0, places=9)

    def test_an_exact_half_turn_step_is_unwrapped_instead_of_rejected(self) -> None:
        self.assertEqual(unwrap_degrees([0.0, 180.0, -180.0]), [0.0, 180.0, 180.0])


if __name__ == "__main__":
    unittest.main()
