# LTspice-Agent-Automation — Codebase Review

**Reviewed at commit `db73870`** ("Add selected design qualification to System Builder"), 2026-08-29
Scope: 23,513 LOC non-test Python + 15,464 LOC tests, 30 top-level modules
Reviewer: Claude (Opus 5)

> All figures below were measured against `db73870`. Several are moving fast — see
> §2, where the headline metric changed measurably during the review itself.

> **Historical review:** this document preserves the original external review
> and its measurements. The current implementation has moved beyond several
> findings and should be judged by the dated remediation notes below, not by
> treating the original suggested sequence as an active task list.

## Maintainer verification addendum — 2026-08-29

The review was useful, but its canonical-JSON divergence claim did not survive direct
artifact verification. At `db73870`, all identified helpers emit the same compact bytes
for non-pretty output. In particular, `robust_selection` and
`optimization_comparison` explicitly used `separators=(",", ":")`; retained robust
selection plans also contain compact definition hashes. There is therefore no historical
spaced representation to support.

The hardening work deliberately keeps one strict representation: new writes use compact
canonical JSON, and reads accept an artifact only when its exact expected hash matches.
Tests pin compact and pretty bytes, reject a spaced noncanonical hash, and verify retained
plan loadability. No hash-skipping or dual-representation fallback was added.

Remediation status:

- Findings 1 and 3: shared canonicalization, hashing, content IDs, strict reads, and
  write-once publication now live in `artifacts.py`; artifact-producing engines were
  migrated without changing their serialized bytes or identifiers.
- Finding 4: `pyproject.toml`, Ruff, Mypy, and cross-platform CI gates were added at an
  explicit initial ratchet. The flat module layout remains intact to avoid an unrelated
  import migration.
- Finding 6: this is a `unittest` suite, so the proposed pytest `conftest.py` would not
  run. Repeated setup in the busiest suites now uses `tests/support.py`.
- Findings 2 and 5, plus the documentation split, remain intentionally separate work.
  They are larger behavior-preserving refactors and should not be mixed into artifact
  identity hardening.

## Final remediation note — 2026-09-03

The intentionally separate follow-up work is now complete where it was
well-scoped: System Builder's backend routes were split into focused routers,
both waveform metric families use explicit registries without changing their
public signatures, and the README was reduced to an on-ramp backed by focused
documents under `docs/`. The frontend remains substantial and should continue
to be split only along proven feature boundaries rather than through a broad
rewrite.

The original sequence below is retained as review history. In particular, its
`conftest.py` recommendation was superseded by the verified `unittest` solution
in `tests/support.py`; it is not current implementation guidance.

---

## What's working

Stated first because it constrains the recommendations — most of this review is about
protecting these properties, not replacing them.

- **The engine/coupling separation is excellent, and independently verified.** A
  separate exercise this session evaluated porting the toolkit to SIMetrix/SIMPLIS.
  ~16,600 LOC of engines turned out to need *no* edits — `optimization_engine`,
  `statistical_engine`, `worst_case_analysis`, `sensitivity_analysis`,
  `local_sensitivity`, `frequency_domain_metrics`, `optimization_study`, and
  `statistical_results` contain zero SPICE-dialect references. Few codebases this size
  survive that probe.
- **Deliberate API surface.** `statistical_engine` exposes 5 public functions of 42;
  `optimization_engine` 10 of 44; `experiment_index` 7 of 32. Someone thought about this.
- **The provenance model is more rigorous than most commercial EDA tooling** —
  content-addressed artifacts, write-once, atomic `os.replace`, hash-on-read validation,
  explicit generator versions.
- **Test ratio 15,464 : 23,513**, cross-platform CI matrix (macOS + Windows), and real-
  hardware smoke tests correctly separated from unit tests.
- **`LEARNINGS.md` is an underrated asset** — the Rosetta regression and the UTF-16LE
  log-encoding findings are exactly the institutional memory that normally evaporates.

Function-size distribution is also healthier than the outliers suggest: 16% of functions
exceed 50 lines, 4% exceed 150. The problem is concentrated, not systemic.

