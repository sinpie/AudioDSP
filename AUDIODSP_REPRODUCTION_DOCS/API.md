# AudioDSP HTTP API

기본 주소는 `http://<Pi 주소>:8080`이다. 현재 API는 인증·TLS가 없는 신뢰된 LAN 전용이다. 모든 JSON 응답은 UTF-8이며 cache를 금지한다.

## 상태와 볼륨

### `GET /api/status`

정규화된 settings, fallback을 반영한 resolved profile, SISO/MIMO 관리 FIR metadata, 플랫폼 MIMO capability, U7 selector, A/B preview 상태를 반환한다.

A/B Preview가 활성 상태이면 `settings`는 저장된 설정을 유지하지만 `resolved`는 실제 임시 CamillaDSP 구성의 mode, 2/4/8채널 수와 FIR 경로를 반환한다. 이때 `/api/fir/front|rear`도 profile 폴더가 아니라 검증된 측정 session 안의 현재 Preview WAV를 제공한다.

### `GET /api/health`

load average, 온도, 메모리, Xonar U7/UMIK 연결 여부를 반환한다.

### `GET /api/volume`

U7 ALSA `PCM Playback Volume` 실제값과 저장값을 반환한다. 조회는 Pi 2 부하를 줄이기 위해 약 2.5초 cache한다.

```json
{
  "available": true,
  "mixer": "hw:U7",
  "control": "PCM,0",
  "saved_db": -10,
  "actual_db": -10.0,
  "raw": 117,
  "raw_channels": [117, 117, 117, 117, 117, 117, 117, 117],
  "channels": 8,
  "uniform": true,
  "percent": 92.1,
  "min_db": -60,
  "max_db": 0,
  "step_db": 1,
  "hardware_min_db": -127.0,
  "hardware_max_db": 0.0
}
```

`available=false`이면 `error`와 `saved_db`는 남고 실제값 필드는 없을 수 있다.

### `PUT /api/volume`

측정 또는 검증 sweep의 오디오 전용 구간에는 HTTP 400으로 거부한다. 이 구간은 선택 dBFS를 U7 DAC 기준으로 만들기 위해 PCM을 임시 0 dB로 사용하며, 원래 볼륨을 복원한 뒤 다시 쓸 수 있다.

```http
Content-Type: application/json

{"db":-20}
```

`db`는 bool/string/float가 아닌 정수 -60~0이어야 한다. 성공하면 즉시 하드웨어에 쓰고 `profile-settings.json`에 저장한 뒤 `GET` 형식의 응답과 `hardware_applied`를 반환한다. CamillaDSP는 재시작하지 않는다.

```powershell
$body = @{ db = -20 } | ConvertTo-Json -Compress
Invoke-RestMethod -Uri 'http://audiodsp-pi2.local:8080/api/volume' -Method Put -ContentType 'application/json' -Body $body
```

잘못된 요청은 HTTP 400과 `{"error":"..."}`를 반환하며 기존 저장값을 바꾸지 않는다.

## FIR과 target

| Method | 경로 | 결과 |
|---|---|---|
| GET | `/api/fir/front` | 현재 유효 Front FIR WAV |
| GET | `/api/fir/rear` | 현재 유효 Rear FIR. copy mode이면 Front와 같은 WAV |
| GET | `/api/profile/{speaker|headphone}/{front|rear}` | 정식 프로필 FIR |
| GET | `/api/staging/{profile}/candidate/{front|rear}` | staged 후보. 없는 band는 적용 시 유지/복사 규칙 반영 |
| GET | `/api/targets` | target 주파수·dB catalog |

현재 프로필이 bypass면 `/api/fir/front`와 `/api/fir/rear`는 HTTP 409다.

## 측정

| Method | 경로 | 결과 |
|---|---|---|
| GET | `/api/measurement/status` | session, 단계, progress, ETA, level, result |
| GET | `/api/measurement/download/front` | 생성 Front WAV |
| GET | `/api/measurement/download/rear` | 생성 Rear WAV |
| GET | `/api/measurement/download/all` | Front+Rear+manifest ZIP. 두 WAV가 있을 때만 |
| GET | `/api/measurement/download/report-md` | 보정 가능/한계/미측정 분류가 포함된 영구 Markdown 보고서 |
| GET | `/api/measurement/download/report-json` | 그래프·진단·검증을 포함한 전체 결과 JSON |

MIMO 결과의 `/download/all`은 네 MIMO WAV, MIMO manifest, JSON/Markdown 보고서를 한 ZIP으로 반환한다.

`GET /api/measurement/status`의 출력 경로 필드:

- `output_selector`: 현재 boot에서 HID monitor가 읽은 실제 U7 경로, label, state byte, stale 여부
- `measurement_profile`: 레벨 검사에서 고정된 `speaker|headphone`; 검사 전에는 `null`
- `measurement_output`: 고정 당시 label/state byte/source/boot ID/time
- `measurement_output_match`: 현재 물리 경로와 고정 경로가 같으면 `true`, 다르면 `false`, 아직 고정 전이면 `null`

고정 경로가 바뀌면 측정/검증/Preview는 HTTP 400으로 실패한다. 생성 결과의 profile과 다른 `/measurement/preview` 또는 `/measurement/apply` 요청도 HTTP 400이며 WAV/settings는 변경되지 않는다.

