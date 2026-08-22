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
8. A Sallen-Key 2nd-order active low-pass filter (ideal op-amp modeled as a
   fixed-gain E-source, tuned to Q=2) validates resonant AC peaking and
   underdamped step-response ringing against closed-form predictions — a
   circuit a single-pole RC example cannot exercise. It is also the first
   circuit in this project authored as a real, GUI-editable `.asc` schematic
   in addition to its text netlist; see
   [LEARNINGS.md](#schematic-compatibility) for how that schematic was built
   and verified.

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

An agent can generate a real, GUI-editable `.asc` schematic from scratch,
confirmed working end to end with `examples/sallen_key_lowpass.asc`:

- The `.asc` grammar (`SHEET`/`WIRE`/`FLAG`/`SYMBOL`/`SYMATTR`/`TEXT`) is
  simple and line-oriented, but symbol pin geometry is not part of that
  grammar and must not be guessed. Read the actual `.asy` file for each part
  from the local library (`~/Library/Application Support/LTspice/lib/sym/`)
  for its `PIN x y` offsets, and derive rotated placements (`R90`, etc.) from
  those offsets rather than assuming a layout. For `R90`, the confirmed
  transform is `(dx, dy) -> (-dy, dx)` added to the symbol's anchor
  (validated against a resistor/capacitor placement in LTspice's own bundled
  `Butterworth.asc` example before trusting it for original work).
- A 4-terminal dependent source (`e.asy`, SPICE `E`) has two output pins
  (`+`/`-`, vertical in `R0`) and two control pins (`P`/`N`, offset to the
  side). No single 90°-multiple rotation lines all four up on one axis;
  routing a short jog from the control input is normal, matches how LTspice's
  own examples draw dependent sources, and is not a sign of a wrong layout.
- **Verify by opening the file in the LTspice GUI, not by assuming the
  hand-derived coordinates are correct.** `open -a LTspice file.asc` renders
  it; a screenshot (`screencapture`, optionally cropped/zoomed with PIL) lets
  an agent inspect pin-level connectivity precisely — confirming, for
  example, that a dependent source's control input lands on the intended net
  and not a visually-adjacent one.

**`-b` cannot batch-convert an `.asc` schematic to a netlist on this
platform.** Confirmed on LTspice 17.2.4/macOS with both an original `.asc`
and LTspice's own officially bundled `Butterworth.asc` example, run from a
plain writable directory (not just inside `runs/`): `LTspice -b file.asc`
runs headless (no dialog) but fails immediately with `Fatal Error: Multiple
instances of "Flag"` (or `"Symattr"`, depending on file contents) — on a
schematic LTspice's own GUI opens and simulates without complaint. This is a
batch-mode-specific parsing bug, not a netlist-correctness problem.
`-netlist file.asc` (with or without `-b`) is *not* a headless alternative
either — it always opens the interactive GUI (including LTspice's one-time
"Welcome" dialog on a fresh install) and blocks; it must be killed
externally (`osascript -e 'quit app "LTspice"'`) rather than waited out.

The practical, verified workflow: **draft and view `.asc` schematics for
humans and GUI editing; keep simulation on the text netlist.** Open the
schematic in LTspice, use *File > Export netlist* (or re-derive/maintain the
equivalent `.cir` by hand, as this project does) to get the netlist that
`ltspice_wrapper.run_netlist()` actually batches. Do not point the automated
pipeline at an `.asc` file and expect `-b` to netlist it.

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
