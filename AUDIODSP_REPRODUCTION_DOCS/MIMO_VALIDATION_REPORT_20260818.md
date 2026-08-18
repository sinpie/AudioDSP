# AudioDSP MIMO 무음 검증 보고서

최초 작성: 2026-08-18 · 최신 무음 회귀: 2026-08-19

## 범위

사용자 요청에 따라 실제 sweep/백색소음/음악은 재생하지 않았다. 연결된 Pi2에서는 후보 파일을 `/tmp`에서만 실행했고 설치된 AudioDSP, CamillaDSP 서비스, 정식 FIR과 설정은 변경하지 않았다.

## 결과

### 세 토폴로지 합성 검증 — PASS

`audiodsp-mimo.py self-test`가 48 kHz 합성 복소 전달함수와 세 위치를 사용했다.

| 토폴로지 | Left target MAE dB | Right target MAE dB | Left 좌석편차 dB | Right 좌석편차 dB | 최종 최대 row sum |
|---|---:|---:|---:|---:|---:|
| MIMO Stereo | 3.279 → 3.347 | 2.223 → 2.274 | 0.731 → 0.730 | 0.691 → 0.690 | 0.969 |
| MIMO 2.1 | 3.279 → 2.694 | 2.223 → 2.081 | 0.731 → 0.597 | 0.691 → 0.606 | 0.887 |
| MIMO 2.2 | 3.279 → 2.711 | 2.223 → 2.229 | 0.731 → 0.593 | 0.691 → 0.505 | 0.875 |

공통 검증:

- finite samples: PASS
- 정확히 4 stereo float32 WAV × 32768 frames: PASS
- 총 matrix paths 8: PASS
- 공통 인과 지연 검사: PASS
- 최악 상관입력 physical-output headroom: PASS
- 예측 target MAE와 좌석 편차 비퇴행: PASS
- Flat/추가 억제 없음/Woofer trim 0 dB 기준 세 토폴로지 모델: PASS
- MIMO UI 19개 옵션 bank의 finite/형식/headroom/인과 구조: 19/19 PASS

Target MAE는 실제 SISO base bank를 먼저 공통 정규화한 뒤, 한 저역 reference-band level만 맞춰 음색 형상을 비교한다. 위치별·주파수별 normalize는 하지 않는다. 실제 broadband 감쇄는 별도 `headroom.global_scale_db`로 남긴다.

Modal late/early energy는 평활 전달함수 기반 ringing proxy이며 실제 RT60 예측이 아니다. 1.5 dB보다 악화되면 적용을 차단한다. 19개 옵션 중 crossover 60/70/80 Hz, Harman target, Safe/지원 제한 12 dB의 다섯 합성 조합은 이 기준으로 `fail_model`이 되었고, UI가 실제 4단계 메뉴명으로 조정을 안내하는 것을 확인했다.

### 실제 CamillaDSP parser·관리자 — PASS

격리된 임시 config/state/profile에서 수행했다.

- MIMO manifest format/rate/taps/channels/float32/SHA-256 검증
- 2→8 mixer, `type: Conv` 8개, 8→4 합산 config 생성
- 실제 `/usr/local/bin/camilladsp --check` 통과
- MIMO 설치 후 `convolution_channels=8`, `effective_rear_mode=mimo_2x4`
- MIMO OFF 후 SISO 2 convolution 복귀
- Pi2에서 MIMO ON 거부

### 백업·복원 staging — PASS

- schema version 2
- 네 MIMO WAV와 `Speaker_MIMO.json` ZIP 포함
- byte/SHA-256 inventory 검증
- 복원 staging에서 MIMO bank 재검증
- 정식 system 상태에는 적용하지 않음

### 기존 기능 회귀 — PASS

- Python/shell/PowerShell 정적 parse
- measurement 합성 end-to-end: magnitude/bass-phase 32768탭, actual FIR target 검증, natural roll-off, decay control PASS
- profile matrix: 4096 상태(3968 valid, 128 expected error), 3136 ordered transitions, 실제 CamillaDSP config 28종, Web/HID/volume/backup/session 삭제/FAIL 가이드/concurrency PASS
- Pi2/Pi4-Pi5 SD writer `-ValidateOnly`: image/binary/Factory FIR/필수 payload/정책 PASS

### Pi 5 2 GB / 5.1 메모리 worst-case — PASS (계획·무음 allocation)

- 현재 2×4/8경로: 원시 FIR 1.00 MiB, runtime 계획 46 MiB, 생성 계획 309 MiB
- 5.1 diagonal/6경로: 원시 FIR 0.75 MiB, runtime 계획 40 MiB, 생성 계획 296 MiB
- 5.1 + dual-sub 완전 dense 6×7/42경로: 원시 FIR 5.25 MiB, runtime 계획 135 MiB, 생성 계획 530 MiB
- 42경로 64-bit CPython 실제 배열 allocation peak: 138.64 MiB
- generator는 path spectrum 조기 해제와 in-place bank scaling을 사용한다.

2 GB 용량 판정은 PASS다. 42경로 연산량은 약 64.6 M partition complex MAC/s이므로 CPU/XRUN은 별도 Pi 5 실기 판정이며 아직 PASS가 아니다. 권장 5.1 구조는 채널별 diagonal FIR + 150 Hz 이하 저역 actuator group MIMO다.

## 완료되지 않은 실기 항목

다음은 사용자 승인 후 실제 소리를 내는 별도 수락 시험이다.

1. 현재 방에서 MIMO 2.1의 각 위치×Front L/Front R/T5S 독립 sweep
2. 적용에 사용하지 않은 검증 위치를 포함한 전/후 재측정
3. 실제 target 오차, 좌석 편차, 저역 decay와 crossover 합산 확인
4. Pi4 또는 Pi5에서 chunksize 1024, 8 convolution의 10분 이상 CPU/XRUN/온도/USB 안정성
5. 청취 A/B와 야간 저역/층간소음 운영값 확인

합성 PASS는 위 실기 결과를 대신하지 않는다. 특히 late reverberation, 비선형 왜곡, 절대 SPL과 구조전달 층간소음은 FIR/MIMO 해결로 표시하지 않는다.

## 종료 시 라이브 상태 확인

2026-08-19 최종 점검은 읽기 전용으로만 수행했다. 연결된 production Pi 2의 CamillaDSP PID는 `7731`이었고 `camilladsp`, `audiodsp-web`, `audiodsp-profile-monitor`는 모두 active였다. Speaker FIR SHA-256은 기존 `8a8a3b2fc31a080a6bc40205f29ea6471df95adf357618b2025bdd193ef45c99` 그대로였고 U7 저장/실제 볼륨은 `0 dB`, 8채널 동일이었다. 소리 재생, CamillaDSP 재시작, 프로필·FIR·볼륨 변경은 하지 않았다. Pi 2의 새 식별자는 hostname `audiodsp-pi2`, user `audiodsp`, Ethernet profile `audiodsp-ethernet`이며 이전 식별자는 활성 경로에서 제거했다.
