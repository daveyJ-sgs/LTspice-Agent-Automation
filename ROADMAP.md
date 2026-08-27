# LTspice Agent Automation Roadmap

This roadmap records the long-term direction for the LTspice MCP project. The
goal is to grow the current simulation bridge into an open, evidence-producing
engineering system that can measure, explore, optimize, and validate circuits
against explicit requirements.

The unifying hardware project is a portable USB 3.x mixed-signal DAQ/scope with
approximately 1 MHz analog bandwidth. It will exercise the full workflow across
analog design, digital behavior, statistical verification, calibration, fault
analysis, PCB feedback, firmware boundaries, and a Windows host implementation.

## Working principles

- Text netlists (`.cir` and `.net`) remain the reproducible simulation boundary.
  Schematics are human-facing artifacts and are not assumed to support reliable
  headless conversion.
- Every conclusion should point back to a netlist, simulator configuration, raw
  result, measurement, and pass/fail requirement.
- Windows is a first-class target throughout development, not a port performed
  after the macOS implementation is complete.
- Optimization must not reward electrically invalid or physically unrealistic
  designs. Operating limits, convergence failures, model validity, and preferred
  component values are explicit constraints.
- The MCP remains local and trusted by default. Broader exposure requires path
  confinement, authentication, authorization, and resource controls.
- Features are developed against progressively harder reference circuits before
  being trusted on the flagship hardware.

## Approved development sequence

### Phase 0: Establish the cross-platform baseline

Before expanding the MCP surface:

- Add direct tests for every MCP tool and its structured response.
- Separate simulator-independent parser/unit tests from LTspice integration
  tests.
- Record the LTspice version, executable architecture, operating system, Python
  version, and MCP version in run manifests.
- Define run retention, cache, and cleanup policies before optimization creates
  thousands of artifacts.
- Add safe project/run path boundaries and validate filenames, output paths,
  timeouts, and payload sizes.

Windows acceptance criteria:

- Run parser and MCP unit tests on Windows in CI without requiring LTspice.
- Run an integration suite on a Windows machine with LTspice installed.
- Support Windows path syntax, executable discovery/override, spaces in paths,
  temporary directories, subprocess behavior, and LTspice log encodings.
- Avoid shell-specific commands and POSIX-only process assumptions.
- Verify stdio MCP operation from a Windows MCP client.

### Phase 1: Waveform property engine

Add full-resolution, machine-checkable waveform analysis as first-class MCP
tools. Phase 1 is split into three independently verifiable sections.

#### Phase 1A: Scalar and first-transition properties — complete

- Structured requirement, threshold, evidence, and result schemas
- Minimum, maximum, mean, RMS, and peak-to-peak values
- Rise time, overshoot, and settling time
- Full-resolution local analysis through `analyze_waveform`

#### Phase 1B: Time-domain and paired-signal properties — complete

- Closed analysis windows with interpolated boundary points
- Fall time, pulse width, duty cycle, and slew rate
- Undershoot, ripple, and monotonicity
- Paired-signal propagation delay and forbidden-region sample assertions
- Step-relative evidence indexes for windowed and paired analysis

#### Phase 1C: Spectral and AC properties — complete

- Fundamental frequency, spectral peak, and total harmonic distortion
- AC gain, cutoff frequency, and peaking
- Gain crossover, gain margin, and phase margin
- Explicit nonuniform Fourier integration, spectral-window, interpolation, and
  phase-unwrapping semantics
- Deterministic real/complex and stepped fixtures on macOS and Windows

The engine must analyze the full waveform locally. Uniformly downsampled data
is useful for agent context but must not be used to prove the absence of narrow
spikes or glitches. Results should include the metric, units, threshold,
pass/fail state, and the source run/vector/time or frequency region.

Windows acceptance criteria:

- Produce identical metrics within documented numeric tolerances from the same
  fixture data on macOS and Windows.
- Test binary, ASCII, complex, double-precision, FastAccess, and stepped raw
  fixtures on both platforms.

### Phase 2: Structured experiment runner

Generalize single-value parameter substitution into durable experiments with:

#### Phase 2A: Deterministic Cartesian experiments — complete

- Multiple named parameters and units
- Ordered explicit value sets and deterministic Cartesian expansion
- Reusable requirements and waveform properties
- Isolated per-point simulation and analysis failures
- Portable experiment manifests plus structured JSON and flat CSV results

#### Phase 2B: Durable experiment jobs — complete

##### Phase 2B-A: Dependent and derived parameters — complete

- Safe textual derived-parameter templates with inferred dependencies
- Stable topological resolution, forward references, and cycle detection
- Base-only Cartesian cardinality with resolved per-point provenance

##### Phase 2B-B: Persistent bounded-concurrency jobs — complete

- Atomic per-point checkpoints, bounded concurrency, and deterministic results
- Portable progress state and restart-safe resume into new attempt directories

##### Phase 2B-C: MCP job lifecycle — complete

- `define_experiment`, `get_experiment`, and `cancel_experiment` lifecycle tools
- Explicit `start_experiment` operation so definition does not launch work
- Idempotent cooperative cancellation with Windows-safe point boundaries

#### Phase 2C: Execution efficiency and reporting — complete

##### Phase 2C-A: Experiment comparison and reports — complete

- Read-only comparison of two completed experiment artifacts
- Exact full-parameter point matching and candidate-minus-baseline deltas
- Stable requirement identities with regression and improvement classification
- Content-addressed deterministic JSON and human-readable Markdown reports

##### Phase 2C-B: Native stepping and cached-result reuse — complete

###### Phase 2C-B1: Provenance-safe simulation cache — complete

- Opt-in content-addressed reuse at the shared LTspice execution boundary
- Simulator, runtime, option, netlist, and resolved-dependency fingerprints
- Atomic entries, verified copied artifacts, and per-run cache provenance
- Conservative bypass for implicit models and unresolved external inputs

###### Phase 2C-B2: Native structured experiments — complete

