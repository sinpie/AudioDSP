# 변경 이력

## 2026-08-22 · 감쇄 상한 의미 통일

- Front 감쇄에 숨어 있던 500 Hz 이상 −6 dB/2 kHz 이상 −3 dB 고정 상한을 제거하고 `최대 룸 감쇄`를 전 대역의 유일한 절대 상한으로 통일
- 500 Hz 이상 peak 감쇄는 SNR·위치 편차·국소 peak 폭·독립 L/R 타깃 초과 일치도를 연속 신뢰도로 곱해 넓은 공통 peak는 충분히 줄이고 좁은/한쪽 peak만 완화하도록 변경; 결과 JSON과 Web 도움말에 정책을 기록
- U7 상단 버튼 이벤트 값 Headphone `0x30`/Speaker `0xA0`와 부팅 안정 `HIDIOCGINPUT` 값 Headphone `0x88`/Speaker `0xE0`를 분리 해석해 첫 부팅의 실제 출력 경로가 `unknown`으로 남던 문제를 수정
- U7 현재 상태 조회는 hidraw를 `O_RDONLY`로 열고 GET_REPORT만 사용하며 output/feature report를 쓰지 않는 전용 무음 회귀를 추가
- `최대 상대 보상` select의 누락된 닫기 태그 때문에 뒤의 `최대 룸 감쇄` 선택값이 드롭다운 밖에 노출되던 Web UI 오류를 수정하고 전체 select 태그 균형 회귀검사를 추가
- FIR 결과의 측정 출력 경로와 현재 U7 물리 출력이 다를 때 A/B 버튼이 비활성화되는 이유, 현재/필요 경로, U7 전환 방법과 자동 활성화 시점을 버튼 바로 아래에 표시
- 실제 측정 경로가 잘못 분류된 완료 세션을 음향 데이터/FIR 변경 없이 교정하는 `correct-output-profile` 관리 명령을 추가; 현재·세션·보고서 JSON을 함께 갱신하고 원본과 raw U7 selector 근거를 백업/감사 기록으로 보존
- Web 상태줄이 1분 load average 소수값을 CPU로 잘못 표시하던 문제를 수정; `/proc/stat` 차분 기반 실제 CPU 사용률을 계산하고 화면에는 읽기 쉬운 정수 `%`로 표시
- 현황의 `현재 FIR 보정 전달함수`에서 우퍼 곡선을 기본 표시하고, Light 테마 그래프를 밝은 배경·진한 축/범례 글자·고대비 L/R/우퍼 색상으로 수정

## 2026-08-21 · v24 MIMO 수학 안정화·측정 UI 연속성

- MIMO 정규화 행렬의 피벗 비율을 condition처럼 표시하던 진단을 실제 induced 1-norm condition number `||A||₁||A⁻¹||₁`로 교체
- 조건수 10,000 초과 주파수에 필요한 만큼만 diagonal loading을 자동 추가하고 중앙/P95/최대 조건수, 적용 bin 수와 최대 추가량을 결과 JSON·보고서·Web에 기록
- 한 band-limited actuator의 낮은 confidence가 측정 위치 전체를 제거하던 `min` 행 가중을 quadratic-mean row observability와 actuator별 uncertainty penalty로 분리
- 음수 Woofer trim을 support penalty와 실제 Rear 전달 상한에 이중 적용하던 경로를 제거하고 `보조 출력 사용 제한`과 `우퍼 최종 트림`의 수학적 역할을 분리
- MIMO 목표 phase를 Front 단독이 아니라 실제 배포 SISO+LR4 Front/Sub 합산 pressure로 계산하고, 제어원 coherence를 주파수별 공간 벡터의 P90으로 바꿔 지연 phase 회전을 독립성으로 오인하지 않도록 수정
- 평균 MAE·좌석 편차만 좋아지고 한 위치가 악화되는 해를 막기 위해 모든 측정 위치의 target MAE 비악화 gate 추가; 20 Hz–20 kHz 전체 결과 그래프에 세 위치 예상 상·하한과 위치별 MAE 표시
- `세션`을 1–6단계 선행 탭으로 분리하고 session 생성/검색/불러오기/삭제/MIMO 가용성을 한 화면에 배치; 활성 session 요약은 탭 위에 유지
- SISO FIR 화면에서 MIMO 옵션을 숨기고 MIMO mode에서만 전달행렬→안정화→8경로 흐름과 핵심 옵션을 표시; 모드별 측정 설명을 실제 계산식에 맞게 분리
- POST/A-B/계산 완료 뒤 현재 탭·스크롤을 복원하고 내부 FAIL 카드로 자동 focus/최상단 이동하지 않도록 navigation continuity 추가
- 프로필 설정을 자주 쓰는 동작/FIR/우퍼 출력으로 묶고 Profile→Engine→Backup 순으로 재배치; 화면 용어를 `A/B 미리듣기`, `결과 검토`, `처리 블록` 등으로 통일
- `세션`+1–6 실제 HTML을 렌더링하는 교차 플랫폼 Web smoke test를 추가하고 MIMO 19개 옵션, 세 토폴로지 기준, Pi2/3/4/5 materialization을 무음 PASS
- 수식, 합산 측정의 역할, 위상 동기 제한, UI 판단을 `MIMO_MATHEMATICAL_AUDIT_20260821.md`에 기록

