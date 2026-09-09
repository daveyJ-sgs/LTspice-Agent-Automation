# Audit fixes — September 7, 2026

The follow-up review and additional fixes are documented in
[the September 8 second pass](AUDIT_SECOND_PASS.md).

All 12 findings from the September 6 audit are addressed. These changes were
validated locally on macOS 26.3.1 arm64, Python 3.14.3, LTspice 17.2.4.
Native Windows validation has not been rerun. The repository CI targets Python
3.13 on macOS and Windows; the real-simulator qualification remains opt-in.

| Finding | Correction | Regression coverage |
| --- | --- | --- |
| 1. Adaptive-step mean/RMS | Integrate elapsed time and the exact square of each linear waveform segment; reject zero-duration/non-increasing axes. | Irregular-grid ramp, resampling invariance, window endpoints, signed ramp, zero signal; real LTspice ramp. |
| 2. Non-project deletion | Reject hidden/reserved folders and require a project recipe before recursive deletion. | Direct deletion and authenticated HTTP DELETE preserve fake `.git`, `.venv`, and unrelated directories. |
| 3. Qualification retention | Protect child experiments referenced by `qualification_job.json`. | Prune planning and apply-time reference revalidation. |
| 4. Pareto dominance cycles | Require every objective to be no worse, with at least one improvement beyond tolerance. | Three-objective cycle fixture evaluated through the study engine; real coarse/refinement workflows. |
| 5. Unsupported confidence claims | Wilson intervals apply only to independent samples. Halton, Latin-hypercube, and pooled repeated-corner bounds are unavailable with an explicit reason. | Statistics, confidence search filters, robust selection, HTML report and live browser display. |
| 6. Descending DC | Infer sweep direction before detecting axis resets. | ASCII/binary single and stepped descending sweeps; real descending DC. |
| 7. Relative study dependencies | Inline bounded, workspace-confined nested includes and selected library sections before source context is discarded. | Nested UTF-16/CRLF libraries, changed dependency hashes, cycles, missing/escaped/symlinked/oversized files; real text-based study execution. |
| 8. External report evidence | Accept an explicit asset workspace; bundled DAQ workflows pass the checkout root. | External-workspace report test and full DAQ/optimization/qualification runs outside the checkout, without asset-copy workarounds. |
| 9. UTF-16 decks | Use the shared decoder before wrapper staging. | UTF-8 and UTF-16 variants plus real UTF-16 simulation. |
| 10. Rising cutoff | Search below the passband reference for rising crossings and above it for falling crossings. | High-pass and existing low-pass cutoff regressions. |
| 11. Launch failures | Persist failed status, error, finish time and duration after process-launch `OSError`. | Permission-denied and missing-executable launch fixtures. |
| 12. Strict margins | Retain signed margins and actual pass/fail for `<`, `<=`, `>`, and `>=`. | All four operators retain failing qualification evidence. |

## Decision changes and stored evidence

Optimization result generator `pareto-evidence-v4` and robust-selection generator
`joint-ac-transient-selection-v2` distinguish corrected decisions from old
artifacts. Re-evaluate older decisions affected by these findings; changing only
a saved version field would misrepresent the evidence.

The corrected DAQ coarse study still has 14 feasible and 2 constraint-failed
candidates. Its frontier contains 9 candidates and selects candidate **3**,
replacing candidate 15. Objective measurements match the earlier baseline;
frontier membership and selection change because the dominance rule changes.
Exact no-worse comparisons can change frontier membership across platforms;
objective tolerances alone cannot guarantee an identical decision.

The qualification example now defaults to the source studies' verified selected
candidates instead of hardcoded historical indices. Explicit candidate overrides
remain available. Refinement evaluates 8 candidates, with 7 feasible, and selects
candidate **2**. Paired AC/transient Halton qualification yields **30/32 at each
corner** for coarse candidate 3 and **32/32 at each corner** for refined candidate
2; the final selection is **refined-finalist**. These are observed sample results,
not independently established population-confidence bounds.

The phase 4B and 4D macOS baseline fixtures were regenerated from real corrected
runs, then verified against separate reruns. Both comparisons pass with zero
exact or numeric/objective mismatches. Windows-labelled fields in the existing
qualification helpers do not turn these local macOS reruns into Windows evidence.

## Validation

- **406 unit/integration tests passed**, with no reported skips.
- Makefile Ruff and mypy gates passed; the broader CI Ruff file list also passed.
  Mypy covers its three configured modules, not the entire repository.
- MCP stdio initialize/list/call smoke passed.
- Real adaptive ramp: mean **0.4999999964 V**, RMS **0.5773502650 V**;
  analytic values are 0.5 V and 1/sqrt(3) V. LTspice `.meas` reports 0.5 V and
  0.577343 V, with its own numerical integration approximation.
- Real descending DC is one three-point sweep. Nested-include study execution
  and direct UTF-16 execution both complete.
- Full DAQ study: **48/48 electrical points pass**, including report generation.
- Coarse optimization: **64 simulation runs** complete and its comparison passes.
- Refinement and **256 paired qualification simulation runs** complete; robust
  comparison passes against the refreshed baseline.
- Live System Builder renderer loaded actual corrected corner evidence and
  displayed the unavailable-confidence reason; browser error collection was empty.
  This was a targeted renderer check, not a complete qualification UI journey.

Reproduce the automated checks from the checkout:

```sh
MPLCONFIGDIR=/private/tmp/ltspice-mpl make PYTHON=.venv/bin/python test lint typecheck
PYTHONPATH=. .venv/bin/python tests/stdio_mcp_client_smoke.py
PYTHONPATH=. REAL_LTSPICE_DAQ_EVIDENCE_DIR=/private/tmp/ltspice-fixed-daq .venv/bin/python tests/real_ltspice_daq.py
PYTHONPATH=. REAL_LTSPICE_OPTIMIZATION_EVIDENCE_DIR=/private/tmp/ltspice-fixed-optimization .venv/bin/python tests/real_ltspice_optimization.py
PYTHONPATH=. REAL_LTSPICE_OPTIMIZATION_EVIDENCE_DIR=/private/tmp/ltspice-fixed-optimization .venv/bin/python tests/real_ltspice_robust_selection.py
```

Local verification artifacts are under `/private/tmp/ltspice-fixes-20260907`;
logs are `/private/tmp/ltspice-fixes-*.log`. These temporary artifacts are not
part of the repository. Existing untracked `projects/` was preserved. No commit,
push, remote workflow dispatch, or hardware migration was performed.
