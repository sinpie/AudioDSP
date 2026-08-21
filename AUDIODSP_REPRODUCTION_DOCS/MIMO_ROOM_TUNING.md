# AudioDSP 룸 튜닝·MIMO 설계와 한계

## 결론

AudioDSP의 MIMO는 Dirac ART의 복제품이 아니다. 공개 연구의 robust multichannel weighted pressure matching을 독립 구현한 48 kHz, 2입력×4출력, 32768탭 feed-forward FIR bank다. 측정한 세 위치에서 저역의 타깃 오차와 위치 편차를 함께 줄이고, 각 물리 출력의 최악 상관입력 headroom과 짧은 공통 인과 지연을 제한한다.

서브우퍼 두 대는 필수가 아니다.

| UI 모드 | 독립 물리 제어원 | 출력 해석 | 기대 효과 |
|---|---:|---|---|
| MIMO Stereo | 2 | ES10 L/R | 두 스피커가 서로 지원하지만 각 스피커의 저역 한계 아래 효과는 작다. |
| MIMO 2.1 | 3 | ES10 L/R + T5S 한 대 | 현재 권장 구성. T5S의 stereo 입력 두 개는 한 물리 음원이며 Rear L/R에 같은 sub 신호를 0.5씩 보낸다. |
| MIMO 2.2 | 4 | ES10 L/R + 서로 다른 위치·배선의 우퍼 두 대 | 독립 공간 응답이 확보되면 좌석 간 모드 제어 자유도가 가장 크다. |

같은 T5S의 좌·우 입력은 두 우퍼가 아니다. 알고리즘과 보고서는 이를 `sub_pair` 한 제어원으로 취급한다. 독립 우퍼 모드는 실제로 서로 다른 위치에 놓고 U7 Rear L/R에 각각 연결한 경우에만 선택한다.

Sub가 있는 topology는 기본 ON/100 Hz LR4 crossover의 complex minimum-phase branch spectrum을 전달행렬 자체에 포함한다. 따라서 최적화가 Front HPF/Woofer LPF를 우회하지 않으며, 결과는 8개 convolution path WAV에 이미 내장된다. 주파수별 noise confidence를 위치 가중치에 반영하고, `correction_low_hz` 아래에서는 최적화를 하지 않되 crossover routing은 유지한다. Woofer trim은 regularization 힌트가 아니라 Rear physical-output row-sum의 실제 transfer 상한으로 적용한다.

보고서의 `before/after_modal_tail_db`는 512점 평활 전달함수에서 만든 impulse-tail proxy다. 실제 RT60이나 late reverberation 예측이 아니며 1.5 dB 넘게 악화될 때 적용을 차단하는 보수적 ringing gate로만 쓴다. 실제 잔향 판정은 저장된 deconvolution impulse의 octave EDT/T20과 적용 후 측정으로 한다.

## 연구 근거와 채택 범위

- Dirac의 공개 ART 설명은 여러 스피커를 공동 제어하는 MIMO, 최소 두 스피커, 주로 20–150 Hz의 능동 저역 제어를 설명한다. Stereo L/R도 서로 지원할 수 있어 sub가 필수는 아니지만, 실용적인 지원 스피커는 충분한 저역 재생 능력이 필요하다. 3개 이상의 유효 측정 위치를 요구하고 제어원·측정점이 늘면 공간 제어가 개선될 수 있다고 설명한다.
  - <https://www.dirac.com/resources/art-technology>
  - <https://www.dirac.com/products/art>
  - <https://helpdesk.dirac.com/en/dirac-art/Dirac-Live-Processor-ART-Stereo>
  - <https://helpdesk.dirac.com/en/dirac-art/Setup-Guide-c3cb>
  - <https://helpdesk.dirac.com/en/dirac-art/How-to-ART-Channel-Group-and-Support-Settings-8382>
  - <https://www.dirac.com/wp-content/uploads/2024/06/Dirac-MIMO-framework-for-active-room-treatment-and-Unison-.pdf>
