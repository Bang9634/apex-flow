# 소스코드 리뷰와 해설

## 읽는 순서와 전체 흐름

`config.py → market.py → storage.py → paper.py → collector.py → cli.py → tests` 순으로 읽으면
설정·자료 구조·회계 규칙을 먼저 이해한 뒤 비동기 실행을 볼 수 있다.

```text
python -m apex_flow
  __main__.py → cli.main()
    Config.load() → Store 생성
    demo: synthetic() → Market.apply() → Paper.submit()/step()
    collect/paper: cli.live() → collector.run_collector()
      WebSocket 수신 → Store.event() → Market.apply() → on_event()
      heartbeat ────────────────────────────────────→ on_event()
        Paper.step() → 필요 시 검사 주문/청산 submit() → 상태 출력
    종료 → 대기 가상 주문 취소 → DB/연결 정리
```

새로 submit한 주문은 같은 콜백에서 즉시 체결하지 않는다. 다음 step이 시간·새 호가 조건을 확인한다.
`live()`라는 이름은 실시간 시세를 뜻한다. 실자금 거래 함수가 아니다.

## config.py — 설정과 검증

`Config`는 frozen dataclass다. 생성 후 필드 재할당을 막아 런타임의 의도치 않은 설정 변경을 줄인다.
`load()`는 TOML을 읽어 금액을 Decimal로 변환한다. 금액을 문자열로 적는 이유는 이진 float 변환에
의한 오차를 피하기 위해서다. bps는 1/10000이며 수수료·슬리피지는 편도 값이다.
`__post_init__()`는 음수·NaN·무한대, 잘못된 시간값, 초기 현금을 넘는 노출 설정을 거절한다.
`killed()`는 파일 존재 여부만 확인한다.

리뷰: 기본값 검증은 있지만 거래소의 실제 종목/필터 조회는 없다. 실행별 설정 버전과
독립 위험 엔진 설정을 추가해야 한다. 상대 경로는 TOML 위치가 아니라 실행 디렉터리 기준이다.

## market.py — 메모리상의 시장 상태와 지표

`Market`은 bids/asks, mark, 최근 100개 가격·거래 방향 표본, 연결 상태를 보유한다.
`last`는 이벤트 유형별 `(수신 시각, 거래소 이벤트 시각)`이다. `default_factory`는
Market 인스턴스들이 리스트·딕셔너리를 공유하지 않도록 한다.

`apply()`의 흐름:

- 종목/상품 유형 확인 후 이벤트 시각을 검사한다.
- depth는 update ID로 중복·역순을 무시하고, 매수 호가 내림차순·매도 호가 오름차순으로 정렬한다.
- 상위 호가 스냅샷 전체를 교체한다. diff-depth를 누적하는 full order book 엔진이 아니다.
- aggTrade는 가격/수량과 방향을 저장한다. `m=True`는 매수자가 maker이므로 적극적 매도량이다.
- markPrice는 평가용 가격을 저장한다.

`healthy()`는 신규 진입용으로 두 연결과 세 종류의 최신 데이터를 모두 요구한다.
`book_healthy()`는 청산 체결용으로 public 연결과 최신 호가를 요구한다.
`disconnect()`는 해당 피드의 유효성을 제거한다. mark 값 자체는 남을 수 있으므로
mark 숫자가 존재한다는 것만으로 최신이라고 판단하면 안 된다.

`features()`:

| 지표 | 코드가 계산하는 값 |
|---|---|
| mid | 최우선 bid/ask의 중간값 |
| spread_bps | `(ask-bid)/mid × 10000` |
| imbalance | 상위 호가 bid/ask 수량 합의 불균형 |
| microprice | 최우선 반대편 가격을 해당 호가 수량으로 가중한 가격 |
| momentum | 최근 표본 마지막 가격 / 첫 가격 − 1 |
| volatility | 표본 간 로그수익률의 모집단 표준편차. 연율화 없음 |
| trade_flow | 부호 있는 적극적 거래량 합 / 전체 거래량 |

리뷰: 최근 100개 표본은 일정 시간 구간이 아니다. 지표 값이 있다고 학습이나 전략이 구현된 것도 아니다.
시간 기반 윈도우, 지표 warm-up, 데이터 품질/갭 처리가 다음 과제다.

## storage.py — SQLite와 커밋 책임

`events`는 원본 데이터, `state`는 계좌와 기준 상태, `orders`는 주문의 현재 상태를 저장한다.
`flock`으로 같은 DB의 중복 프로세스 실행을 막는다. macOS/Linux용이며 모든 SQLite 외부 도구를
강제로 차단하는 잠금은 아니다.