측정 실행은 현재 HTML form POST 경로를 사용한다: `/measurement/new`, `/configure`, `/configure-level`, `/session-note`, `/load-session`, `/delete-session`, `/level`, `/position`, `/restart-positions`, `/validation`, `/post-validation`, `/reset-post-validation`, `/build`, `/preview`, `/restore`, `/apply`, `/cancel`, `/calibration`. 세션은 자동 저장되며 `/session-note`는 진행 상태를 건드리지 않고 최대 500자 주석만 저장한다. `/load-session`은 저장 artifact 무결성을 확인하고 완료된 1–6 checkpoint를 복원한다. `/delete-session`은 정확한 ID와 내부 symbolic link 부재를 확인한 뒤 그 세션의 측정 원본/생성물만 삭제하며 정식 프로필 FIR은 건드리지 않는다. 현재 세션을 삭제하면 Preview를 먼저 복구하고 idle 상태로 돌아간다.

내부 measurement CLI에는 `list-sessions`, `set-session-note <text>`, `load-session <id>`, `delete-session <id>`가 있다. Web의 1단계 session 목록은 `list-sessions`의 ID, 완료 위치, 결과 유무와 인접 주석을 사용한다.

`result`에는 `self_validation`(실제 FIR FFT/target-fit/전달 이득/impulse),
`room_decay`(채널별 octave T20→RT60), `graphs.*.actual_correction_db`,
`graphs.*.effective_target_db`, `graphs.*.decay_control_db`가 포함된다. 각 측정
응답에는 `measurement_quality`의 대역별 SNR/confidence와 Woofer 적응형 통과대역,
`room_decay`, `temporal`, `group_delay`가 저장된다. 새 세션의 UI에는 `level_dbfs`
하나만 노출하며 기본은 -42다. `noise_level_dbfs`는 schema-1 백업/CLI 호환 필드로만 남고 현재 값은 `level_dbfs`와 동기화한다. 플랫폼 capability에는
오프라인 단계별 ETA가 들어간다. 실행 PID가 사라졌으면 raw session 파일을
조회만으로 변경하지 않고 응답에 `interrupted_worker=true`인 복구 오류 view를 반환한다.
`measurement_quality.raw_snr_db`는 활성 구간의 원신호 SNR,
`coherent_integration_gain_db`는 2초 ESS 대비 긴 sweep의 matched-filter 이득,
`snr_db`는 두 값을 더한 필터 생성 판정용 유효 SNR이다. 저장 원본의 계산만 다시
수행하는 form 경로는 `/measurement/reprocess-level`과
`/measurement/reprocess-saved`이며 두 경로 모두 소리를 재생하지 않는다.
MIMO 결과는 `kind=mimo_2x4`, `mimo`, `mimo_files`, `room_tuning_audit`를 추가하며,
`mimo.target_level_normalization`, `solution_blend`, `prediction.*.before/after_modal_tail_db`와
`self_validation.core_checks.predicted_modal_tail_non_regression`을 포함한다.

`POST /measurement/build` form은 기존 필드 뒤에 다음 두 값을 보낸다.

- `crossover_enabled=on|off`: L/R/Woofer 및 sub MIMO의 디지털 crossover. 기본 `on`
- `crossover_frequency_hz=60|70|80|90|100|120`: 기본 `100`

SISO 결과의 `result.crossover`는 `embedded_in_fir`, `frequency_hz`, `additional_runtime_filters=0`, `additional_block_latency_samples=0`, `coherent_upper_guard_pass`, `phase_agnostic_target_pass`, `complex_sum_target_pass`, `safe_deploy_pass`, `phase_verification_status`, `status`를 제공한다. 독립-clock 기본값의 성공 상태는 `pass_safe_upper_phase_limited`다. MIMO 결과는 같은 top-level 필드와 `mimo.crossover`를 제공하고, 실제 우퍼 트림 transfer 한계는 `mimo.headroom.physical_output_limits`에서 확인한다. 합산 L/R 모드는 독립 branch가 없으므로 crossover `on` 요청을 HTTP 400으로 거부한다.

측정 구성 값은 `lr`, `lrw_sum`, `lrw`, `mimo_stereo`, `mimo_one_sub`, `mimo_dual_sub`다. 새 세션 UI 기본은 `lrw_sum`이다. `lrw_sum`은 위치마다 L/R/W/L+W/R+W를 측정하지만 L/R/W만 설계에 사용하고 합산 두 응답은 `premeasured_sum_validation`과 `self_validation.premeasured_sum_model`에만 들어간다. 독립-clock에서 크기 closure와 안전 합산이 통과하면 `crossover_sum.status=pass_safe_sum_phase_limited`다. 더 빠른 `lrw`는 위상 비의존 에너지 타깃과 동상 상한으로 `pass_safe_upper_phase_limited`가 된다. 공통 timing reference가 명시된 경우에만 complex-model 상태를 낸다. 두 구성 모두 FIR 생성 뒤 필수 스윕을 요구하지 않는다. `/measurement/post-validation`은 별도 acoustic audit를 수행할 때만 쓰는 선택 API다.

## 백업

| Method | 경로 | 결과 |
|---|---|---|
| GET | `/api/backup/download` | 현재 전체 설정 ZIP |
| GET | `/api/backup/latest` | 가장 최근 자동 rollback ZIP |

복원은 HTML form의 `/backup/stage`, `/backup/apply`, `/backup/discard`를 사용한다.

## HTML form 호환 경로

JavaScript가 꺼져 있어도 다음 form POST가 동작한다.

- `/volume`: `db=-60..0`
- `/bypass`: `profile`, `enabled=on|off`
- `/mimo-enabled`: `profile=speaker`, `enabled=on|off` (검증된 bank, Pi4/5, 공통 timing reference 필요)
- `/rear-mode`: `profile`, `mode=copy_front|separate`
- `/woofer-trim`: `profile`, `trim_db=-18..0`
- `/chunksize`: `chunksize=512|1024|2048|4096`
- `/upload-stage`, `/staging/preview`, `/staging/restore`, `/staging/apply`, `/staging/discard`

성공한 form은 303 redirect로 해당 화면에 결과 notice를 표시한다.