- 공개 MIMO loudspeaker-room 보정 연구는 모든 스피커·위치의 전달함수를 공동 최적화하고 regularization, pre-ringing/robustness 제약으로 재생 오차와 공간 편차를 줄이는 틀을 제시한다. AudioDSP는 이 계열의 제약 최적화를 사용한다.
  - DOI 10.1109/TASL.2013.2245650, *Compensation of Loudspeaker–Room Responses in a Robust MIMO Control Framework*
- 최근 weighted pressure matching 연구는 공간 가중과 주파수별 안정화가 다중점 sound-field control의 강건성에 중요함을 다룬다.
  - <https://arxiv.org/abs/2210.14711>
  - <https://arxiv.org/abs/2303.13027>
- 2025년 orthogonal loudspeaker matching 연구는 다중 스피커의 상호 독립성을 이용한 공간 제어 방향을 검토한다.
  - <https://www.sciencedirect.com/science/article/pii/S0003682X24006583>
- 2026년 weighted acoustic model matching은 sparse transfer-function 측정에 kernel interpolation을 더해 연속 영역 목표를 구성한다. 현재 AudioDSP에는 측정 좌표·방 기하와 충분한 센서 수가 없으므로 가짜 보간점을 만들지 않고 보류했다.
  - DOI 10.1016/j.jsv.2025.119489: <https://www.sciencedirect.com/science/article/pii/S0022460X25005620>
- 2023년 UMIF-LCMV 연구도 제한된 실제 센서와 plane-wave interpolation으로 제어 영역을 넓힌다. 세 점 UMIK 절차에 그대로 적용하면 가정이 충족되지 않으므로 향후 측정점·좌표 기능과 함께 검토한다.
  - DOI 10.1121/10.0017721: <https://pubmed.ncbi.nlm.nih.gov/37092918/>
- 2026년 common excess-phase zero 식별 연구는 다중 위치 mixed-phase 보정에서 ringing 정량화의 중요성을 보여준다. 현재 구현은 더 보수적으로 저역만, 기존 도착 phase 유지, 공통 지연과 pre-energy 검사를 사용한다. sub-band zero 식별은 아직 구현하지 않았으며 보고서에 phase 보정을 `limited_*`로 표시한다.
  - DOI 10.1016/j.apacoust.2025.111153: <https://www.sciencedirect.com/science/article/abs/pii/S0003682X25006255>
- 2026년 cue-constrained Tikhonov은 두 청취자의 binaural cue를 목적함수로 분리한다. 단일 omni UMIK-1에는 귀별 전달함수가 없어 적용 조건이 없으므로, IACC/양이간 항목을 `not_measured`로 유지한다.
  - <https://link.springer.com/article/10.1186/s13636-026-00461-6>
- 다중 sub 배치는 좌석 간 저주파 편차를 줄여 후단 EQ가 효과적으로 동작하게 할 수 있다.
  - Welti & Devantier, AES Journal: <https://secure.aes.org/forum/pubs/journal/?elib=13680>
- CamillaDSP는 mixer로 중간 채널 수를 늘리고 임의의 FIR pipeline을 구성할 수 있다. AudioDSP는 2→8 확장, 8 convolution, 8→4 합산을 사용한다.
  - <https://github.com/HEnquist/camilladsp>

연구·상용 제품의 아이디어를 참고하되 비공개 구현이나 특허 청구항을 복제하지 않는다. “ART-like”는 여러 출력의 측정 응답을 공동 최적화한다는 기능 범주를 뜻하며, ART와 동일 성능 또는 인증을 뜻하지 않는다.

## 실제 최적화

각 청취 위치 `p`, 물리 제어원 `a`, 입력 채널 `c`, 주파수 `f`에 대해 복소 전달함수 `H[p,a,f]`를 독립 sweep으로 측정한다. 각 입력 채널의 출력 FIR 벡터 `g[a,c,f]`는 다음 항을 함께 최소화한다.

