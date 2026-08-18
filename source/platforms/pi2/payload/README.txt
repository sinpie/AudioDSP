# AudioDSP Pi 2 v1.2.0

This is the release image builder for Raspberry Pi 2 Model B Rev 1.1, ASUS
Xonar U7, a stereo preamp source, Front L/R integrated amplifier output, and
Rear L/R stereo woofer output.

## Install

- SD card: 8 GB minimum; 16 GB or larger recommended.
- Network: Ethernet DHCP only. No fallback/static address is created.
- Connect Ethernet and the Xonar U7 before the first power-on.
- Run `WRITE_PI2_SD_CARD.cmd` as Administrator and verify the exact target
  disk before typing `WRITE DISK N`.
- First boot installs the payload and reboots once. Allow about 4–6 minutes.
- New installations use hostname `audiodsp-pi2`, user `audiodsp`, state path
  `/var/lib/audiodsp`, and AudioDSP service/executable names.
- Web UI: `http://audiodsp-pi2.local:8080` or `http://<DHCP-IP>:8080`.

The original strong-bass-control FIR is installed as the Factory and Speaker
profile. It is stereo float32, 48 kHz, 32768 taps, has no digital preamp, and
has SHA-256 `8a8a3b2fc31a080a6bc40205f29ea6471df95adf357618b2025bdd193ef45c99`.

## Everyday workflow

The status page leads with one `Now` action. The measurement page is a six-step
workflow: calibration, level check, three positions, FIR build, A/B review,
and permanent apply. Completed step headers are clickable. Clicking only
navigates and never deletes data. Affected downstream data is invalidated only
after a changed setting is explicitly applied or the user starts a new level
check, position set, or FIR build.

Profile WAV uploads are also staged: select, inspect the browser-computed SVG
response, A/B listen, then apply. Permanent files are not overwritten before
the final confirmation. Double-submit protection and destructive-action
confirmations are enabled.

`Profile & Settings` can download a versioned full backup ZIP containing all
profile settings, correction preferences, Factory/Speaker/Headphones FIRs,
and both UMIK calibration files. Restore is upload → integrity/schema/WAV/Cal
validation → review → explicit apply. A server-side rollback ZIP is created
before restore and its latest copy can be downloaded from the same page.
Future backup schemas are rejected without mutation; older schemas are
normalized and unknown settings are ignored safely.

## Audio behavior

- 48 kHz capture, 24-bit Xonar U7 I/O, 2 input channels and 4 outputs.
- Pi 2 default chunksize 2048; Web choices: 512/1024/2048/4096.
- No Rear FIR: Front convolution is copied to Rear (two convolutions).
- Rear FIR present and selected: independent Front/Rear processing (four).
- Missing selected profile: use the other profile unchanged, then Factory.
- Per-profile bypass and woofer trim 0 to -18 dB.
- The physical U7 button chooses Speaker/Headphones. The Web UI displays it in
  real time; it does not emulate the undocumented hardware output command.
- Quiet female English `Speaker`, `Headphones`, and boot `DSP ready` prompts are
  mixed into Front L/R only.

## Room correction

The browser supports separate 0° and 90° UMIK-1 calibration files. Normal room
measurement uses the 90° file with the microphone pointing upward. A five-
second silence and five-second low-level white-noise check reports background,
signal, SNR, peak, and clipping before measurements are enabled. Measurement
temporarily bypasses CamillaDSP and disables the U7 Mic/Line capture switches.

Three nearby listening positions can be measured for L/R or L/R plus woofer.
The engine uses regularized sweep deconvolution, spatial dB averaging and
variance weighting, frequency-dependent smoothing, natural-rolloff protection,
boost/cut/transfer limits, magnitude correction, optional bass-only excess
phase correction and time alignment. The result is always a stereo float32
48 kHz 32768-tap Front FIR and, when requested, a Rear FIR. Harman, Flat,
Bruel & Kjaer, RTings, AcoustiX and Not Dr Toole targets are previewed as SVG;
Primus-like and Strong woofer-control presets are provided.

## Verification

Run the non-destructive bundle check:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\write_pi2_sd_as_admin.ps1 -ValidateOnly -NoPause
```

The writer verifies the compressed OS image, uncompressed image hash passed to
Raspberry Pi Imager, CamillaDSP ARMv7 binary, Factory FIR, Linux text format,
Bash syntax, UI/measurement safety features, target disk identity, every copied
payload hash, `cmdline.txt`, and the final FAT volume.

See `AUDIODSP_REQUIREMENTS_VERIFIED.md` for the feature-by-feature record.
