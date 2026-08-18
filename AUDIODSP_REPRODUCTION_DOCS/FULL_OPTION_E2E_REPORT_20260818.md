# Complete option and acoustic end-to-end validation — 2026-08-18

## Result

PASS on the connected Raspberry Pi 2 Model B Rev 1.1, Xonar U7, UMIK-1 and
current L/R/T5S system.  The run covered measurement, every exposed SISO FIR
option, low-level acoustic replay, generated-FIR preview, permanent apply and
mandatory restoration of the user's original listening state.

This is a fixed-microphone functional validation.  Position 1 was deliberately
reused as positions 2 and 3 to avoid additional loud sweeps.  It proves the
workflow and filter-option behavior at the current microphone point, but it is
not a substitute for the final three-position listening-area calibration.

## Sound-producing measurement protocol

- UMIK-1: 90-degree calibration, microphone fixed at the user's current seat.
- Generation measurements: exactly one physical sweep each for L+Woofer,
  R+Woofer and Woofer-only.  No generation sweep was repeated.
- Generation level: 0 dBFS, 14 seconds.  U7 output was -10 dB and the Woofer
  measurement used -9 dB routing attenuation.
- Level check: 16.43 dB SNR, -45.06 dBFS capture peak, PASS.
- L / R / Woofer generation SNR: 22.82 / 19.00 / 23.37 dB.
- Woofer adaptive -3 dB passband: 37.75–67.27 Hz.
- U7 Line/Mic capture was disabled while UMIK measurement playback was active.
- Response deconvolution was deferred until all three sounds had completed.

## FIR option matrix

- 67 exposed option variants generated and digitally validated.
- Every result is 48 kHz, stereo float32 and exactly 32768 taps.
- 134 WAV files (Front and Woofer pair per variant) passed finite-sample,
  format, tap-count, hash and engine-audit checks.
- 41 unique Front responses and 52 unique Woofer responses were produced.
- The final merged matrix regenerated 21 variants with the corrected tone
  algorithm and reused 46 byte-identical results whose inputs were unaffected.
- ZIP: `all-option-filters-v3.zip`, 28,052,852 bytes, SHA-256
  `894aaddffbb9a1946d9040677f3d36614dd133912bf2a0dd680e55d6aabd35d0`.

## Low-level acoustic option validation

- Unfiltered reference plus all 67 variants: 68 entries, 136 L/R sweeps.
- Playback: PCM24, 48 kHz, four channels, maximum digital peak -7.659 dBFS.
- 136/136 captures were usable.  Initial recommended-SNR count was 134/136;
  the two low-SNR sides were selectively repeated at quiet level and both
  passed, yielding 4/4 recommended captures in the retry.
- Main-run minimum/median SNR: 6.976/15.634 dB.  Retry minimum/median SNR:
  15.461/16.216 dB.  Maximum captured peak was -43.348 dBFS in the main run.
- Woofer trim monotonicity, Spearman L/R: 0.9825/0.9930, PASS.
- Bass preference monotonicity, Spearman L/R: 0.9451/0.9231, PASS.
- Treble preference monotonicity, Spearman L/R: 1.000/1.000, PASS.

Baseline filtered versus unfiltered target fit improved at the fixed point:

| Metric | Left improvement | Right improvement |
|---|---:|---:|
| 30 Hz–10 kHz aligned target MAE | 3.547 dB | 5.345 dB |
| 30–120 Hz aligned target MAE | 3.165 dB | 15.302 dB |

The final filtered MAE was 1.737/1.703 dB over 30 Hz–10 kHz and 3.794/4.845 dB
over 30–120 Hz for L/R.  These values describe only the present fixed point.

## Closest generated option to the active RefinedTone preview

The active preview
`Harman_StrongBassControl_RefinedTone_Stereo_48k_NoPreamp.wav` (SHA-256
`15b215879d17daf501326e206acf642444d629f051603ec12fb2c9a5ea251fbb`)
was compared with all 67 final option pairs.  Two independent comparisons
agreed on `correction_low_hz-80` as the closest available option:

