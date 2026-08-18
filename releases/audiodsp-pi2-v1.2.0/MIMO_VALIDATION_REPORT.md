# AudioDSP MIMO 무음 검증 보고서

날짜: 2026-08-18

## 범위

사용자 요청에 따라 실제 sweep/백색소음/음악은 재생하지 않았다. 연결된 Pi2에서는 후보 파일을 `/tmp`에서만 실행했고 설치된 AudioDSP, CamillaDSP 서비스, 정식 FIR과 설정은 변경하지 않았다.

## 결과

### 세 토폴로지 합성 검증 — PASS

`audiodsp-mimo.py self-test`가 48 kHz 합성 복소 전달함수와 세 위치를 사용했다.

| 토폴로지 | Left target MAE dB | Right target MAE dB | Left 좌석편차 dB | Right 좌석편차 dB | 최종 최대 row sum |
|---|---:|---:|---:|---:|---:|
| MIMO Stereo | 5.520 → 3.626 | 4.195 → 2.936 | 0.731 → 0.721 | 0.691 → 0.675 | 0.999 |
| MIMO 2.1 | 5.520 → 3.676 | 4.195 → 2.905 | 0.731 → 0.720 | 0.691 → 0.675 | 0.999 |
| MIMO 2.2 | 5.520 → 3.832 | 4.195 → 3.018 | 0.731 → 0.715 | 0.691 → 0.673 | 0.999 |

공통 검증:

- finite samples: PASS
- 정확히 4 stereo float32 WAV × 32768 frames: PASS
- 총 matrix paths 8: PASS
- 공통 인과 지연 검사: PASS
- 최악 상관입력 physical-output headroom: PASS
- 예측 target MAE와 좌석 편차 비퇴행: PASS
- 제어원별 bulk arrival phase 복원: PASS
- 70~130 Hz 기존 SISO 저역 레벨 anchor: PASS
- modeled late/early 0.5 dB 비악화: PASS

Modal late/early energy는 모든 토폴로지에서 0.26~0.41 dB 나빠졌으므로 decay 개선으로 판정하지 않는다. 다만 0.5 dB보다 악화되면 전체 결과를 실패시키는 core guard 안에는 들어왔다. 이 수치는 합성 선형 모델의 안전 비퇴행 판정이지 실제 방의 잔향 개선 인증이 아니다.

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
- 적용/취소 후 restore staging state와 추출 디렉터리 회수
- 정식 system 상태에는 적용하지 않음

### 기존 기능 회귀 — PASS

- Python/shell/PowerShell 정적 parse
- measurement 합성 end-to-end: magnitude/bass-phase 32768탭, actual FIR target 검증, natural roll-off, decay control PASS
- profile matrix: 4096 상태(3968 valid, 128 expected error), 3136 ordered transitions, 실제 CamillaDSP config 28종, Web/HID/volume/backup/concurrency PASS
- Pi2/Pi4-Pi5 SD writer `-ValidateOnly`: image/binary/Factory FIR/필수 payload/정책 PASS

## 완료되지 않은 실기 항목

다음은 사용자 승인 후 실제 소리를 내는 별도 수락 시험이다.

1. 현재 방에서 MIMO 2.1의 각 위치×Front L/Front R/T5S 독립 sweep
2. 적용에 사용하지 않은 검증 위치를 포함한 전/후 재측정
3. 실제 target 오차, 좌석 편차, 저역 decay와 crossover 합산 확인
4. Pi4 또는 Pi5에서 chunksize 1024, 8 convolution의 10분 이상 CPU/XRUN/온도/USB 안정성
5. 청취 A/B와 야간 저역/층간소음 운영값 확인

합성 PASS는 위 실기 결과를 대신하지 않는다. 특히 late reverberation, 비선형 왜곡, 절대 SPL과 구조전달 층간소음은 FIR/MIMO 해결로 표시하지 않는다.

## 종료 시 라이브 상태 확인

2026-08-18 최종 무음 회귀 직후 읽기 전용 점검에서 production Pi 2의 CamillaDSP PID는 12593이었고 `camilladsp`, `audiodsp-web`, `audiodsp-profile-monitor`는 모두 active였다. 최근 30분 journal의 XRUN/underrun/overrun/panic/error 일치는 0회였으며 Speaker FIR SHA-256은 기존 `8a8a3b2fc31a080a6bc40205f29ea6471df95adf357618b2025bdd193ef45c99` 그대로였다. 온도는 60.5°C, 사용/가용 메모리는 209/710 MB였다. 소리 재생, CamillaDSP 재시작, 프로필·설정 변경은 하지 않았다.
