# AudioDSP UI/UX 명세

## 정보 구조

하단 또는 상단 공통 navigation은 세 화면만 제공한다.

1. `현황`: 다음 할 일, 전역 출력 볼륨, 실제 U7/DSP 상태, 현재 FIR 그래프
2. `측정 · 보정`: calibration부터 정식 적용까지 여섯 단계
3. `프로필 · 설정`: 전체 백업/복원, engine 설정, Speaker/Headphones 프로필 카드

모바일에서는 navigation이 하단 고정이고, PC에서는 콘텐츠 상단에 폭을 나눠 배치한다. 화면 사이를 이동해도 server-side 상태는 유지된다.

## 현황 화면

- 첫 카드는 측정 session 상태에 따라 하나의 `지금 할 일`을 제안한다.
- 그 다음 `오디오 신호 흐름` console은 U7 Line input → CamillaDSP → Front/Rear routing → U7 물리 selector → 실제 스피커 체인을 SVG 아이콘 박스와 연결선으로 보여준다. PC에서는 가로, 980 px 이하에서는 같은 순서를 세로로 배치한다.
- 신호 console의 U7 경로명과 설정 카드 강조는 `/api/status`를 약 1.5초 간격으로 polling해 실제 상단 버튼 전환을 반영한다. 웹은 하드웨어 selector를 쓰지 않는다.
- 출력 볼륨 카드는 큰 dB 숫자, -60~0 slider, ±1 dB, -40/-30/-20/-10 preset, 저장·적용 버튼을 가진다.
- Slider 이동 중 예상값을 즉시 표시하고 release 시 PUT한다. 명시적 버튼은 키보드/비-JS fallback이다.
- U7 실제값과 재부팅 저장값이 다르면 `물리 노브 변경 감지`를 표시한다.
- 볼륨은 Front/Rear 전체 하드웨어 출력이라는 설명과 CamillaDSP 무재시작 설명을 항상 보인다.
- 현재 설정 표는 U7 실제 출력, 요청/유효 profile, fallback, bypass, A/B, Rear mode, convolution 수, 서비스, 장치, format/chunksize를 보여준다.
- FIR 그래프는 L/R 또는 L/R+Woofer toggle이며 브라우저가 WAV를 받아 SVG를 계산한다. 이 그래프는 측정 응답에 곱하는 **보정 전달함수**이며 목표 청취 음압이 아님을 제목과 설명에서 명시한다.

## 측정 화면

상단 workflow는 PC 한 줄·모바일 3×2의 6단계 탭 `연결·Cal → 레벨 확인 → 위치 측정 → FIR 계산 → 검토·A/B → 정식 적용`이다. 선택한 단계의 패널 하나만 탭 바로 아래에 표시하며 긴 문서 anchor 이동을 사용하지 않는다.

