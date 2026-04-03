from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal

from .errors import RiskViolation


@dataclass
class RiskState:
    start_equity: Decimal
    day_start_equity: Decimal
    inventory_base: Decimal = Decimal("0")
    inventory_quote: Decimal = Decimal("0")
    rejects: int = 0
    cancels_failed: int = 0
    total_actions: int = 1
    last_mid: Decimal | None = None
    recent_mids: list[tuple[float, Decimal]] = field(default_factory=list)
    breaker_until_ts: float = 0.0


class RiskEngine:
    def __init__(self, cfg: dict, stale_data_limit_sec: int, max_open_orders: int) -> None:
        self.cfg = cfg
        self.stale_data_limit_sec = stale_data_limit_sec
        self.max_open_orders = max_open_orders

    def check_pre_order(
        self,
        *,
        state: RiskState,
        equity: Decimal,
        spread_bps: Decimal,
        inventory_imbalance_pct: Decimal,
        open_orders: int,
        market_data_age_sec: float,
        mid_price: Decimal,
        now: float,
    ) -> None:
        if self.cfg["safety"]["shutdown_on_stale_market_data"] and market_data_age_sec > self.stale_data_limit_sec:
            raise RiskViolation("market data stale")
        if spread_bps > Decimal(str(self.cfg["max_spread_bps"])):
            raise RiskViolation("spread limit breached")
        if inventory_imbalance_pct > Decimal(str(self.cfg["max_inventory_imbalance_pct"])):
            raise RiskViolation("inventory imbalance too high")
        if open_orders >= self.max_open_orders:
            raise RiskViolation("max open orders reached")

        self._check_circuit_breakers(state=state, mid_price=mid_price, now=now)
        self._check_drawdown(state, equity)

    def check_post_fill(self, state: RiskState, equity: Decimal) -> None:
        self._check_drawdown(state, equity)
        reject_ratio = Decimal(str(state.rejects * 100 / state.total_actions))
        cancel_fail_ratio = Decimal(str(state.cancels_failed * 100 / state.total_actions))
        if reject_ratio > Decimal(str(self.cfg["max_reject_ratio_pct"])):
            raise RiskViolation("reject ratio too high")
        if cancel_fail_ratio > Decimal(str(self.cfg["max_cancel_fail_ratio_pct"])):
            raise RiskViolation("cancel fail ratio too high")

    def _check_circuit_breakers(self, state: RiskState, mid_price: Decimal, now: float) -> None:
        cb = self.cfg["circuit_breakers"]
        if not cb["enabled"]:
            return
        if now < state.breaker_until_ts:
            raise RiskViolation("circuit breaker cooldown active")

        if state.last_mid and state.last_mid > 0:
            gap_bps = abs((mid_price - state.last_mid) / state.last_mid) * Decimal("10000")
            if gap_bps >= Decimal(str(cb["price_gap_bps"])):
                state.breaker_until_ts = now + cb["trip_cooldown_sec"]
                raise RiskViolation("price gap circuit breaker triggered")

        state.recent_mids.append((now, mid_price))
        cutoff = now - 60
        state.recent_mids = [(ts, px) for ts, px in state.recent_mids if ts >= cutoff]
        if len(state.recent_mids) >= 2:
            first = state.recent_mids[0][1]
            if first > 0:
                one_min_move_bps = abs((mid_price - first) / first) * Decimal("10000")
                if one_min_move_bps >= Decimal(str(cb["volatility_spike_bps_1m"])):
                    state.breaker_until_ts = now + cb["trip_cooldown_sec"]
                    raise RiskViolation("volatility circuit breaker triggered")

        state.last_mid = mid_price

    def _check_drawdown(self, state: RiskState, equity: Decimal) -> None:
        max_dd = Decimal(str(self.cfg["max_strategy_drawdown_pct"]))
        max_day_dd = Decimal(str(self.cfg["max_intraday_drawdown_pct"]))
        if equity < state.start_equity * (Decimal("1") - max_dd / Decimal("100")):
            raise RiskViolation("max strategy drawdown exceeded")
        if equity < state.day_start_equity * (Decimal("1") - max_day_dd / Decimal("100")):
            raise RiskViolation("max intraday drawdown exceeded")
