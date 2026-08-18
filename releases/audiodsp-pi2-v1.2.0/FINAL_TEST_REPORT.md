# AudioDSP 1.2.0 final test report

Date: 2026-08-18 · hardware: Raspberry Pi 2 Model B Rev 1.1

## Result

PASS. Production CamillaDSP was not restarted by the candidate tests or final
Web/engine deployment. The later user-authorized quiet paired acoustic A/B test
intentionally stopped and restored it once; its final PID is 30454 and the active Speaker FIR remained SHA-256
`8a8a3b2fc31a080a6bc40205f29ea6471df95adf357618b2025bdd193ef45c99`.

The later complete fixed-microphone end-to-end run also passed: exactly one
L+Woofer, one R+Woofer and one Woofer-only generation sweep; all 67 option
variants and 134 FIR WAVs; 136/136 usable acoustic validation captures; two
successful low-SNR retries; and generated-FIR preview/apply/restore.  See
`../../AUDIODSP_REPRODUCTION_DOCS/FULL_OPTION_E2E_REPORT_20260818.md`.  After the transaction test, the
original managed FIR and the user's RefinedTone preview were restored.

## Profile and Web matrix

- 4096 states: 3968 valid, 128 expected errors.
- 3136 ordered setting pairs from 56 operations.
- 28 unique CamillaDSP configurations checked.
- Seven U7 HID states, fallback, bypass, rear modes, 38 woofer-trim changes,
  chunksizes, malformed requests, rollback and 33 concurrent writes.
- Four staged WAV applies, A/B recovery and generated-pair backup.
- Browser WAV/ZIP downloads, versioned full backup/restore, future-schema
  rejection, automatic rollback, latest rollback download and extracted-staging cleanup.
- Live selector polling, highlighted card, six targets, measurement/health API.
- U7 volume: five API writes, one form write, six invalid-request cases,
  physical-knob divergence, raw/dB mapping, persistence and concurrent write.

## Measurement engine on ARMv7

- FFTW3f 65536-point round-trip error: 2.5625e-7.
- Magnitude build: 51.326 s; bass-excess-phase build: 76.067 s; combined L/R
  single-convolution-copy build: 33.656 s.
- Every output: 48 kHz, stereo float32, exactly 32768 taps.
- Dependency invalidation, same-setting preservation, correction preferences,
  variable smoothing, natural-rolloff guard, adaptive Woofer passband SNR,
  interrupted-worker recovery and offline level check passed.
- Strong woofer mode produced -9.0 dB maximum Rear transfer in the synthetic
  test; phase build produced -5.91 dB after acoustic-plus-FIR total-delay alignment.
- Six targets × three presets and one representative bass-phase actual-FIR
  matrix passed; bass-phase implementation MAE/P95 was 0.0123/0.0548 dB.
- Dynamic sweep timing recovered nominal 400 ms, a cold-start capture with the
  nominal arm interval removed, and 1100 ms delayed capture. Adaptive Woofer
  passband SNR remained higher than full-band SNR in the regression fixture.

## Quiet applied-FIR acoustic A/B

- Four 14-second sweeps at -15 dBFS: raw/FIR for L+Woofer and R+Woofer. This is
  15 dB below the previous 0 dBFS sweep. U7 Mic/Line capture was disabled during
  playback and restored afterward.
- Low-noise-window raw/FIR SNR estimates were 28.29/20.92 and 7.15/7.68 dB
  (L/R). The production gate, which conservatively retains the noisier valid
  pre/post segment, reported raw 27.22/14.94 dB and FIR -2.12/4.59 dB. It rejects
  both filtered captures because the approved FIR attenuates roughly 9-12 dB
  through much of the midband and 20-26 dB around 60-100 Hz; fine residuals are
  exploratory supporting evidence, not a certification.
- Measured applied transfer tracked the FIR from 120 Hz to 10 kHz with MAE
  0.46/0.36 dB (120-500 Hz) and 0.45/0.38 dB (500 Hz-10 kHz). Low-bass tracking
  was less certain: 5.02/1.90 dB MAE for L/R at 30-120 Hz.
- Harman-aligned raw bass peak excess fell from 15.12 to 4.98 dB on L and from
  13.24 to 6.06 dB on R in the measured curves. The theoretical high-confidence
  prediction is a 14.60/10.44 dB L/R peak reduction.
- The strong-control FIR overshoots the Harman bass level after independent
  500-2000 Hz alignment: predicted 30-120 Hz median residual changes from
  +6.41 to -7.24 dB on L and +8.53 to -4.59 dB on R. It is effective boom
  suppression, not the closest neutral-Harman fit.
- Artifacts: `analysis_summary.json`, `frequency_comparison.csv`, and
  `comparison.svg` under `applied_validation_20260818_122253`.

## Live hardware and UI

- `camilladsp`, `audiodsp-web`, and `audiodsp-profile-monitor`: active.
- Xonar U7 and UMIK-1: detected; Ethernet: DHCP.
- Live `GET`/`PUT /api/volume`: -10 dB, raw 117, eight uniform channels;
  CamillaDSP PID 12593 and active FIR SHA remained unchanged.
