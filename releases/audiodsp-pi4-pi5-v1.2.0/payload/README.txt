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
- Wi-Fi credentials are never printed. The generated credential script is
  copied to the root filesystem on first boot, removed from the FAT partition,
  and deletes itself after NetworkManager succeeds.
- No fallback/static IP address is created.
- New hostname: `audiodsp-pi`; user: `audiodsp`.
- Web UI: `http://audiodsp-pi.local:8080` or the router-assigned DHCP address.
- Allow about 2–3 minutes for the initial install and automatic reboot.

The Factory/Speaker FIR is stereo float32, 48 kHz, 32768 taps, NoPreamp, with
SHA-256 `8a8a3b2fc31a080a6bc40205f29ea6471df95adf357618b2025bdd193ef45c99`.
The default chunksize is 1024; 512, 2048 and 4096 are available in the Web UI.

## UI, profiles, measurement and recovery

This bundle contains the same complete AudioDSP 1.2 application as the Pi 2
release: separate Status/Measurement/Settings screens, live U7 selector state,
Speaker/Headphones fallback, per-profile bypass, Front-copy or independent Rear
FIR, woofer trim, client-side SVG FFT graphs, English female announcements,
UMIK 0°/90° calibration, level precheck, three-position room measurement,
32768-tap regularized correction, target/preset selection, A/B review and safe
permanent apply.

Completed measurement steps are navigation controls only. Data is kept until a
changed setting is explicitly applied or a level test, position restart, or
FIR rebuild is actually started. WAV uploads and full backups are staged and
validated before applying.

The versioned backup ZIP includes settings, correction preferences, Factory,
Speaker and Headphones FIRs, and both UMIK calibration files. Restore verifies
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
the target disk, copied payload hashes, generated Wi-Fi script, command line,
FAT volume and final disk identity.

See `AUDIODSP_REQUIREMENTS_VERIFIED.md` for the full verification record.