## 2026-08-21 · v23 사후 검증 출력 안정화

- Pi5 v22 Preview 사후 검증에서 첫 L+Woofer만 30~130 Hz가 예상보다 5~18 dB 높았고 R+Woofer는 정상인 현상을 원본 UMIK 녹음, 입력 WAV, v21/v22 Front/Rear FIR FFT로 분리 분석
- v21/v22 Rear 좌우 FIR 차이는 20~200 Hz에서 대부분 0~2 dB이고 입력 sweep 좌우 크기도 정확히 같아, FIR 설계·채널 index 오류가 아니라 첫 독립 CamillaDSP stream 전환의 저역 오염임을 확인
- 문제 녹음의 sweep 전/후 noise floor가 -50.0/-75.93 dBFS로 25.93 dB 벌어졌고 `switching_transient_suspected=true`였는데도 큰 MAE를 확정 DSP FAIL로 처리하던 판정 오류 수정
- 사후 검증 파일의 시작 무음을 0.35초에서 2초로 늘려 U7/CamillaDSP 전환과 이전 출력의 감쇠를 실제 ESS 저역 시작 전에 끝내고, 추가 가청 sweep 없이 같은 reference로 deconvolution하도록 변경
- sweep 전/후 noise floor 차이와 실제 active-sweep transient를 분리한다. 전·후 바닥 차이만 있고 target/crossover/prediction이 모두 PASS면 유효 실측을 버리지 않으며, active sweep 오염 또는 바닥 차이와 응답 FAIL이 함께 있을 때만 `판정 보류 · 출력 전환 감지`로 표시한다.
- 완료된 사후 응답을 다시 소리 내지 않고 최신 판정식으로 계산하는 `reprocess-post-validation` 명령과 `저장 결과 재판정` 버튼을 추가
- 저장된 빠른 sweep 재분석이 실제 source별 재생 대역 대신 모든 채널에 15 Hz~22 kHz 기준을 쓰던 오류를 수정하고 Front 30 Hz~22 kHz, Woofer 15~320 Hz, 합산 15 Hz~22 kHz 및 측정 시 Woofer 감쇄·짧은 tail을 원 WAV와 정확히 일치시킴
- 사후 검증 SNR 안내가 별도 `검증 sweep 입력` 대신 본 측정 dBFS를 읽어 저장 세션에서 -30 dBFS를 -29 dBFS로 표시하던 오류를 수정하고, 실제 사후 입력값을 응답 계산·권장 상승 dB에 명시적으로 전달

## 2026-08-21 · v22 수학 감사·강건한 평균제곱 응답

