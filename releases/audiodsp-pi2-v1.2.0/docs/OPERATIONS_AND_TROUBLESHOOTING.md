# 운영과 문제 해결

## 정상 확인

```bash
systemctl is-active camilladsp audiodsp-web audiodsp-profile-monitor
pgrep -a camilladsp
curl -fsS http://127.0.0.1:8080/api/status
curl -fsS http://127.0.0.1:8080/api/volume
aplay -l
arecord -l
```

새 설치 주소:

- Pi 2: `http://audiodsp-pi2.local:8080`
- Pi 4/5: `http://audiodsp-pi.local:8080`

mDNS가 안 되면 공유기에서 DHCP 주소를 확인한다. Deco가 client IP를 늦게 표시하면 AP/Deco 재부팅 후 DHCP lease가 나타나는지 본다. 임의 `192.168.x.250` 주소를 넣지 않는다.

## 소리가 안 날 때

1. Preamp의 실제 input source가 맞는지 확인한다.
2. U7 전원/USB와 Front/Rear 케이블을 확인한다.
3. `/api/volume`의 `actual_db`가 지나치게 낮지 않은지 확인한다.
4. `amixer -D hw:U7 cget numid=6`에서 8채널 값이 같은지 본다.
5. Camilla service와 log를 확인한다.

```bash
sudo systemctl status camilladsp --no-pager
sudo journalctl -u camilladsp -n 80 --no-pager
sudo /usr/local/bin/camilladsp --check /etc/camilladsp/camilladsp.yml
```

6. U7 ALSA card ID가 `U7`이 아니면 `/proc/asound/cards`, `/proc/asound/card*/id`, `/proc/asound/card*/usbid`를 확인한다. 시작 래퍼는 USB ID `1043:857c`와 Xonar U7 설명으로 탐색한다.

## 웹이 안 열릴 때

```bash
sudo systemctl status audiodsp-web --no-pager
sudo journalctl -u audiodsp-web -n 80 --no-pager
ss -ltnp | grep ':8080'
curl -v http://127.0.0.1:8080/api/health
```

서비스 restart 직후 1~2초는 listener가 아직 없을 수 있다. Web만 고쳤으면 `sudo systemctl restart audiodsp-web`만 실행하고 CamillaDSP는 그대로 둔다.

## 볼륨 문제

- API 쓰기: `PUT /api/volume`에 정수 -60~0만 보낸다.
- `available=false`: U7 card/control을 찾지 못했거나 amixer가 실패한 것이다.
- 실제값과 저장값 다름: U7 물리 노브가 바뀐 정상 상태다. 그대로 저장하려면 slider/적용 버튼으로 같은 dB를 쓴다.
- 부팅 후 값이 돌아감: 의도된 동작이다. 마지막 Web/API 저장값을 복원한다.
- 좌우/Front/Rear가 다름: `raw_channels`의 `uniform=false`면 API로 원하는 값을 한 번 다시 써 8개 채널을 맞춘다.

## U7 출력 프로필이 안 바뀔 때

웹 카드 클릭은 selector가 아니다. U7 상단 dial을 눌러 LED를 바꾼다. HID monitor 상태:

```bash
sudo systemctl status audiodsp-profile-monitor --no-pager
sudo journalctl -u audiodsp-profile-monitor -n 80 --no-pager
cat /var/lib/audiodsp/u7-selector-state.json
```

물리 상태가 바뀌면 웹은 1초 polling으로 요청/유효 profile을 갱신하고 필요한 경우 새 화면을 reload한다.

## UMIK/측정 오류

```bash
arecord -l
lsusb
sudo /usr/local/bin/audiodsp-measurement.py self-test
ls -l /var/lib/audiodsp/calibration
cat /var/lib/audiodsp/measurements/current.json
```

NOT OK는 오류가 아니라 측정 품질 보호다. 기본 White/Sweep -42 dBFS에서 시작해 background noise를 줄이고 기기 볼륨 또는 측정 출력을 조금씩 올리되 peak clipping과 UI의 높은 출력 경고를 피한다. 실제 sweep를 shell에서 임의 실행하지 말고 UI session 흐름을 사용한다.

측정 중 중단되면 UI cancel을 먼저 사용한다. engine은 child PID를 정리하고 CamillaDSP/U7 input 상태를 복원한다. worker가 비정상 종료되면 상태 API가 PID와 command line을 확인해 `interrupted_worker` 오류로 표시하므로 `current.json`을 수동 편집하지 말고 session 기록을 보존한 채 재시도 또는 새 session을 선택한다. 강제 종료 후에는 `systemctl start camilladsp`와 U7 Line source를 확인한다.

## Pi 2 부하와 latency

- 권장 chunksize 2048: 48 kHz에서 block 자체는 약 42.7 ms다. queue/USB/필터를 합친 round-trip은 더 길 수 있다.
- 1024는 약 21.3 ms지만 CPU 여유와 underrun을 장시간 확인해야 한다.
- 32768-tap FIR은 filter 길이 자체가 latency와 동일하지 않다. CamillaDSP partitioned convolution과 impulse peak 위치가 체감 지연을 좌우한다.

```bash
pidstat -p "$(pgrep -x camilladsp)" 5 120
vcgencmd measure_temp
free -h
```

기존 Pi 2 실기에서 CamillaDSP는 대략 CPU 36%, Web은 약 0.7~1.1% 수준이었으나 source, topology, chunksize에 따라 달라진다.

## 최초 부팅

- Pi 2: Ethernet/U7을 꽂고 4~6분, first-run install 후 한 번 재부팅
- Pi 4/5: Wi-Fi 또는 Ethernet/U7, 약 2~3분, 자동 재부팅
- ACT LED가 멈춰도 OS가 DHCP를 기다리거나 service retry 중일 수 있다.

Windows에서 boot FAT의 `audiodsp-firstboot.log`와 `audiodsp-firstboot-success.txt`를 확인할 수 있다. success marker가 없으면 log 마지막 오류부터 해결한다.
