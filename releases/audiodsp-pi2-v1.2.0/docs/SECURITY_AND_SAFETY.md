# 보안과 안전

## SD 기록

SD writer는 대상 디스크의 기존 내용을 덮어쓴다.

- 자동 선택은 USB/SD bus, 용량 범위, boot/system 여부를 확인한다.
- 사용자가 `TargetDiskNumber`를 지정해도 model, serial, size를 화면에서 다시 확인한다.
- 확인 문자열 `WRITE DISK N` 없이 기록하지 않는다.
- OS SSD나 workspace disk에는 절대 쓰지 않는다.
- Imager가 removable로 분류하지 못할 수 있어 writer는 검증한 exact PhysicalDrive에만 `--enable-writing-system-drives`를 사용한다. 이 옵션을 독립 명령으로 사용하지 않는다.
- SD 기록 전 백업이 끝났는지 확인하고 drive letter가 아니라 PhysicalDrive model/size로 판단한다.

## 네트워크

- Web UI/API는 인증과 TLS가 없다. 인터넷 port forwarding, guest/open Wi-Fi, 공용망에 노출하지 않는다.
- 신뢰된 가정 LAN에서만 TCP 8080을 연다.
- DHCP만 사용한다. 임의 fallback address는 다른 DHCP 장치와 충돌할 수 있다.
- Pi 4/5 Wi-Fi password는 writer 입력 후 base64 placeholder로 일회성 script에 들어가며 화면/log에 출력하지 않는다. 첫 부팅 때 root filesystem으로 옮기고 NetworkManager 설정 후 self-delete한다.
- 배포 ZIP/백업에는 Wi-Fi credential이 포함되지 않는다.

## SSH key

릴리스의 private key는 해당 LAN 기기 관리 권한이다. 외부 저장소·메신저·공개 첨부로 내보내지 않는다. 새 공개 배포를 만들면 key pair를 교체하고 `firstrun.sh`의 authorized key와 writer required file을 함께 갱신한다.

새 계정은 password가 잠기고 SSH key를 사용한다. 현재 `audiodsp` 계정은 passwordless sudo이므로 key 유출은 root 유출과 같다.

## 음량과 청력/기기 안전

- API 범위는 -60~0 dB지만 0 dB는 매우 클 수 있다. Preset UI는 -10 dB까지만 제공한다.
- 자동/실기 시험은 현재값과 같은 볼륨을 쓰며 갑자기 0 dB로 올리지 않는다.
- U7 물리 노브와 preamp/amp volume이 함께 작용한다. 측정 전 -48/-42 dBFS level check를 사용한다.
- 야간·아파트에서는 Strong bass-control과 낮은 전체 볼륨으로 시작한다. 저주파 차단만으로 층간소음을 보장할 수 없으며 구조 전달은 위치·건물에 따라 다르다.
- Measurement는 사용자 동작 없이 자동 실행하지 않는다.

## 파일 입력

- WAV upload는 32 MiB 이하, stereo, 48 kHz, PCM16/24/32 또는 float32/64, 최대 262144 frames, finite sample만 허용한다.
- Backup ZIP은 allowlist path, duplicate, 개별/전체 크기, inventory SHA를 검증한다.
- Path basename과 resolve/commonpath 검사로 다운로드·restore path escape를 막는다.
- 적용 전 staging, 적용 시 이전 파일 backup과 원자 replace를 사용한다.

## 서비스 권한

Web과 manager가 root이므로 입력 검증은 보안 경계다. 새 endpoint에서 shell 문자열 조합을 하지 말고 고정 executable argv list와 enum/range를 사용한다. file path는 관리 root 아래로 resolve한 뒤 확인한다. 오류 응답에 secret이나 전체 환경변수를 노출하지 않는다.