- 다중 위치 결과가 `power-response prototype`이라고 기록되면서 실제로는 dB 산술평균을 사용하던 구현/문서 불일치를 수정하고, 위치·주파수 SNR 가중 평균제곱 전달응답 `10log10(Σw·10^(L/10)/Σw)`으로 변경
- fractional-octave 측정 응답 smoothing도 평균제곱 방식으로 변경하되, filter gain과 cut-only crossover guard는 dB-domain smoothing으로 분리해 안전 감쇄가 0 dB 쪽으로 약해지는 회귀를 차단
- 공간 표준편차를 prototype과 같은 center/SNR 가중치로 계산하고, 사후 Preview FIR 위치 통합도 설계와 같은 weighted power 방식으로 통일
- response estimator revision을 각 JSON에 기록하고 이전 응답은 이중 smoothing 없이 읽되 원본 WAV 무음 재계산을 3단계 UI에서 안내; 1초 UI poll은 mtime/size cache로 response JSON 반복 parse를 피함
- 공유 clock 환경변수가 `AUDIODSP_AUDIODSP_PHASE_CLOCK_SHARED`로 이중 prefix되어 override를 무시하던 오류를 `AUDIODSP_PHASE_CLOCK_SHARED`로 수정
- 표식이 전혀 없는 초기 response JSON도 이미 계산된 artifact로 취급해 절대 재평활하지 않고, 새 power-domain 계산은 원본 WAV 명시적 재처리에서만 수행하도록 호환 경로 보강
- L+Woofer/R+Woofer 대표 그래프·타깃 MAE·상대 지연 탐색을 개별 응답과 같은 위치/SNR 가중 mean-square 기준으로 통일하되, constructive-overlap 안전 guard는 측정 위치 최대값을 유지
- 상대 지연·극성 탐색이 큰 Woofer를 파괴적 상쇄해 타깃에 맞추는 후보를 선택하지 않도록 에너지합 대비 cancellation deficit P90 penalty와 0.25 dB 비악화 적용 gate 추가
- 저장 원본 재계산을 실제 시작할 때 이전 FIR 결과·사후검증·위상/합산 결과·완료 checkpoint를 즉시 무효화해 새 응답과 옛 Preview/Apply가 섞이지 않도록 상태 전이 수정
- MIMO `center` 모드가 150 Hz 이하에서도 중앙 위치를 60%로 고정하던 오류를 수정하고 solver 목표 phase·예측 그래프·공간 편차·MAE가 하나의 주파수별 위치/SNR 가중을 공유하도록 변경
- response grid 길이/증가성/위치 일치와 NaN/Inf를 계산 전 명시적으로 검증하고, 잘못된 confidence를 0~1로 제한해 조용히 잘못된 FIR을 만드는 경로 차단
- LR4 문서 식의 `x²` 오기를 실제 표준 4차 branch magnitude인 `x⁴`로 수정
- 0/0/+12 dB 세 위치의 기하평균 +4 dB와 평균제곱 약 +7.56 dB를 구분하는 exact unit test, 음수 합산 guard smoothing 비약화, 이전 response 재계산 UX 회귀를 추가
- 전체 SISO 수식, 가정, 자동검증, 적용 한계와 2026년 연구 근거를 `ROOM_TUNING_MATH.md`에 코드 단계와 1:1로 문서화

## 2026-08-21 · 공통 레벨 기준·광대역 roll-off·Walsh 위상