---

## Findings, ranked

| # | Finding | Evidence @`db73870` | Effort | Collides with GUI work? |
|---|---|---|---|---|
| 1 | `_canonical_json` defined **10×** in **4 variants** | 2 variants emit different bytes → different sha256 | S | No |
| 2 | `create_app` is **1,435 lines / 60 nested handlers** | grew +203 lines, +11 handlers in one commit | M | **Yes — time it** |
| 3 | Plan-artifact lifecycle duplicated across ~8 engines | sha256→plan_id→write-once→validate, hand-rolled | M | No |
| 4 | Flat 30-module root, no package, no tooling config | `PYTHONPATH := .`; no pyproject/ruff/mypy | S | No |
| 5 | `measure_metric` 345 + 313 lines, ~26 keyword params | if/elif dispatch in both metrics modules | M | No |
| 6 | No `conftest.py`; **24 of 29** test files hand-roll tempdirs | `tests/` isn't a package | S | No |

---

## 1. The canonical-JSON divergence — fix first

Ten definitions across the codebase, in four textually distinct variants (grouped by
hash of the function body):

| Variant | Modules |
|---|---|
| `1bc8ab75` | `adaptive_boundary`, `local_sensitivity`, `optimization_engine`, `statistical_engine` |
| `0d15bf95` | `optimization_study`, `optimization_recipe`, `study_recipe` |
| `e31edf7c` | `optimization_comparison`, `robust_selection` |
| `4260e8d1` | `experiment_engine` |

Two of these provably disagree on output bytes:

```python
# compact variant (separators=(",", ":"))
'{"a":[1,{"z":3}],"b":2}'        sha256 = ef45c49dafe619d6…

# default variant (indent=None → json.dumps defaults to (', ', ': '))
'{"a": [1, {"z": 3}], "b": 2}'   sha256 = b736592ea0058d4d…
```

The cause is that `json.dumps` defaults `separators` to `(', ', ': ')` when `indent` is
`None` — so omitting `separators` is *not* equivalent to passing the compact form.

### This is not a live bug

Verified, not assumed: `robust_selection` computes and verifies `definition_hash` using
its own local variant, so it is internally consistent, and plan-ID digests are taken over
file bytes rather than canonicalized objects. Nothing is broken at `db73870`.

### Why fix it anyway

It's a latent hazard, and a badly-placed one. Plan IDs literally embed `digest[:16]`. The
moment a definition is shared across modules — or someone performs the obvious
"deduplicate this helper" refactor — every stored artifact ID silently changes and
integrity checks begin failing against historical data.

In a system whose central claim is durable provenance, that's the wrong landmine to leave
armed. It is also the cheapest finding here to defuse.

**Fix:** one `artifacts.py` exposing `canonical_json`, `digest`, `write_once`,
`read_verified`. Roughly half a day, and it subsumes finding #3.

**Verification:** before/after, re-hash every artifact under `runs/` and assert
byte-identical output. Pick the compact variant (`1bc8ab75`) as canonical — it's already
the plurality, covering 4 of the 10 sites.

---

## 2. The System Builder GUI — the growth rate is the finding

`create_app` spans a single closure containing **60 nested route handlers**. The five
largest:

```
141L  start
113L  optimization_results_payload
 97L  start_optimization
 93L  freeze_optimization
 75L  freeze
```

The absolute number matters less than the trajectory. Measured one hour apart, across a
single commit:

| | `5707d15` | `db73870` | Δ |
|---|---:|---:|---|
| `create_app` lines | 1,232 | **1,435** | +203 |
| nested handlers | 49 | **60** | +11 |

One commit added 203 lines and 11 handlers to one function — 16% growth. That isn't a
style objection; it's a cost curve. The refactor gets more expensive with every commit,
and the duplication is already compounding: **`freeze` and `freeze_optimization` are 59%
textually identical**, with `start` / `start_optimization` following the same pattern.
That is the experiment flow and the optimization flow diverging into two parallel
implementations of one idea.

