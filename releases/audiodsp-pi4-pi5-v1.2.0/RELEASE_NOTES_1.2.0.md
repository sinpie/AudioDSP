# AudioDSP 1.2.0 release notes

## 2026-08-18 maintenance revision

- Added real Xonar U7 PCM output-volume read/write in the Status Web UI.
- Added `GET /api/volume` and strict `PUT /api/volume` for integer -60..0 dB.
- Saved volume is restored after boot/USB reset; physical-knob changes are
  reported separately and volume-only writes do not restart CamillaDSP.
- Added volume persistence/backup, boundary/error/concurrency tests and full
  reproduction documentation under `docs/`.

- Rebranded new-install runtime identifiers from GSonic to AudioDSP while
  retaining legacy environment-variable fallback for gradual upgrades.
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
