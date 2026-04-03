from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal

from .config_loader import ResolvedConfig
from .errors import MarketDataStaleError, RiskViolation, WebsocketDisconnectError
from .exchange import ExchangeRules, LiveExchangeAdapter, PaperExchangeAdapter
from .order_manager import OrderManager
from .risk_engine import RiskEngine, RiskState
from .state_store import StateStore
from .websocket_client import ReliableWebsocket


@dataclass
class MarketSnapshot:
    bid: Decimal
    ask: Decimal
    ts: float

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread_bps(self) -> Decimal:
        return ((self.ask - self.bid) / self.mid) * Decimal("10000")


class ExecutionEngine:
    def __init__(self, resolved: ResolvedConfig) -> None:
        self.cfg = resolved.strategy
        market = self.cfg["market"]
        tick = Decimal("1") / (Decimal("10") ** market["price_precision"])
        step = Decimal("1") / (Decimal("10") ** market["qty_precision"])
        rules = ExchangeRules(
            symbol=market["symbol"],
            tick_size=tick,
            lot_size=step,
            min_qty=Decimal(str(market["min_qty"])),
            min_notional=Decimal(str(market["min_notional_usd"])),
        )

        mode = self.cfg["run_mode"]
        if mode == "LIVE":
            self.adapter = LiveExchangeAdapter(rules)
        else:
            fees = self.cfg["cost_model"]["fees"]
            self.adapter = PaperExchangeAdapter(
                rules,
                maker_fee_bps=fees["maker_fee_bps"],
                taker_fee_bps=fees["taker_fee_bps"],
                slippage_bps=self.cfg["cost_model"]["estimated_slippage_bps"],
            )

        limits = self.cfg["execution"]["order_limits"]
        retry = self.cfg["execution"]["retry"]
        self.order_manager = OrderManager(
            adapter=self.adapter,
            max_open_orders=limits["max_open_orders"],
            max_cancels_per_sec=limits["max_cancels_per_sec"],
            max_new_orders_per_sec=limits["max_new_orders_per_sec"],
            max_retries=retry["max_retries"],
            retry_backoff_ms=retry["retry_backoff_ms"],
            min_replace_interval_ms=self.cfg["execution"]["cancel_replace"]["min_replace_interval_ms"],
        )
        stale_sec = self.cfg["risk"]["safety"]["stale_market_data_sec"]
        disc_grace = self.cfg["risk"]["safety"]["ws_disconnect_grace_sec"]
        self.ws = ReliableWebsocket(stale_after_sec=stale_sec, disconnect_grace_sec=disc_grace)
        self.risk = RiskEngine(self.cfg["risk"], stale_data_limit_sec=stale_sec, max_open_orders=limits["max_open_orders"])
        self.state = RiskState(start_equity=Decimal("100"), day_start_equity=Decimal("100"))
        self.store = StateStore(self.cfg["ops"]["state_store"]["path"])
        self.config_hash = resolved.config_hash
        self._last_checkpoint_ts = 0.0

    def reconcile_on_start(self, cancel_unknown: bool = True) -> tuple[list[str], list[str]]:
        persisted = self.store.load_runtime()
        if persisted["config_hash"] and persisted["config_hash"] != self.config_hash:
            raise ValueError("config hash mismatch with persisted state")
        if persisted["last_mid"]:
            self.state.last_mid = Decimal(str(persisted["last_mid"]))
        persisted_positions = persisted.get("positions") or {}
        if "base" in persisted_positions:
            self.state.inventory_base = Decimal(str(persisted_positions["base"]))
        if "quote" in persisted_positions:
            self.state.inventory_quote = Decimal(str(persisted_positions["quote"]))
        return self.order_manager.reconcile_open_orders(cancel_unknown=cancel_unknown)

    def on_market_data(self, snapshot: MarketSnapshot) -> None:
        if self.cfg["risk"]["safety"]["shutdown_on_ws_disconnect"] and self.ws.should_force_shutdown():
            raise WebsocketDisconnectError("ws disconnect shutdown trigger")
        self.ws.assert_healthy()
        self.ws.on_message()

        now = time.time()
        market_age = now - snapshot.ts
        if market_age > self.cfg["risk"]["safety"]["stale_market_data_sec"]:
            raise MarketDataStaleError("stale market data")

        inv_imbalance = Decimal("0")
        self.risk.check_pre_order(
            state=self.state,
            equity=self.state.day_start_equity,
            spread_bps=snapshot.spread_bps,
            inventory_imbalance_pct=inv_imbalance,
            open_orders=self.order_manager.open_order_count(),
            market_data_age_sec=market_age,
            mid_price=snapshot.mid,
            now=now,
        )

        levels = self.cfg["grid"]["levels"]
        spacing = Decimal(str(self.cfg["grid"]["spacing_pct"])) / Decimal("100")
        per_notional = Decimal(str(self.cfg["sizing"]["per_level_sizing"]["base_order_notional_usd"]))

        # Only build a bounded subset per cycle to avoid bursts.
        max_new_pairs = min(4, levels // 2)
        for i in range(1, max_new_pairs + 1):
            buy_price = snapshot.mid * (Decimal("1") - spacing * i)
            sell_price = snapshot.mid * (Decimal("1") + spacing * i)
            buy_qty = per_notional / buy_price
            sell_qty = per_notional / sell_price
            self.order_manager.submit_limit(
                intent_key=f"BUY-{i}-{buy_price:.8f}",
                symbol=self.cfg["market"]["symbol"],
                side="BUY",
                price=buy_price,
                qty=buy_qty,
                tif=self.cfg["execution"]["time_in_force"],
                post_only=self.cfg["execution"]["post_only"],
                market_mid=snapshot.mid,
            )
            self.order_manager.submit_limit(
                intent_key=f"SELL-{i}-{sell_price:.8f}",
                symbol=self.cfg["market"]["symbol"],
                side="SELL",
                price=sell_price,
                qty=sell_qty,
                tif=self.cfg["execution"]["time_in_force"],
                post_only=self.cfg["execution"]["post_only"],
                market_mid=snapshot.mid,
            )

        self._run_post_fill_risk_check()
        self._checkpoint(snapshot)

    def _run_post_fill_risk_check(self) -> None:
        for managed in self.order_manager.managed.values():
            if managed.status.status in {"FILLED", "PARTIALLY_FILLED"} and managed.status.filled_qty > managed.accounted_fill_qty:
                delta_fill = managed.status.filled_qty - managed.accounted_fill_qty
                signed = delta_fill if managed.order.side == "BUY" else -delta_fill
                self.state.inventory_base += signed
                notional = delta_fill * managed.status.avg_fill_price
                self.state.inventory_quote -= notional if managed.order.side == "BUY" else -notional
                managed.accounted_fill_qty = managed.status.filled_qty
        self.risk.check_post_fill(state=self.state, equity=self.state.day_start_equity)

    def _checkpoint(self, snapshot: MarketSnapshot) -> None:
        now = time.time()
        interval = self.cfg["ops"]["state_store"]["checkpoint_interval_sec"]
        if now - self._last_checkpoint_ts < interval:
            return
        open_orders = [
            {
                "intent_key": m.intent_key,
                "client_order_id": m.order.client_order_id,
                "exchange_order_id": m.status.exchange_order_id,
                "status": m.status.status,
                "filled_qty": str(m.status.filled_qty),
            }
            for m in self.order_manager.managed.values()
        ]
        positions = {"base": str(self.state.inventory_base), "quote": str(self.state.inventory_quote)}
        self.store.save_runtime(
            config_hash=self.config_hash,
            open_orders=open_orders,
            balances={},
            positions=positions,
            last_mid=float(snapshot.mid),
        )
        self._last_checkpoint_ts = now

    def emergency_shutdown(self) -> None:
        for intent, managed in list(self.order_manager.managed.items()):
            if managed.status.status in {"OPEN", "PARTIALLY_FILLED"}:
                self.order_manager.cancel(intent)

    def handle_failure(self, exc: Exception) -> None:
        if isinstance(exc, RiskViolation):
            self.emergency_shutdown()
            if self.cfg["risk"]["safety"]["panic_flatten_enabled"]:
                return
        raise exc
