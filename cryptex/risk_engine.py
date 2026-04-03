from __future__ import annotations

from dataclasses import dataclass
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
    ) -> None:
        if market_data_age_sec > self.stale_data_limit_sec and self.cfg["safety"]["shutdown_on_stale_market_data"]:
            raise RiskViolation("market data stale")
        if spread_bps > Decimal(str(self.cfg["max_spread_bps"])):
            raise RiskViolation("spread limit breached")
        if inventory_imbalance_pct > Decimal(str(self.cfg["max_inventory_imbalance_pct"])):
            raise RiskViolation("inventory imbalance too high")
        if open_orders >= self.max_open_orders:
            raise RiskViolation("max open orders reached")

        self._check_drawdown(state, equity)

    def check_post_fill(self, state: RiskState, equity: Decimal) -> None:
        self._check_drawdown(state, equity)
        reject_ratio = Decimal(str(state.rejects * 100 / state.total_actions))
        cancel_fail_ratio = Decimal(str(state.cancels_failed * 100 / state.total_actions))
        if reject_ratio > Decimal(str(self.cfg["max_reject_ratio_pct"])):
            raise RiskViolation("reject ratio too high")
        if cancel_fail_ratio > Decimal(str(self.cfg["max_cancel_fail_ratio_pct"])):
            raise RiskViolation("cancel fail ratio too high")

    def _check_drawdown(self, state: RiskState, equity: Decimal) -> None:
        max_dd = Decimal(str(self.cfg["max_strategy_drawdown_pct"]))
        max_day_dd = Decimal(str(self.cfg["max_intraday_drawdown_pct"]))
        if equity < state.start_equity * (Decimal("1") - max_dd / Decimal("100")):
            raise RiskViolation("max strategy drawdown exceeded")
        if equity < state.day_start_equity * (Decimal("1") - max_day_dd / Decimal("100")):
            raise RiskViolation("max intraday drawdown exceeded")
