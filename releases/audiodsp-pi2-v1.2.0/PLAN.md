# AudioDSP 계획과 기준선

## 완료된 v1.2 기준선

- Pi 2 armhf/ARMv7과 Pi 4·Pi 5 arm64/aarch64용 독립 SD 작성 번들
- Xonar U7 2입력 → Front L/R + Rear L/R 4출력 경로
- Speaker/Headphones 프로필, 다른 프로필·Factory 순서의 자동 fallback
- 프로필별 DSP bypass, Rear 복사/별도 FIR, woofer trim, chunksize 설정
- U7 실제 선택 상태를 HID에서 읽어 웹에 실시간 반영하고 영어 여성 안내음을 Front L/R에 믹스
- 부팅 초기화 완료 후 `DSP ready` 안내
- U7 전역 PCM 출력 볼륨의 실제값 조회와 -60~0 dB 저장·적용을 웹/API로 제공
- 현황/측정·보정/프로필·설정 화면 분리, 반응형 light/dark/auto 테마
- 브라우저 계산 SVG FIR 응답, 업로드 전/후 비교, 임시 A/B, 명시적 정식 적용
- UMIK-1 0°/90° calibration 보관, 실제 측정은 90°만 허용
- 5초 무음 + 5초 백색소음 레벨 사전 평가
- L/R 또는 L/R/Woofer 각각 세 위치 측정과 32768-tap FIR 생성
- target, bass-control preset, 음색 tilt, 보정 대역, boost/cut, phase 옵션
- 측정 중 DSP direct bypass와 U7 입력 mute, 완료/오류 시 원상 복구
- 버전형 전체 백업 ZIP, staging 검증, 적용 직전 자동 rollback ZIP
- 4,096개 상태 진리표와 모든 설정 전이·웹·동시 쓰기 회귀 시험

## 변하지 않아야 하는 핵심 조건

1. 오디오 샘플레이트는 전 구간 48 kHz다.
2. 새 FIR은 stereo float32, 정확히 32768 taps이며 전달 최대 이득은 0 dB 이하로 정규화한다.
3. 디지털 preamp/Gain 필터를 자동 삽입하지 않는다.
4. Front FIR이 없을 때는 다른 프로필, 그다음 Factory로 fallback한다. 존재하지 않는 Rear FIR은 Front 처리 결과를 복사한다.
5. 웹에서 U7 Speaker/Headphones 선택을 가장하지 않는다. 실제 HID 상태만 표시한다.
6. 볼륨 변경만으로 CamillaDSP를 재시작하거나 FIR/config를 다시 쓰지 않는다.
7. 측정 단계 링크 이동은 데이터를 삭제하지 않는다. 변경값을 적용하거나 재측정을 실제 시작할 때만 영향받는 후속 결과를 폐기한다.
8. 업로드·복원·새 튜닝은 검토와 A/B 단계를 거친 후 별도 적용 버튼에서만 정식 파일을 교체한다.
9. DHCP 실패용 임의 고정 주소를 만들지 않는다.
10. Pi 2의 CPU·메모리 한계를 이유로 그래프 FFT를 서버에서 반복 계산하지 않는다.

## 다음 릴리스 후보

- HTTP 인증 또는 reverse proxy 옵션. 현재 UI는 신뢰된 LAN 전용이다.
- WebSocket/SSE를 이용한 상태 push. 현재 1초 상태, 3초 볼륨 polling은 Pi 2에서 충분히 가볍다.
- 실제 U7 볼륨 변경 이벤트 감지 시 저장 여부를 사용자가 고르는 옵션.
- 측정 세션 목록·이름 지정·내보내기 UI.
- Pi 5 실기 장시간 부하와 USB 복구 시험. 현재 Pi 5는 arm64 호환 설계와 번들 검증까지만 완료됐다.
- 연구 알고리즘 변경 전 고정 측정 fixture와 수치 회귀 허용오차 정의.

후보 기능은 현재 동작을 깨지 않는 별도 버전에서 진행한다. 특히 인증 추가나 볼륨 저장 정책 변경은 API 호환성과 부팅 동작을 바꾸므로 백업 스키마 및 문서 버전을 함께 올린다.
