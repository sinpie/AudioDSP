# 시험 전략과 실행 방법

## 1. 정적 검사

Python:

```powershell
py -3 -m py_compile .\source\common\payload\*.py .\source\common\tests\*.py .\tools\*.py
```

Shell:

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -n .\source\common\payload\audiodsp-camilladsp-start
& 'C:\Program Files\Git\bin\bash.exe' -n .\source\common\payload\audiodsp-output-profile
& 'C:\Program Files\Git\bin\bash.exe' -n .\releases\audiodsp-pi2-v1.2.0\firstrun.sh
& 'C:\Program Files\Git\bin\bash.exe' -n .\releases\audiodsp-pi4-pi5-v1.2.0\firstrun.sh
```

릴리스 전체:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\releases\audiodsp-pi2-v1.2.0\write_pi2_sd_as_admin.ps1 -ValidateOnly -NoPause
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\releases\audiodsp-pi4-pi5-v1.2.0\write_final_sd_as_admin.ps1 -ValidateOnly -NoPause
```

U7 selector 전용 무음 회귀(Pi에서 실행):

```bash
python3 /tmp/test_u7_profile_monitor.py \
  --monitor /usr/local/bin/audiodsp-profile-monitor.py
```

버튼 이벤트와 부팅 안정 report의 서로 다른 byte 표, `O_RDONLY` HID open,
HID output/feature report 쓰기 부재를 짧게 검증한다.

## 2. Profile matrix

이 시험은 임시 디렉터리와 fake ALSA/systemd helper를 사용해 실제 profile/settings를 변경하지 않는다.

검증 범위:

- 요청 profile 2 × chunksize 4 × bypass 4 × Front 존재 4 × Rear 존재 4 × Rear mode 4 × Factory 2 = 4096 상태
- 정상 3968, 의도된 no-profile error 128
- 56개 설정 operation의 ordered pair 3136
- 생성된 모든 고유 YAML을 실제 CamillaDSP `--check`
- WAV format·rate·channel·NaN·size·tap 경계
- profile fallback, bypass, copy/separate, woofer trim, chunksize
- U7 버튼 이벤트 `0x30/0xA0`, 부팅 안정 상태 `0x88/0xE0`, read-only
  `HIDIOCGINPUT`, 안내음
- 세 화면 UI, 반응형 signal-flow/measurement path-lock SVG marker, staged upload, A/B, apply, backup/restore/latest rollback
- 소리 없는 session 생성/재설정, 보고서 MD/JSON/ZIP download, 측정한 U7 경로 외 Preview/Apply HTTP 400 및 무변경
- session 주석 저장 전후 level/측정/FIR checkpoint 불변, A→B 생성 뒤 A 주석 포함 목록 표시·불러오기, active session 삭제 후 idle 복귀·정식 FIR 불변·중복 삭제 거부, 완료 artifact 누락 session 거부
- SISO/MIMO FAIL fixture가 빨간 상태와 함께 화면의 실제 `1 · 측정 구성`~`5 · 결과 검토`, `안정성/효과`, `공동제어 상한`, `보조 출력 사용 제한`, 실행 버튼명을 사용해 조치를 안내
- 이전 response revision은 원본 WAV 무음 재계산을 제시하고, 새 결과는 평균제곱 공간 통합 방법과 dB 기하평균 대비 변화량을 표시
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

추가 수학 회귀는 음향 응답 power-domain smoothing과 filter-gain dB-domain smoothing을 분리하고, 0/0/+12 dB 세 위치의 weighted mean-square 값, 같은 가중치의 공간 표준편차, metadata가 전혀 없는 초기 파일까지 포함한 이전 response 이중 smoothing 금지, NaN/Inf 거부, shared-clock 환경변수 이름을 exact 값으로 확인한다. L+Woofer/R+Woofer 대표 그래프와 지연 탐색도 같은 위치/SNR 가중을 사용하고 최악 위치 안전 상한은 평균하지 않는지 검사한다.

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

