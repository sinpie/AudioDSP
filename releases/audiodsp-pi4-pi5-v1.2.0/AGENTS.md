# AudioDSP 작업자 지침

이 파일은 사람과 자동화 작업자가 AudioDSP를 수정할 때 지켜야 할 저장소 수준 규칙이다.

## 기준 소스

- 최종 기준은 `releases/audiodsp-pi2-v1.2.0`과 `releases/audiodsp-pi4-pi5-v1.2.0`이다.
- 공통 Python·shell·service·audio asset은 두 payload에 같은 내용으로 유지한다.
- Pi별로 달라야 하는 것은 OS/CPU용 CamillaDSP binary와 이미지, writer, network provisioning, hostname, 초기 chunksize다.
- `legacy-v1-reference`와 `pi2-strong-bass-4ch-v1.1.0`은 이력이다. 새 변경을 그곳에만 남기지 않는다.
- 새 설치 식별자는 `audiodsp`다. `GSONIC_*` 환경변수는 기존 테스트/설치 호환 때문에만 유지하고 신규 이름·파일을 추가하지 않는다.

## 절대 유지 조건

1. 48 kHz, 2채널 capture, 4채널 playback을 유지한다.
2. U7 공유 출력은 `S24_3LE`; CamillaDSP capture/playback은 `S32_LE`다.
3. FIR은 stereo float32 WAV이고 정식 자동 생성 결과는 32768 taps다.
4. 디지털 preamp/Gain을 넣지 않는다. Woofer trim과 전체 U7 하드웨어 볼륨은 별도 기능이다.
5. Factory/Speaker 기준 FIR을 의도 없이 다시 생성·정규화·덮어쓰지 않는다. 기준 SHA를 항상 확인한다.
6. 웹 선택 버튼으로 U7 하드웨어 Speaker/Headphones LED가 바뀌는 것처럼 표현하지 않는다. HID 실제 상태만 강조한다.
7. 볼륨 변경은 CamillaDSP restart, YAML 재생성, FIR mutation을 유발하지 않는다.
8. 고정/비상 IP를 만들지 않는다. DHCP 실패는 명확한 오류로 남긴다.
9. 측정음은 사용자가 시작할 때만 재생한다. 야간 기본값은 -42 dBFS이며 레벨 검사를 선행한다.
10. 단계 링크 이동, 그래프 보기, WAV/ZIP 다운로드만으로 저장 상태를 바꾸지 않는다.
11. MIMO는 Pi4/5만 활성화한다. Pi2 UI/engine/manager 우회를 모두 차단하고 SISO fallback을 보존한다.
12. 한 물리 우퍼의 stereo 입력을 두 독립 제어원으로 세지 않는다. MIMO 2.2는 서로 다른 위치·배선의 두 우퍼가 있어야 한다.
13. MIMO bank는 4 stereo float32 WAV × 32768 taps, 2×4=8 convolution, manifest SHA/self-validation PASS, physical-output row sum ≤1을 유지한다.
14. FIR/MIMO 가능, 부분 개선, 물리 처리, 미측정, 미인증을 결과 JSON/Markdown에 구분한다. 합성 예측을 실기 성공으로 기록하지 않는다.

## 변경 절차

1. 해당 파일의 Pi 2와 Pi 4/5 사본이 같은지 먼저 비교한다.
2. 공통 로직을 한 사본에서 수정하고 양쪽 최종 릴리스로 기계적으로 동기화한다.
3. Python은 `py_compile`, shell은 `bash -n`, writer는 `-ValidateOnly`로 확인한다.
4. 관리자 설정을 추가하면 default, normalize, strict backup validation, status, CLI, UI/API, matrix test, 문서를 모두 갱신한다.
5. Web route를 추가하면 정상/경계/잘못된 입력/동시 요청을 test matrix에 추가한다.
6. writer의 source marker 검증에도 새 핵심 기능을 추가한다.
7. `SHA256SUMS.txt`는 마지막에 다시 만들고 `.log`, `__pycache__`, `.pyc`, 기존 `SHA256SUMS.txt`는 제외한다.

## 실기 배포 안전

- 먼저 `pgrep -x camilladsp` PID와 활성 Speaker FIR SHA를 기록한다.
- Web/manager 변경만이면 `audiodsp-web.service`만 재시작한다. CamillaDSP는 재시작하지 않는다.
- 시작 래퍼 변경은 파일만 설치하고 재부팅 또는 별도 승인된 오디오 restart 때 적용한다.
- 볼륨 API 검증은 현재값과 같은 dB를 써서 갑작스러운 음량 변화를 만들지 않는다.
- 배포 후 PID, FIR SHA, `systemctl is-active`, `/api/status`, `/api/volume`을 다시 확인한다.
- 측정 engine 시험은 실제 측정음을 내므로 사용자 동의와 시간대 확인 없이 실행하지 않는다. 수치 self-test와 isolated matrix는 무음이다.

## 테스트 완료 조건

- 상태 진리표 총 4096 = valid + expected error
- 설정 operation 56개, ordered pair 3136개
- CamillaDSP가 생성된 모든 고유 config를 `--check`로 수락
- WAV 허용/거부 matrix, fallback, bypass, 2/4 convolution, preview/restore/apply, backup/rollback 통과
- 볼륨 GET/PUT/form, -60/0 경계, 형식·범위 오류, 물리 노브 divergence, 동시 쓰기 통과
- Pi 2 실제 장치에서 서비스 active, U7 8채널 동일값, Camilla PID/FIR hash 불변
- 두 writer의 `-ValidateOnly` 통과
- MIMO 세 토폴로지 무음 self-test, 실제 CamillaDSP 8-path parser, schema-v2 bank backup staging, Pi2 활성 차단 통과
- 실제 MIMO 수락은 Pi4/5 10분 CPU/XRUN과 별도 검증 위치 전/후 측정 없이는 완료로 표시하지 않음

## 문서 유지

동작을 바꾸면 `README`, `REQUIREMENTS`, `ARCHITECTURE`, `API`, `TESTING`, `CHANGELOG` 중 영향받는 문서를 같은 변경에 갱신한다. Pi 차이를 바꾸면 두 platform 문서와 두 릴리스 README도 갱신한다. 코드와 문서가 다르면 코드를 확인해 문서를 수정하되, 안전 요구사항과 충돌하는 코드라면 먼저 코드를 고친다.
