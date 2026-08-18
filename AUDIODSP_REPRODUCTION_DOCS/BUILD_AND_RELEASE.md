# 빌드와 릴리스 절차

## 도구

Windows host에 다음이 필요하다.

- 관리자 PowerShell 5.1 이상
- Raspberry Pi Imager: `C:\Program Files\Raspberry Pi Ltd\Imager\rpi-imager.exe`
- Git Bash: `C:\Program Files\Git\bin\bash.exe`
- SD card reader
- Python 3는 source `py_compile`과 보조 검사에 사용

Writer는 OS나 CamillaDSP를 인터넷에서 내려받지 않는다. 릴리스 폴더 안의 고정 image/binary를 사용한다.

## 고정 artifact

| 항목 | Pi 2 | Pi 4/5 |
|---|---|---|
| OS | `2026-06-18-raspios-trixie-armhf-lite.img.xz` | `2026-06-18-raspios-trixie-arm64-lite.img.xz` |
| compressed SHA-256 | `EA4E84C501D6DD4F4B1D04EB84DF133A03F90A05EE2E8AB849185C17C2B0707B` | `ACFF736CA7945E3B305F07CDA4ABDB870910E12634991DA69783611756E381B3` |
| uncompressed SHA-256 | `235aae6e32f40eb294b6485f99232d9ea5b6ee0251c8dc40e370177fac4754c2` | `e235fd24fc5f039c08daba7d3abc04aecc7313f979d16d2a3fdad29dd44c33a9` |
| CamillaDSP | 4.1.3 ARMv7 | 4.1.3 aarch64 |
| binary SHA-256 | `DD47CA27285661AAC2C51E4023E885C8F14A98455B58B36F2E11C9D44254582B` | `E04C7A6603E9482BAB33C1E18AFC41D3C07410B54BA9C246EDA69F7E9CBAEDFA` |
| ELF machine | 40 | 183 |
| Factory FIR SHA-256 | `8A8A3B2FC31A080A6BC40205F29EA6471DF95ADF357618B2025BDD193EF45C99` | 동일 |

Hash가 달라지면 writer의 expected hash를 단순히 새 값으로 바꾸지 않는다. 출처, architecture, 기능 시험을 확인하고 릴리스 버전을 올린다.

## 공통 소스와 플랫폼 overlay

다음 공통 파일은 `source/common`에 한 번만 둔다.

- `audiodsp-profile-manager.py`
- `audiodsp-profile-web.py`
- `audiodsp-measurement.py`
- `audiodsp-mimo.py`
- `audiodsp-profile-monitor.py`
- `audiodsp-camilladsp-start`
- `audiodsp-output-profile`
- systemd service와 ALSA config
- FIR, announcement, target, calibration asset
- `test_profile_matrix.py`, `test_measurement_engine.py`, `test_target_option_matrix.py`, `test_mimo_algorithm_matrix.py`, `test_mimo_runtime.py`, `test_resource_budget.py`

플랫폼 차이는 `source/platforms/<pi>/payload`, release의 `firstrun.sh`, writer, network helper/template, OS image, CamillaDSP binary다. `tools/materialize_releases.py`가 공통+overlay를 `build/<platform>`에 합치므로 release payload를 수동 복사하지 않는다. Pi3는 Pi2 overlay, Pi5는 Pi4 overlay를 상속한다.

## Text encoding

- Linux payload `.py`, shell, service, YAML, ALSA: UTF-8 no BOM, LF
- Windows `.ps1` writer: Windows PowerShell 5의 한글 안정성을 위해 UTF-8 BOM 허용/권장
- `.cmd`: CRLF 가능

Writer의 `Assert-LfNoBom`과 Git Bash `bash -n` 검사를 우회하지 않는다.

## Source 검사