```text
h_p(f) = [H_p,1(f), ..., H_p,A(f)]
q_p(f) = normalize(w_p(f) min_a c_p,a(f))

J(g) = Σ_p q_p |h_p g - d|²
     + gᴴΛ(f)g
     + ρ||g-g₀||²
     + κ||g-g_prev||²

(HᴴQH + Λ + (ρ+κ)I)g
  = HᴴQd + ρg₀ + κg_prev
```

`w_p`는 SISO와 같은 위치 정책이다. `equal`은 세 위치 동일 가중이고 `center`도 룸 모드가 지배하는 200 Hz 이하에서는 동일 가중이다. 중앙 0.60 가중은 2 kHz 이상에만 도달하므로 현재 최대 150 Hz MIMO 대역에는 들어오지 않는다. `c_p,a`는 위치·제어원별 주파수 신뢰도이며 가장 낮은 제어원 신뢰도를 그 행의 상한으로 사용한다. v22부터 solver 목표 phase, 예측 그래프, 공간 표준편차, target MAE가 모두 이 `q_p(f)`를 공유한다.

`d`의 크기는 선택 target과 기존 SISO 저역 anchor로 정하고, phase는 가중 SISO 도착 phase를 유지한다. 이는 모든 위치에 영위상 응답을 강요하는 비인과 역필터가 아니다. `Λ`에는 Tikhonov 제어 에너지, 제어원 자연 재생대역 아래 penalty, 보조 제어원 사용 penalty가 들어간다. `g₀`는 검증된 SISO/crossover baseline, `g_prev`는 바로 앞 주파수 bin의 해다.

1. 세 위치의 선택 타깃에 대한 복소 pressure 오차
2. 제어 에너지 Tikhonov regularization
3. 기존 안전한 SISO L/R FIR에서 과도하게 벗어나지 않는 prior
4. 각 제어원의 측정된 자연 저역 한계 아래 사용을 억제하는 주파수별 penalty
5. 보조 제어원 및 우퍼의 사용량 penalty

추가 안전 처리:

- MIMO 전에 L/R base FIR은 SISO와 같은 L/R 500~2,000 Hz 공통 측정·타깃 기준으로 설계하고 `normalize_fir_bank` common gain 한 번만 적용한다. MIMO target offset도 좌우에 하나만 사용하며, 최종 2×4 matrix는 설계된 출력 간 관계를 보존하는 global scale만 허용한다. 정규화하지 않은 중간값이나 Front-only 응답을 실제 Front+sub baseline과 비교해 false FAIL을 만들지 않는다.
- MIMO 범위는 20–80/120/150 Hz 중 선택하며 끝에서 30 Hz raised-cosine으로 기존 SISO FIR에 전이한다.
- 영위상 역필터를 요구하지 않고 기존 SISO 응답의 가중 도착 phase를 목표 phase로 유지한다.
- 모든 경로에 하나의 공통 인과 지연을 적용하고 32768탭으로 절단·후단 taper한다.
- 주파수별 각 물리 출력의 `|L path| + |R path|`를 0.999 이하로 투영한다. 변환·절단 후 다시 최악 상관입력 row sum을 검사하고 필요한 최소 global scale만 적용한다.
- target MAE는 40 Hz부터 MIMO 상한 또는 130 Hz까지의 한 공통 reference-band level만 맞춘 뒤 응답 형상을 평가한다. 위치별·주파수별 normalize는 금지한다. 상관입력 headroom 때문에 생기는 broadband 감쇄는 `headroom.global_scale_db`와 raw graph로 별도 표시하므로, 단순 볼륨 저하를 음색 실패로 오판하지 않으면서 실제 감쇄를 숨기지도 않는다.
- 대표 before/after 그래프는 `10 log10(Σq_p |H_p g|²)`의 가중 mean-square 응답이다. 좌석 편차는 같은 `q_p`의 weighted dB 표준편차이고, target MAE도 같은 위치 가중을 쓴다. 물리 출력 headroom과 최악 위치 안전 상한은 평균하지 않는다.
- 두 programme speaker뿐인 `MIMO Stereo`는 전용 support 제어원이 없어 큰 cross-feed가 stereo target을 손상시키기 쉽다. 선택 강도의 15%만 기존 SISO에서 벗어나도록 제한한다. `MIMO 2.2`는 자유도가 큰 대신 narrow solution의 tail 위험을 줄이기 위해 선택 강도의 85%를 사용한다.
- NaN/Inf, 정확한 tap/rate/format, manifest SHA-256, 인과성, headroom, 예측 타깃 오차와 공간 편차 비퇴행을 통과해야 Preview/Apply가 열린다.
- 출력은 `MIMO_Front_Left_LR_32768.wav` 등 네 stereo float32 WAV다. 각 WAV의 채널 0/1은 입력 L/R에서 해당 물리 출력으로 가는 두 전달 경로다.

