# AudioDSP 재현 문서

이 폴더는 AudioDSP v1.2 릴리스를 다시 만들고, 수정하고, 실제 Raspberry Pi에서 안전하게 검증하기 위한 기준 문서 모음이다. 최종 배포 기준은 다음 두 디렉터리다.

- Pi 2: `D:\GSonic\RaspberryPi_SD\releases\audiodsp-pi2-v1.2.0`
- Pi 4 / Pi 5: `D:\GSonic\RaspberryPi_SD\releases\audiodsp-pi4-pi5-v1.2.0`

`pi2-strong-bass-4ch-v1.1.0`과 각 릴리스의 `legacy-v1-reference`는 이력 확인용이다. 새 릴리스를 만들 때 이 파일들을 기준 소스로 되돌리지 않는다.

## 문서 지도

- [PLAN.md](PLAN.md): 완료 범위, 유지 조건, 다음 릴리스 계획
- [ARCHITECTURE.md](ARCHITECTURE.md): 오디오·제어·상태·웹 구성
- [AGENTS.md](AGENTS.md): 이후 작업자가 반드시 지킬 변경·검증 규칙
- [REQUIREMENTS.md](REQUIREMENTS.md): 기능 및 비기능 요구사항 기준선
- [BUILD_AND_RELEASE.md](BUILD_AND_RELEASE.md): SD 이미지 생성과 릴리스 절차
- [TESTING.md](TESTING.md): 정적·회귀·실기 시험 방법
- [API.md](API.md): HTTP API와 폼 경로
- [UI_UX_SPEC.md](UI_UX_SPEC.md): PC·모바일 화면과 상호작용 규칙
- [MEASUREMENT_AND_DSP.md](MEASUREMENT_AND_DSP.md): 측정, FIR 설계, 안전 제한
- [MIMO_ROOM_TUNING.md](MIMO_ROOM_TUNING.md): 다중 음원 최적화, 최신 연구 채택 범위와 필터 한계
- [MIMO_VALIDATION_REPORT_20260818.md](MIMO_VALIDATION_REPORT_20260818.md): 무음 합성·실제 Camilla parser 검증과 남은 실기 항목
- [BACKUP_AND_MIGRATION.md](BACKUP_AND_MIGRATION.md): 백업 스키마와 이름 변경 호환성
- [HARDWARE_AND_AUDIO_PATH.md](HARDWARE_AND_AUDIO_PATH.md): 배선, 채널, 형식, U7 볼륨
- [OPERATIONS_AND_TROUBLESHOOTING.md](OPERATIONS_AND_TROUBLESHOOTING.md): 운영과 장애 진단
- [SECURITY_AND_SAFETY.md](SECURITY_AND_SAFETY.md): 네트워크·비밀·디스크·음량 안전
- [FILE_MAP.md](FILE_MAP.md): 소스와 설치 위치 대응표
- [CHANGELOG.md](CHANGELOG.md): 재현 문서와 유지보수 변경 이력
- [platform/PI2.md](platform/PI2.md): Pi 2 전용 조건
- [platform/PI4_PI5.md](platform/PI4_PI5.md): Pi 4/5 전용 조건

## 현재 기준선

- Raspberry Pi OS Lite Trixie, 2026-06-18 이미지
- CamillaDSP 4.1.3
- Xonar U7: 입력 2채널, 출력 4채널, 48 kHz
- CamillaDSP I/O: `S32_LE`; U7 공유 출력 dmix: `S24_3LE`
- FIR: stereo IEEE float32 WAV, 48 kHz, 32768 taps, 디지털 preamp 없음
- L/R/Woofer 개별 룸보정은 기본 ON/100 Hz LR4 디지털 crossover를 Front/Rear FIR WAV 안에 내장한다. 별도 runtime filter와 block latency 증가는 0이며, 최종 acoustic 합산은 phase 신뢰도와 세 위치 복소합 검증을 별도로 통과해야 한다.
- Factory/Speaker FIR SHA-256: `8a8a3b2fc31a080a6bc40205f29ea6471df95adf357618b2025bdd193ef45c99`
- 출력 볼륨: U7 `PCM,0`, 웹/API 범위 -60~0 dB, 초기 저장값 -10 dB
- Web Status는 입력→DSP→라우팅→U7 selector→실제 출력의 반응형 SVG signal console을 표시한다.
- 레벨 검사에서 현재 U7 물리 출력을 session에 고정하며, 경로 변경 시 측정/Preview를 중단하고 다른 profile Apply를 거부한다. selector 공통 상태 파일은 `/var/lib/audiodsp/u7-selector-state.json`이다.
- Pi 2 초기 chunksize 2048; Pi 4/5 초기 chunksize 1024
- Pi 2는 SISO 2/4 convolution만 지원; Pi 4/5 MIMO는 2입력×4출력, 8 convolution, 최소 effective chunksize 1024
- 네트워크는 DHCP만 사용하며 고정·비상 주소를 만들지 않는다.

## 가장 빠른 재현 검사

관리자 권한 PowerShell에서 SD를 쓰기 전 비파괴 검사만 실행한다.

```powershell
Set-Location 'D:\GSonic\RaspberryPi_SD\releases\audiodsp-pi2-v1.2.0'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\write_pi2_sd_as_admin.ps1 -ValidateOnly -NoPause

Set-Location 'D:\GSonic\RaspberryPi_SD\releases\audiodsp-pi4-pi5-v1.2.0'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\write_final_sd_as_admin.ps1 -ValidateOnly -NoPause
```

검사가 통과해도 SD 기록은 별도 작업이다. 기록 스크립트는 선택한 실제 디스크를 덮어쓰므로 [SECURITY_AND_SAFETY.md](SECURITY_AND_SAFETY.md)의 디스크 확인 절차를 먼저 따른다.

## 식별자

새 설치는 앱, 서비스, 상태 경로, 사용자와 호스트 이름에 `audiodsp`를 사용한다. 현재 운용 중인 기존 Pi 2는 중단 없는 이전을 위해 `gsonic-pi2`/`gsonic` 계정을 유지할 수 있지만 설치된 앱 경로와 서비스는 AudioDSP 이름을 쓴다. 코드의 `AUDIODSP_*` 환경변수가 우선이며 `GSONIC_*`는 이전 릴리스 테스트와 점진적 마이그레이션만을 위한 호환 계층이다.
