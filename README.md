# ApexFlow

Python 3.12+ 기반 Binance USDⓈ-M 공개 데이터 수집기와 **로컬 paper trading 기반**입니다.
실제 거래소 주문을 보내는 코드나 API 키 입력 기능은 없습니다. 기본 실행은 관찰만 수행합니다. 전략 판단·백테스트·AI·실거래는 아직 구현되지 않았습니다.
현재 운영체제는 macOS/Linux를 지원합니다(SQLite 단일 프로세스 보호에 `fcntl` 사용).

자세한 사용법은 [실행 안내](docs/runbook.md), 함수별 설명과 검토는
[소스코드 해설](docs/code-walkthrough.md)을 참조하세요.

## 시작

macOS + Python 3.14에서 설치는 성공했지만 `No module named 'apex_flow'`가 나오면,
아래의 [설치 경로 문제 해결](#설치-경로-문제-해결)을 적용한 뒤 테스트하세요.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v

# 오프라인 합성 데이터로 매수/매도 및 비용 계산 확인
.venv/bin/apex-flow --database data/demo.sqlite3 demo

# 실제 공개 시장 데이터만 60초 수집
.venv/bin/apex-flow collect --seconds 60

# 가상 계좌 생성/복구 및 실시간 관찰 (자동 진입 없음)
.venv/bin/apex-flow paper --seconds 60

# 명시적으로 요청하는 1회 가상 매수 요청 시점에서 10초 뒤 청산 시도
# quantity 단위는 기초자산 수량. 가격에 따라 max_notional로 거절될 수 있음.
.venv/bin/apex-flow paper --seconds 30 --exercise-quantity 0.0001 --hold-seconds 10

# 저장된 가상 포지션 reduce-only 청산 시도
.venv/bin/apex-flow paper --seconds 30 --close-position

# 실행 종료 후 계좌/최근 주문 확인
.venv/bin/apex-flow status
```

설치 전에도 오프라인 코어는 표준 라이브러리만으로 실행할 수 있습니다.
`PYTHONPATH=src python3 -m apex_flow --database data/demo.sqlite3 demo`

`--config configs/paper.toml`과 `--database`는 하위 명령 앞에 지정합니다.
명령은 지정 시간이 지나거나 Ctrl-C로 종료됩니다. 종료 시 미체결 주문은 취소하고
이미 체결된 가상 포지션은 저장합니다. 데이터가 끊긴 상태에서 가짜 청산을 기록하지 않습니다.
재시작 후 `--close-position`으로 잔여 포지션의 청산을 시도할 수 있습니다.
동일 DB를 사용하는 두 프로세스는 동시에 실행할 수 없습니다. 수집과 paper의 별도 동시 실행은 서로 다른 DB를 사용하세요.

## 현재 구현

- `/public`의 `depth20@100ms`, `/market`의 `aggTrade`, `markPrice@1s`를 별도 WebSocket 연결로 수신합니다.
- SQLite WAL에 원본 이벤트, 수신 시각, 주문 상태, 체결 내역, 계좌를 기록합니다. 이벤트 시각은 원본 payload에 있습니다.
- 상위 20단계 호가의 spread/imbalance/microprice와 최근 100개 aggTrade의 momentum/volatility/trade flow를 계산합니다.
- 연결 단절 시 해당 데이터 무효화, 재접속 지수 backoff/jitter, 연속 연결 실패 8회 시 종료합니다.
- 신규 매수는 두 연결과 depth/trade/mark 모두 최신이어야 합니다. reduce-only 매도는 public 연결과 최신 depth만 요구합니다. 오래된 호가로 청산을 만들어내지 않습니다. 로컬 시계 동기화가 필요합니다.
- 가상 주문은 지연 시간이 지난 **다음 수신 호가**의 가격·수량을 사용합니다. 보이는 호가 잔량까지만 체결하고 나머지는 취소합니다.
- 매수는 ask, 매도는 bid에서 시작하고 설정한 추가 slippage와 taker fee를 적용합니다. Spread는 호가 가격에 이미 포함됩니다.
- `client_id` 재요청은 기존 결과를 반환하고 다른 내용으로 ID를 재사용하면 거절합니다.
- 계좌와 주문 결과는 한 트랜잭션으로 저장합니다. 재시작 시 미완료 주문은 취소하고 포지션을 복구합니다.
- long-only, 최대 1배, 단일 종목·가상 계좌·동시 대기 주문 1개로 범위를 제한합니다.
- 신규 매수 때 최대 명목 노출, 현금, spread, 간이 일일 손실 기준을 검사합니다.
  UTC 날짜의 첫 처리 시점에 실현 PnL 기준을 저장하며, 매도 전에도 날짜를 갱신합니다.
  기준 이후 실현 PnL 변화에 현재 전체 미실현 PnL을 합산하므로 정확한 자정 equity 대비 손익은 아닙니다.
  손실 기준은 신규 매수 차단용이며 자동 손절이나 최대 손실 보장이 아닙니다.
  한도를 건드렸다가 회복하면 진입이 다시 가능하며, 당일 영구 차단 상태는 아직 없습니다.

`touch data/KILL`은 신규 매수를 차단합니다. `--close-position`은 이 상태에서도 허용됩니다.
파일 삭제로 차단을 해제할 수 있습니다. 이 파일 자체가 자동 청산을 의미하지 않습니다.
수수료 5bps와 slippage 2bps는 **편도 시뮬레이션 가정값**이며 실제 계정 수수료 등급의 조회 결과가 아닙니다.

## 아직 구현하지 않은 것

현재 결과로 전략 수익성을 평가하면 안 됩니다. `--exercise-quantity`는 연결/회계 검사이며 전략이 아닙니다.

- 테스트넷 계좌 조회/주문, 인증·서명, 실제 주문 idempotency/reconciliation, 사용자 데이터 스트림
- short/hedge 모드, 레버리지·마진·청산 가격·funding 계산
- 거래당 손실 제한, SL/TP, 변동성 circuit breaker, 연속 손실 제한, 실제 emergency close
- 전체 diff-depth + REST snapshot 동기화, 체결 큐·maker·주문취소 경합 시뮬레이션
- 백테스터, 학습/라벨링, 검증된 EV 전략, ML, GUI

상위 호가 스냅샷만으로는 큐 순위, 숨은 유동성, 순간 취소, 시장 충격을 재현할 수 없습니다.
새 스냅샷마다 유동성이 보충된 것으로 보는 가정도 낙관적일 수 있습니다.
현재 SQLite 쓰기는 이벤트 루프에서 동기 실행합니다. 초기 단일 종목용이며 이벤트 지연과 저장 처리량을
측정한 뒤 bounded queue + 전용 writer로 분리해야 합니다. DB 용량/보관 기간 관리는 아직 수동입니다.

현재 구조는 [설계 문서](docs/design.md), 결함·한계·개발 우선순위는
[관리자 검토 보고서](docs/review.md), 다음 구현의 완료 기준은 [개발 계획](docs/roadmap.md)을 참조하세요.
원본 시장 데이터와 최종 주문 결과는 DB에 저장하지만, 시스템 로그는 터미널 출력뿐입니다.
`orders`는 현재 상태를 덮어쓰며 모든 상태 전이를 보존하는 감사 로그는 아직 아닙니다.

## 공식 문서 확인 기준

2026-09-28에 아래 문서를 확인했습니다. 구형 `/stream` 단일 경로 대신 분리된 경로를 사용합니다.

- [Binance WebSocket 경로 개편 공지](https://www.binance.com/en/support/announcement/detail/ebf9b0aa9eca4ff3804eef6fb09ba32a)
- [USDⓈ-M Public streams](https://developers.binance.info/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/public)
- [USDⓈ-M Market streams](https://developers.binance.info/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market)

## 설치 경로 문제 해결

macOS에서 editable 설치 후 `ModuleNotFoundError: No module named 'apex_flow'`가 나면
프로젝트의 `.pth` 파일에 `hidden` 속성이 있는지 확인하세요. 이 파일은 설치된 Python에
`src/` 위치를 알려줍니다. Python 3.14는 숨김 `.pth` 파일을 읽지 않으므로 설치 성공 후에도
패키지를 찾지 못할 수 있습니다.

```sh
ls -lO .venv/lib/python*/site-packages/__editable__.apex_flow-*.pth

# 위 파일에 hidden이 표시된 경우, 해당 파일의 숨김 속성만 해제
chflags nohidden .venv/lib/python*/site-packages/__editable__.apex_flow-*.pth

.venv/bin/python -m unittest discover -s tests -v
.venv/bin/apex-flow --help
```

재설치가 `.pth` 파일을 새로 만들면 같은 문제가 재발할 수 있습니다.
가상환경을 삭제하거나 재생성할 필요는 없습니다. 파일 속성을 변경하지 않고 실행하려면
다음처럼 소스 경로를 직접 지정할 수도 있습니다.

```sh
PYTHONPATH=src .venv/bin/python -m apex_flow collect --seconds 60
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

기본 `demo` DB는 `data/demo.sqlite3`이며 실시간 데이터 DB와 별도로 유지합니다.
서로 다른 데이터 출처를 동일 DB에 기록하려 하면 거절합니다.
가상 체결에는 거래소의 LOT_SIZE/MIN_NOTIONAL 필터를 아직 적용하지 않으므로
소량의 연결 검사 주문이 실제 거래소에서도 유효하다는 뜻은 아닙니다.
