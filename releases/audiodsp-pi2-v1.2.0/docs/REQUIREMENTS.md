# AudioDSP 요구사항 기준선

## 오디오와 하드웨어

- 입력: Rod Rain preamp의 볼륨 연동 stereo analog 출력을 Xonar U7 Line input에 연결한다.
- 출력: U7 Front L/R은 인티앰프, Rear L/R은 T5S의 stereo pass-through 입력으로 연결한다.
- 캡처와 재생은 48 kHz로 고정한다. U7 실제 출력은 24-bit packed, CamillaDSP 내부 ALSA 형식은 32-bit container다.
- 디지털 preamp는 사용하지 않는다.
- U7가 USB reset되거나 Pi가 재부팅되면 저장된 출력 볼륨을 복원한다.

## 프로필

- Speaker와 Headphones 설정을 각각 별도 카드로 보여준다.
- U7의 실제 선택 프로필 카드만 외곽선을 강조한다.
- 선택 프로필 Front FIR이 없으면 다른 프로필을 그대로 사용하고, 둘 다 없으면 Factory를 사용한다.
- Rear FIR이 없으면 Front convolution 결과를 Rear로 복사해 2채널 convolution만 수행한다.
- Rear FIR이 있고 separate를 선택하면 Front/Rear L/R을 독립 처리해 4채널 convolution을 수행한다.
- 프로필별 bypass와 -18~0 dB woofer trim을 제공한다.
- U7 물리 전환 때 조용한 영어 여성 음성 `Speaker`/`Headphones`를 현재 음악에 Front L/R로 믹스한다.
- CamillaDSP 초기화가 끝나면 `DSP ready`를 현재 출력으로 알린다.

## 출력 볼륨

- 현황 화면에서 U7 실제 전역 PCM 볼륨을 읽는다.
- Slider, ±1 dB, preset, 명시적 적용 버튼을 제공한다.
- Web/API 쓰기 범위는 정수 -60~0 dB다.
- `GET /api/volume`은 실제값과 재부팅 저장값을 함께 반환한다.
- `PUT /api/volume`은 값을 즉시 적용하고 저장한다.
- 볼륨 변경으로 CamillaDSP나 FIR을 재시작·재생성하지 않는다.
- 물리 노브 변경은 약 3초 안에 표시하되 자동 저장하지 않는다.

## Web UI/UX

- 화면은 현황, 측정·보정, 프로필·설정으로 분리한다.
- PC와 모바일에서 흐름, 현재 상태, 다음 동작이 한눈에 보여야 한다.
- OS 기본 색상 선호로 light/dark를 자동 선택하고 사용자가 Auto/Light/Dark를 고를 수 있다.
- 그래프는 이미지가 아닌 SVG이며 FIR FFT는 브라우저에서 계산한다.
- 현재 FIR 그래프는 L/R 또는 L/R+Woofer를 선택해 본다.
- 설정에서 WAV를 올리면 정식 적용 전에 기존/새 FIR 곡선과 A/B를 확인한다.
- 중복 submit을 막고, 정식 덮어쓰기·재측정·복원에는 확인 문구를 제공한다.

## 측정과 보정

- 0°와 90° UMIK-1 calibration 파일을 별도로 업로드·검증·보관한다.
- 최종 룸 측정은 UMIK를 천장으로 향한 90° calibration만 허용한다.
- 먼저 5초 무음과 5초 저레벨 백색소음을 측정해 background, signal, SNR, peak, clipping을 평가한다.
- NOT OK이면 사용자가 기기 볼륨을 수동 조절하고 다시 검사한다.
- 측정 재생 중 CamillaDSP를 direct bypass하고 U7 Mic/Line capture switch를 끈다. 종료·실패·취소 때 원상 복구한다.
- L/R 또는 L/R/Woofer를 청취 위치 근처 세 지점에서 각각 측정한다.
- L/R/Woofer 모드는 소스를 따로 측정하며 중앙 위치 bulk delay로 Front/Woofer 시간 정렬을 계산한다.
- Woofer 단독/합산 측정음은 Front보다 12 dB 낮추고 reference도 같은 비율로 낮춰 응답 크기는 정확히 보존한다.
- 각 sweep의 무음 pre-roll과 활성 구간으로 개별 SNR을 검증하며 6 dB 미만은 거부하고 15 dB 미만은 경고한다.
- 옥타브별 noise-compensated Schroeder EDT/T20 잔향을 산출한다. 신뢰 가능한 300 Hz 이하 장시간 공진만 최대 3 dB cut-only로 더 감쇄하고 late reverb는 역보정하지 않는다.
- 출력은 48 kHz stereo float32, 32768 taps의 Front WAV와 필요할 때 Rear WAV다.
- Target은 Harman, Flat, Brüel & Kjær, RTINGS, AcoustiX, Not Dr. Toole을 제공하고 선택 곡선을 즉시 SVG로 보여준다.
- 우퍼 preset은 none, Primus360 수준, Strong을 제공한다. 기본은 Strong + woofer trim -9 dB다.
- magnitude-only 또는 저역 excess-phase 옵션을 제공한다.
- 사용자가 보정 대역, 최대 boost/cut, bass/treble tilt, 공간 가중을 고를 수 있다.
- 결과는 전/후 예상 곡선, 공간 편차, 진단, impulse peak delay, hash를 보여준다.
- 결과는 실제 32768탭 FIR FFT를 다시 계산해 target-fit MAE/P90, 설계 구현 오차, 최대 전달 이득, 유한값, impulse 위치를 셀프 검증한다.
- WAV는 한 개면 직접, 두 개면 ZIP으로 브라우저 다운로드한다.
- 기존/이번 튜닝을 임시 전환한 뒤 별도 적용 버튼에서만 프로필 WAV를 덮어쓴다.

