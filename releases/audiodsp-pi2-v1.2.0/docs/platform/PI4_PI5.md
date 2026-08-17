# Raspberry Pi 4 / Pi 5

## 플랫폼

- CPU architecture: AArch64, 64-bit arm64
- OS: Raspberry Pi OS Lite Trixie arm64, 2026-06-18
- CamillaDSP: 4.1.3 aarch64, ELF machine 183
- network: Ethernet DHCP + writer에서 입력한 Wi-Fi
- hostname/user: `audiodsp-pi` / `audiodsp`
- SD: 8~128 GB writer 범위, 16 GB 이상 권장

동일 arm64 release를 Pi 4와 Pi 5에 사용한다. AudioDSP에는 Pi 4 전용 CPU instruction이나 device-tree override가 없다. Pi 5는 official Raspberry Pi OS arm64와 aarch64 binary 관점의 호환 대상이지만 현재 문서 기준 실제 U7 장시간 시험은 완료되지 않았다.

## 오디오 기본값

- chunksize 1024
- queuelimit 4
- ALSA dmix period 1024, buffer 8192
- output volume -10 dB

1024 samples는 48 kHz에서 약 21.3 ms block duration이다. 512는 더 낮은 지연 대신 부하/XRUN 위험이 커지고, 2048/4096은 안정 여유를 늘린다.

## 설치

1. `WRITE_FINAL_SD_CARD.cmd`를 관리자 권한으로 실행한다.
2. SSID/password를 prompt에 넣는다. 화면/log에는 password가 나오지 않아야 한다.
3. exact disk 확인 후 기록한다.
4. U7과 가능하면 Ethernet도 연결하고 전원을 넣는다.
5. 첫 install/reboot에 약 2~3분을 준다.
6. `audiodsp-pi.local:8080` 또는 router DHCP 주소에 접속한다.

## First-run 차이

- writer가 `audiodsp-network-apply.template`의 SSID/PSK base64 placeholder를 정확히 한 번 치환한다.
- first-run은 credential helper를 root filesystem 0700으로 설치한 뒤 FAT의 사본을 제거한다.
- 다음 boot의 oneshot service가 NetworkManager Ethernet/Wi-Fi를 설정하고 성공 후 helper가 self-delete한다.
- static/fallback address를 만들지 않는다.
- 초기 manager chunksize를 1024로 설정한다.

## Pi 5 수락 시험

Pi 5에서 최종 release로 선언하려면 다음을 별도로 기록한다.

- 부팅/네트워크/U7 card ID 탐색
- Front/Rear channel mapping
- Speaker/Headphones HID 전환과 안내음
- `/api/volume` 8채널 read/write
- 2/4 convolution에서 10분 CPU/XRUN/온도
- UMIK level test와 한 개 full 32768-tap build
- reboot 후 volume/profile 복원

실기 전에는 `Pi 5 compatible design`이라고 표현하고 `Pi 5 hardware-verified`라고 쓰지 않는다.
