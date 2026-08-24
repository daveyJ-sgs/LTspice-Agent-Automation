from __future__ import annotations

import math
import unittest

from waveform_metrics import evaluate_requirement, measure_metric


class WaveformMetricTests(unittest.TestCase):
    def test_statistical_metrics_and_evidence(self) -> None:
        axis = [0, 1, 2, 3, 4]
        values = [0, 1, 2, 3, 4]

        minimum = measure_metric(axis, values, "minimum", signal_unit="V")
        maximum = measure_metric(axis, values, "maximum", signal_unit="V")
        mean = measure_metric(axis, values, "mean", signal_unit="V")
        rms = measure_metric(axis, values, "rms", signal_unit="V")
        peak_to_peak = measure_metric(axis, values, "peak_to_peak", signal_unit="V")

        self.assertEqual((minimum.value, minimum.evidence["index"]), (0.0, 0))
        self.assertEqual((maximum.value, maximum.evidence["index"]), (4.0, 4))
        self.assertEqual(mean.value, 2.0)
        self.assertAlmostEqual(rms.value, math.sqrt(6.0))
        self.assertEqual(peak_to_peak.value, 4.0)
        self.assertEqual(peak_to_peak.unit, "V")

    def test_rise_time_uses_interpolated_crossings(self) -> None:
        result = measure_metric(
            list(range(8)),
            [0, 0, 1, 2, 3, 4, 5, 5],
            "rise_time",
            axis_unit="s",
            initial_value=0,
            final_value=5,
        )

        self.assertEqual(result.value, 4.0)
        self.assertEqual(result.evidence["low_axis"], 1.5)
        self.assertEqual(result.evidence["high_axis"], 5.5)
        self.assertEqual(result.unit, "s")

    def test_rise_time_handles_both_crossings_in_one_interval(self) -> None:
        result = measure_metric(
            [0, 1, 2],
            [0, 10, 10],
            "rise_time",
            initial_value=0,
            final_value=10,
        )

        self.assertAlmostEqual(result.value, 0.8)
        self.assertEqual(result.evidence["low_index_before"], 0)
        self.assertEqual(result.evidence["high_index_after"], 1)

    def test_overshoot_handles_rising_and_falling_steps(self) -> None:
        rising = measure_metric(
            [0, 1, 2, 3, 4, 5],
            [0, 2, 5, 6, 5.2, 5],
            "overshoot",
            initial_value=0,
            final_value=5,
        )
        falling = measure_metric(
            [0, 1, 2, 3, 4],
            [5, 3, 0, -0.5, 0],
            "overshoot",
            initial_value=5,
            final_value=0,
        )

        self.assertAlmostEqual(rising.value, 20.0)
        self.assertAlmostEqual(falling.value, 10.0)
        self.assertEqual(rising.evidence["index"], 3)

    def test_settling_time_requires_all_remaining_points_in_band(self) -> None:
        result = measure_metric(
            [0, 1, 2, 3, 4, 5],
            [0, 4, 5.5, 4.95, 5.02, 5],
            "settling_time",
            axis_unit="s",
            initial_value=0,
            final_value=5,
            settling_tolerance=0.02,
        )

        self.assertEqual(result.value, 3.0)
        self.assertEqual(result.evidence["index"], 3)
        with self.assertRaisesRegex(ValueError, "does not settle"):
            measure_metric(
                [0, 1, 2],
                [0, 5, 6],
                "settling_time",
                initial_value=0,
                final_value=5,
            )

    def test_requirement_result_is_structured(self) -> None:
        measurement = measure_metric([0, 1, 2], [0, 2, 4], "maximum", signal_unit="V")
        result = evaluate_requirement(measurement, "<=", 5.0)

        self.assertTrue(result["passed"])
        self.assertEqual(result["threshold"], {"operator": "<=", "target": 5.0, "unit": "V"})
        with self.assertRaisesRegex(ValueError, "operator"):
            evaluate_requirement(measurement, "=", 4.0)

    def test_rejects_ambiguous_or_invalid_vectors(self) -> None:
        with self.assertRaisesRegex(ValueError, "real-valued"):
            measure_metric([0, 1], [complex(1, 1), complex(2, 0)], "maximum")
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            measure_metric(
                [0, 1, 0],
                [0, 1, 2],
                "rise_time",
                initial_value=0,
                final_value=2,
            )
        with self.assertRaisesRegex(ValueError, "Unknown"):
            measure_metric([0, 1], [0, 1], "median")


if __name__ == "__main__":
    unittest.main()