- Opt-in synchronous native mode using one indexed LTspice stepped deck
- Deterministic Cartesian mapping validated against exact log ordinals
- Explicit stepped `.meas` row mapping and shared raw-vector slice validation
- Shared stepped waveforms analyzed through the existing requirement engine
- Native-batch duration, execution-source, cache, and mapping provenance
- Fail-closed numeric-expression and deck eligibility checks

###### Phase 2C-B3: Durable native-batch integration — complete

- Atomic batch recovery and point checkpoint materialization
- Cooperative cancellation with validated batch evidence preservation
- Cache provenance retention and comparison-compatible structured results
- Cross-platform restart, corruption, schema, and lifecycle regression coverage

##### Phase 2C-C: Experiment indexing and visualization — complete

###### Phase 2C-C1: Experiment index and queries — complete

- Rebuildable normalized SQLite index derived from manifests and results
- Schema-v1 synchronous and schema-v2 durable experiment compatibility
- Exact same-point parameter filtering and deterministic pagination
- Relative artifact paths, isolated diagnostics, and atomic replacement
- MCP rebuild/query tools with cross-platform regression coverage

###### Phase 2C-C2: Human experiment reports — complete

- Portable offline HTML reports generated from completed experiment artifacts
- Visible server-rendered SVG overlays with interactive trace inspection
- Structured point, measurement, requirement, and error summaries
- Display-only endpoint-preserving downsampling with full-resolution evidence links
- Schema-v1/v2, independent/native, macOS/Windows, and confinement coverage

###### Phase 2C-C3-A: Comparison visualization — complete

- Portable baseline-versus-candidate HTML reports derived from completed artifacts
- Actual RAW waveform overlays using the validated C2 parser and SVG renderer
- Measurement deltas plus explicit regression and improvement markers
- Relative evidence links, bounded display payloads, and atomic replacement

###### Phase 2C-C3-B: Unified experiment dashboard — complete

- Rebuildable searchable offline dashboard derived from the SQLite index
- Status and execution-mode filters with experiment and comparison summaries
- Relative links to manifests, results, reports, and comparison artifacts
- Malformed-comparison isolation and cross-platform regression coverage

Phase 2A provides synchronous `run_experiment`; Phase 2B adds the durable job
lifecycle without changing that contract. Phase 2C adds execution efficiency
and comparison operations without changing the portable experiment definition.

#### Phase 2D: Foundation hardening — complete

##### Phase 2D-A: Artifact trust boundaries — complete

- Resolved-path confinement for run, RAW, log, report, and experiment artifacts
- Symlink-safe atomic CSV and dashboard publication
- Direct-child enforcement for durable experiment directories

##### Phase 2D-B: Portable simulator inputs and text decoding — complete

- Shared BOM/null-pattern-aware UTF-8 and UTF-16LE LTspice log decoding
- Encoding-preserving real and complex ASCII RAW parsing
- Isolated explicit output directories and source-relative include/library staging

##### Phase 2D-C: Evidence integrity — complete

- One canonical manifest/results validator for index, reports, and comparisons
- Schema-v2 definition-hash verification and point-derived aggregate checks
- SHA-256/size provenance for new simulation and analyzed RAW artifacts

##### Phase 2D-D: Process and REST safety — complete

- Cross-platform runs-directory ownership lock for durable experiment managers
- Explicit SQLite connection closure and strict REST input typing
- Loopback-only REST binding until an authenticated network mode exists

##### Phase 2D-E: Bounded, faithful presentation — complete

- Extrema-preserving report sampling and endpoint-preserving MCP previews
- Bounded legacy sweeps, waveform responses, analyses, and requirements
- Spreadsheet-safe CSV text and repeated-axis step inference

Windows acceptance criteria:

- Persist and resume job state correctly across process restarts.
- Confirm bounded parallel LTspice execution and cancellation semantics on
  Windows rather than assuming Unix signal behavior.
- Keep experiment definitions and result schemas portable between platforms.

### Phase 3: Statistical yield and worst-case analysis

Turn the standalone Monte Carlo example into a general, durable statistical
layer over the Phase 2 experiment system. Statistical studies must reuse the
existing simulation, waveform-analysis, checkpoint, cache, integrity,
comparison, index, and report paths. They must not introduce a second job
runner or weaken the existing experiment contracts.

A statistical sampler produces an ordered, immutable point plan before
simulation begins. This is distinct from a Cartesian sweep: the resistance,
capacitance, temperature, and other values belonging to sample 17 remain one
paired point rather than expanding into a cross-product. The saved point plan
is the reproducibility boundary and can be inspected or transferred without
running LTspice.

#### Phase 3A: Deterministic statistical point plans — complete

##### Phase 3A-1: Uniform point-plan foundation — complete

- Typed statistical variables with nominal values, units, bounds, sample
  count, and a required reproducibility seed
- Versioned SHA-256 counter-based uniform sampling with canonical decimal
  serialization and byte-stable golden fixtures
- Immutable content-addressed sample-plan artifacts with generator, seed,
  sample ordinal, resolved values, definition hash, and artifact hash
- `generate_statistical_plan` and `get_statistical_plan` MCP operations that do
  not invoke LTspice
- Portable explicit-point execution through `run_statistical_experiment`
  without changing Cartesian `run_experiment` behavior
- Existing results, CSV, canonical index, comparison, and offline-report
  compatibility
- Bounded variables, samples, generated cells, identifiers, units, values,
  payloads, and path publication
- Real three-point RC validation proving paired R/C samples execute as three
  Phase 2 points rather than a nine-point cross-product

##### Phase 3A-2: Bounded Gaussian and weighted discrete distributions — complete

- Bounded Gaussian variables with explicit nominal, sigma, bound, minimum-span,
  and rejection-budget semantics
- Fixed high-precision Decimal Marsaglia-polar transformation without
  platform-library random sampling
- Weighted discrete variables with canonical string values, normalized
  positive finite weights, and deterministic cumulative-boundary behavior
- Per-variable draw independence by name, distribution, sample ordinal, seed,
  attempt, coordinate, and generator version
- Backward-compatible Phase 3A-1 uniform artifacts plus a versioned mixed-plan
  generator
- Golden, malformed-input, boundary, workload, population, and reordered-input
  coverage for uniform, Gaussian, discrete, and mixed plans
