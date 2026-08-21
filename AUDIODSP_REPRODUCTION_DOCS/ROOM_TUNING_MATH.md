# 룸튜닝 수학 설계와 감사 기준

이 문서는 `audiodsp-measurement.py`의 SISO 측정·보정 경로를 수식과 코드 단계가 대응하도록 설명한다. MIMO의 행렬 해법은 [MIMO_ROOM_TUNING.md](MIMO_ROOM_TUNING.md)에 이어서 기술한다. 아래에서 주파수는 `f`, 마이크 위치는 `p`, 출력원은 `s∈{L,R,W}`, 복소 전달함수는 `H`, dB magnitude는 `L=20 log10|H|`로 쓴다.

## 1. 선형 측정 모델

한 출력원의 ESS `x_s[n]`를 재생하고 UMIK-1로 받은 신호를 다음처럼 둔다.

```text
y_p,s[n] = h_p,s[n] * x_s[n-d] + n_p[n]
Y_p,s[k] = H_p,s[k] X_s[k] exp(-j2πkd/N) + N_p[k]
```

`d`는 ALSA/USB 준비 지연, `n`은 생활소음과 전기 잡음이다. 50 ms AC-RMS envelope로 실제 ESS 구간을 찾은 뒤 reference에 같은 `d`를 넣는다. 재생 dBFS와 Woofer 측정 감쇄는 녹음과 reference 양쪽에 동일하게 포함되므로 이상적인 선형계에서는 복원된 `H`의 크기를 바꾸지 않고 SNR과 headroom만 바꾼다.

복소 응답은 scale-relative Tikhonov 나눗셈으로 구한다.

```text
H_hat[k] = Y[k] X*[k] / (|X[k]|² + λ)
λ = 10⁻⁹ max_k |X[k]|²
```

현재 `λ`는 reference scale에 비례하는 하나의 작은 수다. 측정대역 밖의 거의 0인 `X`에서 발산하는 것을 막지만, 잡음 공분산을 직접 추정한 주파수별 Wiener/Tikhonov 해는 아니다. 대신 이후 단계에서 주파수별 SNR, 공간 편차, 자연 재생대역과 좁은 딥 신뢰도로 역필터 권한을 줄인다.

UMIK calibration `C_cal(f)`은 log-frequency 선형보간 후 magnitude에 더한다.

```text
L_cal(f) = 20 log10|H_hat(f)| + C_cal(f)
```

위상에는 calibration magnitude를 넣지 않는다. 90° 측정에서는 반드시 90° calibration을 쓴다.

## 2. SNR과 측정 신뢰도

ESS 전후에서 가장 조용한 안정 200 ms 구간의 잡음 PSD 중 큰 값을 사용한다. 각 평가 주파수의 ±1/24 octave FFT bin에서 다음 값을 구한다.

```text
P_sig(f) = max(0, median(|Y|²) - P_noise(f))
SNR(f) = 10 log10(P_sig(f) / P_noise(f))
c_noise(f) = clamp((SNR(f)-6)/9, 0, 1)
```

전체 ESS 품질의 6 dB는 사용 하한, 15 dB는 권장 하한이다. 긴 ESS의 matched-filter 이득은 2초 빠른 검사 대비 `10 log10(T/2)` dB로 기록한다. Woofer는 고역 무출력을 실패로 오인하지 않도록 시간-주파수 chirp 에너지에서 지속 -3 dB 통과대역을 찾고, 그 대역에서만 품질을 판정한다.

## 3. 주파수 smoothing

평가 grid는 20 Hz–20 kHz의 512개 log-frequency 점이다. 중심 `f_i`와 이웃 `f_j`의 거리를 `d_ij=|log2(f_j/f_i)|`로 두고, 지정 fractional-octave 폭 `B_i` 안에서 삼각 가중치를 쓴다.

```text
a_ij = max(0, 1 - d_ij/(B_i/2))
B_i = 1/12 octave, f<200 Hz
      1/6 octave,  200≤f<2000 Hz
      1/3 octave,  f≥2000 Hz
```

