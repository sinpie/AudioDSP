# AudioDSP

AudioDSP is a Raspberry Pi audio processor for Xonar U7, CamillaDSP and UMIK-1. It provides 48 kHz convolution, independent Speaker/Headphone profiles, optional Front/Woofer routing, hardware-volume API control, a responsive Web UI, and a guided one/three-position room-correction workflow that produces 32768-tap stereo FIR WAV files. The recommended precision SISO mode measures L/R/W and L+Woofer/R+Woofer before FIR design so crossover closure can be verified without a later surprise sweep. Pi 4/5 additionally support an experimental robust 2-input×4-output MIMO FIR bank; Pi 2 deliberately remains SISO-only.

## Source and release bundles

Shared runtime, assets and tests have one canonical copy under `source/common`.
Platform overlays are under `source/platforms/pi2|pi3|pi4|pi5`; deterministic
deployable payloads are materialized into the ignored `build/<platform>`
directory. Do not edit generated build files.

- `releases/audiodsp-pi2-v1.2.0`: Raspberry Pi 2 Model B v1.1, 32-bit armhf image writer and runtime
- `releases/audiodsp-pi4-pi5-v1.2.0`: Raspberry Pi 4/5, 64-bit aarch64 image writer and runtime

Downloaded Raspberry Pi OS images, CamillaDSP binaries, private SSH keys, device backups and logs are intentionally excluded from Git. Each release README explains where the prerequisites belong and how the writer assembles and validates canonical source before writing an SD card.

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

Start with [the reproduction documentation](AUDIODSP_REPRODUCTION_DOCS/README.md), then read the platform release README. Architecture, API, UI/UX, backup compatibility, testing, safety and algorithm details have one canonical copy in `AUDIODSP_REPRODUCTION_DOCS`; release folders contain only platform entry points and immutable external inputs.

The latest silent regression records are [SILENT_CALIBRATION_SELF_VALIDATION_20260818.md](AUDIODSP_REPRODUCTION_DOCS/SILENT_CALIBRATION_SELF_VALIDATION_20260818.md) and [MIMO_VALIDATION_REPORT_20260818.md](AUDIODSP_REPRODUCTION_DOCS/MIMO_VALIDATION_REPORT_20260818.md). The algorithm, topology and correction limits are documented in [MIMO_ROOM_TUNING.md](AUDIODSP_REPRODUCTION_DOCS/MIMO_ROOM_TUNING.md).

For a deliberately conservative follow-up to a low-level sweep A/B, `diagnostics/refine_tonal_fir.py` can derive a listening-preview FIR from `frequency_comparison.csv`. It locks 0–120 Hz, applies only broad correction of at most about 1 dB above 120 Hz, preserves the approved FIR phase/peak tap and emits a WAV, JSON audit, CSV comparison and vector SVG. It is not a substitute for multi-position acoustic validation; preview the result before installing it.

## References

AudioDSP uses these publications and primary technical sources as design references. A reference does not imply that every method is implemented; adopted, adapted and deliberately deferred techniques are distinguished in [MIMO_ROOM_TUNING.md](AUDIODSP_REPRODUCTION_DOCS/MIMO_ROOM_TUNING.md).