- Real five-point mixed RC validation through LTspice, the canonical index, and
  the offline report path

Phase 3A verification gate:

- Golden fixtures prove byte-identical sample plans across repeated runs and
  platforms for every Phase 3A distribution.
- Unit tests cover invalid definitions, boundary values, stable ordering, and
  the absence of accidental Cartesian expansion.
- Uniform, Gaussian, and discrete variables compile into ordinary Phase 2
  execution points without a parallel simulation path.

#### Phase 3B: Durable yield studies — complete

##### Phase 3B-1: Frozen durable execution — complete

- Synchronous and durable statistical-study operations execute a
  saved point plan through the existing bounded worker, cancellation, resume,
  cache, and per-point checkpoint machinery.
- The complete ordered point plan and its source hashes are embedded in the
  schema-v2 definition hash, so resume never reloads or redraws samples.

##### Phase 3B-2: Yield and error classification — complete

- Yield is evaluated from the existing measurement and waveform requirements;
  simulation or analysis errors remain separate from electrical failures.
- Structured sample results include pass/fail counts, observed yield, Wilson
  binomial confidence intervals, percentiles, mean, standard deviation, and
  explicit treatment of incomplete or invalid samples.

##### Phase 3B-3: Statistical evidence artifacts — complete

- `statistics.json` and flat `statistics.csv` preserve contributing sample
  ordinals, requirement margins, descriptive statistics, and 95% Wilson bounds.
- Links from every statistic and failed sample lead to its experiment
  point, RAW/log evidence, resolved values, and requirement results.

##### Phase 3B-4: MCP and human report surfaces — complete

- `define_statistical_study` and `summarize_statistical_experiment` expose the
  durable lifecycle and bounded results through MCP.
- Offline reports add yield, confidence, invalid/error accounting, portable
  JSON/CSV links, and exact failed-sample evidence.

Verification gate:

- Cancellation and restart reproduce the uninterrupted result from the same
  saved plan.
- Hand-calculated fixtures verify yield, confidence intervals, percentiles,
  and error accounting.
- A real 12-sample Sallen-Key tolerance plan exercised both AC and transient
  requirements and identifies the exact failing samples.

#### Phase 3C: Correlation, measured populations, and operating corners — complete

##### Phase 3C-1: Correlated Gaussian variables — complete

- Named correlated-Gaussian groups with finite, symmetric, unit-diagonal,
  positive-semidefinite matrices and coefficients bounded to `[-1, 1]`
- Canonical variable-name ordering with matching matrix permutation, disjoint
  groups, Gaussian-only membership, and exact normalized matrix provenance
- Versioned SHA-256 counter draws and an 80-digit Decimal Cholesky transform,
  including singular positive-semidefinite matrices and bounded joint rejection
- Backward-compatible Phase 3A artifact bytes when correlations are absent
- MCP input/output schemas expose normalized correlations, generator version,
  definition hash, and content-addressed plan evidence
- Golden, malformed-matrix, reordering, rejection-budget, 1,000-sample
  population, durable-execution, and real correlated Sallen-Key coverage

##### Phase 3C-2: Empirical measured populations — complete

- Deterministic with-replacement resampling from inline numeric observations or
  a named numeric column in a project-confined UTF-8 CSV
- Self-contained immutable plans freeze canonical observations plus input kind,
  unit, column, raw/canonical SHA-256, row count, and resampling provenance
- Bounded CSV bytes, rows, and columns with fail-closed malformed, nonfinite,
  missing-column, path-escape, and symlink handling before publication
- Versioned per-variable SHA-256 counter draws preserve duplicates and remain
  stable under declaration reordering; Phase 3A/3C-1 artifacts stay unchanged
- MCP schema/protocol, inline/CSV equivalence, golden artifact, source-change,
  1,000-sample frequency, immutable reload, and cross-platform regression tests
- A real 16-point empirical Sallen-Key AC study completed without execution or
  analysis errors and exposed two exact peaking failures (14/16, 87.5% yield)

##### Phase 3C-3: Named operating corners — complete

- Ordered named axes map temperature, supply, load, or a predeclared model name
  to one bounded SPICE-token parameter and expand inside each sample ordinal
- Axis/value/parameter validation, collision rejection, and preflight limits on
  axes, values, expanded points, and total parameter cells fail before output
- Versioned content-addressed plans preserve sample ordinal, named corner map,
  parameter values/units, definition hash, and deterministic expansion order
- Parallel point metadata is frozen into synchronous and durable definitions so
  checkpoint/resume cannot detach a result from its originating corner
- MCP, JSON, CSV, and HTML expose classifications, yield, Wilson interval,
  invalid count, failed samples, and evidence links for every corner combination
- Pooled yield is absent by default and appears only when the immutable plan
  explicitly requests it; per-corner results remain mandatory either way
- A real 4-sample by 4-corner Sallen-Key study completed all 16 analyses: the
  nominal/light corner passed 4/4 while the deliberately weak nominal/heavy-load
  corner failed 4/4 and remained independently visible at 0% yield

##### Phase 3C-4: Stratified and low-discrepancy sampling — complete

- Optional `independent`, `latin_hypercube`, and seeded digit-scrambled `halton`
  methods with byte-identical legacy plans when the method is omitted
- Versioned SHA-256 Latin-hypercube permutations and jitter that use every
  per-variable stratum exactly once
- Stable name-ranked Halton prime dimensions, deterministic digit scrambling,
  and seeded shifts that are invariant to variable declaration reordering
- Inverse-CDF mapping for uniform, weighted-discrete, and empirical variables,
  with ordinary sample-major corner expansion and evidence attribution
- Fail-closed rejection of Gaussian and correlated-Gaussian combinations until
  Phase 3C-5 supplies a deterministic bounded transform with honest guarantees
- Golden artifacts, coverage/discrepancy, malformed-input, population,
  persistence, MCP schema, corner, and macOS/Windows regression coverage
- A real 16-point scrambled-Halton Sallen-Key study completed every LTspice
  analysis with no invalid samples and exposed six exact requirement failures
  (10/16 passing, 62.5% observed yield)
