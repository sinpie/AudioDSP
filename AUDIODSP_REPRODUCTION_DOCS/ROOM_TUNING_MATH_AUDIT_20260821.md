# AudioDSP 룸튜닝 수학·실측 감사 — 2026-08-21

## 감사 범위

이번 감사는 저장된 Pi5 Fast 세션 `20260820_192126`을 기준으로 다음 경로를 다시 확인했다.

1. UMIK-1 raw WAV와 calibration을 이용한 ESS 전달함수 추정
2. fractional-octave smoothing과 다중 위치 공간 통합
3. Flat/Harman 등 target과 L/R/Woofer 공통 음압 기준
4. boost/cut, 좁은 null 보호, 32768-tap 인과 FIR 합성
5. Front HPF/Woofer LPF, L+Woofer/R+Woofer 물리 합산과 상대위상 탐색
6. 완성된 4채널 FIR bank의 공통 NoPreamp 정규화
7. 계산 그래프, 자동검증, CamillaDSP Preview/Apply 상태 전이
8. 실제 Preview FIR을 통과한 사후 L+Woofer/R+Woofer sweep

세부 식과 구현 위치는 [ROOM_TUNING_MATH.md](ROOM_TUNING_MATH.md)에 기록한다. 이 문서는 그 식을 실제 저장 세션과 합성 fixture에 적용한 감사 결과다.

## 수정한 수학·로직 오류

### 응답과 공간 평균

- 측정 magnitude smoothing과 다중 위치 통합을 dB 산술평균에서 전달 파워 평균으로 바꿨다.
- 사용 식은 `10 log10(Σ w·10^(L/10) / Σw)`이며, 위치 가중치와 주파수별 SNR confidence를 같은 계산에 사용한다.
- 0/0/+12 dB 세 위치 fixture는 dB 평균 +4 dB가 아니라 평균제곱 +7.56 dB가 되어야 한다.
- cut-only 합산 보호 곡선은 파워 평균하면 0 dB 쪽으로 약해질 수 있으므로 별도 dB-domain smoothing을 유지한다.

### 공통 레벨과 FIR gain

- L/R/Woofer를 따로 0 dB로 맞추지 않고 L/R 500~2,000 Hz 중앙값 하나를 측정·target 공통 기준으로 사용한다.
- Front L/R와 Rear L/R 네 FIR을 완성한 뒤 전체 bank 최대 전달값으로 common gain을 한 번만 적용한다.
- 따라서 측정 시 Woofer 감쇄, 최종 Woofer trim, crossover branch 관계와 L/R 차이는 normalization 뒤에도 그대로 보존된다.

### crossover와 위상

- LR4 branch는 실제 4차 magnitude를 사용하며 Front HPF와 Woofer LPF를 FIR에 포함한다. 별도 runtime crossover나 추가 block latency는 없다.
- 독립 U7/UMIK clock의 별도 sweep 위상을 절대 거리 지연으로 사용하지 않는다.
- 같은 녹음의 Walsh L/R/W 기준과 L+Woofer/R+Woofer cross-term을 이용할 수 있을 때만 복소 합산을 신뢰한다.
- 상대 delay/극성 탐색은 target 오차만 줄이는 후보가 에너지합보다 더 깊은 파괴 상쇄를 만들면 선택하지 않는다. cancellation-deficit P90은 기준보다 0.25 dB 이상 나빠질 수 없다.

### 사후 검증 stream 전환

