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

    def test_analysis_window_preserves_source_indices(self) -> None:
        result = measure_metric(
            [0, 1, 2, 3, 4, 5],
            [99, 1, 2, 4, 3, 100],
            "maximum",
            window_start=1,
            window_end=4,
        )

        self.assertEqual(result.value, 4.0)
        self.assertEqual(result.evidence, {"index": 3, "axis_value": 3.0})
        self.assertEqual(result.parameters, {"window_start": 1.0, "window_end": 4.0})
        with self.assertRaisesRegex(ValueError, "must not exceed"):
            measure_metric([0, 1], [0, 1], "maximum", window_start=1, window_end=0)
        with self.assertRaisesRegex(ValueError, "captured axis"):
            measure_metric(
                [0, 1], [0, 1], "maximum", window_start=2, window_end=3
            )

        duty = measure_metric(
            [0, 1, 2],
            [2, 2, 2],
            "duty_cycle",
            threshold_value=1,
            window_start=0.5,
            window_end=1.5,
        )
        rise = measure_metric(
            [0, 1, 2],
            [0, 2, 2],
            "rise_time",
            initial_value=0,
            final_value=2,
            window_start=0.05,
            window_end=1.5,
        )
        self.assertEqual(duty.value, 100.0)
        self.assertAlmostEqual(rise.value, 0.8)
        self.assertEqual(duty.evidence["start_index_before"], 0)
        self.assertEqual(duty.evidence["start_index_after"], 1)

    def test_fall_time_undershoot_and_monotonicity(self) -> None:
        fall = measure_metric(
            [0, 1, 2, 3, 4],
            [5, 5, 4, 1, 0],
            "fall_time",
            initial_value=5,
            final_value=0,
            axis_unit="s",
        )
        undershoot = measure_metric(
            [0, 1, 2],
            [-1, 0, 5],
            "undershoot",
            initial_value=0,
            final_value=5,
        )
        falling_undershoot = measure_metric(
            [0, 1, 2],
            [6, 5, 0],
            "undershoot",
            initial_value=5,
            final_value=0,
        )
        monotonicity = measure_metric(
            [0, 1, 2, 3],
            [0, 1, 0.8, 2],
            "monotonicity",
            initial_value=0,
            final_value=2,
            signal_unit="V",
        )

        self.assertEqual(fall.value, 2.0)
        self.assertEqual(undershoot.value, 20.0)
        self.assertEqual(falling_undershoot.value, 20.0)
        self.assertAlmostEqual(monotonicity.value, 0.2)
        self.assertEqual(monotonicity.evidence["start_index"], 1)
        equal_endpoints = measure_metric(
            [0, 1, 2], [0, 1, 0], "monotonicity", direction="rising"
        )
        self.assertEqual(equal_endpoints.value, 1.0)
        with self.assertRaisesRegex(ValueError, "fall_time requires"):
            measure_metric(
                [0, 1], [0, 1], "fall_time", initial_value=0, final_value=1
            )

    def test_pulse_width_and_duty_cycle_use_interpolated_time(self) -> None:
        axis = [0, 1, 2, 4, 5]
        values = [0, 0, 2, 2, 0]
        width = measure_metric(
            axis,
            values,
            "pulse_width",
            threshold_value=1,
            axis_unit="s",
        )
        duty = measure_metric(
            axis,
            values,
            "duty_cycle",
            threshold_value=1,
        )
        low_width = measure_metric(
            axis,
            [2, 2, 0, 0, 2],
            "pulse_width",
            threshold_value=1,
            polarity="low",
        )
        low_duty = measure_metric(
            axis,
            [2, 2, 0, 0, 2],
            "duty_cycle",
            threshold_value=1,
            polarity="low",
        )

        self.assertEqual(width.value, 3.0)
        self.assertEqual(width.evidence["entry_axis"], 1.5)
        self.assertEqual(width.evidence["exit_axis"], 4.5)
        self.assertEqual(duty.value, 60.0)
        self.assertEqual(low_width.value, 3.0)
        self.assertEqual(low_duty.value, 60.0)
        with self.assertRaisesRegex(ValueError, "complete pulse"):
            measure_metric(
                [0, 1, 2], [2, 2, 0], "pulse_width", threshold_value=1
            )

    def test_slew_rate_and_windowed_ripple(self) -> None:
        slew = measure_metric(
            [0, 2, 3], [0, 1, 5], "slew_rate", signal_unit="V", axis_unit="s"
        )
        ripple = measure_metric(
            [0, 1, 2, 3, 4],
            [20, 4.9, 5.1, 5.0, 30],
            "ripple",
            signal_unit="V",
            window_start=1,
            window_end=3,
        )

        self.assertEqual(slew.value, 4.0)
        self.assertEqual(slew.unit, "V/s")
        self.assertAlmostEqual(ripple.value, 0.2)
        self.assertEqual(ripple.evidence["minimum_index"], 1)

    def test_propagation_delay_uses_first_response_after_trigger(self) -> None:
        result = measure_metric(
            [0, 1, 3, 4],
            [0, 0, 2, 2],
            "propagation_delay",
            secondary_values=[2, 0, 2, 0],
            primary_threshold=1,
            secondary_threshold=1,
            primary_edge="rising",
            secondary_edge="falling",
            axis_unit="s",
        )

        self.assertEqual(result.value, 1.5)
        self.assertEqual(result.evidence["primary_axis"], 2.0)
        self.assertEqual(result.evidence["secondary_axis"], 3.5)
        with self.assertRaisesRegex(ValueError, "secondary waveform"):
            measure_metric(
                [0, 1],
                [0, 2],
                "propagation_delay",
                primary_threshold=1,
                secondary_threshold=1,
                primary_edge="rising",
                secondary_edge="rising",
            )

    def test_forbidden_region_samples_supports_signal_pairs(self) -> None:
        single = measure_metric(
            [0, 1, 2, 3],
            [0, 1, 2, 3],
            "forbidden_region_samples",
            forbidden_min=1,
            forbidden_max=2,
        )
        paired = measure_metric(
            [0, 1, 2, 3],
            [0, 1, 2, 3],
            "forbidden_region_samples",
            secondary_values=[0, 3, 2, 0],
            forbidden_min=1,
            forbidden_max=2,
            secondary_forbidden_min=2,
            secondary_forbidden_max=3,
        )

        self.assertEqual(single.value, 2.0)
        self.assertEqual(paired.value, 2.0)
        self.assertEqual(paired.evidence["first_index"], 1)
        with self.assertRaisesRegex(ValueError, "secondary_forbidden_max is required"):
            measure_metric(
                [0, 1],
                [0, 1],
                "forbidden_region_samples",
                secondary_values=[0, 1],
                forbidden_min=0,
                forbidden_max=1,
                secondary_forbidden_min=0,
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
        with self.assertRaisesRegex(ValueError, "polarity"):
            measure_metric(
                [0, 1, 2],
                [0, 2, 0],
                "pulse_width",
                threshold_value=1,
                polarity="middle",
            )
        with self.assertRaisesRegex(ValueError, "edge"):
            measure_metric(
                [0, 1],
                [0, 2],
                "propagation_delay",
                secondary_values=[0, 2],
                primary_threshold=1,
                secondary_threshold=1,
                primary_edge="up",
                secondary_edge="rising",
            )


if __name__ == "__main__":
    unittest.main()