v22부터 측정 응답은 dB를 산술평균하지 않고 평균제곱 음압/전달 파워로 smoothing한다.

```text
L_sm(f_i) = 10 log10( Σ_j a_ij 10^(L_cal(f_j)/10) / Σ_j a_ij )
```

필터 gain과 cut-only 합산 guard는 별도 dB 산술 smoothing을 유지한다.

```text
G_sm(f_i) = Σ_j a_ij G(f_j) / Σ_j a_ij
```

두 연산을 분리하는 이유는 음향 응답의 평균제곱 목표와 필터 제약의 의미가 다르기 때문이다. 음수 cut 곡선을 파워 평균하면 0 dB 쪽으로 치우쳐 필요한 안전 감쇄가 약해질 수 있다.

## 4. 여러 마이크 위치의 공간 prototype

위치 가중치 `w_p(f)`는 `equal`에서 모두 같고, `center`에서는 200 Hz 이하 1/3·1/3·1/3, 2 kHz 이상 0.60·0.20·0.20이며 그 사이를 log-frequency로 보간한다. 각 위치의 실제 가중치는 잡음 신뢰도를 곱한다.

```text
q_p(f) = w_p(f) max(0.05, c_noise,p(f))
```

v22의 대표 응답은 평균제곱 전달응답이다.

```text
L_bar(f) = 10 log10( Σ_p q_p(f) 10^(L_p(f)/10) / Σ_p q_p(f) )
```

위치 불확실성은 같은 `q_p`를 사용한 dB population 표준편차다.

```text
μ_db(f) = Σ_p q_p L_p / Σ_p q_p
σ_db(f) = sqrt(Σ_p q_p (L_p-μ_db)² / Σ_p q_p)
```

이전 구현은 문서·결과에 power-response prototype이라고 표시했지만 실제로 `μ_db`를 대표 응답으로 사용했다. 이는 magnitude의 기하평균이다. Jensen 부등식에 따라 `L_bar≥μ_db`이므로 예를 들어 세 위치가 0/0/+12 dB이면 종전 값은 +4 dB, 새 값은 약 +7.56 dB다. 새 계산은 한 위치의 큰 에너지를 숨기지 않고, 반대로 한 위치에만 있는 깊은 null을 과도한 boost 근거로 쓰는 경향을 줄인다. Fast 1위치는 두 값이 정확히 같아서 공간 평균 변경의 영향이 없다.

## 5. 하나의 공통 음압 기준과 타깃

L/R의 공간 prototype을 합친 500–2000 Hz median을 측정 기준 `R_M`으로 삼는다. 선택 타깃과 bass/treble preference의 같은 대역 median은 `R_T`다.

```text
M_s(f) = L_bar,s(f) - R_M
T(f) = T_named(f) + T_preference(f) - R_T
e_s(f) = T(f) - M_s(f)
```

L, R, Woofer를 따로 0 dB로 맞추지 않는다. 측정 감쇄 -9 dB도 `H_hat`에서 제거되므로 Woofer 최종 trim과 혼동하지 않는다.

## 6. 정규화된 magnitude 보정

자동 보정 창 `W(f)`은 사용자가 고른 하한/상한에서 cosine taper된다. Front boost의 핵심 신뢰도는 다음과 같다.

```text
r_spatial(f) = 1 / (1 + (σ_db(f)/3)²)
r_notch(f)   = local broad/narrow shape reliability, 0…1
e0(f)        = e(f) W(f) c_noise(f)
```

일반 positive correction은 soft ceiling `B=max relative compensation`으로 제한한다.

```text
G_boost(f) = B tanh(e0 r_spatial r_notch / B)
```

L/R 양쪽에 공통으로 나타나는 넓은 roll-off가 SNR·채널 일치·local-shape 검사를 통과하면 edge SNR을 두 번 곱하지 않고 다음처럼 상한까지 사용할 수 있다.

```text
G_broad(f) = min(B, e(f) W(f) r_spatial(f) r_notch(f))
```

