# 시험 전략과 실행 방법

## 1. 정적 검사

Python:

```powershell
py -3 -m py_compile .\payload\audiodsp-profile-manager.py .\payload\audiodsp-profile-web.py .\payload\audiodsp-measurement.py .\payload\audiodsp-mimo.py .\payload\audiodsp-profile-monitor.py .\test_profile_matrix.py .\test_measurement_engine.py .\test_mimo_runtime.py
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
- 세 화면 UI, 반응형 signal-flow/measurement path-lock SVG marker, staged upload, A/B, apply, backup/restore/latest rollback
- 소리 없는 session 생성/재설정, 보고서 MD/JSON/ZIP download, 측정한 U7 경로 외 Preview/Apply HTTP 400 및 무변경
- session 주석 저장 전후 level/측정/FIR checkpoint 불변, A→B 생성 뒤 A 주석 포함 목록 표시·불러오기, 완료 artifact 누락 session 거부
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
조절 가능한 Woofer 측정/reference 비율, 적응형 -3 dB 통과대역 SNR, 정상 0.4초·cold-start 앞부분 절단·1.1초 USB 지연 sweep timing 복구, 잔향 cut-only,
음향+FIR 총지연 정렬과 중단 worker 복구도 확인한다. 또한 정상 2000-sample direct peak는 허용하고, 518895-sample ESS artifact와 FFT 끝에 감긴 음수 지연 peak는 거부하는 0~250 ms bulk-delay gate를 검사한다.

물리 출력 경로 시험은 임시 boot-ID/selector JSON으로 Speaker 경로를 bind한 뒤 같은 경로 허용, Headphone 잭으로 변경 시 `MeasurementError`, 다른 profile 결과 적용 거부를 확인한다. 이는 실제 U7 selector를 누르거나 소리를 내지 않는다. 실제 sweep 재생 함수는 별도 수락 시험에서만 실행한다.

실측 session의 UI SISO 값 전체를 확인할 때는 `diagnostics/run_full_option_matrix.py`로 기준값에서 한 축씩 바꾼 67개 32768탭 Front/Woofer FIR 쌍을 생성한다. `diagnostics/build_option_validation_sequence.py`는 같은 FIR을 정확히 offline convolution한 4채널 저음량 시퀀스와 감쇄한 무필터 전/후 기준을 만들고, `diagnostics/capture_option_validation.py`는 production DSP-bypass/U7-input-off 경로에서 상태를 보존하며 UMIK로 녹음한다. `diagnostics/analyze_option_validation.py`는 모든 L/R 합산 sweep의 SNR·peak·target-fit·생활소음 transient와 Woofer/Bass/Treble 단조성을 분석한다. 이 검사는 조합 폭발을 피하기 위한 one-factor-at-a-time 기능 검증이며 모든 값의 Cartesian product를 의미하지 않는다.

엔진 변경이 특정 옵션 축에만 영향을 줄 때 `run_full_option_matrix.py --variant-id ...`로 해당 값을 선택 재생성할 수 있다. `merge_option_matrix.py`는 새 엔진으로 생성한 baseline FIR SHA가 기존과 동일한지 먼저 증명하고, 선택 결과만 덮어쓴 뒤 67개 모든 FIR SHA를 다시 검증해 재사용/재생성 provenance를 manifest에 남긴다.

Pi 2의 장시간 검증 녹음은 `capture_option_validation.py --record-via-tmpfs`로 `/dev/shm`에 먼저 기록해 SD 쓰기 stall을 피한다. 사용 전 예상 녹음 크기와 32 MiB 여유를 검사하며, 완료 뒤 지정 경로로 복사하고 임시파일을 회수한다. Production engine은 ALSA overrun을 fatal error로 처리한다.

## 4. MIMO 무음 시험

Pi4/5 배포 전 세 토폴로지의 수치 비퇴행을 확인한다. Pi2에서도 계산 fixture는 실행할 수 있지만 실시간 활성화는 별도로 차단된다.

```bash
AUDIODSP_PLATFORM_CLASS=test AUDIODSP_TARGET_DIR=/usr/local/share/audiodsp/targets \
python3 /usr/local/bin/audiodsp-mimo.py --measurement-engine /usr/local/bin/audiodsp-measurement.py self-test
python3 /tmp/test_mimo_runtime.py \
  --manager /usr/local/bin/audiodsp-profile-manager.py \
  --web /usr/local/bin/audiodsp-profile-web.py \
  --measurement /usr/local/bin/audiodsp-measurement.py \
  --camilladsp /usr/local/bin/camilladsp
