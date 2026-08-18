# 측정과 DSP 설계

## 측정 전제

- UMIK-1과 Xonar U7을 모두 연결한다.
- calibration serial은 7200660이며 0°와 90° 파일을 별도 보관한다.
- 실제 룸 측정은 UMIK를 위로 세운 90° 방식만 허용한다. 0° 파일은 별도 활용·검증을 위해 저장하지만 최종 session orientation으로 고를 수 없다.
- 백색소음과 sweep 출력은 서로 독립이며 둘 다 기본 -42 dBFS다. 야간에는 -48 또는 -42 dBFS에서 시작한다. UI는 높은 출력 조합을 즉시 경고하고 실제 sweep 시작 전 현재 Front/Woofer 실효값을 다시 확인한다.
- 모든 측정 재생은 CamillaDSP를 중지한 상태에서 검증된 `audiodsp_announce` 4채널 ALSA 경로를 사용하므로 프로필 FIR을 거치지 않는다.
- 측정 동안 U7 Mic와 Line capture switch를 nocap으로 두며, arecord는 UMIK 장치를 직접 사용한다.

## 레벨 사전 평가

1. UMIK로 5초 무음을 기록한다.
2. U7로 낮은 백색소음을 재생하면서 약 5초를 기록한다.
3. background RMS, white-noise RMS, background를 제거한 signal RMS, SNR, peak를 계산한다.
4. clipping 또는 SNR 부족이면 NOT OK로 위치 측정을 막는다.
5. 사용자가 preamp/amp/U7 볼륨을 조절한 뒤 다시 실행한다.

레벨 검사를 실제 재실행하면 이전 위치 측정과 그로부터 나온 검증/FIR만 무효화한다. 화면 이동이나 dropdown 편집만으로는 지우지 않는다.

## 측정 신호와 응답 추정

- source: L, R, 선택적으로 Woofer를 각각 독립 재생
- 분리 SISO/MIMO의 Woofer 측정 감쇄는 기본 -9 dB이며 -18~0 dB에서 조절한다. reference도 같은 scale을 사용하므로 deconvolution 응답 레벨은 복원되고 SNR/headroom만 달라진다. 합산 `L+Woofer / R+Woofer` 모드에서는 이 값이 측정 조건이자 최종 Woofer trim이므로 임의 복원하지 않는다.
- positions: 청취 위치 주변 3곳
- rate: 48,000 Hz
- sweep 길이: UI 4/8/12초, engine 허용값 2~14초 짝수
- 각 녹음과 알려진 reference를 FFT하고 `Y·conj(X)/(abs(X)^2 + λ)` 형태로 deconvolution한다.
- `λ`는 reference 최대 power의 `1e-9`로 설정한다.
- bulk acoustic/device delay는 impulse peak에서 구하되 인과적인 0~250 ms 구간 안의 peak만 direct 응답으로 인정한다. 대역 제한 Woofer의 ESS 고조파·잡음 peak가 이 범위를 벗어나면 magnitude는 별도 SNR/통과대역 gate로 계속 사용하고 phase·decay·group delay·Front/Woofer 시간 정렬은 비활성화한다.
- UMIK calibration magnitude를 log-frequency 보간해 적용한다.
- ALSA/USB cold-start가 nominal 400 ms 준비 시간을 소비해도 50 ms AC-RMS envelope에서 실제 sweep 길이의 최대에너지 구간을 찾는다. 최대값의 0.5% 안에서는 nominal timing을 우선해 대역 제한 Woofer의 무음 고역 때문에 시작점이 밀리지 않게 하며, 검출한 capture delay를 deconvolution과 noise window에 함께 사용한다.
- 각 녹음의 검출된 sweep 전/후 noise PSD와 sweep 활성 구간으로 주파수별 SNR·신뢰도를 계산한다. Woofer는 chirp-time 에너지의 지속 -3 dB 통과대역을 자동 검출하고 실패할 때만 15~300 Hz로 되돌아간다. 6 dB 미만은 필터 생성을 막고, 15 dB 미만은 결과에 경고한다. 100 ms envelope 이상치는 생활소음 가능성으로 표시하지만 원본 impulse와 잔향을 잘라내지 않는다.

## 잔향과 장시간 공진

Deconvolution impulse의 직접음 전 0.1초부터 2.5초를 잘라 63/125/250/500/1k/2k/4k Hz octave band로 cosine taper FFT filtering한다. 각 band는 tail noise power를 빼는 Schroeder energy decay로 EDT와 T20→RT60, 회귀 `R²`를 계산한다. `R²≥0.80`, 0.05~5초 범위만 신뢰값으로 사용한다.