- L/R 각각과 Woofer를 따로 0 dB로 맞추지 않고 L/R 500~2,000 Hz median으로 하나의 측정·타깃 기준을 만든 뒤 L/R/Woofer 전체 결과와 판정에 동일 적용
- 완성된 4채널 FIR bank의 최대 전달값으로 common gain 한 번만 적용하고, branch별 적용 전/후 peak와 상대레벨 보존 오차를 영구 metadata 및 자동 core check로 추가
- `최대 룸 부스트`를 실제 동작에 맞는 `최대 상대 보상`으로 바꾸고 0/3/6/9/10 dB, 기본 10 dB를 제공; 가장 큰 신뢰 보상을 0 dB로 두고 전체 bank를 같은 값만큼 감쇄해 양의 FIR 이득과 preamp를 만들지 않음
- 양쪽 Front에 공통인 완만한 10–20 kHz roll-off는 edge SNR을 두 번 곱하지 않고 선택한 상대 보상 상한을 실제 ceiling으로 사용; 저장 실측 session에서 15 kHz는 L −0.51/R +0.01 dB까지 개선되고 20 kHz 잔여 L −3.38/R −4.54 dB는 10 dB 상한 한계로 명시
- 결과 UI와 보고 데이터에 전체 bank 공통 감쇄(`상대 보상의 음량 비용`)와 15–20 kHz 잔여 오차를 추가해, 더 평탄한 응답과 전체 재생 레벨 손실의 trade-off를 숨기지 않음
- 사후 FIR 검증이 L/R을 각각 0 dB로 재정규화하고 계산 예상과 직접 비교하지 않던 오류를 수정; 측정/예상 각각 하나의 공통 L/R 기준, 전체·crossover 예상↔실측 MAE/P90과 20 Hz~20 kHz 세 곡선 비교를 추가
- Pi5 실제 v21 사후 검증에서 전체 Flat target과 예상 일치는 PASS였으나 낮은 SNR의 L 50~200 Hz만 경계 초과함을 분리 확인; FIR 공통 감쇄 뒤 실제 검증음이 낮아지는 점을 UI에 명시하고 28초 ESS/-25 dBFS 재검증과 `inconclusive_low_snr` 상태를 추가
- 같은 Pi5/마이크 위치에서 -25 dBFS·28초 ESS 재검증을 실행해 최소 SNR 14.29 dB, 전체 예상↔실측 MAE/P90 L 1.373/3.000 dB·R 1.494/3.049 dB와 50~200 Hz L 1.389/2.660 dB·R 1.569/3.400 dB로 target/crossover/prediction 전체 PASS 확인; 기존 정식 FIR SHA와 입력·볼륨 복원 확인
- `검증 초기화`가 사후 상태가 덮어쓴 `fail_measured`를 모델 판정으로 재사용하던 오류를 component check 기반 복원으로 수정하고 session/report/UI를 함께 갱신; 사후 PASS·SNR 권장 미달·audit 분류를 사용자 용어로 통일하고 완료 후 재실행 순서를 바로 표시
- 오래 열린/부분 폼이 `max_cut_db`를 누락해 400을 내던 문제를 마지막 저장 설정 fallback으로 수정하고, 작업 완료 후 POST 경로를 GET해 404가 되던 자동 갱신을 항상 `/measure`로 복귀하도록 수정
- 양쪽 Front에서 공통으로 나타나는 2~20 kHz 광대역 감쇄만 제한적으로 보상하고, 한 채널의 좁은 deep/null은 양쪽 ±1/6 octave 형상과 공간 신뢰도로 억제해 최대 3 dB를 넘지 않도록 수정
- 새 결과의 자동 검증에 `하나의 L/R/우퍼 레벨 기준`, `완성 bank common gain`, `branch 상대레벨 보존`, `최대 상대 보상`, `좁은 null boost 보호`를 추가하고 UI 오류 안내를 `4 · FIR 계산`의 실제 메뉴명과 조치 순서로 통일
- L/R/W를 한 녹음에서 같은 주파수로 분리하는 4상태 Walsh 기준을 추가하고 각 상태의 시작·끝 guard를 제외한 4회 반복만 평균; 별도 ESS clock drift를 거리로 쓰지 않으면서 L/R/W 상대위상·극성·지연과 L+W/R+W cross-term을 결합
- MIMO 비교 기준이 Front-only였던 오류를 찾아, 실제 배포되는 LR4 Front+sub SISO 경로를 baseline으로 사용하도록 수정; 한 우퍼 해의 안전 blend도 보수적으로 제한
- 무음 회귀에서 target/preset 18조합, SISO 설정 95시나리오, MIMO 19시나리오, 4096 Web/Profile 상태, Pi5 FFTW·8-path Camilla parser·5.1 dual-sub 42경로 메모리 계획을 검증

## 2026-08-20 · 빠른 ESS·독립-clock 합산 안전성·UI 반응성