- Live full backup after deployment: schema 2, 488735 bytes, unique nanosecond
  filename; restore-staging residue count zero.
- PC 1440×1000 and compact/mobile layout visually inspected in dark theme.
- Measurement NOT OK remains on step 2, calibration is collapsible, workflow
  steps are clickable without mutation, and mobile navigation is fixed at the
  bottom with a two-column compact workflow.

## Release preflight

- Pi 2: armhf image, uncompressed image hash, ELF ARM machine 40 CamillaDSP,
  Factory FIR, LF/no-BOM Linux files and Bash syntax: PASS.
- Pi 4/5: arm64 image, uncompressed image hash, ELF AArch64 machine 183
  CamillaDSP, Factory FIR, LF/no-BOM Linux files and Bash syntax: PASS.

## MIMO candidate and full room audit

- Three synthetic topologies passed: stereo 2-actuator, one-sub 3-actuator and
  dual-sub 4-actuator. Every bank contained four stereo float32 WAV files with
  exactly 32768 taps and eight finite convolution paths.
- Robust complex solver, per-output correlated-input headroom projection,
  restored relative bulk arrival, SISO bass-level anchor, spectral continuity,
  causality, modal-tail guard, manifest SHA-256 and actual CamillaDSP `--check`: PASS.
- Isolated manager test installed/enabled/disabled MIMO, returned to SISO and
  rejected MIMO on Pi 2 without touching production state.
- Backup schema 2 MIMO inventory, restore staging validation and extraction
  cleanup: PASS. The common engine/Web code was then deployed to Pi2 while
  real-time MIMO activation remained blocked.
- SISO and MIMO builds now persist JSON and Markdown room-tuning audits that
  separate FIR/MIMO-correctable findings from placement/treatment limits,
  unmeasured items and required runtime/post-measurement validation.
- See `../../AUDIODSP_REPRODUCTION_DOCS/MIMO_VALIDATION_REPORT_20260818.md` for metrics and the remaining sound-producing
  Pi 4/5 MIMO acceptance tests. The acoustic sweep above validates SISO only.

## Final read-only live check

At closeout on 2026-08-18, after the user-authorized paired sweep, the connected
production Pi 2 reported CamillaDSP PID 30454; `camilladsp`, `audiodsp-web` and `audiodsp-profile-monitor` were all
active, CamillaDSP restart count was zero, the last 30 minutes contained zero
XRUN/underrun/overrun/panic/error matches, and
the active Speaker FIR SHA-256 was still
`8a8a3b2fc31a080a6bc40205f29ea6471df95adf357618b2025bdd193ef45c99`.
The paired test stopped and restored CamillaDSP once by design; it did not
change the profile, FIR, volume or persistent audio configuration. Pre-deployment code is recoverable from
`/var/lib/audiodsp/code-backups/20260818T030943Z`.

## 2026-08-18 output-path lock and signal-flow maintenance

- Silent profile/Web matrix reran twice after fixes: 4096 total states, 3968 valid, 128 expected errors, 3136 ordered setting pairs, result `PASS`.
- Added silent session `new/configure/cancel-error`, Markdown/JSON/ZIP report download, one bound-profile Preview and two wrong-profile Preview/Apply rejection checks.
- Offline measurement engine result `PASS`: magnitude 52.269 s, bass phase 77.203 s, combined-copy 33.602 s; every generated WAV remained 48 kHz stereo float32 × 32768 taps.
- Unit fixtures verified U7 path bind, same-path allow, changed-path stop and cross-profile result rejection. Live preflight then found and fixed a default-path mismatch: measurement now reads the same `/var/lib/audiodsp/u7-selector-state.json` as monitor/manager; production status reported `profile=speaker`, `stale=false`.
- Desktop 1440×1200 and compact 390×844 dark-theme renders were inspected. The new vector signal console shows Input → DSP → Routing → U7 selector → physical speaker chain; compact layout changes the connectors to a vertical flow.
- Live Pi 2 deployment replaced only Web/measurement code and restarted only `audiodsp-web`. CamillaDSP PID stayed `17814`; config SHA stayed `dfc5d12715b9e543fc87a1349a636cf26a43df454ac172f7d0377e586f532786`; Speaker FIR SHA stayed `8a8a3b2fc31a080a6bc40205f29ea6471df95adf357618b2025bdd193ef45c99`.
- `audiodsp-web`, `camilladsp`, and `audiodsp-profile-monitor` remained active; `/`, `/measure`, `/settings` returned HTTP 200 and recent Web warning journal was empty. No measurement sound was played.
- Previous production code backup: `/var/backups/audiodsp/code-20260818-signal-flow-path-lock`.

## 2026-08-18 FIR-embedded digital crossover maintenance

- Independent L/R/Woofer SISO defaults to an embedded 100 Hz Linkwitz-Riley
  fourth-order crossover. Front HPF, Woofer LPF and the joint cut-only sum guard
  are all multiplied into the existing 32768-tap WAVs; no runtime filter stage,
  convolution path or CamillaDSP block-latency increment is added.