late reverberation을 FIR로 역보정하지 않는다. 다지점에서 신뢰 가능한 300 Hz 이하 decay가 기준보다 길고 이미 cut 대상인 공진에만 최대 3 dB를 추가 감쇄한다. 이는 잔향 시간을 물리적으로 줄이는 기능이 아니라 해당 모드의 excitation을 줄이는 안전한 cut-only 제어다. 중·고역 장시간 잔향은 UI 진단으로 보여주고 흡음·배치 대상으로 남긴다.

## 공간 결합

각 위치 응답은 먼저 주파수 의존 smoothing을 적용한다.

- 200 Hz 미만: 1/12 octave
- 200~2,000 Hz: 1/6 octave
- 2 kHz 초과: 1/3 octave

`equal`은 세 위치를 각 1/3로 dB 평균한다. `center`는 저역은 공간 대표성을 유지하고 고역으로 갈수록 중앙 위치 비중을 높인다. 동시에 위치별 표준편차를 저장하고, 편차가 큰 null의 boost 신뢰도를 낮춘다.

## Target과 취향

제공 target:

- Harman Kardon
- Flat
- Brüel & Kjær
- RTINGS
- AcoustiX Default
- Not Dr. Toole

파일은 `/usr/local/share/audiodsp/targets/target_*.txt`에 있고 API는 1 kHz 기준 곡선을 반환한다. 사용자는 bass tilt -6~+6 dB@20 Hz, treble tilt -6~+2 dB@20 kHz를 부드러운 house curve로 더할 수 있다.

Bass/treble tilt는 자동 룸 EQ의 boost/cut 제한을 계산한 뒤 별도의 명시적 house-curve 성분으로 더한다. 따라서 큰 Woofer 과출력으로 자동 cut이 포화되어도 사용자의 음색 선택이 사라지지 않는다. 이 성분도 correction window와 기준 대역 정규화를 따르고, 동일한 FIR peak 정규화 안에 포함된다. Woofer의 최종 correction은 여전히 0 dB 이하로 clamp해 선호도 때문에 자동 boost가 생기지 않는다. 결과 JSON은 `automatic_room_correction_db`와 `preference_correction_db`를 분리해 보여준다.

우퍼 억제 preset:

- `none`: 추가 저역 억제 없음
- `primus360`: 96 Hz -7 dB, Q≈3 기준
- `strong`: Primus360에 140 Hz low shelf -9 dB와 63 Hz -5 dB 성격을 추가

Preset은 boost를 만들지 않으며 Front에는 350 Hz 이하, Woofer에는 전체 설계 저역에 더한다. Woofer trim -18~0 dB는 별도로 더한다. 현재 선호 기본은 Harman + Strong + -9 dB다.

## 정규화와 안전 제한

- 타깃 레벨 정규화의 Front 기준 대역은 500~2,000 Hz, Woofer 기준 대역은 50~120 Hz median이다. 이는 위의 적응형 Woofer SNR 판정 대역과 별개다.
- 반 octave median 응답이 기준보다 10 dB 내려간 지점으로 자연 usable band를 추정한다.
- 자연 roll-off 밖에서는 positive correction을 금지한다.
- 공간 표준편차가 3 dB면 boost 신뢰도를 절반 수준으로 낮추는 soft regularization을 쓴다.
- positive correction은 tanh soft limit와 사용자 최대 boost 0/3/6/9 dB를 적용한다. 500 Hz 이상은 최대 3 dB로 더 제한한다.
- cut은 사용자 최대 6/9/12/18/24 dB에서 제한한다.
- Woofer correction은 20~180 Hz에서 cut-only다.
- 보정 범위는 사용자가 20/30/40/60/80 Hz 하한과 300/500/1k/5k/20k Hz 상한을 선택한다.
- 최종 FIR의 최대 전달 이득은 0 dB 이하가 되도록 정규화한다.

## 디지털 crossover와 실제 합산

`L/R/Woofer 개별` 측정에서는 디지털 crossover가 기본 `ON`, 기본 주파수는 100 Hz다. 선택 범위는 60/70/80/90/100/120 Hz다. Front WAV에는 Linkwitz–Riley 4차 HPF, Rear/Woofer WAV에는 같은 주파수의 LR4 LPF magnitude와 그 minimum-phase 전달함수를 넣는다. 별도의 CamillaDSP biquad/filter stage를 추가하지 않고 기존 32768탭 WAV에 곱해 합치므로 convolution 수, chunksize, block latency는 늘지 않는다. FIR 자체의 group delay/phase는 결과에 계속 기록한다.