- Dirac Research, [Active Room Treatment technology overview](https://www.dirac.com/resources/art-technology), [ART setup guide](https://helpdesk.dirac.com/en/dirac-art/Setup-Guide-c3cb), and [public MIMO framework](https://www.dirac.com/wp-content/uploads/2024/06/Dirac-MIMO-framework-for-active-room-treatment-and-Unison-.pdf).
- *Compensation of Loudspeaker–Room Responses in a Robust MIMO Control Framework*, IEEE/ACM Transactions on Audio, Speech, and Language Processing, [doi:10.1109/TASL.2013.2245650](https://doi.org/10.1109/TASL.2013.2245650), 2013.
- S. Koyama and K. Arikawa, *Weighted Pressure Matching Based on Kernel Interpolation for Sound Field Reproduction*, [arXiv:2210.14711](https://arxiv.org/abs/2210.14711), 2022.
- Y. S. Chen and M. R. Bai, *Weighted Acoustic Model Matching with Kernel Interpolation for Acoustic Zone Control*, Journal of Sound and Vibration, [doi:10.1016/j.jsv.2025.119489](https://doi.org/10.1016/j.jsv.2025.119489), 2026.
- W.-L. Lin, Y.-S. Chen, B.-R. Lai and M. R. Bai, *Multichannel Room Response Equalization with a Broadened Control Region Using a Linearly Constrained Approach and Sensor Interpolation*, Journal of the Acoustical Society of America, [doi:10.1121/10.0017721](https://doi.org/10.1121/10.0017721), 2023.
- M. Karjalainen, T. Paatero, J. N. Mourjopoulos and P. D. Hatziantoniou, *About room response equalization and dereverberation*, and related Kautz-filter work summarized by Aalto University, [Equalization of loudspeaker and room responses using Kautz filters](https://research.aalto.fi/en/publications/equalization-of-loudspeaker-and-room-responses-using-kautz-filter). This supports avoiding aggressive inversion of deep room dips that would create ringing.
- J. G. Tylka and E. Y. Choueiri, *Loudspeaker equalization for a moving listener*, [Aalto University research record](https://research.aalto.fi/en/publications/loudspeaker-equalization-for-a-moving-listener/), 2022, for spatially robust multi-position evaluation.
- *Robust multipoint equalization using p-norm optimization*, [DAGA 2012 paper](https://pub.dega-akustik.de/DAGA_2012/data/articles/000216.pdf), for separating common spatial problems from position-specific nulls.
- A. Farina, *Simultaneous Measurement of Impulse Response and Distortion with a Swept-Sine Technique*, [AES paper](https://angelofarina.it/Public/Papers/134-AES00.PDF), for exponential swept-sine measurement and the boundary between linear FIR correction and nonlinear distortion.
- D. Wang, Z. Liu, Y. Han, K. Pan and Y. Shen, *Identification of Common Excess-Phase Zeros for Single-Input Multiple-Output Room Response Equalization via Ringing Quantification*, Applied Acoustics, [doi:10.1016/j.apacoust.2025.111153](https://doi.org/10.1016/j.apacoust.2025.111153), 2026.
- T. Welti and A. Devantier, *Low-Frequency Optimization Using Multiple Subwoofers*, Journal of the Audio Engineering Society, [AES E-Library 13680](https://secure.aes.org/forum/pubs/journal/?elib=13680), 2006.
- H. Enquist, [CamillaDSP](https://github.com/HEnquist/camilladsp): official convolution, mixer and runtime implementation documentation.
- miniDSP, [Which direction should I point the UMIK-1?](https://support.minidsp.com/support/solutions/articles/47000681633-which-direction-should-i-point-the-umik-1-), for matching 0°/90° calibration to microphone orientation.
- FFTW, [Precision](https://www.fftw.org/doc/Precision.html) and [accuracy discussion](https://www.fftw.org/accuracy/comments.html), for the float32 `fftwf` implementation choice and numerical validation boundary.
- Raspberry Pi Ltd., [Raspberry Pi 5 product brief](https://datasheets.raspberrypi.com/rpi5/raspberry-pi-5-product-brief.pdf), [official 2 GB model announcement](https://www.raspberrypi.com/news/2gb-raspberry-pi-5-on-sale-now-at-50/) and [Raspberry Pi OS documentation](https://www.raspberrypi.com/documentation/computers/os.html).

## Validation

Run the matching writer with `-ValidateOnly -NoPause` before writing media. The repository also contains exhaustive isolated profile/Web tests and synthetic measurement-engine tests. Actual acoustic acceptance testing must be performed with UMIK-1 at 90° in the intended listening area and requires explicit permission before AudioDSP emits measurement sound.

For a user-authorized full SISO option audit, `diagnostics/run_full_option_matrix.py` generates the baseline plus every selectable value one axis at a time (67 Front/Woofer FIR pairs). `diagnostics/build_option_validation_sequence.py` streams those exact 32768-tap convolutions into one low-level four-channel WAV, `diagnostics/capture_option_validation.py` records it through the production DSP-bypass/U7-input-off path, and `diagnostics/analyze_option_validation.py` reports every L/R sweep, before/after target error, SNR, transient contamination and option-family monotonicity. This is explicit value coverage, not the multi-million-member Cartesian product of every simultaneous combination.
