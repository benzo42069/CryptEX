from __future__ import annotations

import os
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

from cryptex.config_loader import ConfigLoader
from cryptex.errors import (
    ConfigError,
    ExchangeTransientError,
    ExchangeValidationError,
    InsufficientBalanceError,
    MarketDataStaleError,
    RateLimitExceededError,
    RiskViolation,
    WebsocketDisconnectError,
)
from cryptex.execution_engine import ExecutionEngine, MarketSnapshot


class SystemValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.setdefault("KRAKEN_API_KEY", "test")
        os.environ.setdefault("KRAKEN_API_SECRET", "test")

    def _tmp_strategy(self) -> Path:
        src = Path("strategies/doge_usd_grid_live.json").read_text()
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        p = Path(td.name) / "s.json"
        p.write_text(src)
        return p

    def test_config_loader_hash_and_validation(self) -> None:
        cfg = ConfigLoader().load("strategies/doge_usd_grid_live.json")
        self.assertEqual(len(cfg.config_hash), 64)

    def test_invalid_unknown_field_rejected(self) -> None:
        p = self._tmp_strategy()
        text = p.read_text()
        text = text[:-2] + ',\n  "bad_field": true\n}'
        p.write_text(text)
        with self.assertRaises(ConfigError):
            ConfigLoader().load(str(p))

    def test_mode_tag_must_match(self) -> None:
        p = self._tmp_strategy()
        text = p.read_text().replace('"mode": "LIVE"', '"mode": "PAPER"')
        p.write_text(text)
        with self.assertRaises(ConfigError):
            ConfigLoader().load(str(p))

    def test_paper_mode_does_not_require_live_keys(self) -> None:
        p = self._tmp_strategy()
        text = p.read_text().replace('"run_mode": "LIVE"', '"run_mode": "PAPER"', 1)
        text = text.replace('"mode": "LIVE"', '"mode": "PAPER"', 1)
        p.write_text(text)
        os.environ.pop("KRAKEN_API_KEY", None)
        os.environ.pop("KRAKEN_API_SECRET", None)
        cfg = ConfigLoader().load(str(p))
        self.assertEqual(cfg.strategy["run_mode"], "PAPER")

    def test_engine_places_valid_orders_and_persists_state(self) -> None:
        cfg = ConfigLoader().load("strategies/doge_usd_grid_live.json")
        cfg.strategy["run_mode"] = "PAPER"
        cfg.strategy["ops"]["metrics"]["tags"]["mode"] = "PAPER"
        state_path = tempfile.NamedTemporaryFile(delete=False).name
        cfg.strategy["ops"]["state_store"]["path"] = state_path
        engine = ExecutionEngine(cfg)
        engine.ws.on_connect()
        snap = MarketSnapshot(bid=Decimal("0.2000"), ask=Decimal("0.2002"), ts=time.time())
        engine.on_market_data(snap)
        self.assertGreater(engine.order_manager.open_order_count(), 0)
        self.assertIsNotNone(engine.store.load_runtime()["config_hash"])

    def test_stale_data_circuit_breaker(self) -> None:
        cfg = ConfigLoader().load("strategies/doge_usd_grid_live.json")
        cfg.strategy["run_mode"] = "PAPER"
        cfg.strategy["ops"]["metrics"]["tags"]["mode"] = "PAPER"
        engine = ExecutionEngine(cfg)
        engine.ws.on_connect()
        snap = MarketSnapshot(bid=Decimal("0.2"), ask=Decimal("0.2002"), ts=time.time() - 30)
        with self.assertRaises(MarketDataStaleError):
            engine.on_market_data(snap)

    def test_precision_rejection(self) -> None:
        cfg = ConfigLoader().load("strategies/doge_usd_grid_live.json")
        cfg.strategy["run_mode"] = "PAPER"
        cfg.strategy["ops"]["metrics"]["tags"]["mode"] = "PAPER"
        engine = ExecutionEngine(cfg)
        engine.ws.on_connect()
        with self.assertRaises(ExchangeValidationError):
            engine.order_manager.submit_limit(
                intent_key="x",
                symbol="BTC/USD",
                side="BUY",
                price=Decimal("0.2"),
                qty=Decimal("1"),
                tif="GTC",
                post_only=False,
                market_mid=Decimal("0.2"),
            )

    def test_cancel_rate_limit(self) -> None:
        cfg = ConfigLoader().load("strategies/doge_usd_grid_live.json")
        cfg.strategy["run_mode"] = "PAPER"
        cfg.strategy["ops"]["metrics"]["tags"]["mode"] = "PAPER"
        cfg.strategy["execution"]["order_limits"]["max_cancels_per_sec"] = 1
        engine = ExecutionEngine(cfg)
        engine.ws.on_connect()
        snap = MarketSnapshot(bid=Decimal("0.2000"), ask=Decimal("0.2002"), ts=time.time())
        engine.on_market_data(snap)
        keys = list(engine.order_manager.managed.keys())[:2]
        engine.order_manager.cancel(keys[0])
        with self.assertRaises(RateLimitExceededError):
            engine.order_manager.cancel(keys[1])

    def test_ws_disconnect_shutdown(self) -> None:
        cfg = ConfigLoader().load("strategies/doge_usd_grid_live.json")
        cfg.strategy["run_mode"] = "PAPER"
        cfg.strategy["ops"]["metrics"]["tags"]["mode"] = "PAPER"
        engine = ExecutionEngine(cfg)
        engine.ws.on_connect()
        engine.ws.on_disconnect()
        engine.ws.health.disconnect_started_at = time.time() - 10
        snap = MarketSnapshot(bid=Decimal("0.2"), ask=Decimal("0.2002"), ts=time.time())
        with self.assertRaises(WebsocketDisconnectError):
            engine.on_market_data(snap)

    def test_duplicate_order_submission_is_deduped(self) -> None:
        cfg = ConfigLoader().load("strategies/doge_usd_grid_live.json")
        cfg.strategy["run_mode"] = "PAPER"
        cfg.strategy["ops"]["metrics"]["tags"]["mode"] = "PAPER"
        engine = ExecutionEngine(cfg)
        engine.ws.on_connect()
        managed1 = engine.order_manager.submit_limit(
            intent_key="dup-intent",
            symbol="DOGE/USD",
            side="BUY",
            price=Decimal("0.2"),
            qty=Decimal("100"),
            tif="GTC",
            post_only=False,
            market_mid=Decimal("0.2001"),
        )
        managed2 = engine.order_manager.submit_limit(
            intent_key="dup-intent",
            symbol="DOGE/USD",
            side="BUY",
            price=Decimal("0.2"),
            qty=Decimal("100"),
            tif="GTC",
            post_only=False,
            market_mid=Decimal("0.2001"),
        )
        self.assertEqual(managed1.status.exchange_order_id, managed2.status.exchange_order_id)

    def test_replace_uses_new_client_order_id(self) -> None:
        cfg = ConfigLoader().load("strategies/doge_usd_grid_live.json")
        cfg.strategy["run_mode"] = "PAPER"
        cfg.strategy["ops"]["metrics"]["tags"]["mode"] = "PAPER"
        engine = ExecutionEngine(cfg)
        engine.ws.on_connect()
        managed = engine.order_manager.submit_limit(
            intent_key="repl-intent",
            symbol="DOGE/USD",
            side="BUY",
            price=Decimal("0.2"),
            qty=Decimal("100"),
            tif="GTC",
            post_only=False,
            market_mid=Decimal("0.2001"),
        )
        old_client_id = managed.order.client_order_id
        repl = engine.order_manager.cancel_replace(
            "repl-intent",
            new_price=Decimal("0.1995"),
            new_qty=Decimal("100"),
            market_mid=Decimal("0.2001"),
        )
        self.assertNotEqual(old_client_id, repl.order.client_order_id)

    def test_transient_503_retries_then_succeeds(self) -> None:
        cfg = ConfigLoader().load("strategies/doge_usd_grid_live.json")
        engine = ExecutionEngine(cfg)
        engine.ws.on_connect()
        engine.adapter.inject_transient_failures(2)
        managed = engine.order_manager.submit_limit(
            intent_key="live-transient",
            symbol="DOGE/USD",
            side="BUY",
            price=Decimal("0.2"),
            qty=Decimal("100"),
            tif="GTC",
            post_only=False,
        )
        self.assertEqual(managed.status.status, "OPEN")

    def test_transient_503_exhausts_retries(self) -> None:
        cfg = ConfigLoader().load("strategies/doge_usd_grid_live.json")
        engine = ExecutionEngine(cfg)
        engine.ws.on_connect()
        engine.adapter.inject_transient_failures(10)
        with self.assertRaises(ExchangeTransientError):
            engine.order_manager.submit_limit(
                intent_key="live-transient-fail",
                symbol="DOGE/USD",
                side="BUY",
                price=Decimal("0.2"),
                qty=Decimal("100"),
                tif="GTC",
                post_only=False,
            )

    def test_restart_restores_positions(self) -> None:
        cfg = ConfigLoader().load("strategies/doge_usd_grid_live.json")
        cfg.strategy["run_mode"] = "PAPER"
        cfg.strategy["ops"]["metrics"]["tags"]["mode"] = "PAPER"
        state_path = tempfile.NamedTemporaryFile(delete=False).name
        cfg.strategy["ops"]["state_store"]["path"] = state_path

        engine = ExecutionEngine(cfg)
        engine.ws.on_connect()
        engine.state.inventory_base = Decimal("50")
        engine.state.inventory_quote = Decimal("-10")
        engine._checkpoint(MarketSnapshot(bid=Decimal("0.2"), ask=Decimal("0.2002"), ts=time.time()))

        engine2 = ExecutionEngine(cfg)
        engine2.reconcile_on_start(cancel_unknown=True)
        self.assertEqual(engine2.state.inventory_base, Decimal("50"))
        self.assertEqual(engine2.state.inventory_quote, Decimal("-10"))

    def test_insufficient_balance_error_bubbles(self) -> None:
        cfg = ConfigLoader().load("strategies/doge_usd_grid_live.json")
        cfg.strategy["run_mode"] = "PAPER"
        cfg.strategy["ops"]["metrics"]["tags"]["mode"] = "PAPER"
        engine = ExecutionEngine(cfg)
        engine.ws.on_connect()
        with self.assertRaises(InsufficientBalanceError):
            engine.order_manager.submit_limit(
                intent_key="insufficient",
                symbol="DOGE/USD",
                side="BUY",
                price=Decimal("10"),
                qty=Decimal("100000"),
                tif="GTC",
                post_only=False,
                market_mid=Decimal("10"),
            )

    def test_volatility_circuit_breaker(self) -> None:
        cfg = ConfigLoader().load("strategies/doge_usd_grid_live.json")
        cfg.strategy["run_mode"] = "PAPER"
        cfg.strategy["ops"]["metrics"]["tags"]["mode"] = "PAPER"
        cfg.strategy["risk"]["circuit_breakers"]["volatility_spike_bps_1m"] = 20
        engine = ExecutionEngine(cfg)
        engine.ws.on_connect()
        engine.on_market_data(MarketSnapshot(bid=Decimal("0.2"), ask=Decimal("0.2002"), ts=time.time()))
        with self.assertRaises(RiskViolation):
            engine.on_market_data(MarketSnapshot(bid=Decimal("0.25"), ask=Decimal("0.2502"), ts=time.time()))


if __name__ == "__main__":
    unittest.main()