- Explicit-point execution remains the evidence-preserving path; native
  LTspice stepping is deferred unless every stepped result can be mapped back
  to its planned point and complete artifacts

##### Phase 3C-5: Integration and verification — complete

###### Phase 3C-5A: Gaussian sampler integration — complete

- Deterministic high-precision Decimal normal CDF and inverse-CDF transform for
  bounded Gaussian Latin-hypercube and Halton coordinates
- Exact truncated-probability strata for uncorrelated bounded Gaussians without
  clipping, platform math libraries, or changes to v1-v6 artifacts
- Correlated latent-normal stratification through the existing Decimal Cholesky
  matrix with deterministic whole-vector bound rejection
- Versioned `sha256-stratified-gaussian-v7` plans with golden, persistence,
  probability-strata, tight-bound, 1,000-sample population, MCP, and
  macOS/Windows regression coverage
- A real 16-point v7 Sallen-Key study exercised correlated Gaussian R/C pairs,
  weighted gain/load populations, and complete LTspice evidence with no invalid
  samples (10/16 passing, 62.5% observed yield)

###### Phase 3C-5B: Complete output integration — complete

- Canonical, fail-closed sampling provenance validates the method, generator,
  plan ID, plan SHA-256, durable-definition SHA-256, and runs-relative plan path
- Statistics schema v2 embeds provenance in JSON; flat CSV provenance rows and
  MCP summaries expose the same contract without parsing the manifest
- Durable resume proves the frozen plan and definition hashes survive unchanged;
  legacy studies without an explicit method remain identifiable as independent
- The human HTML report names the sampling method and generator, displays both
  hashes, and links directly to the immutable statistical-plan artifact
- Malformed provenance, persistence, cancellation, MCP-schema, JSON/CSV, HTML,
  and full macOS/Windows regression coverage

###### Phase 3C-5C: Final Phase 3 verification — complete

- A byte-stable 128-sample combined fixture covers empirical inverse-CDF
  frequencies, correlated bounded Gaussians, scrambled Halton sampling, and
  three ordered named corners across 384 exact sample-by-corner points
- The fixture preserves a repeated empirical observation at 64 selections while
  selecting each other bin 32 times, and measures 0.8154 correlation against
  the requested 0.8 coefficient
- Durable restart preserves the combined plan's complete point order, corner
  attribution, immutable source metadata, and six-field sampling provenance
- A real 8-sample by 3-corner Sallen-Key study completed all 24 LTspice
  analyses with zero invalid points: light and nominal loads passed 8/8 while
  the deliberately weak 1 kOhm load failed 8/8 and remained separately visible
- Local evidence is `mcp-experiment-20260824-204055-619847-e1afd7fd`, sourced
  from content-addressed plan `statistical-plan-3b22357b300d3bcb`
- Adversarial review, complete regression, documentation, offline-report, and
  macOS/Windows verification close Phase 3C without implicitly pooling corners

Verification gate:

- Fixture correlations and empirical frequencies match expected tolerances.
- Corner-by-sample cardinality, ordering, resume, and provenance are stable on
  macOS and Windows.
- A deliberately weak Sallen-Key corner fails while stronger corners remain
  independently visible in the structured results.

#### Phase 3D: Sensitivity and worst-credible-case analysis — complete

##### Phase 3D-A: Worst evidenced cases — complete

- Deterministic post-processing ranks each requirement independently by
  ascending signed margin without rerunning LTspice or comparing unlike units
- Dense sample and finite-corner ranks preserve exact ties; the bounded top-25
  sample window expands at its cutoff so a tie is never split
- Every ranked sample carries point/sample ordinals, parameters, named corners,
  measured value, pass state, signed margin, and portable evidence path
- Declared corners with no evaluated evidence remain explicit with null ranks;
  invalid, cancelled, and unfinished points are counted but never ranked
- Individually atomic `worst_cases.json` and flat `worst_cases.csv` artifacts plus
  `analyze_statistical_worst_cases` expose the same validated MCP contract
- Synthetic tie, empty-corner, cutoff, artifact, schema, real Sallen-Key, and
  macOS/Windows regression coverage
- The combined Phase 3C Sallen-Key evidence ranks the weak 1 kOhm load first
  for the 7.6 dB gain floor at -0.06435 dB margin, with exact point evidence

##### Phase 3D-B: Global rank sensitivity — complete

- Per-requirement Spearman coefficients rank sampled inputs against signed
  electrical margin using average ranks for exact ties
- Named corners remain separate scopes; invalid points are excluded and no
  corner population is pooled implicitly
- Five paired observations and variation in both ranks are required;
  insufficient, constant, nonnumeric, weak, and non-monotonic evidence remain
  explicit instead of producing invented influence values
- Absolute rho of 0.5 is the documented meaningful-monotonic threshold, with
  direction, strength, sample cardinality, units, and dense influence rank
- Correlated input names accompany each association so the result is not
  presented as independent causality or a local design derivative
- Individually atomic `sensitivity.json` and `sensitivity.csv` artifacts plus
  `analyze_statistical_sensitivity` expose the same MCP contract
- The 24-point Sallen-Key study keeps all three load corners separate: gain
  margin is most associated with correlated `R1`/`R2`, while peaking margin is
  most associated with `C1`; each corner has eight evaluated samples

##### Phase 3D-C: Local OAT and tornado data — complete

- A completed statistical point can seed a content-addressed baseline plus
  symmetric low/high relative perturbations for every nonzero numeric sampled
  variable while its named corner remains fixed
- The OAT point plan is immutable and runs through the existing durable
  independent executor, inheriting bounded concurrency, cancellation, resume,
  cache policy, and per-point evidence
- Categorical and zero-baseline variables remain explicit skip records instead
  of receiving fabricated numeric derivatives
- Per-requirement tornado rows carry baseline/low/high input values and units,
  signed margins, effects, one-sided slopes, impact ranks, and evidence paths;
  incomplete simulations remain visible but unranked
- Individually atomic `tornado.json` and `tornado.csv` artifacts plus
  `define_local_sensitivity_study` and `analyze_local_sensitivity` expose the
  durable MCP workflow
