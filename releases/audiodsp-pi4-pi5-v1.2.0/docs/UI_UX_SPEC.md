# AudioDSP UI/UX 명세

## 정보 구조

하단 또는 상단 공통 navigation은 세 화면만 제공한다.

1. `현황`: 다음 할 일, 전역 출력 볼륨, 실제 U7/DSP 상태, 현재 FIR 그래프
2. `측정 · 보정`: calibration부터 정식 적용까지 여섯 단계
3. `프로필 · 설정`: 전체 백업/복원, engine 설정, Speaker/Headphones 프로필 카드

모바일에서는 navigation이 하단 고정이고, PC에서는 콘텐츠 상단에 폭을 나눠 배치한다. 화면 사이를 이동해도 server-side 상태는 유지된다.

## 현황 화면

- 첫 카드는 측정 session 상태에 따라 하나의 `지금 할 일`을 제안한다.
- 출력 볼륨 카드는 큰 dB 숫자, -60~0 slider, ±1 dB, -40/-30/-20/-10 preset, 저장·적용 버튼을 가진다.
- Slider 이동 중 예상값을 즉시 표시하고 release 시 PUT한다. 명시적 버튼은 키보드/비-JS fallback이다.
- U7 실제값과 재부팅 저장값이 다르면 `물리 노브 변경 감지`를 표시한다.
- 볼륨은 Front/Rear 전체 하드웨어 출력이라는 설명과 CamillaDSP 무재시작 설명을 항상 보인다.
- 현재 설정 표는 U7 실제 출력, 요청/유효 profile, fallback, bypass, A/B, Rear mode, convolution 수, 서비스, 장치, format/chunksize를 보여준다.
- FIR 그래프는 L/R 또는 L/R+Woofer toggle이며 브라우저가 WAV를 받아 SVG를 계산한다.

## 측정 화면

상단 workflow는 `연결·Cal → 레벨 → 3위치 측정 → FIR 계산 → 검토·A/B → 정식 적용`이다.

- 완료/현재 step은 anchor로 이동할 수 있다. 미래 step은 비활성이다.
- step click은 navigation뿐이며 데이터 mutation을 하지 않는다.
- 편집한 설정은 `변경 적용` 전까지 기존 결과에 영향을 주지 않는다.
- 재검사/재측정/재계산 버튼은 초기화 범위를 confirm 문구로 정확히 말한다.
- 진행 중에는 progress, percent, 단계명, ETA를 1초 polling한다.
- Level 결과는 OK/NOT OK, background, white-noise RMS, 추정 signal, SNR, peak를 카드로 보여준다.
- 백색소음과 sweep 출력은 별도 slider이며 fresh session은 둘 다 -42 dBFS다. 현재 Woofer 실효값을 계산하고 높은 조합을 색+문구로 경고하며 실제 위치 sweep 전 confirm에 수치를 다시 표시한다.
- Target을 바꾸면 1 kHz 기준 곡선과 bass/treble preference를 즉시 SVG에 반영한다.
- 결과 그래프는 각 채널의 측정 전 ±공간 편차, 적용 후 예상, target을 구분한다.
- Pi4/5는 SISO와 MIMO Stereo/2.1/2.2를 구분한다. Pi2는 MIMO 항목과 차단 이유를 보이되 선택할 수 없다.
- MIMO 결과는 타깃 MAE, 좌석 편차, modal late/early 모델값, 기존 SISO 저역 레벨 기준 offset, 해 혼합 강도, 제어원 coherence, headroom과 전체 보정 가능성 분류표를 보인다.
- 다운로드와 A/B는 비파괴라고 명시하고, 정식 적용 버튼만 덮어쓰기 경고를 낸다.

## 설정 화면

- 전체 백업은 페이지 첫 영역이다. 복원은 ZIP 선택 → 무결성 검사 → 내용 확인 → 적용의 네 단계를 시각화한다.
- Engine 표에서 chunksize를 바꾸면 오디오가 잠시 재시작된다고 표시한다.
- Speaker/Headphones는 동일 구조의 카드다. U7 실제 선택 카드만 `active-profile` 외곽선과 badge를 가진다.
- 카드에는 bypass, Front/Rear 파일 metadata, staged upload workflow, 기존/새 response graph, A/B, 정식 apply, Rear mode, woofer trim이 있다.
- Speaker 카드에는 검증된 MIMO bank 설치/활성 상태와 8-path 여부를 표시한다. Headphones에는 4채널 MIMO 적용 버튼을 만들지 않는다.
- 웹은 프로필 설정을 바꿀 수 있지만 U7 물리 LED/출력 selector를 바꾸는 버튼은 제공하지 않는다.

## 오류 방지

- 모든 form은 한 번 submit된 동안 버튼을 비활성화하고 `처리 중…`으로 바꾼다.
- 삭제·덮어쓰기·후속 측정 무효화는 confirm을 사용한다.
- staging과 preview 상태를 명확히 분리하고 `정식 WAV는 그대로` 문구를 반복한다.
- invalid 요청은 원래 상태를 유지한 채 해당 화면에 오류를 표시한다.
- restore는 server-side 자동 rollback을 만든 뒤에만 실제 파일을 교체한다.

## 테마와 접근성

- 기본은 `prefers-color-scheme`; 사용자는 Auto/Light/Dark를 localStorage에 저장한다.
- 색만으로 상태를 구분하지 않고 badge, 텍스트, border를 같이 쓴다.
- 모든 SVG는 `role=img`와 설명 label이 있다.
- range/select/button에는 이름 또는 label이 있어야 한다.
- `prefers-reduced-motion`에서는 transition을 끈다.
- 긴 hash/path와 좁은 모바일 폭은 wrap하며 수평 page overflow를 만들지 않는다.

## 성능 예산

- `/api/status`: 보이는 화면에서 1초 polling, file signature cache 사용
- `/api/volume`: 보이는 현황 화면에서 3초 polling, server cache 2.5초
- `/api/health`: 5초 polling
- 측정 상태: 측정 화면에서 1초 polling
- FIR FFT/그래프: 브라우저에서 한 번 계산하며 Pi 2 서버가 FFT를 반복하지 않는다.