좁은 null은 최대 +3 dB이며 자연 usable band 밖의 단독 딥은 boost하지 않는다. cut은 500 Hz 이상에서 공간 신뢰도를 곱하고, 500–2000 Hz는 최대 -6 dB, 2 kHz 이상은 최대 -3 dB로 더 보수적이다. 저역 peak cut은 사용자 `max_cut` 안에서 허용한다.

Woofer 자동 EQ는 20–180 Hz에서 cut-only다.

```text
G_W,auto(f) = min(0, clamp(T_without_preference(f)-M_W(f), -C, 0)
                     W(f)c_noise(f))
G_W(f) = min(0, G_W,auto + G_preset + trim + preference)
```

따라서 Flat/추가 억제 없음/trim 0 dB은 먼저 측정된 과출력을 타깃까지 내리는 기준 조합이다. Primus360/Strong/음수 trim은 그 결과에서 사용자가 의도적으로 저역을 더 줄인다. 신뢰 가능한 긴 저역 decay가 있고 그 주파수가 이미 cut 대상이면 최대 3 dB를 추가 감쇄하지만, late reverberation 자체를 역필터링하지 않는다.

## 7. minimum-phase FIR와 제한된 excess phase

설계 magnitude `G[k]`에서 real cepstrum minimum-phase FIR을 만든다.

```text
c[n] = IRFFT(ln|G[k]|)
c_min[0] = c[0]
c_min[n] = 2c[n], 0<n<N/2
c_min[N/2] = c[N/2]
G_min[k] = exp(RFFT(c_min[n]))
g_min[n] = IRFFT(G_min[k])
```

32768 taps만 남기고 끝 10%를 cosine fade한다. `bass phase`에서는 중앙 위치의 측정 위상에서 측정 magnitude의 minimum phase를 빼 excess phase를 구한다.

```text
φ_ex(f) = unwrap(φ_measured(f)) - φ_minimum(f)
G_mixed(f) = G_min(f) exp(-j β(f) φ_ex(f))
```

`β(f)`는 저역에서 1, phase cutoff의 70–100% 구간에서 0으로 감쇠한다. 비인과 에너지 99.5%를 앞쪽으로 옮기는 delay는 최대 2048 samples다. 잘라낸 실제 FIR의 magnitude residual이 0.75 dB를 넘으면 `β`를 이분탐색으로 줄이고, 10% 미만이면 phase 보정을 끈다. L/R은 한 공통 excess-phase 법칙과 한 공통 delay를 사용해 stereo 위상을 따로 흔들지 않는다.

## 8. 디지털 crossover와 복소 합산

LR4 magnitude는 `x=f/f_c`에 대해 다음과 같다.

```text
|LP4| = 1/(1+x⁴)
|HP4| = x⁴/(1+x⁴)
```

`f=f_c`에서 두 branch는 각각 0.5, 즉 -6.0206 dB이고 magnitude 합은 1이다. 이 전달함수도 minimum-phase FIR에 포함하므로 별도 CamillaDSP stage나 block latency를 추가하지 않는다.

실제 합산은 dB 합이 아니라 복소합이다.

```text
H_sum,p(f) = H_F,p(f) G_F(f) + H_W,p(f) G_W(f)
```

동일 녹음 Walsh L+R+W 기준이 신뢰되면 주파수별 상대위상을 사용한다. 정밀 모드의 L+W/R+W 실측은 다음 cross-term을 제공한다.

```text
|H_F+H_W|² = |H_F|² + |H_W|² + 2|H_F||H_W|cos(Δφ)
cos(Δφ) = (|H_sum|²-|H_F|²-|H_W|²)/(2|H_F||H_W|)
```

두 branch 중 하나가 너무 작으면 `Δφ`를 관측할 수 없으므로 사용하지 않는다. 합산 실측과 개별 응답이 삼각 부등식 밖이면 라우팅·레벨·비선형 불일치로 보고 계산을 차단한다. 위상 기준이 불신이면 예상값은 에너지합 `sqrt(|F|²+|W|²)`, 안전 guard는 최악 동상 상한 `|F|+|W|`를 사용한다. guard는 cut-only이고 target 초과량만 공동 감쇄한다.

