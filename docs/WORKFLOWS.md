# Simulation and Analysis Workflows

These workflows use text netlists (`.cir`/`.net`) as the reproducible automation
boundary. Human-authored LTspice schematics (`.asc`) remain useful for viewing,
editing, and teaching, while their companion netlists define automated runs.

Run commands from the repository root with `PYTHONPATH=.` where shown.

## Run an existing netlist

```bash
python3 ltspice_wrapper.py path/to/Draft2.net
```

Use `--output-dir` when a stable output location is preferable:

```bash
python3 ltspice_wrapper.py examples/rc_lowpass.cir --output-dir runs/latest
```

An explicit output directory must not already exist; the wrapper creates it
atomically. This prevents concurrent callers from sharing a directory and an
older RAW or log file from being mistaken for evidence from the current
simulation. Resolvable relative `.include`, `.inc`, and `.lib` references are
rewritten in the staged deck so they retain their source-directory meaning.

The wrapper can attempt `.asc` schematics, but older or version-specific files
may require opening in LTspice and exporting or recreating a text netlist before
batch execution. See
[`LEARNINGS.md`](../LEARNINGS.md#schematic-compatibility) for verified schematic
compatibility guidance.

On macOS, validate the LTspice release itself before debugging the automation
layer. LTspice 26.0.2.1 failed during command-line startup on one Apple Silicon
Mac running macOS Tahoe, while the same wrapper and netlists passed with
LTspice 17.2.4. See
[`LEARNINGS.md`](../LEARNINGS.md#ltspice-26-on-macos-tahoe) for the symptoms and
recovery procedure.

For a text-readable raw file, especially for transient analysis:

```bash
python3 ltspice_wrapper.py examples/transient_rc.cir --ascii
```

Each run gets a timestamped directory beneath `runs/`. LTspice writes `.raw`
and `.log` files there, scalar `.meas` results are extracted, and
`run_manifest.json` records the command, paths, netlist hash, options, timing,
status, outputs, and SHA-256/size evidence.

## Parameter sweeps and history

The included sweep varies resistance, runs six independent LTspice jobs, and
writes machine-readable results:

```bash
PYTHONPATH=. python3 examples/sweep_rc.py
```

Each sweep is stored under `runs/sweep-<timestamp>/`, with one `.raw` and `.log`
pair per circuit plus `results.csv` and `results.sqlite3`. Every sweep is also
appended to `runs/history.sqlite3` for cross-run analysis.

Print the accumulated history with:

```bash
PYTHONPATH=. python3 examples/history_report.py
```

Native `.step` runs multiple parameter points in one LTspice invocation:

```bash
PYTHONPATH=. python3 examples/analyze_step_rc.py
```

The example detects step boundaries in the raw axis, parses the stepped
measurement table, and writes one CSV row per native step.

## Waveform analysis

The AC example parses the binary `.raw` file, exports all vectors to CSV,
creates a PNG frequency-response plot, and runs a pass/fail measurement check:

```bash
PYTHONPATH=. python3 examples/analyze_rc.py
```

The transient example exports its waveform, plots the output voltage, and checks
both `.meas` values and decoded raw-vector extrema:

```bash
PYTHONPATH=. python3 examples/analyze_transient.py
```

## Statistical yield

The statistical RC example uses the same production API exposed over MCP. It
creates an immutable 24-sample scrambled-Halton plan with bounded Gaussian R/C
populations, runs one LTspice simulation per paired sample, evaluates a gain
window, and generates statistics, worst-case, sensitivity, CSV/JSON evidence,
and the offline HTML report:

```bash
PYTHONPATH=. python3 examples/statistical_rc_yield.py
```

It does not maintain a second random sampler or private SQLite schema.

### Resource ceilings

The production path rejects oversized work before it can become trusted
evidence: at most 1,000 statistical samples or expanded experiment points, 32
variables, 4 workers, 32 waveform analyses, and 256 requirements are accepted.
Each LTspice process has a 3,600-second maximum timeout. RAW parsing is limited
to 256 MiB, log parsing to 64 MiB, and generated artifacts from one accepted run
to 512 MiB.

A successful process whose artifacts exceed that final ceiling is recorded as
failed before hashing or cache publication; cache entries over the same ceiling
fail integrity validation. The artifact ceiling is a post-process acceptance
guard, not an operating-system disk quota.

Offline reports embed at most 100 traces, 40,000 displayed waveform points, and
2,000 statistical analysis rows. Empirical CSV imports are separately limited
to 1 MB and 10,000 observations. These validation limits keep malformed or
oversized studies from preventing the rebuildable index and dashboard from
isolating and reporting other valid experiments.

## Validation circuits

### CMOS NAND

The NAND experiment drives a transistor-level 3.3 V CMOS NAND gate through all
four input combinations, compares its output against a behavioral reference,
and measures propagation delay:

```bash
PYTHONPATH=. python3 examples/analyze_nand.py
```

It writes waveform CSV, transient plots, measurements, and a manifest below
`runs/`. Generated artifacts are intentionally ignored by Git.

### Sallen-Key active filter

The Sallen-Key example runs both an AC sweep and a step response for a
second-order active low-pass filter (ideal op-amp modeled as a fixed-gain
E-source, K=2.5, Q=2), checks resonant peaking and overshoot against closed-form
predictions, and plots both domains:

```bash
PYTHONPATH=. python3 examples/analyze_sallen_key.py
```

The real LTspice schematics
[`sallen_key_lowpass.asc`](../examples/sallen_key_lowpass.asc) and
[`sallen_key_step.asc`](../examples/sallen_key_step.asc) can be opened and
edited graphically. Automated simulation still runs from the `.cir` netlist.

### Mixed-signal DAQ acquisition channel

The DAQ reference combines a two-pole 1 MHz-class anti-alias path, gain and
output loading, a clocked analog switch, hold capacitor, ADC input capacitance,
and leakage. One immutable correlated scrambled-Halton plan drives both AC and
transient studies across named light/heavy ADC-load corners:

```bash
PYTHONPATH=. python3 examples/mixed_signal_daq_study.py
# or
make mixed-signal-daq
```

The AC contract checks passband gain, cutoff, and peaking. The transient
contract checks front-end settling, full-resolution track error, and hold
droop. Each run emits yield, Wilson intervals, worst evidenced cases, global
sensitivity, and interactive offline HTML. Reports place the schematic,
plain-language circuit and simulation context, and plots first; complete
parameters and evidence remain in a collapsed appendix.

The companion `examples/mixed_signal_daq_boundary.cir` isolates track duration
so an adaptive study can resolve the real acquisition-time pass/fail boundary
without conflating other tolerances.

Open [`mixed_signal_daq.asc`](../examples/mixed_signal_daq.asc) in LTspice for
the human-facing circuit. It uses stock LTspice symbols, labeled functional
blocks and nets, nominal parameter values, and explanatory notes. Automation
runs the `.cir` templates, and a regression test keeps the schematic
component/value inventory aligned with the transient netlist.

## Target-response search

The design-search example uses a logarithmic binary search to select resistance
for a target gain, preserving every trial as CSV, SQLite, PNG, and HTML:

```bash
PYTHONPATH=. python3 examples/design_search_rc.py
```

## Optimization and finalist qualification

Run the bounded mixed-signal DAQ optimization as one durable AC/transient study:

```bash
PYTHONPATH=. .venv/bin/python examples/optimize_mixed_signal_daq_durable.py
```

It compares alias rejection against acquisition settling while enforcing
passband, bandwidth, peaking, tracking-error, and hold-droop constraints across
light and heavy ADC-load corners. The resulting coarse nominal selection is not
a manufacturing-yield proof.

Generate and run a bounded local Pareto-neighborhood refinement from a completed
optimization study:

```bash
PYTHONPATH=. .venv/bin/python examples/refine_mixed_signal_daq.py \
  <optimization-study-id>
```

Apply the same deterministic manufacturing population and ADC-load corners to
the coarse and refined finalists:

```bash
PYTHONPATH=. .venv/bin/python examples/qualify_mixed_signal_daq_finalists.py \
  <coarse-study-id> <refined-study-id>
```

The evaluator pairs each AC point with its matching transient point; a sample
passes only if both analyses pass. Its report compares nominal objectives and
joint corner yield, summarizes worst constraint margins and dominant rank
sensitivities, and retains detailed RAW, JSON, CSV, manifest, and analysis
links. The default example uses 32 scrambled-Halton samples per finalist at both
load corners. This is bounded engineering qualification evidence, not a
production-yield guarantee.

## Dashboard and artifact retention

Index all runs with manifests into static HTML and JSON:

```bash
python3 report_runs.py
```

Open `runs/index.html` to browse statuses, measurements, durations, and
generated artifacts.

Inventory or plan pruning without modifying artifacts:

```bash
python3 artifact_retention.py inspect
python3 artifact_retention.py prune --older-than-days 30 --keep-recent 10
```

`prune` is a dry run unless `--apply` is supplied. It manages only direct
children with valid terminal `run_manifest.json` or `experiment_manifest.json`
files and completed `runs/cache/simulation-*` entries. Recent entries are kept
separately for each class. Active jobs, malformed or unknown directories,
plans, studies, dashboards, and root-level files are never eligible.
Experiments referenced by retained optimization, robust-selection, adaptive,
or comparison evidence are protected.

Immediately before deletion, the tool rechecks every path boundary and manifest
digest; anything changed since planning stops the operation. Repeated `--scope
runs`, `--scope experiments`, or `--scope cache` options narrow cleanup. Review
the dry-run list before repeating the same command with `--apply`.

## Local REST API

Start the service in one terminal:

```bash
PYTHONPATH=. python3 api_server.py
```

Submit a netlist or asynchronous transient job from another terminal:

```bash
PYTHONPATH=. python3 examples/api_client.py
PYTHONPATH=. python3 examples/api_async_client.py
```

The service exposes `GET /health`, `GET /runs`, `GET /jobs`, `GET
/jobs/{job_id}`, `POST /simulate`, and `POST /simulate/async`. Simulation
accepts JSON containing `netlist`, optional `filename`, optional `ascii`, and
optional `timeout`. The asynchronous endpoint returns a job ID immediately.

It binds to numeric `127.0.0.1` by default. An authenticated network mode is
intentionally outside this local bridge.
