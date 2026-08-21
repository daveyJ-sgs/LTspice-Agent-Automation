# LTspice automation learnings

This document records the reusable findings from developing the automation
bridge. It intentionally describes validation circuits and behavior at a high
level; generated simulation runs and private design files are not part of the
repository.

## Reliable automation boundary

Text netlists (`.cir` and `.net`) are the most reliable and portable LTspice
batch inputs. A schematic can remain the human-facing source, while an agent
generates or exports a text netlist for reproducible execution.

The wrapper invokes LTspice with batch mode and absolute paths. The executable
can be discovered from standard macOS or Windows locations, or supplied with
the `LTSPICE_EXECUTABLE` environment variable. The commonly useful switches
are:

- `-b` for batch execution
- `-ascii` for text-readable raw output
- `-netlist` for schematic-to-netlist conversion where supported

The native Mac build tested during development behaved most consistently with
`-b` for text netlists. `-run` can produce more interactive behavior on some
installations, so it should be validated per target version.

## LTspice 26 on macOS Tahoe

During validation on an Apple Silicon Mac running macOS 26.3.1, LTspice
26.0.2.1 repeatedly aborted during startup when invoked from the command line.
The failure occurred before the netlist was parsed or a simulation began:

- `LTspice -version` exited with `SIGABRT` / exit code `-6`.
- Batch AC and transient runs failed in the same way, without useful LTspice
  simulation logs.
- The installed 26.0.2.1 executable was Intel-only and therefore ran through
  Rosetta on this machine.
- The identical Python wrapper and validation netlists passed after replacing
  it with LTspice 17.2.4, whose executable was a universal x86_64/arm64 build.

This is best treated as an LTspice/macOS/Rosetta compatibility regression, not
as a netlist or wrapper failure. An Analog Devices forum report independently
describes LTspice 26.0.2 failing to execute analyses on macOS Tahoe while
17.2.4 works. Keep 17.2.4 as the validated macOS baseline until a newer LTspice
release is verified on the target OS and architecture.

When changing simulator versions, record the LTspice version, macOS version,
CPU architecture, executable architecture, and a small batch regression result.
This separates simulator startup failures from actual circuit or parser
failures quickly.

## Logs and measurements

LTspice writes `.meas` results to its log. The tested Mac installation emitted
UTF-16LE logs, so the parser tries UTF-16LE before UTF-8. Scalar measurements
and stepped measurement tables can be extracted without opening the GUI.

## Raw waveform files

The parser supports the commonly observed LTspice formats used by AC and
transient workflows:

- complex binary data
- compact real binary data
- double-precision real data
- FastAccess data
- UTF-16LE `Values:` output produced by `-ascii`

The raw format is not a formal public API. Parsers should validate headers,
variable counts, point counts, and expected byte lengths rather than assuming
one layout forever. ASCII output is slower and larger but useful when maximum
inspectability matters.

## Validated workflow capabilities

The project exercises the full pipeline with small, intentionally generic
circuits:

1. An AC RC low-pass validates netlist execution, complex waveform parsing,
   dB measurement extraction, CSV export, and plotting.
2. A transient RC step response validates ASCII waveform parsing, scalar
   voltage measurements, and bounds checks.
3. A native `.step` RC circuit validates multiple parameter blocks from one
   LTspice invocation and stepped measurement parsing.
4. Parameter-sweep and Monte Carlo examples validate repeated runs, stable
   summaries, SQLite history, and pass/fail yield reporting.
5. A target-response search validates iterative simulation and selection of a
   component value against a requested electrical result.
6. An intentionally invalid deck validates failed-run manifests and API error
   reporting.
7. A transistor-level CMOS NAND validates digital truth-table behavior against
   a behavioral reference and exercises propagation-delay measurements.

The resulting data flow is:

```text
netlist generation
    -> LTspice batch simulation
    -> log and raw parsing
    -> checks and measurements
    -> CSV / SQLite / plots / HTML
```

## REST bridge

`api_server.py` exposes a trusted-local service with synchronous and
asynchronous simulation endpoints. It also exposes health, run, and job status
queries. Asynchronous job metadata is persisted in SQLite, so completed jobs
remain queryable after a server restart; jobs interrupted by a restart are
reported as interrupted rather than silently marked successful.

The default bind address is `127.0.0.1`, the default worker count is one, and
there is no authentication. It should not be exposed to a network interface
without adding authentication, authorization, and appropriate sandboxing.

## Reproducibility and diagnostics

Each invocation writes a `run_manifest.json` containing the copied netlist,
SHA-256 netlist hash, exact command, working directory, options, timing,
status, return code, diagnostic output tails, and generated result files.

The static dashboard is derived from manifests rather than from shell history.
Generated runs, databases, plots, raw files, logs, and caches are ignored by
Git so source code and validation circuits remain separate from machine-local
artifacts.

## Schematic compatibility

An agent can generate an `.asc` schematic by emitting LTspice's text schematic
format or by using a netlist-to-schematic converter. That requires a symbol
library/pin map and a layout strategy; the resulting schematic should be
round-tripped back to a netlist and compared for connectivity.

Existing GUI-created `.asc` files can be version- or installation-sensitive in
batch mode. The safe cross-version workflow is to open, update, or export the
schematic in LTspice, then batch the resulting `.net`/`.cir` file.

## Windows portability

The Python parser, checks, API, SQLite history, reporting, and test layers are
platform-neutral. Windows-specific work is primarily executable discovery,
path handling, model-library search paths, and validation against the target
LTspice release. The wrapper supports an explicit executable override so the
same automation commands can be used on both macOS and Windows.

The first Windows smoke test should verify, in order:

1. `LTSPICE_EXECUTABLE` resolves to the intended `LTspice.exe`.
2. A minimal text-netlist batch run creates both `.raw` and `.log` files.
3. The RC AC and transient examples pass their measurement checks.
4. The CMOS NAND example produces the expected truth-table samples and delay
   measurements.
5. A model-library or `.asc` conversion test is run separately, since those
   paths are more installation- and version-sensitive than text netlists.

PowerShell can launch the same examples with Windows path syntax, for example:

```powershell
$env:LTSPICE_EXECUTABLE = 'C:\Program Files\ADI\LTspice\LTspice.exe'
python examples\analyze_rc.py
python examples\analyze_nand.py
```

## Lessons for agent integration

- Prefer structured readback over screenshots or GUI assumptions.
- Treat simulator file formats and vendor APIs as observed interfaces; verify
  headers, return codes, and output files.
- Preserve failed artifacts and manifests so an agent can diagnose rather than
  guess.
- Keep a neutral circuit representation between schematic/PCB tools and the
  simulator when building a larger hardware automation loop.
- Keep simulation services local and authenticated before exposing them to
  other machines.
