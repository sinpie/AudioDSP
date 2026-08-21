# MIMO 수학·측정·UI 감사 (2026-08-21)

## 결론

AudioDSP의 2입력×4출력 MIMO는 저역의 다중 위치 복소 pressure matching으로서 기본 방향은 타당하다. 이번 감사에서는 다음 네 가지를 수정했다.

1. 행렬 피벗 비율을 조건수처럼 표시하던 진단을 실제 유도 1-노름 조건수 `||A||₁||A⁻¹||₁`로 교체했다.
2. 한 위치에서 대역 밖인 출력 하나의 신뢰도가 낮다는 이유로 그 위치 전체를 버리던 `min(confidence)` 정책을 제거했다.
3. 출력별·주파수별 측정 불확실성을 별도 diagonal robust regularization으로 넣고, 조건수 10,000 초과 시 필요한 만큼만 자동 diagonal loading을 추가한다.
4. 평균 타깃과 평균 좌석 편차만 좋아지고 특정 측정 위치가 나빠지는 해를 막기 위해 모든 측정 위치의 타깃 MAE 비악화 조건을 적용 gate에 추가했다.

우퍼 최종 trim은 물리 Rear 출력의 전달 상한으로 이미 강제된다. 이전 코드는 음수 trim을 support regularization에도 다시 곱해 우퍼를 이중 억제할 수 있었으므로, 이번 수정에서 trim과 support penalty를 분리했다. `보조 출력 사용 제한`은 반대편 Front·Woofer를 얼마나 적극적으로 쓸지 정하고, `우퍼 최종 트림`은 실제 우퍼 출력 상한만 정한다.

## 전달행렬과 목적함수

주파수 `f`, 측정 위치 `p=1..P`, 물리 제어원 `a=1..A`, 프로그램 입력 `c∈{L,R}`에 대해 다음을 둔다.

- `H_p,a(f)`: 제어원 `a`에서 위치 `p`까지 측정한 복소 전달함수
- `g_a,c(f)`: 입력 `c`를 제어원 `a`로 보내는 보상 필터
- `d_c(f)`: 선택 타깃의 크기와 기존 안전한 SISO 도착 위상을 가진 목표 압력
- `W(f)=diag(w_p)`: 위치·측정 신뢰도 가중치
- `g₀`: 검증된 SISO+crossover 기준 해
- `g_prev`: 바로 전 주파수 bin의 해

구현 목적함수는 다음과 같다.

```text
min_g  ||W^1/2 (H g - d)||²
     + gᴴ R_uncertainty g
     + λ ||D_usable D_support g||²
     + μ ||g - g₀||²
     + ν ||g - g_prev||²
```

정규방정식은 다음과 같다.

```text
A g = b

A = Hᴴ W H + R_uncertainty + R_control + (μ + ν)I
b = Hᴴ W d + μg₀ + νg_prev
```

`A`는 regularization 뒤 Hermitian positive-definite가 되도록 구성한다. 해는 부분 피벗 복소 Gaussian elimination으로 구한다. actuator 수가 2~4이고 MIMO 계산 대역이 최대 180 Hz 전이구간까지라, 각 `A e_j = x_j`를 추가로 풀어 `A⁻¹`의 열을 얻고 정확한 induced 1-norm condition number를 계산해도 전체 메모리에는 영향이 거의 없다.

조건수 10,000은 음질을 증명하는 정리가 아니라 측정 오차 증폭을 막는 보수적 engineering gate다. 초과하면 평균 대각 성분의 `1e-5`부터 10배씩 diagonal loading을 늘리고 최대 6회 안에 기준을 만족시키며, 적용 주파수 수와 최대 추가량을 결과 JSON/UI에 남긴다.

## 측정 신뢰도 처리

이전 행 가중은 한 위치의 모든 출력 confidence 중 최솟값이었다.

```text
old: w'_p = w_p min_a c_p,a
```

Front가 25 Hz를 거의 재생하지 못하거나 Woofer가 150 Hz 위에서 약하면, 다른 출력으로 충분히 제어 가능한 위치도 함께 0에 가까워지는 문제가 있다. 새 행 가중은 actuator set 전체의 관측 가능성을 나타내는 quadratic mean을 사용한다.

