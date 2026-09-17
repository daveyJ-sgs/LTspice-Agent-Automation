# Real LTspice platform benchmark

The **Real LTspice platform benchmark** GitHub Actions workflow installs real
LTspice on fresh macOS 26 and Windows Server 2025 hosted runners, then runs the
same RC AC and transient checks through the Python wrapper. It runs both jobs
in parallel on relevant pushes to `main`, or manually via **Run workflow**.
The ordinary **Tests** workflow still runs software tests without LTspice.

## Versions and setup

- macOS: native LTspice 17.2.4, downloaded from the official Analog Devices legacy
  link, verified against a pinned SHA-256 and installed with Apple's installer.
  The installed version is checked. This is the repo's validated Apple Silicon
  baseline; it is an end-of-support release. See [the compatibility findings](../LEARNINGS.md#ltspice-26-on-macos-tahoe).
- Windows: LTspice 26.0.2, using the same pinned MSI and first-run usage-sharing
  opt-out sequence as the existing Windows qualification workflow.
- Both: Python 3.13, identical netlists, compression disabled, no result caching.

Official downloads: [Analog Devices LTspice](https://www.analog.com/en/resources/design-tools-and-calculators/ltspice-simulator.html).
The legacy Mac URL is not versioned: a changed download fails its checksum
instead of silently changing the benchmark baseline.

## What is measured

Each job runs one warmup AC/transient pair followed by five measured pairs.
Every pair must pass the existing real simulator smoke assertions, including
RAW output parsing, gain, cutoff and preservation of transient time steps.
The AC cutoff also gets checked against the analytical RC transfer function.
The example starts at 10 Hz, so its reported cutoff is 3 dB below the gain at
10 Hz (about 21.29 Hz), not the DC-referenced pole of 15.92 Hz.

The job summary and uploaded `benchmark.json` / `benchmark.md` provide:

- Median, minimum and maximum wrapper durations for AC and transient runs.
- Full pair wall time including parsing and smoke assertions.
- Every individual timing, including the excluded warmup.
- Simulator identity, OS, architecture, CPU count, runner image and commit.
- Netlist hashes and the original RAW files, logs and run manifests.

Use the Actions step durations to compare download, installation and first-run
setup separately. Job duration includes other overhead; queue time is separate.
The wrapper timings include process launch and file I/O and are **not pure
solver CPU time**. These tiny circuits primarily measure batch startup overhead.
Different hardware and simulator versions prevent attributing the difference
solely to the OS. Repeat workflow runs before drawing performance conclusions;
larger representative circuits are needed to compare solver throughput.

This workflow qualifies batch simulation, not GUI rendering or native LSB app
packaging. It does not add macOS as an LSB remote-execution backend.

## Full 100 MHz DAQ analog-channel comparison

Choose **Run workflow → workload: daq100** to benchmark the separate
100 MHz DAQ project's full analog channel. The default `rc` workload and
push-triggered RC checks remain quick. The DAQ option runs one warmup and
three measured iterations of each of two circuits: the nominal AC sweep and
a 600 ns, 100 MHz sine transient with a 10 ps maximum step.

[Fixture provenance and numerical acceptance](../tests/fixtures/daq100/README.md)
record the original saved studies and exact model hashes. Models are fetched
from TI and transformed identically on both runners, then checked against the
original project. Circuit settings and numerical shunts are preserved. Every
trace must be finite, engineering checks must pass, and selected numerical
metrics must match the retained reference before timings are reported.

Each simulation has a 180-second timeout. The job has a 30-minute bound.
`benchmark.json`, `samples.json`, RAW files, logs and manifests are uploaded.
The wrapper median includes simulation launch and file handling, while the
separate wall median also includes waveform parsing and numerical validation.
This compares the analog model on the two hosted setups; it does not simulate
the DAQ's digital capture, USB transport, PCB or actual ADC silicon.

The first DAQ Windows run exposed duplicate global `VNSE` / `FEMT` helpers in
the TI FDA and VGA libraries. The portable benchmark uniquely names the FDA
helpers on both platforms without changing equations; original and portable
model hashes are both pinned. See the fixture notes for provenance and the
separate implication for historical noise studies.
