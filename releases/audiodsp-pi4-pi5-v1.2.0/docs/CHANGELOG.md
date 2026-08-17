# 변경 이력

## 2026-08-18 · v1.2 유지보수 revision

- 2026년까지의 MIMO/weighted pressure matching/excess-phase/공간 보간 연구 재검토와 채택·보류 근거 문서화
- Pi4/5 MIMO Stereo/2.1/2.2, 2×4/8-path 32768탭 bank와 SISO 전이 구현
- 주파수별 physical-output headroom 투영, 공통 인과 지연, 제어원 coherence·예측 비퇴행 검사 추가
- Pi2 MIMO 측정/UI/runtime 차단, 한 stereo-fed T5S를 한 물리 제어원으로 강제
- MIMO Preview/Apply/rollback, schema-v2 bank 백업/복원 staging 추가
- 모든 결과에 보정 가능·제한·물리 처리·미측정·미인증 JSON/Markdown 보고서 추가
- 세 토폴로지 합성, 실제 CamillaDSP 8-path parser, backup staging 무음 회귀시험 추가

- 측정 재생을 검증된 `audiodsp_announce` 4채널 경로로 통일
- Woofer 측정/reference를 함께 -12 dB 감쇄하고 sweep별 SNR gate 추가
- octave noise-compensated Schroeder EDT/T20 및 저역 장시간 공진 cut-only 제어 추가
- 실제 32768탭 FIR FFT 기반 target-fit/구현오차/전달이득/impulse 셀프검증 추가
- Pi 2 14초 4채널 sweep WAV 생성을 약 50초에서 17초로 단축
- 6 target × 3 preset × Front/Woofer 무음 행렬과 잔향 합성 회귀시험 추가
- U7 전역 PCM 출력 볼륨을 Web UI와 API에서 읽고 쓰는 기능 추가
- 실제 하드웨어값과 재부팅 저장값을 분리 표시
- -60~0 dB 정수 제한, 8채널 동일 raw mapping, 물리 노브 변화 polling
- 볼륨 변경 시 CamillaDSP/FIR 무재시작 보장
- 부팅/USB reset 후 `profile-settings.json`의 볼륨 복원
- full backup strict schema에 `output_volume_db` 추가; 이전 backup은 -10 dB로 보완
- profile matrix에 volume operations, API/form/invalid/physical/concurrent 시험 추가
- Pi 2와 Pi 4/5 writer preflight에 volume marker 추가
- 재현용 README, PLAN, ARCHITECTURE, AGENTS, API, DSP, 플랫폼, 시험, 보안 문서 작성

## 2026-08-18 · v1.2.0 정리

- UI/코드의 제품명을 AudioDSP로 통일
- 새 설치 hostname/user/service/state 식별자를 `audiodsp`로 변경
- Pi 2와 Pi 4/5 최종 릴리스 디렉터리 분리
- 단계형 측정 UI, dependency-aware invalidation, staged WAV, full backup/restore 완성
- multi-position 32768-tap FIR engine, target/preset, phase/시간 정렬, browser SVG 완성
- Pi 2 exhaustive/measurement 실기 시험과 bundle validator 추가

## 이전 기준

`legacy-v1-reference`와 `pi2-strong-bass-4ch-v1.1.0`에 GSonic 이름의 개발·장애 대응 이력이 보존되어 있다. 이 항목들은 새 릴리스 입력이 아니다.
