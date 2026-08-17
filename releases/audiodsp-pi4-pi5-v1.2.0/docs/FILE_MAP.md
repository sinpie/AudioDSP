# 파일 지도

## Release root

| 파일 | 역할 |
|---|---|
| `*.img.xz` | 고정 Raspberry Pi OS Lite image |
| `camilladsp-linux-*.tar.gz` | upstream archive 기록 |
| `firstrun.sh` | image 첫 부팅 설치기 |
| `write_*_sd_as_admin.ps1` | 검증·대상 확인·image 기록·FAT payload 복사 |
| `WRITE_*.cmd` | 관리자 writer 실행 진입점 |
| `verify_*.ps1`, `VERIFY_*.cmd` | 작성 후 또는 live 검증 진입점 |
| `audiodsp_pi_ed25519(.pub)` | 새 설치 SSH key pair; private 취급 |
| `test_profile_matrix.py` | exhaustive isolated profile/Web/HID/volume 시험 |
| `test_measurement_engine.py` | 합성 measurement/DSP 시험 |
| `SHA256SUMS.txt` | 릴리스 파일 inventory |

## Payload → 설치 위치

| Payload | Raspberry Pi 위치 |
|---|---|
| `camilladsp` | `/usr/local/bin/camilladsp` |
| `audiodsp-camilladsp-start` | `/usr/local/bin/audiodsp-camilladsp-start` |
| `audiodsp-profile-manager.py` | `/usr/local/bin/audiodsp-profile-manager.py` |
| `audiodsp-profile-web.py` | `/usr/local/bin/audiodsp-profile-web.py` |
| `audiodsp-measurement.py` | `/usr/local/bin/audiodsp-measurement.py` |
| `audiodsp-profile-monitor.py` | `/usr/local/bin/audiodsp-profile-monitor.py` |
| `audiodsp-output-profile` | `/usr/local/bin/audiodsp-output-profile` |
| `audiodsp-dsp-ready` | `/usr/local/bin/audiodsp-dsp-ready` |
| `asound-audiodsp.conf` | `/etc/asound.conf` |
| `camilladsp.yml` | `/etc/camilladsp/camilladsp.yml` 초기 template |
| `*.service` | `/etc/systemd/system/*.service` |
| `Harman_StrongBassControl_*.wav` | Factory 및 초기 Speaker profile |
| `7200660*.txt` | `/var/lib/audiodsp/calibration/` |
| `target_*.txt` | `/usr/local/share/audiodsp/targets/` |
| `announce_*.wav` | `/usr/local/share/audiodsp/` |

## Runtime filesystem

| 경로 | 내용 |
|---|---|
| `/etc/camilladsp/camilladsp.yml` | 관리자 생성 config template |
| `/run/camilladsp-active.yml` | 시작 래퍼가 U7 card ID를 치환한 실행 config |
| `/etc/camilladsp/profiles/` | Factory/Speaker/Headphones Front/Rear 정식 FIR |
| `/var/lib/audiodsp/profile-settings.json` | profile/chunksize/volume/bypass/rear/trim |
| `/var/lib/audiodsp/output-profile` | legacy selection 호환 |
| `/var/lib/audiodsp/u7-selector-state.json` | 실제 HID selector state |
| `/var/lib/audiodsp/fir-preview.json` | 같은 boot에만 유효한 A/B preview |
| `/var/lib/audiodsp/profile-backups/` | FIR 교체 이전 파일 |
| `/var/lib/audiodsp/upload-staging/` | Web WAV 후보와 manifest |
| `/var/lib/audiodsp/measurements/` | session과 `current.json` |
| `/var/lib/audiodsp/correction-preferences.json` | 마지막 build 선택값 |
| `/var/lib/audiodsp/restore-staging/` | 검증된 복원 후보 |
| `/var/lib/audiodsp/system-backups/` | restore 직전 rollback ZIP |
| `/run/audiodsp-*.lock` | process 간 mutation/audio lock |

## Platform-only files

Pi 2:

- `audiodsp-pi2-ethernet-apply`
- `audiodsp-pi2-ethernet-apply.service`
- `write_pi2_sd_as_admin.ps1`
- `verify_audiodsp_pi2.ps1`

Pi 4/5:

- `audiodsp-network-apply.template`
- writer가 생성하는 `audiodsp-network-apply`
- `write_final_sd_as_admin.ps1`
- `verify_audiodsp_pi4_pi5.ps1`

## 제외 파일

`__pycache__`, `.pyc`, `*.log`는 실행 부산물이며 기준 소스도 checksum 대상도 아니다. `legacy-v1-reference`는 이전 장애 분석에만 사용한다.
