# 하드웨어와 오디오 경로

## 권장 배선

```text
Source/DAC → Rod Rain preamp analog stereo out
             └→ Xonar U7 Line input

Xonar U7 Front L/R → Azur 340A integrated amplifier → main speakers
Xonar U7 Rear L/R  → 3.5 mm stereo to stereo RCA → T5S subwoofer stereo input
```

Preamp 출력과 U7 입력을 연결하므로 기존 preamp 노브와 소스 볼륨 연동이 유지된다. U7의 Mic jack과 Line 입력은 물리 jack/control을 공유할 수 있어 시작 래퍼가 Line source를 선택한다.

## 채널 번호

| Camilla 출력 | U7 채널 | 용도 |
|---:|---|---|
| 0 | Front Left | 인티앰프 L |
| 1 | Front Right | 인티앰프 R |
| 2 | Rear Left | T5S stereo L |
| 3 | Rear Right | T5S stereo R |

T5S가 오른쪽 스피커의 오른편에 있어도 현재 보정은 Woofer를 하나의 source로 측정하고 같은 FIR을 Rear L/R에 넣는다. 이는 케이블·입력 호환을 위한 두 채널 복제이며 두 개의 독립 서브우퍼를 뜻하지 않는다.

## 샘플 형식

- Camilla capture: 2ch, `S32_LE`, 48 kHz
- Camilla playback: 4ch, `S32_LE`, 48 kHz
- ALSA dmix slave: 4ch, `S24_3LE`, 48 kHz
- UMIK recording: mono `S24_3LE`, 48 kHz
- FIR/announcement WAV: 48 kHz; FIR은 float32 stereo

U7의 24-bit sample은 CamillaDSP의 32-bit container 안에서 처리된다. 이것은 24-bit 측정을 32-bit float FIR로 계산하는 데 정밀도 문제가 있다는 뜻이 아니다. FFTW3f와 float32 FIR의 수치 noise는 이 시스템의 acoustic/ADC noise보다 충분히 낮으며 self-test의 round-trip 허용오차는 `2e-5`다.

## 출력 볼륨

실제 ALSA control:

```bash
amixer -D hw:U7 cget numid=6
amixer -D hw:U7 set 'PCM',0 117
```

- raw 0 = -127 dB
- raw 127 = 0 dB
- AudioDSP 안전 UI/API 범위 raw 67~127 = -60~0 dB
- 초기값 raw 117 = -10 dB

`PCM,0` 쓰기는 U7가 노출하는 FL/FR/RL/RR/FC/Woofer/SL/SR 8개 playback channel을 같은 값으로 맞춘다. AudioDSP가 실제로 사용하는 것은 첫 4개다.

전역 볼륨은 FIR response를 바꾸지 않는다. `woofer_trim_db`는 Rear relative level만 바꾸며 Camilla config 변경과 restart가 필요하다. 두 기능을 혼동하지 않는다.

## U7 Speaker/Headphones

상단 dial 클릭의 output 선택은 U7 HID report에서 감지한다. 현재 확인된 state는 Headphones `0x30`, Speaker `0xA0`이며 button press mask가 함께 들어온다. 웹은 이를 읽어 active 카드와 실제 상태를 갱신한다.

U7 hardware output selector를 바꾸는 공개 ALSA command가 확인되지 않았으므로 웹에서 LED를 바꾸는 control은 제공하지 않는다. U7 물리 버튼으로 바꾼다.

## 측정 연결

UMIK-1은 별도 USB microphone이다. 룸 측정 때 U7 Line input이 아니라 `hw:CARD=UMIK1,DEV=0`에서 녹음한다. 따라서 보정 중 U7 input은 mute해 feedback/불필요한 capture를 막아도 UMIK 녹음에는 영향이 없다.
