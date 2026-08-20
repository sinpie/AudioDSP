# AudioDSP Pi 4 / Pi 5 v1.2.0

This release uses Raspberry Pi OS Lite 64-bit and CamillaDSP 4.1.3 aarch64.
It is suitable for Raspberry Pi 4 and Raspberry Pi 5 with an ASUS Xonar U7.
Pi 5 compatibility assumes a stable power supply, USB connection and the same
U7 ALSA devices; no Pi-4-specific CPU instruction or device-tree setting is
used by AudioDSP.

## Install

- SD card: 8 GB minimum; 16 GB or larger recommended.
- Run `WRITE_FINAL_SD_CARD.cmd`, confirm the exact target disk, and enter the
  Wi-Fi SSID/password when prompted. Ethernet DHCP is configured in parallel.
  An administrator PowerShell may instead pass
  `-WindowsWifiProfile <stored-profile>`; the writer uses a uniquely named
  temporary Windows WLAN XML, never prints its key, and verifies immediate
  deletion before continuing.
- Wi-Fi credentials are never printed. The generated credential script is
  copied to the root filesystem on first boot, removed from the FAT partition,
  and deletes itself after NetworkManager succeeds.
- No fallback/static IP address is created.
- New hostname: `audiodsp-pi`; user: `audiodsp`.
- Web UI: `http://audiodsp-pi.local:8080` or the router-assigned DHCP address.
- Allow about 2–3 minutes for the initial install and automatic reboot.

To carry exactly one completed/in-progress measurement session to a replacement
card, create it with `tools/export_session_migration.py` and pass the resulting
archive as `-SessionMigrationArchive <archive.tar.gz>`. The exporter records
per-file sizes and SHA-256 values. First boot verifies the archive again, imports
it atomically as the current session, clears transient worker/Preview state, and
removes the FAT archive. It does not migrate unrelated sessions or overwrite an
existing session ID.

The Factory/Speaker FIR is stereo float32, 48 kHz, 32768 taps, NoPreamp, with
SHA-256 `8a8a3b2fc31a080a6bc40205f29ea6471df95adf357618b2025bdd193ef45c99`.
The default chunksize is 1024; 512, 2048 and 4096 are available in the Web UI.
The Status screen and `GET`/`PUT /api/volume` expose the real Xonar U7 global
PCM level from -60 to 0 dB. Web/API changes persist without restarting
CamillaDSP; physical-knob changes are displayed but remain temporary until the
user saves them.

## UI, profiles, measurement and recovery

This bundle contains the same complete AudioDSP 1.2 application as the Pi 2
release: separate Status/Measurement/Settings screens, live U7 selector state,
Speaker/Headphones fallback, per-profile bypass, Front-copy or independent Rear
FIR, woofer trim, client-side SVG FFT graphs, English female announcements,
UMIK 0°/90° calibration, level precheck, three-position room measurement,
32768-tap regularized correction, target/preset selection, A/B review and safe
permanent apply.

Independent L/R/Woofer and sub-MIMO correction defaults to a 100 Hz LR4 digital
crossover. Front HPF and Woofer LPF are embedded in the generated 32768-tap WAV
or MIMO FIR bank, so no extra runtime filter stage or block latency is added.
SISO adds a one/three-position cut-only coherent upper guard plus a phase-
agnostic target estimate. The recommended precision SISO session measures
L/R/W plus L+Woofer/R+Woofer before calculation and needs no mandatory later
sweep when its absolute magnitude closure and final safe prediction pass.
Post-Preview acoustic validation remains an optional audit.

The Status screen includes a responsive vector signal console from U7 Line
input through DSP/routing to the physical output. `Speaker` and `Headphone` are
independent output-chain profile keys; either physical path may feed speakers.
The level check binds the session to the selected U7 path, later playback stops
on selector mismatch, and Preview/Apply are restricted to the measured chain.