- 모든 탭은 이전/현재/미래 상태와 관계없이 열 수 있다. 데이터가 아직 없는 단계는 선행 조건 안내를 표시하고, 실제 실행 버튼만 조건에 따라 비활성화한다.
- 클릭·방향키·Home/End로 탭을 바꿀 수 있고 `role=tablist/tab/tabpanel`, `aria-selected`, `aria-current=step`을 함께 유지한다.
- step click은 navigation뿐이며 데이터 mutation을 하지 않는다.
- 편집한 설정은 `변경 적용` 전까지 기존 결과에 영향을 주지 않는다.
- 재검사/재측정/재계산 버튼은 초기화 범위를 confirm 문구로 정확히 말한다.
- 진행 중에는 progress, percent, 단계명, ETA를 0.5~1.5초 간격으로 polling한다. Polling은 DOM 수치만 바꾸며 페이지 전체를 새로고침하지 않는다. 작업 완료·위치 증가·물리 출력 변경처럼 화면 구조가 실제로 달라질 때만 한 번 reload한다.
- 레벨 결과는 PASS/FAIL, 출력 조합별 SNR, 평가 대역, peak와 올릴 수 있는 안전 dBFS를 카드로 보여준다. 본 측정 결과는 원신호 SNR, 긴 ESS의 coherent integration 이득, 최종 유효 SNR을 한 문장 안에서 구분한다.
- 활성 session의 ID, 생성 시각, 상태, 완료 위치, 이어갈 단계, FIR 결과 유무와 주석 편집은 1–6 단계 탭 바로 위에 항상 표시한다. 주석 저장은 별도 metadata만 바꾸며 어떤 측정·FIR 단계도 초기화하지 않는다.
- session은 생성 즉시 자동 저장한다. 1단계의 저장 session 목록은 최신순으로 ID·시각·측정 구성·완료 단계·위치·FIR 유무·주석을 함께 보여주고, `이어하기`는 파일 무결성을 확인한 뒤 저장된 1–6 완료 지점 전체를 복원한다. `삭제`는 복구 불가 범위와 현재 정식 FIR 불변을 confirm에 표시하고 정확한 session 하나만 지운다. 작업 중 session 전환·삭제는 차단한다.
- 저장 session 목록은 ID·날짜·주석 client-side 검색을 제공한다. 활성 주석을 편집하면 즉시 `저장되지 않은 주석` 상태를 표시하고, 저장 전에 페이지를 떠날 때만 확인한다.
- 레벨 검사를 누르는 순간 현재 U7 물리 출력을 session의 `measurement_profile`로 고정한다. 화면은 현재/고정 경로의 일치 여부를 별도 lock 카드로 보여주고, 불일치하면 위치 측정과 A/B를 비활성화한다.
- 생성 결과의 Preview/Apply 버튼은 고정된 한 출력 체인만 제공한다. 경로 정보가 없는 schema-1 이전 session에만 수동 확인 경고와 두 버튼을 보이는 호환 모드를 쓴다.
- 2단계에는 본 측정과 빠른 검사에 공통인 스윕 출력 slider 하나만 둔다. 현재 우퍼 실효값을 계산하고 높은 조합을 색+문구로 경고하며 실제 위치 스윕 전 confirm에 수치를 다시 표시한다. 백색소음 제어는 완전히 숨긴다.
- 새 세션의 권장 측정 구성은 `정밀 분리+합산 · L/R/우퍼/L+우퍼/R+우퍼 · 위치당 5회`다. L/R/우퍼만 설계하고 합산 두 응답은 절대 레벨 closure에만 쓰며 FIR 생성 뒤 다시 측정하라고 요구하지 않는다. 독립-clock에서는 위상 정밀도를 제한으로 표시하고 보수적 합산 상한을 사용한다.
- Target을 바꾸면 1 kHz 기준 곡선과 bass/treble preference를 즉시 SVG에 반영한다.
- L/R/Woofer와 sub MIMO에서는 디지털 crossover를 주요 옵션으로 표시하고 기본 ON/100 Hz로 둔다. 설명은 Front LR4 HPF, Woofer LR4 LPF, 32768탭 WAV 내장, 추가 block latency 0을 함께 말한다. 합산 L/R 모드에서는 숨은 OFF 값과 독립 branch가 없다는 이유를 표시한다.
- 결과 그래프는 각 채널의 측정 전 ±공간 편차, 적용 후 예상, target을 구분한다.
- `최대 상대 보상`은 0/3/6/9/10 dB로 표시하고 기본 10 dB다. 도움말은 “신뢰되는 최고 보상을 0 dB로 두고 L/R/Woofer 전체를 같은 값만큼 낮춤”과 “좁은 deep은 최대 3 dB”를 함께 설명한다. 결과 카드에는 실제 `상대 보상의 음량 비용`과 `15–20 kHz 잔여 오차`를 표시하고, 상한을 모두 써도 남는 slope는 amber 경고와 실제 메뉴명 기반 가이드로 설명한다. `최대 부스트`나 채널별 0 dB처럼 실제 동작과 다른 용어를 사용하지 않는다.
- 결과 요약은 `L/R/Woofer 공통 0 dB 기준`, 공통 FIR gain, 채널별 독립 정규화 없음, branch 상대레벨 보존을 한 카드에서 보여준다. 그래프도 L/R 500~2,000 Hz 하나의 기준을 사용했다는 설명을 legend 가까이에 둔다.
- 결과의 `Woofer 최종 trim`은 FIR 계산 옵션을, `측정 시 Woofer 감쇄`는 sweep SNR 확보용 측정 조건을 별도 항목으로 표시한다.
- 결과에 기록된 `algorithm_revision`이 현재 엔진과 다르면 측정 원본은 보존하되 이전 계산임을 경고하고 4단계 FIR 재계산 전 Preview/Apply를 차단한다.
- 최신 결과라도 `self_validation.overall_pass=false`이면 다운로드와 A/B Preview는 허용하지만 정식 Apply는 UI와 엔진 양쪽에서 차단한다.
- 자동 검증 체크리스트는 모든 core/FIR, 독립 3위치, L/R/Woofer target-fit, crossover 합산, SNR 판정을 `PASS`, `FAIL`, `대기`, `해당 없음`으로 표시한다. FAIL 행은 빨간색과 함께 실제 화면의 `1 · 연결·Cal`, `2 · 레벨 확인`, `3 · 위치 측정`, `4 · FIR 계산`, `5 · 검토·A/B` 단계명과 실제 select/button 문구를 사용한 직접 행동 지침을 제공한다. 안내에 언급된 단계는 바로 여는 버튼도 함께 만들며, 탭 이동만으로 측정값은 바뀌지 않는다. 실행할 수 없는 사후 측정을 해결 방법으로 쓰지 않는다.
- 체크리스트 바로 위에서 MAE를 판정 대역 평균 절대오차, P90을 주파수 지점 90%가 그 값 이내인 오차로 설명한다. 합산 FAIL은 signed median error를 사용해 Target보다 높은지/낮은지와 dB를 밝히고, `Woofer 최종 trim`, `우퍼 과잉 억제`, `Phase 방식`, `Crossover 주파수`의 실제 표시명으로 변경 방향을 안내한다.
- 결과 카드는 `pass`, `pass_safe_upper_phase_limited`, `pass_safe_sum_phase_limited`, `fail_target`, `fail_upper_guard`를 구분하고, 위상 제한 PASS를 정확한 복소 위상 검증으로 표현하지 않는다.
- 선택형 사후 검증 뒤 결과 그래프는 20 Hz~20 kHz의 실제 FIR 통과 실측, 계산 예상, 선택 target을 동시에 표시한다. 실측/예상 L/R은 각각 하나의 공통 기준만 사용한다. SNR 6~15 dB에서 경계값을 조금 넘으면 빨간 FAIL 대신 `판정 보류 · SNR 부족`과 실제 메뉴 `검증 초기화`, `검증 sweep 입력 -25 dBFS`를 표시한다. FIR 입력값과 공통 FIR 감쇄 뒤 실제 출력이 다를 수 있음을 control 바로 아래에서 설명한다.
- Pi4/5는 SISO와 MIMO Stereo/2.1/2.2를 구분하되 공통 timing reference가 없으면 MIMO 차단 이유를 표시한다. Pi2는 조건과 무관하게 선택할 수 없다.
- MIMO 결과는 타깃 MAE, 좌석 편차, 평활 전달함수 기반 impulse-tail proxy, 기존 SISO 저역 레벨 기준 offset, 해 혼합 강도, 제어원 coherence, crossover, 실제 output별 headroom과 전체 보정 가능성 분류표를 보인다. impulse-tail proxy는 RT60/잔향 예측이 아니며 1.5 dB 초과 악화 시 적용을 차단한다.
- 다운로드와 A/B는 비파괴라고 명시하고, 정식 적용 버튼만 덮어쓰기 경고를 낸다.
- 모든 펼침 영역은 일반 카드와 구분되는 테두리·배경, 최소 48 px 제목 행과 오른쪽 vector chevron을 사용한다. 닫힘/열림에 따라 chevron 방향과 제목 accent가 바뀌며 브라우저 기본 marker는 중복 표시하지 않는다.

