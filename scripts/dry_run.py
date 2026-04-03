import os
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptex.config_loader import ConfigLoader
from cryptex.execution_engine import ExecutionEngine, MarketSnapshot


def main() -> None:
    os.environ.setdefault("KRAKEN_API_KEY", "dry-run")
    os.environ.setdefault("KRAKEN_API_SECRET", "dry-run")
    resolved = ConfigLoader().load("strategies/doge_usd_grid_live.json")
    resolved.strategy["run_mode"] = "PAPER"
    resolved.strategy["ops"]["metrics"]["tags"]["mode"] = "PAPER"
    engine = ExecutionEngine(resolved)
    engine.ws.on_connect()

    prices = [Decimal("0.1980"), Decimal("0.2000"), Decimal("0.2030"), Decimal("0.2010")]
    for p in prices:
        snap = MarketSnapshot(bid=p - Decimal("0.0001"), ask=p + Decimal("0.0001"), ts=time.time())
        engine.on_market_data(snap)
        time.sleep(1.05)

    unknown, missing = engine.reconcile_on_start()
    print(
        f"dry_run_ok open_orders={engine.order_manager.open_order_count()} unknown={len(unknown)} missing={len(missing)} "
        f"config_hash={resolved.config_hash[:12]}"
    )


if __name__ == "__main__":
    main()
