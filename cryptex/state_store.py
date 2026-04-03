from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        self.conn.commit()

    def put_json(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, json.dumps(value, sort_keys=True)),
        )
        self.conn.commit()

    def get_json(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        if not row:
            return default
        return json.loads(row[0])

    def save_runtime(
        self,
        *,
        config_hash: str,
        open_orders: list[dict],
        balances: dict,
        positions: dict,
        last_mid: float | None,
    ) -> None:
        self.put_json("config_hash", config_hash)
        self.put_json("open_orders", open_orders)
        self.put_json("balances", balances)
        self.put_json("positions", positions)
        self.put_json("last_mid", last_mid)
        self.put_json("last_checkpoint_ts", time.time())

    def load_runtime(self) -> dict:
        return {
            "config_hash": self.get_json("config_hash"),
            "open_orders": self.get_json("open_orders", []),
            "balances": self.get_json("balances", {}),
            "positions": self.get_json("positions", {}),
            "last_mid": self.get_json("last_mid"),
            "last_checkpoint_ts": self.get_json("last_checkpoint_ts"),
        }