## 측정 절차

MIMO 모드는 각 위치에서 모든 물리 제어원을 하나씩 독립 재생한다. 현재 구현은 중앙과 그 주변의 작은 청취영역 세 위치를 사용한다. UMIK-1은 실제 룸 측정 시 90° calibration과 천장 방향을 사용한다.

1. U7/UMIK 연결과 90° calibration을 확인한다.
2. 현재 구성의 모든 제어원을 본 측정과 같은 2초 ESS로 빠르게 검사한다. SNR 15 dB 이상을 권장하고, 6 dB 미만 또는 clipping은 측정을 차단한다.
3. 위치 1–3에서 각 제어원을 독립 sweep한다. 측정 재생 동안 기존 DSP는 direct bypass이고 U7 input monitor는 mute다.
4. 타깃, T5S 저역 억제, 보정 범위, boost/cut, MIMO 상한·강도·지원 penalty를 고른다.
5. 계산 결과의 예측 그래프, headroom, 제어원 coherence, 전체 분류표와 보고서를 검토한다.
6. Speaker Preview로 비교한 뒤 정식 Apply한다. 정식 적용은 기존 bank와 설정을 먼저 백업한다.
7. 적용에 쓰지 않은 별도 위치를 포함해 전/후 재측정하고 CPU load/XRUN을 확인한다.

세 위치는 최소 운용 단위이지 방 전체 보증이 아니다. 넓은 소파나 여러 좌석을 보정하려면 향후 측정점 확장이 필요하며, 측정점만 늘리고 공간 가중을 설계하지 않으면 미측정 영역이 나빠질 수도 있다.

## 룸 튜닝 요소별 경계

모든 생성 결과는 `Room_Tuning_Report.json`과 사람이 읽는 `Room_Tuning_Report.md`를 남긴다. Web UI에서도 같은 표를 보여주고 ZIP/MD/JSON으로 내려받는다.

