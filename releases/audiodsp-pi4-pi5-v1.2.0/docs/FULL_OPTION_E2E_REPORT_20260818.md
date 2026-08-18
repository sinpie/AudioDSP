# Complete option and acoustic end-to-end validation — 2026-08-18

The authoritative report is reproduced in
`AUDIODSP_REPRODUCTION_DOCS/FULL_OPTION_E2E_REPORT_20260818.md` at repository
root.  The common SISO code was validated on the connected Pi 2, Xonar U7 and
UMIK-1 with one L+Woofer, one R+Woofer and one Woofer-only generation sweep; 67
option variants and 134 FIR WAVs; 136/136 usable low-level acoustic captures;
two successful selective SNR retries; generated-FIR preview/apply/restore; and
the 4096-state Web/profile, engine, target and MIMO-isolation regressions.

The Pi 4/5 image shares that validated SISO implementation.  MIMO engine and
isolated runtime tests passed, while sound-producing MIMO acceptance remains a
Pi 4/5 hardware task.  Pi 5 2 GB is the recommended AudioDSP-only target; 4 GB
is not required because CPU, USB scheduling and XRUN margin dominate memory.

