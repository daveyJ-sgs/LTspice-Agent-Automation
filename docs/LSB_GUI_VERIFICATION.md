# LSB empty-shell and execution verification

Verified locally on September 10, 2026 with macOS LTspice 17.2.4.

## Changes

- Startup no longer fetches or previews the DAQ example. Study and Optimization
  start unloaded. New projects contain no fabricated variables or experiments.
- RC and three-op-amp instrumentation starter projects remain available to open.
- Optimization freezes and starts the recipe's explicitly declared study/netlists.
  Qualification uses explicitly declared models, correlations, and corners;
  the DAQ example carries its own configuration instead of backend defaults.
- Frozen execution hashes are checked again before launch. Switching optimization
  projects clears previous frozen state and results.
- Standalone optimization plan preview still works without execution definitions;
  launching requires them. See WORKFLOWS.md for the explicit qualification schema.

## Evidence

- 426 tests passed; configured Ruff and mypy checks passed.
- Browser: startup has null Study/Optimization recipes; both starter projects
  open with the correct variables/netlists and valid previews. An RC sample-count
  edit from 8 to 4 persisted after Save, switching to instrumentation, and reopening
  RC. Creating `Verified empty shell` produced empty variables and experiments.
- Real LSB API execution: RC optimization completed four simulations, with two
  feasible candidates. Its selected R=1000 ohm, C=100 nF design then completed eight
  qualification simulations using explicitly declared 1% Gaussian component
  variation, bounded to 95–105% of nominal, four samples, and no named corners.
  Every run manifest was completed and every executed deck was verified as RC.
- Optimization job: `optimization-job-58504f470386abcf`.
- Qualification job: `qualification-job-79b4062dbce651cb`.
- Temporary evidence: `/private/tmp/lsb-final-e2e-20260909/verification.json` and
  its `runs/` tree; script `/private/tmp/lsb_finish_e2e.py`.

This was local verification. Windows GUI/CI and remote dispatch were not rerun
for these changes. Existing user projects were left untouched.
