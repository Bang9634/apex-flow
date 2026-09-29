"""사용자 명령과 모듈을 연결한다. exercise는 전략이 아닌 단일 가상 왕복 검사다."""
import argparse
import asyncio
import math
from decimal import Decimal as D
import json
import logging

from .config import Config
from .demo import demo
from .storage import Store


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
        p.add_argument("--metrics-output", help="New JSONL report file; default logs/metrics-<run-id>.jsonl")
        p.add_argument("--metrics-interval", type=float, default=30, help="Report interval in seconds (>=1)")
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
    if hasattr(args, "metrics_interval") and (not math.isfinite(args.metrics_interval) or args.metrics_interval < 1):
        parser.error("metrics-interval must be finite and >=1")
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
            from .runtime import SessionOptions, run_market_session
            options = SessionOptions(
                command=args.command, seconds=args.seconds, metrics_output=args.metrics_output,
                metrics_interval=args.metrics_interval,
                exercise_quantity=getattr(args, "exercise_quantity", None),
                hold_seconds=getattr(args, "hold_seconds", 10),
                close_position=getattr(args, "close_position", False))
            asyncio.run(run_market_session(config, store, options))
    except KeyboardInterrupt:
        logging.info("Stopped by user")
    finally:
        store.close()