`event()`는 한 건을 스스로 커밋한다. 반면 `put()`과 `save_order()`는 commit하지 않는다.
계좌와 주문을 함께 변경할 때 호출자가 아래처럼 하나의 트랜잭션으로 묶기 때문이다.

```python
with store.db:
    store.put("account", account)
    store.save_order(client_id, request, status, result)
```

중간에 예외가 나면 두 변경이 함께 롤백된다. DB 커밋 뒤에 메모리 계좌를 바꿔 일관성을 유지한다.
WAL/FULL은 저장 내구성을 위한 설정이며 비동기 저장을 의미하지 않는다.

리뷰: `orders`는 상태 이력을 덮어쓴다. append-only 주문 이벤트, 스키마 버전,
실행 메타데이터, 저장 성능 관측을 추가해야 한다. 읽기 전용 status 연결도 개선 후보다.

## paper.py — 가상 주문, 위험 검사, 회계

`__init__()`는 DB의 계좌를 복구하며 종목/초기 현금이 다르면 거절한다. PENDING 주문은
CANCELLED로 바꾼다. 이는 가상 주문에만 맞는 정책이며 실제 주문은 거래소 조회가 먼저다.

`submit()`은 주문을 등록할 뿐 체결하지 않는다. SELL은 reduce-only만 허용하며 short는 없다.
동일 ID 재요청은 기존 결과를 돌려주지만 요청 내용이 다르면 오류다. 현재 quantity의 문자열 표현도
비교하므로 `0.5`와 `0.50`의 동일성 정규화는 향후 개선 대상이다.
동시 대기 주문 하나 제한으로 주문 예약 노출의 복잡성을 줄였다.

`step()`은 다음 순서다.

1. UTC 날짜가 바뀌면 실현 손익 기준을 갱신한다. 주문이 없어도 실행한다.
2. 대기 주문과 가상 지연 만료를 확인한다.
3. 데이터 상태와 주문 도착 이후 새 호가가 있는지 확인한다.
4. BUY는 kill/손실/spread/노출/현금을 검사한다. SELL은 보유량 이내로 제한한다.
5. BUY는 ask, SELL은 bid 수량을 순서대로 소진하고 추가 슬리피지를 적용한다.
6. 체결 수량/금액/수수료와 새 계좌를 계산한다.
7. 계좌와 주문 결과를 함께 커밋한 후 메모리 상태를 갱신한다.

미체결 잔량은 재대기시키지 않는 IOC 근사다. 같은 호가 ID를 연이어 소진하지 않도록 하지만,
다음 스냅샷의 유동성이 완전히 새것이라는 근사는 남아 있다.

회계에서 `cash`는 남은 현금, `quantity`는 보유 수량, `basis`는 평균 매입가다.
수수료는 매수·매도 순간 `realized`에서 차감하고 basis에 또 더하지 않는다.

```text
매수 후 basis = (기존 수량×기존 basis + 새 매수 금액) / 새 총수량
매수 후 cash  = 기존 cash − 매수 금액 − 수수료
매도 실현손익 증가 = 매도 금액 − 매도 수량×basis − 수수료
매도 후 cash  = 기존 cash + 매도 금액 − 수수료
```

spread는 bid/ask 체결 가격에 이미 반영되어 있다. 비용으로 다시 빼면 이중 차감이다.
`equity()`는 현금+평가금액을 계산하지만 freshness를 검사하지 않으며 현재 기본 상태 출력에 쓰이지 않는다.
가격이 없을 때 0으로 평가하거나 오래된 mark를 쓸 수 있으므로 위험 엔진에 바로 연결하면 안 된다.
`cancel()`은 대기 주문만 취소하며 포지션은 유지한다. `_finish()`는 거절/만료 등을 기록한다.

리뷰: 현재 step에 위험·체결·회계 책임이 모여 있다. 기능 확장 전에 위험 판단 결과를 별도 구조로
분리하고, 단위·거절 이유·시간 기준을 명확히 해야 한다. 일일 기준은 정확한 자정 equity PnL이
아니며 손실 한도에 닿은 뒤 회복하면 진입이 다시 허용된다. SL/TP·funding·마진은 없다.

## collector.py — 비동기 수신과 장애 처리

`stream_urls()`는 public 호가와 market 체결/mark를 별도 URL로 만든다.
`collect_group()`는 메시지를 받고 원본 저장 → 시장 반영 → CLI 콜백 순으로 처리한다.
내부 파싱/저장/콜백 오류는 RuntimeError로 전달해 통신 오류 재시도와 구분한다.
네트워크 오류는 backoff+jitter로 재시도하고 연속 8회 실패 시 중단한다.
성공적으로 메시지를 처리하면 실패 횟수는 초기화된다. 전체 장애 횟수 누계가 아니다.

