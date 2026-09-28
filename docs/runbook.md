# 실행 안내

현재는 공개 시세 수집과 로컬 가상 거래만 지원한다. API 키 입력·실계좌 주문·AI 전략 명령은 없다.
`.venv`는 Python 의존성 격리이며 가상 거래 계좌와는 별개다.

## 1. 실행 준비

프로젝트 루트에서 실행한다. 설정과 DB의 상대 경로는 현재 작업 디렉터리를 기준으로 한다.

```sh
cd /Users/bangdeokjae/Desktop/develop/apex-flow

# .venv가 없다면 한 번만 생성
python3 -m venv .venv
.venv/bin/python -m pip install -e .

# 선택 사항: 활성화. 아래 명령들은 활성화 없이도 실행 가능하다.
source .venv/bin/activate
```

이미 설치했다면 매번 가상환경 생성/설치를 반복하지 않는다. editable 설치에서는 소스 수정이
다음 실행에 반영되며, 의존성이나 패키지 설정을 바꿀 때 재설치를 고려한다.
아래 명령은 이 환경의 hidden `.pth` 문제를 피하도록 `PYTHONPATH=src`를 명시한다.
전역 환경변수를 바꾸지 않고 해당 명령에만 소스 경로를 지정한다.

## 2. 테스트

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

현재 25개 테스트다. 외부 Binance 연결 없이 가상 계좌·장애 처리·CLI 연결 흐름을 검사한다.
테스트 통과는 전략 수익성 또는 실주문 안정성 증거가 아니다.

## 3. 오프라인 demo

```sh
PYTHONPATH=src .venv/bin/python -m apex_flow demo
PYTHONPATH=src .venv/bin/python -m apex_flow --database data/demo.sqlite3 status
```

코드가 만든 가격으로 0.5 수량의 매수/매도를 시도하고, 결과와 계좌를 출력한다.
기본 DB는 `data/demo.sqlite3`다. 반복 실행은 기존 잔고를 이어 사용한다.
가격은 실제 BTC 시세가 아니며 기본 비용 설정에서는 가격 상승에도 순손실이 발생한다.
설정을 바꾸면 주문이 거절될 수 있다. 기존 포지션이 남은 DB에서는 demo를 시작하지 않는다.

새 가상 계좌로 실험하려면 기존 DB를 지우기보다 사용하지 않은 경로를 지정한다.

```sh
PYTHONPATH=src .venv/bin/python -m apex_flow --database data/demo-new.sqlite3 demo
```

## 4. 실시간 데이터만 수집

```sh
PYTHONPATH=src .venv/bin/python -m apex_flow collect --seconds 60
```

Binance 공개 BTCUSDT 체결·상위 20호가·mark price를 수신하고 원본을 기본 DB
`data/paper.sqlite3`에 저장한다. 지표도 계산하지만 주문이나 가상 계좌를 새로 만들지는 않는다.
같은 DB에 이전 계좌가 있어도 collect는 그 계좌를 관리하지 않는다.
약 5초마다 터미널에 상태가 나온다. 기본 실행 시간은 60초이고 최대 86400초다.

## 5. 가상 계좌를 포함한 실시간 관찰

```sh
PYTHONPATH=src .venv/bin/python -m apex_flow paper --seconds 60
```

처음에는 가상 현금 10000 USDT로 시작하며, 다음 실행부터 저장된 계좌를 복구한다.
이 명령만으로는 자동 진입·손절·익절하지 않는다. 이전 포지션도 명시적 청산 요청 없이는 유지한다.

## 6. 실시간 시세에서 가상 왕복 검사

```sh
PYTHONPATH=src .venv/bin/python -m apex_flow paper \
  --seconds 30 \
  --exercise-quantity 0.0001 \
  --hold-seconds 10
```

1. 데이터가 정상 상태가 되면 0.0001 BTC 가상 매수를 한 번 요청한다.
2. 주문 지연 이후 들어온 새 호가로 체결 또는 거절을 결정한다.
3. **매수 요청 시각**에서 10초가 지나면 잔여 보유 수량 청산을 시도한다.
4. 왕복 완료 후 추가 매수하지 않고 종료 시각까지 관찰한다.

반복 매매 전략이 아니다. 기존 보유 수량이 있으면 검사를 시작할 수 없으며,
매수가 거절되어도 새 매수는 반복하지 않는다. 매도 일부만 체결되면 남은 수량의 매도를 시도한다.
실제 Binance 주문은 발생하지 않는다. 수량/최소 금액 등 실제 거래소 필터는 아직 반영하지 않는다.

## 7. 잔여 가상 포지션 청산

```sh
PYTHONPATH=src .venv/bin/python -m apex_flow paper --seconds 30 --close-position
```

