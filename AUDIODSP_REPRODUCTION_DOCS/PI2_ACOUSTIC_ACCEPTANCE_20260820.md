# Pi 2 실제 음향 수락 시험 · 2026-08-20

## 범위와 안전 상태

- 장치: Raspberry Pi 2 Model B v1.1, Xonar U7, UMIK-1, T5S Woofer
- 세션: `20260820_192126`
- 구성: `정밀 분리+합산`, Fast 1위치, L/R/Woofer/L+Woofer/R+Woofer
- 본 스윕: 출력별 14초, 최종 출력 `-29 dBFS`
- 측정 중 CamillaDSP 우회와 U7 입력 mute를 사용했고 완료 후 세 서비스를 복구했다.
- Preview와 정식 적용은 실행하지 않았다. 정식 Speaker FIR SHA-256
  `8a8a3b2fc31a080a6bc40205f29ea6471df95adf357618b2025bdd193ef45c99`는 시험 전후 동일하다.

## 발견한 오류와 수정

1. T5S의 실제 음향 통과 구간은 2초 ESS 중 약 0.16초라서 고정 0.20초 조건이 유효한 우퍼를 거부했다. 우퍼 최소 유효 구간을 50 ms로 바꾸고 지속 -3 dB 통과대역만 평가했다.
2. 빠른 스윕 timing이 음악·스위칭 transient를 따라 nominal보다 약 350 ms 앞을 선택했다. 1초 중역 signature의 FFT correlation을 예상 ALSA 시작 범위 안에서만 탐색하도록 바꿨다. 대역 제한 우퍼에는 이 timing 판정을 강제하지 않는다.
3. 본 스윕은 instantaneous RMS SNR만 사용해 2초 빠른 검사보다 불리하게 판정됐다. ESS matched-filter의 coherent integration 이득을 2초 기준으로 더하도록 고쳤다. 14초에서는 `10·log10(14/2) = 8.451 dB`다.
4. root worker를 일반 Web 사용자로 조회할 때 `kill(pid, 0)`의 `PermissionError`를 종료로 오인했다. `/proc/<pid>/cmdline`을 함께 확인해 실행 중인 worker를 정확히 표시한다.
5. 저장 원본 재계산 중 이전 source의 품질 카드가 잠시 남았다. source 시작 시 stale 품질을 지우고 현재 stage/progress만 표시하도록 수정했다.

## 실제 빠른 검사

`-30 dBFS` 첫 실행에서는 R만 5.56 dB로 6 dB 하한에 0.44 dB 부족했다. UI가 `+1 dB`, 즉 `-29 dBFS`를 제안했고 같은 저장 경로로 다시 실행했다.

| 출력 | `-29 dBFS` 빠른 SNR | 판정 |
|---|---:|---|
| L | 7.12 dB | PASS |
| R | 8.80 dB | PASS |
| Woofer | 7.67 dB | PASS |
| L+Woofer | 14.32 dB | PASS |
| R+Woofer | 12.30 dB | PASS |

6 dB는 응답 생성이 가능한 하한이고, 15 dB는 더 안정적인 권장값이다. 6–15 dB도 적용 가능한 PASS지만 공간 평균과 좁은 대역 판단의 여유가 적다는 경고를 유지한다.

## 실제 14초 본 측정

저장된 다섯 원본을 소리 없이 같은 엔진으로 재계산했다. 표의 유효 SNR은 `원신호 SNR + 8.451 dB ESS 적분 이득`이다.

| 출력 | 원신호 SNR | 유효 SNR | 판정 |
|---|---:|---:|---|
| L | 3.00 dB | 11.45 dB | PASS |
| R | -1.04 dB | 7.42 dB | PASS |
| Woofer | 17.53 dB | 25.98 dB | 권장 PASS |
| L+Woofer | 12.48 dB | 20.93 dB | 권장 PASS |
| R+Woofer | 16.00 dB | 24.45 dB | 권장 PASS |

필터 전 합산 magnitude closure도 독립 정규화 없이 통과했다.

| 합산 | MAE | P90 |
|---|---:|---:|
| L+Woofer | 0.085 dB | 0.339 dB |
| R+Woofer | 0.143 dB | 0.525 dB |

U7 출력과 UMIK-1 입력은 공통 hardware clock이 아니므로 서로 다른 sweep 사이의 절대 위상은 검증값으로 쓰지 않았다. 위 표는 동일 reference scale의 magnitude closure이며, 최종 필터에는 위상 비의존 에너지 타깃과 최악 동상 합산 cut-only 상한을 사용했다.

## 실제 FIR 시나리오

모든 파일은 48 kHz, stereo float32, 채널당 32768 taps이며 실제 FIR FFT로 구현 오차와 전달 이득을 다시 검사했다.

| Target / 억제 / trim / crossover | 결과 | 해석 |
|---|---|---|
| Flat / 없음 / 0 dB / 100 Hz | PASS | 필수 기준값. L/R/W 구현 MAE 0.064/0.057/0.070 dB |
| Harman / 없음 / 0 dB / 100 Hz | FAIL | R P90 7.55 dB로 허용 7 dB 초과 |
| Harman / 없음 / 0 dB / 80 Hz | FAIL | 실제 측정 조건에서 100 Hz보다 악화 |
| Harman / 없음 / 0 dB / 120 Hz | PASS | L/R target P90 5.70/6.68 dB |
| Flat / Strong / -4 dB / 100 Hz | FAIL | crossover 상한 guard 초과 |
| Flat / Strong / -4 dB / 120 Hz | FAIL | L P95 1.234 dB, 허용 1 dB 초과 |
| Flat / Strong / -5 dB / 120 Hz | FAIL | L P95 1.229 dB. Woofer trim을 더 내려도 Front 지배 구간은 거의 변하지 않음 |

비기준 옵션은 물리 응답과 사용자가 의도한 추가 감쇄 때문에 반드시 PASS하는 값이 아니다. FAIL 안내는 더 이상 무조건 Woofer trim을 내리라고 하지 않고, 실제 최악 주파수와 지배 branch를 보여준 뒤 화면의 `Crossover 주파수`, `우퍼 과잉 억제`, `Woofer 최종 trim` 또는 T5S 위치·극성·LPF를 조치로 제시한다.

시험 종료 시 세션 결과는 필수 기준인 Flat/없음/0 dB/100 Hz PASS로 되돌렸다. Harman/없음/0 dB/120 Hz PASS 결과도 PC의 `.test-output/pi2-acceptance-20260820`에 별도 보존했지만 정식 프로필에는 적용하지 않았다.

## 무음 회귀와 Web/CamillaDSP 검증

- 측정 엔진 synthetic 회귀: PASS
- Target 6종 × 억제 3종 18조합: PASS
- 기준값에서 모든 UI FIR 값을 한 축씩 바꾼 94개 시나리오: 구조·단조성·필수 Flat 기준 PASS
- Profile/Web 상태 4096개: 정상 3968 + 의도된 오류 128, PASS
- 설정 operation 56개의 ordered pair 3136개, 고유 CamillaDSP 설정 28개, Preview topology 16개, 동시 write 33개: PASS
- 측정 완료·FIR SHA 변경 시 1회 갱신, idle/진행 중 반복 전체 새로고침 없음: PASS
- 세 서비스 active, `/api/status`와 `/api/volume` 정상, 정식 FIR SHA 불변: PASS

Fast 1위치는 한 청취점의 기능·기준 필터 수락 시험이다. 머리 위치 주변의 공간 안정성까지 주장하려면 같은 정밀 5경로 구성을 Standard 3위치로 다시 측정해야 한다.
