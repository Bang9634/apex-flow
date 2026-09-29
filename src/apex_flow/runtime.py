"""애플리케이션 조립: 시계·수집·paper·계측의 생명주기를 관리한다. CLI 파싱은 하지 않는다."""
import asyncio
from dataclasses import asdict, dataclass
from decimal import Decimal
import json
import logging
import platform
import sys
import uuid

from .clock import SystemClock
from .collector import run_collector
from .market import Market
from .observability.metrics import Metrics
from .observability.reporting import JsonlReporter
from .paper import Paper

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionOptions:
    """CLI/테스트/향후 서비스 실행기가 공유하는 실행 설정. argparse 객체에 의존하지 않는다."""
    command: str
    seconds: float = 60
    metrics_output: str | None = None
    metrics_interval: float = 30
    exercise_quantity: Decimal | None = None
    hold_seconds: float = 10
    close_position: bool = False


async def run_market_session(config, store, options: SessionOptions, *, clock=None):
    clock = clock or SystemClock()
    metrics = Metrics(clock)
    run_id = uuid.uuid4().hex
    reporter = JsonlReporter(options.metrics_output or f"logs/metrics-{run_id}.jsonl", dict(
        schema_version=1, run_id=run_id, mode=options.command, symbol=config.symbol,
        started_wall_ms=metrics.started.wall_ms, python=platform.python_version(),
        platform=platform.platform(), database=str(store.path), config=asdict(config),
        options=dict(seconds=options.seconds, metrics_interval=options.metrics_interval,
                     exercise_quantity=str(getattr(options, "exercise_quantity", None)),
                     hold_seconds=getattr(options, "hold_seconds", None),
                     close_position=getattr(options, "close_position", False))))
    log.info("metrics_output=%s run_id=%s", reporter.path, run_id)
    market = Market(config.symbol)
    paper = None
    last_report = None
    submitted = False
    opened_at = None

    def on_event(now):
        nonlocal last_report, submitted, opened_at
        if paper:
            with metrics.measure("paper.step_ms"):
                result = paper.step(market, now)
            if result:
                metrics.increment(f"paper.status.{result['status']}")
                details = json.loads(result["result"])
                if "simulated_wait_ms" in details:
                    metrics.observe("paper.simulated_wait_ms", details["simulated_wait_ms"])
                log.info("paper_order=%s", json.dumps(result))
            if options.exercise_quantity and not submitted and market.healthy(now, config.stale_ms):
                if Decimal(paper.account["quantity"]):
                    raise ValueError("Exercise requires a flat account; existing position retained")
                paper.submit(uuid.uuid4().hex, "BUY", options.exercise_quantity, now)
                submitted, opened_at = True, now
            if ((options.close_position or (opened_at is not None and now.elapsed_ms(opened_at) >= options.hold_seconds * 1000))
                    and Decimal(paper.account["quantity"]) > 0 and not paper.pending
                    and market.book_healthy(now, config.stale_ms)):
                paper.submit(uuid.uuid4().hex, "SELL", paper.account["quantity"], now, reduce_only=True)
        if last_report is None or now.elapsed_ms(last_report) >= 5000:
            with metrics.measure("processing.features_ms"):
                features = market.features()
            log.info("status=%s", json.dumps(dict(healthy=market.healthy(now, config.stale_ms),
                     features=features, account=paper.account if paper else None)))
            last_report = now

    async def report_loop():
        while True:
            await asyncio.sleep(options.metrics_interval)
            start = clock.now()
            await reporter.write(dict(type="metrics", run_id=run_id, **metrics.snapshot()))
            metrics.observe("reporting.write_ms", clock.now().elapsed_ms(start))

    outcome = "error"
    try:
        paper = Paper(config, store) if options.command == "paper" else None
        async with asyncio.TaskGroup() as tasks:
            reporting = tasks.create_task(report_loop())
            await run_collector(config, market, store, on_event, options.seconds, clock, metrics, run_id)
            reporting.cancel()
        outcome = "completed"
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    finally:
        try:
            if paper:
                if paper.pending:
                    metrics.increment("paper.shutdown_cancellations")
                paper.cancel("collector stopped")
                log.info("paper_final=%s", json.dumps(paper.account))
                if Decimal(paper.account["quantity"]):
                    log.warning("Open virtual position persisted; shutdown does not fabricate an exit")
        finally:
            # 마지막에도 0개 데이터/단절/오류를 보고한다. 강제 kill 시 final은 보장할 수 없다.
            primary_error = sys.exception()
            try:
                await reporter.write(dict(type="final", outcome=outcome, run_id=run_id, **metrics.snapshot()))
                log.info("metrics_final=%s", reporter.path)
            except Exception:
                log.exception("Failed to write final metrics")
                if primary_error is None:
                    raise