```text
row_confidence_p = sqrt(mean_a(c_p,a²))
w'_p = normalize(w_p row_confidence_p)
```

대신 각 actuator 열의 신뢰도는 별도로 계산한다.

```text
c_a = Σ_p w'_p c_p,a
r_a = α |H|² (1-c_a)² / max(c_a², 0.04)
R_uncertainty = diag(r_a)
```

이는 출력별 전달함수 오차를 서로 독립인 multiplicative uncertainty로 놓았을 때 `E[(H+ΔH)ᴴW(H+ΔH)]`에 생기는 양의 대각 항을 근사한다. 낮은 신뢰도의 출력만 덜 쓰고, 다른 출력이 유효한 위치의 공간 정보는 보존한다. UI는 actuator별 confidence P10과 자동 안정화 bin 수를 표시한다.

## 위치·타깃·공통 레벨 검증

타깃 MAE를 계산하기 위해 위치별 또는 채널별로 별도 normalize하지 않는다. L/R/MIMO bank 전체에 하나의 40~130 Hz 공통 정렬 scalar만 사용한다. 상관입력 headroom 때문에 전체 bank가 내려간 값은 별도의 실제 감쇄로 보고하고, 음색 형상 MAE에서만 공통 정렬한다.

검증은 다음을 모두 요구한다.

- L/R 가중 target MAE가 기존 SISO보다 0.25 dB 넘게 악화되지 않음
- 평균 좌석 편차가 0.10 dB 넘게 악화되지 않음
- 각 L/R의 모든 측정 위치 MAE가 기존보다 0.75 dB 넘게 악화되지 않음
- 평활 전달함수 impulse-tail proxy가 1.5 dB 넘게 악화되지 않음
- 실제 1-노름 조건수 ≤10,000
- 변환·32768탭 절단 후 각 물리 출력의 `|G_L|+|G_R|`가 상관입력 headroom 이내
- 모든 경로 finite, 하나의 공통 인과 shift, 하나의 공통 bank gain

세 위치는 설계점이자 in-sample 검증점이다. 위치별 gate는 한 위치 희생을 막지만 미측정 위치의 일반화 성능을 증명하지 않는다. 정식 수락은 A/B 미리듣기와 선택형 적용 후 합산 실측, 가능하면 설계에 쓰지 않은 추가 위치 측정이 필요하다. 향후 별도 validation 위치가 추가되면 설계 위치와 검증 위치를 UI에서 분리해야 한다.

목표 복소 위상은 Front 단독이 아니라 현재 배포되는 SISO+LR4 Front/Sub 합산의 세 위치 가중 pressure에서 얻는다. 이 기준을 빠뜨리면 crossover 대역에서 목표 phase와 실제 baseline phase가 달라질 수 있다. 제어원 독립성도 여러 주파수 복소값을 먼저 합치지 않고 주파수마다 세 위치 공간 coherence를 계산한 뒤 P90을 쓴다. 따라서 단순한 시간 지연에 의한 주파수별 phase 회전을 독립 제어 자유도로 오인하지 않는다.

## 합산 측정의 올바른 역할

선형·시간불변이고 위상 동기된 MIMO 전달행렬에서는 독립 응답을 알면 합산은 이미 결정된다.

```text
H_(L+W) g = H_L g_L + H_W g_W
```

같은 L+W/R+W 측정을 동일 목적함수에 다시 넣으면 독립 정보가 늘기보다 해당 조합을 이중 가중할 수 있다. 따라서 MIMO 생성은 독립 물리 제어원 열만 사용한다. 합산 측정은 다음 용도에는 유효하다.

- 독립 측정과 실제 합산이 맞는지 확인하는 linearity/closure 검사
- polarity·상대 위상·아날로그 LPF가 모델과 일치하는지 확인
- FIR 적용 뒤 실제 4채널 경로의 선택형 수락 시험

반대로 현재 `정밀 분리+합산 SISO`에서 L+W/R+W는 독립 USB DAC/ADC clock의 세션 간 상대 위상 불확실성을 보완하는 실제 합산 제약이라 유용하다. SISO와 MIMO의 측정 역할을 UI 설명에서 분리했다.

## 위상 기준과 적용 차단

