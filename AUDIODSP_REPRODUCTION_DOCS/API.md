# AudioDSP HTTP API

기본 주소는 `http://<Pi 주소>:8080`이다. 현재 API는 인증·TLS가 없는 신뢰된 LAN 전용이다. 모든 JSON 응답은 UTF-8이며 cache를 금지한다.

## 상태와 볼륨

### `GET /api/status`

정규화된 settings, fallback을 반영한 resolved profile, 관리 FIR metadata, U7 selector, A/B preview 상태를 반환한다.

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

측정 실행은 현재 HTML form POST 경로를 사용한다: `/measurement/new`, `/configure`, `/level`, `/position`, `/restart-positions`, `/validation`, `/build`, `/preview`, `/restore`, `/apply`, `/cancel`, `/calibration`.

`result`에는 `self_validation`(실제 FIR FFT/target-fit/전달 이득/impulse),
`room_decay`(채널별 octave T20→RT60), `graphs.*.actual_correction_db`,
`graphs.*.effective_target_db`, `graphs.*.decay_control_db`가 포함된다. 각 측정
응답에는 `measurement_quality`와 `room_decay`가 저장된다.

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
- `/rear-mode`: `profile`, `mode=copy_front|separate`
- `/woofer-trim`: `profile`, `trim_db=-18..0`
- `/chunksize`: `chunksize=512|1024|2048|4096`
- `/upload-stage`, `/staging/preview`, `/staging/restore`, `/staging/apply`, `/staging/discard`

성공한 form은 303 redirect로 해당 화면에 결과 notice를 표시한다.