```powershell
$repo = (Resolve-Path '.').Path
$pi2 = Join-Path $repo 'releases\audiodsp-pi2-v1.2.0'
$pi45 = Join-Path $repo 'releases\audiodsp-pi4-pi5-v1.2.0'

py -3 -m py_compile `
  .\source\common\payload\audiodsp-profile-manager.py `
  .\source\common\payload\audiodsp-profile-web.py `
  .\source\common\payload\audiodsp-measurement.py `
  .\source\common\payload\audiodsp-mimo.py `
  .\source\common\tests\test_profile_matrix.py `
  .\source\common\tests\test_measurement_engine.py `
  .\source\common\tests\test_target_option_matrix.py `
  .\source\common\tests\test_mimo_algorithm_matrix.py `
  .\source\common\tests\test_mimo_runtime.py `
  .\source\common\tests\test_resource_budget.py

foreach ($platform in 'pi2','pi3','pi4','pi5') {
  py -3 .\tools\materialize_releases.py --platform $platform --assemble
  py -3 .\tools\materialize_releases.py --platform $platform --check
}

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$pi2\write_pi2_sd_as_admin.ps1" -ValidateOnly -NoPause
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$pi45\write_final_sd_as_admin.ps1" -ValidateOnly -NoPause
```

Writer preflight는 새 핵심 기능의 source marker도 검사한다. 볼륨 revision에서는 manager의 `set-output-volume`/`output_volume_db`, starter의 saved-volume restore, Web의 `/api/volume`/`output_volume_control`이 필수다.

## SD 작성

Pi 2:

```powershell
Set-Location '.\releases\audiodsp-pi2-v1.2.0'
.\WRITE_PI2_SD_CARD.cmd
```

Pi 4/5:

```powershell
Set-Location '.\releases\audiodsp-pi4-pi5-v1.2.0'
.\WRITE_FINAL_SD_CARD.cmd
```

Pi 4/5 writer는 SSID/password를 묻고 secret을 출력하지 않는다. 두 writer 모두 대상 Disk number, model, size, serial을 표시한 뒤 exact confirmation을 요구한다. 기록 후 FAT에 payload hash, first-run hash, cmdline token, marker를 다시 확인한다.

## 최초 부팅 인수조건

- boot FAT에 `audiodsp-firstboot-success.txt`가 생성됨
- `camilladsp`, `audiodsp-web`, `audiodsp-profile-monitor` active
- `/api/status`와 `/api/volume` 응답
- U7 실제 volume -10 dB/raw117 초기화
- Speaker profile 적용, 다른 프로필 fallback 정상
- Pi 4/5에서 MIMO bank manifest 검증, 8 Conv parser 검사, OFF 시 SISO 복귀
- Pi 2에서 MIMO 활성화 요청이 상태 변경 없이 거부됨
- `DSP ready` 안내
- DHCP 주소 확인, 고정 주소 없음

## 무결성 검사

공통 source와 생성 bundle의 byte 일치는 `materialize_releases.py --check`가 전 파일을 검사한다. Writer는 고정 OS image, architecture CamillaDSP binary, Factory FIR SHA-256과 필수 source marker를 별도로 검사한다. 중복된 정적 `SHA256SUMS.txt`는 source 변경마다 낡는 문제 때문에 제거했다.

## 릴리스 문서

각 릴리스 root에는 플랫폼 전용 항목만 둔다.

- `README.md`
- `RELEASE_NOTES_<version>.md`
- `FINAL_TEST_REPORT.md`
- `AUDIODSP_REQUIREMENTS_VERIFIED.md`
- OS image와 architecture CamillaDSP binary
- writer/verify/firstrun/network 입력

공통 문서·payload·tests를 release root에 복제하지 않는다. 재현 문서는 `AUDIODSP_REPRODUCTION_DOCS`, 실행/시험 원본은 `source/common`, 생성 결과는 `build/<platform>`이 정본이다.

완료 보고에는 실제 시험 플랫폼, Pi 5 실기 여부, Camilla PID/FIR hash 유지 여부, volume API 결과를 명시한다.
