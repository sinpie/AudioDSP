# 변경 이력

## 2026-08-18 · v1.2 유지보수 revision

- 자동 room-EQ cut 포화가 bass/treble 사용자 선호도를 소거하던 문제를 수정하고, 명시적 house curve를 자동 제한 뒤 적용하며 두 correction 성분을 보고서에 분리
- UMIK 장시간 녹음의 ALSA overrun을 성공으로 오인하지 않도록 `arecord --fatal-errors`를 적용해 해당 측정을 즉시 실패·재시도 대상으로 처리
- 대역 제한 Woofer ESS의 10.8초 고조파/잡음 peak를 direct delay로 오인하던 문제를 발견해 0~250 ms 인과 gate, phase/decay fallback, 부분 상대지연 금지, `time_alignment_safe` 검사를 추가
- 실측 한 세트로 모든 UI SISO 값을 one-factor-at-a-time 생성하는 67개 FIR matrix와 134개 저음량 합산 sweep 검증 도구 추가
- 백색소음/sweep 안전 기본값을 모두 -42 dBFS로 통일하고 독립 slider, 높은 출력 실시간 경고, 실제 sweep 전 수치 confirm 추가
- Woofer SNR을 고정 전대역 대신 chirp-time 지속 -3 dB 통과대역과 pre/post noise PSD로 판정하고 순간 생활소음 confidence 적용
- 첫 UMIK/ALSA cold-start가 고정 pre-roll을 소비해도 실제 sweep 활성구간과 capture delay를 자동 복구하고, 검출 구간 밖에서만 noise PSD를 계산하도록 수정
- Front/Woofer 정렬에 음향 bulk delay와 FIR 에너지 지연을 함께 사용하고 L/R 공통 phase·magnitude 보존 자동 축소 추가
- FFTW plan/buffer 재사용, Pi별 offline ETA, PID/cmdline 기반 중단 worker 복구 추가
- MIMO에 상대 bulk-delay phase 복원, 기존 SISO 저역 레벨 anchor, 인접-bin continuity, 안전 해 blend, modeled late/early 0.5 dB 적용 차단 추가
- 백업 파일명을 고유화하고 검증 실패·교체·취소·적용 시 restore staging 추출 디렉터리 누수 제거
- 위 항목을 SD writer 필수 marker와 profile/measurement/MIMO 회귀시험에 추가
- 2026년까지의 MIMO/weighted pressure matching/excess-phase/공간 보간 연구 재검토와 채택·보류 근거 문서화
- Pi4/5 MIMO Stereo/2.1/2.2, 2×4/8-path 32768탭 bank와 SISO 전이 구현
- 주파수별 physical-output headroom 투영, 공통 인과 지연, 제어원 coherence·예측 비퇴행 검사 추가
- Pi2 MIMO 측정/UI/runtime 차단, 한 stereo-fed T5S를 한 물리 제어원으로 강제
- MIMO Preview/Apply/rollback, schema-v2 bank 백업/복원 staging 추가
- 모든 결과에 보정 가능·제한·물리 처리·미측정·미인증 JSON/Markdown 보고서 추가
- 세 토폴로지 합성, 실제 CamillaDSP 8-path parser, backup staging 무음 회귀시험 추가

- 측정 재생을 검증된 `audiodsp_announce` 4채널 경로로 통일
- Woofer 측정/reference를 같은 조절 가능 비율로 감쇄하고 sweep별 SNR gate 추가
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