## 설정 화면

- 전체 백업은 페이지 첫 영역이다. 복원은 ZIP 선택 → 무결성 검사 → 내용 확인 → 적용의 네 단계를 시각화한다.
- Engine 표에서 chunksize를 바꾸면 오디오가 잠시 재시작된다고 표시한다.
- 내부 키 `speaker/headphone`은 호환성을 위해 유지하지만 화면의 두 카드는 각각 `Speaker 출력 체인`, `Headphone 잭 출력 체인`이다. 이 설치에서는 둘 다 실제 스피커에 연결됨을 subtitle로 명시한다. U7 실제 선택 카드만 `active-profile` 외곽선과 badge를 가진다.
- 각 카드 상단은 Front FIR → Front/Rear routing → speaker chain의 compact signal-flow를 SVG 아이콘과 함께 표시한다.
- 카드에는 bypass, Front/Rear 파일 metadata, staged upload workflow, 기존/새 response graph, A/B, 정식 apply, Rear mode, woofer trim이 있다.
- Speaker 카드에는 검증된 MIMO bank 설치/활성 상태와 8-path 여부를 표시한다. Headphones에는 4채널 MIMO 적용 버튼을 만들지 않는다.
- 웹은 프로필 설정을 바꿀 수 있지만 U7 물리 LED/출력 selector를 바꾸는 버튼은 제공하지 않는다.

