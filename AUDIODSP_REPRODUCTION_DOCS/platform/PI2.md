# Raspberry Pi 2 Model B Rev 1.1

## 플랫폼

- CPU architecture: ARMv7, 32-bit armhf
- RAM: 1 GB
- OS: Raspberry Pi OS Lite Trixie armhf, 2026-06-18
- CamillaDSP: 4.1.3 ARMv7, ELF machine 40
- onboard Wi-Fi: 없음
- network: Ethernet `eth0`, NetworkManager DHCP only
- hostname/user: `audiodsp-pi2` / `audiodsp`
- SD: 8~64 GB writer 허용, 16 GB 이상 권장

## 오디오 기본값

- chunksize 2048
- queuelimit 4
- ALSA dmix period 1024, buffer 8192
- output volume -10 dB

2048 samples는 48 kHz에서 약 42.7 ms block duration이다. 1024는 사용할 수 있지만 4 convolution, Web/measurement 병행, 장시간 열 상태에서 XRUN을 검사한다.

## 설치

1. Ethernet과 U7을 먼저 연결한다.
2. `WRITE_PI2_SD_CARD.cmd`를 관리자 권한으로 실행한다.
3. 정확한 disk를 확인하고 기록한다.
4. 첫 부팅과 자동 재부팅에 4~6분을 준다.
5. 공유기 DHCP 또는 `audiodsp-pi2.local`로 접속한다.

Pi 2는 onboard Wi-Fi가 없으므로 USB Wi-Fi는 별도 driver/NetworkManager 작업이다. TP-Link TX20U 같은 장치를 꽂는 것만으로 release가 자동 구성된다고 가정하지 않는다.

## First-run 차이

- `audiodsp-pi2-ethernet-apply.service`를 다음 정상 boot에 실행해 explicit Ethernet DHCP profile을 만든다.
- `network-config`에는 eth0 DHCP와 optional만 쓴다.
- Wi-Fi secret을 요구하거나 생성하지 않는다.
- 초기 manager chunksize를 2048로 설정한다.

## 확인된 실기 특성

- 현재 운용 Pi 2에서 4채널 음악 출력 정상
- CamillaDSP 대략 CPU 36%; Web 약 0.7~1.1% 관측
- FFTW3f 32768-tap build: magnitude 약 53초, bass phase 약 60초 수준 관측
- 4096 state/profile matrix와 measurement engine 시험 통과 이력

수치는 현재 FIR/topology에서의 관측값이며 릴리스 보증 상한은 아니다.

## MIMO 지원 경계

- Pi 2에서는 실시간 MIMO를 활성화할 수 없다. 8 convolution과 2→8→4 matrix를 안정적으로 운용할 CPU 여유가 검증되지 않았기 때문이다.
- MIMO 측정·설계·manifest 코드는 백업 호환성과 Pi 4/5 이전을 위해 설치되지만 Web과 manager가 실행을 명시적으로 거부한다.
- Pi 2의 정식 운용 경로는 Front/Rear를 공유하는 2 convolution 또는 독립 Front/Rear의 4 convolution SISO다.
- Pi 4/5에서 만든 MIMO bank를 포함한 schema 2 백업은 보관할 수 있지만, Pi 2에서 복원된 MIMO 설정은 `configured but inactive`로 표시된다.
- 룸 측정과 일반 32768-tap SISO 설계는 가능하다. 합성 시험에서 약 49~56초가 걸렸으므로 작업 중 음악 재생과 Web 부하를 최소화하고, 결과 적용 전 반드시 별도 검증 위치를 재측정한다.

## 기존 운용 장치

기존 장치도 hostname `audiodsp-pi2`, user `audiodsp`, 서비스·앱 `audiodsp-*`, 상태 `/var/lib/audiodsp`로 통일한다. SSH와 sudo를 새 계정으로 확인하기 전에는 기존 계정을 제거하지 않는 단계적 이전 절차를 따른다.

2026-08-19 production 장치는 이 절차를 완료했다. 새 `audiodsp` 계정은 기존 장치 이전 때문에 UID 1001이며, 신규 SD의 UID 1000과 달라도 파일 소유권과 실행에는 문제가 없다. Ethernet 연결 이름은 `audiodsp-ethernet`이고 UUID/IP 할당 방식은 유지했다. 이전 과정에서 CamillaDSP PID `7731`, Speaker FIR SHA, 저장/실제 U7 볼륨 `0 dB`를 보존했다.