- v22 실제 검증에서 첫 L+Woofer만 30~130 Hz가 예상보다 5~18 dB 높았지만 R+Woofer는 정상으로 나왔다.
- v21/v22 입력 WAV는 활성 채널이 정확히 -30 dBFS이고, Rear FIR 좌우 및 버전 차이는 저역에서 대부분 0~2 dB였다. FIR channel index나 공통 normalization은 원인이 아니었다.
- 문제 L 녹음은 처음 0.4초가 약 -47 dBFS에서 감쇠했고, sweep 전/후 noise 추정이 -50.0/-75.93 dBFS로 25.93 dB 달랐다. 저장 JSON도 `switching_transient_suspected=true`였다.
- 원인은 production CamillaDSP를 멈추고 독립 WavFile graph를 여는 전환과 직전 출력의 감쇠가 0.35초 lead를 넘어 저역 ESS 시작에 겹친 것이다.
- v23은 사후 입력 파일에 2초 무음 lead를 둔다. active sweep transient가 검출되거나 sweep 전/후 noise floor 불일치와 음향 지표 FAIL이 함께 있으면 PASS도 확정 FIR FAIL도 아닌 `판정 보류 · 출력 전환 감지`로 분류한다. 전·후 바닥 차이만 있고 target/crossover/prediction이 모두 PASS면 유효 응답을 버리지 않는다.
- 완료 응답은 `저장 결과 재판정`으로 소리 없이 최신 판정식을 적용할 수 있다.

### 빠른 SNR 저장 원본 재분석

- 실제 빠른 sweep은 Front 30 Hz~22 kHz, Woofer 15~320 Hz, 합산 15 Hz~22 kHz로 재생하지만 저장 원본 재분석은 하나의 15 Hz~22 kHz reference를 재사용하고 있었다.
- v23은 source별 재생 대역, 짧은 tail, Woofer 측정 감쇄까지 원 WAV와 같은 reference를 다시 생성한다. 따라서 우퍼의 재생 불가능한 고역이나 프런트의 30 Hz 아래가 SNR 판정에 잘못 들어가지 않는다.
- 사후 검증의 SNR·권장 출력 안내에는 본 측정 dBFS가 아니라 사용자가 `검증 sweep 입력`에서 고른 실제 dBFS를 전달한다.

## 저장 세션 v21 → v22 무음 계산 비교

동일 raw WAV에서 response revision과 FIR만 다시 계산했다.

| 항목 | v21 | v22 | 해석 |
| --- | ---: | ---: | --- |
| Left target MAE | 0.558 dB | 0.327 dB | 개선 |
| Right target MAE | 0.484 dB | 0.410 dB | 개선 |
| 15–20 kHz 최악 잔여오차 | 4.537 dB | 3.986 dB | 개선, 10 dB 상대보상 상한은 유지 |
| Woofer 공통기준 median 오차 | +0.043 dB | -0.033 dB | 공통 레벨 유지 |
| Left 약 129 Hz dip | -5.49 dB | -4.73 dB | 완화 |
| Right 약 146 Hz dip | -10.86 dB | -10.34 dB | 좁은/깊은 null 보호로 제한 |
| 공통 음량 비용 | 10.000 dB | 10.000 dB | 선택한 최대 상대보상과 일치 |

복소 합산 target MAE는 L 0.668→0.818 dB, R 0.941→1.063 dB로 약간 늘었다. 이는 target만 맞추기 위해 더 깊은 파괴 상쇄를 만드는 delay 후보를 거부한 결과이며 안전성 우선의 의도된 trade-off다.

## 실제 음향 검증

### 이전 v21 기준

- -25 dBFS, 28초 ESS
- 최소 SNR 14.29 dB
- target MAE/P90: L 1.434/3.102 dB, R 1.475/2.765 dB
- crossover target MAE/P90: L 1.600/3.384 dB, R 1.798/3.367 dB
- 예상↔실측 MAE/P90: L 1.373/3.000 dB, R 1.494/3.049 dB
- 전체 PASS 후 기존 정식 FIR로 복귀

### v22 첫 검증과 판정 수정

- -30 dBFS, 28초 ESS
- R target MAE/P90 1.541/3.626 dB, crossover 2.275/4.494 dB로 PASS
- L target MAE/P90 4.034/13.914 dB, crossover 8.153/14.016 dB로 실패처럼 보였으나 stream 전환 오염이 확인됨
- 이 결과는 v23 판정에서 확정 FIR 실패가 아니라 재측정 보류 대상이다.
- 시험 종료 후 원래 Speaker FIR SHA-256 `8a8a3b2fc31a080a6bc40205f29ea6471df95adf357618b2025bdd193ef45c99`, U7 입력, -10 dB 청취 볼륨으로 복귀했다.

### v23 2초 무음 lead 재검증

