# AudioDSP requirements and verification record

Release: 1.2.0 · 2026-08-18

## UI and recovery

- [x] Separate Status, Measurement/Correction, and Profile/Settings screens.
- [x] PC/mobile responsive layout, system light/dark default and manual theme.
- [x] Sticky mobile navigation and measurement workflow; no horizontal clipping.
- [x] One prominent current action and clear physical/requested/effective state.
- [x] Click completed measurement steps without discarding measurements.
- [x] Same-value configuration apply preserves all data.
- [x] Changed configuration invalidates only dependent downstream results.
- [x] Actual level retest, position restart, and rebuild warn before invalidation.
- [x] Duplicate form submission is blocked.
- [x] Staged FIR upload with SVG before/after, A/B, discard, and final apply.
- [x] Versioned full ZIP backup and staged restore with SHA-256 inventory.
- [x] Reject future schema without mutation; normalize supported older schema.
- [x] Automatic pre-restore rollback ZIP and browser access to the latest copy.
- [x] Unique rollback names and validation/replacement/discard cleanup of extracted staging directories.
- [x] Status-screen U7 output slider, +/- steps, presets and no-JS form fallback.
- [x] Actual hardware volume and reboot-persistent saved volume shown separately.

## Profiles and playback

- [x] Speaker/Headphones selection follows the physical Xonar U7 selector.
- [x] Selected-profile fallback to the other profile and then Factory.
- [x] Per-profile bypass, rear copy/separate, woofer trim, and chunksize controls.
- [x] Client-side FFT and SVG L/R or L/R+woofer response graph.
- [x] Female English Speaker/Headphones/DSP-ready Front-only announcements.
- [x] 48 kHz, stereo capture, four outputs; Pi 2 default chunksize 2048.
- [x] `GET`/`PUT /api/volume`, strict -60..0 dB, eight-channel U7 PCM mapping.
- [x] Volume-only changes never restart CamillaDSP or mutate FIR/config.

## Measurement and correction

- [x] Independent UMIK 0° and 90° calibration upload/validation.
- [x] Five-second silence plus five-second low-level white-noise precheck.
- [x] Independent white-noise/sweep sliders, -42 dBFS defaults, high-output warning and pre-sweep confirmation.
- [x] Background/signal/SNR/peak/clipping result and actionable NOT OK guidance.
- [x] L/R or L/R/woofer, three listening positions, progress and ETA.
- [x] Measurement bypasses DSP and disables U7 Mic and Line capture.
- [x] 65536-point FFTW3f design; output 48 kHz float32 stereo 32768 taps.
- [x] Regularized inverse, spatial weighting/variance and variable smoothing.
- [x] Natural-rolloff and unreliable-null boost protection.
- [x] Soft boost, hard cut and transfer gain limits.
- [x] Magnitude-only or bass excess-phase correction with causal delay limit.
- [x] L/R/woofer alignment and summed-bass cancellation diagnostics.
- [x] Front/Woofer acoustic-plus-FIR total-delay alignment and common-L/R phase magnitude-preservation guard.
- [x] Six target curves, bass/treble preference, correction band controls.
- [x] Primus-like and Strong woofer-control modes.
- [x] Spatial uncertainty band, before/after/target graph, diagnostics and manifest.
- [x] Browser WAV/ZIP download, non-destructive A/B, rollback and final apply.
- [x] Persistent JSON/Markdown room-tuning audit distinguishes FIR limits,
  placement/treatment, not-measured and not-certified factors.
- [x] MIMO bank/backup formats share the Pi4/5 code, while Pi2 UI, engine and
  runtime reject MIMO measurement/activation and retain SISO processing.
- [x] Common MIMO math restores actuator bulk arrival, anchors SISO bass level,
  regularizes adjacent bins and rejects modeled modal-tail regression before Pi4/5 deployment.

## Automated and hardware verification

- [x] 4096 profile states: 3968 valid, 128 expected invalid.
- [x] 3136 pairwise setting transitions from 56 operations and 28 configs.
- [x] WAV validation, fallback, rollback, staged uploads, concurrency and HID cases.
- [x] Volume API/form boundaries, invalid types, physical-knob divergence and concurrency.
- [x] Dependency-invalidation and correction-preference persistence tests.
- [x] Pi 2 ARMv7 full 32768-tap magnitude and bass-phase builds.
- [x] Ten-minute Pi 2 load check: CamillaDSP about 36%, Web about 0.7–1.1%.
- [x] Live services and Xonar U7/UMIK detection verified.
- [x] Operating Speaker FIR SHA-256 unchanged during Web deployment.
- [x] Pi 2 bundle preflight validates image, binary, FIR, scripts and hashes.
- [x] Pi 4/5 ARM64 release uses the same application with default chunksize 1024.
- [x] Silent MIMO math/runtime fixtures pass, and Pi2 activation blocking is verified.
- [x] Six targets × three woofer presets and representative bass-phase actual-FIR matrix PASS.
- [x] Ethernet uses DHCP only; no collision-prone emergency static address.

## Algorithm references

- REW official EQ guidance: variable smoothing and limiting correction beyond
  natural speaker roll-off.
- Kirkeby/Nelson regularized inverse-filter principles.
- Frequency-dependent regularization and causality-aware room equalization.
- Multi-position response aggregation with uncertainty-dependent correction.

AudioDSP deliberately uses conservative deterministic DSP inspired by these
principles; it does not claim to implement an unrelated neural model verbatim.
