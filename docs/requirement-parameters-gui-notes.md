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
requirement dict, filters that down to whatever the selected metric's
registry entry (`_METRIC_REGISTRY` in `frequency_domain_metrics.py`) actually
accepts, and passes the rest as kwargs into `measure_metric(...)`.

**Key implication for the GUI:** parameters must be flat sibling keys inside
the requirement object — not nested under something like `"metric_parameters"`.
(That nested shape exists elsewhere — the `.ltopt` optimization-goal schema —
but it is a *different* schema from `.ltstudy` requirements. Don't conflate
the two when building GUI forms/serializers.)

## Per-metric accepted parameters (from `_METRIC_REGISTRY`)

| Metric | Accepted parameters |
| --- | --- |
| `ac_gain_db` | `frequency_value` (**required**), `secondary_variable` |
| `cutoff_frequency` | `reference_frequency`, `cutoff_drop_db`, `direction`, `secondary_variable` |
| `peaking_db` | `reference_frequency`, `secondary_variable` |
| `gain_crossover_frequency` | `secondary_variable` (no numeric field needed) |
| `phase_margin` | `secondary_variable` (no numeric field needed) |
| `gain_margin` | `secondary_variable` (no numeric field needed) |

`secondary_variable` here refers to the analysis-level `secondary_variable`
(e.g. `V(out1)`), not a requirement-level field — already exposed at the
analysis form level (see `differential_gain` example), not per-requirement.

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
3. Mark `frequency_value` as required specifically for `ac_gain_db` (other
   metrics have no strictly-required numeric parameter beyond what's listed).
