from __future__ import annotations

import os
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

from cryptex.config_loader import ConfigLoader
from cryptex.errors import ConfigError, ExchangeValidationError, MarketDataStaleError
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

    def test_paper_mode_does_not_require_live_keys(self) -> None:
        p = self._tmp_strategy()
        text = p.read_text().replace('"LIVE"', '"PAPER"', 1)
        p.write_text(text)
        os.environ.pop("KRAKEN_API_KEY", None)
        os.environ.pop("KRAKEN_API_SECRET", None)
        cfg = ConfigLoader().load(str(p))
        self.assertEqual(cfg.strategy["run_mode"], "PAPER")

    def test_engine_places_valid_orders_and_persists_state(self) -> None:
        cfg = ConfigLoader().load("strategies/doge_usd_grid_live.json")
        cfg.strategy["run_mode"] = "PAPER"
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
        engine = ExecutionEngine(cfg)
        engine.ws.on_connect()
        snap = MarketSnapshot(bid=Decimal("0.2"), ask=Decimal("0.2002"), ts=time.time() - 30)
        with self.assertRaises(MarketDataStaleError):
            engine.on_market_data(snap)

    def test_precision_rejection(self) -> None:
        cfg = ConfigLoader().load("strategies/doge_usd_grid_live.json")
        cfg.strategy["run_mode"] = "PAPER"
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


if __name__ == "__main__":
    unittest.main()
