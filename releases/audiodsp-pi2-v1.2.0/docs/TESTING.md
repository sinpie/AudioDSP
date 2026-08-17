# 시험 전략과 실행 방법

## 1. 정적 검사

Python:

```powershell
py -3 -m py_compile .\payload\audiodsp-profile-manager.py .\payload\audiodsp-profile-web.py .\payload\audiodsp-measurement.py .\payload\audiodsp-profile-monitor.py .\test_profile_matrix.py .\test_measurement_engine.py
```

Shell:

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -n .\firstrun.sh
& 'C:\Program Files\Git\bin\bash.exe' -n .\payload\audiodsp-camilladsp-start
& 'C:\Program Files\Git\bin\bash.exe' -n .\payload\audiodsp-output-profile
```

릴리스 전체:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\write_pi2_sd_as_admin.ps1 -ValidateOnly -NoPause
# 또는 Pi 4/5의 write_final_sd_as_admin.ps1
```

## 2. Profile matrix

이 시험은 임시 디렉터리와 fake ALSA/systemd helper를 사용해 실제 profile/settings를 변경하지 않는다.

검증 범위:

- 요청 profile 2 × chunksize 4 × bypass 4 × Front 존재 4 × Rear 존재 4 × Rear mode 4 × Factory 2 = 4096 상태
- 정상 3968, 의도된 no-profile error 128
- 56개 설정 operation의 ordered pair 3136
- 생성된 모든 고유 YAML을 실제 CamillaDSP `--check`
- WAV format·rate·channel·NaN·size·tap 경계
- profile fallback, bypass, copy/separate, woofer trim, chunksize
- U7 HID 0x30/0xA0와 안내음
- 세 화면 UI, SVG marker, staged upload, A/B, apply, backup/restore/latest rollback
- volume 실제 read, API/form write, -60/0, invalid JSON/range/type, 물리 knob divergence
- 33개 동시 form write 후 JSON/config 유효성

Pi에서 실행 예:

```bash
cp /usr/local/bin/audiodsp-output-profile /tmp/audiodsp-output-profile-test
chmod 755 /tmp/audiodsp-output-profile-test
python3 /tmp/test_profile_matrix.py \
  --manager /usr/local/bin/audiodsp-profile-manager.py \
  --web /usr/local/bin/audiodsp-profile-web.py \
  --monitor /usr/local/bin/audiodsp-profile-monitor.py \
  --switcher /tmp/audiodsp-output-profile-test \
  --camilladsp /usr/local/bin/camilladsp \
  --measurement /usr/local/bin/audiodsp-measurement.py \
  --cal-dir /var/lib/audiodsp/calibration \
  --target-dir /usr/local/share/audiodsp/targets
```

`result=PASS`, operation/pair/count가 위 기준과 일치해야 한다.

## 3. Measurement engine 시험

Self-test는 소리를 내지 않는다.

```bash
sudo /usr/local/bin/audiodsp-measurement.py self-test
```

기대값:

- `fft_backend=fftw3f`
- `rate=48000`
- `taps=32768`
- FFT round-trip error ≤ `2e-5`
- none/primus360/strong preset이 boost를 포함하지 않음

`test_measurement_engine.py`는 합성 응답으로 target/regularization/phase/WAV/build 경로를 시험한다. 실제 UMIK sweep와 구분한다.

전체 target/preset 설계 행렬도 무음이다. Pi 2에서는 약 6분 30초가 걸릴 수 있다.

```bash
sudo /usr/local/bin/audiodsp-measurement.py self-test-targets
```

6 target × 3 preset × Front/Woofer의 실제 32768탭 FFT와 bass-phase 대표
조합을 검증한다. Offline engine test는 합성 0.60초 감쇠의 Schroeder T20,
우퍼 측정/reference -12 dB 비율, 개별 SNR, 잔향 cut-only 동작도 확인한다.

## 4. 실제 Pi 무중단 배포 확인

배포 전:

```bash
pgrep -x camilladsp
sha256sum /etc/camilladsp/profiles/Speaker_Front_LR.wav
curl -fsS http://127.0.0.1:8080/api/volume
```

Web/manager/starter를 설치한 뒤 Web만 restart한다. 볼륨 PUT은 현재값과 같은 값으로 실행한다.

```powershell
$body = @{ db = -10 } | ConvertTo-Json -Compress
Invoke-RestMethod -Uri 'http://<PI-IP>:8080/api/volume' -Method Put -ContentType 'application/json' -Body $body
```

배포 후:

- Camilla PID 동일
- Speaker FIR SHA 동일
- 서비스 3개 active
- API `actual_db=saved_db=-10`, raw117, channels8, uniform=true, hardware_applied=true
- 음악 출력 유지

## 5. 볼륨 경계 시험

자동 시험 외 수동 확인:

1. 현황 페이지의 실제/저장값이 같다.
2. 현재값에서 -1 dB 후 +1 dB로 복귀하고 모든 출력이 함께 변한다.
3. U7 물리 노브를 한 click 바꾸면 3초 안에 실제값만 변한다.
4. `저장·적용`하면 실제/저장값이 다시 같아진다.
5. Web console/network에서 PUT이 하나만 전송되는지 본다.

큰 음량으로 올리는 시험은 하지 않는다.

## 6. 실제 측정 수락 시험

실제 음향 시험은 사용자가 허용한 시간에만 한다.

- 90° calibration serial/point 확인
- -48 또는 -42 dBFS level check, clipping 없음, SNR OK
- 세 위치 L/R 또는 L/R/Woofer 완료
- build progress/ETA와 Pi 응답성 유지
- 결과 WAV 48k/float32/stereo/32768
- maximum transfer ≤ 0 dB
- 개별 sweep SNR ≥ 6 dB(15 dB 이상 권장), octave T20 신뢰도 확인
- `self_validation.overall_pass=true`, target-fit MAE/P90와 actual FIR FFT 확인
- preview에서 기존/이번 전환, apply 전 profile hash 불변
- apply 후 backup 생성 및 새 hash 반영
- restore 기존 튜닝 정상

## 7. 장시간 성능

Pi 2는 10분 이상 다음을 기록한다.

```bash
pidstat -p "$(pgrep -x camilladsp)" 5 120
```

XRUN, service restart, thermal throttling, 지속 90% 이상 CPU가 없어야 한다. chunksize 1024를 선택했다면 2048과 별도 비교한다. Pi 4/5도 같은 방식으로 측정하되 architecture 차이를 이유로 Pi 2 수치를 그대로 복사하지 않는다.
