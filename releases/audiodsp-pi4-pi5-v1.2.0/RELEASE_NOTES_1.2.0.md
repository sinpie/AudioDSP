# AudioDSP 1.2.0 release notes

## 2026-08-18 maintenance revision

- Restored independent White/Sweep -42 dBFS safe defaults, live high-output
  warning and explicit pre-sweep confirmation; Woofer SNR now follows the
  detected sustained -3 dB acoustic passband.
- Corrected SISO acoustic-plus-FIR crossover delay and common-L/R phase safety;
  cached FFTW plans and recovered interrupted workers without mutating sessions.
- Corrected MIMO actuator-relative bulk phase, anchored existing SISO bass
  level, added spectral continuity/base blending and a modal-tail apply gate.
- Made rollback/staging names unique and removed extracted restore files on
  validation failure, replacement, discard and successful apply.
- Added Pi4/Pi5 MIMO Stereo/2.1/2.2 measurement and robust 2×4 weighted
  pressure-matching FIR banks: four stereo WAVs, eight 32768-tap paths.
- Added per-frequency physical-output headroom, common causal delay, SISO
  transition, MIMO preview/apply/rollback and schema-v2 bank backup.
- Added persistent room-tuning JSON/Markdown reports that separate FIR/MIMO
  improvements from placement/treatment, unmeasured and uncertified limits.
- Added silent three-topology numerical tests, real CamillaDSP parser test and
  explicit remaining real-room/Pi4/Pi5 load acceptance list.

- Added real Xonar U7 PCM output-volume read/write in the Status Web UI.
- Added `GET /api/volume` and strict `PUT /api/volume` for integer -60..0 dB.
- Saved volume is restored after boot/USB reset; physical-knob changes are
  reported separately and volume-only writes do not restart CamillaDSP.
- Added volume persistence/backup, boundary/error/concurrency tests and linked
  the single canonical reproduction documentation tree.

- Standardized all new-install and runtime identifiers on AudioDSP and
  removed the obsolete environment-variable fallback.
- Added separate Status, Measurement/Correction and Profile/Settings screens
  with PC/mobile responsive navigation and explicit next actions.
- Added non-destructive clickable measurement steps and dependency-aware
  invalidation only on confirmed setting apply or actual re-execution.
- Added UMIK 0°/90° management, silence/white-noise level precheck, three-
  position acquisition, targets, correction preferences and 32768-tap build.
- Added safe FIR staging/A-B/apply and versioned full backup/staged restore with
  integrity checks, schema compatibility and downloadable automatic rollback.
- Added Pi 2 performance optimizations: cached status, atomic status reads and
  client-side FFT/SVG graphs.
- Current-device migration changes only application services and paths; the
  existing Pi 2 hostname/user may stay legacy to avoid disrupting SSH. Fresh
  cards use AudioDSP hostname/user/service/state identifiers throughout.