- 모든 빠른 검사·본 측정·합산·사후 검증 sweep의 dBFS를 평상시 U7 볼륨과 독립된 DAC 기준으로 변경; 입력 OFF 후 PCM 0 dB read-back, sweep 프로세스 종료 후 원래 볼륨 read-back, 그 뒤에만 입력 복귀
- 측정 오디오 lock과 profile manager mutation을 연결해 sweep 중 Web/API 볼륨·프로필·DSP 변경을 거부하고, 볼륨 복원 실패 시 입력을 계속 차단하는 fail-closed 회귀시험 추가
- 백색소음 UI와 독립 레벨 값을 제거하고 현재 측정 구성의 모든 출력을 본 측정과 같은 ESS·라우팅·유효대역 SNR 계산으로 각 2초 검사하도록 통일
- 빠른 검사와 본 측정의 사용 가능 하한을 모두 6 dB로 통일하고 6–15 dB는 적용 가능한 권장 경고, 15 dB 이상은 권장 PASS로 분리; 입력 peak -6 dBFS 여유 안에서 올릴 수 있는 정확한 dBFS를 안내
- 저장된 원본 5종 녹음 재계산과 응답 계산 batch 경로를 추가하고 우퍼의 무음 고역이 전체 SNR을 낮추지 않도록 지속 -3 dB 통과대역만 평가
- 빠른 2초 ESS와 긴 본 스윕의 판정 물리량을 일치시키고, 본 스윕에는 2초 기준 matched-filter coherent integration 이득을 반영; 14초는 +8.451 dB이며 원신호/적분/유효 SNR을 UI에 분리 표시
- 좁은 T5S 통과대역의 유효 chirp가 약 0.16초뿐인 실측을 반영해 Woofer 최소 검출 구간을 50 ms로 조정하고, 프런트 timing은 예상 ALSA 범위 안의 고정 1초 중역 signature FFT correlation으로 transient 오검출을 차단
- 저장된 빠른/본 스윕을 소리 없이 다시 판정하는 경로, worker 권한 안전 상태 확인, source 전환 시 stale 품질 카드 제거를 추가
- U7 재생과 UMIK-1 캡처가 기본적으로 하드웨어 clock을 공유하지 않는 점을 반영해 서로 다른 sweep의 절대 복소 위상·상대 delay를 사용하지 않도록 수정
- 독립-clock SISO는 위상 비의존 에너지 타깃 MAE/P90과 최악 동상 `|Front|+|Woofer|` 감쇄 전용 상한을 모두 통과해야 적용 가능; clock drift가 만든 약 150 Hz 가짜 복소 딥은 그래프·딥 진단에서 제외
- 정밀 L/R/우퍼/L+우퍼/R+우퍼 측정은 독립 정규화 없이 절대 전달 크기 closure를 FIR 전에 검증하고, 위상 제한은 FAIL이 아닌 명시적 권장 경고로 분리
- Pi4/5 MIMO 계산 능력과 측정 위상 유효성을 분리하고, 검증된 공통 timing reference가 없으면 production MIMO 생성·활성화를 차단; 합성 MIMO 회귀는 shared-clock fixture로만 실행
- FIR 완료·위치 완료·오류·결과 SHA 변경을 `result_token`으로 감지해 측정 화면을 한 번만 즉시 갱신하고 1초 전체 새로고침을 제거
- 결과 그래프 기본 범위를 20 Hz–20 kHz로 복원하고 20–250 Hz 저역 확대를 별도 버튼으로 제공; 합산 실측은 5단계의 선택 검증으로 노출하되 정식 적용 필수 조건으로 만들지 않음
- 우퍼 독립 LPF branch를 전체 타깃과 비교하던 false FAIL을 N/A로 고정하고 최종 L+우퍼/R+우퍼 합산만 타깃 판정; `fail_target`은 실제 합산 signed median 방향과 메뉴의 트림·억제·크로스오버 조치를 안내
- 자동 검증의 PASS/FAIL/권장/대기/해당 없음, MAE/P90 설명, 1–6단계 바로가기, 오류 빨간색, 스텝 탭, 세션 주석·이어하기·삭제를 한 화면 흐름으로 정리
- 합성 측정 엔진 PASS, 6 target×3 preset 및 94개 SISO 옵션 PASS, shared-clock MIMO 19개 수치 시나리오 PASS, 5.1+dual-sub 42경로 메모리 계획 530 MiB와 실제 배열 peak 138.64 MiB PASS
- Pi2 실제 Fast 1위치 5경로 수락 시험에서 -29 dBFS 빠른 SNR 7.12/8.80/7.67/14.32/12.30 dB와 14초 유효 SNR 11.45/7.42/25.98/20.93/24.45 dB PASS; Flat/없음/0 dB/100 Hz와 Harman/없음/0 dB/120 Hz FIR PASS, 정식 FIR SHA 불변
- 비유한 Python `Infinity`/`NaN` 때문에 브라우저가 측정 status 전체를 폐기해 결과 그래프가 비던 문제를 수정하고, 모든 API JSON을 RFC 호환 `null`로 강제; Pi2 실제 브라우저에서 20 Hz–20 kHz SVG graph 갱신 확인
- 현재 session 한 개만 size/SHA-256 manifest와 함께 export/import하는 Pi4/5 migration 경로를 추가하고 경로 traversal·symlink·중복 ID·불완전 archive를 차단
- Pi4/5 writer가 Windows 저장 WLAN profile을 key 비노출 상태로 읽는 옵션과 migration archive의 기록 후 hash 검증을 지원

## 2026-08-19 · 구조·보정·Pi 5 2 GB 검증