## 단계와 데이터 수명

- 완료된 상단 step은 클릭해 이전 단계로 이동할 수 있다.
- 이동 자체로 측정값을 버리지 않는다.
- 설정을 편집만 한 상태도 기존 결과를 유지한다.
- 변경된 설정을 실제 적용하거나 level/position/build를 다시 실행할 때 영향받는 단계 이후만 초기화한다.
- 새 session은 기존 session 폴더를 보존하고 현재 포인터만 새 session으로 바꾼다.

## 백업과 복구

- 전체 백업은 settings, correction preferences, Factory/Speaker/Headphones FIR, 0°/90° calibration을 버전형 ZIP으로 내려받는다.
- 복원은 upload → schema/hash/크기/WAV/Cal 검증 → 내용 확인 → 명시적 적용 순서다.
- 적용 직전 현재 전체 상태를 Pi 내부 rollback ZIP으로 자동 저장한다.
- 지원하지 않는 미래 schema는 아무 변경 없이 거부한다.
- 알려진 이전 schema는 default로 보완하고 알 수 없는 설정 key는 안전하게 무시한다.
- 출력 볼륨 저장값은 `profile-settings.json`을 통해 백업·복원된다.

## 성능과 플랫폼

- Pi 2는 chunksize 2048이 기본이고 UI/상태 polling/브라우저 FFT를 저부하로 유지한다.
- Pi 4/5는 chunksize 1024가 기본이다.
- Pi 2에서도 측정 녹음과 FFTW3f FIR 계산을 완료할 수 있고 진행률과 예상시간을 보여준다.
- 설정을 바꾸지 않는 상태 조회는 불필요한 Python child process와 FFT를 반복하지 않는다.

## 전체 룸 튜닝 진단과 결과 분류

- 각 결과는 `Room_Tuning_Report.json`과 사람이 읽는 `Room_Tuning_Report.md`로 session에 영구 보관하고 Web에서 내려받을 수 있어야 한다.
- 모든 항목을 `fir_correctable`, `mimo_correctable`, `limited_fir`, `limited_mimo`, `diagnostic_placement`, `physical_treatment`, `not_measured`, `not_certified`, `runtime_validation` 중 하나로 분류한다.
- 측정 gate에는 calibration 방향, background/noise SNR, peak/clipping을 포함한다.
- 보정 설계에는 target magnitude, 자연 저역 한계와 boost/headroom, 위치간 편차, L/R 일치, 도달시간·극성·저역 excess phase, crossover를 포함한다.
- 시간영역 진단에는 EDT/T20뿐 아니라 C50, C80, D50, center time, direct-to-remainder, 초기 반사 창과 20~300 Hz group delay를 포함한다.
- late reverberation, SBIR/초기반사, 비선형 왜곡·compression, directivity/off-axis, binaural/IACC, 절대 SPL·청력·층간 구조전달, clock/XRUN과 독립 post-verification을 누락하지 않는다.
- FIR/MIMO가 직접 해결하지 못하는 항목은 성공으로 표시하지 않는다. 배치·흡음·베이스트랩·방진·출력 제한·추가 계측 같은 물리적 또는 운영상 대책을 별도로 제시한다.

## MIMO 룸 보정

- 실시간 MIMO는 Pi 4/5에서만 허용하고 Pi 2에서는 manager와 UI가 명시적으로 차단한다.
- `MIMO Stereo`는 Front L/R 두 독립 actuator, `MIMO 2.1`은 Front L/R과 한 T5S, `MIMO 2.2`는 Front L/R과 독립 배치한 두 서브우퍼를 사용한다.
- 세 청취 위치에서 각 actuator를 독립 sweep하고, 좌표 없는 세 측정점에 robust weighted complex pressure matching을 적용한다.
- 주파수별 Tikhonov regularization, SISO prior, 자연 저역·support penalty, 공통 target phase, row-sum headroom projection과 인과 지연을 적용한다.
- 출력 bank는 48 kHz stereo float32, 정확히 32768 taps인 WAV 네 개이며 CamillaDSP에서는 8 convolution path가 된다.
- MIMO 범위는 기본 20~120 Hz이고 80/120/150 Hz 중 선택한다. 범위 위에서는 SISO로 부드럽게 복귀한다.
- 결과에는 condition/coherence, target MAE, 위치간 편차, headroom, causality와 modeled modal tail을 표시한다. modal tail이 0.5 dB보다 악화되면 경고하며 잔향 개선으로 인증하지 않는다.
- 현재 구현은 ART와 같은 다중 음원 제어 계열이지만 Dirac ART의 독점 구현과 동등하다고 주장하지 않는다. 지속 방사장 억제, 좌표 기반 wave-field control과 비선형 제어는 범위 밖이다.

## 네트워크와 이름

- Pi 2는 Ethernet DHCP 전용이며 onboard Wi-Fi가 없음을 명시한다.
- Pi 4/5는 Ethernet DHCP와 사용자가 writer에 넣은 Wi-Fi를 함께 구성한다.
- DHCP 실패 시 고정/비상 주소를 쓰지 않는다.
- 새 설치 hostname/user/app 식별자는 `audiodsp`다.
- Web은 TCP 8080의 신뢰된 LAN 전용 서비스다.
