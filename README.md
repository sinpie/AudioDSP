# AudioDSP

AudioDSP is a Raspberry Pi audio processor for Xonar U7, CamillaDSP and UMIK-1. It provides 48 kHz convolution, independent Speaker/Headphone profiles, optional Front/Woofer routing, hardware-volume API control, a responsive Web UI, and a guided three-position room-correction workflow that produces 32768-tap stereo FIR WAV files.

## Release bundles

- `releases/audiodsp-pi2-v1.2.0`: Raspberry Pi 2 Model B v1.1, 32-bit armhf image writer and runtime
- `releases/audiodsp-pi4-pi5-v1.2.0`: Raspberry Pi 4/5, 64-bit aarch64 image writer and runtime

Downloaded Raspberry Pi OS images, CamillaDSP binaries, private SSH keys, device backups and logs are intentionally excluded from Git. Each release README explains where the prerequisites belong and how the writer validates them before writing an SD card.

## Audio behavior

- Xonar U7 capture: 48 kHz, 2 channels
- Xonar U7 playback: 48 kHz, 4 channels
- Front output: independent L/R FIR
- Rear output: Front copy after one 2-channel convolution, or a separate stereo Woofer FIR
- No digital preamp in the DSP graph; volume uses the U7 hardware mixer
- Missing Speaker/Headphone FIR falls back to the other profile and then the immutable Factory FIR
- Uploads and generated tuning can be previewed, compared, discarded and backed up before Apply

Room correction combines three nearby measurements, guarded/regularized cut-biased magnitude correction, optional low-frequency excess-phase correction, bass-mode control and octave-band decay diagnostics. Late reverberation is not aggressively inverted; only reliable long-decay low-frequency bands can receive an additional cut.

## Documentation

Start with [the reproduction documentation](AUDIODSP_REPRODUCTION_DOCS/README.md), then read the platform release README. Architecture, API, UI/UX, backup compatibility, testing, safety and algorithm details are maintained in both release bundles.

The latest silent regression record is [SILENT_CALIBRATION_SELF_VALIDATION_20260818.md](AUDIODSP_REPRODUCTION_DOCS/SILENT_CALIBRATION_SELF_VALIDATION_20260818.md).

## Validation

Run the matching writer with `-ValidateOnly -NoPause` before writing media. The repository also contains exhaustive isolated profile/Web tests and synthetic measurement-engine tests. Actual acoustic acceptance testing must be performed with UMIK-1 at 90° in the intended listening area and requires explicit permission before AudioDSP emits measurement sound.
