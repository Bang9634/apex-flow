# 계측 사용법과 해석 — 2026-09-29

API 키나 실주문 없이 `collect`/`paper`를 실행하면 자동으로 지연과 연결 상태를 기록한다.
기본 파일은 `logs/metrics-<run-id>.jsonl`이며 시작/종료 로그에 경로를 출력한다.

```sh
# PC 또는 OCI에서 동일하게 실행. 파일 이름은 실행마다 새것을 사용한다.
PYTHONPATH=src .venv/bin/python -m apex_flow collect \
  --seconds 60 \
  --metrics-output logs/metrics-pc-01.jsonl \
  --metrics-interval 10

# 가상 계좌 포함 관찰. 명시적 exercise 옵션이 없으므로 매매하지 않는다.
PYTHONPATH=src .venv/bin/python -m apex_flow paper --seconds 60

# 종료 후 마지막 보고서 읽기
 tail -n 1 logs/metrics-pc-01.jsonl | .venv/bin/python -m json.tool
```

`--metrics-output`은 하위 명령 뒤에 둔다. 기존 파일이면 오류로 종료해 덮어쓰지 않는다.
`--metrics-interval`은 1초 이상이며 기본 30초다. 지정 시간보다 실행이 짧아도 run/final은 기록한다.
시스템 로그는 여전히 터미널 출력이다. 계측 JSONL은 시스템 로그나 주문 감사 로그의 대체물이 아니다.

## 보고서 구조

- `run`: schema_version, 실행 ID, 시작 시각, 모드, OS/Python, 실제 DB 경로, 설정/실행 옵션.
- `metrics`: 주기적인 누적 집계와 최근 표본 백분위수.
- `final`: 종료 집계. outcome은 completed / cancelled / error.

Ctrl-C/정상 예외는 final 기록을 시도한다. 강제 kill, 전원 차단, 디스크 장애에는 보장하지 않는다.
보고서 기록 실패는 조용히 무시하지 않고 실행을 실패시킨다. 이미 발생한 원래 오류는 보존한다.

각 측정값의 count/mean/min/max는 실행 누적이다. window_p50/p95/p99는
**해당 지표의 최근 최대 4096개 관측값**에 대한 nearest-rank 백분위수다.
시간 윈도우가 아니며 전체 실행의 p99로 해석하면 안 된다. window_count와 count를 함께 본다.
표본이 1개면 모든 백분위수가 같은 값이다. 표본이 전혀 없으면 항목이 없으며 0ms로 만들지 않는다.

## 지표 의미

| 이름 | 단위/정의 | 해석상의 제한 |
|---|---|---|
| depthUpdate/aggTrade/markPriceUpdate.arrival_age_ms | Python에서 읽은 UTC 시각 − payload E | 시계 오차·서버 발행·네트워크·로컬 버퍼/루프 대기 포함. 순수 네트워크 지연 아님 |
| *.negative_age | 음수 arrival age 횟수 | 시계 불일치 등의 징후. 0으로 잘라내지 않음 |
| public/market.interarrival_ms | 연결에서 메시지를 연속해서 읽은 간격 | market은 trade/mark 혼합. 데이터가 조용한 것과 손실을 구분하지 못함 |
| *.receipt_silence_ms | heartbeat에서 마지막 적용된 해당 이벤트 수신 이후 경과 | 데이터가 없어진 구간을 관찰. 첫 수신 전에는 표본 없음 |
| public/market.ws_rtt_ms | websockets 자동 ping/pong의 최신 양방향 시간 | 실주문 응답 시간 아님. 약 20초 keepalive 이후 메시지 처리에서 새 값 수집 |
| public/market.connect_ms | 연결 시도부터 성공까지 | DNS/TCP/TLS/WS handshake 등을 포함. 성공한 연결의 표본만 기록 |
| processing.parse_ms | JSON 파싱과 기본 이벤트 분류 | 로컬 monotonic 측정 |
| processing.persist_ms | 원본 이벤트 저장과 SQLite 커밋 | 실제 병목 여부를 확인할 값 |
| processing.market_ms | Market.apply 처리 | 지표 전체 계산 시간은 아님 |
| processing.callback_ms | 수신 후 runtime 콜백 | paper/상태 출력 등을 포함 |
| processing.total_ms | 메시지 읽기 이후 위 처리 전체 | 타이머 호출·메모리 집계 오버헤드도 포함. 전송 지연은 제외 |
| processing.features_ms | 상태 출력 때 계산한 feature 시간 | 5초 주기 표본. paper 내부 spread 계산은 paper.step_ms에 포함 |
| processing.heartbeat_callback_ms | heartbeat의 콜백 | 수신 콜백과 구분 |
| paper.step_ms | 가상 주문 평가·저장 | 실제 거래소 주문 처리 아님 |
| paper.simulated_wait_ms | 매수/매도 제출부터 가상 체결까지 | 설정 지연+새 호가 대기. 실측 거래소 체결 지연으로 사용 금지 |
| loop.lag_ms | heartbeat 예정 대기 시간보다 늦게 깨어난 시간 | 로컬 부하/스케줄링 징후 |
| reporting.write_ms | 스냅샷 thread 쓰기 완료 대기 | final 자체의 쓰기 시간은 그 final에 포함하지 않음 |
| clock.jump_count / clock.jump_ms | 연속 샘플의 UTC 변화와 monotonic 변화가 250ms 넘게 다름 | 자동 NTP 보정이 아니며 시계 상태 경고용 |