두 branch를 따로 target에 맞추는 것으로 끝내지 않는다. acoustic bulk delay와 FIR energy delay를 맞춘 뒤 세 위치 각각의 `Front+Woofer` 복소합과 `|Front|+|Woofer|` 최악 구성 상한을 다시 계산한다. 상한이 target을 넘는 대역에는 두 branch에 같은 minimum-phase cut-only guard를 내장한다. null/cancellation은 boost로 메우지 않는다. 신뢰 가능한 상대 phase가 없거나 최종 복소합 MAE/P90을 통과하지 못하면 WAV는 Preview용으로 만들 수 있지만 `self_validation.overall_pass`와 crossover 상태는 PASS가 아니다.

`L+Woofer/R+Woofer 합산` 모드는 이미 하나의 FIR 뒤에서 Front/Rear를 복사하므로 두 출력을 독립 HPF/LPF로 분리할 수 없다. 이 모드에서는 crossover를 강제로 OFF하며, 사용자가 ON을 요청하면 조용히 무시하지 않고 계산을 거부한다. 독립 crossover가 필요하면 L/R/Woofer 개별 측정을 새로 해야 한다.

Woofer trim과 Strong/Primus 억제는 의도적인 target 이탈을 만들 수 있다. 구조적으로 유효한 WAV라는 사실과 acoustic target 달성을 분리해서 표시하며, 적용 후 작은 레벨의 별도 검증 sweep 전에는 실제 crossover 성공을 확정하지 않는다.

## Phase와 시간 정렬

기본 magnitude 설계는 minimum phase다. `bass` 모드는 중앙 위치의 측정 phase에서 minimum-phase 성분을 빼 excess phase를 구하고 지정 cutoff 80/120/160/200/250 Hz 아래에서만 보정한다. cutoff 전이는 cosine window이며 causality를 위한 shift는 최대 2048 samples로 제한한다. impulse 끝 10%는 fade한다.

L/R/Woofer 모드에서는 신뢰 가능한 중앙 위치 Front L/R 음향 bulk delay뿐 아니라 생성된 Front/Rear FIR의 에너지 중앙 지연까지 더한 전체 재생 지연을 비교한다. 필요한 상대 지연이 최대 3008 samples(약 62.7 ms) 안일 때만 더 빠른 쪽을 완전히 맞춘다. direct peak가 신뢰 불가하거나 필요한 지연이 한계를 넘으면 부분 지연도 넣지 않고 이유를 결과에 기록한다. L/R은 같은 phase·강도·상대 지연을 사용하며, dense FFT에서 magnitude 잔차가 0.75 dB를 넘으면 phase 강도를 축소하고 10% 미만이면 자동 해제한다. Woofer 한 개를 Rear L/R 두 채널에 같은 FIR로 복사하므로 T5S의 stereo 입력 케이블을 그대로 쓸 수 있다.

Phase 보정은 모든 반사를 완전히 역필터링하는 기능이 아니다. 위치별로 달라지는 고역 phase는 보정하지 않고, 시간적으로 비교적 일관된 저역과 source 정렬에만 제한한다.

## 출력과 검토

### U7 물리 출력 경로 고정

새 session은 아직 특정 프로필에 묶이지 않는다. `5초 무음 + 5초 백색소음`을 시작하기 직전에 HID monitor가 기록한 현재 boot의 U7 selector 상태를 읽고 `measurement_profile=speaker|headphone`과 state byte를 session에 저장한다. 오래된 boot ID, 없는 state 파일, 알 수 없는 profile이면 소리를 시작하지 않는다. MIMO는 실제 4채널 Speaker output에서만 고정할 수 있다.

모든 sweep/validation은 DSP를 멈추기 전, 각 재생 직전, 재생 polling 중, 재생 직후에 selector를 다시 확인한다. 상단 버튼을 눌러 경로가 달라지면 재생·녹음 프로세스를 종료하고 저장된 이전 측정값은 유지한 채 worker를 오류로 끝낸다. Preview도 현재 물리 경로가 원래 측정 경로와 같아야 한다. Apply는 생성 FIR을 측정한 프로필에만 허용하므로 한 출력 체인의 룸 응답을 다른 스피커 체인에 잘못 덮어쓸 수 없다.

- `Generated_Front_LR_32768.wav`: L/R 독립 stereo float32
- `Generated_Rear_LR_32768.wav`: Woofer FIR을 L/R 동일 복사하거나, L/R 모드에서 Front 복사본에 woofer trim을 bake한 stereo float32
- 둘 다 정확히 48 kHz, 32768 frames
- 결과 JSON에는 전/후 예상, effective target, 공간편차, requested/actual correction, octave decay, natural band, guarded bin, phase shift, impulse peak/energy, SHA-256가 포함된다.
- 생성 뒤 실제 32768탭 WAV를 다시 FFT해 설계 잔차, target-fit MAE/P90, 최대 전달 이득, 유한값, impulse 위치와 시간 정렬 안전성을 `self_validation`으로 검증한다. 그래프의 예상 후 곡선도 설계 배열이 아니라 실제 FIR FFT 기준이다.
- crossover 결과는 `result.crossover`에 내장 여부, 주파수, 추가 runtime filter/block latency(모두 0), 상한 guard, 복소합 target, phase 신뢰도와 상태를 저장한다.