물리 출력 경로 시험은 임시 boot-ID/selector JSON으로 Speaker 경로를 bind한 뒤 같은 경로 허용, Headphone 잭으로 변경 시 `MeasurementError`, 다른 profile 결과 적용 거부를 확인한다. 같은 무음 fixture는 `Camilla stop → Mic nocap → Line nocap → PCM 0 dB → 원래 PCM 복원 → Camilla start` 순서와 복원 실패 시 Camilla 미시작도 검사한다. 이는 실제 U7 selector를 누르거나 소리를 내지 않는다. 실제 sweep 재생 함수는 별도 수락 시험에서만 실행한다.

`test_profile_matrix.py`는 응답 fixture에 `inf/-inf/nan`을 주입해 API 직렬화가 표준 JSON `null`만 내고 브라우저 parser를 깨지 않는지 확인한다. 실제 Pi UI는 CDP smoke test에서 `liveState=실시간`, 결과 SVG/polyline 존재, 20 Hz–20 kHz 요약을 확인한다.

`test_web_measurement_flow.py`는 Linux 서비스나 CamillaDSP parser 없이도 실행되는 교차 플랫폼 무음 smoke test다. 실제 `measurement_panel()`을 SISO와 MIMO fixture로 렌더링해 `세션`+1–6 총 7개 tab/panel, `측정 구성`/`결과 검토` 용어, MIMO 전용 옵션 카드, SISO에서 MIMO 제어가 숨겨지는지를 확인한다.

```powershell
python source/common/tests/test_web_measurement_flow.py `
  --web source/common/payload/audiodsp-profile-web.py `
  --measurement source/common/payload/audiodsp-measurement.py
```

`test_session_migration.py`는 실제 session export/import 왕복에서 ID, 모든 파일 byte 수와 SHA-256, current pointer를 검증하고 `../` 경로를 포함한 악성 archive를 거부한다. SD writer `-ValidateOnly -WindowsWifiProfile <profile> -SessionMigrationArchive <archive>`는 key를 출력하지 않은 채 WLAN profile과 archive hash까지 검사한다.

`test_measurement_engine.py`는 `lrw_sum` 정밀 fixture에서 위치당 L/R/W/L+W/R+W를 모두 준비한 뒤에만 FIR 계산이 열리는지 확인한다. L+W/R+W는 합산 closure에만 쓰이고 FIR 평균·정규화에는 들어가지 않아야 한다. 측정 level -9 dB를 reference와 함께 바꿔도 복원된 전달함수와 생성 FIR이 같아야 하며, combined response를 독립 normalize하거나 두 번 보정한 fixture는 실패해야 한다. Crossover ON/OFF 모두 `pass_premeasured_model`이면 추가 사후 sweep 없이 Apply 가능해야 한다.

`test_target_option_matrix.py`는 6 target×3 preset 18개 조합과 Web에 노출된 target/crossover/trim/억제/phase/보정 범위/최대 상대 보상/cut/취향 값의 one-axis 및 대표 상호작용 95개를 실제 32768탭으로 계산한다. `Flat + 추가 억제 없음 + trim 0 dB + 최대 상대 보상 10 dB` 기준 조합은 반드시 PASS하고, 비기준 조합은 안전 제한에 따라 FAIL할 수 있으나 형식·유한값·headroom 검사는 항상 통과해야 한다.

같은 회귀는 L/R 500~2,000 Hz에서 얻은 하나의 측정·타깃 기준이 Woofer까지 전달되는지, 완성 4채널 bank에 common gain 한 번만 적용되는지, branch 간 상대 dB가 부동소수점 허용오차 안에서 유지되는지를 검사한다. 양쪽 Front의 넓은 10 kHz 이상 roll-off fixture는 16 kHz에서 4 dB를 넘게 상대 보상하되 선택 상한 10 dB를 넘지 않아야 하고, 한 채널의 좁은 15 dB null은 3 dB를 넘게 boost하거나 common attenuation을 결정해서는 안 된다. 고역 감쇄 회귀는 양쪽의 넓은 8 kHz peak가 과거 숨은 −3 dB 상한을 넘어 보정되는지, 좁은 한쪽 peak는 신뢰도에 따라 완화되는지, UI의 `최대 룸 감쇄`만 최종 절대 상한으로 동작하는지를 별도로 확인한다. 실측 저장 session 회귀는 10/15/20 kHz의 요청 correction·FIR 실제 correction·예상 음압과 전체 common attenuation을 함께 기록한다.