- A real nine-point, 1% OAT study around weak-load Sallen-Key point 2 completed
  with zero invalid simulations and 12 complete effects. `C1` led local gain
  impact, while `C2` and `C1` led peaking impact, demonstrating why local OAT
  and global rank association must remain separate
- Real evidence is `mcp-experiment-20260825-075328-231073-99031848`, with its
  interactive `report.html` and traceable RAW/log artifacts

##### Phase 3D-D: Adaptive boundary sampling — complete

- Content-addressed one-dimensional studies require two completed source points
  that differ only in the selected numeric variable and have opposite outcomes
  for one stable requirement identity
- Deterministic evenly spaced interior batches run through the existing durable
  executor, inheriting bounded concurrency, cache policy, restart, and complete
  per-point LTspice evidence
- Atomic parent manifests preserve the active child before launch and record
  every observation, signed margin, evidence path, bracket, width, shrink
  ratio, and cumulative sample count at batch boundaries
- Input tolerance, sample budget, and numeric resolution are explicit terminal
  rules; child failure, malformed evidence, and multiple pass/fail transitions
  fail closed instead of inventing a boundary
- Adaptive samples remain separate from Wilson yield confidence because their
  selection is intentionally biased toward the boundary
- MCP tools define, advance, and inspect the resumable workflow; continuous
  design optimization and Pareto selection remain deferred to Phase 4
- A real nine-sample Sallen-Key load study refined the 7.6 dB gain-floor
  transition from 1-10 kOhm to 1140.625-1281.25 ohm in three batches, stopping
  honestly at its sample budget with complete RAW/log evidence
- Real evidence is `adaptive-study-b29f5876eda244b0`; its final child report is
  `mcp-experiment-20260825-081030-076374-3524e910/report.html`

Verification gate:

- Synthetic monotonic and non-monotonic fixtures exercise honest sensitivity
  reporting.
- Known worst finite corners are recovered exactly, including ties.
- Adaptive studies resume at a batch boundary and reproduce their full sample
  and boundary-resolution history from the same definition.

#### Phase 3E: Statistical reports, indexing, and hardening

##### Phase 3E-A: Integrated analysis reports — complete

- Completed statistical reports regenerate and link validated statistics,
  worst-case, and global-sensitivity JSON/CSV artifacts without rerunning
  LTspice
- Bounded HTML tables add measurement and signed-margin distributions, worst
  evidenced cases, named-corner sensitivity ranks, and correlation cautions to
  the existing yield, Wilson interval, corner, failure, and waveform views
- Completed local OAT reports regenerate and link tornado JSON/CSV evidence and
  retain incomplete effects explicitly instead of ranking them
- User content remains escaped, analysis row counts and waveform payloads are
  bounded, and every displayed conclusion remains traceable to structured or
  full-resolution simulation evidence
- Real statistical and OAT Sallen-Key reports were rebuilt successfully; the
  statistical report is
  `mcp-experiment-20260824-204055-619847-e1afd7fd/report.html`

##### Phase 3E-B: Statistical index and dashboard queries — complete

- Schema-v2 rebuildable SQLite records circuit hashes, statistical/sampling
  identity, aggregate yield and Wilson bounds, sampled variables, named corner
  definitions and per-corner summaries, and requirement metrics
- Exact query filters cover circuit, status, aggregate or same-corner yield and
  confidence floors, variable, requirement, and corner values without treating
  the derived index as authoritative evidence
- Multiple corner predicates plus yield/confidence thresholds bind to one
  actual corner result; unpooled studies retain null aggregate fields
- The offline dashboard exposes method and aggregate or per-corner yield and
  searches variables and requirements alongside existing experiment metadata
- Synthetic validation covers matching, misses, malformed inputs, and corner
  binding; rebuilding 48 real experiments and 1,640 points produced zero issues

##### Phase 3E-C: Compatible statistical comparison — complete

- Compatible studies share normalized population, corner, parameter-unit, and
  electrical-analysis contracts while allowing circuit or sampling changes
- Same-plan comparisons preserve exact point pairing and classification
  transitions; changed plans use unpaired population summaries only
- Attribution distinguishes repeat evidence, paired circuit outcomes, sample
  plan changes, and confounded simultaneous circuit/plan changes
- Content-addressed JSON, CSV, and offline HTML expose aggregate and per-corner
  yield evidence, Wilson bounds, requirement-margin distribution deltas,
  invalid evidence, and source-result links without rerunning LTspice
- Missing requirement populations from infrastructure failures remain explicit
  nulls and incompatible populations fail before output publication
- Real comparison `00b0ea046f0b291b` pairs two 12-point transient Sallen-Key
  runs from the same plan: the baseline's 12 analysis errors transition to five
  electrical passes and seven failures in the repeat report

##### Phase 3E-D: Resource hardening and production example — complete

- Enforce bounded sample counts, corner expansion, imported data, report
  payloads, runtime, and disk use; malformed studies must be isolated during
  index and dashboard rebuilds
- Per-process timeouts stop at 3,600 seconds; RAW and log readers reject files
  above 256 MiB and 64 MiB before reading, and successful or cached run
  artifacts above 512 MiB are rejected before hashing or cache publication
- The 512 MiB artifact check is explicitly a post-process evidence-acceptance
  guard rather than an operating-system disk quota
- Retire the standalone Monte Carlo example in favor of a documented example
  built on the production statistical API
- The real 24-point scrambled-Halton RC example completed as
  `mcp-experiment-20260825-184130-112101`: all points produced simulator and
  analysis evidence, observed yield was 100%, and its Wilson 95% interval was
  86.20%–100%

##### Phase 3E-E: Mixed-signal DAQ validation circuit — complete

- Added a portable 1 MHz-class DAQ acquisition channel with two-pole
  anti-alias filtering, gain/output loading, clocked sampling switch, hold
  capacitor, ADC capacitance, and leakage
- A shared 12-sample correlated scrambled-Halton population expands over light
  and heavy ADC-load corners into 24 AC and 24 transient LTspice points
- Final AC experiment `mcp-experiment-20260825-190609-038703` and transient
  experiment `mcp-experiment-20260825-190610-370539` each produced 100% yield
  separately in both named corners with no invalid evidence
