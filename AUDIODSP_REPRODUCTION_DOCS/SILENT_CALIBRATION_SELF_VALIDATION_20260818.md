# AudioDSP 무음 캘리브레이션 자체 검증 — 2026-08-18

## 검증 범위

사용자 요청에 따라 최종 검증 단계에서는 스피커, 우퍼, 안내 음성을 포함한 어떤 소리도 재생하지 않았다. 실제 ALSA 장치와 현재 프로필을 사용하지 않는 합성 응답 및 격리 환경에서 알고리즘, Web UI, 프로필 전환과 복원 동작을 검증했다.

실제 UMIK-1 음향 측정은 연기했다. 따라서 이 문서는 현재 임의 마이크 위치에서 방의 실제 응답이 특정 target에 도달했다는 수락 시험 결과가 아니다.

## 결과

- FFTW3f 48 kHz/32768-tap round-trip: PASS, 최대 오차 `2.562475210909909e-07`
- 6 targets × 3 bass presets × Front/Woofer = 36 FIR 경로 및 저역 phase 대표 경로: PASS
- 실제 생성된 32768-tap WAV 재-FFT 검증: PASS
- 합성 0.60 s decay의 noise-compensated Schroeder EDT/T20→RT60 추정: PASS
- 신뢰 가능한 300 Hz 이하 긴 decay에만 최대 3 dB cut을 추가하고 boost를 만들지 않는 정책: PASS
- 오프라인 측정 엔진 전체 회귀: PASS, Pi 2에서 2분 18초
- 프로필 상태 4,096개: 정상 3,968개, 의도된 no-profile 오류 128개, 모두 PASS
- 설정 변경 순서 3,136쌍, 56 operations: PASS
- Web 통합: WAV/ZIP 다운로드, staged preview/apply/discard, backup/restore, fallback, volume, bypass, rear mode, chunksize, woofer trim, HID 상태 반영 및 동시 요청 33개 모두 PASS
- Python compile 12 files, shell syntax 3 files: PASS
- Pi 2 및 Pi 4/5 SD bundle validator: PASS

## 현재 장치 보존 확인

- `camilladsp.service`, `audiodsp-web.service`, `audiodsp-profile-monitor.service`: active
- CamillaDSP PID: `2036`, 검증 중 재시작 없음
- Speaker Front FIR SHA-256: `8a8a3b2fc31a080a6bc40205f29ea6471df95adf357618b2025bdd193ef45c99`, 변경 없음
- Xonar U7 출력: 저장값/실제값 모두 `-10 dB`, 8채널 동일
- 현재 측정 포인터: 기존 세션 `20260818_004556`, `ready`, 새 FIR 미적용

사전 백업은 `D:\GSonic\RaspberryPi_SD\pretest-backups\20260818-031015`에 있다.

## 잔향 보정의 의미와 한계

AudioDSP는 octave-band impulse decay에서 EDT와 T20을 RT60으로 환산해 보여준다. 충분한 SNR과 선형성(R² ≥ 0.80)이 있는 저역 decay만 사용하며, 이미 감쇄가 필요한 300 Hz 이하 모드에 최대 3 dB의 추가 cut을 적용한다. 이는 음악이 해당 room mode를 덜 자극하게 해 저음의 꼬리와 웅웅거림을 줄이는 방식이다.

단일 재생 FIR은 방의 물리적 흡음이나 모든 위치의 late reverberation을 제거하지 못한다. 중·고역 late reverb와 위치마다 달라지는 반사음을 무리하게 역필터링하지 않는다. 그 영역은 스피커/청취 위치, 흡음, 베이스 트랩 또는 다중 우퍼 배치로 다루는 것이 안전하다.

## 남은 실제 수락 시험

주간에 UMIK-1을 90°로 천장 방향 배치하고 청취 위치 주변 3점을 측정한다. 레벨 검사와 각 sweep SNR이 통과한 뒤 기존/이번 tuning을 preview로 비교하되, Apply 전에는 profile WAV를 덮어쓰지 않는다. 현재 임의 위치에서 앞서 시도한 개별 sweep은 SNR이 부족했으므로 실제 수락 결과로 사용하지 않는다.
