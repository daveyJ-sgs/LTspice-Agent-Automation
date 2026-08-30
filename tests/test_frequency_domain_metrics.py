from __future__ import annotations

import cmath
import math
import unittest
from unittest.mock import patch

import frequency_domain_metrics
from frequency_domain_metrics import measure_metric


def response(gain_db: float, phase_degrees: float = 0.0) -> complex:
    return cmath.rect(10.0 ** (gain_db / 20.0), math.radians(phase_degrees))


class FrequencyDomainMetricTests(unittest.TestCase):
    def test_registry_is_complete_and_routes_metric_parameters(self) -> None:
        self.assertEqual(
            set(frequency_domain_metrics._METRIC_REGISTRY),
            frequency_domain_metrics.SUPPORTED_METRICS,
        )
        specification = frequency_domain_metrics._METRIC_REGISTRY["spectral_peak"]
        self.assertEqual(
            specification.parameters,
            frozenset({"frequency_min", "frequency_max", "frequency_resolution"}),
        )
        captured = []

        def handler(request):  # type: ignore[no-untyped-def]
            captured.append(request)
            return frequency_domain_metrics.waveform_metrics.MetricMeasurement(
                request.metric, 11.0, request.signal_unit, {}, {}
            )

        with patch.dict(
            frequency_domain_metrics._METRIC_REGISTRY,
            {
                "spectral_peak": frequency_domain_metrics._FrequencyMetricSpec(
                    handler, specification.parameters
                )
            },
        ):
            result = measure_metric(
                [0, 1],
                [0, 1],
                "spectral_peak",
                frequency_min=10,
                frequency_max=20,
                frequency_resolution=0.5,
                signal_unit="V",
            )

        self.assertEqual(result.value, 11.0)
        self.assertEqual(captured[0].frequency_min, 10)
        self.assertEqual(captured[0].frequency_max, 20)
        self.assertEqual(captured[0].frequency_resolution, 0.5)
        self.assertEqual(captured[0].signal_unit, "V")
        with self.assertRaisesRegex(ValueError, "Unknown frequency-domain metric"):
            measure_metric([0, 1], [0, 1], "median_frequency")

    def test_frequency_uses_interpolated_matching_edges(self) -> None:
        axis = [0, 0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2]
        values = [-1, 0, 1, 0, -1, 0, 1, 0, -1]
        rising = measure_metric(
            axis, values, "frequency", threshold_value=0, edge="rising", axis_unit="s"
        )
        falling = measure_metric(
            axis, values, "frequency", threshold_value=0, edge="falling", axis_unit="s"
        )

        self.assertEqual(rising.value, 1.0)
        self.assertEqual(falling.value, 1.0)
        self.assertEqual(rising.unit, "Hz")
        self.assertEqual(rising.evidence["cycle_count"], 1)
        with self.assertRaisesRegex(ValueError, "at least two"):
            measure_metric([0, 1, 2], [0, 1, 0], "frequency", threshold_value=0.5)

    def test_spectral_peak_uses_time_weighted_nonuniform_integration(self) -> None:
        uniform_axis = [index / 2000 for index in range(2001)]
        nonuniform_axis = [(index / 2000) ** 1.2 for index in range(2001)]

        def waveform(axis: list[float]) -> list[float]:
            return [
                math.sin(2 * math.pi * 50 * time)
                + 0.25 * math.sin(2 * math.pi * 120 * time)
                for time in axis
            ]

        results = [
            measure_metric(
                axis,
                waveform(axis),
                "spectral_peak",
                frequency_min=100,
                frequency_max=140,
                frequency_resolution=1,
                signal_unit="V",
                axis_unit="s",
            )
            for axis in (uniform_axis, nonuniform_axis)
        ]

        for result in results:
            self.assertAlmostEqual(result.value, 0.25, delta=0.002)
            self.assertEqual(result.evidence["peak_frequency"], 120.0)
            self.assertEqual(result.unit, "V")
        with self.assertRaisesRegex(ValueError, "Nyquist"):
            measure_metric(
                [0, 0.5, 1],
                [0, 1, 0],
                "spectral_peak",
                frequency_min=1,
                frequency_max=2,
            )
        with self.assertRaisesRegex(ValueError, "Nyquist"):
            measure_metric(
                [0, 0.001, 0.002],
                [0, 1, 0],
                "spectral_peak",
                frequency_min=1,
                frequency_max=900,
                frequency_resolution=1000,
            )

    def test_spectral_work_is_bounded(self) -> None:
        axis = [index / 20000 for index in range(5001)]
        with self.assertRaisesRegex(ValueError, "point-frequency operations"):
            measure_metric(
                axis,
                [0.0] * len(axis),
                "spectral_peak",
                frequency_min=1,
                frequency_max=1001,
                frequency_resolution=1,
            )

    def test_thd_uses_coherent_whole_cycles(self) -> None:
        axis = [index / 2000 for index in range(2001)]
        values = [
            2 * math.sin(2 * math.pi * 50 * time)
            + 0.2 * math.sin(2 * math.pi * 100 * time)
            + 0.1 * math.sin(2 * math.pi * 150 * time)
            for time in axis
        ]
        result = measure_metric(
            axis,
            values,
            "thd",
            fundamental_frequency=50,
            maximum_harmonic=5,
            axis_unit="s",
        )

        self.assertAlmostEqual(result.value, math.sqrt(0.2**2 + 0.1**2) / 2 * 100)
        self.assertAlmostEqual(result.evidence["harmonic_1_amplitude"], 2.0)
        self.assertEqual(result.evidence["cycle_count"], 50)
        with self.assertRaisesRegex(ValueError, "integer"):
            measure_metric(
                axis,
                values,
                "thd",
                fundamental_frequency=50,
                maximum_harmonic=True,
            )
        with self.assertRaisesRegex(ValueError, "2 through 100"):
            measure_metric(
                axis,
                values,
                "thd",
                fundamental_frequency=1,
                maximum_harmonic=101,
            )

    def test_ac_gain_cutoff_and_peaking_use_log_frequency(self) -> None:
        frequency = [10, 100, 1000, 10000]
        gain_db = [0, 3, 0, -10]
        reference = [complex(2, 0)] * len(frequency)
        primary = [2 * response(gain) for gain in gain_db]

        gain = measure_metric(
            frequency,
            primary,
            "ac_gain_db",
            secondary_values=reference,
            frequency_value=math.sqrt(1000),
            axis_unit="Hz",
        )
        cutoff = measure_metric(
            frequency,
            primary,
            "cutoff_frequency",
            secondary_values=reference,
            reference_frequency=10,
            axis_unit="Hz",
        )
        peaking = measure_metric(
            frequency,
            primary,
            "peaking_db",
            secondary_values=reference,
            reference_frequency=10,
        )

        self.assertAlmostEqual(gain.value, 1.5)
        self.assertAlmostEqual(cutoff.value, 2000.0)
        self.assertAlmostEqual(peaking.value, 3.0)
        self.assertEqual(cutoff.unit, "Hz")

    def test_gain_and_phase_margins_are_signed(self) -> None:
        frequency = [1, 10, 100, 1000]
        values = [
            response(gain, phase)
            for gain, phase in zip([20, 10, 0, -10], [-90, -120, -135, -200])
        ]

        crossover = measure_metric(
            frequency, values, "gain_crossover_frequency", axis_unit="Hz"
        )
        phase_margin = measure_metric(frequency, values, "phase_margin")
        gain_margin = measure_metric(frequency, values, "gain_margin")

        self.assertEqual(crossover.value, 100.0)
        self.assertEqual(phase_margin.value, 45.0)
        self.assertAlmostEqual(gain_margin.value, 6.923076923076923)
        self.assertEqual(gain_margin.unit, "dB")

    def test_margin_crossovers_include_the_window_start(self) -> None:
        frequency = [1, 10, 100]
        phase_margin = measure_metric(
            frequency,
            [response(10, -120), response(0, -135), response(-10, -160)],
            "phase_margin",
            window_start=10,
        )
        gain_margin = measure_metric(
            frequency,
            [response(10, -150), response(5, -180), response(-5, -210)],
            "gain_margin",
            window_start=10,
        )

        self.assertEqual(phase_margin.value, 45.0)
        self.assertEqual(phase_margin.evidence["frequency"], 10.0)
        self.assertEqual(gain_margin.value, -5.0)
        self.assertEqual(gain_margin.evidence["frequency"], 10.0)

    def test_ac_rejects_ambiguous_or_invalid_data(self) -> None:
        with self.assertRaisesRegex(ValueError, "zero reference"):
            measure_metric(
                [1, 10],
                [1, 1],
                "ac_gain_db",
                secondary_values=[1, 0],
                frequency_value=1,
            )
        gain = measure_metric(
            [1, 10],
            [response(0, 0), response(0, 180)],
            "ac_gain_db",
            frequency_value=1,
        )
        self.assertEqual(gain.value, 0.0)
        with self.assertRaisesRegex(ValueError, "180 degree"):
            measure_metric(
                [1, 10],
                [response(1, 0), response(-1, 180)],
                "phase_margin",
            )
        ambiguous = [response(gain, -120) for gain in [10, -1, 1, -1]]
        with self.assertRaisesRegex(ValueError, "multiple gain crossover"):
            measure_metric([1, 10, 100, 1000], ambiguous, "phase_margin")


if __name__ == "__main__":
    unittest.main()
