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

## 연구 근거와 채택 범위

- Dirac의 공개 ART 설명은 여러 스피커를 공동 제어하는 MIMO, 최소 두 스피커, 주로 20–150 Hz의 능동 저역 제어를 설명한다. Stereo L/R도 서로 지원할 수 있어 sub가 필수는 아니지만, 실용적인 지원 스피커는 충분한 저역 재생 능력이 필요하다. 3개 이상의 유효 측정 위치를 요구하고 제어원·측정점이 늘면 공간 제어가 개선될 수 있다고 설명한다.  
  - <https://www.dirac.com/resources/art-technology>
  - <https://helpdesk.dirac.com/en/dirac-art/Dirac-Live-Processor-ART-Stereo>
  - <https://helpdesk.dirac.com/en/dirac-art/Setup-Guide-c3cb>
  - <https://www.dirac.com/wp-content/uploads/2025/05/ART_Use-case-definition-and-setup-guidelines.pdf>
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

1. 세 위치의 선택 타깃에 대한 복소 pressure 오차
2. 제어 에너지 Tikhonov regularization
3. 기존 안전한 SISO L/R FIR에서 과도하게 벗어나지 않는 prior
4. 각 제어원의 측정된 자연 저역 한계 아래 사용을 억제하는 주파수별 penalty
5. 보조 제어원 및 우퍼의 사용량 penalty

추가 안전 처리:

- MIMO 범위는 20–80/120/150 Hz 중 선택하며 끝에서 30 Hz raised-cosine으로 기존 SISO FIR에 전이한다.
- 영위상 역필터를 요구하지 않고 기존 SISO 응답의 가중 도착 phase를 목표 phase로 유지한다.
- 모든 경로에 하나의 공통 인과 지연을 적용하고 32768탭으로 절단·후단 taper한다.
- 주파수별 각 물리 출력의 `|L path| + |R path|`를 0.999 이하로 투영한다. 변환·절단 후 다시 최악 상관입력 row sum을 검사하고 필요한 최소 global scale만 적용한다.
- NaN/Inf, 정확한 tap/rate/format, manifest SHA-256, 인과성, headroom, 예측 타깃 오차와 공간 편차 비퇴행을 통과해야 Preview/Apply가 열린다.
- 출력은 `MIMO_Front_Left_LR_32768.wav` 등 네 stereo float32 WAV다. 각 WAV의 채널 0/1은 입력 L/R에서 해당 물리 출력으로 가는 두 전달 경로다.

## 측정 절차

MIMO 모드는 각 위치에서 모든 물리 제어원을 하나씩 독립 재생한다. 현재 구현은 중앙과 그 주변의 작은 청취영역 세 위치를 사용한다. UMIK-1은 실제 룸 측정 시 90° calibration과 천장 방향을 사용한다.

1. U7/UMIK 연결과 90° calibration을 확인한다.
2. 5초 무음과 5초 백색소음 검사를 사용자가 직접 시작한다. SNR 15 dB 이상을 권장하고, 6 dB 미만 또는 clipping은 적용을 차단한다.
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
| 배경소음·SNR·clipping | measurement gate | 무음/백색소음과 각 sweep 품질 검사 | 기기 볼륨·환경 소음을 사용자가 조정 |
| 주파수 응답·타깃 | FIR 보정 가능 | 공간 가중, 가변 smoothing, boost/cut 제한 | 시간변화와 미측정 위치는 보장하지 않음 |
| 자연 저역 확장·headroom | 제한적 | roll-off 아래 boost 억제 | 드라이버 변위, 앰프 출력, 왜곡은 늘릴 수 없음 |
| 좌석 간 저역 편차 | MIMO 개선 가능 | 저역 복소 pressure matching | 제어원 독립성과 측정영역에 의존 |
| 도착시간·극성·저역 phase | 제한적 | 공통 지연, 저역 excess phase/공동 phase | 위치마다 다른 고역 phase는 역보정하지 않음 |
| 룸 모드·저역 decay | 제한적 | peak cut, MIMO가 초기 modal energy를 줄일 수 있음 | 물리 RT60 전체 제거 및 모든 결과의 decay 개선 보장 불가 |
| SBIR·초기반사·명료도 | 진단/배치 | C50/C80/D50·반사창 진단, 깊은 null boost 금지 | 벽 거리, 스피커/좌석 이동, 1차 반사 흡음 |
| 중·고역 late reverb | 물리 처리 | EDT/T20 보고만 함 | 흡음·확산·가구·배치 필요 |
| L/R 감도·음색 | FIR 보정 가능 | 독립 L/R magnitude | 지향성 차이와 power response는 별도 문제 |
| 메인–우퍼 합산 | 제한적/MIMO | 레벨·지연·극성·저역 phase | 아날로그 crossover와 비선형은 변경 불가 |
| 고조파 왜곡·압축·잡음 | 미측정 | 현재 없음 | 다중 레벨 Farina harmonic 분리 측정 필요; 선형 FIR로 보정 불가 |
| 지향성·오프축/power response | 미측정 | 현재 없음 | 회전/근접 다각도 측정 필요 |
| IACC·양이간 공간감 | 미측정 | 현재 없음 | 단일 UMIK-1로 직접 측정 불가; 2마이크/더미헤드 필요 |
| 절대 SPL·청력·층간소음 | 미인증 | volume cap/저역 감쇄는 위험 저감 | UMIK sensitivity와 전체 체인 검교정, 수음세대 측정 없이는 무소음 보장 불가 |
| latency·clock drift·XRUN | runtime 검증 | 상태/부하 검사 | 실제 USB·Pi·chunksize 조합으로 장시간 확인 필요 |

`fir_correctable`은 “완벽히 제거”가 아니라 측정한 영역과 선형·시간불변 조건에서 안전하게 개선 가능한 항목이다. 깊은 null, late reverberation, 비선형 왜곡, 구조전달 층간소음은 성공 항목으로 표시하지 않는다.

## Pi별 지원

- Pi 2: 기존 SISO 2/4 convolution만 지원한다. MIMO 모드는 UI에서 비활성이고 API/CLI도 활성화를 거부한다. 측정·오프라인 계산 코드는 공통이지만 실시간 8경로를 적용하지 않는다.
- Pi 4/Pi 5: MIMO 8 convolution을 허용한다. 활성화 시 effective chunksize는 최소 1024다. 적용 후 실제 CPU load, XRUN, USB 안정성을 확인해야 하며 Pi 5도 실기 장시간 검증 전에는 무조건적인 성능 보장을 하지 않는다.

## 의도적으로 하지 않는 것

- 마이크를 상시 연결한 폐루프 feedback ANC
- 미측정 방 전체의 sound field 보장
- 상용 ART와 동일하다는 표현
- FIR로 late reverberation, 구조진동, 비선형 왜곡을 “제거”했다는 판정
- 한 물리 우퍼의 두 입력을 두 독립 제어원으로 계산
- 사용자 클릭 없이 측정음을 자동 재생