- FFTW3f measurement regression on the physical Pi 2 passed. The reported
  round-trip error was `2.562475210909909e-07`; all crossover-default,
  LR4-complement, joint Front+Woofer guard and acoustic-false-positive rejection
  fixtures passed. Magnitude/bass-phase/combined builds took approximately
  52.24/137.83/33.93 seconds in that run.
- MIMO silent self-test passed structural, causality and physical-output-limit
  checks. Stereo and one-sub fixtures were applicable; the deliberately unsafe
  dual-sub fixture was rejected by the impulse-tail proxy instead of being
  misreported as an acoustic pass.
- The final isolated Web/profile matrix, including legacy preference migration
  to crossover `ON/100 Hz`, passed 4096 states (3968 valid, 128 expected errors),
  56 operations, 3136 ordered operation pairs and 28 unique CamillaDSP configs.
- Both release writers passed `-ValidateOnly`; Python compilation and repository
  whitespace checks passed. Pi 2 and Pi 4/5 common engine/Web/test sources have
  identical SHA-256 values.
- Production received only the three common Python programs and only
  `audiodsp-web` was restarted. At final read-only verification all three
  services were active, CamillaDSP PID remained `17814`, and the managed Speaker
  FIR remained
  `8a8a3b2fc31a080a6bc40205f29ea6471df95adf357618b2025bdd193ef45c99`.
  No measurement sound was played and no new FIR was applied. Previous code is
  recoverable from `/var/backups/audiodsp-code-20260818-crossover`.

## 2026-08-18 resumable measurement UX and disclosure maintenance

- The active session summary is rendered above the six workflow tabs and shows
  session ID, note, creation time, state, measurement count, resumable step and
  FIR availability. Saved sessions can be searched, annotated and resumed from
  their last verified checkpoint; editing only the note does not invalidate any
  measurement, calculation or completion state.
- Session loading validates persisted response JSON and Front/Rear WAV
  artifacts. A copied 41 MB completed session restored all three positions,
  level check, build result, target and bass preset without playing sound or
  changing the live session.
- Automatic diagnostics now label every applicable row as `PASS` or `FAIL` and
  unavailable/non-applicable evidence as `N/A`. Failed rows are red and include
  a concrete next action. Acoustic failure is not conflated with a structurally
  valid downloadable WAV.
- Every native expandable section now has a CSS-vector chevron, a 48 px summary
  hit area and an accented open state. The browser-default marker is suppressed
  consistently, so expandable controls are distinguishable from static cards.
- Hidden Chromium validation passed at 375x812 and 1440x900: zero horizontal
  overflow, six tabs/panels, overview containment, disclosure toggle, open-state
  chevron rule, session search and note dirty/clean transitions. The reusable
  validator is `diagnostics/validate_web_mobile.ps1`.
- The final isolated Pi 2 matrix passed 4096 states (3968 valid, 128 expected
  errors), 56 operations, 3136 ordered pairs, 16 Preview resolution cases, 33
  concurrent writes and Web/session integration. Both release writers passed
  `-ValidateOnly`.
- Only `audiodsp-web` was restarted for the final live UI deployment.
  `camilladsp` remained active with pre/post PID `7731`; no sound was played, no FIR was
  applied and the audio engine was not restarted.
- A final headless-browser close exposed harmless but noisy client-disconnect
  tracebacks. The request handler now absorbs BrokenPipe/ConnectionReset at the
  connection boundary; two PC/mobile close tests left the Web journal clean.
  The active Front FIR SHA-256 remained
  `8a8a3b2fc31a080a6bc40205f29ea6471df95adf357618b2025bdd193ef45c99`.
  Previous Web code is recoverable from
  `/usr/local/bin/audiodsp-profile-web.py.pre-disclosure-final-20260818`.

## 2026-08-19 final silent validation and identity migration

- The canonical SISO engine passed the Flat/none/0 dB baseline plus 18 target/preset and 94 selectable-option 32768-tap scenarios. Precision L/R/W/L+W/R+W closes the crossover before build; Standard L/R/W uses the same-clock complex model. Neither path requires an impossible late mandatory sweep.
- Standalone Woofer LPF is no longer compared with a full-range target. Target-fit MAE/P90 is evaluated on the audible Front+Woofer sum and the UI names the exact stage/control or measurement to change when a gate fails.
- Exact Chrome CDP tests passed at 390×844 and 1440×1200 for all pages and all six workflow tabs: no document overflow, one visible panel, correct ARIA/hash state, disclosure toggling and zero unexpected navigation.
- Production identity is `audiodsp-pi2` / `audiodsp` / `audiodsp-ethernet`; new-account SSH and sudo were verified before legacy identity removal. Active-path legacy-name searches returned zero.
- Final live preservation check: all three services active, CamillaDSP PID `7731`, Speaker FIR SHA-256 `8a8a3b2fc31a080a6bc40205f29ea6471df95adf357618b2025bdd193ef45c99`, saved/actual U7 volume `0 dB` on eight uniform channels. No sound was played and no FIR/profile/volume was changed.