브라우저 다운로드와 그래프 확인은 playback을 바꾸지 않는다. Preview는 runtime config만 임시 교체하며 profile WAV/settings는 그대로다. Apply에서 기존 파일을 백업한 뒤 정식 WAV를 교체한다.

현황/설정에서 표시하는 FIR FFT는 목표 음압 자체가 아니라 측정 응답에 곱하는 보정 전달함수다. 따라서 Harman target의 저역 상승·고역 하강과 같은 모양일 필요가 없다. 목표 달성 여부는 측정·보정 결과의 `effective_target_db`와 실제 FIR FFT 기반 `predicted_db`, 그리고 crossover 사용 시 Front+Woofer 합산 검증으로 판단한다.

모든 새 SISO/MIMO 결과는 `algorithm_revision`을 기록한다. 저장된 측정값으로 만든 이전 revision 결과는 보존·다운로드할 수 있지만 Preview/Apply할 수 없으며, FIR 계산만 다시 수행해야 한다. 최신 revision도 `self_validation.overall_pass=false`이면 정식 Apply를 엔진에서 거부한다.

## 작업 복구와 계산 성능

- 각 session 디렉터리의 `session.json`은 설정, level 검사, 측정 index, 생성 FIR 결과와 적용 이력을 자동 보존하고 `session-note.txt`는 최대 500자의 사용자 주석을 독립 보존한다. 주석 변경은 dependency invalidation을 일으키지 않는다.
- 저장 session 불러오기는 현재 session을 먼저 자동 저장한 뒤 응답 JSON과 Front/Rear WAV 등 완료 상태의 근거 파일을 검증한다. 검증 성공 시 레벨·3위치·FIR·적용 이력을 그대로 복원하며, 완료 숫자만 있고 근거 파일이 빠진 session은 불러오지 않는다.
- FFTW3f forward/inverse plan과 aligned buffer는 한 작업 안에서 재사용하고 호출마다 입력 buffer를 0으로 초기화한다.
- Pi 2 UI 예상치는 응답 채널당 약 70초, magnitude FIR 약 55초, bass-phase FIR 약 85초다. Pi 4/5는 각각 약 20/20/40초를 기준으로 표시하되 실제 시간은 sweep 길이와 온도에 따라 달라진다.
- 실행 PID와 `/proc/<pid>/cmdline`을 함께 확인한다. 전원·SSH·서비스 중단으로 worker가 사라지면 원본 session JSON을 조회만으로 덮어쓰지 않고 UI에 복구 가능한 오류 상태를 파생해 무한 진행 표시를 막는다.

## 연구 근거와 한계

Pi4/5의 다중 제어원 MIMO, 2026년까지 검토한 연구, 알고리즘 수식, 세 토폴로지, 전체 룸 요소 분류와 의도적으로 보정하지 않는 항목은 [MIMO_ROOM_TUNING.md](MIMO_ROOM_TUNING.md)에 별도로 고정한다. 모든 새 결과는 `Room_Tuning_Report.json/.md`에도 같은 경계를 저장한다.

구현은 다지점 응답 결합, smoothing, regularized inversion, boost 제한, 저역 위상 보정이라는 현대적 실내 보정 원칙을 반영한다. 참고 자료는 다음과 같다.

- [Room EQ Wizard EQ 도움말](https://www.roomeqwizard.com/help/help_en-GB/html/eqwindow.html)
- [arXiv:2409.10131](https://arxiv.org/abs/2409.10131)
- [arXiv:2109.04241](https://arxiv.org/abs/2109.04241)
- [University of Southampton ISVR publication 9229](https://resource.isvr.soton.ac.uk/staff/pubs/PubPDFs/Pub9229.pdf)
- [Schroeder integrated impulse method comparison, NRC Canada](https://nrc-publications.canada.ca/eng/view/object/?id=35a62e95-3db6-4704-abed-b9cd6a2ce11e)
- [Modal Equalization of Loudspeaker-Room Responses at Low Frequencies, JAES](https://secure.aes.org/forum/pubs/journal/?elib=12226)

Schroeder backward integration은 decay 추정에 사용한다. 능동 modal equalization 연구가 보여 주듯 저역 모드의 초기 decay는 재생 신호의 선택적 감쇄로 줄일 수 있지만, 이 구현은 별도 secondary radiator 제어나 방 전체 late-reverb cancellation을 주장하지 않는다. 알고리즘을 바꿀 때는 동일 측정 fixture의 수치 회귀, impulse causality, 최대 전달 이득, 청취 A/B를 모두 다시 검증해야 한다.