This 64-bit bundle contains Pi-4/Pi-5-only MIMO Stereo, 2.1 and 2.2 solver and
runtime support, but production generation additionally requires a verified
common timing reference. The default U7-output/UMIK-input setup uses independent
USB clocks and is therefore blocked rather than producing a false complex model.
With a valid reference it creates a robust 2-input x 4-output bank as four
stereo float32 WAVs (eight 32768-tap convolution paths), validates correlated-
input headroom and a common causal delay, and uses chunksize 1024 or larger.
One T5S with two RCA inputs remains one physical control source; dual-sub mode
requires two independently placed and wired subs. Pi 2 explicitly rejects MIMO
activation while retaining all SISO functions.

The UI exposes one sweep level with a night-safe -42 dBFS default. Its quick
check uses the same ESS, routing and passband SNR estimator for every selected
output; white-noise controls are hidden. All quick, full, combined and post-FIR
sweeps disconnect normal input before temporarily verifying U7 PCM at 0 dB,
making the selected dBFS the actual DAC reference regardless of listening
volume. The previous volume is restored and read back before input resumes;
profile/volume writes are locked out meanwhile. Separate Woofer measurement attenuation defaults to -9 dB
and uses the same reference scaling; combined mode treats it as the final
Woofer trim. Every sweep has a frequency-dependent SNR/confidence gate, and
Woofer quality uses its detected sustained -3 dB acoustic passband. Octave-band
noise-compensated Schroeder EDT/T20 reports decay; reliable long-decay bass
resonances can receive up to 3 dB additional cut, but late reverberation is
never inverted. The result page verifies the actual truncated FIR FFT,
normalized target-fit MAE/P90, maximum transfer and early impulse position.
Every SISO or MIMO result also persists `Room_Tuning_Report.json` and
`Room_Tuning_Report.md`. The UI and download distinguish filter-correctable,
MIMO-limited, placement/acoustic-treatment, not-measured and not-certified
items instead of presenting deep nulls, late reverberation, distortion or
neighbor noise as solved.

Bass-phase alignment includes both measured acoustic arrival and generated FIR
delay while keeping L/R phase common. MIMO restores actuator-relative arrival
phase, first normalizes the deployable SISO base bank, anchors 70–130 Hz level,
regularizes adjacent frequency bins and separates one common target-shape level
alignment from reported physical headroom attenuation. Apply is blocked if the
smoothed-transfer low-bass late/early proxy worsens by more than 1.5 dB. This
proxy is never labeled an actual RT60/decay improvement.

Completed measurement steps are navigation controls only. Data is kept until a
changed setting is explicitly applied or a level test, position restart, or
FIR rebuild is actually started. WAV uploads and full backups are staged and
validated before applying.

The versioned schema-v2 backup ZIP includes settings, correction preferences,
Factory, Speaker and Headphones FIRs, optional managed MIMO banks, and both
UMIK calibration files. Restore verifies
schema, sizes, SHA-256, WAV and calibration data before review; it creates a
downloadable server rollback ZIP before changing the device. A backup from an
unsupported future schema is rejected without mutation.

## Verification

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\write_final_sd_as_admin.ps1 -ValidateOnly -NoPause
```

This checks the 64-bit OS image and uncompressed hash, aarch64 CamillaDSP,
Factory FIR, Bash syntax, LF/no-BOM Linux files and the required AudioDSP UI,
measurement and recovery features. The destructive writer additionally checks
the target disk, copied payload hashes, generated Wi-Fi script, optional
single-session archive, command line, FAT volume and final disk identity.

See `AUDIODSP_REQUIREMENTS_VERIFIED.md` for the full verification record and
`../../AUDIODSP_REPRODUCTION_DOCS/README.md` for the complete reproduction and maintenance documentation.
See `../../AUDIODSP_REPRODUCTION_DOCS/MIMO_ROOM_TUNING.md` for research sources, topology rules, the exact
optimization/safety pipeline, measurement instructions and explicit limits.
`../../AUDIODSP_REPRODUCTION_DOCS/MIMO_VALIDATION_REPORT_20260818.md` records the silent numerical/runtime/backup tests
and the remaining real-room and Pi4/Pi5 load acceptance work.