실측 session의 UI SISO 값 전체를 확인할 때는 `diagnostics/run_full_option_matrix.py`로 Flat/추가 억제 없음/trim 0 dB/최대 상대 보상 10 dB 기준에서 한 축씩 바꾼 68개 32768탭 Front/Woofer FIR 쌍을 생성한다. `diagnostics/build_option_validation_sequence.py`는 같은 FIR을 정확히 offline convolution한 4채널 저음량 시퀀스와 감쇄한 무필터 전/후 기준을 만들고, `diagnostics/capture_option_validation.py`는 production DSP-bypass/U7-input-off 경로에서 상태를 보존하며 UMIK로 녹음한다. `diagnostics/analyze_option_validation.py`는 모든 L/R 합산 sweep의 SNR·peak·target-fit·생활소음 transient와 Woofer/Bass/Treble 단조성을 분석한다. 이 검사는 조합 폭발을 피하기 위한 one-factor-at-a-time 기능 검증이며 모든 값의 Cartesian product를 의미하지 않는다.

엔진 변경이 특정 옵션 축에만 영향을 줄 때 `run_full_option_matrix.py --variant-id ...`로 해당 값을 선택 재생성할 수 있다. `merge_option_matrix.py`는 새 엔진으로 생성한 baseline FIR SHA가 기존과 동일한지 먼저 증명하고, 선택 결과만 덮어쓴 뒤 68개 모든 FIR SHA를 다시 검증해 재사용/재생성 provenance를 manifest에 남긴다.

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

첫 명령은 Stereo/2.1/2.2 각각 상대 bulk-delay 복원, 200 Hz 이하 `center`/`equal` 동일 기하 가중, 위치/행 관측 가능성 가중과 actuator별 uncertainty regularization, 실제 1-노름 조건수·자동 diagonal loading, solver·그래프·MAE 일치, deployable SISO base-bank 공통 정규화, 기존 SISO 저역 기준 레벨 고정, finite, 인과성, physical output별 최악 상관입력 row sum, 실제 Woofer trim 상한, LR4 FIR-bank 내장, 타깃 MAE·좌석편차·모든 측정 위치 MAE·평활 전달함수 impulse-tail proxy 비퇴행과 네 WAV×32768탭을 검사한다. 세 토폴로지의 `Flat + 추가 억제 없음 + trim 0 dB` 모델은 반드시 PASS한다. 이어 MIMO UI 값 19개를 실제 8경로 bank로 만들고 구조 검증은 전부 PASS, acoustic 비퇴행 실패 조합은 `fail_model`로 안전 차단되는지 확인한다. 두 번째는 격리된 임시 config에서 8 Conv와 2→8→4 mixer를 실제 CamillaDSP `--check`로 검사하고 Pi2 enable 거부와 MIMO 백업 임시파일 회수를 확인한다. 둘 다 오디오 장치를 열거나 소리를 내지 않는다.

2026-08-21 v24 무음 회귀 결과:

- MIMO Stereo/2.1/2.2의 `Flat + 추가 억제 없음 + trim 0 dB` 기준은 모두 model PASS. 실제 1-노름 최대 조건수는 각각 5.609/7.741/8.682이고 모든 측정 위치 비악화, 20 Hz–20 kHz graph coverage를 PASS했다.
- MIMO UI 옵션 19개는 structural failure 0, model-limited 0으로 PASS했다. near-singular와 exact-singular 2×2 fixture는 자동 diagonal loading 뒤 조건수 10,000 이하로 복구됐다.
- `test_web_measurement_flow.py`는 7 tab/7 panel, MIMO 전용 카드, SISO 숨김과 용어 marker를 PASS했다. Python compile, UI source stale-term 검사와 `git diff --check`도 PASS했다.
- Pi2/Pi3/Pi4/Pi5 canonical materialization은 각각 40/40/38/38 파일 assemble/check PASS, 5.1+dual-sub dense 42경로 계획은 runtime 135 MiB/생성 530 MiB로 2 GB 제한을 PASS했다.
- Windows 호스트에는 WSL과 실행 가능한 Linux CamillaDSP가 없어 `test_profile_matrix.py`의 실제 CamillaDSP `--check` 및 `test_mimo_runtime.py`는 v24에서 재실행하지 못했다. v23에서 같은 runtime/config 경로가 full PASS했으며, v24의 runtime topology·mixer·manager 코드는 변경하지 않았다. 다음 Pi 연결 시 이 두 Linux 회귀를 다시 실행한다.