합산 결과 그래프와 타깃 MAE는 4절과 같은 주파수별 위치/SNR 가중 mean-square 응답을 쓴다. 상대 지연·극성 탐색도 같은 가중 위치 오차를 최소화한다. 다만 constructive-overlap 안전 guard는 평균으로 약화하지 않고 측정 위치 중 최대 합산을 계속 사용한다. 즉 “대표 음색”과 “최악 위치 headroom”은 의도적으로 서로 다른 통계량이다.

지연 탐색이 과도한 Woofer를 역상 상쇄해 타깃에 맞춘 것처럼 보이지 않도록 다음 cancellation deficit도 목적함수에 넣는다.

```text
D_cancel,p(f) = max(0,
  10log10(|F_p|²+|W_p|²) - 20log10|F_p+W_p|)

J_delay = MAE_target + 0.45 P90_target + 0.75 P90(D_cancel)
```

후보 지연의 cancellation P90이 현재 지연보다 0.25 dB 넘게 나빠지면, 타깃 오차가 줄어도 자동 적용하지 않는다. 이 값은 “위상을 모두 같게 만들기”가 아니라 crossover 대역의 파괴적 상쇄를 EQ 수단으로 악용하지 않기 위한 강건성 제약이다.

## 9. 하나의 FIR bank gain

모든 magnitude, phase, delay, crossover, 합산 guard를 적용한 뒤에만 bank 전체 peak를 구한다.

```text
P = max_s max_f |G_s(f)|
α = min(1, 1/P)
G_s,final(f) = αG_s(f), 모든 s에 같은 α
```

채널별 독립 정규화는 금지한다. 이 방식은 L/R/W 상대레벨과 crossover 합산을 유지하고 digital preamp 없이 최대 전달을 0 dB 이하로 만든다. positive 보상 10 dB는 최대 약 10 dB의 전체 음량 비용이 될 수 있다.

## 10. 자동 검증

실제 저장 직전 32768-tap FIR을 다시 FFT해 설계 magnitude와 비교한다. constant normalization offset을 제거한 구현 잔차는 MAE ≤0.25 dB, P95 ≤0.80 dB여야 한다.

타깃 오차는 log-frequency 평가점의 절대오차 `a_i=|L_pred(f_i)-T(f_i)|`다.

```text
MAE = (1/N) Σ_i a_i
P90 = quantile_0.90({a_i})
```

MAE는 전체 평균, P90은 평가점 90%가 그 값 이하라는 뜻이다. 하나의 큰 딥이 평균에 숨는 것을 막기 위해 둘 다 본다. 독립 Woofer branch는 full-range 타깃 판정 대상이 아니고 L+W/R+W 최종 합산이 전체 타깃을 판정한다.

선택형 사후 검증도 위치 응답을 같은 평균제곱 방식으로 통합하고, 실측 L/R과 예상 L/R 각각에 한 번의 공통 500–2000 Hz 기준만 적용한다. 타깃 오차와 `예상↔실측` 오차를 분리한다. 이 차이가 크면 FIR 수학만의 문제가 아니라 스피커 비선형/압축, 아날로그 crossover, 물리 노브, 라우팅, 시간 변화, 측정 SNR도 원인 후보로 표시해야 한다.

## 11. v22 감사에서 수정한 오류

