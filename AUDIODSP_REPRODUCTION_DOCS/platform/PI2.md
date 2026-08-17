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

## 기존 운용 장치

기존 장치는 무중단 이전 때문에 hostname `gsonic-pi2`, user `gsonic`을 유지할 수 있다. 서비스와 앱 파일은 `audiodsp-*`, 상태는 `/var/lib/audiodsp`다. 새 SD는 반드시 위의 새 이름을 사용한다.