- direct FIR transfer comparison: combined Front/full-band and Woofer/20–120
  Hz RMS difference 3.976 dB; Front 4.046 dB and Woofer 3.807 dB;
- fixed-point acoustic-shape comparison, aligned only by the 500–2000 Hz
  session level: combined L/R RMS difference 4.036 dB.

The runner-up was `correction_low_hz-60`, at 4.323 dB by direct FIR transfer
and 4.172 dB by acoustic shape.  The closest option settings are:

| Setting | Value |
|---|---|
| Target / bass control | Harman / Strong |
| Woofer trim | -9 dB |
| Phase | Bass, 200 Hz |
| Spatial weighting | Equal |
| Bass / treble preference | 0 / 0 dB |
| Correction range | 80 Hz–20 kHz |
| Maximum room boost / cut | +6 / -18 dB |

This is the nearest option, not an identical replacement.  RefinedTone is one
stereo FIR copied to Front and Woofer, preserves the approved source below 120
Hz, and applies only a broad ±1 dB refinement above it.  The generated option
uses separately measured Front and Woofer FIRs from the current session.  The
80 Hz lower correction boundary is the main reason it is closer than the
nominal 20 Hz baseline.

## Defects found and corrected

1. A late Woofer ESS impulse peak was being interpreted as 10.8 seconds of
   physical delay.  Direct-arrival delay is now accepted only in a causal
   0–250 ms plausibility window.  Unreliable delay disables phase, decay and
   cross-channel time alignment while preserving valid magnitude correction.
2. Bass/treble preference was inside the automatic room-EQ limiter, so a large
   room cut could erase the requested house curve.  Automatic correction and
   user preference are now calculated separately and reported separately.
   Woofer correction remains cut-only, so preference cannot create automatic
   Woofer boost.
3. ALSA could report a UMIK overrun while returning success.  Every engine
   capture now uses `arecord --fatal-errors`; option validation can record to
   tmpfs after checking capacity and then copy the complete capture to storage.
4. Selective option regeneration and selective acoustic retry gained explicit
   variant selection, hash-checked matrix merging and not-applicable handling
   for single-value monotonic families.

## Full regression and live transaction checks

- Profile/Web: 4096 states, 3968 valid, 128 expected errors, 3136 ordered
  setting pairs, 28 Camilla configurations, 33 concurrent writes: PASS.
- Measurement engine: FFTW3f error 2.562475e-7; magnitude/bass-phase/combined
  builds 54.756/80.950/35.507 seconds: PASS.
- Target matrix: six targets × three presets, 18 combinations: PASS.
- MIMO engine and isolated runtime: eight paths × 32768 taps, three supported
  topologies, backup schema and Pi 2 activation block: PASS.
- Generated-FIR preview, preview restore, permanent apply and full snapshot
  restore: every check PASS.

## Final live state

- `camilladsp`, `audiodsp-web`, `audiodsp-profile-monitor`: active; no failed
  service and CamillaDSP restart count zero.
- Xonar U7 and UMIK-1 detected; Ethernet DHCP address `192.168.0.221`.
- Output volume -10 dB; eight U7 mixer channels all raw 117.
- Original managed Speaker FIR restored, SHA-256
  `8a8a3b2fc31a080a6bc40205f29ea6471df95adf357618b2025bdd193ef45c99`.
- Pre-existing RefinedTone listening preview restored, SHA-256
  `15b215879d17daf501326e206acf642444d629f051603ec12fb2c9a5ea251fbb`.
- Speaker, `copy_front`, 2048-sample chunk and -10 dB settings restored.

## Pi platform decision

Pi 2 remains supported for the current SISO runtime and offline FIR creation;
real-time eight-convolution MIMO is intentionally blocked by CPU budget.  A
Raspberry Pi 5 with 2 GB RAM is the recommended MIMO target for AudioDSP-only
use.  The eight 32768-tap float32 paths contain only 1 MiB of raw coefficients;
CPU, USB scheduling and XRUN margin—not RAM capacity—are the limiting factors.
4 GB is optional only when unrelated services will share the Pi.