- 공통 runtime/assets/tests를 `source/common` 한 사본으로 통합하고 Pi2/3/4/5 overlay manifest와 deterministic `build/<platform>` materializer를 추가; release의 중복 payload/docs/tests/legacy tree 제거
- 두 SD writer가 기록 직전 canonical source를 조립하고 전체 byte 일치, image/Camilla/FIR hash와 정책 marker를 검증하도록 변경
- 제품·서비스·환경변수 식별자를 AudioDSP/audiodsp로 통일하고 이전 environment fallback을 제거
- 기본 보정 기준을 target + 저역 억제 `none` + Woofer trim `0 dB`로 고정; Strong/Primus/음수 trim을 기준 튜닝 이후 의도적 추가 감쇄로 분리
- magnitude/phase/crossover 각 단계의 채널별 peak 정규화가 Front/Woofer 상대레벨과 LR4 합산을 바꾸던 문제를 제거하고 전체 FIR bank에 공통 normalization 한 번만 적용
- 측정 출력 dBFS와 임시 Woofer 감쇄가 전달함수 크기를 바꾸지 않는 scale-relative regularized deconvolution 회귀 추가
- 권장 정밀 측정 `L/R/W/L+W/R+W`를 추가해 FIR 계산 전에 절대 복소합 closure를 검증하고, L+W/R+W를 보정·평균·독립 normalize하지 않도록 고정; 통과 시 사후 sweep 제거
- 표준 L/R/W와 MIMO의 복소 전달함수 적용 게이트를 도입했으며, 2026-08-20에 U7+UMIK 독립-clock 현실을 반영해 SISO 안전 상한과 MIMO 공통 timing reference 조건으로 보강
- 실제 FIR 사후 L+Woofer/R+Woofer 합산, Fast 1위치 N/A semantics, Standard 3위치 안정성, 18 target/preset 조합과 94개 SISO UI 옵션 시나리오를 실제 32768탭 무음 합성 fixture로 검증
- phase 신뢰 시 crossover guard를 이론적 `|Front|+|Woofer|` 상한이 아니라 세 위치 실제 복소합 최대값으로 변경하고, 저역 극성 ±/상대 지연 robust 탐색 추가; phase 불신뢰 때만 보수적 상한 fallback
- MIMO가 미정규화 SISO 중간값과 headroom 제한 후 bank를 비교해 false FAIL을 만들던 오류를 수정; deployable SISO bank 공통 정규화, 한 reference-band target-shape 평가, 실제 global 감쇄 분리 보고 추가
- Flat/추가 억제 없음/trim 0 dB 기준에서 MIMO Stereo/2.1/2.2 모델 PASS, MIMO UI 19개 bank 구조 PASS와 비기준 5개 modal-tail 안전 차단 검증
- 자동 진단 FAIL/PENDING/N/A 안내를 실제 1–5단계, select, 실행 버튼명으로 통일하고 MIMO core별 조치 추가
- active session 삭제·idle 복귀·정식 FIR 불변·중복 삭제 거부를 Web matrix에 추가
- Web이 알고리즘 revision을 따로 하드코딩해 새 결과를 stale로 오판하던 오류를 제거하고 measurement engine을 단일 revision 원본으로 사용
- 4096 profile 상태, 3136 ordered setting pairs, 28 Camilla config, 33 concurrent writes와 전체 UI/API/backup/preview 경로 재검증
- Pi 5 2 GB 계획 검증 추가: 현재 8경로 runtime/생성 46/309 MiB, 5.1+dual-sub 완전 dense 42경로 135/530 MiB, 64-bit 실제 배열 allocation peak 138.64 MiB
- MIMO 생성기가 path spectrum을 causalization 전에 해제하고 bank scaling을 in-place로 수행해 향후 5.1 생성 피크를 제한
- Woofer 독립 LPF branch를 full-range target과 비교하던 MAE/P90 false FAIL을 제거하고, 실제 Front+Woofer 합산 target 오차와 crossover 중앙 오차를 별도 진단으로 표시
- MAE/P90의 의미, 높음/낮음 방향, 실제 메뉴 `4 · FIR 계산`의 수정 control과 `3 · 위치 측정` 재측정 대상을 FAIL 카드에 직접 안내
- 390×844/1440×1200 실제 Chrome CDP 회귀를 추가해 1–6 탭, disclosure, 자동 새로고침 없음, 가로 overflow 없음과 앱/단계 탭의 구별을 검증
- production Pi 2를 `audiodsp-pi2`/`audiodsp`/`audiodsp-ethernet`으로 단계 이전하고 새 SSH·sudo 확인 뒤 이전 식별자를 활성 경로에서 제거; CamillaDSP PID/FIR/볼륨 보존

## 2026-08-18 · v1.2 유지보수 revision

