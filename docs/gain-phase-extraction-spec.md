# Gain/Phase Extraction Helper — Implementation Spec

## Context

`LTspice-Agent-Automation` already has a working binary `.raw` parser (`raw_parser.py`)
that correctly detects the `Complex` flag in AC-analysis raw files and parses
each point into a native Python `complex` value. There is currently **no
downstream helper** that turns that complex data into gain (dB) and phase
(degrees) — this needs to be added.

No new dependency is required (no `PyLTSpice`/`ltspice.py`/`spicelib`) — this
builds directly on the existing `RawData` dataclass.

## Existing data shape (`raw_parser.py`)

```python
@dataclass
class RawData:
    flags: str
    variables: list[str]
    values: dict[str, list[float | complex]]
    step_count: int = 1
    points_per_step: int | None = None
```

- `values[name]` is a list of `complex` per point when the raw file's `flags`
  contains `"complex"` (AC analysis).
- `step_slices(data)` already exists and returns a `list[slice]`, one per
  `.step` block, inferred from resets in the independent axis (frequency).
  Any new helper must respect this — i.e. work per-step-slice, not assume a
  single flat sweep.

## Goal

Add a function that takes two node/branch variable names (numerator,
denominator — e.g. `V(out)`, `V(out1)`) and a `RawData` instance, and returns
gain in dB and phase in degrees, split per step block.

## Proposed signature

```python
def gain_phase(
    data: RawData,
    numerator: str,
    denominator: str,
    unwrap_phase: bool = False,
) -> list[dict[str, list[float]]]:
    """Compute gain (dB) and phase (deg) of numerator/denominator per step.

    Returns one dict per step block, each containing:
        - "frequency": list[float]  (independent axis for that step)
        - "gain_db":   list[float]
        - "phase_deg": list[float]

    Raises ValueError if either variable is missing from data.values, or if
    the raw file has no Complex flag set (not an AC-analysis dataset).
    """
```

## Implementation notes

1. **Validate AC data.** Raise clearly if `"complex" not in data.flags.lower()`
   — calling this on a `.tran` raw file should fail loudly, not silently
   return zeros.
2. **Case-insensitive variable lookup.** LTspice raw files may store variable
   names in a different case than the netlist (`V(out)` vs `V(OUT)`) —
   match case-insensitively against `data.variables`/`data.values` keys
   rather than requiring an exact string match.
3. **Per-step split.** Use the existing `step_slices(data)` to get index
   ranges, then slice `values[numerator]`, `values[denominator]`, and the
   independent axis (`values[data.variables[0]]`) per step — do not compute
   over the flattened full-length lists when `step_count > 1`.
4. **Core math per point:**
   ```python
   ratio = num_val / den_val
   gain_db = 20 * math.log10(abs(ratio))
   phase_deg = math.degrees(cmath.phase(ratio))
   ```
   Guard divide-by-zero (`den_val == 0`) — skip or `nan` the point rather than
   raising, since a single degenerate point shouldn't kill the whole sweep.
5. **Optional phase unwrapping.** `unwrap_phase=True` should apply standard
   unwrapping (e.g. `numpy.unwrap` on the radian values before converting to
   degrees) so multi-decade sweeps with >180° total phase shift don't show
   discontinuous jumps — relevant for stability margin analysis.
6. **No plotting side effects.** This function returns data only. Any
   matplotlib/plot-pane rendering stays a separate concern (GUI layer or a
   separate `plotting.py`), so this stays unit-testable without display
   dependencies.

## Where it lives

Either:
- **Option A:** add as a function in `raw_parser.py` next to `step_slices`,
  since it depends directly on that helper and the `RawData` shape.
- **Option B:** new module `ac_analysis.py` that imports `RawData` and
  `step_slices` from `raw_parser.py`, keeping raw-file parsing and
  post-processing separated.

No strong preference — Option B is cleaner if more AC-specific post-processing
(margins, crossover frequency, etc.) is anticipated later.

## Suggested test coverage

- Single-step AC sweep: known synthetic `RawData` with hand-computed
  gain/phase at a few points.
- Multi-step (`.step` active) AC sweep: confirm per-step split matches
  `step_slices` boundaries and each step's output length is correct.
- Non-AC raw file (`.tran`, no `Complex` flag): confirm `ValueError` is raised.
- Case mismatch in variable name (`V(OUT)` vs `V(out)`): confirm lookup still
  resolves.
- Divide-by-zero point in denominator: confirm it doesn't raise and is
  handled per the guard chosen in step 4.
