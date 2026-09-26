# LTspice System Builder

LTspice System Builder is the browser-first human interface to the same
deterministic study, optimization, and qualification contracts exposed through
the MCP server. It runs locally, keeps LTspice execution behind explicit
confirmation, and produces the same immutable plans and portable evidence as
agent-driven workflows.

Windows users who do not need the Python development environment can use the
verified ZIP application bundle. See [Windows setup and qualification](WINDOWS.md)
for the packaged and source-launcher paths.

## Install and launch

**Windows:** skip the `make`/`python3` commands below — `make` is not a
standard Windows tool and `python3` commonly resolves to the Microsoft Store
alias stub instead of a real interpreter, so that path fails on a stock
Windows machine. Use the no-admin launcher instead, which creates the local
environment and runs Python/LTspice diagnostics automatically:

```powershell
.\Start-SystemBuilder.cmd
```

Pass `-Workspace` to point it at a folder of your own circuit projects instead
of the repository's dogfooding examples:

```powershell
.\Start-SystemBuilder.cmd -Workspace 'C:\Users\Dave\Documents\LTspice Projects'
```

See [Windows setup and qualification](WINDOWS.md#start-system-builder) for
first-run behavior and troubleshooting.

**macOS/Linux with `make` installed** — install the optional GUI dependencies
and launch the loopback-only application:

```bash
python3 -m pip install -r requirements-gui.txt
make system-builder
```

**macOS (no-admin alternative):** double-click `Start-SystemBuilder.command`
(or run it from a terminal). It creates the private `.venv`, installs the GUI
dependencies, reports LTspice in `/Applications` or `~/Applications`, and
opens the same loopback-only application. No administrator access or
machine-wide changes. Pass `--workspace` the same way to point it at your own
projects folder (`--workspace=PATH` and `--workspace PATH` both work):

```bash
./Start-SystemBuilder.command --workspace=/Users/dave/Documents/LTspice/projects
```

If Gatekeeper blocks the first double-click of a ZIP download, choose
**Open Anyway** in **System Settings > Privacy & Security**, or run the
launcher once from Terminal.

The application opens the default browser on a random `127.0.0.1` port. A
repository-root development launch can load the bundled advanced reference
recipe at
[`examples/mixed_signal_daq.ltstudy.json`](../examples/mixed_signal_daq.ltstudy.json).

## Projects, starters, and navigation

System Builder distinguishes the **workspace** selected at launch from the
**projects** inside it. A project is a first-level workspace folder containing
a `.ltstudy.json` or `.ltopt.json` recipe. The Projects view lists those
folders and can create, open, save, and delete them without restarting System
Builder. The enclosing workspace remains fixed for the life of the process;
working in a different workspace still requires relaunching with `--workspace`
or `-Workspace`.

When launched against a user workspace, System Builder adds each missing
worked example without overwriting an existing folder:

- `rc-lowpass-starter` demonstrates a compact tolerance study.
- `instrumentation-amp-starter` demonstrates a classic three-op-amp
  instrumentation amplifier with a real LTspice-compatible schematic and
  self-contained batch netlist.

**New project** creates an intentionally incomplete recipe shell rather than a
fake circuit. Select an existing `.cir` or `.net` file from the netlist picker,
use **Import netlist** to copy one into the open project, or edit the selected
netlist in the built-in editor. Editor text is kept until it is saved:
rescanning netlists, importing, or restructuring the recipe does not discard
it, leaving the page or opening another project asks first, and
**Simulate once** (which runs the file on disk) offers to save pending edits.
Opening a project attaches its recipe to the editor, so **Save to project**
writes changes back atomically; loading an unrelated recipe detaches that
association. Project deletion uses an explicit two-step confirmation; deleting
the project that is open keeps its recipe in the editor, detached, with a
warning, so Save downloads it instead of writing to the missing folder.

The sidebar separates Projects, Study, Optimization, Qualification, Workspace,
Guide, and FAQ; below about 900px wide it collapses into a top bar with a
Menu drawer. Browser Back and Forward follow that navigation, and the app
warns before an action would discard unsaved Study or Optimization edits. The
theme control offers the dark **Solder Mask**, light **Copper Print**, and
schematic-inspired **Wire & Grid** themes; the selection is stored only in the
local browser.

## Study definition and immutable plans

System Builder can load or save a portable `.ltstudy.json` recipe; edit sampling
controls, manufacturing variables, correlations, operating corners, and
waveform requirements; and preview the exact future plan identity, corner
expansion, and LTspice run count. Changes are validated automatically against
the existing engine, with field-scoped errors.

Preview is pure: it does not publish a plan, create a run directory, or start
LTspice. After a valid preview, **Create immutable plan** publishes the
content-addressed plan but still does not run LTspice. A separate acknowledgement
exposes **Start local study**, which launches the recipe's paired analyses
through the same durable experiment manager used by MCP. Editing the recipe
invalidates the confirmation, a freeze response that arrives after an edit is
discarded, and repeated Start requests cannot duplicate the launch. While the
LTspice executable is not found, **Start local study** and **Simulate once**
stay disabled with the reason shown beside them.

Job cards on the Study page belong to the project that launched them. Points
that did not simulate (for example, LTspice exited with an error) are counted
separately from points that simulated and missed a requirement, and the card
shows the first recorded point error.

### Requirement metrics and their parameters

Each requirement picks its metric from a list grouped into AC/frequency-domain
and time-domain metrics, and the editor then shows exactly the parameters that
metric accepts, read from `GET /api/metrics` rather than from a table kept in
the browser. Parameters are written as flat sibling keys of
`metric`/`operator`/`target` inside the requirement, which is the `.ltstudy`
shape; the nested `metric_parameters` object is the separate `.ltopt`
optimization-goal shape and is not interchangeable with it.

The target shows the unit the metric is measured in (dB, Hz, deg, %, or the
analysis signal/axis unit). Changing to a metric measured in a different unit
clears the old target with a note rather than reinterpreting it.

Parameters a metric cannot use are dropped when the metric changes, and a
required one -- `frequency_value` for `ac_gain_db`, `reference_frequency` for
`cutoff_frequency` and `peaking_db`, `threshold_value` for `frequency`, and so
on -- is marked and flagged while empty. The recipe validator rejects a missing
or misspelled parameter during preview, so it surfaces before a run rather than
part-way through one.

Frequency-valued fields show the parent experiment's `.AC` sweep range, parsed
from its netlist, and flag a value outside it. Those values are read by
interpolation in log frequency between the two bracketing simulated points, not
snapped to the nearest one, which the field's help text states. Entries may use
SPICE magnitude suffixes (`50k`, `1Meg`); the recipe stores the resolved number
and the field reports what it resolved to.

Engineering-unit selectors are available for variables and corner axes whose
unit is capacitance (`F`: `pF`, `nF`, `µF`) or resistance (`ohm`: `Ω`, `kΩ`,
`MΩ`); other units are edited as free text. In the variable table the unit
sits beside the values it scales. These are display and entry choices only: the
portable recipe and immutable plan retain canonical SI values, so changing a
selector without changing the physical value preserves the plan identity. The
Gaussian spread column is σ, one standard deviation in the variable's unit,
with its ±% of nominal shown beneath it.

Dedicated editors support weighted discrete choices and empirical populations.
Empirical data may be entered inline or loaded from a named column in a
workspace-confined CSV file. Correlation groups expose their Gaussian members
and symmetric matrix directly; diagonal values remain fixed at one, and
variables that become non-Gaussian are removed from their groups. The engine
remains authoritative for normalization and positive-definite matrix
validation.

The DAQ acceptance test proves that the human-authored recipe and the existing
agent-authored definition produce byte-identical statistical plans. Its default
preview resolves 12 manufacturing samples across two ADC-load corners into 24
points and two paired experiments: 48 prospective LTspice runs.

### Execution, corners, and study identity

Parallelism (up to four concurrent LTspice runs) and simulation cache reuse are
editable beside the sampling controls rather than fixed when the project was
scaffolded. `corner_aggregate` is a checkbox over the corner axes, and
disappears with the last axis because the engine rejects it without them. The
study's name, description, and the report's narrative fields live in a "Study
identity and report narrative" block.

## Waveforms, comparison, and history

Every finished job offers **Waveforms**, which plots the `.raw` captures the run
already wrote: a capture picker, per-trace toggles, selectable resolution, a
vertical-scale control, and a CSV export at full resolution. A transient axis
is drawn linearly and a frequency axis in decades. Nothing in the viewer
launches LTspice or writes an artifact.

Traces are grouped and scaled by the unit LTspice records for each vector in
the RAW header. Volts and amperes never share one vertical scale -- a
milliamp trace drawn against a 3.3 V range is a flat line on the baseline, so
each family gets its own axis, drawn left and right. A capture holding a third
family says which one it could not fit rather than flattening it silently.

A capture also opens on one readable family rather than on every node it
holds: voltages where there are any, otherwise the most populous unit, capped
at six traces. **All** and **None** override that.

AC captures are complex, so the viewer plots magnitude, and opens in dB on the
log frequency axis -- a Bode plot. Linear magnitude stays one click away, and
exact gain and phase remain the requirement engine's job. A trace that is
identically zero is negative infinity dB, and the viewer says so instead of
drawing nothing. A single-sample capture, such as an operating point, is drawn
as markers rather than as a line through one point.

**Simulate once** on the netlist editor runs one deck through LTspice without
the define/preview/freeze/acknowledge sequence, for checking that a deck runs
at all, and opens the viewer on whatever it captured.

The history view filters by text, status, and outcome, and **Compare runs**
diffs two completed experiments, reporting requirement regressions and
improvements alongside the portable comparison artifact it writes.

## Studies beyond yield

Three engines that were previously MCP-only are now driven from the browser:

- **Sensitivity** perturbs each manufacturing variable above and below one
  finished sample and draws a tornado of how far every requirement margin
  moves, widest swing first. It answers which component actually matters.
- **Boundary** bisects one variable between a sampled point that passes a check
  and one that fails it, advancing a batch at a time, to find the value a
  requirement turns over at rather than a yield percentage.
- **Robust selection** compares several feasible Pareto finalists under the
  same manufacturing model, instead of qualifying only the winner.

A finished optimization also offers **Refine this study**, which freezes new
candidates in the neighbourhoods it already proved feasible and runs them
through the same paired analyses.

## Remote Windows execution

After freezing a statistical study, **Preview GitHub workload** binds that exact
plan ID, complete plan SHA-256, recipe SHA-256, point count, and run count to a
proposed GitHub repository and ref. The resulting preview has its own
content-derived ID and SHA-256 and forecasts the Windows runner, candidate
workflow, seven-day retention, and expected RAW, log, manifest, JSON, CSV, and
HTML evidence.

Preview is a local calculation only. It creates no file, launches no process,
makes no network request, and neither requests nor stores a credential. A
changed recipe invalidates the frozen launch and its remote preview; a plan
already started locally cannot be newly dispatched remotely.

Dispatch is a separate external-action boundary. System Builder requires all
of the following before enabling it:

1. The recipe has been previewed and frozen into an immutable plan.
2. The exact GitHub workload preview is still current.
3. **Check GitHub access** confirms an existing authenticated `gh` session.
4. The user acknowledges that the complete recipe, resolved netlists, analyses,
   immutable plan, and confirmed run count will leave the computer.

System Builder never reads, receives, logs, or writes the GitHub credential;
GitHub CLI owns it. Install `gh`, authenticate with `gh auth login`, and ensure
the active account can run Actions in the selected repository. Workflow inputs
are not secrets, so proprietary circuit definitions belong in a private
repository.

The submitted envelope is compressed and size-bounded, then verified again on
the Windows runner before LTspice starts. Its content identity covers the full
recipe, immutable plan, and resolved experiment definitions. A dispatch intent
is written locally before the network request and keyed by that identity, so a
retry recovers the matching GitHub run instead of silently launching a
duplicate.

Remote jobs remain visible after a browser or System Builder restart. Status
refresh and evidence download are explicit actions; there is no background
network polling. A successful download is staged outside the durable job,
checked against the runner's evidence manifest and every listed SHA-256, and
only then admitted beneath `runs/remote-jobs/`. Verified HTML reports are linked
from the recovered job card. GitHub retains the downloadable workflow artifact
for seven days; the admitted local copy remains under the normal workspace
retention policy.

GUI-D4 qualification used this path for the default mixed-signal DAQ recipe:
GitHub run `33347518632` installed LTspice 26.0.2 on Windows, completed all 24 AC
and 24 transient points, and returned a 264-file evidence set. System Builder
verified its 15,010,763-byte manifest and every file hash before exposing the
two local HTML report links.

## Durable local execution

Execution is server-owned. The interface follows point-level progress, can
cooperatively cancel a running qualification, and can resume an independent
cancelled job without rerunning completed points. Closing the browser does not
stop the work: the local System Builder process continues the experiments and
automatically produces statistics, worst cases, sensitivities, and the offline
HTML report.

Restarting System Builder recovers queued or running manifests. A completed job
whose report failed can be finalized again from the workspace or job panel.
Each run gets a timestamped directory beneath `runs/` with its `.raw`, `.log`,
measurements, and `run_manifest.json` provenance.

## Schematic preview and native capture

The circuit view is recipe-driven. A study can select an existing workspace
PNG/JPEG or name a companion LTspice `.asc` source and use **Capture from
LTspice**. Native capture opens that exact schematic, zooms it to full extent,
verifies the launched process and document title, and writes a content-addressed
PNG plus provenance JSON beneath `runs/system-builder-assets/`.

System Builder immediately displays the managed image, and the same PNG is
embedded into completed offline reports. macOS capture requires Accessibility
and Screen Recording permission for the terminal application. Windows uses the
local LTspice window through PowerShell and does not transmit the schematic. On
a fresh Windows installation, capture dismisses only positively identified
LTspice change-log and Web Sync panes before recording the validated schematic
window.

## Optimization workspace

The separate optimization workspace is backed directly by the Phase 4 engine.
It edits continuous, integer, categorical, explicit preferred-value, and
generated E6/E12/E24 domains; finite operating corners; Pareto objectives; hard
constraints; weights; targets; and metric arguments. Goal metrics come from
the same list as study requirements, and metric arguments are checked against
the selected metric's parameters -- an argument it does not take, or a missing
one that would leave the goal matching more than one measured result, is
reported in place.

Preview reports domain expansion, candidate/corner/point counts, the AC and
transient run workload, engine ceilings, selection policy, and the exact future
content-addressed plan ID. It does not publish that plan or launch LTspice. The
bundled
[`examples/mixed_signal_daq.ltopt.json`](../examples/mixed_signal_daq.ltopt.json)
resolves to the already-qualified Phase 4B plan
`optimization-plan-2b6f2d62d7ca7c14`: 16 candidates, two ADC-load corners, 32
expanded points, and 64 prospective AC/transient simulations.

**Publish confirmed plan** revalidates the exact recipe hash, plan ID, point
count, and total run count before writing the same content-addressed
optimization plan used by MCP. A separate acknowledgement then defines and
launches the paired DAQ AC/transient work through `OptimizationStudyManager`.
The interface reports candidate and corner structure, per-analysis progress,
aggregate LTspice progress, evaluation state, cooperative cancellation, and
resume controls. Opening or reloading an optimization recipe rediscovers the
most recent durable job for the plan that recipe resolves to -- never another
project's job -- but never launches or resumes it automatically. If a status
read fails, the job card backs off, and after repeated failures shows
"Status unavailable" with a Retry button.

A completed job becomes an engineering decision view without re-evaluating the
study. It shows selected component values, worst-corner objective values, every
hard-constraint margin, feasible/rejected/Pareto counts, and a two-objective
candidate plot. The expandable candidate table states why each rejected design
failed, while links retain access to the portable report, JSON, CSV, and
immutable plan. Visible values use compact engineering units; exact stored
values remain available on hover.

## Tolerance qualification

The selected optimization winner can be carried into the shared statistical and
robust-selection contracts for an explicitly confirmed tolerance proof. Preview
shows the nine-variable manufacturing model, correlations, named ADC-load
corners, deterministic Halton population, and paired AC/transient run cost
without writing artifacts.

Publication freezes the immutable statistical and qualification plans; a second
acknowledgement starts a recoverable job with cancel/resume controls. The
completed view places joint corner yield, sampling-appropriate confidence information, worst
requirement margins, rank sensitivities, failed samples, and portable evidence
beside the nominal optimization decision. Nominal selection and tolerance
qualification remain separate claims.

## Workspace history and evidence

The read-only workspace reads current durable-job progress directly from
experiment manifests, reports whether the rebuildable experiment index is
current, and launches existing experiment, optimization, and robust-selection
HTML reports. Relative links from a report to JSON, CSV, RAW, manifest, and plan
evidence remain available through a session-protected, workspace-confined route.
Refresh never rebuilds an index, resumes a job, or launches LTspice.

## Local security model

System Builder uses a session cookie, same-origin mutation checks, a strict
content-security policy, bounded JSON bodies, and workspace-confined regular
inputs and evidence. It loads no font, stylesheet, script, or telemetry service
from the network. IBM Plex Sans and IBM Plex Mono are bundled locally, and the
compact instrument-style interface uses flat LTspice-red accents with persistent
light and dark themes.

Local jobs write durable manifests and evidence beneath `runs/`. The browser may
be closed while a job runs, but the System Builder process must remain active.
If that process stops, a later launch recovers unfinished jobs through the
durable engine and resumes automatic report processing. GitHub access occurs
only through the explicit remote controls described above; the default session
still performs no remote action.

See the [MCP reference](MCP_REFERENCE.md) for the parallel agent-facing tool
contracts and [ROADMAP.md](../ROADMAP.md) for completed System Builder work and
the next project milestones.