| 요소 | 분류 | AudioDSP 처리 | 필터 밖의 한계/조치 |
|---|---|---|---|
| 배경소음·SNR·clipping | measurement gate | 모든 제어원의 빠른 ESS와 각 본 스윕 품질 검사 | 기기 볼륨·환경 소음을 사용자가 조정 |
| 주파수 응답·타깃 | FIR 보정 가능 | 공간 가중, 가변 smoothing, boost/cut 제한 | 시간변화와 미측정 위치는 보장하지 않음 |
| 자연 저역 확장·headroom | 제한적 | roll-off 아래 boost 억제 | 드라이버 변위, 앰프 출력, 왜곡은 늘릴 수 없음 |
| 좌석 간 저역 편차 | MIMO 개선 가능 | 저역 복소 pressure matching | 제어원 독립성과 측정영역에 의존 |
| 도착시간·극성·저역 phase | 제한적 | 공통 지연, 저역 excess phase/공동 phase | 위치마다 다른 고역 phase는 역보정하지 않음 |
| 룸 모드·저역 decay | 제한적 | peak cut, MIMO가 초기 modal energy를 줄일 수 있음 | 물리 RT60 전체 제거 및 모든 결과의 decay 개선 보장 불가 |
| SBIR·초기반사·명료도 | 진단/배치 | C50/C80/D50·반사창 진단, 깊은 null boost 금지 | 벽 거리, 스피커/좌석 이동, 1차 반사 흡음 |
| 중·고역 late reverb | 물리 처리 | EDT/T20 보고만 함 | 흡음·확산·가구·배치 필요 |
| L/R 감도·음색 | FIR 보정 가능 | 독립 L/R magnitude | 지향성 차이와 power response는 별도 문제 |
| 메인–우퍼 합산 | 제한적/MIMO | FIR 내장 LR4 HPF/LPF, 레벨·지연·극성·저역 phase, 세 위치 복소합 | 적용 후 검증 sweep 전에는 확정하지 않으며 아날로그 crossover와 비선형은 변경 불가 |
| 고조파 왜곡·압축·잡음 | 미측정 | 현재 없음 | 다중 레벨 Farina harmonic 분리 측정 필요; 선형 FIR로 보정 불가 |
| 지향성·오프축/power response | 미측정 | 현재 없음 | 회전/근접 다각도 측정 필요 |
| IACC·양이간 공간감 | 미측정 | 현재 없음 | 단일 UMIK-1로 직접 측정 불가; 2마이크/더미헤드 필요 |
| 절대 SPL·청력·층간소음 | 미인증 | volume cap/저역 감쇄는 위험 저감 | UMIK sensitivity와 전체 체인 검교정, 수음세대 측정 없이는 무소음 보장 불가 |
| latency·clock drift·XRUN | runtime 검증 | 상태/부하 검사 | 실제 USB·Pi·chunksize 조합으로 장시간 확인 필요 |

`fir_correctable`은 “완벽히 제거”가 아니라 측정한 영역과 선형·시간불변 조건에서 안전하게 개선 가능한 항목이다. 깊은 null, late reverberation, 비선형 왜곡, 구조전달 층간소음은 성공 항목으로 표시하지 않는다.

## Pi별 지원

- Pi 2: 기존 SISO 2/4 convolution만 지원한다. MIMO 모드는 UI에서 비활성이고 API/CLI도 활성화를 거부한다. 측정·오프라인 계산 코드는 공통이지만 실시간 8경로를 적용하지 않는다.
- Pi 4/Pi 5: MIMO 8 convolution을 허용한다. 활성화 시 effective chunksize는 최소 1024다. 적용 후 실제 CPU load, XRUN, USB 안정성을 확인해야 하며 Pi 5도 실기 장시간 검증 전에는 무조건적인 성능 보장을 하지 않는다.

### Pi 5 2 GB와 5.1 확장 worst case

`tools/estimate_dsp_memory.py`와 `test_resource_budget.py`의 계획 모델은 48 kHz, 32768 taps, chunksize 1024를 기준으로 한다. CamillaDSP 수치는 1024-sample partition 32개, float32 complex spectrum/history와 4배 구현 여유를 포함한다. 생성 수치는 64-bit CPython의 complex/float object, FFTW·긴 녹음 한 채널 처리 256 MiB와 2배 live-bank 여유를 포함한다.

| 구성 | FIR 경로 | 원시 계수 | 실시간 DSP 계획 | 필터 생성 계획 | partition complex MAC/s |
|---|---:|---:|---:|---:|---:|
| 현재 2입력×4출력 dense | 8 | 1.00 MiB | 46 MiB | 309 MiB | 12.3 M |
| 5.1 독립 채널별 FIR | 6 | 0.75 MiB | 40 MiB | 296 MiB | 9.2 M |
| 5.1 입력×5 main+dual-sub 완전 dense | 42 | 5.25 MiB | 135 MiB | 530 MiB | 64.6 M |