- 활성 session 요약/주석을 1–6 탭 위로 이동하고 자동 저장된 session 목록, 주석과 함께 불러오기, 완료 checkpoint 전체 복원, 누락 artifact 차단을 추가
- 자동 검증을 PASS/FAIL/N/A 체크리스트로 바꾸고 모든 FAIL에 재측정·설정·배치 해결 가이드를 추가
- FIR 계산 중에도 선택 옵션을 그대로 표시하되 fieldset만 잠그고, 상단 앱 탐색과 내부 단계 탭의 강조색을 분리
- L/R/Woofer 개별 보정에 기본 ON/100 Hz Linkwitz–Riley 4차 디지털 crossover를 추가하고 Front HPF·Woofer LPF를 기존 32768탭 WAV에 내장해 추가 runtime filter와 block latency를 0으로 유지
- 기존 `correction-preferences.json`에 crossover 키가 없어도 Web/API가 마이그레이션 기본값 `ON/100 Hz`를 병합해 표시·백업하고, 사용자가 저장할 때 정규화된 새 스키마로 유지
- 독립 branch 설계 뒤 세 위치의 실제 복소합과 최악 구성 상한을 다시 계산하고, target 초과만 두 branch 공통 cut-only guard로 억제; phase 불신뢰·합산 target 실패를 acoustic PASS로 오표시하지 않도록 수정
- 독립 HPF/LPF가 불가능한 L+Woofer/R+Woofer 공유-filter 모드에서 crossover ON을 명시적으로 거부
- MIMO 전달행렬에 LR4 branch spectrum, 주파수별 측정 confidence, 사용자 보정 하한, 실제 Woofer output trim transfer 제한을 반영
- MIMO `modal_tail`을 실제 잔향 예측이 아닌 평활 전달함수 impulse-tail proxy로 명확히 고치고 1.5 dB 비퇴행 gate와 fail-closed 합성 검증 적용

- 현황 화면에 입력→DSP→라우팅→U7 selector→스피커 체인의 반응형 SVG signal console을 추가하고 설정 카드를 두 개의 실제 스피커 출력 체인으로 명확히 표시
- 레벨 검사 시 U7 물리 출력 경로를 session에 고정하고 sweep 재생 전·중·후 불일치 중단, 측정 경로 전용 Preview/Apply, 다른 profile 덮어쓰기 차단 추가
- profile matrix에 silent session 생성/재설정, 보고서 다운로드, 경로별 Preview/Apply 거부와 signal-flow UI marker 검증 추가
- 자동 room-EQ cut 포화가 bass/treble 사용자 선호도를 소거하던 문제를 수정하고, 명시적 house curve를 자동 제한 뒤 적용하며 두 correction 성분을 보고서에 분리
- UMIK 장시간 녹음의 ALSA overrun을 성공으로 오인하지 않도록 `arecord --fatal-errors`를 적용해 해당 측정을 즉시 실패·재시도 대상으로 처리
- 대역 제한 Woofer ESS의 10.8초 고조파/잡음 peak를 direct delay로 오인하던 문제를 발견해 0~250 ms 인과 gate, phase/decay fallback, 부분 상대지연 금지, `time_alignment_safe` 검사를 추가
- 실측 한 세트로 모든 UI SISO 값을 one-factor-at-a-time 생성하는 67개 FIR matrix와 134개 저음량 합산 sweep 검증 도구 추가
- 백색소음/sweep 안전 기본값을 모두 -42 dBFS로 통일하고 독립 slider, 높은 출력 실시간 경고, 실제 sweep 전 수치 confirm 추가
- Woofer SNR을 고정 전대역 대신 chirp-time 지속 -3 dB 통과대역과 pre/post noise PSD로 판정하고 순간 생활소음 confidence 적용
- 첫 UMIK/ALSA cold-start가 고정 pre-roll을 소비해도 실제 sweep 활성구간과 capture delay를 자동 복구하고, 검출 구간 밖에서만 noise PSD를 계산하도록 수정
- Front/Woofer 정렬에 음향 bulk delay와 FIR 에너지 지연을 함께 사용하고 L/R 공통 phase·magnitude 보존 자동 축소 추가
- FFTW plan/buffer 재사용, Pi별 offline ETA, PID/cmdline 기반 중단 worker 복구 추가
- MIMO에 상대 bulk-delay phase 복원, 기존 SISO 저역 레벨 anchor, 인접-bin continuity, 안전 해 blend, modeled impulse-tail 적용 차단 추가
- 백업 파일명을 고유화하고 검증 실패·교체·취소·적용 시 restore staging 추출 디렉터리 누수 제거
- 위 항목을 SD writer 필수 marker와 profile/measurement/MIMO 회귀시험에 추가
- 2026년까지의 MIMO/weighted pressure matching/excess-phase/공간 보간 연구 재검토와 채택·보류 근거 문서화
- Pi4/5 MIMO Stereo/2.1/2.2, 2×4/8-path 32768탭 bank와 SISO 전이 구현
- 주파수별 physical-output headroom 투영, 공통 인과 지연, 제어원 coherence·예측 비퇴행 검사 추가
- Pi2 MIMO 측정/UI/runtime 차단, 한 stereo-fed T5S를 한 물리 제어원으로 강제
- MIMO Preview/Apply/rollback, schema-v2 bank 백업/복원 staging 추가
- 모든 결과에 보정 가능·제한·물리 처리·미측정·미인증 JSON/Markdown 보고서 추가
- 세 토폴로지 합성, 실제 CamillaDSP 8-path parser, backup staging 무음 회귀시험 추가

