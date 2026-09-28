"""Long-only, unleveraged synthetic IOC execution. Never sends real orders."""
from datetime import datetime, timezone
from decimal import Decimal as D
import json


class Paper:
    """한 계좌·한 종목·대기 주문 하나를 처리하는 로컬 가상 엔진. 실주문을 보내지 않는다."""
    def __init__(self, config, store):
        """계좌를 복구하고 미완료 가상 주문을 취소한다. 실제 거래소 복구 정책은 아니다."""
        self.config, self.store = config, store
        with store.db:
            identity = store.get("identity")
            expected = {"symbol": config.symbol, "initial_cash": str(config.initial_cash)}
            if identity is not None and identity != expected:
                raise ValueError("Database symbol/initial_cash mismatch; use a separate database")
            store.put("identity", expected)
            self.account = store.get("account", {"cash": str(config.initial_cash), "quantity": "0", "basis": "0", "realized": "0"})
            store.put("account", self.account)
            # Pending requests are cancelled on restart, never silently resubmitted.
            store.db.execute("UPDATE orders SET status='CANCELLED',result=? WHERE status='PENDING'",
                             (json.dumps({"reason": "process restart"}),))
        self.pending = None
        self.used_book = -1
        self.daily = store.get("daily")

    def submit(self, client_id, side, quantity, now_ms, reduce_only=False):
        """요청을 PENDING으로 기록한다. 체결은 이후 step에서 일어난다.

        quantity는 기초자산 수량이고 now_ms는 로컬 wall-clock ms다.
        같은 ID/직렬화된 요청은 기존 결과를 반환하며 다른 요청은 거절한다.
        """
        quantity = D(str(quantity))
        if not client_id or side not in ("BUY", "SELL") or not quantity.is_finite() or quantity <= 0:
            raise ValueError("Invalid paper order")
        if side == "SELL" and not reduce_only:
            raise ValueError("SELL must be reduce-only; shorting not implemented")
        if side == "BUY" and reduce_only:
            raise ValueError("BUY reduce-only is unsupported in long-only paper mode")
        request = dict(side=side, quantity=str(quantity), reduce_only=reduce_only)
        old = self.store.order(client_id)
        if old:
            if json.loads(old["request"]) != request:
                raise ValueError("client_id reused with different parameters")
            return old
        if self.pending:
            raise ValueError("Only one pending order allowed")
        with self.store.db:
            self.store.save_order(client_id, request, "PENDING", {"submitted_ms": now_ms})
        self.pending = (client_id, request, now_ms)
        return self.store.order(client_id)

    def cancel(self, reason="cancelled"):
        """대기 중인 가상 주문만 취소한다. 이미 보유한 포지션은 유지한다."""
        if self.pending:
            client_id, request, _ = self.pending
            with self.store.db:
                self.store.save_order(client_id, request, "CANCELLED", {"reason": reason})
            self.pending = None

    def equity(self, market):
        """현금+보유 수량×평가 가격. 가격 신선도를 검사하지 않아 위험 판단에 바로 쓰면 안 된다."""
        q = D(self.account["quantity"])
        price = market.mark or (market.bids[0][0] if market.bids else D(0))
        return D(self.account["cash"]) + q * price

    def step(self, market, now_ms):
        """날짜 기준을 갱신하고 대기 주문을 한 번 진행한다.

        아직 처리할 수 없으면 None, 종료되면 저장된 주문을 반환한다.
        내부 순서는 시간/데이터 검사 → 진입 위험 검사 → 호가 체결 → 회계 커밋이다.
        """
        # Roll before ANY fill (including a next-day SELL), even on idle heartbeats.
        # This is realized-change + full open PnL, not midnight equity accounting.
        day = datetime.fromtimestamp(now_ms / 1000, timezone.utc).date().isoformat()
        if not self.daily or self.daily["day"] != day:
            daily = {"day": day, "realized": self.account["realized"]}
            with self.store.db:
                self.store.put("daily", daily)
            self.daily = daily
        if not self.pending:
            return None
        client_id, request, submitted = self.pending
        if now_ms < submitted + self.config.latency_ms:
            return None
        # Require a snapshot observed after simulated arrival. No same-tick fills.
        # 위험을 줄이는 매도는 trade/mark 장애와 분리하되, 최신 호가는 반드시 요구한다.
        healthy = market.book_healthy if request["reduce_only"] else market.healthy
        if not healthy(now_ms, self.config.stale_ms):
            return self._finish("REJECTED", {"reason": "unhealthy or stale market"})
        if market.last["depthUpdate"][0] <= submitted + self.config.latency_ms or market.update_id <= self.used_book:
            if now_ms - submitted > self.config.stale_ms:
                return self._finish("EXPIRED", {"reason": "no fresh executable snapshot"})
            return None
        side = request["side"]
        cash, held, basis = (D(self.account[k]) for k in ("cash", "quantity", "basis"))
        quantity = D(request["quantity"])
        if side == "SELL":
            quantity = min(quantity, held)
            if quantity <= 0:
                return self._finish("REJECTED", {"reason": "no position to reduce"})
        else:
            pnl = D(self.account["realized"]) - D(self.daily["realized"]) + held * ((market.mark or market.bids[0][0]) - basis)
            spread = D(market.features()["spread_bps"])
            if self.config.killed() or pnl <= -self.config.max_daily_loss or spread > self.config.max_spread_bps:
                return self._finish("REJECTED", {"reason": "kill, daily loss, or spread limit"})
            # Conservative worst visible execution price including extra slippage.
            cap = market.asks[-1][0] * (1 + self.config.slippage_bps / 10000)
            if (held + quantity) * cap > self.config.max_notional or quantity * cap * (1 + self.config.taker_fee_bps/10000) > cash:
                return self._finish("REJECTED", {"reason": "exposure or cash limit"})
        # 매수는 ask, 매도는 bid를 순서대로 소진한다. 호가 차이에 spread가 이미 포함된다.
        # 별도 spread 비용을 또 빼면 이중 차감이다. 잔량은 이후 재대기하지 않는다.
        levels = market.asks if side == "BUY" else market.bids
        remaining, notional = quantity, D(0)
        for price, available in levels:
            filled = min(remaining, available)
            price *= 1 + (1 if side == "BUY" else -1) * self.config.slippage_bps / 10000
            notional += filled * price
            remaining -= filled
            if remaining == 0:
                break
        filled = quantity - remaining
        fee = notional * self.config.taker_fee_bps / 10000
        # 수수료는 즉시 realized에 반영한다. basis는 수수료를 제외한 가중평균 체결가다.
        realized = D(self.account["realized"]) - fee
        if side == "BUY":
            new_qty = held + filled
            basis = (held * basis + notional) / new_qty
            cash -= notional + fee
        else:
            new_qty = held - filled
            realized += notional - filled * basis
            cash += notional - fee
            if not new_qty:
                basis = D(0)
        # JSON에는 Decimal을 문자열로 기록해 float 변환에 따른 회계 오차를 피한다.
        account = dict(cash=str(cash), quantity=str(new_qty), basis=str(basis), realized=str(realized))
        result = dict(filled=str(filled), unfilled=str(D(request["quantity"])-filled),
                      average_price=str(notional/filled), fee=str(fee), book_id=market.update_id, time_ms=now_ms)
        status = "FILLED" if filled == D(request["quantity"]) else "PARTIAL_CANCELLED"
        # Position and fill are one transaction; a crash cannot persist only one.
        with self.store.db:
            self.store.put("account", account)
            self.store.save_order(client_id, request, status, result)
        self.account, self.pending, self.used_book = account, None, market.update_id
        return self.store.order(client_id)

    def _finish(self, status, result):
        """체결 없이 주문을 종료한다. 계좌는 변경하지 않는다."""
        client_id, request, _ = self.pending
        with self.store.db:
            self.store.save_order(client_id, request, status, result)
        self.pending = None
        return self.store.order(client_id)
