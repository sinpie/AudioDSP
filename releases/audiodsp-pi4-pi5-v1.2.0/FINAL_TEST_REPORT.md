# AudioDSP 1.2.0 final test report

Date: 2026-08-18 · hardware: Raspberry Pi 2 Model B Rev 1.1

## Result

PASS. Production CamillaDSP was not restarted by the candidate tests or final
Web/engine deployment. The later user-authorized quiet paired acoustic A/B test
intentionally stopped and restored it once; its final PID is 30454 and the active Speaker FIR remained SHA-256
`8a8a3b2fc31a080a6bc40205f29ea6471df95adf357618b2025bdd193ef45c99`.

The later complete fixed-microphone end-to-end run also passed on the connected
Pi 2 using the shared SISO code: exactly one L+Woofer, one R+Woofer and one
Woofer-only generation sweep; all 67 option variants and 134 FIR WAVs; 136/136
usable acoustic validation captures; two successful low-SNR retries; and
generated-FIR preview/apply/restore.  See
`../../AUDIODSP_REPRODUCTION_DOCS/FULL_OPTION_E2E_REPORT_20260818.md`.  Pi 5 2 GB is sufficient for the
AudioDSP-only MIMO target; 4 GB is not required.

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

- Common Pi2/Pi4/Pi5 Web and measurement sources passed the Pi 2 silent profile matrix: 4096 total states, 3968 valid, 128 expected errors and 3136 ordered setting pairs.
- Offline ARMv7 measurement build passed magnitude, bass-phase and combined-copy 32768-tap output checks plus U7 bind/same-path/change-path/cross-profile rejection fixtures.
- Writer preflight now requires the responsive vector signal console, measurement path-lock UI, path enforcement functions and the shared selector-state default.
- Live deployment and visual inspection were performed on Pi 2 only. Pi 4/5 common-source and writer checks are covered, but this revision does not claim a Pi 4/5 hardware runtime or MIMO acoustic acceptance test.

## 2026-08-18 FIR-embedded digital crossover maintenance

- Independent L/R/Woofer SISO defaults to an embedded 100 Hz Linkwitz-Riley
  fourth-order crossover. Front HPF, Woofer LPF and the joint cut-only sum guard
  are all multiplied into the existing 32768-tap WAVs; no runtime filter stage,
  convolution path or CamillaDSP block-latency increment is added.
- The common measurement regression passed LR4-complement, default migration,
  joint Front+Woofer guard, acoustic-false-positive rejection and 32768-tap WAV
  structure checks. The common MIMO self-test also passed causality and physical
  output limits, including fail-closed rejection of its unsafe fixture.
- The final isolated Web/profile matrix on Pi 2, using byte-identical common
  Pi 4/5 sources, passed 4096 states (3968 valid, 128 expected errors), 56
  operations, 3136 ordered operation pairs and 28 unique CamillaDSP configs.
- The Pi 4/5 writer passed `-ValidateOnly`; Python compilation and repository
  whitespace checks passed. This is common-source and bundle validation, not a
  claim of Pi 4/5 hardware acoustic or real-time MIMO acceptance.

## 2026-08-18 resumable measurement UX and disclosure maintenance

- The active session summary is rendered above the six workflow tabs and shows
  session ID, note, creation time, state, measurement count, resumable step and
  FIR availability. Saved sessions can be searched, annotated and resumed from
  their last verified checkpoint; editing only the note does not invalidate any
  measurement, calculation or completion state.
- Session loading validates persisted response JSON and Front/Rear WAV
  artifacts. Automatic diagnostics label applicable rows `PASS` or `FAIL` and
  unavailable/non-applicable evidence `N/A`; failed rows are red and provide a
  concrete next action.
- Every native expandable section has a CSS-vector chevron, a 48 px summary hit
  area and an accented open state, making disclosure controls visually distinct
  from static cards.
- Common-source responsive validation passed at 375x812 and 1440x900 with zero
  horizontal overflow, six tabs/panels, correct disclosure toggling, session
  search and note dirty/clean transitions. The reusable validator is
  `diagnostics/validate_web_mobile.ps1`.
- The common Web/profile/session source passed the complete isolated matrix on
  Pi 2: 4096 states (3968 valid, 128 expected errors), 56 operations, 3136
  ordered pairs, 16 Preview resolution cases and 33 concurrent writes. The Pi
  4/5 writer passed `-ValidateOnly`; this is release/common-source validation,
  not a Pi 4/5 hardware runtime claim.
- Common request handling absorbs normal BrokenPipe/ConnectionReset client
  disconnects at the connection boundary so polling-browser navigation does not
  produce misleading Web error tracebacks.

## 2026-08-19 final common-source validation

- The shared SISO source passed Flat/none/0 dB baseline, 18 target/preset and 94 UI-option 32768-tap scenarios. Precision L/R/W/L+W/R+W validates crossover closure before build; target-fit diagnostics use the full audible sum rather than a standalone LPF branch.
- MIMO Stereo/2.1/2.2 baseline models passed. The 19-option matrix intentionally rejected five unsafe modal-tail variants instead of weakening the safety gate.
- Pi 5 2 GB planning passed for the current 2×4 bank and a dense 5.1+dual-sub 6×7/42-path worst case: 135 MiB runtime plan, 530 MiB generation plan and 138.64 MiB measured 64-bit array allocation peak. CPU/XRUN remains a required Pi 5 hardware acceptance test.
- Pi 2/3/4/5 deterministic materialization, both release writer validation paths, exact Chrome CDP PC/mobile UI regression, Python compile and shell syntax checks passed. This does not claim a Pi 4/5 acoustic or real-time MIMO hardware acceptance test.