- -25 dBFS, 28초 ESS, 각 sweep 전 2초 무음
- 최소 SNR 18.54 dB
- target MAE/P90: L 1.388/3.337 dB, R 1.340/2.953 dB
- crossover target MAE/P90: L 1.748/3.978 dB, R 1.990/4.409 dB
- 예상↔실측 MAE/P90: L 1.318/2.885 dB, R 1.278/2.706 dB
- L/R 공통 기준 level 차이 0.652 dB, shape 차이 중앙값 1.368 dB
- 모든 target/crossover/prediction 지표 PASS
- L noise side spread 1.50 dB, R 5.19 dB였지만 active sweep transient는 양쪽 모두 검출되지 않았다. R 지표도 모두 PASS이므로 R의 stationary floor 변화만으로 판정을 보류하면 false negative다.
- 이 실제 결과를 이용해 `noise floor 차이만 있음 + 모든 음향 지표 PASS`는 PASS를 유지하고, active transient 또는 `noise floor 차이 + 음향 지표 FAIL`만 판정 보류로 수정했다.
- 종료 후 기존 Speaker FIR SHA, -10 dB 볼륨, U7 입력과 service 상태를 복원했다.

## 자동 시험 기준

- 측정 엔진은 power smoothing, weighted power 공간 통합, legacy response 비재평활, raw 재처리 무효화, destructive-cancellation guard, 자연 Woofer 대역, 환경변수 override를 검사한다.
- Target option matrix는 Flat/추가 억제 없음/trim 0 dB/100 Hz/max 상대보상 10 dB/max cut 18 dB 기준을 반드시 PASS시키고, target/preset 18조합과 설정 95시나리오를 계산한다.
- MIMO matrix는 SISO와 같은 위치/SNR 가중치를 solver·그래프·MAE에 사용하고 spatial consistency를 검사한다.
- Profile/Web matrix는 4096 profile 상태, Preview/Apply/rollback, volume, chunksize, fallback, UI 상태와 사후 검증의 PASS/SNR 보류/출력 전환 보류를 검사한다.

## 최종 실행 결과

- 측정 엔진 회귀: PASS
- Target option matrix: 6 targets, target/preset 18조합, SISO 설정 95시나리오 PASS
- MIMO algorithm matrix: PASS, SISO/MIMO spatial weight consistency `true`
- Profile/Web matrix: 4096 상태와 실제 CamillaDSP parser PASS
- Pi 2/3/4/5 materializer: 각각 39/39/37/37 파일 assemble 후 canonical source 일치 PASS
- Pi5 설치 source SHA-256: measurement `ad5a50acc5d1bcba318873d8f8b999e1861fa0b6339dd6f95eceeea7944dd5c9`, Web `1bc1370cd5510127363ee827529f91af20a37bb82f8656250460bf2d0469d8cc`
- Pi5 저장 사후 응답 무음 재판정: `verification_status=pass`, `application_blocking=false`, 전체 self-validation PASS
- Pi5 최종 활성 정식 FIR은 시험 전과 같은 SHA-256 `8a8a3b2fc31a080a6bc40205f29ea6471df95adf357618b2025bdd193ef45c99`
- Pi5 최종 상태: Preview OFF, U7 Speaker, 실제/저장 볼륨 -10 dB, 입력 restored, `camilladsp`/`audiodsp-web`/`audiodsp-profile-monitor` active

## 물리적으로 남는 한계

- 깊고 위치에 민감한 room null은 FIR boost로 제거하지 않는다.
- FIR은 late reverberation을 소거하지 않는다. 신뢰되는 긴 저역 decay의 excitation만 cut-only로 줄인다.
- 한 우퍼 SISO는 넓은 청취영역의 저역을 같은 레벨로 만들 수 없다. MIMO도 제어점과 headroom/coherence 범위 안에서만 개선한다.
- 20 kHz 부근 roll-off를 완전히 평탄하게 만들려면 전체 bank 음량 비용이 커질 수 있으므로 선택한 최대 상대보상 안에서만 보정한다.
- 사후 실측 PASS는 마이크 위치, 물리 볼륨, 케이블, U7 출력 상태가 원측정과 같다는 조건에서 유효하다.