- Local experiment `mcp-experiment-20260825-190623-810704-b04fd1b5` completed
  all 19 baseline/OAT points and all 36 requirement effects
- Adaptive study `adaptive-study-d94f3ff5a0cced71` narrowed the 5 mV
  acquisition-error boundary to 584.375 ns failing and 586.328 ns passing, a
  1.953 ns bracket, after 12 real LTspice samples
- The richer transient uncovered LTspice binary RAW sign-bit time encoding;
  the parser now restores physical time magnitudes before step detection

##### Phase 3E-F: Real Windows LTspice CI prototype — complete

- As the final Phase 3 hardening step, prototype a GitHub-hosted Windows job
  that installs a pinned, checksum-verified LTspice build and runs bounded real
  simulator smoke tests after resolving first-run consent noninteractively
- The opt-in workflow pins the official 26.0.2 x64 MSI at SHA-256
  `485dabd2d7d8293de733a399719f6538efda4a54b48b181a14e07271186984d3`,
  installs it silently, explicitly selects **No** for usage-data sharing, and
  fails closed if the verified dialog does not close
- Successful GitHub run `32924033759` completed in 1m26s on Windows Server
  2025: real LTspice 26.0.2 simulated 501 AC points in 0.599s, reported
  -35.964697 dB at 1 kHz and a 21.290309 Hz sweep-referenced cutoff, and emitted
  an 82,843-byte hashed artifact set; its retained 48,904-byte RAW file matched
  the manifest SHA-256
- The same smoke contract passed locally on macOS LTspice 17.2.4 with identical
  cutoff and a 0.000003 dB gain difference, providing real cross-platform
  numeric evidence rather than only mocked Windows path tests

##### Phase 3E-G: Final adversarial hardening — complete

- Adaptive boundary studies restart the same durable child if execution is
  interrupted after definition but before queueing
- Canonical result validation rejects unsupported comparison operators and
  recomputes each requirement pass state from its measured value and threshold
- Summaries, worst-case analysis, sensitivity, comparisons, and indexing all
  verify the referenced immutable statistical plan, complete SHA-256, generator
  version, definition hash, and sampling method before trusting provenance
- Sensitivity preflights its projected output and worst-case analysis bounds its
  serialized rankings with a shared 2,000-row artifact budget; HTML rendering
  retains an independent defense-in-depth limit
- The final adversarial regression suite contains 240 passing tests

##### Phase 3E-H: Real Windows mixed-signal DAQ qualification — complete

- Upgraded the opt-in real-Windows job from an RC-only smoke test to the full
  production DAQ path while retaining the fast RC control simulation
- Real GitHub run `32969899223` installed checksum-pinned LTspice 26.0.2 on
  Windows Server 2025 and passed exact commit `0ed04b1` in 2m00s
- The immutable 24-point plan `statistical-plan-3648d75d3bc11f35` produced
  Windows AC experiment `mcp-experiment-20260826-124252-671728` and transient
  experiment `mcp-experiment-20260826-124303-038862`
- Both analyses completed 24/24 points with zero simulator or analysis errors,
  zero invalid evidence, and 100% yield independently in both light and heavy
  ADC-load corners
- The retained 14 MiB evidence tree contains 262 files, including 48 primary
  RAW waveforms, 48 per-point run manifests, 60 JSON files, eight CSV files,
  the immutable plan, and two self-contained interactive HTML reports
- macOS LTspice 17.2.4 reproduced the same point plan and electrical
  classifications; AC requirement-margin means agreed to floating-point noise,
  while transient margins preserved the same pass/fail conclusions
- Added `mixed_signal_daq.asc`, a stock-symbol educational schematic with four
  labeled functional blocks, explanatory notes, and a regression-checked
  component/value inventory matching the transient automation netlist
- The expanded 241-test suite passed locally and in the ordinary GitHub macOS
  and Windows jobs for the qualification commit

##### Phase 3E-I: Human-first engineering reports — complete

- Added bounded, reusable narrative metadata for a circuit explanation,
  simulation purpose, MCP-verification context, and an embedded repository-local
  schematic; the metadata persists beside each experiment for deterministic
  rebuilds
- Reordered reports around the schematic and interactive plots, with compact
  sample/corner labels, shared corner colors, and humanized analysis names
- Added engineering-number formatting such as pF, kΩ, MHz, ns, and µs with
  four significant digits while leaving authoritative JSON/CSV values unchanged
- Summarized parameter populations as values or ranges and moved full trace
  parameters, requirements, provenance hashes, JSON/CSV, and RAW links into
  collapsed evidence sections
- The mixed-signal DAQ AC and transient examples now supply distinct explanatory
  narratives while sharing the same educational LTspice schematic
- Moved cursor details into a responsive side inspector, added two-axis
  crosshairs with a selected-trace marker, and added horizontal drag zoom with
  button and double-click reset while preserving fully offline reports
- The expanded 243-test regression suite passes with the report, comparison,
  MCP-schema, path-confinement, and deterministic-rebuild contracts intact

Phase 3 completion criteria:

- Given the same normalized definition and seed, macOS and Windows generate
  the same canonical point plan and equivalent statistical conclusions within
  documented numeric tolerances.
- Reports clearly distinguish sample-generation changes, simulator/platform
  numeric differences, electrical failures, and infrastructure errors.
- A resumed durable study is evidence-equivalent to an uninterrupted study.
- The Sallen-Key reference study reports yield, confidence, corners,
  sensitivities, and worst evidenced cases with traceable RAW/log artifacts.
- Unit, adversarial, resource-bound, and real LTspice integration tests pass on
  both supported platforms before Phase 4 begins.

Recommended implementation sequence:

1. Build and freeze the deterministic point-plan schema and golden fixtures.
2. Reuse the durable Phase 2 executor for independent paired sample points.
3. Add yield summaries and the first end-to-end Sallen-Key tolerance study.
4. Add correlation, empirical populations, and deterministic corner axes.
5. Add sensitivity, bounded adaptive sampling, reports, indexing, and final
   cross-platform hardening.

### Phase 4: Constrained multi-objective optimization