| 항목 | 종전 문제 | 수정 |
|---|---|---|
| 위치 통합 | power prototype이라고 기록했지만 dB 산술평균 사용 | noise-confidence weighted 평균제곱 응답으로 변경 |
| 공간 편차 | center/SNR 가중치와 무관한 단순 표준편차 | 실제 prototype과 같은 가중치의 dB 표준편차 사용 |
| fractional smoothing | 음향 응답의 dB 산술평균 | 응답은 파워 평균, 필터 gain은 dB 평균으로 분리 |
| 사후 검증 | 위치별 dB 산술평균 | 설계와 같은 weighted power prototype 사용 |
| 이전 세션 | 옛 response JSON과 새 계산을 구분하지 않음 | revision 기록, 원본 WAV 무음 재계산 안내, 이중 smoothing 금지 |
| shared clock | 환경변수 suffix가 이중 prefix되어 override 무시 | `AUDIODSP_PHASE_CLOCK_SHARED`를 정확히 읽도록 수정 |
| L+W/R+W 대표값 | 위치별 dB 중앙값이 개별 응답의 power prototype과 불일치 | 같은 위치/SNR 가중 mean-square 응답으로 통일; 안전 상한은 위치 최대 유지 |
| 상대 지연 탐색 | 주파수마다 위치 오차 중앙값을 사용해 한 위치를 쉽게 무시 | 같은 위치/SNR 가중 MAE로 통일 |
| 위상 상쇄 | 큰 Woofer를 역상 상쇄해 타깃 오차만 줄이는 후보가 선택될 수 있음 | 에너지합 대비 cancellation P90 penalty 및 0.25 dB 비악화 gate 추가 |
| MIMO center 모드 | 150 Hz 이하에서도 중앙 위치를 고정 60% 가중 | 200 Hz 이하 세 위치 동일 기하 가중, 측정 신뢰도만 추가 반영 |
| LR4 문서식 | 구현은 `x⁴`인데 문서가 `x²`로 잘못 표기 | 표준 4차 branch magnitude 식으로 수정 |

## 12. 의도적인 한계

- 3개 위치는 청취영역 표본이지 방 전체의 연속 sound field가 아니다.
- 한 개 우퍼의 stereo 입력은 두 독립 MIMO 제어원이 아니다.
- 깊은 room null, SBIR, 좌석별 고역 phase, late reverberation, 지향성, 비선형 왜곡, 구조전달 층간소음은 FIR로 완전 제거하지 않는다.
- 현재 SISO magnitude 해는 full covariance 최적화가 아니라 해석 가능한 신뢰도-제한 역필터다. 주파수별 noise covariance Tikhonov, local PCA spatial estimator, common excess-phase zero 식별은 연구 근거가 있지만 실제 하드웨어 A/B와 causality/ringing 회귀 없이 자동 활성화하지 않는다.
- 실제 적용 성공은 합성 예측만으로 확정하지 않는다. Preview 뒤 낮은 레벨 사후 ESS, CPU/XRUN, 청취 A/B를 별도 기록한다.

## 13. 연구 근거

- O. Kirkeby와 P. A. Nelson, [Digital Filter Design for Inversion Problems in Sound Reproduction](https://resource.isvr.soton.ac.uk/staff/pubs/PubPDFs/Pub9229.pdf): 정규화된 역필터와 ill-conditioned 주파수의 안정화.
- A. Carini 외, [Multiple position room response equalization in frequency domain](https://doi.org/10.1109/TASL.2011.2158420): 다중 위치 주파수영역 룸 보정.
- W. Jin 외, [Acoustic Room Compensation Using Local PCA-based Room Average Power Response Estimation](https://arxiv.org/abs/2206.15356): spatial average power response `r(ω)|G_eq(ω)|²=t(ω)` 목표와 위치 변화 강건성.
- M. Karjalainen 외, [Equalization of loudspeaker and room responses using Kautz filters](https://doi.org/10.1155/2007/60949): 직접 역산이 깊은 딥과 ringing을 악화시키는 문제.
- D. Wang 외, [Identification of Common Excess-Phase Zeros … via Ringing Quantification](https://doi.org/10.1016/j.apacoust.2025.111153): 공통 excess-phase 성분과 ringing 기반 제한.
- W.-L. Lin 외, [Multichannel room response equalization … linearly constrained approach](https://doi.org/10.1121/10.0017721): 넓은 제어영역의 제약식 기반 multichannel 보정.
- A. Farina, [Simultaneous Measurement of Impulse Response and Distortion with a Swept-Sine Technique](https://angelofarina.it/Public/Papers/134-AES00.PDF): ESS 측정과 선형 응답/비선형 고조파의 분리.
