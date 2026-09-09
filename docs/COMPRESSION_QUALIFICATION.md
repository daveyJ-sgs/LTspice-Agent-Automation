# Uncompressed numerical qualification — September 9, 2026

The first real Windows qualification after the audit failed its exact optimizer
comparison: macOS selected candidate 3 with nine Pareto candidates, while Windows
selected candidate 2 with eleven. All objective differences were within the
existing 0.05 dB / 50 ns tolerances. The failing run was
[34302457591](https://github.com/daveyJ-sgs/LTspice-Agent-Automation/actions/runs/34302457591).

## Cause and controlled reproduction

The macOS LTspice 17.2.4 transient RAW files contained 106–124 saved points;
Windows LTspice 26.0.2 files contained 1,031–1,032. Around the settling threshold,
macOS samples could be 28 ns apart despite the deck's 2 ns maximum simulation
step. Waveform compression removes saved points after the simulation.

The settling metric conservatively returns the first saved point inside the
band after the last excursion. Compression therefore changes this estimate.
Exact no-worse Pareto comparisons amplify small changes in candidate ordering;
the later score tolerance cannot restore a candidate already removed from the
frontier. The earlier fix preventing dominance cycles remains necessary.

Replaying the saved objective vectors on one machine reproduced both decisions.
Replacing only macOS settling values with Windows values reproduced the Windows
frontier; replacing AC values did not. Disabling compression in 32 fresh macOS
transient simulations restored the full Windows frontier and candidate 2.
The maximum point-level timing difference fell from 25.713 ns to 1.310 ns.
An offline comparison passed with zero exact and objective mismatches.
Interpolating the existing compressed threshold crossings also restored agreement,
but changing the metric's defined semantics was unnecessary for this correction.

## Correction

- The Python wrapper accepts `disable_compression=True`; its CLI exposes
  `--disable-compression`. It adds `.options plotwinsize=0` immediately after
  the staged deck's title, ahead of existing options and includes. A real
  simulator probe confirmed that LTspice uses the first setting: appending the
  override retained 115 compressed points, while prepending it saved 1,046.
- MCP simulation entry points always enable this option. Optimization,
  statistical qualification, native batches, and System Builder use those paths.
- Source netlists remain unchanged. The manifest records the requested option,
  and the staged netlist hash separates compressed and uncompressed cache entries.
- Engine version 3 rejects recovery of unfinished version-1/2 jobs. Historical
  completed evidence remains readable without relabeling or recomputation.
- The two macOS baseline fixtures were regenerated from complete real studies.
  Comparison tolerances and exact Pareto/selection acceptance were not relaxed.

## Validation

Local Python 3.14.3 / macOS LTspice 17.2.4 validation: 423 tests, configured Ruff
and mypy checks passed. Regressions cover UTF-16 input, existing option placement,
source preservation, cache separation/reuse, MCP policy, and old-job rejection.

Fresh macOS coarse optimization completed 64 runs, selected candidate 2, and
retained eleven Pareto candidates. Refinement selected candidate 1. The 256-run
paired finalist qualification selected `coarse-winner`; its refreshed baseline
records the actual new finalist parameters and statistical evidence.

Local refresh evidence is under
`/private/tmp/ltspice-compression-final-20260909`.
The controlled investigation is under
`/private/tmp/ltspice-discrepancy-uncompressed`.
These are temporary local evidence directories, not committed run data.

Remote acceptance is checked by the automatic macOS/Windows Tests matrix and
manual Real LTspice Windows qualification. Final run results are recorded below
once completed. Packaged executable builds are outside this qualification.