Optimize component and model parameters against explicit objectives and hard
constraints. The system should support:

- Continuous, integer, categorical, and preferred-value-series parameters
- Multiple objectives and Pareto-front reporting
- Hard electrical, thermal, cost, and manufacturability constraints
- Failed-simulation and invalid-operating-point handling
- Coarse search followed by local refinement
- Final corner and Monte Carlo verification of selected candidates
- An explanation of why the winning candidate was selected

The mixed-signal DAQ is the primary Phase 4 qualification circuit. Its first
optimization trades anti-alias rejection against acquisition settling while
holding bandwidth, peaking, tracking error, droop, output range, and ADC-load
corners as explicit constraints. Component choices and acquisition settings
are design variables; manufacturing variation and ADC loading remain separate
uncertainty/corner axes. A small Sallen-Key fixture remains only as a fast,
deterministic optimizer regression.

#### Phase 4A: Deterministic coarse optimization — complete

- Freeze a content-addressed candidate-plan schema containing bounded design
  domains, fixed circuit parameters, named operating corners, objectives, hard
  constraints, generator/version metadata, and deterministic selection policy
- Support bounded continuous grids and explicit preferred-value-series choices
  without creating a second simulation runner
- Evaluate the same candidate plan through the existing AC and transient
  experiment paths, preserving simulation errors, analysis errors, and
  electrical constraint failures as distinct outcomes
- Produce a deterministic Pareto front, select one feasible candidate by an
  explicit normalized-regret policy, and retain the evidence and explanation
  needed to reproduce that decision
- Qualify the first slice on the DAQ anti-alias/acquisition tradeoff; do not add
  local refinement until this evidence contract is stable

Phase 4A verification gate:

- `optimization-plan-762b2ccc92fff6f6` freezes 16 DAQ candidates and 32
  light/heavy ADC-load points using E12 resistor/capacitor choices plus a
  bounded continuous output-resistance grid
- Real local LTspice AC experiment `mcp-experiment-20260826-194300-922975` and
  transient experiment `mcp-experiment-20260826-194302-971933` evaluate the
  same immutable plan through the existing independent runner
- `optimization-study-fee26beaaca4513b` classifies 14 feasible candidates and
  two maximum-bandwidth failures, identifies nine Pareto candidates, and
  selects candidate 8: CAA1=100 pF, CAA2=82 pF, RAA1=820 ohm, ROUT=35 ohm
- The selected worst-corner objectives are -25.27 dB gain at 10 MHz and
  1.003 us settling; every Phase 4A hard constraint passes at both ADC-load
  corners, without claiming manufacturing yield
- Content-address, reorder-stability, domain-bound, tamper, deterministic
  rebuild, MCP-runner reuse, real-report, responsive-browser, and complete
  251-test regression checks pass

#### Phase 4B: Durable orchestration and cross-platform resume — complete

- Define, queue, cancel, resume, and inspect optimization studies through the
  durable experiment manager while retaining one immutable candidate plan
- Serialize all state required to evaluate or resume the same unfinished plan
  on macOS and Windows
- Run the frozen DAQ population with real LTspice on both platforms and compare
  candidate classifications, objective values, Pareto membership, and selected
  design within documented numeric tolerances

Phase 4B verification gate:

- `define_optimization_study`, `start_optimization_study`,
  `get_optimization_study`, and `cancel_optimization_study` compose immutable
  optimization plans through the existing durable child-experiment manager;
  job manifests contain relative plan, child, and result identities rather than
  machine-specific paths
- Restart, relocation, cooperative cancellation, child-definition tamper,
  content-addressed comparison, MCP schema, and complete 263-test regression
  checks pass; Windows-safe child identity checks use the experiment-manager
  lock rather than racing atomic manifest replacement, and report tests decode
  portable HTML explicitly as UTF-8 on every platform
- Tolerance-aware plan `optimization-plan-2b6f2d62d7ca7c14` records 0.05 dB
  alias-gain and 50 ns settling-time decision resolution. Real macOS LTspice
  study `optimization-study-15a3b0b178405e19` evaluates 32 AC and 32 transient
  points with zero errors, classifies 14 feasible and two bandwidth-failing
  candidates, and selects candidate 15 as the stable Pareto design
- GitHub Windows run `33037990442` repeats all 64 points with LTspice 26.0.2;
  AC child `mcp-experiment-20260827-040105-732569-7cb9d4a5` and transient child
  `mcp-experiment-20260827-040105-735334-845f9681` each retain 32 RAW files and
  32 run manifests with zero errors
- Cross-platform evidence `optimization-comparison-e0df542a44aa096a` reports
  zero exact or objective mismatches, identical candidate classifications,
  Pareto membership, and candidate-15 selection. Maximum observed deltas are
  approximately 1.1e-14 dB and 25.72 ns, both within the frozen tolerances

#### Phase 4C: Local refinement and richer design domains

- Add bounded integer and categorical domains plus generated E-series ranges
- Refine only feasible Pareto neighborhoods while retaining parent/child
  provenance and strict global evaluation budgets
- Reject duplicate, out-of-domain, non-finite, or electrically invalid
  candidates before they can distort the optimizer state

#### Phase 4D: Robust selection proof and reporting

- Re-run finalists through Phase 3 named corners and deterministic Monte Carlo
  yield analysis rather than treating nominal optimization as design proof
- Add human-first Pareto, constraint-margin, sensitivity, and selection-rationale
  reports with direct links to candidate RAW, JSON, CSV, and manifest evidence
- Index and compare optimization studies, document the DAQ design decision, and
  close Phase 4 with local, macOS CI, and real Windows LTspice verification

Windows acceptance criteria:

- Serialize optimizer state so a search can move between macOS and Windows.
- Compare final candidates and constraint results across both LTspice
  installations before declaring the workflow portable.

### Phase 5: Flagship portable mixed-signal DAQ/scope

Develop a portable USB 3.x DAQ/scope with an initial target of approximately
1 MHz analog bandwidth. Exact channel count, resolution, sample rate, input
range, digital-channel count, and form factor will be frozen during system
architecture rather than assumed here.

