# 백업, 복원, 마이그레이션

## Backup schema 2

브라우저 전체 백업은 `AudioDSP_backup_YYYYMMDD-HHMMSS.zip`이다.

필수:

- `manifest.json`
- `profile-settings.json`
- `profiles/Factory_Speaker_Front_LR.wav`

선택:

- `correction-preferences.json`
- `profiles/Speaker_Front_LR.wav`
- `profiles/Speaker_Rear_LR.wav`
- `profiles/Headphone_Front_LR.wav`
- `profiles/Headphone_Rear_LR.wav`
- `profiles/mimo/MIMO_Front_Left_LR_32768.wav`
- `profiles/mimo/MIMO_Front_Right_LR_32768.wav`
- `profiles/mimo/MIMO_Rear_Left_LR_32768.wav`
- `profiles/mimo/MIMO_Rear_Right_LR_32768.wav`
- `profiles/mimo/Speaker_MIMO.json`
- `calibration/7200660.txt`
- `calibration/7200660_90deg.txt`
- `README.txt`

Manifest 예:

```json
{
  "format": "AudioDSP Backup",
  "schema_version": 2,
  "app_version": "1.2.0",
  "created_unix": 0,
  "files": {
    "profile-settings.json": {
      "bytes": 0,
      "sha256": "..."
    }
  }
}
```

`files` inventory는 manifest/README를 제외한 모든 data member와 정확히 일치해야 한다.

## 복원 안전 절차

1. ZIP을 메모리에서 열고 중복·허용 외 경로를 거부한다.
2. 개별 32 MiB, 전체 압축 해제 64 MiB를 넘으면 거부한다.
3. format과 schema를 확인한다.
4. inventory의 byte 수와 SHA-256을 모두 확인한다.
5. 임의 token 디렉터리 `/var/lib/audiodsp/restore-staging/...`에 0600으로 원자 작성한다.
6. settings를 strict normalize하고 모든 FIR WAV와 calibration을 실제 parser로 검증한다.
7. UI에 요약만 보여주고 현재 장치는 바꾸지 않는다.
8. 사용자가 적용하면 현재 상태를 `/var/lib/audiodsp/system-backups`에 자동 ZIP으로 먼저 저장한다.
9. calibration/preferences/FIR/settings를 적용한다. 중간 실패 시 이전 calibration/preferences와 관리자 snapshot 복구 경로를 사용한다.
10. staging state를 제거한다.

측정/계산/cancel이 진행 중이면 복원을 거부한다.

## Settings 호환

Schema 2 `profile-settings.json`에는 다음 key가 정식이다.

- `requested_profile`
- `chunksize`
- `output_volume_db`
- `bypass.speaker`, `bypass.headphone`
- `mimo_enabled.speaker`, `mimo_enabled.headphone`
- `rear_mode.speaker`, `rear_mode.headphone`
- `woofer_trim_db.speaker`, `woofer_trim_db.headphone`

이전 schema 1 백업에 `output_volume_db` 또는 `mimo_enabled`가 없으면 각각 -10 dB와 false default를 사용한다. 알 수 없는 top-level key는 보고 후 무시한다. 알려진 key의 타입/범위가 잘못되면 복원 전 검증에서 거부한다. 현재 지원 버전보다 큰 schema는 downgrade 추측 없이 거부한다.

볼륨은 settings에 포함되므로 복원 때 저장값도 바뀐다. 관리자 snapshot 복원은 CamillaDSP를 재시작하며 시작 래퍼가 복원된 볼륨을 적용한다.

## AudioDSP 식별자 통일

신규 설치:

- user: `audiodsp`
- hostname: Pi 2 `audiodsp-pi2`, Pi 4/5 `audiodsp-pi`
- state: `/var/lib/audiodsp`
- share: `/usr/local/share/audiodsp`
- service/binary: `audiodsp-*`

기존 장치는 먼저 Web backup과 `/var/lib/audiodsp` snapshot을 만든다. 이어서 `audiodsp` 계정·SSH·sudo를 만들고 새 계정의 로그인을 실제 확인한 뒤 hostname과 소유권을 바꾼다. 설치·실행·시험 환경변수는 `AUDIODSP_<NAME>`만 사용한다. 이전 식별자 fallback은 제거되어 있으므로 복원한 오래된 systemd override도 함께 점검한다.

2026-08-19 production Pi 2 이전 백업:

- `audiodsp-pre-final-20260819.tar.gz`: 이전 전 앱·설정 상태
- `legacy-identity-home-20260819.tar.gz`: 이전 계정 home
- `legacy-identity-residuals-20260819.tar.gz`: 제거 전 잔여 unit/config/payload

로컬 `diagnostics/pretest-backups`와 Pi의 root 전용 `/var/backups/audiodsp`에 같은 복구본을 둔다. 이전 완료 조건은 새 계정 SSH와 passwordless sudo, 세 서비스 active, CamillaDSP PID·Speaker FIR SHA·U7 볼륨 불변, 활성 `/etc`·`/usr/local`·`/var/lib/audiodsp`·`/boot`의 이전 식별자 검색 0건이다.

## 릴리스 간 복원

Pi 2와 Pi 4/5는 앱 settings/FIR/calibration 형식이 같아 schema 1/2 백업을 서로 복원할 수 있다. MIMO bank와 설정을 Pi2에 복원해도 실시간 활성화는 차단되며 SISO로 동작한다. backup의 chunksize도 그대로 복원되므로 Pi 2에 1024를 옮긴 경우 부하를 확인하고 2048로 되돌리는 것이 권장된다. OS 이미지, SSH key, network secret, systemd unit, CamillaDSP binary는 브라우저 backup에 포함되지 않는다.
