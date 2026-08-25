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

#### Phase 3C: Correlation, measured populations, and operating corners — in progress

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

##### Phase 3C-2: Empirical measured populations

- Add empirical distributions from inline values and confined CSV inputs,
  including column, unit, resampling, and source-hash provenance.

##### Phase 3C-3: Named operating corners

- Support temperature, supply, load, and finite device-model corners as named
  deterministic axes around a statistical sample plan.
- Report each corner separately as well as an explicitly defined aggregate;
  do not hide a weak corner inside a global average.

##### Phase 3C-4: Stratified and low-discrepancy sampling

- Add stratified Latin-hypercube and low-discrepancy sampling as versioned plan
  generators. Native LTspice stepping remains an execution optimization only
  when the resulting evidence can be mapped back to every planned point.

##### Phase 3C-5: Integration and verification

- Extend MCP, JSON/CSV, reports, documentation, and macOS/Windows regression
  coverage across correlation, empirical populations, corners, and samplers.

Verification gate:

- Fixture correlations and empirical frequencies match expected tolerances.
- Corner-by-sample cardinality, ordering, resume, and provenance are stable on
  macOS and Windows.
- A deliberately weak Sallen-Key corner fails while stronger corners remain
  independently visible in the structured results.

#### Phase 3D: Sensitivity and worst-credible-case analysis

- Rank input influence using documented global rank-correlation measures and
  local one-at-a-time perturbations; state when the data are insufficient or
  the relationship is not meaningfully monotonic.
- Generate tornado data from controlled perturbations with units, baselines,
  and requirement margins rather than chart-only values.
- Enumerate declared finite corners exactly and rank observed samples by
  requirement margin to identify the worst evidenced cases.
- Add deterministic, batched adaptive sampling near observed failure
  boundaries, with stopping rules, sample budgets, and confidence history.
- Keep continuous design optimization and Pareto selection in Phase 4; Phase 3
  may characterize statistical risk but must not silently redesign a circuit.

Verification gate:

- Synthetic monotonic and non-monotonic fixtures exercise honest sensitivity
  reporting.
- Known worst finite corners are recovered exactly, including ties.
- Adaptive studies resume at a batch boundary and reproduce their full sample
  and confidence history from the same definition and seed.

#### Phase 3E: Statistical reports, indexing, and hardening

- Extend the offline HTML report and dashboard with yield summaries,
  confidence history, distributions, failed-sample tables, corner matrices,
  sensitivity rankings, and tornado plots.
- Keep charts display-only: every conclusion must remain available in bounded
  JSON/CSV and link to full-resolution simulation evidence.
- Index statistical definitions and summaries for queries by circuit, status,
  yield, confidence bound, corner, variable, and requirement.
- Add content-addressed comparison of two compatible statistical studies,
  distinguishing changed sample plans from changed circuit outcomes.
- Enforce bounded sample counts, corner expansion, imported data, report
  payloads, runtime, and disk use; malformed studies must be isolated during
  index and dashboard rebuilds.
- Retire the standalone Monte Carlo example in favor of a documented example
  built on the production statistical API.

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

Initial validation will optimize the existing Sallen-Key filter for cutoff,
Q, peaking, step response, component values, and tolerance yield.

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

Begin Phase 3B with the smallest complete durable-yield slice:

1. Define a statistical-study artifact that binds an immutable point plan to
   an experiment definition and its requirements.
2. Execute the saved plan through the durable Phase 2 worker without redrawing
   samples during cancellation or resume.
3. Separate simulation/analysis errors from electrical requirement failures.
4. Produce observed yield and a Wilson binomial confidence interval with
   hand-calculated fixtures.
5. Validate one resumable Sallen-Key tolerance study on macOS and Windows.