`run_collector()`는 TaskGroup으로 두 연결을 실행하고, 약 250ms heartbeat를 호출한다.
한 작업의 치명적 예외는 다른 작업도 중단시킨다. 종료 deadline은 monotonic clock을 쓰지만
주문 지연과 freshness에 전달되는 `now_ms()`는 wall clock이다.

리뷰: asyncio는 외부 대기 시간을 겹칠 수 있게 할 뿐 동기 DB 쓰기를 병렬화하지 않는다.
서버 메시지 갭, 큐 지연, 데이터 지연 분포, 장시간 재접속 정책을 계측/보완해야 한다.

## cli.py — 명령과 모듈 조립

`main()`은 argparse 검증, 설정 로딩, DB 출처 확인과 실행/종료를 담당한다.
`synthetic()`와 `demo()`는 합성 이벤트로 회계를 검사한다.
`live()`는 collect에서는 계좌 없이 수집하고 paper에서는 계좌를 복구한다.
안쪽 `on_event()`는 step을 먼저 실행하고, 이후 명시적 검사 주문/청산을 요청한다.
`nonlocal`은 콜백이 바깥 함수의 제출 여부/시간/출력 시각을 갱신하도록 한다.

`opened_at`은 이름과 달리 매수 요청 시각이다. 현재 hold_seconds는 체결 후 보유 시간이 아니다.
기본 paper에는 전략·자동 손절이 없다. `finally`는 정상 종료/예외 모두에서 대기 주문을 취소한다.

리뷰: 전략이 생기면 CLI 콜백에 계속 분기를 추가하지 말고 별도 실행 조정 모듈로 분리한다.
출력은 현재 DB의 JSON 문자열을 포함하므로 읽기 편한 로그/보고서로 바꿀 여지가 있다.

## 나머지 파일

| 파일 | 역할과 검토 사항 |
|---|---|
| __main__.py | python -m 실행을 cli.main으로 전달하는 진입점 |
| __init__.py | 패키지 설명. 실행 로직 없음 |
| tests/test_core.py | 회계·중복 ID·위험·부분 체결·복구·트랜잭션 롤백·날짜 경계 검증 |
| tests/test_collector.py | URL 분리, 메시지 없는 heartbeat, 내부 오류 재시도 금지 |
| tests/test_cli.py | 기존 포지션 복구 후 market 피드 없이 최신 호가로 청산하는 CLI 통합 검증 |
| configs/paper.toml | 기본 비용/위험 가정. 실제 계정 수수료 조회 결과가 아님 |
| pyproject.toml | Python 버전/의존성 범위, src 패키지 탐색, apex-flow 명령 등록. 완전한 lockfile은 아님 |
| .gitignore | 키 파일·가상환경·DB 등을 Git에서 제외. 비밀정보 보호 전체를 보장하는 기능은 아님 |
| data/.gitkeep, logs/.gitkeep | 빈 디렉터리 보존용. 실행 로직 없음 |
| README.md | 시작 안내와 구현 범위 |
| docs/design.md | 구조·상태 일관성·기대값 가정 |
| docs/review.md | 우선순위별 결함/한계와 수정 기록 |
| docs/roadmap.md | 다음 개발 단계와 완료 조건 |
| docs/validation.md | 실제 수행한 검증과 하지 않은 검증의 구분 |
| docs/runbook.md | 현재 지원하는 명령, DB/로그/종료/오류 안내 |
| docs/code-walkthrough.md | 이 해설 문서 |

## 주석 작성 기준

변수 대입과 반복문의 동작을 그대로 한국어로 옮기는 주석은 보통 필요 없다.
반면 아래 정보는 함수명만으로 알 수 없으므로 명시한다.

- 입력/출력의 단위, 시계 종류, 표본 구간, 준비되지 않은 값의 의미
- 거래소 특성 또는 회계 처리의 가정
- 오류/재시도/재시작 시 보장하는 상태
- 트랜잭션 소유자, 저장과 메모리 갱신의 순서
- 구현 범위와 알려진 한계

주석은 의도를 설명하고 테스트는 그 동작이 유지되는지 확인한다. 주석을 많이 쓴다고
불명확한 함수 경계나 데이터 구조가 해결되지는 않으며, 동작 변경 시 주석·문서·테스트를 함께 갱신한다.