최신 public 호가가 들어오면 보유분만 매도한다. trade/mark 피드만 끊겨도 호가가 유효하면
청산할 수 있지만, 호가가 오래됐거나 collector 자체가 중단되면 청산을 완료할 수 없다.
`--close-position`과 `--exercise-quantity`는 동시에 사용할 수 없다.

## 8. 신규 매수 차단과 종료

```sh
# 기본 설정의 신규 매수 차단 파일 생성. 별도 터미널에서 실행할 수 있다.
touch data/KILL

# 차단 해제
rm -f data/KILL
```

파일은 신규 매수 차단만 의미하며 자동 청산이 아니다. 기존 보유분 청산 명령은 허용한다.
사용자 지정 config의 kill_file을 변경했다면 그 경로를 사용한다.

Ctrl-C 또는 지정 시간 종료 시 대기 주문을 취소하고, 이미 체결된 가상 포지션은 저장한다.
WebSocket 연결 정리 때문에 몇 초 더 걸릴 수 있다. 프로세스 재시작도 미완료 가상 주문을
취소한다. 실제 거래소의 주문 취소/복구 정책으로 그대로 사용할 수 있는 기능은 아니다.

## 9. 설정·상태·로그

```sh
PYTHONPATH=src .venv/bin/python -m apex_flow \
  --config configs/paper.toml \
  --database data/experiment-01.sqlite3 \
  paper --seconds 60

# 실행을 종료한 뒤 같은 DB 조회
PYTHONPATH=src .venv/bin/python -m apex_flow \
  --database data/experiment-01.sqlite3 status

# 도움말
PYTHONPATH=src .venv/bin/python -m apex_flow --help
PYTHONPATH=src .venv/bin/python -m apex_flow paper --help
```

`--config`/`--database`는 하위 명령 앞에, `--seconds` 등은 하위 명령 뒤에 놓는다.
동일 DB로 실행하는 프로그램은 한 번에 하나만 가능하므로 status도 수집/paper 종료 후 실행한다.
새 DB에서 status만 실행하면 계좌는 null이다. status는 DB 열기/스키마 준비를 하므로
없는 경로를 오타로 지정하면 빈 DB가 생성될 수 있다.

기본 설정:

| 항목 | 값 | 의미 |
|---|---|---|
| symbol | BTCUSDT | 단일 종목 |
| initial_cash | 10000 | 최초 가상 현금. 기존 계좌에 추가 입금하지 않음 |
| max_notional | 100 | 신규 매수 시 허용 노출 금액 |
| max_daily_loss | 20 | 간이 손실 기준의 신규 매수 차단선. 최대 손실 보장 아님 |
| max_spread_bps | 10 | 신규 매수 spread 상한 0.1% |
| taker_fee_bps | 5 | 편도 수수료 가정 0.05% |
| slippage_bps | 2 | 편도 추가 슬리피지 가정 0.02% |
| latency_ms | 100 | 가상 지연. 실측값 아님 |
| stale_ms | 2000 | 데이터 신선도 기준 |

같은 계좌 DB의 symbol/initial_cash를 바꾸면 오류가 난다. 새 DB로 실험을 분리한다.
그 외 설정 변경은 허용하지만 아직 실행별 설정 스냅샷이 저장되지 않으므로 별도 DB/설정 파일로
비교 실험을 분리하는 것이 재현에 유리하다. 합성/실시간 데이터 출처 혼합은 거절한다.

`healthy`는 신규 진입에 필요한 데이터 상태이며 전략 품질이나 수익 가능성을 뜻하지 않는다.
`cash`는 현금, `quantity`는 보유량, `basis`는 수수료 제외 평균 매입가,
`realized`는 수수료를 즉시 차감한 누적 실현 손익이다. 미실현 손익/equity는 현재 기본 출력에 없다.
`request`/`result`가 문자열 안 JSON으로 출력되는 것은 DB 표현을 그대로 출력하기 때문이다.

시스템 로그는 자동 파일 저장되지 않는다. 필요하면 셸에서 출력 전체를 저장할 수 있다.

```sh
PYTHONPATH=src .venv/bin/python -m apex_flow paper --seconds 60 > logs/paper-session.log 2>&1
```

이 명령은 해당 파일을 덮어쓴다. 보존하려면 파일 이름을 다르게 지정한다.
원본 시장 이벤트·계좌·주문 결과는 별도로 DB에 저장된다.

## 10. 설치 오류

설치는 성공했지만 import 오류가 나면 `PYTHONPATH=src`를 사용하거나,
macOS의 해당 `.pth` 파일 hidden 속성을 확인/해제한다. [README의 해결 절차](../README.md#설치-경로-문제-해결).
가상환경 전체를 삭제할 필요는 없다. 다른 디렉터리에서 실행하면 src/config 경로를 찾지 못할 수 있다.