```

첫 명령은 Stereo/2.1/2.2 각각 상대 bulk-delay 복원, 기존 SISO 저역 기준 레벨 고정, finite, 인과성, physical output별 최악 상관입력 row sum, 실제 Woofer trim 상한, LR4 FIR-bank 내장, 타깃 MAE·좌석편차·평활 전달함수 impulse-tail proxy 비퇴행과 네 WAV×32768탭을 검사한다. acoustic 비퇴행을 통과하지 못한 합성 topology는 구조 시험을 실패시키지 않고 `safe_rejection=true`로 기록하되 실제 적용은 차단되는지 확인한다. 두 번째는 격리된 임시 config에서 8 Conv와 2→8→4 mixer를 실제 CamillaDSP `--check`로 검사하고 Pi2 enable 거부와 MIMO 백업 임시파일 회수를 확인한다. 둘 다 오디오 장치를 열거나 소리를 내지 않는다.

## 5. 실제 Pi 무중단 배포 확인

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

## 6. 볼륨 경계 시험

자동 시험 외 수동 확인:

1. 현황 페이지의 실제/저장값이 같다.
2. 현재값에서 -1 dB 후 +1 dB로 복귀하고 모든 출력이 함께 변한다.
3. U7 물리 노브를 한 click 바꾸면 3초 안에 실제값만 변한다.
4. `저장·적용`하면 실제/저장값이 다시 같아진다.
5. Web console/network에서 PUT이 하나만 전송되는지 본다.

큰 음량으로 올리는 시험은 하지 않는다.

## 7. 실제 측정 수락 시험

실제 음향 시험은 사용자가 허용한 시간에만 한다.

- 90° calibration serial/point 확인
- -48 또는 -42 dBFS level check, clipping 없음, SNR OK
- 세 위치 L/R, L/R/Woofer 또는 Pi4/5의 독립 제어원 MIMO 완료
- build progress/ETA와 Pi 응답성 유지
- 결과 WAV 48k/float32/stereo/32768
- L/R/Woofer 개별 모드의 기본 crossover ON/100 Hz, Front HPF/Rear LPF actual FIR response, 추가 runtime filter/block latency 0 확인
- 세 위치 `|Front|+|Woofer|` 상한 guard와 신뢰 가능한 phase의 복소합 target 확인; 실패·불신뢰를 PASS로 표시하지 않는지 확인
- maximum transfer ≤ 0 dB
- 개별 sweep SNR ≥ 6 dB(15 dB 이상 권장), octave T20 신뢰도 확인
- `self_validation.overall_pass=true`, target-fit MAE/P90와 actual FIR FFT 확인
- preview에서 기존/이번 전환, apply 전 profile hash 불변
- apply 후 backup 생성 및 새 hash 반영
- restore 기존 튜닝 정상
- MIMO이면 네 WAV/manifest/report, 8 convolution, coherence/headroom, 별도 검증 위치를 추가 확인

## 8. 장시간 성능

Pi 2는 10분 이상 다음을 기록한다.

```bash
pidstat -p "$(pgrep -x camilladsp)" 5 120
```

XRUN, service restart, thermal throttling, 지속 90% 이상 CPU가 없어야 한다. chunksize 1024를 선택했다면 2048과 별도 비교한다. Pi 4/5도 같은 방식으로 측정하되 architecture 차이를 이유로 Pi 2 수치를 그대로 복사하지 않는다.

MIMO 8-path는 Pi4/5에서 chunksize 1024 이상으로 최소 10분 측정한다. 합성 수치 PASS나 CamillaDSP parser PASS를 실제 CPU/XRUN 수락으로 대신하지 않는다.

## 9. Web UI 접근성·반응형 회귀

- `/`, `/measure`, `/settings`에 각각 한 개의 `aria-current=page`와 서로 다른 문서 제목이 있어야 한다.
- 측정 화면의 현재 단계는 링크를 유지하면서 `aria-current=step`이어야 한다.
- skip link가 `#main-content`로 이동하고, 오류는 focus 가능한 `role=alert`여야 한다.
- Speaker/Headphone Front/Rear WAV와 백업 ZIP의 파일 입력은 모두 연결된 visible label을 가져야 한다.
- 의미를 텍스트가 이미 설명하는 SVG icon은 접근성 트리에서 숨기고, FIR 비교 그래프는 focus 가능한 region 및 내부 `role=img`를 유지한다.
- 390 CSS px에서 document `scrollWidth == clientWidth`; 700 px FIR graph만 자체 `.graph-scroll` 안에서 좌우 이동해야 한다.
- 390 CSS px에서 주요 button/select/navigation과 summary 높이는 44 px 이상이어야 한다. 1440 px PC 화면에서는 카드·표·단계 흐름이 겹치거나 잘리지 않아야 한다.
- dark theme의 `--on-accent`/`--accent` 대비는 WCAG AA(일반 텍스트 4.5:1) 이상이어야 한다.
- 측정 경로가 아직 `null`인 idle 화면을 3초 이상 열어도 document navigation/reload가 발생하지 않아야 한다.
- `algorithm_revision`이 없거나 다른 이전 결과는 재계산 경고와 disabled Preview/Apply를 보이고 두 POST를 엔진도 거부해야 한다.
- 최신 결과라도 `self_validation.overall_pass=false`이면 Preview는 가능하지만 정식 Apply POST는 거부해야 한다.
- Speaker/Headphone × 저장 copy/separate × 저장 bypass on/off × Preview 2ch/4ch의 16개 상태에서 `/api/status`, 실제 config topology, 설정 불변, 복귀 mode/channel을 모두 확인한다.
- 측정 화면의 6개 tab/tabpanel, `aria-selected`/`aria-current=step`, 방향키 이동, 한 패널만 표시, 탭과 패널 간격 0, PC/모바일 document overflow 0과 4초간 자동 navigation 0회를 확인한다.
- 활성 session 요약이 단계 탭보다 앞에 있고, 저장 session 카드의 주석/완료 막대/이어하기가 모바일과 PC에서 잘리지 않는지 확인한다. 실패 fixture에서는 tab·진단 카드가 빨간색이며 PASS/FAIL/N/A와 FAIL 해결 방법이 동시에 보여야 한다.
- 모든 `details > summary`에 vector chevron이 보이고, 클릭/키보드로 열었을 때 open 강조와 화살표 방향이 바뀌는지 확인한다. 검사 브라우저를 응답 도중 종료해도 Web journal에 BrokenPipe/ConnectionReset traceback이 남지 않아야 한다.
- 0 dB Woofer trim 결과에서 `Woofer 최종 trim +0 dB`와 `측정 시 Woofer 감쇄 −9 dB`가 별도 항목으로 표시되어야 한다.