Pi 5 2 GB와 향후 5.1 worst-case 메모리는 다음 무음 시험으로 고정한다.

```powershell
python .\tools\estimate_dsp_memory.py
python .\source\common\tests\test_resource_budget.py --mimo .\source\common\payload\audiodsp-mimo.py --allocate
```

현재 2×4/8경로, 5.1 diagonal/6경로, 5.1+dual-sub 완전 dense 6×7/42경로의 원시 계수·partition 연산·실시간/생성 계획값을 검증한다. `--allocate`는 42경로의 실제 64-bit Python 배열 생명주기를 할당해 peak가 계획 상한 아래인지 확인한다. 메모리 PASS와 Pi 5 CPU/XRUN PASS는 별도다.

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

2026-08-20 Pi2 실제 Fast 1위치 5경로 시험의 원본 SNR, coherent integration,
합산 closure, FIR 옵션별 PASS/FAIL과 정식 FIR 불변 확인은
[PI2_ACOUSTIC_ACCEPTANCE_20260820.md](PI2_ACOUSTIC_ACCEPTANCE_20260820.md)에 기록했다.

- 90° calibration serial/point 확인
- 현재 세션 dBFS로 모든 출력 조합의 2초 빠른 ESS, clipping 없음, 최저 SNR 6 dB 이상(15 dB 권장)
- 빠른 측정 1위치 또는 표준 측정 3위치의 L/R/우퍼와 선택형 L+우퍼/R+우퍼 완료
- build progress/ETA와 Pi 응답성 유지
- 결과 WAV 48k/float32/stereo/32768
- L/R/Woofer 개별 모드의 기본 crossover ON/100 Hz, Front HPF/Rear LPF actual FIR response, 추가 runtime filter/block latency 0 확인
- 독립-clock 기본 구성은 위상 비의존 에너지 타깃과 `|Front|+|Woofer|` cut-only 상한을 함께 통과해 `pass_safe_upper_phase_limited` 또는 `pass_safe_sum_phase_limited`가 되는지 확인한다. 그래프에 clock-drift 복소 딥을 표시하지 않아야 한다.
- 공통 timing reference를 명시한 합성 fixture에서만 복소 위상·극성·상대 delay와 MIMO 수치 경로를 확인한다. 어떤 SISO 구성도 뒤늦은 필수 사후 측정을 요구하지 않아야 한다.
- maximum transfer ≤ 0 dB
- 개별 sweep 유효 SNR ≥ 6 dB(15 dB 이상 권장), 긴 ESS는 원신호 SNR과 2초 기준 coherent integration 이득을 별도 기록, octave T20 신뢰도 확인
- `self_validation.overall_pass=true`, target-fit MAE/P90와 actual FIR FFT 확인
- 선택형 Preview 사후 검증은 실측 L/R을 개별 normalize하지 않고 하나의 공통 기준을 사용하며, target뿐 아니라 계산 예상과의 전체/crossover MAE·P90을 판정한다. SNR 6~15 dB에서도 모든 엄격 오차 기준을 만족하면 `PASS · 권장 미달`로 수락한다. 이 구간에서 기준을 근소하게 넘으면 확정 FAIL 대신 재검증을 권장하고, 6 dB 미만 또는 큰 오차만 즉시 차단한다. sweep 전/후 noise floor가 4 dB 이상 다른 것만으로 PASS 응답을 버리지 않는다. active sweep transient가 검출되거나 noise floor 불일치와 응답 FAIL이 함께 있을 때만 `판정 보류 · 출력 전환 감지`로 두며, 완료 응답은 `저장 결과 재판정`으로 소리 없이 다시 계산할 수 있어야 한다.
- preview에서 기존/이번 전환, apply 전 profile hash 불변
- apply 후 backup 생성 및 새 hash 반영
- restore 기존 튜닝 정상
- MIMO이면 공통 timing reference 확인 후 네 WAV/manifest/report, 8 convolution, coherence/headroom과 `pass_multichannel_complex_model`을 확인한다. reference가 없으면 생성·활성화가 차단되어야 한다.