A provisional acquisition rate of roughly 10-20 MS/s per analog channel is a
reasonable investigation range for preserving useful detail above a 1 MHz
analog bandwidth, but it is not yet a committed specification. USB 3.x is
intended to support continuous multi-channel streaming, digital capture, deep
acquisitions, low latency, and future growth rather than merely the minimum
bandwidth of one analog channel.

#### Hardware workstreams

1. **Analog input and protection**
   - Selectable attenuation and input ranges
   - AC/DC coupling, biasing, impedance, and overvoltage protection
   - Low-noise buffer/driver and anti-alias filtering
   - Recovery from overload without damaging downstream circuitry

2. **Acquisition path**
   - ADC resolution, sample-rate, channel-skew, aperture-jitter, and input-drive
     requirements
   - Clock generation/distribution and jitter budget
   - Analog and digital trigger paths
   - Digital/logic channels and threshold behavior

3. **Processing and transport**
   - FPGA, programmable logic, or MCU partitioning
   - Triggering, buffering, decimation, timestamping, and packetization
   - USB 3.x bridge/device implementation and sustained-transfer strategy

4. **Calibration and self-test**
   - Offset, gain, timebase, inter-channel skew, and frequency-response
     calibration
   - Stored calibration data with version and temperature context
   - Loopback or reference-source paths for production and field self-test

5. **Physical implementation**
   - Power integrity, grounding, partitioning, shielding, and thermal behavior
   - PCB parasitic extraction/back-annotation for sensitive analog and clock
     paths
   - Prototype correlation against oscilloscope, signal-generator, and VNA data

#### Software workstreams

- Firmware/FPGA acquisition and trigger pipeline
- A versioned device protocol with recoverable streaming and diagnostics
- A Windows-first host library and application
- Device discovery, configuration, live plotting, recording, export, and
  scripted automation
- Reproducible test-vector playback for host development without hardware
- Optional cross-platform host support without weakening Windows verification

#### Simulation and verification boundary

LTspice will model and verify the analog front end, protection, ADC drive and
loading, anti-alias response, noise contributors, clock-jitter approximations,
power integrity, calibration networks, and behavioral mixed-signal interfaces.
It will not attempt transistor-level simulation of the USB protocol, FPGA
fabric, firmware, Windows driver, or desktop application. Those layers require
digital simulation, protocol tests, recorded test vectors, hardware-in-the-loop
tests, and host integration tests.

#### Flagship success criteria

- Requirements are machine-readable and traceable to verification evidence.
- Analog bandwidth, flatness, noise, distortion, input protection, settling,
  and channel isolation meet the frozen specification across corners.
- Statistical analysis reports manufacturing yield and dominant sensitivities.
- Fault injection identifies unsafe or misleading failure modes and detection
  mechanisms.
- Calibration reduces measured prototype errors to their specified limits.
- The device streams continuously and reliably to the Windows host at the
  required aggregate rate.
- Measured prototype waveforms can calibrate models and close the loop between
  hardware and simulation.

## Long-term project portfolio

The following projects remain approved directions. Several become capabilities
used by the DAQ/scope rather than isolated demonstrations.

### 1. Robust circuit design optimizer

Select component and model parameters against multiple performance, cost,
thermal, and manufacturability objectives, then prove the selected design over
corners and tolerance distributions.

### 2. Mixed-signal DAQ/scope design copilot

Use the complete MCP workflow to design, validate, calibrate, and refine the
portable USB 3.x DAQ/scope described above. This replaces the previously
suggested switching-power-supply flagship.

### 3. Statistical yield and worst-case engine

Estimate production yield, locate worst credible corners, rank sensitivities,
and provide reproducible evidence for tolerance and component-selection
decisions.

### 4. Automatic fault-injection and FMEA system

Apply topology-aware opens, shorts, drift, stuck devices, supply faults, load
faults, and sensor errors. Simulate each mutation and produce severity,
detection, affected requirements, and mitigation evidence.

### 5. Lab-to-simulation model calibration

Ingest oscilloscope, VNA, curve-tracer, or DAQ data and fit physically bounded
model/parasitic parameters. Preserve alignment choices, residuals, uncertainty,
and validation data so a good numeric fit is not mistaken for a valid model.

### 6. PCB parasitic back-annotation loop

Import or estimate trace, via, plane, package, and placement parasitics; generate
an LTspice-compatible interconnect model; and return layout feedback for signal
integrity, power integrity, ringing, coupling, and analog stability.

### 7. Waveform specification language

Express electrical behavior as reusable, machine-readable temporal and
frequency-domain properties. This is the verification foundation for every
experiment, optimization, calibration, and fault-analysis project.

### 8. Constrained circuit topology synthesis

Explore passive networks, filters, compensation, protection, level shifting,
and small analog/mixed-signal structures through a typed circuit grammar.
Reject floating, unsafe, non-convergent, or model-invalid candidates before
expensive evaluation.

## Additional future extensions

- Regression suites that compare simulator releases and operating systems
- Component/model library provenance and validity-range tracking
- Thermal and safe-operating-area verification
- Automated design-review reports with direct links to evidence
- Hardware-in-the-loop execution and measured/simulated waveform comparison
- Integrations with schematic, PCB, requirements, and CI systems

## Immediate next milestone

Begin Phase 4A with the smallest complete deterministic optimization slice:

1. Freeze a versioned candidate-plan schema that records optimizer state,
   objectives, hard constraints, parameter domains, and reproducibility data.
2. Generate a bounded coarse mixed-signal DAQ candidate population containing
   continuous and explicit preferred-value-series component choices without
   invoking a second simulation runner. Keep Sallen-Key only as a small unit
   fixture.
3. Evaluate candidates through the existing experiment and requirement paths,
   keeping electrical constraint failures separate from simulation or analysis
   errors.
4. Produce a traceable Pareto report and an explicit explanation for one
   selected candidate; do not add local refinement until the coarse-search
   evidence contract is stable.
5. Stop Phase 4A after local real-LTspice DAQ qualification. Serialize, resume,
   and compare the same candidate plan on macOS and Windows in Phase 4B before
   adding refinement or richer parameter types.