- 측정 재생을 검증된 `audiodsp_announce` 4채널 경로로 통일
- Woofer 측정/reference를 같은 조절 가능 비율로 감쇄하고 sweep별 SNR gate 추가
- octave noise-compensated Schroeder EDT/T20 및 저역 장시간 공진 cut-only 제어 추가
- 실제 32768탭 FIR FFT 기반 target-fit/구현오차/전달이득/impulse 셀프검증 추가
- Pi 2 14초 4채널 sweep WAV 생성을 약 50초에서 17초로 단축
- 6 target × 3 preset × Front/Woofer 무음 행렬과 잔향 합성 회귀시험 추가
- U7 전역 PCM 출력 볼륨을 Web UI와 API에서 읽고 쓰는 기능 추가
- 실제 하드웨어값과 재부팅 저장값을 분리 표시
- -60~0 dB 정수 제한, 8채널 동일 raw mapping, 물리 노브 변화 polling
- 볼륨 변경 시 CamillaDSP/FIR 무재시작 보장
- 부팅/USB reset 후 `profile-settings.json`의 볼륨 복원
- full backup strict schema에 `output_volume_db` 추가; 이전 backup은 -10 dB로 보완
- profile matrix에 volume operations, API/form/invalid/physical/concurrent 시험 추가
- Pi 2와 Pi 4/5 writer preflight에 volume marker 추가
- 재현용 README, PLAN, ARCHITECTURE, AGENTS, API, DSP, 플랫폼, 시험, 보안 문서 작성

## 2026-08-18 · v1.2.0 정리

- PC/모바일 UI 정밀 점검 후 dark primary 대비, 현재 탭 indicator, contextual title, skip link, `aria-current`, alert/live status, 파일 입력 label, focusable graph region, 40/44 px control 높이, tabular numeral을 보강
- 중복 낭독을 막기 위해 텍스트 옆 SVG 아이콘을 장식 요소로 바꾸고 실제 응답 SVG의 설명 semantics는 유지
- 측정 전 `None`/API `null` 표현 차이로 측정 화면이 매초 새로고침되던 상태 비교를 동일한 빈 token으로 정규화
- 측정 workflow를 PC 한 줄·모바일 3×2의 실제 6단계 tab/tabpanel로 바꾸고 선택 단계만 바로 아래에 표시; 탭 이동은 비파괴이며 미래 단계는 선행 조건 안내를 표시
- 모든 펼침 영역에 CSS-vector chevron, 48 px 클릭 행, 열림 상태 강조를 추가해 정적 카드와 disclosure control을 구분
- polling 중 브라우저 이동·종료로 생기는 정상적인 client disconnect를 서버 오류로 기록하지 않도록 BrokenPipe/ConnectionReset 처리를 보강
- 결과 카드에서 FIR의 `Woofer 최종 trim`과 sweep용 `측정 시 Woofer 감쇄`를 분리해 0 dB 계산 결과가 −9 dB로 오인되던 표시 오류를 수정
- Preview 중 저장 mode가 아니라 실제 임시 CamillaDSP의 copy/separate·2/4ch·FIR 경로를 `/api/status`와 Web에 표시하고 양방향 복귀를 검증
- FIR 전달함수와 목표 청취 음압을 화면에서 명확히 구분하고 결과 `algorithm_revision`을 추가; 이전 계산의 Preview/Apply와 self-validation 실패 결과의 정식 Apply를 UI·엔진 양쪽에서 차단
- UI/코드의 제품명을 AudioDSP로 통일
- 새 설치 hostname/user/service/state 식별자를 `audiodsp`로 변경
- Pi 2와 Pi 4/5 최종 릴리스 디렉터리 분리
- 단계형 측정 UI, dependency-aware invalidation, staged WAV, full backup/restore 완성
- multi-position 32768-tap FIR engine, target/preset, phase/시간 정렬, browser SVG 완성
- Pi 2 exhaustive/measurement 실기 시험과 bundle validator 추가

## 이전 기준

이전 개발·장애 대응 자료는 Git 이력에 보존한다. 현재 source/release tree에는 이전 제품 식별자를 남기지 않으며, 이력 파일을 새 릴리스 입력으로 사용하지 않는다.
