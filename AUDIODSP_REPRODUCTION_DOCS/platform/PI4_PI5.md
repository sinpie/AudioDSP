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

## MIMO 운용

- MIMO는 Pi 4/5 전용 기능이다. 입력 L/R 각각을 Front L, Front R, Rear L, Rear R로 보내는 8 convolution과 2→8→4 matrix를 사용한다.
- 한 T5S의 stereo 입력은 같은 물리 음원을 구동하므로 `MIMO 2.1`에서는 Rear L/R에 각각 0.5를 배분한다. 독립 배치·배선된 서브우퍼 두 대만 `MIMO 2.2`의 네 독립 actuator로 취급한다.
- MIMO가 활성화되면 effective chunksize 하한은 1024다. 512를 저장해도 실행 config는 1024로 올려 XRUN 위험을 줄인다.
- 합성·parser 검증은 통과했지만 실제 방 성능과 Pi 4/5 지속 부하는 아직 실기 인증 전이다. 10분 이상의 CPU/XRUN/온도/USB 검사와 사용하지 않은 위치의 전후 재측정이 필수다.
- Pi 5는 설계상 호환 대상이며, 실제 U7 장시간 시험 전까지 하드웨어 검증 완료로 표시하지 않는다.

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
- 8 convolution MIMO 2.1/2.2에서 10분 CPU/XRUN/온도와 effective chunksize 1024 확인
- UMIK level test와 한 개 full 32768-tap build
- reboot 후 volume/profile 복원

실기 전에는 `Pi 5 compatible design`이라고 표현하고 `Pi 5 hardware-verified`라고 쓰지 않는다.