2026-08-21 Pi5 Fast 세션의 v21 실제 Preview 검증(-30 dBFS FIR 입력, 14초)은 원래 profile을 자동 복원한 상태에서 다음을 확인했다. FIR 공통 감쇄 약 10 dB 때문에 사후 SNR은 L/R 9.42/7.20 dB로 내려갔다. 전체 Flat target은 L MAE/P90 1.968/4.516 dB, R 1.562/3.265 dB로 PASS했고 예상↔실측도 L 2.058/4.666 dB, R 1.638/3.467 dB였다. 단 L 50~200 Hz 예상 P90 6.170 dB와 target P90 5.482 dB가 5 dB 기준을 근소하게 넘었으므로 낮은 SNR 재검증 대상으로 분류한다. 10/15/20 kHz 실측은 L -0.36/-0.19/-2.23 dB, R -2.14/-0.54/-0.80 dB였다.

같은 위치에서 -25 dBFS FIR 입력과 자동 28초 ESS로 한 번만 재검증한 결과 최소 SNR 14.29 dB에서 전체 PASS했다. 전체 target MAE/P90은 L 1.434/3.102 dB, R 1.475/2.765 dB, 예상↔실측은 L 1.373/3.000 dB, R 1.494/3.049 dB였다. 50~200 Hz target은 L 1.600/3.384 dB, R 1.798/3.367 dB, 예상↔실측은 L 1.389/2.660 dB, R 1.569/3.400 dB였다. 즉 첫 검증의 저역 경계 초과는 낮은 SNR의 변동이었으며, v21 예측과 실제 합산이 크게 다른 현상은 재현되지 않았다. 종료 후 입력과 117/127 U7 볼륨을 복원하고 기존 Speaker FIR SHA-256 `8a8a3b2fc31a080a6bc40205f29ea6471df95adf357618b2025bdd193ef45c99`로 복귀했다.

v22 재계산 결과를 -30 dBFS/28초로 검증했을 때 R은 target MAE/P90 1.541/3.626 dB로 정상이나 첫 L은 4.034/13.914 dB로 실패했다. 입력 WAV와 FIR FFT는 좌우 대칭이었지만 L 녹음의 첫 0.4초는 약 -47 dBFS에서 감쇠했고 sweep 전/후 noise 추정치가 -50.0/-75.93 dBFS로 25.93 dB 벌어졌다. 이 stream-start 오염이 30/40/50/60/80/100/130 Hz를 예상보다 각각 약 +18.3/+16.3/+14.6/+14.7/+10.1/+7.5/+5.1 dB 올린 원인이므로 v23은 2초 무음 lead와 판정 보류를 적용한다. 이 시험 뒤에도 기존 Speaker FIR SHA와 -10 dB 볼륨, 입력 복귀를 확인했다.

v23을 -25 dBFS/28초와 2초 무음 lead로 검증한 결과 최소 SNR은 18.54 dB였다. target MAE/P90은 L 1.388/3.337 dB, R 1.340/2.953 dB, crossover MAE/P90은 L 1.748/3.978 dB, R 1.990/4.409 dB, 예상↔실측 MAE/P90은 L 1.318/2.885 dB, R 1.278/2.706 dB로 전부 PASS했다. L sweep 전/후 noise 차이는 1.50 dB, R은 5.19 dB였으나 두 active sweep transient 검출은 false였고 R 음향 지표도 모두 PASS였다. 따라서 전·후 stationary floor 차이만으로 유효 응답을 폐기하지 않는 판정식이 필요함을 실제 데이터로 확인했다. 시험 뒤 기존 Speaker FIR SHA, -10 dB 볼륨, U7 입력과 세 service를 복원했다.