**Fix:** `APIRouter` modules with `Depends()` for manager injection, rather than closure
capture. Then unify each `X` / `X_optimization` pair behind a study-kind parameter instead
of maintaining both.

**Timing — important.** This is the one finding that will conflict with in-flight work.
Land it as the *first* task after GUI-C4 closes, not as an interrupt. Everything else in
this review is safe to act on concurrently.

---

## 3. Plan-artifact lifecycle duplication

Every engine hand-rolls the same sequence: canonical-JSON the definition → sha256 →
derive `plan_id` from `digest[:16]` → atomic write-once → validate-on-read against the
embedded hash. Present in `adaptive_boundary`, `local_sensitivity`, `optimization_engine`,
`optimization_comparison`, `optimization_study`, `robust_selection`, `statistical_engine`,
and `worst_case_analysis`.

Same root cause as #1, one level up. Extracting `artifacts.py` for #1 and then migrating
each engine onto it resolves both. Do #1 first as a pure no-op refactor, then migrate
engines one per commit so any hash change is bisectable.

---

## 4. Packaging and tooling

The flat root with `export PYTHONPATH := .` works, and it's genuinely *consistent* with
the stdlib-only ethos — that ethos isn't wrong and shouldn't be abandoned.

But a `src/ltspice_agent/` package with a `pyproject.toml` costs nothing philosophically
(zero runtime dependencies either way) and buys real imports, `pip install -e .`, and
somewhere to hang tool config.

Most concretely: the codebase uses `TypedDict` extensively across 17 modules and thorough
type annotations throughout, with **no type checker configured to verify any of it**.
That's unpaid work sitting on the table. Adding `mypy` (or `pyright`) and `ruff` to the
existing CI job is a small diff against a large existing investment.

---

## 5. `measure_metric` × 2

345 lines in `waveform_metrics.py:318`, 313 in `frequency_domain_metrics.py:312`, with
roughly 26 keyword parameters on the waveform signature. The parameter list is doing the
job that a per-metric parameter object should do — most arguments are irrelevant to any
given metric.

**Fix:** a `SUPPORTED_METRICS` registry mapping metric name → `(handler, params_model)`.
Both functions become ~40-line dispatchers, and adding a metric becomes a local change
rather than an edit to a 26-parameter signature.

Note `study_recipe.preview_study_recipe` (354 lines) is the single longest non-GUI
function and likely deserves the same treatment, though I didn't examine it closely.

---

## 6. Test scaffolding

`tests/` is not a package, has no `conftest.py`, and **24 of 29 test files** independently
construct the same tempdir / runs-dir scaffolding. One shared fixture module removes a
substantial amount of duplicated setup and makes the suite easier to extend.

---

## Non-code observation: documentation weight

3,422 lines of Markdown, of which `ROADMAP.md` is 1,461 and `README.md` is 706. The
content is good; the front door is just long. Consider splitting the reference material
into `docs/` and leaving `README.md` as an on-ramp. `LEARNINGS.md` (343 lines) is the
right size and shouldn't change.

---

## Suggested sequence

Given GUI work is in flight:

1. **#1 `artifacts.py`** — isolated, no GUI collision, defuses the silent-failure risk
2. **#6 `conftest.py`** — isolated, makes every subsequent refactor easier to test
3. **#4 packaging + ruff/mypy in CI** — catches regressions during #3 and #2
4. **#2 GUI router split** — *first task after GUI-C4 closes*, before the curve steepens
5. **#3 migrate engines onto `artifacts.py`** — one engine per commit
6. **#5 metric registry** — whenever

---

## The through-line

Findings #1, #3, and the `freeze` / `freeze_optimization` split share one root cause: a
shared abstraction that was never extracted, so each new engine or route copied the
previous one. That's the characteristic failure mode of a codebase that grew by adding
capability quickly — which is also why it *has* this much capability.

The architecture is sound. The separation of concerns is better than most commercial
tooling in this space, and it has been externally validated by surviving a
different-simulator port evaluation. What's needed is consolidation of the copied
scaffolding, not redesign.
