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

Install the optional GUI dependencies and launch the loopback-only application:

```bash
python3 -m pip install -r requirements-gui.txt
make system-builder
```

On Windows, the no-admin first-start path creates the local environment and
performs Python/LTspice diagnostics automatically:

```powershell
.\Start-SystemBuilder.cmd
```

See [Windows setup and qualification](WINDOWS.md#start-system-builder) for
first-run behavior, an explicit workspace example, and troubleshooting.

The application opens the default browser on a random `127.0.0.1` port. The
bundled reference recipe is
[`examples/mixed_signal_daq.ltstudy.json`](../examples/mixed_signal_daq.ltstudy.json).

## Study definition and immutable plans

System Builder can load or save a portable `.ltstudy.json` recipe; edit sampling
controls, manufacturing variables, correlations, operating corners, and
waveform requirements; and preview the exact future plan identity, corner
expansion, and LTspice run count. Changes are validated automatically against
the existing engine, with field-scoped errors.

Preview is pure: it does not publish a plan, create a run directory, or start
LTspice. After a valid preview, **Create immutable plan** publishes the
content-addressed plan but still does not run LTspice. A separate acknowledgement
exposes **Start local qualification**, which launches the recipe's paired
analyses through the same durable experiment manager used by MCP. Editing the
recipe invalidates the confirmation, and repeated Start requests cannot
duplicate the launch.

Engineering-unit selectors are available for capacitance (`pF`, `nF`, `µF`)
and resistance (`Ω`, `kΩ`, `MΩ`). These are display and entry choices only: the
portable recipe and immutable plan retain canonical SI values, so changing a
selector without changing the physical value preserves the plan identity.

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

## Remote execution preview

After freezing a statistical study, **Preview GitHub workload** binds that exact
plan ID, complete plan SHA-256, recipe SHA-256, point count, and run count to a
proposed GitHub repository and ref. The resulting preview has its own
content-derived ID and SHA-256 and forecasts the Windows runner, candidate
workflow, seven-day retention, and expected RAW, log, manifest, JSON, CSV, and
HTML evidence.

This is a local calculation only. It creates no file, launches no process,
makes no network request, and neither requests nor stores a credential. The
candidate real-LTspice workflow still requires the GUI-D4 frozen-plan input
contract before it can accept a System Builder dispatch. A changed recipe
invalidates the frozen launch and its remote preview; a plan already started
locally cannot be newly previewed for remote dispatch.

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
constraints; weights; targets; and metric arguments.

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
resume controls. Refreshing the page rediscovers the durable optimization job
but never launches or resumes it automatically.

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
completed view places joint corner yield, Wilson confidence intervals, worst
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
durable engine and resumes automatic report processing. Remote execution remains
disabled.

See the [MCP reference](MCP_REFERENCE.md) for the parallel agent-facing tool
contracts and [ROADMAP.md](../ROADMAP.md) for planned GUI-D packaging and gated
remote execution.
