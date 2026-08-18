# 파일 지도

## Canonical source

| 파일 | 역할 |
|---|---|
| `source/common/payload/*` | Pi 2/3/4/5가 공유하는 유일한 runtime·asset 원본 |
| `source/common/tests/*` | 공유하는 유일한 무음 회귀 시험 원본 |
| `source/platforms/pi2/payload/*` | ARMv7/Pi2 전용 config·Ethernet helper overlay |
| `source/platforms/pi4/payload/*` | ARM64/Pi4 전용 config overlay |
| `source/platforms/pi3/platform.json` | Pi2 overlay를 상속하는 Pi3 manifest |
| `source/platforms/pi5/platform.json` | Pi4 overlay를 상속하는 Pi5 manifest |
| `tools/materialize_releases.py` | canonical source+overlay+architecture binary를 `build/<platform>`에 조립·검사 |
| `build/<platform>` | 무시되는 생성 bundle; 직접 편집 금지 |

## Release root

| 파일 | 역할 |
|---|---|
| `*.img.xz` | 고정 Raspberry Pi OS Lite image |
| `firstrun.sh` | image 첫 부팅 설치기 |
| `write_*_sd_as_admin.ps1` | 검증·대상 확인·image 기록·FAT payload 복사 |
| `WRITE_*.cmd` | 관리자 writer 실행 진입점 |
| `verify_*.ps1`, `VERIFY_*.cmd` | 작성 후 또는 live 검증 진입점 |
| `audiodsp_pi_ed25519(.pub)` | 새 설치 SSH key pair; private 취급 |
| `payload/camilladsp` | architecture 전용 외부 binary 하나만 보관; 나머지 payload는 build에서 생성 |

## Payload → 설치 위치

| Payload | Raspberry Pi 위치 |
|---|---|
| `camilladsp` | `/usr/local/bin/camilladsp` |
| `audiodsp-camilladsp-start` | `/usr/local/bin/audiodsp-camilladsp-start` |
| `audiodsp-profile-manager.py` | `/usr/local/bin/audiodsp-profile-manager.py` |
| `audiodsp-profile-web.py` | `/usr/local/bin/audiodsp-profile-web.py` |
| `audiodsp-measurement.py` | `/usr/local/bin/audiodsp-measurement.py` |
| `audiodsp-mimo.py` | `/usr/local/bin/audiodsp-mimo.py` |
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
| `/etc/camilladsp/profiles/mimo/` | 선택적 MIMO manifest와 네 2-input stereo FIR WAV |
| `/var/lib/audiodsp/profile-settings.json` | profile/chunksize/volume/bypass/MIMO/rear/trim |
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

- `audiodsp-ethernet-apply`
- `audiodsp-ethernet-apply.service`
- `write_pi2_sd_as_admin.ps1`
- `verify_audiodsp_pi2.ps1`

Pi 4/5:

- `audiodsp-network-apply.template`
- writer가 생성하는 `audiodsp-network-apply`
- `write_final_sd_as_admin.ps1`
- `verify_audiodsp_pi4_pi5.ps1`

## 제외 파일

`__pycache__`, `.pyc`, `*.log`는 실행 부산물이며 기준 소스가 아니다. 과거 중복 payload/docs/legacy tree는 release에서 제거하고 Git 이력으로만 보존한다.
