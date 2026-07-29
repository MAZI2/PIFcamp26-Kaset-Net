# DSP Test Workspace

This folder is for quick experiments on `../clip.wav`.

`clean_clip.py` uses only Python standard libraries.
`try_noise_variants.py` uses NumPy and SciPy.

Run:

```bash
python3 clean_clip.py
python3 try_noise_variants.py
```

Outputs are written to `dsp/out/`:

- `01_dc_removed.wav`: per-channel DC offset removed.
- `02_highpass.wav`: DC removal plus a gentle high-pass filter.
- `03_noise_reduced.wav`: high-pass plus a soft downward expander for quieter noise.
- `04_hum_notched_50hz_harmonics.wav`: 50 Hz mains-hum harmonics reduced.
- `05_hum_notched_lowpass_6k5.wav`: hum reduction plus low-pass hiss reduction.
- `06_spectral_gate_light.wav`: light spectral noise gate.
- `07_spectral_gate_strong.wav`: stronger spectral noise gate.
- `08_despike_spectral_gate.wav`: click/spike reduction plus spectral gate.
- `09_targeted_tone_notches.wav`: targeted notches around the strongest detected tones.
- `10_repaired_aggressive.wav`: clipped-sample softening, tone notches, despike, and spectral gate.

Start by comparing `04`, `07`, `08`, `09`, and `10`.
The first script is intentionally conservative; the second script is more aggressive and diagnostic.
