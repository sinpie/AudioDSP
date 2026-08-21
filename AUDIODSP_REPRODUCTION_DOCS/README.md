# AudioDSP 재현 문서

이 폴더는 AudioDSP v1.2 릴리스를 다시 만들고, 수정하고, 실제 Raspberry Pi에서 안전하게 검증하기 위한 단일 기준 문서 모음이다. 공통 실행 소스는 `source/common`, 플랫폼 overlay는 `source/platforms`, SD writer와 외부 binary/image 입력은 다음 두 디렉터리에 있다.

- Pi 2: `releases/audiodsp-pi2-v1.2.0`
- Pi 4 / Pi 5: `releases/audiodsp-pi4-pi5-v1.2.0`

완성 payload와 시험 묶음은 `python tools/materialize_releases.py --platform <pi2|pi3|pi4|pi5> --assemble`로 `build/<platform>`에 만든다. `build`는 생성물이며 직접 편집하지 않는다. 과거 중복 release tree는 제거했고 필요한 이력은 Git에만 보존한다.

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
- [ROOM_TUNING_MATH.md](ROOM_TUNING_MATH.md): SISO 응답 추정·공간 평균·역필터·위상·크로스오버·검증의 수식과 감사 기준
- [ROOM_TUNING_MATH_AUDIT_20260821.md](ROOM_TUNING_MATH_AUDIT_20260821.md): 저장 Pi5 세션의 v21/v22 계산 비교, 실제 사후 sweep, v23 출력 안정화 감사 결과
- [MIMO_ROOM_TUNING.md](MIMO_ROOM_TUNING.md): 다중 음원 최적화, 최신 연구 채택 범위와 필터 한계
- [MIMO_MATHEMATICAL_AUDIT_20260821.md](MIMO_MATHEMATICAL_AUDIT_20260821.md): MIMO 목적함수·조건수·측정 불확실성·합산 역할·UI 흐름 감사
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
- 새 session의 권장 룸보정은 위치마다 L/R/W/L+Woofer/R+Woofer를 먼저 측정한다. L/R/W만 FIR 설계에 사용하고 실제 합산 두 응답은 정규화하지 않은 복소 closure에만 사용하므로, 검증 PASS 뒤에는 FIR 계산 후 별도 합산 sweep을 요구하지 않는다.
- 분리 룸보정은 기본 ON/100 Hz LR4 디지털 crossover를 Front/Rear FIR WAV 안에 내장한다. 별도 runtime filter와 block latency 증가는 0이며, phase 신뢰 시 세 위치 실제 복소합 최대값을, 불신뢰 시 보수적 상한을 사용한다.
- Factory/Speaker FIR SHA-256: `8a8a3b2fc31a080a6bc40205f29ea6471df95adf357618b2025bdd193ef45c99`
- 출력 볼륨: U7 `PCM,0`, 웹/API 범위 -60~0 dB, 초기 저장값 -10 dB
- Web Status는 입력→DSP→라우팅→U7 selector→실제 출력의 반응형 SVG signal console을 표시한다.
- 레벨 검사에서 현재 U7 물리 출력을 session에 고정하며, 경로 변경 시 측정/Preview를 중단하고 다른 profile Apply를 거부한다. selector 공통 상태 파일은 `/var/lib/audiodsp/u7-selector-state.json`이다. 버튼 report `0x30/0xA0`와 부팅 안정 report `0x88/0xE0`를 분리하며 read-only GET_REPORT만 사용한다.
- Pi 2 초기 chunksize 2048; Pi 4/5 초기 chunksize 1024
- Pi 2는 SISO 2/4 convolution만 지원; Pi 4/5 MIMO는 2입력×4출력, 8 convolution, 최소 effective chunksize 1024
- 네트워크는 DHCP만 사용하며 고정·비상 주소를 만들지 않는다.

## 가장 빠른 재현 검사

관리자 권한 PowerShell에서 SD를 쓰기 전 비파괴 검사만 실행한다.

```powershell
Set-Location '.\releases\audiodsp-pi2-v1.2.0'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\write_pi2_sd_as_admin.ps1 -ValidateOnly -NoPause

Set-Location '.\releases\audiodsp-pi4-pi5-v1.2.0'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\write_final_sd_as_admin.ps1 -ValidateOnly -NoPause
```

검사가 통과해도 SD 기록은 별도 작업이다. 기록 스크립트는 선택한 실제 디스크를 덮어쓰므로 [SECURITY_AND_SAFETY.md](SECURITY_AND_SAFETY.md)의 디스크 확인 절차를 먼저 따른다.

## 식별자

앱, 서비스, 상태 경로, 사용자와 호스트 이름은 `audiodsp`를 사용한다. 코드·systemd·시험 launcher의 환경변수도 `AUDIODSP_*`만 허용한다. 기존 장치는 새 계정의 SSH/sudo를 먼저 검증하는 단계적 절차로 이전한다.