복소 MIMO에서 출력 간 상대 위상이 틀리면 해 자체가 무의미해진다. 별도 ESS 녹음의 시작 시각과 USB clock이 독립이면 각 응답의 bulk delay만 복원해도 출력 간 절대 위상이 보장되지 않는다. 현재 production MIMO는 다음 두 조건을 모두 요구한다.

1. Pi4/5 64-bit 연산 지원
2. `phase_clock_shared=true`로 검증된 출력–마이크 timing reference

조건이 없으면 Web의 MIMO 모드, 생성 API, 적용 API를 차단하고 이유를 세션 화면에 표시한다. Pi5라는 이유만으로 허용하지 않는다. U7+UMIK-1 독립 USB 구성에서는 `정밀 분리+합산`이 현재 안전한 기본이다. SISO의 동시 Walsh L/R/W 기준을 MIMO 2.2까지 일반화하려면 4제어원 공통-bin 분리, ESS 위상 anchor 보정, closure 오차 gate를 별도 구현·실측해야 하며 이번 변경에서 지원된 것으로 표시하지 않는다.

## UI 흐름 감사

- `세션`을 1~6단계의 선행 탭으로 분리했다. 새 세션, 저장 세션 검색·불러오기·삭제, MIMO 가능 여부를 한 화면에서 처리한다.
- 활성 세션 ID·주석·완료 위치·이어갈 단계는 탭 위에 계속 보인다.
- SISO 화면에서는 사용되지 않는 MIMO 세부값을 숨긴다.
- MIMO일 때만 `세 위치 전달행렬 → 불확실성·조건수 안정화 → 32768탭×8경로` 카드와 세 핵심 옵션을 표시한다.
- `MIMO 강도`는 `안정성/효과`, `지원 제어원 제한`은 `보조 출력 사용 제한`으로 바꿔 결과와 입력 용어를 맞췄다.
- 모드별 3단계 설명을 실제 계산식에 맞춰 분리했다. “여섯 측정 공동 계산”이 MIMO/합산 SISO에 잘못 표시되지 않는다.
- MIMO 결과 그래프는 저역만 잘라 보여주지 않고 20 Hz–20 kHz 전체에서 L/R 가중 평균과 각 세 위치의 예상 상·하한을 함께 표시한다. MIMO 상한 위는 기존 SISO FIR로 전환되는 구간이라 함께 확인해야 한다.
- 결과에는 실제 조건수 중앙/P95/최대, 자동 안정화 bin, actuator confidence P10, 위치별 MAE를 표시한다.
- POST, 탭 이동, 계산 완료 reload는 현재 탭·스크롤 위치를 보존하며 내부 진단 FAIL 때문에 화면 최상단으로 focus가 이동하지 않는다.

## 근거 자료와 적용 범위

- Brännmark, Bahne, Ahlén, *Compensation of Loudspeaker–Room Responses in a Robust MIMO Control Framework*, IEEE TASLP 21(6), 2013, DOI `10.1109/TASL.2013.2245650`: 다중 스피커 공동 제어, 공간/주파수 제어오차와 control action penalty, 강건성·pre-ringing 제약의 근거.
- Lin, Chen, Lai, Bai, *Multichannel room response equalization with a broadened control region using a linearly constrained approach and sensor interpolation*, JASA 153, 2023, DOI `10.1121/10.0017721`: 제한된 실제 control point 밖의 영역을 넓히려면 센서 보간과 추가 가정이 필요하다는 근거. AudioDSP의 3점만으로 방 전체를 보장하지 않는다.
- Koyama et al., *Weighted Pressure Matching Based on Kernel Interpolation for Sound Field Reproduction*, arXiv `2210.14711`, 2022/2023: control point 사이를 고려하는 공간 가중 pressure matching의 근거. 현재는 마이크 좌표·방 기하가 없어 kernel 보간은 보류한다.
- Koyama et al., *Weighted Pressure and Mode Matching for Sound Field Reproduction*, arXiv `2303.13027`, 2023: weighted pressure matching과 mode matching의 비교 근거.

AudioDSP는 공개 수학 원리를 독립 구현한 것이며 Dirac ART 복제나 동등 성능 주장이 아니다. 능동 저역 제어가 구조 전달 층간소음, 비선형 왜곡, late reverberation 또는 방 안 모든 위치의 동일 저음을 보장하지 않는다.
