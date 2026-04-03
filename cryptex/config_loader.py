from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .schema_validator import SchemaValidator

SENSITIVE_FIELD_MARKERS = {"key", "secret", "token", "password", "passphrase"}


@dataclass(frozen=True)
class EnvConfig:
    kraken_api_key: str | None
    kraken_api_secret: str | None


@dataclass(frozen=True)
class ResolvedConfig:
    strategy: dict[str, Any]
    env: EnvConfig
    config_hash: str

    def redacted_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "env": {
                "kraken_api_key": "***" if self.env.kraken_api_key else None,
                "kraken_api_secret": "***" if self.env.kraken_api_secret else None,
            },
            "config_hash": self.config_hash,
        }


class ConfigLoader:
    def __init__(self, schema_path: str = "schemas/strategy.schema.json") -> None:
        self.schema_path = Path(schema_path)
        if not self.schema_path.exists():
            raise ConfigError(f"schema file missing: {schema_path}")
        self.schema = json.loads(self.schema_path.read_text())

    def load(self, strategy_path: str) -> ResolvedConfig:
        path = Path(strategy_path)
        if not path.exists():
            raise ConfigError(f"strategy file not found: {strategy_path}")
        try:
            strategy = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid JSON in {strategy_path}: {exc}") from exc

        self._assert_no_embedded_secrets(strategy)
        self._validate_schema(strategy)
        self._validate_cross_field_constraints(strategy)
        self._apply_defaults(strategy)

        serialized = json.dumps(strategy, sort_keys=True, separators=(",", ":"))
        config_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()

        env = EnvConfig(
            kraken_api_key=os.getenv("KRAKEN_API_KEY"),
            kraken_api_secret=os.getenv("KRAKEN_API_SECRET"),
        )

        if strategy["run_mode"] == "LIVE" and (not env.kraken_api_key or not env.kraken_api_secret):
            raise ConfigError("LIVE mode requires KRAKEN_API_KEY and KRAKEN_API_SECRET in environment")

        return ResolvedConfig(strategy=strategy, env=env, config_hash=config_hash)

    def _validate_schema(self, strategy: dict[str, Any]) -> None:
        issues = SchemaValidator().validate(strategy, self.schema)
        if issues:
            rendered = "\n".join(f" - {issue.path}: {issue.message}" for issue in issues)
            raise ConfigError(f"strategy schema validation failed:\n{rendered}")

    def _validate_cross_field_constraints(self, strategy: dict[str, Any]) -> None:
        levels = strategy["grid"]["levels"]
        max_open = strategy["execution"]["order_limits"]["max_open_orders"]
        if max_open < levels * 2:
            raise ConfigError(
                f"execution.order_limits.max_open_orders ({max_open}) must be >= 2 * grid.levels ({levels*2})"
            )

        if strategy["run_mode"] == "PAPER" and not strategy["market"].get("paper_trading_supported", False):
            raise ConfigError("paper mode requested but market.paper_trading_supported=false")

    def _apply_defaults(self, strategy: dict[str, Any]) -> None:
        strategy.setdefault("execution", {}).setdefault("retry", {})
        strategy["execution"]["retry"].setdefault("max_retries", 3)
        strategy["execution"]["retry"].setdefault("retry_backoff_ms", 200)

    def _assert_no_embedded_secrets(self, payload: Any, path: str = "$") -> None:
        if isinstance(payload, dict):
            for key, value in payload.items():
                normalized = key.lower()
                if any(marker in normalized for marker in SENSITIVE_FIELD_MARKERS):
                    raise ConfigError(f"secret-like field '{path}.{key}' is not allowed in strategy JSON")
                self._assert_no_embedded_secrets(value, f"{path}.{key}")
        elif isinstance(payload, list):
            for idx, item in enumerate(payload):
                self._assert_no_embedded_secrets(item, f"{path}[{idx}]")
