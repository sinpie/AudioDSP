# AudioDSP 1.2.0 final test report

Date: 2026-08-18 · hardware: Raspberry Pi 2 Model B Rev 1.1

## Result

PASS. Production CamillaDSP was not restarted by the UI deployment or tests;
its PID remained 28488 and the active Speaker FIR remained SHA-256
`8a8a3b2fc31a080a6bc40205f29ea6471df95adf357618b2025bdd193ef45c99`.

## Profile and Web matrix

- 4096 states: 3968 valid, 128 expected errors.
- 3136 ordered setting pairs from 56 operations.
- 28 unique CamillaDSP configurations checked.
- Seven U7 HID states, fallback, bypass, rear modes, 38 woofer-trim changes,
  chunksizes, malformed requests, rollback and 33 concurrent writes.
- Four staged WAV applies, A/B recovery and generated-pair backup.
- Browser WAV/ZIP downloads, versioned full backup/restore, future-schema
  rejection, automatic rollback and latest rollback download.
- Live selector polling, highlighted card, six targets, measurement/health API.
- U7 volume: five API writes, one form write, six invalid-request cases,
  physical-knob divergence, raw/dB mapping, persistence and concurrent write.

## Measurement engine on ARMv7

- FFTW3f 65536-point round-trip error: 2.5625e-7.
- Magnitude build: 52.966 s; bass-excess-phase build: 60.344 s.
- Every output: 48 kHz, stereo float32, exactly 32768 taps.
- Dependency invalidation, same-setting preservation, correction preferences,
  variable smoothing, natural-rolloff guard and offline level check passed.
- Strong woofer mode produced -9.0 dB maximum Rear transfer in the synthetic
  test; phase build produced -5.91 dB after the synthetic alignment case.

## Live hardware and UI

- `camilladsp`, `audiodsp-web`, and `audiodsp-profile-monitor`: active.
- Xonar U7 and UMIK-1: detected; Ethernet: DHCP.
- Live `GET`/`PUT /api/volume`: -10 dB, raw 117, eight uniform channels;
  CamillaDSP PID 28488 and active FIR SHA remained unchanged.
- Live full backup: schema 1, 488669 bytes, eight ZIP members, all SHA verified.
- PC 1440×1000 and compact/mobile layout visually inspected in dark theme.
- Measurement NOT OK remains on step 2, calibration is collapsible, workflow
  steps are clickable without mutation, and mobile navigation is fixed at the
  bottom with a two-column compact workflow.

## Release preflight

- Pi 2: armhf image, uncompressed image hash, ELF ARM machine 40 CamillaDSP,
  Factory FIR, LF/no-BOM Linux files and Bash syntax: PASS.
- Pi 4/5: arm64 image, uncompressed image hash, ELF AArch64 machine 183
  CamillaDSP, Factory FIR, LF/no-BOM Linux files and Bash syntax: PASS.
