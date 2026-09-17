# 100 MHz DAQ benchmark fixtures

These are two retained circuits from Dave's `Electronics_Projects/100MHz_DAQ`
project, not the automation repository's simplified mixed-signal DAQ example.
`provenance.json` records original study paths, hashes and reference metrics.
Only absolute model include paths were changed to relative `models/` paths.

- `ac.cir`: nominal 1 MΩ, 1:1 input, gain code 21; AC sweep 1 kHz–2 GHz.
- `transient.cir`: 50 Ω input, gain code 21; 100 MHz sine, 600 ns duration,
  maximum step 10 ps. Preserves the original 30 pS numerical shunt.

Both exercise the OPA817 buffer, LMH5401 FDA, LMH6401 VGA and nine-pole
anti-alias filter. The ADC is only a resistive/capacitive load. Supply sources
and several physical parasitics are idealized; this is not hardware validation.

TI model sources are downloaded at runtime by `tests/prepare_daq100_models.py`.
It verifies the original archive/container hashes, reproduces the original
OPA817 noiseless-resistor and LMH6401 DC-conductance adaptations, and verifies
the resulting model hashes against the project. Vendor model contents are not
committed here. Their existing notices remain intact in the runtime copies.

The benchmark requires every saved trace to be finite, AC bandwidth ≥100 MHz,
passband within ±0.5 dB, stopband rejection ≥60 dB, and transient output within
±1 V differential / 1.3–1.5 V common mode. Selected metrics must also match the
retained Mac reference: 0.05 dB absolute tolerance for dB values, otherwise 1%
with a 0.001 absolute floor. These are regression tolerances, not a claim of
physical model accuracy. Harmonics are not used to claim SFDR or SINAD.

Runtime artifacts retain all raw results and per-run metrics. A fast run that
fails numerical checks does not qualify as a successful speed comparison.

## Duplicate vendor helper names found by Windows qualification

The original LMH5401 and LMH6401 libraries both define global `VNSE` and `FEMT`
subcircuits, with different noise parameters. LTspice 26 on Windows rejects
this combination as ill-formed; Mac 17.2.4 accepted it. The benchmark now adds
`_LMH5401` to the FDA's two definitions and three active calls on **both**
platforms. All equations, parameters, comments and source models are preserved.
The script verifies original hashes before this change and separate portable
hashes afterward. Historical project files are not modified.

AC/transient metrics must still match the original reference. This does not
revalidate the original project's noise studies: duplicate noise helper names
make those worth revisiting separately with the portable models.
