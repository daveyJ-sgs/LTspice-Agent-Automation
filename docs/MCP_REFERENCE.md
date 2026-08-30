# MCP experiment and analysis reference

This reference contains the detailed MCP workflows, contracts, and analysis
metrics for LTspice Agent Automation. Start with the [project README](../README.md)
for installation, orientation, and the shortest runnable examples. See
[ROADMAP.md](../ROADMAP.md) for the development sequence and current phase.

The MCP server keeps LTspice as the numerical simulation engine while adding
deterministic experiment definitions, structured evidence, statistical and
optimization studies, durable orchestration, and portable human-facing reports.

## Tool inventory

`mcp_server.py` currently exposes:

- Simulation and waveform access: `run_netlist`, `run_netlist_file`,
  `get_measurements`, `get_waveform`, `export_waveform_csv`, `analyze_waveform`,
  and `run_parameter_sweep`
- Structured experiments: `run_experiment`, `define_experiment`,
  `start_experiment`, `get_experiment`, and `cancel_experiment`
- Statistical studies: `generate_statistical_plan`, `get_statistical_plan`,
  `run_statistical_experiment`, `define_statistical_study`,
  `summarize_statistical_experiment`, `analyze_statistical_worst_cases`,
  `analyze_statistical_sensitivity`, `define_local_sensitivity_study`,
  `analyze_local_sensitivity`, `define_adaptive_boundary_study`,
  `advance_adaptive_boundary_study`, and `get_adaptive_boundary_study`
- Optimization: `generate_optimization_plan`, `get_optimization_plan`,
  `run_optimization_experiment`, `evaluate_optimization_study`,
  `define_optimization_study`, `start_optimization_study`,
  `get_optimization_study`, `cancel_optimization_study`, and
  `compare_optimization_studies`, `generate_robust_selection_plan`,
  `get_robust_selection_plan`, `evaluate_robust_selection_study`, and
  `compare_robust_selection_studies`; selected-design qualification:
  `define_selected_qualification`, `start_selected_qualification`,
  `get_selected_qualification`, `cancel_selected_qualification`, and
  `resume_selected_qualification`
- Comparison and reporting: `compare_experiments`,
  `compare_statistical_experiments`, `build_experiment_index`,
  `query_experiments`, `query_studies`, `build_experiment_report`,
  `build_comparison_report`,
  `build_experiment_dashboard`, `list_runs`, `build_dashboard`, and
  `list_examples`

## Contents

