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

#### Phase 2C: Execution efficiency and reporting — in progress

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

##### Phase 2C-C: Experiment indexing and visualization — in progress

###### Phase 2C-C1: Experiment index and queries — complete

- Rebuildable normalized SQLite index derived from manifests and results
- Schema-v1 synchronous and schema-v2 durable experiment compatibility
- Exact same-point parameter filtering and deterministic pagination
- Relative artifact paths, isolated diagnostics, and atomic replacement
- MCP rebuild/query tools with cross-platform regression coverage

###### Phase 2C-C2: Human experiment reports — planned

- Portable offline HTML experiment reports
- Interactive waveform overlays and structured requirement summaries
- Display-only waveform downsampling with full-resolution evidence links

###### Phase 2C-C3: Comparison visualization and unified dashboard — planned

- Baseline-versus-candidate plots with regression and improvement markers
- Searchable experiment dashboard linked to reports and comparisons

Phase 2A provides synchronous `run_experiment`; Phase 2B adds the durable job
lifecycle without changing that contract. Phase 2C adds execution efficiency
and comparison operations without changing the portable experiment definition.

Windows acceptance criteria:

- Persist and resume job state correctly across process restarts.
- Confirm bounded parallel LTspice execution and cancellation semantics on
  Windows rather than assuming Unix signal behavior.
- Keep experiment definitions and result schemas portable between platforms.

### Phase 3: Statistical yield and worst-case analysis

Turn the current Monte Carlo example into a general tolerance engine supporting:

- Gaussian, uniform, bounded, discrete, and measured distributions
- Correlated component and process variables
- Temperature, supply, load, and device-model corners
- Pseudorandom, Latin-hypercube, and low-discrepancy sampling
- Reproducible seeds and complete sample provenance
- Yield confidence intervals and adaptive sampling near failure boundaries
- Sensitivity rankings and worst-credible-corner search
- Distribution, yield, and tornado plots

Windows acceptance criteria:

- Given an experiment definition and seed, generate the same sample set and
  equivalent statistical conclusions on macOS and Windows.
- Clearly distinguish simulator/platform numeric differences from changes in
  the statistical experiment.

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

The first implementation milestone is Phase 0 followed by the smallest useful
slice of Phase 1:

1. Add direct MCP tests without changing existing tool behavior.
2. Define a structured requirement/result schema.
3. Implement full-resolution minimum, maximum, RMS, peak-to-peak, rise time,
   overshoot, and settling-time metrics.
4. Expose those metrics through one focused MCP analysis tool.
5. Verify identical fixture behavior on macOS and Windows before expanding the
   property set.