2026-08-22 감쇄 신뢰도 v24는 같은 Pi5 Fast 저장 세션 `20260821_195955`의 격리 복사본을 소리 없이 재계산했다. Flat/추가 억제 없음/우퍼 트림 0 dB/최대 상대 보상 10 dB/최대 룸 감쇄 18 dB/100 Hz crossover에서 7.65 kHz L/R 요청 감쇄는 이전의 고정 −3/−3 dB에서 −6.718/−5.805 dB로 바뀌고 예상 타깃 잔차는 +4.002/+2.910 dB에서 +0.278/+0.099 dB로 줄었다. 전체 Front target MAE/P90은 L 0.639/1.637 dB, R 0.790/2.109 dB로 모두 PASS했다. 95개 옵션 행렬, 엔진 E2E와 Web 측정 흐름도 무음 PASS했으며 운영 Speaker FIR SHA `1ae8126b…d890c`/`21d31525…1e99e`는 변경하지 않았다.

## 8. 장시간 성능

Pi 2는 10분 이상 다음을 기록한다.

```bash
pidstat -p "$(pgrep -x camilladsp)" 5 120
```

XRUN, service restart, thermal throttling, 지속 90% 이상 CPU가 없어야 한다. chunksize 1024를 선택했다면 2048과 별도 비교한다. Pi 4/5도 같은 방식으로 측정하되 architecture 차이를 이유로 Pi 2 수치를 그대로 복사하지 않는다.

MIMO 8-path는 공통 timing reference를 갖춘 Pi4/5에서 chunksize 1024 이상으로 최소 10분 측정한다. 합성 수치 PASS나 CamillaDSP parser PASS를 실제 CPU/XRUN 수락으로 대신하지 않는다.

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
- 측정 화면의 `세션`+1~6 총 7개 tab/tabpanel, `aria-selected`/`aria-current=step`, 방향키 이동, 한 패널만 표시, 탭과 패널 간격 0, PC/모바일 document overflow 0과 4초간 자동 navigation 0회를 확인한다.
- POST와 A/B 미리듣기 뒤 현재 scroll/tab이 유지되고, 계산 완료 때만 5 · 결과 검토로 이동하는지 확인한다. 내부 진단 FAIL은 고정 toast가 아니며 화면 최상단으로 focus를 이동시키지 않아야 한다.
- SISO mode의 FIR 계산 화면에는 MIMO 전용 select가 보이지 않아야 한다. MIMO mode에서는 세 핵심 select와 조건수/actuator confidence/위치별 MAE·범위 그래프가 함께 보여야 한다.
- 활성 session 요약이 단계 탭보다 앞에 있고, 저장 session 카드의 주석/완료 막대/이어하기가 모바일과 PC에서 잘리지 않는지 확인한다. 실패 fixture에서는 tab·진단 카드가 빨간색이며 PASS/FAIL/N/A와 FAIL 해결 방법이 동시에 보여야 한다.
- 모든 `details > summary`에 vector chevron이 보이고, 클릭/키보드로 열었을 때 open 강조와 화살표 방향이 바뀌는지 확인한다. 검사 브라우저를 응답 도중 종료해도 Web journal에 BrokenPipe/ConnectionReset traceback이 남지 않아야 한다.
- 0 dB Woofer trim 결과에서 `Woofer 최종 trim +0 dB`와 `측정 시 Woofer 감쇄 −9 dB`가 별도 항목으로 표시되어야 한다.

정확한 CSS viewport와 실제 HTTP 응답을 함께 확인하는 재사용 명령:

```powershell
python diagnostics/render_ui_cdp.py --chrome "C:\Program Files\Google\Chrome\Application\chrome.exe" --url http://<PI-IP>:8080/measure --width 390 --height 844 --output diagnostics/ui-check/measure-mobile.png
```

`--url`, `--width`, `--height`, `--output`을 바꿔 390×844와 1440×1200에서 `/`, `/measure`, `/settings`를 각각 실행한다. overflow, 현재 앱 탭, `세션`+1–6 중 한 panel만 표시, hash/ARIA 상태, disclosure toggle, 4초 동안 예기치 않은 navigation 0회를 검사한다. 스크린샷은 판정 보조 자료이고 DOM/CDP assertion이 자동 PASS 조건이다.
