# Windows Setup and Qualification

Windows is a first-class target for the wrapper, MCP server, System Builder,
and portable evidence contracts.

## Install LTspice

The baseline suite and RC AC example are verified on Windows 11 with LTspice
26.0.2. Install it with:

```powershell
winget install --id AnalogDevices.LTspice
```

Launch LTspice once from the Start menu and answer the **Anonymously Share
LTspice Usage Data** prompt. Until it is answered, batch mode hangs without
output or an error. See
[`LEARNINGS.md`](../LEARNINGS.md#windows-portability) for the diagnosis.

## Start System Builder

After cloning or downloading this repository, double-click
[`Start-SystemBuilder.cmd`](../Start-SystemBuilder.cmd). The launcher:

1. Finds Python 3.13 or newer, or prints the exact `winget` installation command.
2. Creates a private `.venv` inside the repository when needed.
3. Installs the declared System Builder dependencies into that environment.
4. Finds LTspice in the standard per-user and machine-wide locations.
5. Opens System Builder in the default browser on a random loopback-only port.

It does not request administrator privileges, change machine-wide PowerShell
policy, or enable remote execution. If LTspice is not installed, the launcher
warns and continues: recipe editing and pure plan preview remain available,
while simulation and schematic capture stay unavailable.

PowerShell users can select another circuit workspace explicitly:

```powershell
.\Start-SystemBuilder.ps1 -Workspace 'C:\Users\Dave\Documents\My Circuits'
```

Use `-NoBrowser` for diagnostics or automated startup checks. The launcher
prints the local URL and workspace; open that URL manually if desired. Stop the
server with `Ctrl+C` in the launcher window.

The wrapper checks common install locations, including winget's per-user
default at `%LOCALAPPDATA%\Programs\ADI\LTspice\LTspice.exe`. Set
`LTSPICE_EXECUTABLE` only for a nonstandard installation:

```powershell
$env:LTSPICE_EXECUTABLE = 'C:\Program Files\ADI\LTspice\LTspice.exe'
python ltspice_wrapper.py
```

The `make` targets default to `python3`, which can resolve to the Microsoft Store
alias stub instead of an interpreter. Pass it explicitly:

```powershell
make PYTHON=python test
```

The wrapper uses `subprocess` and `pathlib` rather than shell-specific command
strings. Model-library search paths and representative `.asc` files still need
verification against the target LTspice version.

## Real LTspice GitHub qualification

The opt-in **Real LTspice Windows qualification** workflow performs the same
path on an ephemeral hosted runner without burdening every push. It downloads
the
[official Analog Devices Windows installer](https://www.analog.com/en/resources/design-tools-and-calculators/ltspice-simulator.html),
pins LTspice 26.0.2 and the complete MSI SHA-256, installs silently, explicitly
selects **No** in the first-run usage-data dialog, and executes:

- `tests/real_ltspice_smoke.py`
- `tests/real_ltspice_daq.py`
- `tests/real_ltspice_optimization.py`
- `tests/real_ltspice_robust_selection.py`

Run it manually with:

```bash
gh workflow run ltspice-windows-real.yml --ref main
```

The RC smoke requires a real 501-point RAW file, decoded `.meas` evidence, the
expected cutoff and gain, and a completed run manifest. The DAQ qualification
runs the complete shared 24-point AC and 24-point transient statistical studies
and fails closed unless both ADC-load corners pass without simulation or
analysis errors.

Uploaded evidence preserves the immutable point plan, all primary RAW/log/run
manifests, structured JSON/CSV analyses, and both interactive HTML reports with
their relative links intact. Evidence is retained for seven days.

## Verified qualification results

The first complete DAQ qualification,
[GitHub run 32969899223](https://github.com/daveyJ-sgs/LTspice-Agent-Automation/actions/runs/32969899223),
passed on Windows Server 2025 with LTspice 26.0.2. It completed all 24 AC and 24
transient points with zero invalid evidence and 100% yield in both ADC-load
corners.

Download that run's retained evidence while it is available with:

```bash
gh run download 32969899223
```

Phase 4B
[run 33037990442](https://github.com/daveyJ-sgs/LTspice-Agent-Automation/actions/runs/33037990442)
ran the frozen 16-candidate DAQ optimization as 32 AC and 32 transient points
with zero errors. It selected the same tolerance-aware Pareto design as macOS
and published comparison `optimization-comparison-e0df542a44aa096a` with zero
classification, Pareto, selection, or objective mismatches. The largest
settling-time difference between LTspice 17.2.4 on macOS and 26.0.2 on Windows
was 25.72 ns, inside the plan's explicit 50 ns decision resolution.

Phase 4D
[run 33080244673](https://github.com/daveyJ-sgs/LTspice-Agent-Automation/actions/runs/33080244673)
completed the coarse/refined tolerance proof with 256 primary RAW files and 256
run manifests. Both finalists passed all 32 deterministic manufacturing samples
at both ADC-load corners. Comparison
`robust-selection-comparison-a8327f7e16d468e3` reported zero exact or numeric
mismatches and retained the coarse 65 Ω design on both platforms. The largest
settling difference was 26.25 ns against the declared 50 ns tolerance.

The consent-dialog targeting is relative to verified window bounds rather than
absolute screen coordinates. The workflow intentionally fails closed if a
future LTspice release changes or does not close that window.
