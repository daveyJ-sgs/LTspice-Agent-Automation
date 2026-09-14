# Requirement Parameters (e.g. `frequency_value`) — GUI Implementation Notes

## Context

While reviewing why `ac_gain_db` requirements "just worked" without an obvious
place where `frequency_value` gets set, traced it through `mcp_server.py` and
`frequency_domain_metrics.py`. This documents how metric parameters actually
flow, so the GUI can expose the right fields per metric.

## How it actually works today

`frequency_value` (and other metric parameters) are **not set separately** —
they're just additional flat keys sitting directly inside the requirement
object, alongside `metric`/`operator`/`target`:

```json
{
  "metric": "ac_gain_db",
  "operator": ">=",
  "target": 39,
  "frequency_value": 1000
}
```

`mcp_server.py` collects any key matching a fixed, known parameter-name set
(`all_numeric_parameters` / `all_string_parameters`) if present in the
requirement dict, filters that down to the parameters of the metric's *domain*
(frequency-domain vs time-domain), and passes the rest as kwargs into
`measure_metric(...)`.

**Correction (resolved):** an earlier draft of this note said the filter was
per-metric, using the selected metric's `_METRIC_REGISTRY` entry. It was not.
`_METRIC_REGISTRY`'s per-metric `parameters` frozensets were dead data, read
only by their own round-trip tests, so `measure_metric` accepted any
domain-level parameter and silently ignored the ones the handler did not read
-- which is why a misspelled `frequency` instead of `frequency_value` failed as
"frequency_value is required" rather than as an unknown field. Those frozensets
are now the source of the per-metric schema (`waveform_metrics.metric_parameters`,
`frequency_domain_metrics.metric_parameters`, merged by
`experiment_engine.metric_schema`), which the recipe validator and the GUI both
read.

**Key implication for the GUI:** parameters must be flat sibling keys inside
the requirement object — not nested under something like `"metric_parameters"`.
(That nested shape exists elsewhere — the `.ltopt` optimization-goal schema —
but it is a *different* schema from `.ltstudy` requirements. Don't conflate
the two when building GUI forms/serializers.)

## Per-metric accepted parameters (from `_METRIC_REGISTRY`)

Every frequency-domain metric also accepts `window_start`/`window_end`, which
bound the analysis to part of the swept axis.

| Metric | Accepted parameters |
| --- | --- |
| `frequency` | `threshold_value` (**required**), `edge` |
| `spectral_peak` | `frequency_min` (**required**), `frequency_max` (**required**), `frequency_resolution` |
| `thd` | `fundamental_frequency` (**required**), `maximum_harmonic` |
| `ac_gain_db` | `frequency_value` (**required**) |
| `cutoff_frequency` | `reference_frequency` (**required**), `cutoff_drop_db`, `direction` |
| `peaking_db` | `reference_frequency` (**required**) |
| `gain_crossover_frequency` | none |
| `phase_margin` | none |
| `gain_margin` | none |

**Correction (resolved):** an earlier draft of this table listed only the six
gain/phase metrics and marked only `frequency_value` as required. `frequency`,
`spectral_peak` and `thd` are frequency-domain metrics too, and
`reference_frequency` is equally required -- `_reference_gain` puts it through
the same `_finite(...)` guard that raises "`<name>` is required", so
`cutoff_frequency` and `peaking_db` fail at measurement time without it exactly
as `ac_gain_db` does. The draft's suggested GUI rule that "other metrics have
no strictly-required numeric parameter" would therefore have left four metrics
able to fail mid-run.

`secondary_variable` is not a requirement-level parameter at all: it is the
analysis-level field, reaching the metric as the `secondary_values` vector. It
is listed in the registry frozensets and is deliberately excluded from the
requirement schema.

## `ac_gain_db` specifics — what to expose in the GUI

- **`frequency_value` is required.** If omitted, the backend raises
  `ValueError: frequency_value is required` at measurement time — this
  should be a required field in the form, not optional, to avoid a
  late/confusing failure.
- **Must fall within the experiment's actual `.AC` sweep range.** The
  measurement interpolates in log-frequency space (`_interpolate_log`) and
  raises `ValueError: requested frequency is outside the analysis window` if
  `frequency_value` is outside `[fstart, fstop]` of that experiment's `.AC`
  directive. GUI should ideally show the experiment's swept range next to
  the field, or validate against it client-side, so users get a clear error
  before a run rather than a failed measurement after.
- **Value is log-interpolated, not snapped to the nearest simulated point.**
  If `frequency_value` doesn't land exactly on a simulated `.AC` point (very
  common with `dec`/`oct` spacing), the engine linearly interpolates in log
  space between the two bracketing points. Worth a short tooltip in the GUI
  so users understand this isn't "nearest simulated point" behavior.
- Same interpolation + range-check behavior applies to `reference_frequency`
  on `cutoff_frequency` and `peaking_db` — the numeric input for those should
  get the same range validation/tooltip treatment.

## Suggested GUI behavior

1. When a user selects a metric from the dropdown, dynamically show only its
   accepted parameter fields (per the table above) — nothing nested, no
   generic nested "parameters" blob.
2. For any frequency-valued field (`frequency_value`, `reference_frequency`),
   pull the parent experiment's `.AC` sweep range and either display it as
   help text or validate the entered value against it before submit.
3. Mark every required parameter for the selected metric — `frequency_value`
   for `ac_gain_db`, `reference_frequency` for `cutoff_frequency` and
   `peaking_db`, `threshold_value` for `frequency`, `frequency_min`/
   `frequency_max` for `spectral_peak`, `fundamental_frequency` for `thd`.

## Implemented

All three, plus the pieces the investigation turned up along the way:

- **One schema, read by everything.** `waveform_metrics.MetricParameter`
  describes a requirement parameter (kind, required, choices, default, unit,
  and whether it is log-interpolated onto the analysis axis).
  `waveform_metrics.metric_parameters` and
  `frequency_domain_metrics.metric_parameters` build those from the
  `_METRIC_REGISTRY` frozensets, and `experiment_engine.metric_schema` merges
  both domains. A registry parameter with no description fails its module's
  test, so the schema cannot drift from the metrics.
- **Failing early instead of mid-run.** `experiment_engine` now rejects a
  requirement that omits a required parameter or carries one the metric cannot
  use, so both surface as field-scoped preview errors rather than after LTspice
  has already been launched. A misspelled parameter used to be dropped in
  silence, which measured something other than what the author wrote.
- **The editor.** `GET /api/metrics` serves the schema; the requirement editor
  picks the metric from a grouped dropdown, renders that metric's parameters as
  flat sibling keys, drops keys the new metric cannot use when the metric
  changes, marks and flags required fields, and folds the shared
  `window_start`/`window_end` away. Frequency fields show the `.AC` sweep range
  parsed from the experiment's netlist, flag values outside it, state the
  log-interpolation behaviour, and accept SPICE suffixes (`50k`).
- **The optimization editor too.** `.ltopt` goals keep their nested
  `metric_parameters` shape — deliberately not conflated with these flat keys —
  but take their metric from the same list and have their argument names checked
  against it.
