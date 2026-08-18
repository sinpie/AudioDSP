# AudioDSP 1.2.0 release notes

## 2026-08-18 maintenance revision

- Restored independent White/Sweep -42 dBFS safe defaults, live high-output
  warning and explicit pre-sweep confirmation; Woofer SNR now follows the
  detected sustained -3 dB acoustic passband.
- Corrected SISO acoustic-plus-FIR crossover delay and common-L/R phase safety;
  cached FFTW plans and recovered interrupted workers without mutating sessions.
- Updated common MIMO math with actuator-relative bulk phase, SISO bass anchor,
  spectral continuity/base blending and modal-tail gate; Pi2 remains blocked.
- Made rollback/staging names unique and removed extracted restore files on
  validation failure, replacement, discard and successful apply.
- Added common MIMO bank/report/backup compatibility for migration to Pi4/5,
  while explicitly blocking MIMO measurement and eight-path runtime on Pi2.
- Added persistent SISO room-tuning JSON/Markdown reports that separate FIR
  improvements from partial, physical-treatment, unmeasured and uncertified limits.
- Added schema-v2 backup compatibility and silent MIMO math/runtime/Pi2-block tests.

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