RTT는 라이브러리의 최신 latency 값이 바뀔 때 기록한다. 중간에 메시지가 없다면 RTT 표본도
늦게 반영되거나 빠질 수 있다. 완전한 ping 이력 수집기가 아니다.
`connections`에는 연결별 상태와 누적 down_ms가 있다. 최초 연결 대기도 down에 포함한다.
연결 해제를 인지하기 전 무응답은 down_ms에 즉시 반영되지 않을 수 있으므로 freshness 지표와 함께 본다.
`health.unhealthy_samples`와 `health.unhealthy_ms_estimate`는 heartbeat 시점의
신규 진입 불가 상태다. 시작 준비 시간·데이터 부족·네트워크 단절을 모두 포함하며 정확한 장애 지속 시간은 아니다.
connection_attempts/connections/retries/network_errors/remote_closes와 accepted/ignored도 기록한다.

## 시간 모델과 DB 호환

`TimePoint(wall_ms, mono_ns)`로 시각을 전달한다.
UTC는 이벤트 시각 비교·로그·UTC 날짜에 쓰고, monotonic은 신선도 경과·주문 지연·보유 시간에 쓴다.
수신 당시의 arrival age에 monotonic 경과 시간을 더해 오래된 이벤트의 재활성화를 방지한다.
수신 시점의 UTC 오차는 여전히 영향을 주므로 PC/서버의 NTP 동기화가 필요하다.
시계 점프 감지는 진단이며 자동 교정이나 일일 회계 정책 해결을 의미하지 않는다.

DB events에 received_mono_ns와 run_id를 추가한다. 기존 이벤트와 계좌는 보존하며 옛 이벤트의
새 컬럼은 NULL이다. 합성 demo는 실측 계측이 아니므로 새 컬럼을 채우지 않는다.
monotonic 값은 같은 실행 안에서만 비교한다. 재시작·서버 간 절대 시간 비교에 사용하면 안 된다.
주문은 기존처럼 재시작 때 미완료 가상 주문을 취소하므로 이전 monotonic deadline을 복구하지 않는다.

## 오버헤드와 아직 없는 계측

매 메시지마다 파일 로그를 쓰지 않는다. 메모리 집계는 제한된 표본만 유지하고,
주기 보고서 파일 쓰기는 별도 thread에서 수행한다. 시장 원본 SQLite 쓰기는 여전히 동기식이다.
보고서 스냅샷 계산/상태 로그도 이벤트 루프에서 실행되므로 완전한 zero-overhead 계측은 아니다.
파일 rotation/용량 제한, NTP 상태 자동 조회, REST 시각 offset 보정, CPU/메모리 지표,
정확한 trade/depth sequence gap 판정, 실제 주문 ACK/체결 latency는 아직 없다.

같은 코드/설정/종목으로 PC와 OCI에서 비교하고, 서로 다른 시장 활동 시간의 결과를 단순 비교하지 않는다.
LLM을 같은 서버에 올릴 경우 활성/비활성 부하 조건을 나눠 측정한다.
짧은 smoke의 p99나 RTT 1개로 리전/서버 성능을 확정하지 않는다.

참고: [websockets keepalive/latency](https://websockets.readthedocs.io/en/stable/topics/keepalive.html).
실제 검증 환경은 Python 3.14.7 / websockets 16.1.1이며 계측은 이 설치 버전에서 확인했다.