## 오류 방지

- 모든 form은 한 번 submit된 동안 버튼을 비활성화하고 `처리 중…`으로 바꾼다.
- 삭제·덮어쓰기·후속 측정 무효화는 confirm을 사용한다.
- staging과 preview 상태를 명확히 분리하고 `정식 WAV는 그대로` 문구를 반복한다.
- Preview가 active이면 현재 설정 표·signal-flow·FIR API는 저장된 profile mode보다 실제 임시 CamillaDSP 구성을 우선한다. 저장 `copy_front`/Preview `separate`와 그 반대 방향을 모두 정확히 표시하고 복귀 즉시 저장 mode로 돌아간다.
- invalid 요청은 원래 상태를 유지한 채 해당 화면에 오류를 표시한다.
- restore는 server-side 자동 rollback을 만든 뒤에만 실제 파일을 교체한다.

## 테마와 접근성

- 기본은 `prefers-color-scheme`; 사용자는 Auto/Light/Dark를 localStorage에 저장한다.
- 색만으로 상태를 구분하지 않고 badge, 텍스트, border를 같이 쓴다.
- Light/Dark의 primary control은 별도 `--on-accent` 전경색을 사용해 cyan 계열 accent에서도 텍스트 대비를 유지한다. 현재 탭은 배경뿐 아니라 하단 indicator와 `aria-current=page`로 표시한다.
- 본문 건너뛰기 링크와 화면별 `<title>`, 측정 단계의 `aria-current=step`, 오류의 `role=alert`를 제공한다.
- 장식 SVG 아이콘은 `aria-hidden=true`; 실제 응답 그래프만 `role=img`와 설명 label을 사용한다.
- range/select/button/file input에는 이름 또는 연결된 label이 있어야 한다. 파일 입력은 native selector를 테마 색에 맞추되 OS 파일 선택 동작은 유지한다.
- 좁은 화면의 700 px 주파수 그래프는 페이지 전체를 넘치게 하지 않고 자체 영역에서 스크롤한다. 이 영역은 키보드 focus와 region 설명을 제공한다.
- PC의 button/select는 최소 40 px, 760 px 이하 화면의 주요 control은 최소 44 px 높이를 사용한다. 고정 하단 navigation을 고려해 모바일 본문과 anchor에 여유 공간을 둔다.
- 수치·표·진행률은 tabular numeral을 사용해 polling 중 자리 폭 변화를 줄인다.
- `prefers-reduced-motion`에서는 transition을 끈다.
- 긴 hash/path와 좁은 모바일 폭은 wrap하며 수평 page overflow를 만들지 않는다.

## 성능 예산

- `/api/status`: 보이는 화면에서 약 1.5초 polling, file signature cache 사용
- `/api/volume`: 보이는 현황 화면에서 3초 polling, server cache 2.5초
- `/api/health`: 5초 polling
- 측정 상태: 측정 화면에서 0.5~1.5초 polling
- FIR FFT/그래프: 브라우저에서 한 번 계산하며 Pi 2 서버가 FFT를 반복하지 않는다.