- [Structured experiments](#run-a-structured-experiment)
- [Statistical planning and yield studies](#generate-a-deterministic-statistical-plan)
- [Durable orchestration and experiment comparison](#run-a-durable-experiment-job)
- [Deterministic coarse optimization](#run-a-deterministic-coarse-optimization)
- [Durable optimization studies](#run-a-durable-optimization-study)
- [Portable reports and experiment browsing](#build-a-portable-experiment-report)
- [Verified artifact reuse](#reuse-verified-simulation-artifacts)
- [Native LTspice stepping](#run-a-structured-experiment-as-one-native-ltspice-batch)
- [Waveform analysis and requirement metrics](#waveform-analysis-and-requirement-metrics)

## Run a structured experiment

Phase 2A adds synchronous, deterministic Cartesian experiments. Parameters are
ordered records; declaration order is significant, each value order is
preserved, and the first parameter changes slowest. Units are metadata only and
do not modify the rendered value. A parameter named `R` replaces every `{R}`
placeholder in the netlist template.

```json
{
  "netlist_template": "R1 in out {R}\nC1 out 0 {C}\n.ac dec 100 10 1Meg\n.end\n",
  "parameters": [
    {"name": "R", "values": ["1k", "2.2k"], "unit": "ohm"},
    {"name": "C", "values": ["10n", "22n"], "unit": "F"}
  ],
  "waveform_analyses": [
    {
      "name": "bandwidth",
      "variable": "V(out)",
      "requirements": [
        {"metric": "cutoff_frequency", "operator": ">=", "target": 1000,
         "reference_frequency": 10}
      ]
    }
  ]
}
```

Phase 2B-A adds optional derived parameters without evaluating Python or shell
expressions. Dependencies come from placeholders in each derived `template`,
are resolved in stable topological order, and do not increase the Cartesian
point count. Base parameters may be used only through a derived parameter:

```json
"derived_parameters": [
  {"name": "RC", "template": "({R})*({C})", "unit": "s"},
  {"name": "DOUBLE_RC", "template": "2*{RC}", "unit": "s"}
]
```

Forward references are allowed. Unknown dependencies, duplicate names, cycles,
and parameters that cannot affect the netlist are rejected before LTspice runs.
Returned point parameters and CSV columns contain resolved derived strings in
their original declaration order.

## Generate a deterministic statistical plan

Phase 3A separates sampling from simulation. Call `generate_statistical_plan`
with a seed and bounded variables to create an immutable, content-addressed
plan under `runs/statistical-plans/` without invoking LTspice:

```json
{
  "variables": [
    {"name": "R", "distribution": "gaussian", "minimum": 9000,
     "maximum": 11000, "nominal": 10000, "sigma": 500, "unit": "ohm"},
    {"name": "C", "distribution": "uniform", "minimum": 0.0000009,
     "maximum": 0.0000011, "nominal": 0.000001, "unit": "F"},
    {"name": "GAIN", "distribution": "discrete",
     "values": ["0.9", "1", "1.1"], "weights": [1, 3, 1], "nominal": "1"}
  ],
  "sample_count": 24,
  "seed": 20260824
}
```

The versioned SHA-256 counter generator gives each `(seed, sample, variable)`
an independent deterministic draw. Reordering unrelated variables or resuming
later cannot consume a different random stream. Uniform definitions retain the
Phase 3A-1 generator and artifact bytes. Mixed plans use the versioned Phase
3A-2 distribution generator.

Bounded Gaussian variables require `nominal`, positive `sigma`, `minimum`, and
`maximum`; the bounds must span at least 0.1 sigma. A fixed Decimal
Marsaglia-polar transform rejects out-of-bound draws with a hard 4,096-attempt
limit. Discrete variables require ordered unique string `values` and matching
positive finite `weights`. Weights are normalized canonically, and an exact
cumulative boundary selects the next bin. Plans are limited to 1,000 samples,
32 variables, and 10,000 generated values.

Phase 3C-1 adds optional correlated Gaussian groups. Each group names at least
two Gaussian variables and supplies a matching correlation matrix:

```json
"correlations": [
  {
    "variables": ["R1", "R2"],
    "matrix": [[1, 0.8], [0.8, 1]]
  }
]
```

Matrices must be finite, symmetric, positive semidefinite, have unit diagonal,
and contain coefficients from -1 through 1. A variable may belong to only one
group. Group variables and matrices are canonicalized by variable name, so an
equivalent reordered definition produces the same named samples. The versioned
Decimal Cholesky transform redraws the complete group whenever any bounded
component is rejected; it never clips individual values or weakens the declared
relationship. Existing uncorrelated Phase 3A plans and artifact hashes remain
unchanged.

Phase 3C-2 adds empirical variables for resampling observed component
populations. Supply either inline numeric observations:

```json
{"name": "R", "distribution": "empirical",
 "values": [9980, 10020, 10075, 9950], "unit": "ohm"}
```

or a UTF-8 CSV path confined to this project and a numeric column:

```json
{"name": "R", "distribution": "empirical",
 "csv_path": "examples/illustrative_component_population.csv",
 "column": "resistance_ohm", "unit": "ohm"}
```

Sampling is deterministic with replacement and preserves duplicate
observations, so repeated measured values retain their empirical frequency.
The immutable plan freezes the canonical observations along with source kind,
SHA-256, observation count, resampling method, and CSV path/column when
applicable. Editing or removing a CSV after plan generation cannot change a
saved study; regenerating from changed bytes produces different provenance and
a different content-addressed plan. CSV input is bounded to 1 MB, 10,000 rows,
and 256 columns. Missing, blank, nonnumeric, nonfinite, malformed, escaped, and
symlinked inputs fail before a plan is written. Units remain metadata and are
never converted implicitly. Empirical variables cannot be placed in the
Gaussian correlation groups described above.

The bundled CSV is an illustrative test population, not claimed laboratory
data. Replace it with measured lot characterization or another traceable source
when drawing engineering conclusions.

Phase 3C-3 crosses every statistical sample with finite named operating-corner
axes. Each axis controls one netlist placeholder and declares ordered named
values:

```json
"corner_axes": [
  {
    "name": "temperature",
    "parameter": "TEMP",
    "unit": "degC",
    "values": [
      {"name": "cold", "value": -40},
      {"name": "room", "value": 27},
      {"name": "hot", "value": 125}
    ]
  },
  {
    "name": "device_model",
    "parameter": "MODEL",
    "values": [
      {"name": "slow", "value": "DIODE_SLOW"},
      {"name": "fast", "value": "DIODE_FAST"}
    ]
  }
],
"corner_aggregate": false
```

Corner values are finite single SPICE tokens, so axes can select temperature,
supply, load, or a predeclared device-model name without injecting directives.
Axis and value declaration order defines a deterministic Cartesian corner set;
samples are the outer ordering and corners vary inside each sample. Axis
parameters must be unique and cannot collide with statistical variables. Plans
are limited to eight axes, sixteen values per axis, 1,000 expanded points, and
10,000 total parameter cells before any simulation is scheduled.

Every expanded point records its base sample ordinal and named corners. That
metadata is frozen into synchronous and durable experiment definitions and
survives checkpoint/resume. `statistics.json`, `statistics.csv`, the MCP
summary, and the offline report always expose each corner combination
separately. The overall yield is deliberately absent unless
`corner_aggregate` is explicitly `true`, in which case a clearly labeled
pooled yield is added without replacing the per-corner results.

Phase 3C-4 adds deterministic space-filling alternatives to independent random
sampling. Set `sampling_method` when generating a plan:

```json
{
  "variables": [
    {"name": "R", "distribution": "uniform", "minimum": 9000,
     "maximum": 11000, "nominal": 10000, "unit": "ohm"},
    {"name": "C", "distribution": "empirical",
     "values": [9.8e-9, 10e-9, 10.2e-9], "unit": "F"}
  ],
  "sample_count": 16,
  "seed": 20260824,
  "sampling_method": "latin_hypercube"
}
```

`latin_hypercube` uses every one-dimensional stratum exactly once, with a
seeded permutation and deterministic within-stratum jitter for each variable.
`halton` uses stable prime dimensions, deterministic digit scrambling, and a
seeded shift. Variable names determine their streams and Halton dimensions, so
reordering declarations does not redraw an existing named variable. Uniform
variables use the resulting fraction directly; weighted-discrete and empirical
variables use inverse-CDF selection. Named corners still expand after sampling
and therefore preserve the same sample-major evidence mapping.

Omitting `sampling_method`, or setting it to `independent`, preserves all prior
generator versions and artifact bytes. The versioned stratified generator is
`sha256-stratified-halton-v6`. Phase 3C-5A adds bounded and correlated Gaussian
support under `sha256-stratified-gaussian-v7`. Uncorrelated values map each
space-filling coordinate through a deterministic Decimal truncated-normal CDF.
Correlated groups stratify independent latent-normal coordinates before the
existing Decimal Cholesky transform. If a correlated vector exceeds any bound,
the whole vector is deterministically rejected and redrawn; individual values
are never clipped and the declared matrix is never altered. The Latin-hypercube
guarantee therefore applies exactly to uncorrelated truncated-Gaussian
probability strata and to the initial latent coordinates of correlated groups.
Native LTspice stepping is not used; each planned point keeps its own log, RAW
file, measurements, and requirement evidence.

Use `get_statistical_plan` to verify and inspect an existing plan. Pass its
`plan_id` plus an ordinary netlist template to `run_statistical_experiment`:

```json
{
  "plan_id": "statistical-plan-0123456789abcdef",
  "netlist_template": "* Statistical RC study\nR1 in out {R}\nC1 out 0 {C}\n.ac dec 100 10 1Meg\n.end\n"
}
```

Statistical values stay paired by sample ordinal: 24 R/C samples execute as 24
independent Phase 2 points, not a 24-by-24 Cartesian grid. The resulting
manifest records the source plan hash and remains compatible with the existing
results, CSV, index, comparison, and offline-report validators.

## Run a durable statistical yield study

Call `define_statistical_study` with a verified `plan_id`, netlist template,
waveform requirements, and optional `max_concurrency`. It freezes the plan's
ordered points inside the hashed durable experiment definition. Use the same
`start_experiment`, `get_experiment`, and `cancel_experiment` lifecycle as an
ordinary Phase 2 job. A restart resumes only missing point checkpoints and
never draws replacement samples or reloads the sampler.

After the job completes or is cancelled,
`summarize_statistical_experiment` writes bounded `statistics.json` and
`statistics.csv` artifacts. Statistics schema v2 carries the same validated
`sampling_provenance` through JSON, flat CSV provenance rows, and the MCP
summary: method, generator version, plan ID, plan SHA-256, durable-definition
SHA-256, and the runs-relative immutable plan path. Durable resume returns that
frozen provenance instead of reconstructing it from current sampler code.
Legacy studies without an explicit method are identified as independent
sampling.

Observed yield is electrical
passes divided by electrically evaluated samples. Simulation errors, waveform
analysis errors, and cancelled samples are reported separately and excluded
from that denominator; `planned_pass_fraction` also shows passes divided by
the full planned population. The summary includes a Wilson 95% binomial
interval, mean, sample standard deviation, 5th/50th/95th percentiles,
requirement-margin statistics, contributing point ordinals, and exact failed
sample evidence.

For completed jobs, `build_experiment_report` detects statistical studies
automatically and adds yield, confidence, error-accounting, and failed-sample
sections while linking the JSON and CSV evidence. Its Sampling provenance panel
names the method and generator, displays both frozen hashes, and links directly
to the immutable statistical-plan artifact. It also regenerates validated
worst-case and global-rank-sensitivity artifacts, presents bounded distribution
summaries, and links every analysis JSON/CSV file. Local OAT studies receive a
ranked tornado-data section with incomplete perturbations left explicit. These
tables are display views of the structured evidence, not alternate sources of
electrical truth. For transient studies on LTspice, request `ascii_raw=true`;
AC studies should retain binary RAW so complex waveforms are preserved.

The final Phase 3C verification combines these contracts in one reproducible
study: correlated bounded-Gaussian resistors, empirical capacitor observations,
scrambled Halton sampling, and three named load corners. A real eight-sample
Sallen-Key run completed all 24 analyses with no invalid points. The light and
nominal loads passed 8/8 independently, while the deliberately weak 1 kOhm load
failed 8/8 only on the 7.6 dB low-frequency gain floor (observed
7.5356–7.5357 dB); no aggregate yield was reported because pooling was not
requested.
A byte-stable 128-sample fixture separately verifies the requested 0.8
correlation, repeated-observation empirical frequencies, sample-major corner
ordering, immutable reload, and durable-resume provenance on macOS and Windows.

## Rank worst evidenced statistical cases

Call `analyze_statistical_worst_cases` with a terminal statistical experiment
ID. It performs no simulations and writes portable `worst_cases.json` and
`worst_cases.csv` artifacts from the already validated requirement results.
Each requirement is ranked independently by ascending signed margin, so values
with different metrics or units are never compared. Dense ranks preserve exact
ties, and a nominal top-25 sample bound expands only when needed to retain every
tie at the cutoff. Named corners are ranked by their worst observed margin for
that same requirement; declared corners with no evaluated evidence remain
visible with a null rank instead of disappearing.

Every sample row retains its point ordinal, base sample ordinal, resolved
parameters, named corners, measured value, signed margin, pass/fail result, and
runs-relative evidence path. Invalid, cancelled, and unfinished points are
counted but excluded from electrical rankings. This is an observed-evidence
ranking, not a prediction beyond the simulated population.
The canonical experiment validator rejects duplicate intrinsic requirement
identities, so ordinal copies of an otherwise identical check are not treated
as separate requirements.

## Rank global statistical sensitivity

Call `analyze_statistical_sensitivity` with a terminal statistical experiment
ID. It writes individually atomic `sensitivity.json` and `sensitivity.csv`
artifacts without rerunning LTspice. For every requirement and sampled variable,
it computes Spearman's rank correlation between the variable and signed
requirement margin. Average ranks preserve exact ties. Positive correlation
means increasing the variable tends to improve margin; negative correlation
means it tends to consume margin.

Named corners are analyzed independently and are never pooled implicitly. A
coefficient requires at least five evaluated samples and variation in both the
input and response. Otherwise the row explicitly reports
`insufficient_samples`, `constant_input`, `constant_response`, or
`non_numeric_input`. An absolute coefficient of at least 0.5 is labeled
meaningfully monotonic; lower values remain visible as weak or negligible
associations rather than being discarded.

These coefficients are descriptive associations, not causal effects or design
derivatives. The artifacts name correlated sampled inputs beside each result,
because two correlated components can both rank highly even when the study
cannot isolate their independent influence. Controlled local perturbations are
handled separately by Phase 3D-C.

## Run controlled local sensitivity studies

Call `define_local_sensitivity_study` with a completed statistical experiment,
one electrically evaluated point index, and a relative step. The tool freezes a
content-addressed plan containing the selected baseline and one low/high pair
for every nonzero numeric sampled variable. Named-corner parameters remain
fixed, while categorical and zero-baseline variables are retained as explicit
skip records. Start and monitor the returned experiment with the normal durable
`start_experiment` and `get_experiment` tools, including cancellation and
resume support.

After the study reaches a terminal state, `analyze_local_sensitivity` writes
individually atomic `tornado.json` and `tornado.csv` artifacts. Every
requirement-variable row contains the baseline, low, and high input values;
their units and signed requirement margins; low/high effects and slopes; impact
rank; and direct point-evidence paths. Missing perturbation results remain
visible as incomplete and are never ranked.

Tornado impact is the larger absolute margin change from the baseline. This is
a controlled local response around one evidenced point, not a population-wide
importance claim. Compare it with global rank sensitivity rather than using
one as a substitute for the other.

## Refine an observed pass/fail boundary

Call `define_adaptive_boundary_study` with two completed source points, one
requirement `check_id`, and the single numeric parameter to refine. The points
must differ only in that parameter and must have opposite electrical outcomes
for the selected requirement. This keeps the task to evidenced,
one-dimensional boundary characterization rather than circuit optimization.

Each `advance_adaptive_boundary_study` call either incorporates a completed
child batch or starts the next deterministic batch of evenly spaced interior
values. Poll the returned `active_experiment_id` with `get_experiment`, then
advance again when it is terminal. `get_adaptive_boundary_study` inspects the
parent without changing it. The content-addressed parent manifest records every
child experiment, input, signed margin, evidence path, bracket, width, shrink
ratio, and cumulative sample count, so a restart resumes at a batch boundary.

The study stops at its input tolerance, sample budget, or numeric resolution.
It fails closed if child evidence is incomplete or reveals more than one
pass/fail transition. Adaptively selected points do not update Wilson yield
confidence: they resolve a local boundary and are not an unbiased population
sample.

## Run a durable experiment job

The Phase 2B lifecycle keeps definition and execution separate. First call
`define_experiment` with the same definition accepted by `run_experiment`, plus
an optional `execution_mode` of `"independent"` or `"native"`. Independent mode
accepts `max_concurrency` from 1 through 4; native mode always uses one stepped
LTspice process. Definition validates and persists the experiment but does not
launch LTspice. Then call
`start_experiment` with the returned `experiment_id`, and poll
`get_experiment` for `finished_points`, `pending_points`, `running_points`, and
the existing pass/error counts.

Experiment definitions, progress, and terminal checkpoints are stored as atomic
UTF-8 JSON. A restarted MCP manager automatically requeues jobs that were queued
or running. Independent mode skips valid `point_result.json` checkpoints and
places interrupted points in fresh attempt directories. Native mode treats the
validated batch as one recovery unit: it atomically writes
`native-batch/batch_result.json` before materializing ordered point checkpoints.
If that batch checkpoint is absent after an interruption, the complete stepped
deck is retried in a fresh attempt; a valid checkpoint is recovered without
rerunning LTspice. Final JSON and CSV are always assembled in Cartesian order.

`cancel_experiment` is idempotent and cooperative. It immediately cancels a
defined or queued job. For a running job it prevents new points from being
scheduled; already-running LTspice processes are allowed to finish so behavior
does not depend on Unix signals and remains portable to Windows. If cancellation
arrives before waveform analysis, that analysis is skipped. For native mode,
the one in-flight LTspice process is also allowed to finish; any fully validated
batch evidence is checkpointed before the terminal experiment is marked
`cancelled`. Partial or completed simulation evidence therefore remains
available without platform-specific process signals.

The file-backed manager intentionally supports one active MCP process per
`runs/` directory. Experiments are coordinated FIFO, while points within the
active experiment use the declared concurrency bound. Multi-process locking is
outside Phase 2B.

The portable experiment implementation lives in `experiment_engine.py`. It
owns definition validation, parameter expansion, durable state, checkpoints,
and result assembly without importing MCP. `mcp_server.py` provides the thin
LTspice execution and waveform-analysis adapter plus the public tool wrappers.
This boundary lets other Python front ends reuse the engine without running an
MCP server.

## Compare completed experiments

Phase 2C-A adds `compare_experiments`, a read-only comparison of two completed
experiment IDs. It reads their existing `results.json` artifacts and never
runs LTspice. Points match only when their complete parameter maps match
exactly, including derived values: parameter key order is irrelevant, but
value text is case-sensitive and is not normalized (`1k` and `1000` differ).

The result reports matched, added, and removed points and measurements;
measurement deltas are `candidate - baseline`. Requirements match by analysis
name, metric, threshold, unit, and metric parameters. A passing baseline check
that fails in the candidate is a regression; the reverse is an improvement.
Added and removed checks remain explicit rather than being inferred.

Each comparison is content-addressed from both experiment IDs and both result
file hashes. Repeating an unchanged comparison returns the same ID and rewrites
the same deterministic UTF-8 artifacts under
`runs/comparisons/comparison-<id>/comparison.json` and `comparison.md`.
Malformed, unfinished, ambiguous, or non-finite inputs are rejected before an
output directory is created.

`compare_statistical_experiments` adds a statistical evidence contract. Both
studies must share the same normalized population, corner, parameter-unit, and
electrical-analysis definitions. A shared immutable plan enables exact
point-by-point classification transitions. Different compatible plans are
compared only as unpaired population summaries; if both the circuit and sample
plan change, attribution is explicitly labeled confounded.

The content-addressed comparison records circuit and plan hashes, aggregate and
per-corner yield/interval evidence, requirement-margin distribution deltas,
invalid counts, and paired classification transitions in JSON and CSV. Its
offline `report.html` links both source results. Missing electrical margins due
to simulation or analysis errors remain null rather than becoming fabricated
deltas. The comparison never reruns LTspice.

## Build and query the experiment index

Phase 2C-C1 adds a rebuildable SQLite catalog at
`runs/experiments.sqlite3`. Call `build_experiment_index` to scan existing
`experiment_manifest.json` and terminal `results.json` artifacts. The index
stores experiment summaries, declared parameters, materialized point values,
measurements, and requirement results. It never runs LTspice and is not an
authoritative result store; deleting and rebuilding it leaves the source
artifacts unchanged.

The builder supports both synchronous schema-v1 experiments and durable
schema-v2 jobs. Artifact paths stored in SQLite are relative to `runs/`, so a
copied experiment tree does not retain machine-specific macOS or Windows
paths. A malformed manifest is reported and skipped. A valid manifest with
invalid terminal results remains discoverable with `index_state` set to
`invalid_results`, but no untrusted point data is indexed. The complete staged
database is integrity-checked, closed, and atomically replaces the previous
index; a fatal build or replacement error leaves the prior database intact.

`query_experiments` supports deterministic pagination and optional exact
filters for status, execution mode, pass/fail state, parameter values, circuit
SHA-256, statistical-study status, aggregate yield or confidence floor, sampled
variable, requirement metric, and named-corner values.
Multiple parameter filters must occur together on the same materialized point;
values remain case-sensitive LTspice text, so `1k` and `1000` are distinct.
When yield or confidence is combined with corner filters, all requested corner
values and thresholds must match one corner result; an intentionally unpooled
study never acquires an invented aggregate. Query responses include circuit and
sampling metadata, aggregate and per-corner summaries, sampled variable names,
declared parameters, measurement names, and requirement metrics. Full point
data remains in the linked structured artifacts. The searchable dashboard uses
the same rebuilt index and shows the sampling method plus aggregate or
per-corner yield.

## Run a deterministic coarse optimization

Phase 4A adds a circuit-independent coarse optimizer without adding another
simulation runner. `generate_optimization_plan` freezes design domains, fixed
parameters, named corners, metric objectives, and hard constraints in a
content-addressed JSON plan. Supported domains are bounded continuous grids,
bounded integer ranges with an exact step, string-valued categorical choices,
explicit numeric preferred values, and bounded generated E6, E12, or E24
component values. Generated series include both bounds when they are members
of the requested series and fail before publication if the range contains
fewer than two or more than 64 values.
`run_optimization_experiment` sends that plan through the existing independent
experiment engine. Run each required analysis from the same plan, then call
`evaluate_optimization_study` with the completed experiment IDs.

The evaluator verifies every point and parameter map against the immutable
plan, uses the worst named-corner value for each objective and constraint,
keeps simulation/analysis errors separate from electrical failures, computes
the feasible Pareto front, and selects one candidate with the versioned
equal-weight normalized-regret policy. Deterministic JSON, CSV, and a portable
human-first Pareto report are written below `runs/optimization-studies/`.

Run the bounded mixed-signal DAQ qualification with:

```bash
PYTHONPATH=. .venv/bin/python examples/optimize_mixed_signal_daq.py
```

The first study evaluates 16 component/driver candidates at light and heavy
ADC-load corners in both AC and transient LTspice runs. Its competing
objectives are lower 10 MHz alias gain and faster acquisition settling;
passband gain, bandwidth, peaking, tracking error, and hold droop remain hard
constraints. A coarse nominal winner is not a manufacturing-yield proof:
Phase 4D reuses the Phase 3 corner and Monte Carlo machinery for finalists.

## Run a durable optimization study

Phase 4B composes the required AC and transient work through the existing
durable experiment manager. `define_optimization_study` accepts one immutable
optimization `plan_id` and an experiment-definition object whose names must
exactly match the plan. It creates one child experiment per named analysis but
does not start simulation. Use `start_optimization_study`, poll
`get_optimization_study`, and call `cancel_optimization_study` for cooperative
cancellation.

The optimization-job manifest stores only the plan ID and hash, child
experiment IDs and definition hashes, current statuses, and relative result
identity. It contains no machine-specific paths. Existing point checkpoints
remain owned by the Phase 2 experiment manager, so process restart queues only
unfinished work and completed points are not redrawn or renumbered. When every
child completes, the next status inspection publishes the normal deterministic
Pareto JSON, CSV, and HTML evidence.

Run the complete mixed-signal DAQ example with:

```bash
PYTHONPATH=. .venv/bin/python examples/optimize_mixed_signal_daq_durable.py
```

A one-finalist robust plan is a qualification contract rather than a design
comparison. Pass it to `define_selected_qualification` with exactly `ac` and
`transient` experiment definitions, then use the start/get/cancel/resume tools
as for other durable jobs. Once both children complete, status inspection
produces their statistical summaries, worst cases, sensitivities, HTML reports,
and the joint named-corner qualification evidence. The parent manifest keeps
only portable plan and child identities, so a restarted MCP server can recover
the job without launching or resuming it implicitly.

`compare_optimization_studies` enforces the cross-platform acceptance contract:
candidate parameters, classifications, Pareto membership, and selected design
must match exactly, while each objective must remain within its named absolute
plus relative tolerance. The comparison is itself content-addressed JSON and
offline HTML evidence. The DAQ qualification's versioned tolerance-aware
selection policy uses 0.05 dB absolute tolerance for 10 MHz alias gain and
50 ns for settling time; both relative tolerances are zero. Values within those
declared resolution limits cannot create a platform-specific dominance or
selection decision.

Phase 4C can derive a bounded local plan from a completed optimization study
with `optimization_engine.generate_optimization_refinement_plan`. It verifies
the parent study from its experiment evidence, accepts only feasible Pareto
parents, and forms each neighborhood from the current value plus adjacent
discrete choices or continuous interval midpoints. Candidates already present
in the parent or any available ancestor plan are removed before publication.

The child remains an ordinary immutable optimization plan, so the existing AC,
transient, durable-job, evaluation, and reporting paths consume it unchanged.
Its definition records the parent plan/study IDs and hashes, originating Pareto
candidate indices, refinement-policy version, and candidate/expanded-point
budgets. Budget overflow, duplicate or non-finite values, out-of-domain values,
tampered parent evidence, and neighborhoods with no new candidates fail before
simulation. `generate_optimization_refinement_plan` exposes the transformation
through MCP; its result includes the parent identities, originating candidate
indices, policy, and budgets without changing the older coarse-plan response
schema. The returned `plan_id` can be passed directly to
`define_optimization_study` or `run_optimization_experiment`.

Run the durable mixed-signal DAQ refinement with a completed parent study ID:

```bash
PYTHONPATH=. .venv/bin/python examples/refine_mixed_signal_daq.py \
  optimization-study-15a3b0b178405e19 --max-candidates 8 --max-points 16
```

The refinement HTML links to its parent and explicitly labels its selection as
child-plan-only. A new child candidate is not assumed to beat the parent; that
comparison remains an engineering conclusion and Phase 4D supplies the final
robust-selection proof.

## Prove optimization finalists under tolerance

`generate_robust_selection_plan` accepts two to eight feasible selected or
Pareto candidates from completed optimization studies. It reproduces each
source study before freezing its candidate parameters, source hashes, explicit
tie-break rank, and one deterministic statistical plan per finalist. Every
design-variable nominal must match the frozen candidate, preventing a tolerance
study from silently testing a different circuit.

Run each returned statistical plan through the ordinary AC and transient
experiment tools. Then call `evaluate_robust_selection_study` with an exact
`{finalist: {ac: experiment_id, transient: experiment_id}}` mapping. Point
parameters and ordinals must match the plan. A sample is a joint electrical
pass only when both experiments complete and every AC and transient requirement
passes. Named corners remain separate; selection maximizes the worst-corner
joint yield and uses the frozen source rank only for an exact statistical tie.

The content-addressed result includes per-corner Wilson intervals, worst signed
requirement margins, Phase 3 Spearman sensitivity summaries, the selection
rationale, and direct experiment/RAW/JSON/CSV/manifest links. `query_studies`
searches optimization and robust-selection studies by kind, selected result,
or source study. `compare_robust_selection_studies` requires identical portable
plans, candidate definitions, corner outcomes, and final selection while
checking worst requirement values against explicit per-metric tolerances.

The DAQ reference implementation is:

```bash
PYTHONPATH=. .venv/bin/python examples/qualify_mixed_signal_daq_finalists.py \
  optimization-study-15a3b0b178405e19 \
  optimization-study-322aec214a85b8df
```

## Build a portable experiment report

Call `build_experiment_report` with a completed experiment ID to write a
self-contained `report.html` beside that experiment's structured artifacts.
The report opens directly from disk without a web server or CDN. Its default
layout leads with a brief experiment explanation and interactive SVG waveform
overlays, summarizes parameter ranges in engineering units, and keeps complete
point, requirement, provenance, JSON, CSV, and RAW evidence in a collapsed
appendix. The waveform cursor uses a separate responsive inspector with
two-axis crosshairs and a selected-trace marker. Drag horizontally to zoom;
use Reset zoom or double-click the plot to restore its full domain. Callers may
supply bounded `report_context` text plus a repository-
local PNG/JPEG schematic; that context is persisted as `report_context.json`
so later report rebuilds remain deterministic. Existing callers receive a
generic human-readable summary without changing their API usage. Reports have
a fail-closed 100-trace/40,000-display-point ceiling. Large statistical studies
may explicitly set `max_traces_per_plot` to show an evenly spaced,
corner-balanced representative overlay; every omitted trace remains available
through the structured evidence and RAW links.

Waveforms are parsed from the existing RAW evidence without rerunning LTspice.
Each trace is sliced to its validated native or independent step before a
bounded, endpoint-preserving display sample is embedded in the report. The
requirement engine's full-resolution result remains authoritative, and the
report links relatively to `results.json`, `results.csv`, the manifest, and
the original RAW file. Relative links and Windows/POSIX path normalization let
the complete experiment directory move between machines.

The builder rejects incomplete or inconsistent experiments, missing or
escaped RAW artifacts, invalid step mappings, non-finite plot data, and
oversized display payloads before replacing an existing report. User-provided
labels and values are escaped in both HTML and embedded JSON.

Phase 2D applies the same canonical manifest/results validation to indexing,
individual reports, and comparisons. Schema-v2 definition hashes are
recomputed, aggregate pass/fail counts are derived from points, and new
waveform analyses bind their RAW evidence by SHA-256 and byte size. Reports use
extrema-preserving display sampling so narrow glitches found by full-resolution
analysis remain visible; the structured JSON, RAW, and CSV artifacts remain
authoritative.

## Visualize comparisons and browse experiments

Phase 2C-C3 adds two derived, human-facing views while keeping JSON, CSV, RAW,
and SQLite artifacts authoritative. Call `build_comparison_report` with a
baseline and candidate experiment ID to produce a portable
`runs/comparisons/comparison-<id>/comparison.html`. The report overlays the
experiments' actual RAW waveform traces through the same validated parser and
bounded SVG renderer used by individual experiment reports. It also presents
candidate-minus-baseline measurement deltas and marks requirement regressions,
improvements, additions, removals, and unchanged checks.

Call `build_experiment_dashboard` to rebuild `runs/experiments.sqlite3` and
write `runs/dashboard.html`. The offline dashboard lists structured
experiments newest-first, supports text, status, and execution-mode filters,
and links relatively to available manifests, results, reports, and comparison
artifacts. It requires no web server or external JavaScript. Invalid experiment
artifacts remain visible through index diagnostics, while malformed comparison
artifacts are counted and skipped so one damaged result cannot prevent the
dashboard from being generated.

Both builders validate that inputs and outputs remain under `runs/`, normalize
portable Windows/POSIX artifact references, escape embedded content, enforce
display limits, and publish HTML atomically. They never rerun LTspice.

## Reuse verified simulation artifacts

Phase 2C-B1 adds an opt-in simulation cache to `run_netlist`,
`run_netlist_file`, `run_parameter_sweep`, `run_experiment`, and
`define_experiment`. Set `reuse_cache` to `true` to reuse a previously completed
LTspice simulation when its complete cache identity still matches:

```json
{
  "netlist": "V1 in 0 AC 1\nR1 in out 10k\nC1 out 0 1u\n.ac dec 20 10 1Meg\n.end\n",
  "filename": "filter.cir",
  "reuse_cache": true
}
```

The identity covers the exact netlist and filename, binary versus ASCII output,
timeout, simulator executable and version, operating system and architecture,
and recursively resolved include/library files. Entries and their artifacts
are content-addressed and verified by size and SHA-256 before any file is
copied. A hit receives fresh run provenance and independent artifacts under the
new run directory; measurement and waveform analysis still execute normally
against those copied artifacts.

Caching is disabled by default and fails closed. B1 deliberately bypasses
model-dependent devices, unresolved includes, dynamic file inputs, and corrupt
entries because LTspice may resolve data through implicit installation or user
libraries that cannot yet be fingerprinted confidently. Explicit nonempty
output directories are rejected before any input, manifest, or simulation
artifact is written.
Every `run_manifest.json` records whether caching was requested, whether the
run was eligible, its cache key, hit/miss state, whether a new entry was stored,
and any bypass reason. Cache entries live under `runs/cache/`.
B1 does not automatically evict entries. The repository-level retention tool
now provides deliberate, dry-run-first cleanup without placing deletion on the
MCP surface:

```bash
python3 artifact_retention.py inspect
python3 artifact_retention.py prune --scope cache --older-than-days 30 --keep-recent 10
```

Only completed, manifest-validated cache entries are eligible. Supplying
`--apply` performs the planned deletion after revalidating path boundaries and
manifest digests; without it, the command is inspection-only. The same tool can
independently manage terminal simulator runs and finished experiments while
leaving active and unrecognized artifacts untouched. Experiments referenced by
retained optimization, robust-selection, adaptive, or comparison evidence are
protected from pruning.

## Run a structured experiment as one native LTspice batch

Phase 2C-B2 adds an opt-in native execution mode to synchronous
`run_experiment`. Set `execution_mode` to `"native"` to expand the same ordered
Cartesian grid into one stepped LTspice deck instead of one process per point:

```json
{
  "netlist_template": "V1 in 0 AC 1\nR1 in out {R}\nC1 out 0 {C}\n.ac dec 20 100 100k\n.end\n",
  "parameters": [
    {"name": "R", "values": ["1k", "2k"]},
    {"name": "C", "values": ["10n", "20n"]}
  ],
  "execution_mode": "native"
}
```

The engine uses one private integer `.step` and generated parameter tables, so
the first declared parameter still changes slowest even though LTspice's nested
`.step` ordering differs. After simulation it requires the log to report the
exact private sequence `0..N-1`; waveform experiments must also contain exactly
`N` raw-vector slices. Only then are stepped `.meas` rows and full-resolution
waveform analyses attached to structured points.

Native values are deliberately limited to safe numeric LTspice expressions.
Existing `.step` directives, reserved `__mcp_` identifiers, ambiguous `.end`
directives, unsafe value text, and path/model directive placeholders are
rejected before an experiment directory is created. All points share the
`native-batch` run directory and keep their own `native_step_index`; batch
duration, cache source, key, and validated ordering are recorded once in
`native_batch`. `reuse_cache` can be enabled independently, and a cache hit is
subject to the same log and waveform mapping checks.

Phase 2C-B3 extends the same mode to `define_experiment`. Durable native jobs
recover only from a complete, definition-bound batch checkpoint; torn point
materialization is repaired from that checkpoint, while an interrupted batch
without one starts a new numbered attempt. Corrupt, mismatched, or out-of-order
batch checkpoints fail closed. The resulting `results.json` has the same shape
as synchronous native output and can be passed directly to
`compare_experiments`.

`run_experiment` remains the backward-compatible synchronous path. It validates
the complete definition before running, executes sequentially, and caps the
Cartesian product at 1,000 points. Requirements are
defined once and reused unchanged at every successful point. A failed
simulation or analysis is recorded without preventing later points from
running; a requirement miss remains a completed analysis with `all_passed`
false. Each experiment writes `experiment_manifest.json`, full `results.json`,
and a deliberately flat `results.csv` under a stable `point-0000`,
`point-0001`, ... directory layout.

## Waveform analysis and requirement metrics

`analyze_waveform` always evaluates the full parsed vector rather than the
downsampled agent payload. Requirements are structured metric/operator/target
objects, for example:

```json
[
  {"metric": "maximum", "operator": "<=", "target": 5.5},
  {"metric": "overshoot", "operator": "<=", "target": 5.0, "final_value": 5.0},
  {"metric": "settling_time", "operator": "<=", "target": 0.00002,
   "final_value": 5.0, "settling_tolerance": 0.02}
]
```

Each result includes units, pass/fail state, the threshold, metric parameters,
and point or region evidence. A stepped raw file requires `step_index`; the
tool splits on actual axis resets rather than assuming equal transient lengths.

Phase 1B adds closed, per-requirement `window_start`/`window_end` selection and
the `fall_time`, `pulse_width`, `duty_cycle`, `slew_rate`, `undershoot`,
`ripple`, `monotonicity`, `propagation_delay`, and
`forbidden_region_samples` metrics. Window evidence indexes remain relative to
the selected raw step, not the smaller window. Bounds between recorded samples
are linearly interpolated and retain both bracketing source indexes.

| Metric | Definition | Required parameters |
| --- | --- | --- |
| `fall_time` | First 90–10% falling transition | Falling `initial_value`/`final_value`, or falling window endpoints |
| `pulse_width` | First complete active pulse; `polarity` defaults to `high` | `threshold_value` |
| `duty_cycle` | Interpolated active time divided by window duration | `threshold_value` |
| `slew_rate` | Largest absolute adjacent slope | None |
| `undershoot` | Excursion beyond the initial endpoint, normalized to step size | Distinct step endpoints |
| `ripple` | Peak-to-peak value in the window | None |
| `monotonicity` | Largest adjacent reversal; zero is monotonic | `direction` only when endpoints are equal |
| `propagation_delay` | First secondary edge at or after the first primary edge | Secondary variable, two thresholds, and two edge directions |
| `forbidden_region_samples` | Recorded samples inside one band or simultaneous paired bands | `forbidden_min` and `forbidden_max` |

For paired checks, pass `secondary_variable` to `analyze_waveform`.
`propagation_delay` treats the primary `variable` as the trigger and measures
the first requested secondary edge at or after the first requested primary
edge; both thresholds and edge directions are explicit. A forbidden-region
requirement counts full-resolution samples inside the inclusive primary band
and, when secondary bounds are present, inside both bands simultaneously. A
requirement such as `{"metric": "forbidden_region_samples", "operator":
"<=", "target": 0, ...}` proves that no recorded sample violated the region.

Phase 1C adds frequency-domain requirements:

| Metric | Definition | Required parameters |
| --- | --- | --- |
| `frequency` | Average rate between first and last matching interpolated edges | `threshold_value`; `edge` defaults to `rising` |
| `spectral_peak` | Amplitude of the largest non-DC component in an explicit band | `frequency_min`, `frequency_max` |
| `thd` | RMS harmonics 2…N divided by the fundamental amplitude | `fundamental_frequency`; `maximum_harmonic` defaults to 5 |
| `ac_gain_db` | Log-frequency-interpolated gain | `frequency_value` |
| `cutoff_frequency` | First selected-direction crossing below reference gain by 3.0103 dB | `reference_frequency` |
| `peaking_db` | Peak gain minus gain at the reference frequency | `reference_frequency` |
| `gain_crossover_frequency` | Falling 0 dB loop-gain crossing | None |
| `gain_margin` | Negative gain at the falling odd-180° phase crossing | None |
| `phase_margin` | 180° plus phase at the falling 0 dB crossing | None |

Spectral analysis integrates the adaptive LTspice samples in time instead of
treating them as uniformly spaced. `spectral_peak` uses a Hann window and a
bounded frequency grid; `thd` uses the largest whole-cycle subwindow and an
explicit fundamental. Both reject requested content above the conservative
Nyquist limit implied by the largest recorded time gap. Resource limits cap a
spectral search at 4,096 bins and 5,000,000 point-frequency operations, and THD
at the 100th harmonic.

AC metrics use the complex primary vector, divided by `secondary_variable`
when supplied. Magnitude and unwrapped phase are interpolated in log frequency.
Stability metrics reject absent or multiple crossovers; narrow
`window_start`/`window_end` when a response legitimately contains several.
The default axis unit is inferred as `Hz` for a `frequency` vector and `s`
otherwise.
