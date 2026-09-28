"""사용자 명령과 모듈을 연결한다. exercise는 전략이 아닌 단일 가상 왕복 검사다."""
import argparse
import asyncio
from decimal import Decimal as D
import json
import logging
import time
import uuid

from .config import Config
from .market import Market
from .paper import Paper
from .storage import Store


def synthetic(market, store, t, update, bid="100", ask="100.02"):
    """오프라인 demo/테스트용 이벤트를 만든다. 실제 BTC 가격이나 실제 시계 흐름이 아니다."""
    events = [
        (f"{market.symbol.lower()}@aggTrade", dict(e="aggTrade", E=t, s=market.symbol, a=update, p=bid, q="1", m=False)),
        (f"{market.symbol.lower()}@markPrice@1s", dict(e="markPriceUpdate", E=t, s=market.symbol, p=bid)),
        (f"{market.symbol.lower()}@depth20@100ms", dict(e="depthUpdate", E=t, s=market.symbol, u=update, b=[[bid,"2"]], a=[[ask,"2"]])),
    ]
    for stream, event in events:
        store.event(stream, event, t)
        market.apply(stream, event, t)


def demo(config, store):
    """합성 가격에서 매수/매도·비용·회계를 확인한다. 기존 DB 잔고는 이어서 사용한다."""
    paper, market = Paper(config, store), Market(config.symbol)
    if D(paper.account["quantity"]):
        raise ValueError("Demo requires a flat account; use a dedicated database")
    market.connected = {"public", "market"}
    t = time.time_ns() // 1_000_000
    synthetic(market, store, t, 1)
    paper.submit(uuid.uuid4().hex, "BUY", "0.5", t)
    t += config.latency_ms + 1
    synthetic(market, store, t, 2)
    print(json.dumps(paper.step(market, t)))
    paper.submit(uuid.uuid4().hex, "SELL", "0.5", t, reduce_only=True)
    t += config.latency_ms + 1
    synthetic(market, store, t, 3, "100.10", "100.12")
    print(json.dumps(paper.step(market, t)))
    print(json.dumps({"account": paper.account, "features": market.features()}))


async def live(config, store, args):
    """실시간 시세 모드. 함수명 live는 실시간 수신을 뜻하며 실제 주문은 없다."""
    from .collector import run_collector
    market = Market(config.symbol)
    paper = Paper(config, store) if args.command == "paper" else None
    last_report = 0
    submitted = False
    opened_at = None

    def on_event(now):
        """시세 수신 또는 heartbeat마다 대기 주문 처리 후 검사 주문/청산을 요청한다."""
        nonlocal last_report, submitted, opened_at
        if paper:
            result = paper.step(market, now)
            if result:
                logging.info("paper_order=%s", json.dumps(result))
            # Explicit connectivity exercise, not a profitable strategy.
            if args.exercise_quantity and not submitted and market.healthy(now, config.stale_ms):
                if D(paper.account["quantity"]):
                    raise ValueError("Exercise requires a flat account; existing position retained")
                paper.submit(uuid.uuid4().hex, "BUY", args.exercise_quantity, now)
                # opened_at은 실제 체결 시각이 아닌 매수 요청 시각이다.
                submitted, opened_at = True, now
            if ((args.close_position or (opened_at is not None and now - opened_at >= args.hold_seconds * 1000))
                    and D(paper.account["quantity"]) > 0 and not paper.pending
                    and market.book_healthy(now, config.stale_ms)):
                paper.submit(uuid.uuid4().hex, "SELL", paper.account["quantity"], now, reduce_only=True)
        if now - last_report >= 5000:
            logging.info("status=%s", json.dumps(dict(healthy=market.healthy(now, config.stale_ms),
                         features=market.features(), account=paper.account if paper else None)))
            last_report = now
    try:
        await run_collector(config, market, store, on_event, args.seconds)
    finally:
        # 종료 시 미체결 주문만 취소한다. 호가 없이 잔여 포지션을 청산한 척하지 않는다.
        if paper:
            paper.cancel("collector stopped")
            logging.info("paper_final=%s", json.dumps(paper.account))
            if D(paper.account["quantity"]):
                logging.warning("Open virtual position persisted; shutdown does not fabricate an exit")


def main():
    """CLI 인자 검증 → 설정/DB 준비 → 명령 실행 → 자원 정리의 진입점."""
    parser = argparse.ArgumentParser(description="ApexFlow: public market data + LOCAL paper only")
    parser.add_argument("--config", default="configs/paper.toml")
    parser.add_argument("--database", help="Override SQLite path")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="Offline synthetic round trip; use a separate database")
    sub.add_parser("status", help="Print persisted account and recent orders")
    for name in ("collect", "paper"):
        p = sub.add_parser(name)
        p.add_argument("--seconds", type=float, default=60)
        if name == "paper":
            action = p.add_mutually_exclusive_group()
            action.add_argument("--exercise-quantity", type=D, help="Explicit one-shot virtual BUY/SELL connectivity exercise")
            action.add_argument("--close-position", action="store_true", help="Reduce existing virtual position using fresh public quotes")
            p.add_argument("--hold-seconds", type=float, default=10)
    args = parser.parse_args()
    if hasattr(args, "seconds") and (not 0 < args.seconds <= 86400):
        parser.error("seconds must be in (0,86400]")
    if hasattr(args, "hold_seconds") and not 0 < args.hold_seconds <= 86400:
        parser.error("hold-seconds must be in (0,86400]")
    if getattr(args, "exercise_quantity", None) is not None:
        if not args.exercise_quantity.is_finite() or args.exercise_quantity <= 0:
            parser.error("exercise-quantity must be finite and positive")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = Config.load(args.config)
    store = Store(args.database or ("data/demo.sqlite3" if args.command == "demo" else config.database))
    try:
        if args.command != "status":
            source = "synthetic" if args.command == "demo" else "binance_public"
            with store.db:
                previous = store.get("data_source")
                if previous is not None and previous != source:
                    raise ValueError("Cannot mix synthetic and live data in one database")
                store.put("data_source", source)
        if args.command == "demo":
            demo(config, store)
        elif args.command == "status":
            print(json.dumps(dict(account=store.get("account"), orders=[dict(r) for r in store.db.execute("SELECT * FROM orders ORDER BY rowid DESC LIMIT 10")]), indent=2))
        else:
            asyncio.run(live(config, store, args))
    except KeyboardInterrupt:
        logging.info("Stopped by user")
    finally:
        store.close()
