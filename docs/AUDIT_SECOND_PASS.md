# Second audit pass — September 8, 2026

This pass reviewed the existing fixes in place and checked additional boundaries
across the automation repository. Eleven further defect groups were confirmed
and fixed locally. This is evidence of improved coverage, not proof that every
possible defect has been eliminated. The first pass remains documented in
[AUDIT_FIXES.md](AUDIT_FIXES.md).

## Confirmed defects and corrections

| ID | Priority | Reproducer and consequence | Correction and regression coverage |
| --- | --- | --- | --- |
| A01 | High | `delete_project(..., "EXAMPLES")` bypassed the reserved-folder check. On case-insensitive filesystems this can delete the actual examples directory. | Case-insensitive reserved-name checks in deletion and discovery; tests preserve reserved folders containing recipes. |
| A02 | High | A pre-existing symlink at a predictable `.tmp` filename caused saving to overwrite its target. Concurrent saves also shared that temporary path. | Exclusive creation of unique temporary files for recipe saves, netlist saves, simulator settings, and capture metadata. Four regressions verify sentinel files are preserved and saved files remain regular files. |
| A03 | Medium | Two concurrent `write_once` calls could both pass the existence check and replace one another with different content. | Atomically publish the completed temporary file without replacement using a hard link. A synchronized two-writer regression verifies one winner, one conflict, preserved winner content, and temporary-file cleanup. |
| A04 | Medium | Netlist import checked existence and then used an overwriting write; a file created between those operations was lost. | Open the destination exclusively and report an existing-file error; regression inserts a competing creation at the boundary. |
| A05 | High | Duplicate RAW vector names merged unrelated samples. Invalid dimensions/indexes and extra ASCII rows were accepted. Some truncated double-precision payloads were silently interpreted as compact data. | Validate positive dimensions, consecutive indexes, unique vector names, exact ASCII row counts, and exact supported binary payload lengths. Malformed fixtures now fail with `ValueError`. |
| A06 | Medium | A real three-point stepped `.op` result was treated as one sweep. Its log omitted `.step` lines, so native batch execution also rejected otherwise valid evidence. | Recognize single-point operating-point blocks; verify the generated step identity against the RAW axis when the log omits it. Ordered and scrambled fixtures plus real binary/ASCII native batches verify mapping and rejection of wrong order. |
| A07 | High | Scalar and legacy stepped log parsing could convert `1.#INF` to `1`; incomplete exponent tokens could also become valid prefixes. Overflowed scalar values were accepted as infinity. | Require complete numeric tokens, reject non-finite values, and route legacy stepped reads through the strict row parser. Regressions cover malformed tokens and overflow. Valid scalar results in 117 real logs were unchanged. |
| A08 | Medium | A singleton or zero-width analysis window caused `spectral_peak` to divide by zero before validating duration. | Reject zero duration before computing frequency resolution; both forms have regression coverage. |
| A09 | High | An interrupted experiment from before the numerical corrections could resume using old checkpoints alongside newly calculated results. | New durable jobs use engine version 2. Recovery rejects version-1 execution before calling a point executor. Indexing still accepts completed historical version-1 evidence. |
| A10 | Medium | The legacy REST bridge accepted foreign `Host` and `Origin` headers despite being intended for local access. | Validate loopback hosts and, when supplied, same-origin headers before reads or job submission. HTTP regressions verify rejection before the job manager is called; ordinary local clients still pass. |
| A11 | Medium | Project recipe reads bypassed the bounded finite-JSON loader. Arrays appeared valid in discovery; non-finite JSON could reach response serialization; oversized files were read without the recipe budget. | Reuse the recipe loader for discovery and reads, and disallow non-finite JSON on save. Regression covers arrays, NaN, infinity, oversize, and preservation of an existing recipe after rejected save. |

## Coverage and verification

Source review covered wrapper staging/cache/process handling, RAW and LOG parsing,
waveform and frequency metrics, sampling/correlation and confidence assumptions,
Pareto selection and comparisons, durable execution/recovery, retention,
project/netlist writes, reports/indexing, REST/MCP and System Builder routes,
frontend unit conversion and result rendering, remote execution contracts, and
launcher/package/CI configuration. No browser presentation changes were made in
this pass. System Builder routes were exercised through their integration tests.

Verified on macOS 26.3.1 arm64, Python 3.14.3, LTspice 17.2.4:

- **421 unit/integration tests passed**, with no skips reported.
- Makefile lint and configured mypy checks passed; the complete CI Ruff file list
  also passed. Mypy covers its three configured modules.
- Repository-wide undefined-name/syntax checks passed, as did syntax checks for
  both frontend scripts and `pip check` for installed dependency consistency.
- MCP stdio initialize/list/call smoke passed.
- Real RC smoke and nested relative-include execution passed.
- Real DAQ study: **48/48 electrical points passed**, with reports generated.
- Real coarse optimization: **64 simulation runs completed**; baseline comparison
  had zero exact/objective mismatches, with 9 Pareto candidates and candidate 3
  selected.
- Real refinement and **256 paired qualification simulation runs** completed;
  robust comparison passed with zero exact/numeric mismatches and selected
  `refined-finalist` again.
- Real binary and ASCII native operating-point batches: three correctly mapped
  points each. A separate 100-case analytic ramp probe confirmed corrected mean
  and RMS under irregular sample placement and signed endpoint values.

## Stored evidence and limitations

Define a new experiment to replace an unfinished version-1 job; do not relabel
old checkpoints as version 2. Completed historical results remain historical:
reading them does not recompute their measurements. The earlier selection
baselines were not changed during this pass.

Native Windows simulator, launcher, and packaged-build execution were not run
here. Tests of portable contracts on macOS do not establish Windows acceptance.
Hard-link publication also needs the destination filesystem to support hard
links; unsupported filesystems fail rather than falling back to an overwriting
publication. Dependency consistency was checked, not a complete advisory scan.

Time-domain integration is exact for the piecewise-linear captured waveform;
it cannot recover behavior absent from the captured points. Spectral integration
remains a numerical approximation, and settling time remains a conservative
first-recorded-in-band estimate. Circuit model fidelity and physical hardware
performance are separate from software correctness.

Reproduce the main checks from the checkout:

```sh
MPLCONFIGDIR=/private/tmp/ltspice-mpl make PYTHON=.venv/bin/python test lint typecheck
PYTHONPATH=. .venv/bin/python tests/stdio_mcp_client_smoke.py
PYTHONPATH=. REAL_LTSPICE_EVIDENCE_DIR=/private/tmp/second-audit-smoke .venv/bin/python tests/real_ltspice_smoke.py
PYTHONPATH=. REAL_LTSPICE_DAQ_EVIDENCE_DIR=/private/tmp/second-audit-daq .venv/bin/python tests/real_ltspice_daq.py
PYTHONPATH=. REAL_LTSPICE_OPTIMIZATION_EVIDENCE_DIR=/private/tmp/second-audit-optimization .venv/bin/python tests/real_ltspice_optimization.py
PYTHONPATH=. REAL_LTSPICE_OPTIMIZATION_EVIDENCE_DIR=/private/tmp/second-audit-optimization .venv/bin/python tests/real_ltspice_robust_selection.py
```

Use new output directories for fresh simulations. This pass's evidence is under
`/private/tmp/ltspice-second-pass-20260908`; logs are
`/private/tmp/ltspice-second-pass-*.log`. They are temporary local evidence,
not committed fixtures. Earlier local changes and `projects/` were preserved.
No commit, push, or remote workflow dispatch was performed.
