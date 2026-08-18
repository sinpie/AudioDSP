# AudioDSP

AudioDSP is a Raspberry Pi audio processor for Xonar U7, CamillaDSP and UMIK-1. It provides 48 kHz convolution, independent Speaker/Headphone profiles, optional Front/Woofer routing, hardware-volume API control, a responsive Web UI, and a guided three-position room-correction workflow that produces 32768-tap stereo FIR WAV files. Pi 4/5 additionally support an experimental robust 2-input×4-output MIMO FIR bank; Pi 2 deliberately remains SISO-only.

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

Room correction combines three nearby measurements, guarded/regularized cut-biased magnitude correction, optional low-frequency excess-phase correction, bass-mode control and octave-band decay diagnostics. The optional MIMO path jointly optimizes independently measured speakers/subwoofers at the three positions, with frequency-dependent regularization, natural-rolloff penalties, correlated-input headroom limits and causal-delay checks. Late reverberation, nonlinear distortion and structural noise are diagnosed or marked unmeasured—not claimed as fixed by FIR.

## Documentation

Start with [the reproduction documentation](AUDIODSP_REPRODUCTION_DOCS/README.md), then read the platform release README. Architecture, API, UI/UX, backup compatibility, testing, safety and algorithm details are maintained in both release bundles.

The latest silent regression records are [SILENT_CALIBRATION_SELF_VALIDATION_20260818.md](AUDIODSP_REPRODUCTION_DOCS/SILENT_CALIBRATION_SELF_VALIDATION_20260818.md) and [MIMO_VALIDATION_REPORT_20260818.md](AUDIODSP_REPRODUCTION_DOCS/MIMO_VALIDATION_REPORT_20260818.md). The algorithm, topology and correction limits are documented in [MIMO_ROOM_TUNING.md](AUDIODSP_REPRODUCTION_DOCS/MIMO_ROOM_TUNING.md).

For a deliberately conservative follow-up to a low-level sweep A/B, `diagnostics/refine_tonal_fir.py` can derive a listening-preview FIR from `frequency_comparison.csv`. It locks 0–120 Hz, applies only broad correction of at most about 1 dB above 120 Hz, preserves the approved FIR phase/peak tap and emits a WAV, JSON audit, CSV comparison and vector SVG. It is not a substitute for multi-position acoustic validation; preview the result before installing it.

## References

AudioDSP uses these publications and primary technical sources as design references. A reference does not imply that every method is implemented; adopted, adapted and deliberately deferred techniques are distinguished in [MIMO_ROOM_TUNING.md](AUDIODSP_REPRODUCTION_DOCS/MIMO_ROOM_TUNING.md).

- Dirac Research, [Active Room Treatment technology overview](https://www.dirac.com/resources/art-technology) and [ART use-case definition and setup guidelines](https://www.dirac.com/wp-content/uploads/2025/05/ART_Use-case-definition-and-setup-guidelines.pdf), 2025.
- *Compensation of Loudspeaker–Room Responses in a Robust MIMO Control Framework*, IEEE/ACM Transactions on Audio, Speech, and Language Processing, [doi:10.1109/TASL.2013.2245650](https://doi.org/10.1109/TASL.2013.2245650), 2013.
- S. Koyama and K. Arikawa, *Weighted Pressure Matching Based on Kernel Interpolation for Sound Field Reproduction*, [arXiv:2210.14711](https://arxiv.org/abs/2210.14711), 2022.
- Y. S. Chen and M. R. Bai, *Weighted Acoustic Model Matching with Kernel Interpolation for Acoustic Zone Control*, Journal of Sound and Vibration, [doi:10.1016/j.jsv.2025.119489](https://doi.org/10.1016/j.jsv.2025.119489), 2026.
- W.-L. Lin, Y.-S. Chen, B.-R. Lai and M. R. Bai, *Multichannel Room Response Equalization with a Broadened Control Region Using a Linearly Constrained Approach and Sensor Interpolation*, Journal of the Acoustical Society of America, [doi:10.1121/10.0017721](https://doi.org/10.1121/10.0017721), 2023.
- D. Wang, Z. Liu, Y. Han, K. Pan and Y. Shen, *Identification of Common Excess-Phase Zeros for Single-Input Multiple-Output Room Response Equalization via Ringing Quantification*, Applied Acoustics, [doi:10.1016/j.apacoust.2025.111153](https://doi.org/10.1016/j.apacoust.2025.111153), 2026.
- T. Welti and A. Devantier, *Low-Frequency Optimization Using Multiple Subwoofers*, Journal of the Audio Engineering Society, [AES E-Library 13680](https://secure.aes.org/forum/pubs/journal/?elib=13680), 2006.
- H. Enquist, [CamillaDSP](https://github.com/HEnquist/camilladsp): official convolution, mixer and runtime implementation documentation.

## Validation

Run the matching writer with `-ValidateOnly -NoPause` before writing media. The repository also contains exhaustive isolated profile/Web tests and synthetic measurement-engine tests. Actual acoustic acceptance testing must be performed with UMIK-1 at 90° in the intended listening area and requires explicit permission before AudioDSP emits measurement sound.
