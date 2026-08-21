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

The Status screen also presents a responsive vector signal console: U7 Line
input → CamillaDSP → Front/Rear routing → physical U7 selector → speaker chain.
The two profile cards are named `Speaker output chain` and `Headphone-jack
output chain`; in this installation both physical paths feed speakers.

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
- MIMO source/report/backup formats are included for safe migration, but the Pi 2 UI and CLI reject MIMO measurement or 8-path activation. Real-time MIMO requires both the Pi 4/Pi 5 release and a verified common timing reference; all SISO features remain available.
- Global Xonar U7 output volume is readable and writable in the Status UI and
  `GET`/`PUT /api/volume`. The safe application range is -60 to 0 dB; the
  saved default is -10 dB. A volume-only change never restarts CamillaDSP.
- A physical U7 knob change appears in the UI but is not persisted
  automatically; reboot/USB reset restores the last Web/API-saved value.
- The physical U7 button chooses Speaker/Headphones. The Web UI displays it in
  real time; it does not emulate the undocumented hardware output command.
- A level check binds its session to that physical U7 output. Changing the
  selector stops later sweeps/Preview, and Apply can overwrite only the profile
  that was actually measured.
- Quiet female English `Speaker`, `Headphones`, and boot `DSP ready` prompts are
  mixed into Front L/R only.

## Room correction

The browser supports separate 0° and 90° UMIK-1 calibration files. Normal room
measurement uses the 90° file with the microphone pointing upward. A five-
second silence and five-second low-level white-noise check reports background,
signal, SNR, peak, and clipping before measurements are enabled. Measurement
temporarily bypasses CamillaDSP and disables the U7 Mic/Line capture switches.
Every quick, full, combined and post-FIR sweep then verifies U7 PCM at 0 dB, so
the selected dBFS is the DAC reference rather than `dBFS + listening volume`.
The exact prior listening volume is restored and verified before Line input or
CamillaDSP is allowed to resume; restore failure stays muted and fail-closed.
Each generated SISO result saves browser-downloadable `Room_Tuning_Report.json`
and `.md`, explicitly separating FIR-correctable, limited, physical-treatment,
not-measured and not-certified room factors.
See `../../AUDIODSP_REPRODUCTION_DOCS/MIMO_ROOM_TUNING.md` and `../../AUDIODSP_REPRODUCTION_DOCS/MIMO_VALIDATION_REPORT_20260818.md` for the common
design, Pi2 block, silent validation evidence, and remaining Pi4/5 real tests.

Three nearby listening positions can be measured for L/R or L/R plus woofer.
The engine uses regularized sweep deconvolution, spatial dB averaging and
variance weighting, frequency-dependent smoothing, natural-rolloff protection,
boost/cut/transfer limits, magnitude correction, common-L/R optional bass-only
excess phase correction and acoustic-plus-FIR time alignment. White noise and
sweep have independent sliders with a -42 dBFS safe default. Separate Woofer
attenuation defaults to -9 dB and its deconvolution reference is scaled
identically; combined mode treats the value as the final Woofer trim. Every
sweep has a frequency-dependent SNR/confidence gate, and Woofer quality uses
its detected sustained -3 dB acoustic passband. Octave-band
noise-compensated Schroeder EDT/T20 diagnoses decay; only reliable long-decay
bass resonances receive up to 3 dB additional cut, while late reverberation is
never inverted. The result is always a stereo float32
48 kHz 32768-tap Front FIR and, when requested, a Rear FIR. Harman, Flat,
Bruel & Kjaer, RTings, AcoustiX and Not Dr Toole targets are previewed as SVG;
Primus-like and Strong woofer-control presets are provided.
Independent L/R/Woofer correction defaults to a 100 Hz LR4 digital crossover:
Front HPF, Woofer LPF and a three-position cut-only sum guard are embedded in
the same 32768-tap WAVs. This adds no CamillaDSP filter stage or block latency.
The recommended precision session measures L/R/W and L+Woofer/R+Woofer at the
same one/three positions. Only L/R/W design the FIR; the two sums verify the
absolute complex closure without independent normalization. If that model and
the final FIR prediction pass, no later validation sweep is required. The
faster standard L/R/W session still requires a low-level validation sweep after
Preview. Combined L+Woofer/R+Woofer mode cannot split independent
branches and therefore requires crossover OFF.
The result page verifies the actual truncated FIR FFT, normalized target-fit
MAE/P90, maximum transfer, finite samples and early impulse position.

## Verification

Run the non-destructive bundle check:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\write_pi2_sd_as_admin.ps1 -ValidateOnly -NoPause
```

The writer verifies the compressed OS image, uncompressed image hash passed to
Raspberry Pi Imager, CamillaDSP ARMv7 binary, Factory FIR, Linux text format,
Bash syntax, UI/measurement safety features, target disk identity, every copied
payload hash, `cmdline.txt`, and the final FAT volume.

See `AUDIODSP_REQUIREMENTS_VERIFIED.md` for the feature-by-feature record and
`../../AUDIODSP_REPRODUCTION_DOCS/README.md` for the complete reproduction, architecture, API, platform,
testing, and recovery documentation.