42경로와 같은 Python object를 실제 할당한 64-bit probe peak는 138.64 MiB였다. 530 MiB는 이 측정치에 response/FFTW와 큰 안전 여유를 더한 상한이다. 따라서 Raspberry Pi OS Lite, AudioDSP Web과 CamillaDSP만 운용하는 Pi 5 2 GB는 실시간 FIR과 오프라인 생성 모두 메모리 안에 들어온다. 필터 생성기는 큰 path spectrum을 causalization 전에 해제하고 전체 bank scaling을 제자리에서 수행해 old/new bank 중복을 피한다. 생성 중 새 backup 복원·대형 upload 등 다른 무거운 작업은 동시에 시작하지 않는다.

다만 42경로 완전 dense 5.1은 메모리보다 CPU가 약 5.25배 커지는 것이 문제다. Pi 5 실기에서 1024 chunksize, 실제 U7/향후 6+출력 interface로 10분 이상 XRUN·온도·CPU를 통과하기 전에는 지원으로 표시하지 않는다. 실용적인 5.1은 우선 6개 diagonal FIR을 쓰고, MIMO는 150 Hz 이하의 독립 subwoofer/저역 actuator group에만 제한한다. swap은 실시간 오디오 여유로 계산하지 않는다.

4 GB는 다른 서버·database·desktop을 함께 운영하거나 매우 큰 session archive를 동시에 다룰 때의 선택 여유다. RAM 증설 자체는 음질이나 latency를 개선하지 않는다. 현재 2×4 MIMO의 실제 병목도 CPU와 USB/XRUN 안정성이므로 새 Pi 5 2 GB에서 장시간 수락 시험으로 최종 확정한다.

## 2026-08-21 무음 알고리즘 회귀 결과

- v22 수학 감사에서 `center`가 150 Hz 이하 MIMO에도 중앙 위치를 0.60으로 고정하던 오류를 수정했다. 이제 200 Hz 이하의 기하 가중은 1/3·1/3·1/3이고, 위치별 측정 신뢰도만 추가된다. solver·그래프·검증이 동일 가중을 사용하도록 회귀시험을 추가했다.
- `Flat / 추가 억제 없음 / Woofer trim 0 dB / 최대 상대 보상 10 dB` 기준 합성 session은 MIMO Stereo, MIMO 2.1, MIMO 2.2 모두 finite/headroom/causality/타깃·공간 비악화/modal-tail 비악화 모델 검증을 PASS했다.
- MIMO 전용 UI 값 19개를 실제 32768탭×8경로로 생성했다. 구조·형식·headroom 검사는 19/19 PASS했다.
- 2026-08-21 회귀에서 기존 다섯 `fail_model`은 비교 baseline이 Front-only인 반면 후보는 LR4 Front+sub였던 검증 오류로 확인했다. baseline도 실제 배포되는 crossover routing으로 수정한 뒤 19/19가 구조와 모델 비악화를 PASS했다. 실제 방에서 비기준 조합이 실패할 수 있다는 정책은 유지하며 Web은 `4 · FIR 계산`의 실제 항목명으로 조정 순서를 안내한다.
- 합성 MIMO 기준 모델 PASS는 `AUDIODSP_PHASE_CLOCK_SHARED=1`인 공통-clock fixture의 수치 검증이다. 현재 U7+UMIK-1 독립 USB clock 실측에서는 복소 전달행렬의 절대 위상을 보장할 수 없어 production 생성과 활성화를 차단한다. 향후 loopback/reference channel 등 공통 timing reference가 추가된 뒤에도 선택적인 저레벨 acoustic audit와 Pi5 10분 CPU/XRUN 검증을 별도 수행해야 실제 하드웨어 성능을 수락할 수 있다.

## 의도적으로 하지 않는 것

- 마이크를 상시 연결한 폐루프 feedback ANC
- 미측정 방 전체의 sound field 보장
- 상용 ART와 동일하다는 표현
- FIR로 late reverberation, 구조진동, 비선형 왜곡을 “제거”했다는 판정
- 한 물리 우퍼의 두 입력을 두 독립 제어원으로 계산
- 사용자 클릭 없이 측정음을 자동 재생
